"""API humaine de planification et de consultation des sprints."""
from datetime import timezone
from typing import Literal
from uuid import UUID

from fastapi import APIRouter, Depends, Header, HTTPException
from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, field_validator
from sqlalchemy import select

from app.agent_api import db_session, human, human_project, check_csrf
from app.agent_models import AgentPlanVersion, AgentSprint, AgentSprintRevision
from app.agent_sprint_service import create_sprint, revise_sprint, transition_sprint, sprint_board, sprint_view

router = APIRouter(prefix='/api/projects/{project_id}/agent-sprints',tags=['agent-sprints'])
Column = Literal['todo','in_progress','blocked','done','cancelled']
Action = Literal['start','close','cancel']


class SprintItem(BaseModel):
    model_config = ConfigDict(extra='forbid')
    task_id: UUID
    target_due_at: AwareDatetime | None = None

    @field_validator('target_due_at')
    @classmethod
    def utc(cls,value):
        return value.astimezone(timezone.utc) if value else value


class SprintFields(BaseModel):
    model_config = ConfigDict(extra='forbid',str_strip_whitespace=True)
    name: str = Field(min_length=1,max_length=120)
    goal: str = Field(default='',max_length=1000)
    starts_at: AwareDatetime
    ends_at: AwareDatetime
    items: list[SprintItem] = Field(default_factory=list,max_length=20)

    @field_validator('starts_at','ends_at')
    @classmethod
    def utc(cls,value):
        return value.astimezone(timezone.utc)


class SprintDraft(SprintFields):
    plan_id: UUID


class SprintEdit(SprintFields):
    expected_revision: int = Field(ge=1,strict=True)


class SprintTransition(BaseModel):
    model_config = ConfigDict(extra='forbid')
    expected_revision: int = Field(ge=1,strict=True)


async def authorize_sprint(db,actor,project_id,csrf=None,*,write=False,sprint_id=None,plan_id=None):
    if write: check_csrf(actor[2],csrf)
    project = await human_project(db,actor[0],project_id,'agent.run.manage' if write else 'agent.read')
    if write and project.status=='archived': raise HTTPException(status_code=409,detail='Projet archivé.')
    if sprint_id and not await db.scalar(select(AgentSprint.id).where(AgentSprint.id==sprint_id,AgentSprint.project_id==project_id)):
        raise HTTPException(status_code=404)
    if plan_id and not await db.scalar(select(AgentPlanVersion.id).where(AgentPlanVersion.id==plan_id,AgentPlanVersion.project_id==project_id)):
        raise HTTPException(status_code=404)
    return project


@router.post('',status_code=201)
async def new_sprint(project_id:str,payload:SprintDraft,x_csrf_token:str|None=Header(default=None),
                     db=Depends(db_session),actor=Depends(human)):
    await authorize_sprint(db,actor,project_id,x_csrf_token,write=True,plan_id=str(payload.plan_id))
    try: sprint=await create_sprint(db,project_id,str(payload.plan_id),payload.model_dump(),actor[0].id)
    except ValueError as exc: raise HTTPException(status_code=409,detail=str(exc)) from exc
    await db.commit(); return sprint_view(sprint)


@router.get('')
async def list_sprints(project_id:str,db=Depends(db_session),actor=Depends(human)):
    await authorize_sprint(db,actor,project_id)
    rows=(await db.scalars(select(AgentSprint).where(AgentSprint.project_id==project_id)
        .order_by(AgentSprint.starts_at.desc(),AgentSprint.id))).all()
    return {'sprints':[sprint_view(s) for s in rows]}


@router.get('/{sprint_id}')
async def get_board(project_id:str,sprint_id:str,column:Column|None=None,overdue_only:bool=False,
                    blocked_only:bool=False,db=Depends(db_session),actor=Depends(human)):
    await authorize_sprint(db,actor,project_id,sprint_id=sprint_id)
    return await sprint_board(db,project_id,sprint_id,column=column,overdue_only=overdue_only,blocked_only=blocked_only)


@router.put('/{sprint_id}')
async def edit_sprint(project_id:str,sprint_id:str,payload:SprintEdit,x_csrf_token:str|None=Header(default=None),
                      db=Depends(db_session),actor=Depends(human)):
    await authorize_sprint(db,actor,project_id,x_csrf_token,write=True,sprint_id=sprint_id)
    try: sprint=await revise_sprint(db,project_id,sprint_id,payload.expected_revision,payload.model_dump(),actor[0].id)
    except ValueError as exc: raise HTTPException(status_code=409,detail=str(exc)) from exc
    await db.commit(); return sprint_view(sprint)


@router.get('/{sprint_id}/history')
async def sprint_history(project_id:str,sprint_id:str,db=Depends(db_session),actor=Depends(human)):
    await authorize_sprint(db,actor,project_id,sprint_id=sprint_id)
    revisions=(await db.scalars(select(AgentSprintRevision).where(AgentSprintRevision.sprint_id==sprint_id)
        .order_by(AgentSprintRevision.revision))).all()
    return {'revisions':[{'revision':r.revision,'snapshot':r.snapshot,'action':r.action,
                         'actor_id':r.actor_id,'created_at':r.created_at} for r in revisions]}


@router.post('/{sprint_id}/{action}')
async def control_sprint(project_id:str,sprint_id:str,action:Action,payload:SprintTransition,
                         x_csrf_token:str|None=Header(default=None),db=Depends(db_session),actor=Depends(human)):
    await authorize_sprint(db,actor,project_id,x_csrf_token,write=True,sprint_id=sprint_id)
    try: sprint=await transition_sprint(db,project_id,sprint_id,action,payload.expected_revision,actor[0].id)
    except ValueError as exc: raise HTTPException(status_code=409,detail=str(exc)) from exc
    await db.commit(); return sprint_view(sprint)
