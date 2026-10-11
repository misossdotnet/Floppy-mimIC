"""Écrans de sprint serveur : mêmes guards et services que l'API JSON."""
from datetime import datetime, timedelta, timezone
from typing import Literal
from zoneinfo import ZoneInfo

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import RedirectResponse
from pydantic import ValidationError
from sqlalchemy import select

from app.agent_api import db_session
from app.agent_models import AgentPlanVersion, AgentSprint, AgentSprintRevision, AgentTeam
from app.agent_coordinator import plan_tasks
from app.agent_sprint_api import Action, SprintDraft, SprintEdit, authorize_sprint
from app.agent_sprint_service import create_sprint, revise_sprint, transition_sprint, sprint_board, sprint_items
from app.identity_models import utcnow
from app.identity_service import project_permission

router=APIRouter(prefix='/projects/{project_id}/agent-sprints',tags=['sprint-pages'])
PARIS=ZoneInfo('Europe/Paris')
COLUMNS={'todo':'À faire','in_progress':'En cours','blocked':'Bloqué','done':'Terminé techniquement','cancelled':'Annulé'}
STATES={'draft':'Brouillon','active':'Actif','closed':'Clos','cancelled':'Annulé'}
WebColumn=Literal['','todo','in_progress','blocked','done','cancelled']


async def web_actor(request:Request,db=Depends(db_session)):
    from app.main import principal
    return await principal(request,db)


def local_input(value):
    return value.astimezone(PARIS).strftime('%Y-%m-%dT%H:%M') if value else ''


def display_date(value):
    return value.astimezone(PARIS).strftime('%d/%m/%Y %H:%M') if value else '—'


def parse_local(value):
    parsed=datetime.fromisoformat(value)
    if parsed.tzinfo: return parsed.astimezone(timezone.utc)
    aware=parsed.replace(tzinfo=PARIS)
    if (aware.utcoffset()!=parsed.replace(tzinfo=PARIS,fold=1).utcoffset()
            or aware.astimezone(timezone.utc).astimezone(PARIS).replace(tzinfo=None)!=parsed):
        raise ValueError("Heure de Paris ambiguë ou inexistante : choisissez une autre heure.")
    return aware.astimezone(timezone.utc)


async def form_data(request):
    form=await request.form()
    items=[{'task_id':tid,'target_due_at':parse_local(form[f'due_{tid}']) if form.get(f'due_{tid}') else None}
           for tid in form.getlist('task_ids')]
    values={'name':form.get('name',''),'goal':form.get('goal',''),'starts_at':parse_local(form.get('starts_at','')),
            'ends_at':parse_local(form.get('ends_at','')),'items':items}
    return form,values


async def render(request,db,actor,project,*,plan_id=None,sprint_id=None,form=None,error=None,status=200,
                 column=None,overdue_only=False,blocked_only=False):
    from app.main import page
    manage=project.status!='archived' and await project_permission(db,actor[0],project.id,'agent.run.manage')
    plans=(await db.execute(select(AgentPlanVersion,AgentTeam.name).join(AgentTeam,AgentTeam.id==AgentPlanVersion.team_id)
        .where(AgentPlanVersion.project_id==project.id,AgentPlanVersion.status=='approved')
        .order_by(AgentPlanVersion.created_at.desc()))).all()
    sprints=(await db.scalars(select(AgentSprint).where(AgentSprint.project_id==project.id)
        .order_by(AgentSprint.starts_at.desc(),AgentSprint.id))).all()
    board=await sprint_board(db,project.id,sprint_id,column=column,overdue_only=overdue_only,blocked_only=blocked_only) if sprint_id else None
    history=[]
    if board:
        plan_id=board['sprint']['plan_version_id']
        history=(await db.scalars(select(AgentSprintRevision).where(AgentSprintRevision.sprint_id==sprint_id)
            .order_by(AgentSprintRevision.revision.desc()))).all()
    candidates=[]
    if plan_id:
        await authorize_sprint(db,actor,project.id,plan_id=plan_id)
        tasks=await plan_tasks(db,plan_id); parents={t.parent_task_id for t in tasks if t.parent_task_id}
        retained={i.task_id for i in await sprint_items(db,sprint_id)} if sprint_id else set()
        candidates=[t for t in tasks if t.id not in parents and (t.status not in {'completed','cancelled'} or t.id in retained)]
    if form is None:
        if board:
            s=board['sprint']; selected=await sprint_items(db,sprint_id)
            form={'name':s['name'],'goal':s['goal'],'starts_at':local_input(s['starts_at']),'ends_at':local_input(s['ends_at']),
                  'expected_revision':s['revision'],'task_ids':[i.task_id for i in selected],
                  'dues':{i.task_id:local_input(i.target_due_at) for i in selected}}
        else:
            now=utcnow()
            form={'name':'','goal':'','starts_at':local_input(now-timedelta(minutes=5)),
                  'ends_at':local_input(now+timedelta(days=14)),'task_ids':[],'dues':{}}
    response=page(request,'agent_sprints',actor[0],actor[2],project=project,board=board,sprints=sprints,
        plans=plans,plan_id=plan_id,candidates=candidates,form=form,columns=COLUMNS,states=STATES,
        filters={'column':column,'overdue_only':overdue_only,'blocked_only':blocked_only},history=history,
        display_date=display_date,error_message=error,permissions={'platform_admin':actor[0].is_platform_admin,'sprint_manage':manage})
    response.status_code=status
    return response


