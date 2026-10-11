"""Coordinateur borné des sessions : DAG, créneaux, tentatives et contrôles humains."""
from collections import defaultdict
from datetime import datetime, timedelta, timezone

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.agent_models import AgentPlanVersion, AgentSession, AgentTask, AgentTaskClaim, AgentTaskRun, FlowRun
from app.agent_progress import aggregate_plan, lock_task_plan
from app.identity_models import utcnow
from app.identity_service import audit


def leaf_dependencies(tasks: list[dict]) -> dict[str, set[str]]:
    by_key = {t['task_key']: t for t in tasks}
    children = defaultdict(list)
    for t in tasks:
        children[t.get('parent_task_key')].append(t['task_key'])
        deps = t.get('dependency_keys', [])
        if len(deps) != len(set(deps)) or any(k not in by_key for k in deps):
            raise ValueError('Dépendance absente ou dupliquée.')
    def leaves(key, path=frozenset()):
        if key in path:
            raise ValueError('Cycle de hiérarchie.')
        return {key} if not children[key] else set().union(*(leaves(c, path | {key}) for c in children[key]))
    result = {}
    for key in by_key:
        if children[key]:
            continue
        deps, current, visited = set(), key, set()
        while current is not None:
            if current in visited or current not in by_key:
                raise ValueError('Hiérarchie invalide.')
            visited.add(current)
            for target in by_key[current].get('dependency_keys', []):
                deps.update(leaves(target))
            current = by_key[current].get('parent_task_key')
        result[key] = deps
    done, visiting = set(), set()
    def visit(key):
        if key in visiting:
            raise ValueError('Cycle dans les dépendances effectives.')
        if key in done:
            return
        visiting.add(key)
        for dep in result[key]:
            visit(dep)
        visiting.remove(key)
        done.add(key)
    for key in result:
        visit(key)
    return result


async def plan_tasks(db, plan_id):
    return list((await db.scalars(select(AgentTask).where(AgentTask.plan_version_id == plan_id).order_by(AgentTask.id)
        .execution_options(populate_existing=True))).all())


def dependency_graph(tasks):
    keys = {t.id: t.task_key for t in tasks}
    return leaf_dependencies([{'task_key': t.task_key, 'parent_task_key': keys.get(t.parent_task_id),
                               'dependency_keys': t.dependency_keys} for t in tasks])


def blockers(task, tasks, plan, run_status='running'):
    graph = dependency_graph(tasks)
    by_key = {t.task_key: t for t in tasks}
    if task.task_key not in graph:
        return ['parent']
    reasons = [f'dependency:{k}' for k in sorted(graph[task.task_key]) if by_key[k].status != 'completed']
    if run_status != 'running' or plan.status != 'approved': reasons.append('run_inactive')
    if task.status != 'ready': reasons.append(f'status:{task.status}')
    if task.attempt_count >= plan.max_attempts: reasons.append('attempt_limit')
    if task.retry_not_before and task.retry_not_before > utcnow(): reasons.append('retry_delay')
    if sum(t.status == 'claimed' for t in tasks) >= plan.max_parallel_tasks: reasons.append('parallel_limit')
    return reasons


def retry_delay(number):
    return timedelta(seconds=min(60, 2 ** min(number, 6)))


async def close_attempt(db, claim, status, error_code=None):
    attempt = await db.scalar(select(AgentTaskRun).where(AgentTaskRun.claim_id == claim.id).with_for_update())
    if attempt and attempt.status == 'running':
        from app.runtime_service import finish_client
        await finish_client(db, attempt, status)
        attempt.status, attempt.error_code, attempt.ended_at = status, error_code, utcnow()
    return attempt


async def settle_expired(db, plan):
    from app.agent_routing import _release_task
    for task in await plan_tasks(db, plan.id):
        if task.status == 'claimed' and task.lease_expires_at and task.lease_expires_at <= utcnow():
            await _release_task(db, task, 'lease_expired')


