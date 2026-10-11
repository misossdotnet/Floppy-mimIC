"""API incidents : signalement technique, décisions humaines et boîtes isolées."""
from typing import Literal
from uuid import UUID

from fastapi import APIRouter, Depends, Header, HTTPException, Query
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import select

from app.agent_api import authorized_agent, bearer_token, check_csrf, db_session, human, human_project
from app.incident_models import TechnicalIncident
from app.incident_service import (IncidentMissing, ack_notification, change_incident, create_incident,
    incident_list, incident_view, notifications)
from app.identity_models import Project
from app.identity_service import audit
from app.memory_api import MemoryRoute, SourceRef, private_response

Priority = Literal['P0', 'P1', 'P2', 'P3']
State = Literal['open', 'acknowledged', 'escalated', 'resolved']
Action = Literal['acknowledge', 'assign', 'escalate', 'priority', 'edit', 'resolve']
router = APIRouter(prefix='/api/projects/{project_id}/incidents', tags=['incidents'],
    route_class=MemoryRoute, dependencies=[Depends(private_response)])
agent_router = APIRouter(prefix='/api/projects/{project_id}/agent-incidents', tags=['agent-incidents'],
    route_class=MemoryRoute, dependencies=[Depends(private_response)])


class IncidentNew(BaseModel):
    model_config = ConfigDict(extra='forbid', str_strip_whitespace=True)
    request_key: UUID
    team_id: UUID
    task_id: UUID | None = None
    responsible_profile_id: UUID
    title: str = Field(min_length=1, max_length=180)
    description: str = Field(min_length=1, max_length=4000)
    priority: Priority = 'P2'


class AgentIncidentNew(IncidentNew):
    session_id: UUID


class IncidentChange(BaseModel):
    model_config = ConfigDict(extra='forbid', str_strip_whitespace=True)
    expected_revision: int = Field(ge=1, strict=True)
    reason: str = Field(default='', max_length=1000)
    responsible_profile_id: UUID | None = None
    priority: Priority | None = None
    title: str | None = Field(default=None, min_length=1, max_length=180)
    description: str | None = Field(default=None, min_length=1, max_length=4000)
    source_refs: list[SourceRef] = Field(default_factory=list, max_length=10)

    def for_action(self, action):
        required = {'assign': {'responsible_profile_id'}, 'escalate': {'responsible_profile_id'},
                    'priority': {'priority'}, 'edit': {'title', 'description'}, 'resolve': {'source_refs'},
                    'acknowledge': set()}[action]
        extra = self.model_fields_set - required - {'expected_revision', 'reason'}
        values = self.model_dump(mode='json')
        if extra or any(values[k] is None for k in required):
            raise ValueError('Champs incompatibles avec cette action.')
        return {k: values[k] for k in required | {'expected_revision', 'reason'}}


class AgentSessionInput(BaseModel):
    model_config = ConfigDict(extra='forbid')
    session_id: UUID


async def authorize_incident(db, actor, project_id, *, write=False, csrf=None, report=False):
    if write: check_csrf(actor[2], csrf)
    project = await human_project(db, actor[0], project_id,
        'incident.report' if report else 'incident.manage' if write else 'agent.read')
    if write:
        project = await db.scalar(select(Project).where(Project.id == project_id).with_for_update()
            .execution_options(populate_existing=True))
        if project.status == 'archived': raise HTTPException(status_code=409, detail='Projet archivé.')
    return project


async def reject(db, project_id, actor_id, exc, action='incident.manage'):
    await db.rollback()
    audit(db, action, 'denied', actor_id=actor_id, project_id=project_id)
    await db.commit()
    raise HTTPException(status_code=404 if isinstance(exc, IncidentMissing) else 409, detail=str(exc))


@router.post('', status_code=201)
async def report(project_id: str, payload: IncidentNew, x_csrf_token: str | None = Header(default=None),
                 db=Depends(db_session), actor=Depends(human)):
    await authorize_incident(db, actor, project_id, write=True, csrf=x_csrf_token, report=True)
    try:
        row, created = await create_incident(db, project_id, payload.model_dump(mode='json'), actor[0].id)
    except ValueError as exc: await reject(db, project_id, actor[0].id, exc, 'incident.report')
    result = await incident_view(db, row, with_history=True)
    await db.commit()
    return {'incident': result, 'created': created}


@router.get('')
async def listing(project_id: str, status: State | None = None, priority: Priority | None = None,
                  profile_id: UUID | None = None, limit: int = Query(50, ge=1, le=100),
                  offset: int = Query(0, ge=0, le=10000), db=Depends(db_session), actor=Depends(human)):
    await authorize_incident(db, actor, project_id)
    return await incident_list(db, project_id, status=status, priority=priority,
        profile_id=str(profile_id) if profile_id else None, limit=limit, offset=offset)


