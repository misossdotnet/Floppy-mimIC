"""Équipe IA : listes serveur accessibles, filtres et mutations via les services métier."""
from typing import Literal
from uuid import UUID, uuid4

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import RedirectResponse
from pydantic import ValidationError
from sqlalchemy import select

from app.agent_api import db_session, human_project
from app.agent_models import AgentTask, AgentPlanVersion, AgentTeam, AgentTeamSkill, SkillProfile
from app.agent_sprint_web import display_date, web_actor
from app.conversation_models import CoordinationChannel
from app.conversation_service import channel_view, history
from app.identity_models import Project, ProjectMember, User
from app.identity_service import audit, project_permission
from app.incident_api import Action, IncidentChange, IncidentNew, Priority, State, authorize_incident
from app.incident_models import TechnicalIncident
from app.incident_service import (IncidentMissing, ack_notification, change_incident, create_incident,
    incident_list, incident_view, notifications)
from app.memory_api import Action as MemoryAction, Kind, MemoryDecision, MemoryEdit, MemoryNew, authorize_memory
from app.llm_trace import redact
from app.memory_models import ProjectMemory, ProjectMemoryVersion
from app.memory_service import (VALIDATION_PERMISSIONS, create_memory, decide_memory, grant_view,
    memory_view, project_grants, revise_memory, revoke_grant, version_view)

router = APIRouter(prefix='/projects/{project_id}/agents', tags=['agent-space-pages'])
View = Literal['memory', 'coordination', 'incidents']
MemoryState = Literal['Brouillon IA', 'À revoir', 'Validé', 'Rejeté', 'Obsolète']


async def projects_for(db, user):
    query = select(Project)
    if not user.is_platform_admin:
        query = query.join(ProjectMember, ProjectMember.project_id == Project.id).where(
            ProjectMember.user_id == user.id, ProjectMember.is_active.is_(True))
    return (await db.scalars(query.order_by(Project.name, Project.id))).all()


async def render(request, db, actor, project, view, *, error=None, status_code=200, form=None, **data):
    from app.main import page
    permissions = {'platform_admin': actor[0].is_platform_admin}
    for code in ('memory.propose', 'incident.report', 'incident.manage', 'agent.run.manage'):
        permissions[code.replace('.', '_')] = project.status != 'archived' and await project_permission(db, actor[0], project.id, code)
    response = page(request, 'agent_space', actor[0], actor[2], project=project, view=view,
        projects=await projects_for(db, actor[0]), display_date=display_date, permissions=permissions,
        error_message=error, form=form or {}, request_key=str(uuid4()), **data)
    response.status_code = status_code
    return response


@router.get('')
async def team_home(request: Request, project_id: str, db=Depends(db_session), actor=Depends(web_actor)):
    from app.main import page
    project = await human_project(db, actor[0], project_id, 'agent.read')
    teams = (await db.scalars(select(AgentTeam).where(AgentTeam.project_id == project_id).order_by(AgentTeam.name))).all()
    plans = (await db.scalars(select(AgentPlanVersion).where(AgentPlanVersion.project_id == project_id)
        .order_by(AgentPlanVersion.created_at.desc()).limit(20))).all()
    return page(request, 'agent_home', actor[0], actor[2], project=project, teams=teams, plans=plans)


async def memory_data(db, actor, project, *, kind=None, status=None, q='', selected_id=None, offset=0):
    query = select(ProjectMemory).join(ProjectMemoryVersion,
        (ProjectMemoryVersion.memory_id == ProjectMemory.id) &
        (ProjectMemoryVersion.version_number == ProjectMemory.current_version_number)).where(
        ProjectMemory.project_id == project.id, ProjectMemory.scope == 'project')
    if kind: query = query.where(ProjectMemory.kind == kind)
    if status: query = query.where(ProjectMemoryVersion.status == status)
    if q: query = query.where(ProjectMemoryVersion.title.icontains(q, autoescape=True) |
                              ProjectMemoryVersion.content.icontains(q, autoescape=True))
    rows = (await db.scalars(query.order_by(ProjectMemory.created_at.desc(), ProjectMemory.id).offset(offset).limit(21))).all()
    selected, versions, reference, can_decide = None, [], None, False
    if selected_id:
        memory = await authorize_memory(db, actor, project.id, memory_id=selected_id)
        selected = await memory_view(db, memory)
        versions = list(await db.scalars(select(ProjectMemoryVersion).where(ProjectMemoryVersion.memory_id == memory.id)
            .order_by(ProjectMemoryVersion.version_number.desc())))
        reference = await db.get(ProjectMemoryVersion, memory.reference_version_id) if memory.reference_version_id else None
        can_decide = project.status != 'archived' and await project_permission(db, actor[0], project.id, VALIDATION_PERMISSIONS[memory.kind])
    globals_ = [{**grant_view(g, m, v), 'content': v.content, 'source_note': v.source_note}
                for g, m, v in await project_grants(db, project.id)]
    return {'memories': [await memory_view(db, m) for m in rows[:20]], 'selected': selected, 'versions': versions,
            'reference': reference, 'can_decide': can_decide, 'global_refs': globals_,
            'next_offset': offset + 20 if len(rows) > 20 else None}


