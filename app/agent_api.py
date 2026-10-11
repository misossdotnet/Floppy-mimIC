"""API de contrôle des sessions d'agents ; aucun droit documentaire n'y est délégué."""

import hmac
import os
from typing import Literal
from uuid import UUID

from fastapi import APIRouter, Depends, Header, HTTPException, Request, Response
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.agent_catalog import CAPABILITY_CATALOG, CLIENT_CATALOG, SKILL_CATALOG
from app.agent_models import (AgentPlanVersion, AgentSession, AgentTask, AgentTeam,
                              AgentTeamSkill, AgentTaskRun, FlowRun, FlowVersion, SkillProfile)
from app.agent_service import (authenticated_session, create_team, expire_project_sessions, heartbeat, register_session,
                               revoke_session)
from app.agent_routing import release_own_task, reserve_task
from app.agent_progress import report_progress, task_view
from app.identity_models import LoginSession, Project, User, utcnow
from app.identity_service import audit, csrf_for, project_permission, session_user


router = APIRouter(prefix="/api/projects/{project_id}/agent-sessions", tags=["agent-sessions"])
team_router = APIRouter(prefix="/api/projects/{project_id}/agent-teams", tags=["agent-teams"])
catalog_router = APIRouter(prefix="/api/projects/{project_id}/agent-catalog", tags=["agent-catalog"])
task_router = APIRouter(prefix="/api/projects/{project_id}/agent-tasks", tags=["agent-tasks"])


async def db_session(request: Request):
    async with request.app.state.sessions() as db:
        yield db


async def human(request: Request, db: AsyncSession = Depends(db_session)) -> tuple[User, LoginSession, str]:
    cookie = ("__Host-" if os.environ.get("MIMIC_COOKIE_SECURE", "true").lower() != "false" else "") + "mimic-session"
    token = request.cookies.get(cookie)
    result = await session_user(db, token)
    if result is None:
        raise HTTPException(status_code=401)
    return result[0], result[1], token


def check_csrf(token: str, supplied: str | None) -> None:
    if not supplied or not hmac.compare_digest(csrf_for(token, "session"), supplied):
        raise HTTPException(status_code=403)


class SessionRegistration(BaseModel):
    model_config = ConfigDict(extra="forbid")
    team_id: str = Field(min_length=36, max_length=36)
    client_code: str = Field(min_length=1, max_length=40)
    skill_code: str = Field(min_length=1, max_length=40)
    capabilities: list[str] = Field(min_length=1, max_length=10)


def session_view(session: AgentSession) -> dict:
    return {"id": session.id, "project_id": session.project_id,
            "client_code": session.client_code, "skill_code": session.skill_code,
            "team_id": session.team_id, "skill_profile_id": session.skill_profile_id,
            "capabilities": session.capabilities, "status": session.status,
            "created_at": session.created_at, "last_heartbeat_at": session.last_heartbeat_at,
            "expires_at": session.expires_at}


async def human_project(db: AsyncSession, user: User, project_id: str, permission: str) -> Project:
    project = await db.get(Project, project_id)
    if project is None or not await project_permission(db, user, project_id, "project.read"):
        audit(db, permission, "denied", actor_id=user.id,
              project_id=project.id if project else None)
        await db.commit()
        raise HTTPException(status_code=404)
    if not await project_permission(db, user, project_id, permission):
        audit(db, permission, "denied", actor_id=user.id, project_id=project_id)
        await db.commit()
        raise HTTPException(status_code=403)
    return project


async def authorized_agent(db: AsyncSession, *, project_id: str, session_id: str,
                           authorization: str | None, capability: str,
                           permission: str) -> AgentSession:
    session = await authenticated_session(db, token=bearer_token(authorization),
        session_id=session_id, project_id=project_id)
    if session is None:
        await db.commit()
        raise HTTPException(status_code=401)
    creator = await db.get(User, session.created_by)
    if capability not in session.capabilities or creator is None or not await project_permission(
            db, creator, project_id, permission):
        audit(db, permission, "denied", project_id=project_id,
              target_type="agent_session", target_id=session.id,
              details={"capability": capability})
        await db.commit()
        raise HTTPException(status_code=403)
    return session