@router.get('')
async def sprint_list(request:Request,project_id:str,plan_id:str|None=None,db=Depends(db_session),actor=Depends(web_actor)):
    project=await authorize_sprint(db,actor,project_id)
    return await render(request,db,actor,project,plan_id=plan_id)


@router.get('/{sprint_id}')
async def sprint_page(request:Request,project_id:str,sprint_id:str,column:WebColumn|None=None,overdue_only:bool=False,
                      blocked_only:bool=False,db=Depends(db_session),actor=Depends(web_actor)):
    project=await authorize_sprint(db,actor,project_id,sprint_id=sprint_id)
    return await render(request,db,actor,project,sprint_id=sprint_id,column=column or None,overdue_only=overdue_only,blocked_only=blocked_only)


async def save_form(request,db,actor,project_id,sprint_id=None):
    raw=await request.form()
    project=await authorize_sprint(db,actor,project_id,raw.get('csrf_token'),write=True,sprint_id=sprint_id)
    plan_id=raw.get('plan_id') if not sprint_id else None
    if plan_id: await authorize_sprint(db,actor,project_id,plan_id=plan_id)
    try:
        _,values=await form_data(request)
        if sprint_id:
            payload=SprintEdit(**values,expected_revision=int(raw.get('expected_revision','0')))
            sprint=await revise_sprint(db,project_id,sprint_id,payload.expected_revision,payload.model_dump(),actor[0].id)
        else:
            payload=SprintDraft(**values,plan_id=plan_id)
            sprint=await create_sprint(db,project_id,str(payload.plan_id),payload.model_dump(),actor[0].id)
    except (ValueError,ValidationError) as exc:
        # Conserver la saisie, sans afficher les détails bruts de Pydantic.
        values={'name':raw.get('name',''),'goal':raw.get('goal',''),'starts_at':raw.get('starts_at',''),
                'ends_at':raw.get('ends_at',''),'task_ids':raw.getlist('task_ids'),
                'expected_revision':raw.get('expected_revision'),'dues':{tid:raw.get(f'due_{tid}','') for tid in raw.getlist('task_ids')}}
        error="Vérifiez le nom, les dates et la sélection." if isinstance(exc,ValidationError) else str(exc)
        return await render(request,db,actor,project,plan_id=plan_id,sprint_id=sprint_id,form=values,error=error,status=409)
    await db.commit()
    return RedirectResponse(f'/projects/{project_id}/agent-sprints/{sprint.id}',status_code=303)


@router.post('')
async def create_from_form(request:Request,project_id:str,db=Depends(db_session),actor=Depends(web_actor)):
    return await save_form(request,db,actor,project_id)


@router.post('/{sprint_id}/edit')
async def edit_from_form(request:Request,project_id:str,sprint_id:str,db=Depends(db_session),actor=Depends(web_actor)):
    return await save_form(request,db,actor,project_id,sprint_id)


@router.post('/{sprint_id}/{action}')
async def transition_from_form(request:Request,project_id:str,sprint_id:str,action:Action,
                               expected_revision:int=Form(...),csrf_token:str=Form(...),confirmation:bool=Form(False),
                               db=Depends(db_session),actor=Depends(web_actor)):
    project=await authorize_sprint(db,actor,project_id,csrf_token,write=True,sprint_id=sprint_id)
    try:
        if action in {'close','cancel'} and not confirmation:
            raise ValueError("Confirmez l'arrêt du suivi de ce sprint ; les tâches continuent indépendamment.")
        await transition_sprint(db,project_id,sprint_id,action,expected_revision,actor[0].id)
    except ValueError as exc:
        return await render(request,db,actor,project,sprint_id=sprint_id,error=str(exc),status=409)
    await db.commit(); return RedirectResponse(f'/projects/{project_id}/agent-sprints/{sprint_id}',status_code=303)
