"""Validation humaine du flux et du plan précédant les claims."""

from typing import Literal

from fastapi import APIRouter, Depends, Header, HTTPException
from pydantic import AwareDatetime, BaseModel, ConfigDict, Field
from sqlalchemy.ext.asyncio import AsyncSession

from app.agent_api import check_csrf, db_session, human, human_project
from app.agent_flow_service import (create_flow, launch_flow, propose_plan,
                                    transition_flow, transition_plan)
from app.identity_models import LoginSession, User

router = APIRouter(prefix="/api/projects/{project_id}/agent-flows", tags=["agent-flows"])


class FlowDraft(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: str = Field(min_length=1, max_length=120)
    team_id: str = Field(min_length=36, max_length=36)


class PlanTaskDraft(BaseModel):
    model_config = ConfigDict(extra="forbid")
    task_key: str = Field(min_length=1, max_length=80)
    title: str = Field(min_length=1, max_length=200)
    skill_code: str = Field(min_length=1, max_length=40)
    parent_task_key: str | None = Field(default=None, min_length=1, max_length=80)
    priority: Literal["P0", "P1", "P2", "P3"] = "P2"
    due_at: AwareDatetime | None = None
    dependency_keys: list[str] = Field(default_factory=list, max_length=20)
    action_class: Literal["standard", "sensitive"] = "standard"


class PlanDraft(BaseModel):
    model_config = ConfigDict(extra="forbid")
    max_parallel_tasks: int = Field(default=4, ge=1, le=8, strict=True)
    max_attempts: int = Field(default=3, ge=1, le=5, strict=True)
    autonomy_mode: Literal["bounded", "sensitive", "per_action"] = "bounded"
    tasks: list[PlanTaskDraft] = Field(min_length=1, max_length=20)


async def authorize(db, actor, project_id, permission, csrf):
    user, _, cookie_token = actor
    check_csrf(cookie_token, csrf)
    project = await human_project(db, user, project_id, permission)
    if project.status == "archived":
        raise HTTPException(status_code=409, detail="Projet archivé.")
    return user


def conflict(exc):
    return HTTPException(status_code=409, detail=str(exc))


@router.post("", status_code=201)
async def new_flow(project_id: str, payload: FlowDraft,
                   x_csrf_token: str | None = Header(default=None),
                   db: AsyncSession = Depends(db_session),
                   actor: tuple[User, LoginSession, str] = Depends(human)):
    user = await authorize(db, actor, project_id, "flow.edit", x_csrf_token)
    try:
        version = await create_flow(db, project_id=project_id, team_id=payload.team_id,
                                    name=payload.name, actor_id=user.id)
    except ValueError as exc:
        raise conflict(exc) from exc
    await db.commit()
    return {"version_id": version.id, "flow_definition_id": version.flow_definition_id,
            "status": version.status, "graph": version.graph}


@router.post("/{version_id}/review")
async def review_flow(project_id: str, version_id: str,
                      x_csrf_token: str | None = Header(default=None),
                      db: AsyncSession = Depends(db_session),
                      actor: tuple[User, LoginSession, str] = Depends(human)):
    user = await authorize(db, actor, project_id, "flow.edit", x_csrf_token)
    try:
        version = await transition_flow(db, project_id=project_id, version_id=version_id,
                                        actor_id=user.id, target="review")
    except ValueError as exc:
        raise conflict(exc) from exc
    await db.commit()
    return {"version_id": version.id, "status": version.status}


@router.post("/{version_id}/validate")
async def validate_flow(project_id: str, version_id: str,
                        x_csrf_token: str | None = Header(default=None),
                        db: AsyncSession = Depends(db_session),
                        actor: tuple[User, LoginSession, str] = Depends(human)):
    user = await authorize(db, actor, project_id, "flow.validate", x_csrf_token)
    try:
        version = await transition_flow(db, project_id=project_id, version_id=version_id,
                                        actor_id=user.id, target="validated")
    except ValueError as exc:
        raise conflict(exc) from exc
    await db.commit()
    return {"version_id": version.id, "status": version.status}


@router.post("/{version_id}/runs", status_code=201)
async def new_run(project_id: str, version_id: str,
                  x_csrf_token: str | None = Header(default=None),
                  db: AsyncSession = Depends(db_session),
                  actor: tuple[User, LoginSession, str] = Depends(human)):
    user = await authorize(db, actor, project_id, "flow.execute", x_csrf_token)
    try:
        run = await launch_flow(db, project_id=project_id, version_id=version_id,
                                actor_id=user.id)
    except ValueError as exc:
        raise conflict(exc) from exc
    await db.commit()
    return {"run_id": run.id, "status": run.status}


@router.post("/runs/{run_id}/plans", status_code=201)
async def new_plan(project_id: str, run_id: str, payload: PlanDraft,
                   x_csrf_token: str | None = Header(default=None),
                   db: AsyncSession = Depends(db_session),
                   actor: tuple[User, LoginSession, str] = Depends(human)):
    user = await authorize(db, actor, project_id, "agent.run.manage", x_csrf_token)
    try:
        plan = await propose_plan(db, project_id=project_id, run_id=run_id,
            tasks=[task.model_dump() for task in payload.tasks], actor_id=user.id,
            max_parallel_tasks=payload.max_parallel_tasks, max_attempts=payload.max_attempts,
            autonomy_mode=payload.autonomy_mode)
    except ValueError as exc:
        raise conflict(exc) from exc
    await db.commit()
    return {"plan_id": plan.id, "status": plan.status, "autonomy_mode": plan.autonomy_mode}


@router.post("/plans/{plan_id}/review")
async def review_plan(project_id: str, plan_id: str,
                      x_csrf_token: str | None = Header(default=None),
                      db: AsyncSession = Depends(db_session),
                      actor: tuple[User, LoginSession, str] = Depends(human)):
    user = await authorize(db, actor, project_id, "agent.run.manage", x_csrf_token)
    try:
        plan = await transition_plan(db, project_id=project_id, plan_id=plan_id,
                                     actor_id=user.id, target="review")
    except ValueError as exc:
        raise conflict(exc) from exc
    await db.commit()
    return {"plan_id": plan.id, "status": plan.status}


@router.post("/plans/{plan_id}/approve")
async def approve_plan(project_id: str, plan_id: str,
                       x_csrf_token: str | None = Header(default=None),
                       db: AsyncSession = Depends(db_session),
                       actor: tuple[User, LoginSession, str] = Depends(human)):
    user = await authorize(db, actor, project_id, "agent.plan.approve", x_csrf_token)
    try:
        plan = await transition_plan(db, project_id=project_id, plan_id=plan_id,
                                     actor_id=user.id, target="approved")
    except ValueError as exc:
        raise conflict(exc) from exc
    await db.commit()
    return {"plan_id": plan.id, "status": plan.status}


@router.post("/plans/{plan_id}/{action}")
async def manage_plan(project_id: str, plan_id: str, action: Literal["pause", "resume", "cancel"],
                       x_csrf_token: str | None = Header(default=None),
                       db: AsyncSession = Depends(db_session),
                       actor: tuple[User, LoginSession, str] = Depends(human)):
    from sqlalchemy import select
    from app.agent_models import AgentPlanVersion
    from app.agent_coordinator import control_plan
    user = await authorize(db, actor, project_id, "agent.run.manage", x_csrf_token)
    if not await db.scalar(select(AgentPlanVersion.id).where(
            AgentPlanVersion.id == plan_id, AgentPlanVersion.project_id == project_id)):
        raise HTTPException(status_code=404)
    try:
        run = await control_plan(db, project_id, plan_id, action, user.id)
    except ValueError as exc:
        raise conflict(exc) from exc
    await db.commit()
    return {"run_id": run.id, "status": run.status}
