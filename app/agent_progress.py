"""Hiérarchie immuable et progression technique calculée côté API."""

from collections import defaultdict

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.agent_models import AgentPlanVersion, AgentSession, AgentTask, AgentTaskClaim
from app.identity_models import utcnow
from app.identity_service import audit


def hierarchy_depths(tasks: list[dict]) -> dict[str, int]:
    """Valide la forêt complète avant insertion, indépendamment de l'ordre fourni."""
    if not 1 <= len(tasks) <= 20:
        raise ValueError("Le plan doit contenir de 1 à 20 tâches.")
    by_key = {item["task_key"]: item for item in tasks}
    if len(by_key) != len(tasks):
        raise ValueError("Clés de tâches dupliquées.")
    depths = {}
    visiting = set()

    def visit(key):
        if key in visiting:
            raise ValueError("Cycle dans la hiérarchie des tâches.")
        if key in depths:
            return depths[key]
        if key not in by_key:
            raise ValueError("Parent absent de la proposition de plan.")
        visiting.add(key)
        parent = by_key[key].get("parent_task_key")
        depth = 1 if parent is None else visit(parent) + 1
        if depth > 3:
            raise ValueError("La hiérarchie est limitée à trois niveaux, racine comprise.")
        visiting.remove(key)
        depths[key] = depth
        return depth

    for key in by_key:
        visit(key)
    return depths


async def lock_task_plan(db: AsyncSession, task_id: str, project_id: str) -> AgentPlanVersion:
    plan_id = await db.scalar(select(AgentTask.plan_version_id).where(
        AgentTask.id == task_id, AgentTask.project_id == project_id))
    # FOR NO KEY UPDATE : même exclusion des mutations, compatible avec les FK de suspension.
    plan = await db.scalar(select(AgentPlanVersion).where(
        AgentPlanVersion.id == plan_id, AgentPlanVersion.project_id == project_id).with_for_update(key_share=True))
    if plan is None:
        raise ValueError("Tâche indisponible dans le projet.")
    return plan


async def aggregate_plan(db: AsyncSession, plan_id: str, *, session_id: str | None = None, actor_id: str | None = None) -> None:
    """Appel sous verrou du plan : remonte les enfants vers la racine atomiquement."""
    tasks = (await db.scalars(select(AgentTask).where(
        AgentTask.plan_version_id == plan_id))).all()
    children = defaultdict(list)
    for task in tasks:
        children[task.parent_task_id].append(task)
    for task in sorted(tasks, key=lambda item: item.depth, reverse=True):
        direct = children.get(task.id)
        if not direct:
            continue
        progress = sum(child.progress_percent for child in direct) // len(direct)
        if all(child.status == "completed" for child in direct):
            status = "completed"
        elif all(child.status in {"completed", "cancelled"} for child in direct):
            status = "cancelled"
        elif any(child.status == "failed" for child in direct):
            status = "failed"
        elif any(child.progress_percent or child.status in {"claimed", "running", "completed"} for child in direct):
            status = "running"
        else:
            status = "ready"
        if (task.progress_percent, task.status) == (progress, status):
            continue
        previous = {"status": task.status, "progress_percent": task.progress_percent}
        task.status, task.progress_percent = status, progress
        audit(db, "agent.task.aggregate", "success", actor_id=actor_id, project_id=task.project_id,
              target_type="agent_task", target_id=task.id,
              details={"session_id": session_id, "previous": previous,
                       "status": status, "progress_percent": progress})
    await db.flush()


def task_view(task: AgentTask) -> dict:
    return {"id": task.id, "plan_id": task.plan_version_id, "task_key": task.task_key,
            "title": task.title, "action_class": task.action_class, "parent_task_id": task.parent_task_id,
            "depth": task.depth, "skill_profile_id": task.skill_profile_id,
            "priority": task.priority, "due_at": task.due_at,
            "status": task.status, "progress_percent": task.progress_percent,
            "dependency_keys": task.dependency_keys, "attempt_count": task.attempt_count,
            "retry_not_before": task.retry_not_before}


async def report_progress(db: AsyncSession, *, session: AgentSession, task_id: str,
                          claim_id: str, progress_percent: int) -> AgentTask:
    if type(progress_percent) is not int or not 0 <= progress_percent <= 100:
        raise ValueError("Progression entière attendue entre 0 et 100.")
    if session.status != "active" or session.expires_at <= utcnow():
        raise ValueError("Session inactive ou expirée.")
    plan = await lock_task_plan(db, task_id, session.project_id)
    from app.agent_routing import require_approved_execution
    await require_approved_execution(db, plan, session.project_id, session.team_id)
    task = await db.scalar(select(AgentTask).where(AgentTask.id == task_id).with_for_update())
    if task.team_id != session.team_id or task.skill_profile_id != session.skill_profile_id:
        raise ValueError("Tâche incompatible avec la session.")
    if await db.scalar(select(AgentTask.id).where(AgentTask.parent_task_id == task.id).limit(1)):
        raise ValueError("La progression d'un parent est calculée depuis ses enfants.")
    claim = await db.scalar(select(AgentTaskClaim).where(
        AgentTaskClaim.id == claim_id, AgentTaskClaim.task_id == task.id,
        AgentTaskClaim.session_id == session.id).with_for_update())
    if claim is None:
        raise ValueError("Réservation incompatible.")
    if (task.status == "completed" and progress_percent == 100
            and claim.released_at is not None and claim.release_reason == "completed"):
        return task
    now = utcnow()
    if (task.status != "claimed" or task.claimed_session_id != session.id
            or claim.released_at is not None or not task.lease_expires_at
            or task.lease_expires_at <= now or claim.lease_expires_at <= now):
        raise ValueError("Réservation absente ou périmée.")
    if progress_percent < task.progress_percent:
        raise ValueError("La progression ne peut pas diminuer.")
    from app.runtime_service import guard_progress
    await guard_progress(db, claim, task, session)
    if progress_percent == task.progress_percent:
        return task
    previous = {"status": task.status, "progress_percent": task.progress_percent}
    task.progress_percent = progress_percent
    if progress_percent == 100:
        task.status = "completed"
        task.claimed_session_id = None
        task.lease_expires_at = None
        claim.released_at = now
        claim.release_reason = "completed"
        from app.agent_coordinator import close_attempt
        await close_attempt(db, claim, "completed")
    audit(db, "agent.task.progress", "success", project_id=task.project_id,
          target_type="agent_task", target_id=task.id,
          details={"session_id": session.id, "claim_id": claim.id, "previous": previous,
                   "status": task.status, "progress_percent": progress_percent})
    await db.flush()
    await aggregate_plan(db, plan.id, session_id=session.id)
    return task
