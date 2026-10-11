"""Portes humaines d'une tentative, sans délégation de permissions métier."""
from sqlalchemy import select

from app.agent_models import AgentActionApproval, AgentTask
from app.agent_progress import lock_task_plan
from app.identity_models import utcnow
from app.identity_service import audit


def effective_action_class(task, tasks):
    by_id = {t.id: t for t in tasks}
    current = task
    while current:
        if current.action_class == "sensitive":
            return "sensitive"
        current = by_id.get(current.parent_task_id)
    return "standard"


def approval_required(task, tasks, plan):
    return plan.autonomy_mode == "per_action" or (
        plan.autonomy_mode == "sensitive" and effective_action_class(task, tasks) == "sensitive")


def approval_view(approval):
    return {"id": approval.id, "task_id": approval.task_id, "attempt_number": approval.attempt_number,
            "autonomy_mode": approval.autonomy_mode, "action_class": approval.action_class,
            "status": approval.status, "requested_by": approval.requested_by,
            "requested_session_id": approval.requested_session_id, "created_at": approval.created_at,
            "decision_result": approval.decision_result, "decided_by": approval.decided_by,
            "decided_at": approval.decided_at, "consumed_claim_id": approval.consumed_claim_id,
            "invalidated_at": approval.invalidated_at, "invalidation_reason": approval.invalidation_reason}


async def active_approval(db, task):
    return await db.scalar(select(AgentActionApproval).where(
        AgentActionApproval.task_id == task.id, AgentActionApproval.attempt_number == task.attempt_count + 1,
        AgentActionApproval.status.in_(["pending", "approved"])).execution_options(populate_existing=True))


async def ensure_approval(db, task, tasks, plan, *, actor_id=None, session_id=None):
    """Le verrou de plan est détenu ; un appel technique ne peut qu'ouvrir une demande."""
    if not approval_required(task, tasks, plan):
        return None
    existing = await active_approval(db, task)
    if existing:
        return existing
    approval = AgentActionApproval(task_id=task.id, attempt_number=task.attempt_count + 1,
        autonomy_mode=plan.autonomy_mode, action_class=effective_action_class(task, tasks),
        status="pending", requested_by=actor_id, requested_session_id=session_id)
    db.add(approval)
    await db.flush()
    audit(db, "agent.approval.request", "success", actor_id=actor_id, project_id=task.project_id,
        target_type="agent_action_approval", target_id=approval.id,
        details={"task_id": task.id, "attempt_number": approval.attempt_number, "session_id": session_id,
                 "autonomy_mode": plan.autonomy_mode, "action_class": approval.action_class})
    return approval


async def request_approval(db, project_id, task_id, attempt_number, actor_id):
    from app.agent_coordinator import plan_tasks
    from app.agent_routing import require_approved_execution
    plan = await lock_task_plan(db, task_id, project_id)
    await require_approved_execution(db, plan, project_id, plan.team_id)
    tasks = await plan_tasks(db, plan.id)
    task = next(t for t in tasks if t.id == task_id)
    if (task.status != "ready" or any(t.parent_task_id == task.id for t in tasks)
            or attempt_number != task.attempt_count + 1 or task.attempt_count >= plan.max_attempts):
        raise ValueError("Feuille prête et numéro de prochaine tentative requis.")
    if not approval_required(task, tasks, plan):
        raise ValueError("Cette tâche ne requiert pas d'approbation d'action.")
    return await ensure_approval(db, task, tasks, plan, actor_id=actor_id)


async def decide_approval(db, project_id, task_id, approval_id, decision, actor_id):
    from app.agent_coordinator import cancel_tasks, plan_tasks
    from app.agent_routing import require_approved_execution
    if decision not in {"approve", "reject"}:
        raise ValueError("Décision invalide.")
    plan = await lock_task_plan(db, task_id, project_id)
    task = await db.scalar(select(AgentTask).where(AgentTask.id == task_id).with_for_update())
    approval = await db.scalar(select(AgentActionApproval).where(
        AgentActionApproval.id == approval_id, AgentActionApproval.task_id == task_id).with_for_update())
    if approval is None:
        raise ValueError("Demande introuvable pour cette tâche.")
    result = "approved" if decision == "approve" else "rejected"
    # Un rejeu rapporte la décision conservée, sans autoriser un nouveau claim.
    if approval.decision_result == result:
        return approval
    if approval.status != "pending":
        raise ValueError("Décision déjà prise ou demande invalidée.")
    await require_approved_execution(db, plan, project_id, plan.team_id)
    tasks = await plan_tasks(db, plan.id)
    if (task.status != "ready" or approval.attempt_number != task.attempt_count + 1
            or task.attempt_count >= plan.max_attempts or not approval_required(task, tasks, plan)
            or approval.autonomy_mode != plan.autonomy_mode
            or approval.action_class != effective_action_class(task, tasks)):
        raise ValueError("Périmètre ou tentative périmés.")
    approval.status = approval.decision_result = result
    approval.decided_by, approval.decided_at = actor_id, utcnow()
    audit(db, f"agent.approval.{decision}", "success", actor_id=actor_id, project_id=project_id,
        target_type="agent_action_approval", target_id=approval.id,
        details={"task_id": task_id, "attempt_number": approval.attempt_number})
    await db.flush()
    if result == "rejected":
        await cancel_tasks(db, plan, [task], actor_id)
    return approval


async def consume_approval(db, approval, claim, task):
    if approval is None:
        return
    if approval.status != "approved" or approval.attempt_number != task.attempt_count:
        raise ValueError("Accord humain absent ou périmé.")
    approval.status, approval.consumed_claim_id = "consumed", claim.id
    audit(db, "agent.approval.consume", "success", project_id=task.project_id,
        target_type="agent_action_approval", target_id=approval.id,
        details={"task_id": task.id, "claim_id": claim.id, "session_id": claim.session_id,
                 "attempt_number": task.attempt_count})


async def invalidate_approvals(db, tasks, actor_id, reason):
    if not tasks:
        return
    approvals = (await db.scalars(select(AgentActionApproval).where(
        AgentActionApproval.task_id.in_([t.id for t in tasks]),
        AgentActionApproval.status.in_(["pending", "approved"]))
        .order_by(AgentActionApproval.id).with_for_update())).all()
    project_id = tasks[0].project_id
    for approval in approvals:
        approval.status, approval.invalidated_at, approval.invalidation_reason = "invalidated", utcnow(), reason
        audit(db, "agent.approval.invalidate", "success", actor_id=actor_id, project_id=project_id,
            target_type="agent_action_approval", target_id=approval.id,
            details={"task_id": approval.task_id, "attempt_number": approval.attempt_number, "reason": reason})
    await db.flush()
