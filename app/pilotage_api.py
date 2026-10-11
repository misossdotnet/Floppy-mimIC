"""Lecture du pilotage humain sous ACL projet, HTML et JSON identiques."""
from typing import Literal
from uuid import UUID

from fastapi import APIRouter, Depends, Query, Request
from fastapi.responses import RedirectResponse
from sqlalchemy import select

from app.agent_api import db_session, human, human_project
from app.agent_sprint_web import display_date, web_actor
from app.identity_service import project_permission
from app.identity_models import Project, ProjectMember, RolePermission
from app.memory_api import MemoryRoute, private_response
from app.pilotage_service import KINDS, STATES, projection

Kind = Literal['', 'documents', 'sessions', 'tasks', 'messages', 'worktrees', 'reviews', 'quotas', 'alerts', 'budgets', 'budget_alerts', 'coffy']
State = Literal['', 'En attente', 'En cours', 'Décision humaine', 'Terminé', 'Échec']
CoffyPeriod = Literal['7d', '30d', '90d', 'all']
CoffyStatus = Literal['', 'pending', 'queued', 'running', 'retrying', 'success', 'failed', 'timeout', 'cancelled']
CoffyProvider = Literal['', 'openai', 'azure_openai', 'anthropic', 'ollama', 'openai_compatible']
router = APIRouter(tags=['pilotage'], route_class=MemoryRoute, dependencies=[Depends(private_response)])


async def projects_for(db, user):
    query = select(Project)
    if not user.is_platform_admin:
        allowed_roles = select(RolePermission.role_code).where(RolePermission.permission_code == 'pilotage.read')
        readable_roles = select(RolePermission.role_code).where(RolePermission.permission_code == 'project.read')
        query = query.join(ProjectMember, ProjectMember.project_id == Project.id).where(
            ProjectMember.user_id == user.id, ProjectMember.is_active.is_(True),
            ProjectMember.role_code.in_(allowed_roles), ProjectMember.role_code.in_(readable_roles))
    return (await db.scalars(query.order_by(Project.name, Project.id))).all()


async def read(db, actor, project_id, kind, state, offset, coffy_filters):
    project = await human_project(db, actor[0], project_id, 'pilotage.read')
    data = await projection(db, project_id, agent_read=await project_permission(db, actor[0], project_id, 'agent.read'),
        coffy_read=await project_permission(db, actor[0], project_id, 'scheduled_task.read'),
        coffy_filters=coffy_filters, kind=kind, state=state, offset=offset)
    return project, data


def coffy_filter_values(period, status, provider, profile, subject):
    return {'period': period, 'status': status, 'provider': provider,
            'profile': str(profile) if profile else None,
            'subject': str(subject) if subject else None}


@router.get('/api/projects/{project_id}/pilotage')
async def api(project_id: UUID, kind: Kind = '', state: State = '', offset: int = Query(0, ge=0, le=10000),
              coffy_period: CoffyPeriod = '30d', coffy_status: CoffyStatus = '',
              coffy_provider: CoffyProvider = '', coffy_profile: UUID | None = None,
              coffy_subject: UUID | None = None,
              db=Depends(db_session), actor=Depends(human)):
    _, data = await read(db, actor, str(project_id), kind, state, offset,
        coffy_filter_values(coffy_period, coffy_status, coffy_provider, coffy_profile, coffy_subject))
    return data


@router.get('/projects/{project_id}/pilotage')
async def home(request: Request, project_id: UUID, project: UUID | None = None, kind: Kind = '', state: State = '',
               offset: int = Query(0, ge=0, le=10000), coffy_period: CoffyPeriod = '30d',
               coffy_status: CoffyStatus = '', coffy_provider: CoffyProvider = '',
               coffy_profile: UUID | None = None, coffy_subject: UUID | None = None,
               db=Depends(db_session), actor=Depends(web_actor)):
    from app.main import page
    current = await human_project(db, actor[0], str(project_id), 'pilotage.read')
    if project and project != project_id:
        await human_project(db, actor[0], str(project), 'pilotage.read')
        # Une sélection de projet remet filtres et pagination à zéro.
        return RedirectResponse(f'/projects/{project}/pilotage', 303)
    _, data = await read(db, actor, str(project_id), kind, state, offset,
        coffy_filter_values(coffy_period, coffy_status, coffy_provider, coffy_profile, coffy_subject))
    return page(request, 'pilotage', actor[0], actor[2], project=current,
                projects=await projects_for(db, actor[0]), filters={'kind': kind, 'state': state},
                kinds=KINDS, states=STATES, display_date=display_date, pilotage=data)
