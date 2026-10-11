"""Configuration humaine et admission du client connecté, sous ACL projet."""
from decimal import Decimal
from typing import Literal
from uuid import UUID

from fastapi import APIRouter, Depends, Header, HTTPException, Query, Response
from pydantic import BaseModel, ConfigDict, Field, model_validator
from sqlalchemy import select
from app.agent_api import authorized_agent, db_session, human, human_project
from app.agent_models import AgentTaskClaim, AgentTaskRun
from app.identity_service import audit, csrf_for
from app.runtime_models import RuntimeBinding, RuntimeQuota, RuntimeSelection, RuntimeUsage, RuntimeVersion
from app import runtime_service as service

router = APIRouter(prefix='/api/projects/{project_id}/runtimes', tags=['runtimes'])
agent_router = APIRouter(prefix='/api/projects/{project_id}/agent-tasks', tags=['agent-runtimes'])
Scope = Literal['project','agent','task','session','model']


class Input(BaseModel):
    model_config = ConfigDict(extra='forbid')
    reason: str = Field(min_length=1, max_length=500)


class RuntimeInput(Input):
    runtime_id: UUID | None = None
    expected_version: int = Field(default=0, ge=0, strict=True)
    name: str = Field(min_length=1,max_length=120)
    kind: Literal['llm','client']
    profile_version_id: UUID | None = None
    client_code: str | None = Field(default=None,max_length=32)
    timeout_seconds: int = Field(default=80,ge=1,le=3600,strict=True)
    enabled: bool = True
    include_memory: bool = False

    @model_validator(mode='after')
    def shape(self):
        if (self.kind == 'llm') != (self.profile_version_id is not None) or (self.kind == 'client') != (self.client_code is not None):
            raise ValueError('Choisissez exactement un profil LLM ou un client catalogué.')
        if self.kind == 'client' and self.include_memory:
            raise ValueError('Le client utilise l’API Mémoire ; injection réservée au worker interne.')
        if self.kind == 'llm' and self.timeout_seconds > 80:
            raise ValueError('Le worker documentaire est borné à 80 secondes.')
        return self


class BindingInput(Input):
    scope: Literal['project','agent','task','session']
    scope_id: str = Field(min_length=1,max_length=64)
    expected_version: int = Field(default=0,ge=0,strict=True)
    runtime_version_id: UUID | None = None


class QuotaInput(Input):
    scope: Scope
    scope_id: str = Field(min_length=1,max_length=64)
    expected_version: int = Field(default=0,ge=0,strict=True)
    max_tokens: int | None = Field(default=None,ge=1,le=2_000_000_000,strict=True)
    max_seconds: Decimal | None = Field(default=None,gt=0,le=1_000_000_000,max_digits=16,decimal_places=6)
    max_concurrency: int | None = Field(default=None,ge=1,le=1000,strict=True)
    max_cost: Decimal | None = Field(default=None,gt=0,le=1_000_000_000,max_digits=16,decimal_places=6)
    currency: str | None = Field(default=None,pattern=r'^[A-Z]{3}$')

    @model_validator(mode='after')
    def currency_required(self):
        if (self.max_cost is not None) != (self.currency is not None):
            raise ValueError('Un coût et sa devise doivent être renseignés ensemble.')
        return self


class ReconcileInput(Input):
    stopped_confirmed: bool = False


async def authorize(db, actor, project_id, token=None, permission='agent.read'):
    project = await human_project(db, actor[0], project_id, permission)
    if permission != 'agent.read':
        import hmac
        if not token or not hmac.compare_digest(token, csrf_for(actor[2], 'session')):
            raise HTTPException(status_code=403, detail='CSRF invalide.')
        if project.status == 'archived':
            raise HTTPException(status_code=409, detail='Projet archivé.')
    return project


async def mutation(db, project_id, actor, callback, *, event="runtime.mutation"):

    actor_id=actor[0].id
    try:
        row = await callback()
    except service.RuntimeDenied as exc:
        await db.rollback()
        audit(db,event,'denied',actor_id=actor_id,project_id=project_id,details={'error_code':exc.code})
        await db.commit()
        raise HTTPException(status_code=404 if event=='budget.mutation' and exc.code in ('alert_unavailable','suspension_unavailable','scope_unavailable') else 409,detail=exc.code) from None
    await db.commit()
    return service.view(row)


@router.get('')
async def runtime_list(project_id:str,response:Response,offset:int=Query(default=0,ge=0,le=10000),
                       db=Depends(db_session),actor=Depends(human)):
    await authorize(db,actor,project_id)
    response.headers['Cache-Control']='no-store'
    rows=(await db.scalars(select(RuntimeVersion).where(RuntimeVersion.project_id==project_id)
        .order_by(RuntimeVersion.created_at.desc(),RuntimeVersion.id).offset(offset).limit(51))).all()
    bindings=(await db.scalars(select(RuntimeBinding).where(RuntimeBinding.project_id==project_id,
                                                           RuntimeBinding.is_current.is_(True)))).all()
    return {'versions':[service.view(r) for r in rows[:50]],'bindings':[service.view(r) for r in bindings],
            'next_offset':offset+50 if len(rows)>50 else None}


