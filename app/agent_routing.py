"""Routage et réservations pour un plan humain issu d'un flux validé."""

from datetime import timedelta

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.agent_models import AgentPlanVersion, AgentSession, AgentTask, AgentTaskClaim, AgentTaskRun, FlowRun, FlowVersion
from app.agent_progress import aggregate_plan, lock_task_plan
from app.identity_models import utcnow
from app.identity_service import audit


LEASE_SECONDS = 120


async def _release_task(db: AsyncSession, task: AgentTask, reason: str) -> bool:
    task = await db.scalar(select(AgentTask).where(AgentTask.id == task.id).with_for_update()
                           .execution_options(populate_existing=True))
    if reason == "lease_expired" and task.lease_expires_at and task.lease_expires_at > utcnow():
        return False
    if task.status != "claimed" or not task.claimed_session_id:
        return False
    now = utcnow()
    claim = await db.scalar(select(AgentTaskClaim).where(
        AgentTaskClaim.task_id == task.id, AgentTaskClaim.session_id == task.claimed_session_id,
        AgentTaskClaim.released_at.is_(None)).with_for_update())
    old_session_id = task.claimed_session_id
    plan = await db.get(AgentPlanVersion, task.plan_version_id)
    task.status = "failed" if task.attempt_count >= plan.max_attempts else "ready"
    task.claimed_session_id = None
    task.lease_expires_at = None
    if claim is not None:
        claim.released_at = now
        claim.release_reason = reason
        from app.agent_coordinator import close_attempt, retry_delay
        await close_attempt(db, claim, 'cancelled' if reason in {'voluntary', 'paused', 'cancelled', 'session_revoked'} else 'failed',
                            'lease_expired' if reason == 'lease_expired' else None)
        if reason in {'lease_expired', 'session_expired'}:
            task.retry_not_before = now + retry_delay(task.attempt_count)
    audit(db, "agent.task.release", "success", project_id=task.project_id,
          target_type="agent_task", target_id=task.id,
          details={"session_id": old_session_id, "reason": reason})
    await db.flush()
    await aggregate_plan(db, task.plan_version_id, session_id=old_session_id)
    return True


async def release_session_claims(db: AsyncSession, session: AgentSession, reason: str) -> int:
    plan_ids = (await db.scalars(select(AgentTask.plan_version_id).where(
        AgentTask.claimed_session_id == session.id, AgentTask.status == "claimed").distinct()
        .order_by(AgentTask.plan_version_id))).all()
    for plan_id in plan_ids:
        await db.scalar(select(AgentPlanVersion).where(
            AgentPlanVersion.id == plan_id).with_for_update(key_share=True))
    tasks = (await db.scalars(select(AgentTask).where(
        AgentTask.claimed_session_id == session.id, AgentTask.status == "claimed")
        .order_by(AgentTask.id).with_for_update())).all()
    count = 0
    for task in tasks:
        count += bool(await _release_task(db, task, reason))
    return count


async def extend_session_claims(db: AsyncSession, session: AgentSession) -> None:
    new_deadline = min(utcnow() + timedelta(seconds=LEASE_SECONDS), session.expires_at)
    tasks = (await db.scalars(select(AgentTask).where(
        AgentTask.claimed_session_id == session.id, AgentTask.status == "claimed")
        .order_by(AgentTask.id).with_for_update())).all()
    for task in tasks:
        claim = await db.scalar(select(AgentTaskClaim).where(
            AgentTaskClaim.task_id == task.id, AgentTaskClaim.session_id == session.id,
            AgentTaskClaim.released_at.is_(None)).with_for_update())
        if claim is not None:
            from app.runtime_models import RuntimeSelection
            runtime_deadline=await db.scalar(select(RuntimeSelection.deadline).join(AgentTaskRun,
                AgentTaskRun.id==RuntimeSelection.attempt_id).where(AgentTaskRun.claim_id==claim.id))
            deadline=min(new_deadline,runtime_deadline) if runtime_deadline else new_deadline
            task.lease_expires_at=claim.lease_expires_at=deadline
        else:
            task.lease_expires_at=new_deadline
    if tasks:
        await db.flush()


