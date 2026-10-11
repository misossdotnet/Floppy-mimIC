"""Délégation documentaire d'un claim approuvé au worker LLM existant."""

from sqlalchemy import select

from app.agent_models import AgentSession, AgentTask, AgentTaskClaim, AgentTaskRun, AgentTeam, AgentTeamSkill, SkillProfile
from app.agent_progress import lock_task_plan, report_progress
from app.agent_routing import require_approved_execution
from app.business_models import GenerationRun
from app.generation_service import start_generation
from app.identity_models import Project, User, utcnow
from app.identity_service import audit, project_permission
from app.llm_models import LLMProfileVersion
from app.llm_trace import redact


async def execution_scope(db, attempt):
    claim = await db.get(AgentTaskClaim, attempt.claim_id)
    session = await db.scalar(select(AgentSession).where(AgentSession.id == claim.session_id)
                              .with_for_update().execution_options(populate_existing=True))
    task = await db.get(AgentTask, attempt.task_id)
    plan = await lock_task_plan(db, task.id, task.project_id)
    await db.refresh(task)
    await db.refresh(claim)
    await db.refresh(attempt)
    await require_approved_execution(db, plan, task.project_id, task.team_id)
    project = await db.get(Project, task.project_id)
    creator = await db.get(User, session.created_by)
    team = await db.get(AgentTeam, task.team_id)
    skill = await db.get(SkillProfile, task.skill_profile_id)
    team_skill = await db.scalar(select(AgentTeamSkill.id).where(
        AgentTeamSkill.team_id == task.team_id, AgentTeamSkill.skill_profile_id == task.skill_profile_id))
    now = utcnow()
    if (attempt.status != "running" or claim.released_at or session.status != "active" or
        min(session.expires_at, session.max_expires_at) <= now or task.status != "claimed" or task.claimed_session_id != session.id or
        not task.lease_expires_at or min(task.lease_expires_at, claim.lease_expires_at) <= now or
        session.project_id != task.project_id or session.team_id != task.team_id or
        session.skill_profile_id != task.skill_profile_id or "task.claim" not in session.capabilities or
        project.status == "archived" or not team.is_active or not skill.is_active or not team_skill or
        skill.code != session.skill_code or creator is None or not creator.is_active or
        not await project_permission(db, creator, task.project_id, "agent.session.connect") or
        not await project_permission(db, creator, task.project_id, "agent.task.claim") or
        not await project_permission(db, creator, task.project_id, "generation.create")):
        raise ValueError("Exécution agentique indisponible ou réservation périmée.")
    return session, task, creator


async def guard_generation(db, run):
    attempt = await db.scalar(select(AgentTaskRun).where(AgentTaskRun.generation_run_id == run.id))
    if attempt is not None:
        await execution_scope(db, attempt)
        from app.runtime_models import RuntimeSelection
        from app.runtime_service import guard_selection
        selection = await db.scalar(select(RuntimeSelection).where(RuntimeSelection.attempt_id == attempt.id))
        if selection:
            await guard_selection(db, selection)
    return attempt


async def delegate_generation(db, session, task_id, claim_id, kind, style, item_ids, profile_version_id=None):
    attempt = await db.scalar(select(AgentTaskRun).join(AgentTaskClaim).where(
        AgentTaskRun.task_id == task_id, AgentTaskRun.claim_id == claim_id,
        AgentTaskClaim.session_id == session.id))
    if attempt is None:
        raise ValueError("Réservation incompatible.")
    _, task, creator = await execution_scope(db, attempt)
    if attempt.generation_run_id:
        existing = await db.get(GenerationRun, attempt.generation_run_id)
        if (existing.parameters["style"] != style or existing.parameters["document_type"] != kind or
            sorted(item["id"] for item in existing.context_snapshot) != sorted(item_ids) or
            (profile_version_id and existing.llm_profile_version_id != profile_version_id)):
            raise ValueError("Cette tentative est déjà figée avec d'autres entrées.")
        return existing, False
    from app.runtime_service import select_runtime
    selection = await select_runtime(db, attempt, session, task, explicit_profile=profile_version_id)
    profile = await db.get(LLMProfileVersion, selection.profile_version_id)
    run = await start_generation(db, project_id=task.project_id, actor=creator,
                                kind=kind, style=style, item_ids=item_ids, profile=profile)
    run.prompt_version = "agent-document-v1"
    run.parameters = {**run.parameters, "document_type": kind,
                      "task": {"id": task.id, "plan_version_id": task.plan_version_id,
                               "skill_profile_id": task.skill_profile_id, "title": redact(task.title)}}
    if selection.memory_snapshot:
        run.parameters = {**run.parameters, "memory": selection.memory_snapshot}
    attempt.llm_profile_version_id, attempt.generation_run_id = profile.id, run.id
    audit(db, "agent.llm.request", "success", project_id=task.project_id,
          target_type="agent_task_run", target_id=attempt.id,
          details={"generation_run_id": run.id, "profile_version_id": profile.id})
    await db.flush()
    return run, True


async def finish_generation(db, run, error):
    attempt = await db.scalar(select(AgentTaskRun).where(AgentTaskRun.generation_run_id == run.id))
    if attempt is None:
        return
    if error:
        from app.agent_routing import _release_task
        task = await db.get(AgentTask, attempt.task_id)
        if attempt.status == "running":
            claim = await db.get(AgentTaskClaim, attempt.claim_id)
            if task.claimed_session_id == claim.session_id:
                await _release_task(db, task, "runtime_error")
                # L'échec demande une relance humaine via le coordinateur.
                task.status = "failed"
                attempt.status, attempt.error_code, attempt.ended_at = "failed", "execution_error", utcnow()
                from app.agent_progress import aggregate_plan
                await db.flush()
                await aggregate_plan(db, task.plan_version_id)
        return
    session, task, _ = await execution_scope(db, attempt)
    await report_progress(db, session=session, task_id=task.id, claim_id=attempt.claim_id, progress_percent=100)