async def coordination_data(db, project, *, selected_id=None, kind=None, offset=0, message_offset=0):
    query = select(CoordinationChannel).where(CoordinationChannel.project_id == project.id)
    if kind: query = query.where(CoordinationChannel.kind == kind)
    rows = (await db.scalars(query.order_by(CoordinationChannel.created_at.desc(), CoordinationChannel.id)
        .offset(offset).limit(21))).all()
    selected, messages, message_next = None, [], None
    if selected_id:
        row = await db.scalar(select(CoordinationChannel).where(CoordinationChannel.id == selected_id,
                                                               CoordinationChannel.project_id == project.id))
        if not row: raise HTTPException(status_code=404)
        selected = channel_view(row)
        result = await history(db, project.id, channel_id=row.id, limit=20, offset=message_offset)
        messages, message_next = result['messages'], result['next_offset']
    profiles = {p.id: f'{p.label} v{p.version_number}' for p in (await db.scalars(select(SkillProfile)
        .join(AgentTeamSkill, AgentTeamSkill.skill_profile_id == SkillProfile.id)
        .join(AgentTeam, AgentTeam.id == AgentTeamSkill.team_id).where(AgentTeam.project_id == project.id))).all()}
    return {'channels': [channel_view(r) for r in rows[:20]], 'selected': selected, 'messages': messages,
            'profile_names': profiles, 'message_next': message_next,
            'next_offset': offset + 20 if len(rows) > 20 else None}


async def incident_data(db, actor, project, *, selected_id=None, status=None, priority=None, profile_id=None, offset=0, inbox_offset=0):
    result = await incident_list(db, project.id, status=status, priority=priority, profile_id=profile_id, limit=20, offset=offset)
    selected = None
    if selected_id:
        row = await db.scalar(select(TechnicalIncident).where(TechnicalIncident.id == selected_id, TechnicalIncident.project_id == project.id))
        if not row: raise HTTPException(status_code=404)
        selected = await incident_view(db, row, with_history=True)
    teams = (await db.scalars(select(AgentTeam).where(AgentTeam.project_id == project.id,
        AgentTeam.is_active.is_(True)).order_by(AgentTeam.name))).all()
    profiles = (await db.execute(select(SkillProfile, AgentTeamSkill.team_id).join(AgentTeamSkill,
        AgentTeamSkill.skill_profile_id == SkillProfile.id).join(AgentTeam, AgentTeam.id == AgentTeamSkill.team_id)
        .where(AgentTeam.project_id == project.id, AgentTeam.is_active.is_(True), SkillProfile.is_active.is_(True)))).all()
    tasks = (await db.scalars(select(AgentTask).where(AgentTask.project_id == project.id,
        AgentTask.team_id.in_([t.id for t in teams])).order_by(AgentTask.title, AgentTask.id).limit(500))).all()
    inbox = await notifications(db, project.id, user_id=actor[0].id, limit=20, offset=inbox_offset)
    return {**result, 'selected': selected, 'teams': teams, 'profiles': profiles, 'tasks': tasks,
            'inbox': inbox['notifications'], 'inbox_next': inbox['next_offset']}


@router.get('/memory')
async def memory_page(request: Request, project_id: str, project: UUID | None = None,
                      selected: UUID | None = None, kind: Kind | Literal[''] | None = None, status: MemoryState | Literal[''] | None = None,
                      q: str = Query('', max_length=200), offset: int = Query(0, ge=0, le=10000),
                      db=Depends(db_session), actor=Depends(web_actor)):
    current = await human_project(db, actor[0], project_id, 'memory.read')
    if project and str(project) != project_id:
        await human_project(db, actor[0], str(project), 'memory.read')
        return RedirectResponse(f'/projects/{project}/agents/memory', status_code=303)
    return await render(request, db, actor, current, 'memory', filters={'kind': kind, 'status': status, 'q': q},
        **await memory_data(db, actor, current, kind=kind, status=status, q=q, selected_id=str(selected) if selected else None, offset=offset))