async def require_approved_execution(db: AsyncSession, plan: AgentPlanVersion | None,
                                     project_id: str, team_id: str) -> None:
    if (plan is None or plan.project_id != project_id or plan.team_id != team_id
            or plan.status != "approved" or not plan.validated_flow_run_id
            or not plan.approved_by or not plan.approved_at):
        raise ValueError("Plan non approuvé ou hors projet.")
    run = await db.get(FlowRun, plan.validated_flow_run_id)
    version = await db.get(FlowVersion, run.flow_version_id) if run else None
    if (run is None or run.project_id != project_id or run.team_id != team_id
            or run.status != "running" or version is None or version.status != "validated"
            or not version.validated_by or not version.validated_at):
        raise ValueError("Exécution de flux non validée ou inactive.")


async def reserve_task(db: AsyncSession, *, session: AgentSession, task_id: str, request_key: str | None = None) -> AgentTaskClaim:
    """Sérialise un claim et vérifie les preuves persistées de validation humaine."""
    if session.status != "active" or session.expires_at <= utcnow():
        raise ValueError("Session inactive ou expirée.")
    plan = await lock_task_plan(db, task_id, session.project_id)
    task = await db.scalar(select(AgentTask).where(
        AgentTask.id == task_id, AgentTask.project_id == session.project_id).with_for_update())
    if task is None or task.team_id != session.team_id or task.skill_profile_id != session.skill_profile_id:
        raise ValueError("Tâche incompatible avec la session.")
    await require_approved_execution(db, plan, task.project_id, task.team_id)
    from app.budget_service import guard_plan
    await guard_plan(db, plan)
    if await db.scalar(select(AgentTask.id).where(AgentTask.parent_task_id == task.id).limit(1)):
        raise ValueError("Seules les feuilles du plan peuvent être réservées.")
    from app.agent_coordinator import settle_expired, plan_tasks, blockers
    await settle_expired(db, plan)
    now = utcnow()
    if session.expires_at <= now:
        raise ValueError("Session expirée.")
    if task.status == "claimed" and task.lease_expires_at and task.lease_expires_at <= now:
        await _release_task(db, task, "lease_expired")
    if task.status == "claimed" and task.claimed_session_id == session.id:
        claim = await db.scalar(select(AgentTaskClaim).where(
            AgentTaskClaim.task_id == task.id, AgentTaskClaim.session_id == session.id,
            AgentTaskClaim.released_at.is_(None)))
        if claim is not None:
            return claim
    if task.status != "ready":
        raise ValueError("Tâche déjà réservée ou non prête.")
    from app.runtime_service import ensure_task_recoverable
    await ensure_task_recoverable(db,task.id)
    tasks = await plan_tasks(db, plan.id)
    reasons = blockers(task, tasks, plan)
    if reasons:
        raise ValueError("Tâche bloquée : " + ", ".join(reasons))
    from app.agent_autonomy import ensure_approval, consume_approval
    approval = await ensure_approval(db, task, tasks, plan, session_id=session.id)
    if approval is not None and approval.status != "approved":
        raise ValueError("Tâche suspendue : approval_pending.")
    previous = await db.scalar(select(AgentTaskRun).where(AgentTaskRun.task_id == task.id)
                              .order_by(AgentTaskRun.attempt_number.desc()).limit(1))
    deadline = min(now + timedelta(seconds=LEASE_SECONDS), session.expires_at)
    claim = AgentTaskClaim(task_id=task.id, session_id=session.id,
                           claimed_at=now, lease_expires_at=deadline, request_key=request_key)
    db.add(claim)
    task.attempt_count += 1
    task.status = "claimed"
    task.claimed_session_id = session.id
    task.lease_expires_at = deadline
    await db.flush()
    await consume_approval(db, approval, claim, task)
    attempt = AgentTaskRun(task_id=task.id, claim_id=claim.id, attempt_number=task.attempt_count,
                           approval_id=approval.id if approval else None,
                           parent_run_id=previous.id if previous else None, status="running", started_at=now)
    db.add(attempt)
    await db.flush()
    audit(db, "agent.task.claim", "success", project_id=task.project_id,
          target_type="agent_task", target_id=task.id,
          details={"session_id": session.id, "claim_id": claim.id,
                   "skill_profile_id": session.skill_profile_id, "attempt_id": attempt.id})
    await aggregate_plan(db, plan.id, session_id=session.id)
    return claim


async def release_own_task(db: AsyncSession, *, session: AgentSession, task_id: str) -> bool:
    try:
        await lock_task_plan(db, task_id, session.project_id)
    except ValueError:
        return False
    task = await db.scalar(select(AgentTask).where(
        AgentTask.id == task_id, AgentTask.project_id == session.project_id).with_for_update())
    if task is None or task.claimed_session_id != session.id:
        return False
    return await _release_task(db, task, "voluntary")