def bearer_token(authorization: str | None) -> str | None:
    if not authorization or not authorization.startswith("Bearer "):
        return None
    return authorization[7:]


@router.post("", status_code=201)
async def create_agent_session(project_id: str, payload: SessionRegistration, response: Response,
                               x_csrf_token: str | None = Header(default=None),
                               db: AsyncSession = Depends(db_session),
                               actor: tuple[User, LoginSession, str] = Depends(human)):
    user, _, cookie_token = actor
    check_csrf(cookie_token, x_csrf_token)
    project = await human_project(db, user, project_id, "agent.session.connect")
    if project.status == "archived":
        raise HTTPException(status_code=409, detail="Projet archivé.")
    try:
        session, agent_token = await register_session(db, actor=user, project_id=project_id,
            team_id=payload.team_id,
            client_code=payload.client_code, skill_code=payload.skill_code,
            capabilities=payload.capabilities)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    await db.commit()
    response.headers["Cache-Control"] = "no-store"
    response.headers["Referrer-Policy"] = "no-referrer"
    return {"session": session_view(session), "token": agent_token}


@router.get("")
async def list_agent_sessions(project_id: str, db: AsyncSession = Depends(db_session),
                              actor: tuple[User, LoginSession, str] = Depends(human)):
    await human_project(db, actor[0], project_id, "agent.read")
    await expire_project_sessions(db, project_id)
    sessions = (await db.scalars(select(AgentSession).where(
        AgentSession.project_id == project_id).order_by(AgentSession.created_at.desc()))).all()
    await db.commit()
    return {"sessions": [session_view(session) for session in sessions]}


@router.post("/{session_id}/heartbeat")
async def agent_heartbeat(project_id: str, session_id: str,
                          authorization: str | None = Header(default=None),
                          db: AsyncSession = Depends(db_session)):
    session = await authenticated_session(db, token=bearer_token(authorization),
        session_id=session_id, project_id=project_id, capability="session.heartbeat")
    if session is None:
        await db.commit()
        raise HTTPException(status_code=401)
    await heartbeat(db, session)
    await db.commit()
    return session_view(session)


@router.post("/{session_id}/disconnect")
async def agent_disconnect(project_id: str, session_id: str,
                           authorization: str | None = Header(default=None),
                           db: AsyncSession = Depends(db_session)):
    session = await authenticated_session(db, token=bearer_token(authorization),
        session_id=session_id, project_id=project_id, capability="session.disconnect")
    if session is None:
        await db.commit()
        raise HTTPException(status_code=401)
    await revoke_session(db, session)
    await db.commit()
    return {"status": "revoked"}