@router.get('/coordination')
async def coordination_page(request: Request, project_id: str, project: UUID | None = None,
                            selected: UUID | None = None, kind: Literal['', 'direct', 'broadcast', 'thread'] | None = None,
                            offset: int = Query(0, ge=0, le=10000), message_offset: int = Query(0, ge=0, le=10000),
                            db=Depends(db_session), actor=Depends(web_actor)):
    current = await human_project(db, actor[0], project_id, 'agent.read')
    if project and str(project) != project_id:
        await human_project(db, actor[0], str(project), 'agent.read')
        return RedirectResponse(f'/projects/{project}/agents/coordination', status_code=303)
    return await render(request, db, actor, current, 'coordination', filters={'kind': kind},
        **await coordination_data(db, current, selected_id=str(selected) if selected else None, kind=kind, offset=offset, message_offset=message_offset))


@router.get('/incidents')
async def incidents_page(request: Request, project_id: str, project: UUID | None = None,
                         selected: UUID | None = None, status: State | Literal[''] | None = None, priority: Priority | Literal[''] | None = None,
                         profile_id: UUID | Literal[''] | None = None, offset: int = Query(0, ge=0, le=10000),
                         inbox_offset: int = Query(0, ge=0, le=10000),
                         db=Depends(db_session), actor=Depends(web_actor)):
    current = await authorize_incident(db, actor, project_id)
    if project and str(project) != project_id:
        await authorize_incident(db, actor, str(project))
        return RedirectResponse(f'/projects/{project}/agents/incidents', status_code=303)
    return await render(request, db, actor, current, 'incidents', filters={'status': status, 'priority': priority, 'profile_id': str(profile_id) if profile_id else None},
        **await incident_data(db, actor, current, selected_id=str(selected) if selected else None,
            status=status, priority=priority, profile_id=str(profile_id) if profile_id else None, offset=offset, inbox_offset=inbox_offset))


def form_number(value, convert):
    try: return convert(value)
    except (ValueError, TypeError): raise ValueError('Nombre ou version invalide.') from None


async def form_error(request, db, actor, project, view, raw, exc, selected_id=None):
    project_id, actor_id = project.id, actor[0].id
    await db.rollback()
    project = await db.get(Project, project_id)
    actor = (await db.get(User, actor_id), actor[1], actor[2])
    audit(db, 'agent.space.' + view, 'denied', actor_id=actor_id, project_id=project_id)
    await db.commit()
    data = (await memory_data(db, actor, project, selected_id=selected_id) if view == 'memory' else
            await incident_data(db, actor, project, selected_id=selected_id))
    message = 'Vérifiez les champs et leurs limites.' if isinstance(exc, ValidationError) else str(exc)
    return await render(request, db, actor, project, view, error=message, status_code=409,
                        form={k: redact(str(v)) for k, v in raw.items() if k != 'csrf_token'}, filters={}, **data)


@router.post('/memory')
@router.post('/memory/{memory_id}/edit')
async def memory_save(request: Request, project_id: str, memory_id: str | None = None,
                      db=Depends(db_session), actor=Depends(web_actor)):
    raw = await request.form()
    await authorize_memory(db, actor, project_id, memory_id=memory_id, write=True, csrf=raw.get('csrf_token'))
    project = await human_project(db, actor[0], project_id, 'memory.read')
    try:
        values = {k: raw.get(k, '') for k in ('title', 'content', 'source_note', 'change_note')}
        values['confidence'] = form_number(raw['confidence'], float) if raw.get('confidence') else None
        if memory_id:
            memory = await authorize_memory(db, actor, project_id, memory_id=memory_id)
            head = await db.get(ProjectMemoryVersion, (await memory_view(db, memory))['current']['id'])
            values['source_refs'] = head.source_refs
            payload = MemoryEdit(**values, expected_version=form_number(raw.get('expected_version', '0'), int))
            row = await revise_memory(db, memory_id, project_id, payload.expected_version,
                payload.model_dump(mode='json'), actor_id=actor[0].id)
        else:
            payload = MemoryNew(**values, kind=raw.get('kind'))
            row = await create_memory(db, project_id, payload.kind, payload.model_dump(mode='json'), actor[0].id)
    except (ValueError, ValidationError) as exc:
        return await form_error(request, db, actor, project, 'memory', raw, exc, memory_id)
    await db.commit()
    return RedirectResponse(f'/projects/{project_id}/agents/memory?selected={row.id}', status_code=303)