async def ready_for_session(db, session):
    from app.agent_routing import require_approved_execution
    from app.agent_autonomy import ensure_approval
    plans = (await db.scalars(select(AgentPlanVersion).where(
        AgentPlanVersion.project_id == session.project_id, AgentPlanVersion.team_id == session.team_id,
        AgentPlanVersion.status == 'approved').order_by(AgentPlanVersion.id).with_for_update(key_share=True))).all()
    candidates = []
    for plan in plans:
        try:
            await require_approved_execution(db, plan, session.project_id, session.team_id)
            from app.budget_service import guard_plan
            await guard_plan(db, plan)
        except ValueError:
            continue
        await settle_expired(db, plan)
        tasks = await plan_tasks(db, plan.id)
        for task in tasks:
            if task.skill_profile_id != session.skill_profile_id or blockers(task, tasks, plan):
                continue
            from app.runtime_service import ensure_task_recoverable, RuntimeDenied
            try:
                await ensure_task_recoverable(db,task.id)
            except RuntimeDenied:
                continue
            approval = await ensure_approval(db, task, tasks, plan, session_id=session.id)
            if approval is None or approval.status == 'approved':
                candidates.append(task)
    return sorted(candidates, key=lambda t: (t.priority, t.due_at or datetime.max.replace(tzinfo=timezone.utc), t.task_key, t.id))


async def claim_next(db, session, request_key):
    # Même ordre que l'API : session puis plan. La clé ne réserve jamais deux tâches.
    await db.scalar(select(AgentSession).where(AgentSession.id == session.id).with_for_update())
    prior = await db.scalar(select(AgentTaskClaim).where(
        AgentTaskClaim.session_id == session.id, AgentTaskClaim.request_key == request_key))
    if prior:
        return prior
    candidates = await ready_for_session(db, session)
    if not candidates:
        raise ValueError('Aucune tâche prête compatible.')
    from app.agent_routing import reserve_task
    return await reserve_task(db, session=session, task_id=candidates[0].id, request_key=request_key)


async def fail_task(db, session, task_id, claim_id, error_code):
    from app.agent_routing import require_approved_execution
    plan = await lock_task_plan(db, task_id, session.project_id)
    await require_approved_execution(db, plan, session.project_id, session.team_id)
    task = await db.get(AgentTask, task_id)
    claim = await db.scalar(select(AgentTaskClaim).where(AgentTaskClaim.id == claim_id,
        AgentTaskClaim.task_id == task.id, AgentTaskClaim.session_id == session.id).with_for_update())
    attempt = await db.scalar(select(AgentTaskRun).where(AgentTaskRun.claim_id == claim_id))
    if (task.team_id != session.team_id or task.skill_profile_id != session.skill_profile_id or claim is None or attempt is None):
        raise ValueError('Réservation incompatible.')
    if claim.release_reason == 'failed' and attempt.error_code == error_code and task.status == 'failed':
        latest = await db.scalar(select(AgentTaskRun.id).where(AgentTaskRun.task_id == task.id)
            .order_by(AgentTaskRun.attempt_number.desc()).limit(1))
        if latest == attempt.id: return task
    if (task.status != 'claimed' or task.claimed_session_id != session.id or claim.released_at
            or not task.lease_expires_at or task.lease_expires_at <= utcnow()
            or session.status != 'active' or session.expires_at <= utcnow()):
        raise ValueError('Réservation absente ou périmée.')
    claim.released_at, claim.release_reason = utcnow(), 'failed'
    await close_attempt(db, claim, 'failed', error_code)
    task.status, task.claimed_session_id, task.lease_expires_at = 'failed', None, None
    task.retry_not_before = claim.released_at + retry_delay(task.attempt_count)
    audit(db, 'agent.task.fail', 'success', project_id=task.project_id, target_type='agent_task', target_id=task.id,
          details={'session_id': session.id, 'attempt_id': attempt.id, 'error_code': error_code})
    await db.flush()
    await aggregate_plan(db, plan.id, session_id=session.id)
    return task