class TeamCreation(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: str = Field(min_length=1, max_length=120)
    skill_codes: list[str] = Field(min_length=1, max_length=7)


async def team_view(db: AsyncSession, team: AgentTeam) -> dict:
    codes = (await db.scalars(select(SkillProfile.code).join(
        AgentTeamSkill, AgentTeamSkill.skill_profile_id == SkillProfile.id).where(
        AgentTeamSkill.team_id == team.id).order_by(SkillProfile.code))).all()
    return {"id": team.id, "project_id": team.project_id, "name": team.name,
            "is_active": team.is_active, "skill_codes": codes}


@team_router.post("", status_code=201)
async def create_agent_team(project_id: str, payload: TeamCreation,
                            x_csrf_token: str | None = Header(default=None),
                            db: AsyncSession = Depends(db_session),
                            actor: tuple[User, LoginSession, str] = Depends(human)):
    user, _, cookie_token = actor
    check_csrf(cookie_token, x_csrf_token)
    project = await human_project(db, user, project_id, "agent.team.configure")
    if project.status == "archived":
        raise HTTPException(status_code=409, detail="Projet archivé.")
    try:
        team = await create_team(db, actor=user, project_id=project_id,
                                 name=payload.name, skill_codes=payload.skill_codes)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except IntegrityError as exc:
        await db.rollback()
        raise HTTPException(status_code=409, detail="Équipe déjà enregistrée.") from exc
    result = await team_view(db, team)
    await db.commit()
    return result


@team_router.get("")
async def list_agent_teams(project_id: str, db: AsyncSession = Depends(db_session),
                           actor: tuple[User, LoginSession, str] = Depends(human)):
    await human_project(db, actor[0], project_id, "agent.read")
    teams = (await db.scalars(select(AgentTeam).where(
        AgentTeam.project_id == project_id).order_by(AgentTeam.name))).all()
    return {"teams": [await team_view(db, team) for team in teams]}


@catalog_router.get("")
async def agent_catalog(project_id: str, db: AsyncSession = Depends(db_session),
                        actor: tuple[User, LoginSession, str] = Depends(human)):
    await human_project(db, actor[0], project_id, "agent.read")
    profiles = (await db.scalars(select(SkillProfile).order_by(
        SkillProfile.code, SkillProfile.version_number.desc()))).all()
    return {"clients": CLIENT_CATALOG, "skills": SKILL_CATALOG,
            "capabilities": CAPABILITY_CATALOG,
            "skill_profiles": [{"id": profile.id, "code": profile.code,
                "version_number": profile.version_number, "is_active": profile.is_active,
                "allowed_capabilities": profile.allowed_capabilities} for profile in profiles]}


@router.delete("/{session_id}")
async def human_revoke(project_id: str, session_id: str,
                       x_csrf_token: str | None = Header(default=None),
                       db: AsyncSession = Depends(db_session),
                       actor: tuple[User, LoginSession, str] = Depends(human)):
    user, _, cookie_token = actor
    check_csrf(cookie_token, x_csrf_token)
    await human_project(db, user, project_id, "agent.run.manage")
    session = await db.scalar(select(AgentSession).where(
        AgentSession.id == session_id, AgentSession.project_id == project_id).with_for_update())
    if session is None:
        raise HTTPException(status_code=404)
    await revoke_session(db, session, actor_id=user.id)
    await db.commit()
    return {"status": "revoked"}


@task_router.get("/ready")
async def ready_agent_tasks(project_id: str, session_id: str,
                            authorization: str | None = Header(default=None),
                            db: AsyncSession = Depends(db_session)):
    session = await authorized_agent(db, project_id=project_id, session_id=session_id,
        authorization=authorization, capability="task.claim", permission="agent.task.claim")
    from app.agent_coordinator import ready_for_session
    tasks = await ready_for_session(db, session)
    await db.commit()
    return {"tasks": [task_view(task) for task in tasks]}


@task_router.post("/{task_id}/claim")
async def claim_agent_task(project_id: str, task_id: str, session_id: str,
                           authorization: str | None = Header(default=None),
                           db: AsyncSession = Depends(db_session)):
    session = await authorized_agent(db, project_id=project_id, session_id=session_id,
        authorization=authorization, capability="task.claim", permission="agent.task.claim")
    try:
        claim = await reserve_task(db, session=session, task_id=task_id)
    except ValueError as exc:
        await db.commit()
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    await db.commit()
    return {"claim_id": claim.id, "task_id": claim.task_id,
            "lease_expires_at": claim.lease_expires_at}


@task_router.post("/{task_id}/release")
async def release_agent_task(project_id: str, task_id: str, session_id: str,
                             authorization: str | None = Header(default=None),
                             db: AsyncSession = Depends(db_session)):
    session = await authorized_agent(db, project_id=project_id, session_id=session_id,
        authorization=authorization, capability="task.release", permission="agent.task.claim")
    released = await release_own_task(db, session=session, task_id=task_id)
    await db.commit()
    return {"released": released}


@task_router.get("")
async def list_plan_tasks(project_id: str, plan_id: str,
                          db: AsyncSession = Depends(db_session),
                          actor: tuple[User, LoginSession, str] = Depends(human)):
    await human_project(db, actor[0], project_id, "agent.read")
    plan = await db.scalar(select(AgentPlanVersion).where(
        AgentPlanVersion.id == plan_id, AgentPlanVersion.project_id == project_id))
    if plan is None:
        raise HTTPException(status_code=404)
    tasks = (await db.scalars(select(AgentTask).where(
        AgentTask.plan_version_id == plan_id, AgentTask.project_id == project_id)
        .order_by(AgentTask.depth, AgentTask.priority, AgentTask.task_key))).all()
    from app.agent_coordinator import blockers, dependency_graph
    from app.agent_autonomy import active_approval, approval_required, effective_action_class
    run = await db.get(FlowRun, plan.validated_flow_run_id)
    graph = dependency_graph(tasks)
    views = []
    for task in tasks:
        view = task_view(task)
        view["effective_dependency_keys"] = sorted(graph.get(task.task_key, set()))
        view["blocked_by"] = blockers(task, tasks, plan, run.status if run else "unavailable")
        view["effective_action_class"] = effective_action_class(task, tasks)
        required = approval_required(task, tasks, plan) and task.task_key in graph
        approval = await active_approval(db, task) if required else None
        view["approval_required"] = required
        view["approval_id"] = approval.id if approval else None
        view["approval_status"] = approval.status if approval else None
        if required and task.status == "ready" and (approval is None or approval.status != "approved"):
            view["blocked_by"].append("approval_pending" if approval else "approval_required")
        views.append(view)
    return {"plan_id": plan.id, "plan_status": plan.status,
            "autonomy_mode": plan.autonomy_mode,
            "max_parallel_tasks": plan.max_parallel_tasks, "max_attempts": plan.max_attempts,
            "tasks": views}


class TaskProgress(BaseModel):
    model_config = ConfigDict(extra="forbid")
    claim_id: str = Field(min_length=36, max_length=36)
    progress_percent: int = Field(ge=0, le=100, strict=True)


@task_router.post("/{task_id}/progress")
async def progress_agent_task(project_id: str, task_id: str, session_id: str,
                              payload: TaskProgress,
                              authorization: str | None = Header(default=None),
                              db: AsyncSession = Depends(db_session)):
    session = await authorized_agent(db, project_id=project_id, session_id=session_id,
        authorization=authorization, capability="task.claim", permission="agent.task.claim")
    from app.business_models import GenerationRun
    active_llm = await db.scalar(select(GenerationRun.id).join(AgentTaskRun,
        AgentTaskRun.generation_run_id == GenerationRun.id).where(AgentTaskRun.claim_id == payload.claim_id,
        GenerationRun.status.in_(["queued", "running"])))
    if active_llm:
        raise HTTPException(status_code=409, detail="Production déléguée au worker LLM en cours.")
    try:
        task = await report_progress(db, session=session, task_id=task_id,
            claim_id=payload.claim_id, progress_percent=payload.progress_percent)
    except ValueError as exc:
        audit(db, "agent.task.progress", "denied", project_id=project_id,
              target_type="agent_session", target_id=session.id)
        await db.commit()
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    await db.commit()
    return task_view(task)


class ClaimNextDraft(BaseModel):
    model_config = ConfigDict(extra="forbid")
    request_key: UUID


@task_router.post("/claim-next")
async def next_agent_task(project_id: str, session_id: str, payload: ClaimNextDraft,
                          authorization: str | None = Header(default=None),
                          db: AsyncSession = Depends(db_session)):
    from app.agent_coordinator import claim_next
    session = await authorized_agent(db, project_id=project_id, session_id=session_id,
        authorization=authorization, capability="task.claim", permission="agent.task.claim")
    try:
        claim = await claim_next(db, session, str(payload.request_key))
    except ValueError as exc:
        await db.commit()
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    attempt = await db.scalar(select(AgentTaskRun).where(AgentTaskRun.claim_id == claim.id))
    await db.commit()
    return {"claim_id": claim.id, "task_id": claim.task_id, "lease_expires_at": claim.lease_expires_at,
            "released_at": claim.released_at, "attempt_id": attempt.id, "attempt_status": attempt.status}


class TaskFailure(BaseModel):
    model_config = ConfigDict(extra="forbid")
    claim_id: UUID
    error_code: Literal["execution_error", "timeout", "invalid_result"]


@task_router.post("/{task_id}/fail")
async def fail_agent_task(project_id: str, task_id: str, session_id: str, payload: TaskFailure,
                          authorization: str | None = Header(default=None),
                          db: AsyncSession = Depends(db_session)):
    from app.agent_coordinator import fail_task
    session = await authorized_agent(db, project_id=project_id, session_id=session_id,
        authorization=authorization, capability="task.claim", permission="agent.task.claim")
    try:
        task = await fail_task(db, session, task_id, str(payload.claim_id), payload.error_code)
    except ValueError as exc:
        audit(db, "agent.task.fail", "denied", project_id=project_id,
              target_type="agent_session", target_id=session.id)
        await db.commit()
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    await db.commit()
    return task_view(task)


class TaskRetry(BaseModel):
    model_config = ConfigDict(extra="forbid")
    attempt_id: UUID


async def task_manager(db, actor, project_id, task_id, csrf, permission="agent.run.manage"):
    user, _, cookie = actor
    check_csrf(cookie, csrf)
    project = await human_project(db, user, project_id, permission)
    if project.status == "archived": raise HTTPException(status_code=409)
    if not await db.scalar(select(AgentTask.id).where(AgentTask.id == task_id, AgentTask.project_id == project_id)):
        raise HTTPException(status_code=404)
    return user


@task_router.post("/{task_id}/retry")
async def retry_agent_task(project_id: str, task_id: str, payload: TaskRetry,
                           x_csrf_token: str | None = Header(default=None),
                           db: AsyncSession = Depends(db_session),
                           actor: tuple[User, LoginSession, str] = Depends(human)):
    from app.agent_coordinator import retry_task
    user = await task_manager(db, actor, project_id, task_id, x_csrf_token)
    try:
        task = await retry_task(db, project_id, task_id, str(payload.attempt_id), user.id)
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    await db.commit()
    return task_view(task)


@task_router.post("/{task_id}/cancel")
async def cancel_agent_task(project_id: str, task_id: str,
                            x_csrf_token: str | None = Header(default=None),
                            db: AsyncSession = Depends(db_session),
                            actor: tuple[User, LoginSession, str] = Depends(human)):
    from app.agent_coordinator import cancel_subtree
    user = await task_manager(db, actor, project_id, task_id, x_csrf_token)
    try:
        task = await cancel_subtree(db, project_id, task_id, user.id)
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    await db.commit()
    return task_view(task)


@task_router.get("/{task_id}/attempts")
async def task_attempts(project_id: str, task_id: str, db: AsyncSession = Depends(db_session),
                        actor: tuple[User, LoginSession, str] = Depends(human)):
    await human_project(db, actor[0], project_id, "agent.read")
    task = await db.scalar(select(AgentTask).where(AgentTask.id == task_id, AgentTask.project_id == project_id))
    if task is None: raise HTTPException(status_code=404)
    attempts = (await db.scalars(select(AgentTaskRun).where(AgentTaskRun.task_id == task_id)
        .order_by(AgentTaskRun.attempt_number))).all()
    return {"attempts": [{"id": a.id, "claim_id": a.claim_id, "parent_run_id": a.parent_run_id,
                         "approval_id": a.approval_id,
                         "llm_profile_version_id": a.llm_profile_version_id,
                         "generation_run_id": a.generation_run_id,
                         "attempt_number": a.attempt_number, "status": a.status, "error_code": a.error_code,
                         "started_at": a.started_at, "ended_at": a.ended_at} for a in attempts]}


class TaskLLMDelegation(BaseModel):
    model_config = ConfigDict(extra="forbid")
    claim_id: UUID
    document_type: Literal["scope", "use_cases", "user_stories", "architecture", "data_model",
                           "roles_permissions", "diagrams", "technical_doc", "functional_doc"]
    style: Literal["concise", "detailed"] = "concise"
    context_ids: list[UUID] = Field(min_length=1, max_length=20)
    profile_version_id: UUID | None = None


@task_router.post("/{task_id}/llm", status_code=202)
async def delegate_task_llm(project_id: str, task_id: str, session_id: str, payload: TaskLLMDelegation,
                            authorization: str | None = Header(default=None),
                            db: AsyncSession = Depends(db_session)):
    from app.agent_llm_service import delegate_generation, finish_generation
    from app.business_models import GenerationRun
    session = await authorized_agent(db, project_id=project_id, session_id=session_id,
        authorization=authorization, capability="task.claim", permission="agent.task.claim")
    creator = await db.get(User, session.created_by)
    if not await project_permission(db, creator, project_id, "generation.create"):
        raise HTTPException(status_code=403)
    try:
        async with db.begin_nested():
            run, created = await delegate_generation(db, session, task_id, str(payload.claim_id),
                payload.document_type, payload.style, [str(item) for item in payload.context_ids],
                str(payload.profile_version_id) if payload.profile_version_id else None)
    except ValueError as exc:
        audit(db, "agent.llm.request", "denied", project_id=project_id,
              target_type="agent_session", target_id=session.id)
        await db.commit()
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    await db.commit()
    if created:
        try:
            from app.worker import generate_document
            generate_document.delay(run.id)
        except Exception:
            from app.agent_llm_service import guard_generation
            try:
                await guard_generation(db, run)
            except ValueError:
                pass
            run = await db.scalar(select(GenerationRun).where(GenerationRun.id == run.id).with_for_update())
            if run.status == "queued":
                run.status, run.error_code, run.completed_at = "failed", "dispatch_unavailable", utcnow()
                await finish_generation(db, run, run.error_code)
                audit(db, "agent.llm.dispatch", "failed", project_id=project_id,
                      target_type="generation_run", target_id=run.id)
                await db.commit()
    return {"generation_run_id": run.id, "profile_version_id": run.llm_profile_version_id, "status": run.status}


class ApprovalRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    attempt_number: int = Field(ge=1, le=5, strict=True)


@task_router.post("/{task_id}/approval-request")
async def request_task_approval(project_id: str, task_id: str, payload: ApprovalRequest,
                                x_csrf_token: str | None = Header(default=None),
                                db: AsyncSession = Depends(db_session),
                                actor: tuple[User, LoginSession, str] = Depends(human)):
    from app.agent_autonomy import request_approval, approval_view
    user = await task_manager(db, actor, project_id, task_id, x_csrf_token)
    try:
        approval = await request_approval(db, project_id, task_id, payload.attempt_number, user.id)
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    await db.commit()
    return approval_view(approval)


@task_router.get("/{task_id}/approvals")
async def task_approvals(project_id: str, task_id: str, db: AsyncSession = Depends(db_session),
                         actor: tuple[User, LoginSession, str] = Depends(human)):
    from app.agent_models import AgentActionApproval
    from app.agent_autonomy import approval_view
    await human_project(db, actor[0], project_id, "agent.read")
    if not await db.scalar(select(AgentTask.id).where(AgentTask.id == task_id, AgentTask.project_id == project_id)):
        raise HTTPException(status_code=404)
    approvals = (await db.scalars(select(AgentActionApproval).where(AgentActionApproval.task_id == task_id)
        .order_by(AgentActionApproval.created_at, AgentActionApproval.id))).all()
    return {"approvals": [approval_view(a) for a in approvals]}


@task_router.post("/{task_id}/approvals/{approval_id}/{decision}")
async def decide_task_approval(project_id: str, task_id: str, approval_id: str,
                               decision: Literal["approve", "reject"],
                               x_csrf_token: str | None = Header(default=None),
                               db: AsyncSession = Depends(db_session),
                               actor: tuple[User, LoginSession, str] = Depends(human)):
    from app.agent_autonomy import decide_approval, approval_view
    from app.agent_models import AgentActionApproval
    user = await task_manager(db, actor, project_id, task_id, x_csrf_token, "agent.plan.approve")
    if not await db.scalar(select(AgentActionApproval.id).where(
            AgentActionApproval.id == approval_id, AgentActionApproval.task_id == task_id)):
        raise HTTPException(status_code=404)
    try:
        approval = await decide_approval(db, project_id, task_id, approval_id, decision, user.id)
    except ValueError as exc:
        audit(db, f"agent.approval.{decision}", "denied", actor_id=user.id, project_id=project_id,
              target_type="agent_task", target_id=task_id)
        await db.commit()
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    await db.commit()
    return approval_view(approval)