@router.post('/versions',status_code=201)
async def runtime_save(project_id:str,payload:RuntimeInput,x_csrf_token:str|None=Header(default=None),
                       db=Depends(db_session),actor=Depends(human)):
    await authorize(db,actor,project_id,x_csrf_token,'runtime.configure')
    return await mutation(db,project_id,actor,lambda:service.revise_runtime(db,project_id,actor[0],payload))


@router.post('/bindings',status_code=201)
async def binding_save(project_id:str,payload:BindingInput,x_csrf_token:str|None=Header(default=None),
                       db=Depends(db_session),actor=Depends(human)):
    await authorize(db,actor,project_id,x_csrf_token,'runtime.configure')
    return await mutation(db,project_id,actor,lambda:service.revise_policy(db,project_id,actor[0],payload))


@router.post('/quotas',status_code=201)
async def quota_save(project_id:str,payload:QuotaInput,x_csrf_token:str|None=Header(default=None),
                     db=Depends(db_session),actor=Depends(human)):
    await authorize(db,actor,project_id,x_csrf_token,'quota.manage')
    return await mutation(db,project_id,actor,lambda:service.revise_policy(db,project_id,actor[0],payload,quota=True))


@router.get('/usage')
async def usage(project_id:str,response:Response,scope:Scope='project',scope_id:str|None=None,
                offset:int=Query(default=0,ge=0,le=10000),db=Depends(db_session),actor=Depends(human)):
    await authorize(db,actor,project_id)
    scope_id=scope_id or project_id
    try:
        await service.validate_scope(db,project_id,scope,scope_id)
    except service.RuntimeDenied:
        raise HTTPException(status_code=404) from None
    response.headers['Cache-Control']='no-store'
    rows=(await db.scalars(service.scope_query(project_id,scope,scope_id).order_by(
        RuntimeUsage.started_at.desc(),RuntimeUsage.id).offset(offset).limit(51))).all()
    policies=(await db.scalars(select(RuntimeQuota).where(RuntimeQuota.project_id==project_id,RuntimeQuota.scope==scope,
                                                        RuntimeQuota.scope_id==scope_id).order_by(RuntimeQuota.version.desc()))).all()
    selections=(await db.scalars(select(RuntimeSelection).where(RuntimeSelection.project_id==project_id).order_by(
        RuntimeSelection.created_at.desc()).limit(50))).all()
    return {'scope':scope,'scope_id':scope_id,'totals':await service.totals(db,project_id,scope,scope_id),
        'policies':[service.view(p) for p in policies], 'calls':[service.view(r) for r in rows[:50]],
        'selections':[{k:v for k,v in service.view(s).items() if k!='memory_snapshot'} for s in selections],
        'next_offset':offset+50 if len(rows)>50 else None,'accounting':'Depuis la migration 0021 ; traces antérieures hors registre.'}


@router.post('/usage/{usage_id}/reconcile')
async def usage_reconcile(project_id:str,usage_id:UUID,payload:ReconcileInput,x_csrf_token:str|None=Header(default=None),
                          db=Depends(db_session),actor=Depends(human)):
    await authorize(db,actor,project_id,x_csrf_token,'quota.manage')
    return await mutation(db,project_id,actor,lambda:service.reconcile(db,project_id,str(usage_id),actor[0],
                                                                    payload.reason,payload.stopped_confirmed))


class ClientStart(BaseModel):
    model_config=ConfigDict(extra='forbid')
    claim_id: UUID


@agent_router.post('/{task_id}/runtime',status_code=202)
async def start_client(project_id:str,task_id:UUID,session_id:str,payload:ClientStart,
                       authorization:str|None=Header(default=None),db=Depends(db_session)):
    session=await authorized_agent(db,project_id=project_id,session_id=session_id,authorization=authorization,
                                   capability='task.runtime',permission='agent.task.claim')
    attempt=await db.scalar(select(AgentTaskRun).join(AgentTaskClaim).where(AgentTaskRun.task_id==str(task_id),
        AgentTaskRun.claim_id==str(payload.claim_id),AgentTaskClaim.session_id==session.id))
    if not attempt:
        raise HTTPException(status_code=404)
    from app.agent_llm_service import execution_scope
    technical_session_id=session.id
    try:
        _,task,_=await execution_scope(db,attempt)
        selection,row=await service.begin_client(db,attempt,session,task)
    except ValueError as exc:
        await db.rollback()
        audit(db,'runtime.client.start','denied',project_id=project_id,target_type='agent_session',target_id=technical_session_id,
              details={'error_code':exc.code if isinstance(exc,service.RuntimeDenied) else 'execution_unavailable'})
        await db.commit()
        raise HTTPException(status_code=409,detail='Admission du runtime refusée.') from None
    await db.commit()
    return {'selection_id':selection.id,'runtime_version_id':selection.runtime_version_id,'usage_id':row.id,
            'deadline':selection.deadline,'tokens':None,'cost':None,'metrics_label':'Non disponible'}