@router.get('/notifications')
async def inbox(project_id: str, limit: int = Query(50, ge=1, le=100), offset: int = Query(0, ge=0, le=10000),
                db=Depends(db_session), actor=Depends(human)):
    await authorize_incident(db, actor, project_id)
    return await notifications(db, project_id, user_id=actor[0].id, limit=limit, offset=offset)


@router.post('/notifications/poll')
async def poll(project_id: str, x_csrf_token: str | None = Header(default=None),
               db=Depends(db_session), actor=Depends(human)):
    check_csrf(actor[2], x_csrf_token)
    await authorize_incident(db, actor, project_id)
    result = await notifications(db, project_id, user_id=actor[0].id, poll=True)
    await db.commit()
    return result


@router.post('/notifications/{notification_id}/ack')
async def ack(project_id: str, notification_id: UUID, x_csrf_token: str | None = Header(default=None),
              db=Depends(db_session), actor=Depends(human)):
    check_csrf(actor[2], x_csrf_token)
    await authorize_incident(db, actor, project_id)
    try: result = await ack_notification(db, project_id, str(notification_id), user_id=actor[0].id)
    except ValueError as exc: await reject(db, project_id, actor[0].id, exc)
    await db.commit()
    if result['status'] == 'expired': raise HTTPException(status_code=409, detail='Notification expirée.')
    return result


@router.get('/{incident_id}')
async def detail(project_id: str, incident_id: UUID, db=Depends(db_session), actor=Depends(human)):
    await authorize_incident(db, actor, project_id)
    row = await db.scalar(select(TechnicalIncident).where(TechnicalIncident.project_id == project_id,
                                                        TechnicalIncident.id == str(incident_id)))
    if not row: raise HTTPException(status_code=404)
    return await incident_view(db, row, with_history=True)


@router.post('/{incident_id}/{action}')
async def change(project_id: str, incident_id: UUID, action: Action, payload: IncidentChange,
                 x_csrf_token: str | None = Header(default=None), db=Depends(db_session), actor=Depends(human)):
    await authorize_incident(db, actor, project_id, write=True, csrf=x_csrf_token)
    try:
        row = await change_incident(db, project_id, str(incident_id), action, payload.for_action(action), actor[0].id)
    except ValueError as exc: await reject(db, project_id, actor[0].id, exc)
    result = await incident_view(db, row, with_history=True)
    await db.commit()
    return result


async def technical(db, project_id, session_id, authorization, capability):
    return await authorized_agent(db, project_id=project_id, session_id=str(session_id), authorization=authorization,
        capability=capability, permission='incident.report' if capability == 'incident.report' else 'agent.read')


@agent_router.post('', status_code=201)
async def agent_report(project_id: str, payload: AgentIncidentNew, authorization: str | None = Header(default=None),
                       db=Depends(db_session)):
    session = await technical(db, project_id, payload.session_id, authorization, 'incident.report')
    try:
        row, created = await create_incident(db, project_id, payload.model_dump(mode='json', exclude={'session_id'}),
            session.created_by, session=session, sensitive_token=bearer_token(authorization))
    except ValueError as exc: await reject(db, project_id, session.created_by, exc, 'incident.report')
    result = await incident_view(db, row, with_history=True)
    await db.commit()
    return {'incident': result, 'created': created}


@agent_router.post('/notifications/poll')
async def agent_poll(project_id: str, payload: AgentSessionInput, authorization: str | None = Header(default=None),
                     db=Depends(db_session)):
    session = await technical(db, project_id, payload.session_id, authorization, 'incident.read')
    result = await notifications(db, project_id, session=session, poll=True)
    await db.commit()
    return result


@agent_router.post('/notifications/{notification_id}/ack')
async def agent_ack(project_id: str, notification_id: UUID, payload: AgentSessionInput,
                    authorization: str | None = Header(default=None), db=Depends(db_session)):
    session = await technical(db, project_id, payload.session_id, authorization, 'incident.ack')
    try: result = await ack_notification(db, project_id, str(notification_id), session=session)
    except ValueError as exc: await reject(db, project_id, session.created_by, exc)
    await db.commit()
    if result['status'] == 'expired': raise HTTPException(status_code=409, detail='Notification expirée.')
    return result


@agent_router.get('/{incident_id}')
async def agent_detail(project_id: str, incident_id: UUID, session_id: UUID,
                       authorization: str | None = Header(default=None), db=Depends(db_session)):
    session = await technical(db, project_id, session_id, authorization, 'incident.read')
    row = await db.scalar(select(TechnicalIncident).where(TechnicalIncident.project_id == project_id,
        TechnicalIncident.team_id == session.team_id, TechnicalIncident.id == str(incident_id)))
    if not row: raise HTTPException(status_code=404)
    return await incident_view(db, row, with_history=True)