async def retry_task(db, project_id, task_id, attempt_id, actor_id):
    from app.agent_routing import require_approved_execution
    plan = await lock_task_plan(db, task_id, project_id)
    await require_approved_execution(db, plan, project_id, plan.team_id)
    task = await db.get(AgentTask, task_id)
    if task.retry_from_run_id == attempt_id:
        return task
    latest = await db.scalar(select(AgentTaskRun).where(AgentTaskRun.task_id == task.id)
        .order_by(AgentTaskRun.attempt_number.desc()).limit(1))
    if (task.status != 'failed' or latest is None or latest.id != attempt_id
            or latest.status not in {'failed', 'cancelled'} or task.attempt_count >= plan.max_attempts):
        raise ValueError('Tentative périmée, tâche non échouée ou budget épuisé.')
    task.status, task.retry_from_run_id = 'ready', attempt_id
    audit(db, 'agent.task.retry', 'success', actor_id=actor_id, project_id=project_id,
          target_type='agent_task', target_id=task.id, details={'attempt_id': attempt_id})
    await db.flush()
    await aggregate_plan(db, plan.id, actor_id=actor_id)
    return task


async def cancel_tasks(db, plan, tasks, actor_id):
    from app.agent_routing import _release_task
    from app.agent_autonomy import invalidate_approvals
    await invalidate_approvals(db, tasks, actor_id, 'cancelled')
    parents = {t.parent_task_id for t in await plan_tasks(db, plan.id) if t.parent_task_id}
    for task in tasks:
        if task.id in parents: continue
        if task.status in {'completed', 'cancelled'}: continue
        if task.status == 'claimed': await _release_task(db, task, 'cancelled')
        task.status = 'cancelled'
        audit(db, 'agent.task.cancel', 'success', actor_id=actor_id, project_id=task.project_id,
              target_type='agent_task', target_id=task.id)
    await db.flush()
    await aggregate_plan(db, plan.id, actor_id=actor_id)


async def cancel_subtree(db, project_id, task_id, actor_id):
    plan = await lock_task_plan(db, task_id, project_id)
    run = await db.get(FlowRun, plan.validated_flow_run_id)
    if plan.status != 'approved' or not run or run.status not in {'running', 'paused', 'cancelled'}:
        raise ValueError('Plan non exécutable.')
    tasks = await plan_tasks(db, plan.id)
    selected = {task_id}
    for task in sorted(tasks, key=lambda t: t.depth):
        if task.parent_task_id in selected: selected.add(task.id)
    await cancel_tasks(db, plan, [t for t in tasks if t.id in selected], actor_id)
    return await db.get(AgentTask, task_id)


async def control_plan(db, project_id, plan_id, action, actor_id):
    plan = await db.scalar(select(AgentPlanVersion).where(AgentPlanVersion.id == plan_id,
        AgentPlanVersion.project_id == project_id).with_for_update(key_share=True))
    if not plan or plan.status != 'approved': raise ValueError('Plan approuvé requis.')
    run = await db.scalar(select(FlowRun).where(FlowRun.id == plan.validated_flow_run_id).with_for_update())
    if not run or run.project_id != project_id: raise ValueError('Run indisponible.')
    target = {'pause': 'paused', 'resume': 'running', 'cancel': 'cancelled'}[action]
    if run.status == target: return run
    if run.status not in ({'paused'} if action == 'resume' else {'running', 'paused'}):
        raise ValueError('Transition de run interdite.')
    tasks = await plan_tasks(db, plan.id)
    if action == 'cancel':
        await cancel_tasks(db, plan, tasks, actor_id)
    elif action == 'pause':
        from app.agent_routing import _release_task
        from app.agent_autonomy import invalidate_approvals
        for task in tasks:
            if task.status == 'claimed': await _release_task(db, task, 'paused')
        await invalidate_approvals(db, tasks, actor_id, 'paused')
    run.status = target
    audit(db, f'agent.run.{action}', 'success', actor_id=actor_id, project_id=project_id,
          target_type='flow_run', target_id=run.id)
    return run