@router.post('/memory/{memory_id}/{action}')
async def memory_decision(request: Request, project_id: str, memory_id: str, action: MemoryAction,
                          db=Depends(db_session), actor=Depends(web_actor)):
    raw = await request.form()
    memory = await authorize_memory(db, actor, project_id, memory_id=memory_id)
    await authorize_memory(db, actor, project_id, write=True, csrf=raw.get('csrf_token'),
        permission='memory.propose' if action == 'submit' else VALIDATION_PERMISSIONS[memory.kind])
    project = await human_project(db, actor[0], project_id, 'memory.read')
    try:
        payload = MemoryDecision(expected_version=form_number(raw.get('expected_version', '0'), int), reason=raw.get('reason', ''))
        await decide_memory(db, memory_id, project_id, payload.expected_version, action, actor[0].id, payload.reason)
    except (ValueError, ValidationError) as exc:
        return await form_error(request, db, actor, project, 'memory', raw, exc, memory_id)
    await db.commit()
    return RedirectResponse(f'/projects/{project_id}/agents/memory?selected={memory_id}', status_code=303)


@router.post('/memory-grants/{grant_id}/revoke')
async def memory_revoke(request: Request, project_id: str, grant_id: UUID,
                        db=Depends(db_session), actor=Depends(web_actor)):
    raw = await request.form()
    await authorize_memory(db, actor, project_id, write=True, csrf=raw.get('csrf_token'), permission='agent.run.manage')
    try: await revoke_grant(db, project_id, str(grant_id), actor[0].id)
    except ValueError as exc: raise HTTPException(status_code=409, detail=str(exc)) from exc
    await db.commit()
    return RedirectResponse(f'/projects/{project_id}/agents/memory', status_code=303)


@router.post('/incidents')
async def incident_report(request: Request, project_id: str, db=Depends(db_session), actor=Depends(web_actor)):
    raw = await request.form()
    project = await authorize_incident(db, actor, project_id, write=True, csrf=raw.get('csrf_token'), report=True)
    try:
        payload = IncidentNew(**{k: raw.get(k, '') for k in ('request_key', 'team_id', 'responsible_profile_id', 'title', 'description', 'priority')},
                              task_id=raw.get('task_id') or None)
        row, _ = await create_incident(db, project_id, payload.model_dump(mode='json'), actor[0].id)
    except (ValueError, ValidationError) as exc:
        return await form_error(request, db, actor, project, 'incidents', raw, exc)
    await db.commit()
    return RedirectResponse(f'/projects/{project_id}/agents/incidents?selected={row.id}', status_code=303)


@router.post('/incidents/{incident_id}/{action}')
async def incident_action(request: Request, project_id: str, incident_id: UUID, action: Action,
                          db=Depends(db_session), actor=Depends(web_actor)):
    raw = await request.form()
    project = await authorize_incident(db, actor, project_id, write=True, csrf=raw.get('csrf_token'))
    try:
        values = {'expected_revision': form_number(raw.get('expected_revision', '0'), int), 'reason': raw.get('reason', '')}
        keys = {'assign': ('responsible_profile_id',), 'escalate': ('responsible_profile_id',), 'priority': ('priority',),
                'edit': ('title', 'description')}.get(action, ())
        values.update({k: raw.get(k) for k in keys})
        payload = IncidentChange(**values)
        await change_incident(db, project_id, str(incident_id), action, payload.for_action(action), actor[0].id)
    except (ValueError, ValidationError) as exc:
        if isinstance(exc, IncidentMissing): raise HTTPException(status_code=404) from exc
        return await form_error(request, db, actor, project, 'incidents', raw, exc, str(incident_id))
    await db.commit()
    return RedirectResponse(f'/projects/{project_id}/agents/incidents?selected={incident_id}', status_code=303)


@router.post('/notifications/{notification_id}/ack')
async def notification_ack(request: Request, project_id: str, notification_id: UUID,
                           db=Depends(db_session), actor=Depends(web_actor)):
    from app.agent_api import check_csrf
    raw = await request.form()
    check_csrf(actor[2], raw.get('csrf_token'))
    project = await authorize_incident(db, actor, project_id)
    try: result = await ack_notification(db, project_id, str(notification_id), user_id=actor[0].id)
    except IncidentMissing as exc: raise HTTPException(status_code=404) from exc
    await db.commit()
    if result['status'] == 'expired':
        return await render(request, db, actor, project, 'incidents', error='Notification expirée.', status_code=409,
            filters={}, **await incident_data(db, actor, project))
    return RedirectResponse(f'/projects/{project_id}/agents/incidents', status_code=303)
