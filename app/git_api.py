"""API Git humaine et technique ; décisions sensibles réservées aux humains."""
from typing import Literal
from uuid import UUID

from fastapi import APIRouter,Depends,Header,HTTPException,Query
from pydantic import BaseModel,ConfigDict,Field,model_validator
from sqlalchemy import select

from app.agent_api import authorized_agent,check_csrf,db_session,human,human_project
from app.git_adapter import GitError,catalog
from app.git_models import Repository,GitWorktree,GitOperation,CodeChange,CodeReview
from app.git_service import (PERMISSIONS,SENSITIVE,attach,prepare_tree,request_operation,decide_operation,
    advance_workflow,review_create,review_action,tree_detail,view,event,GitMissing)
from app.identity_models import Project
from app.identity_service import audit
from app.memory_api import MemoryRoute,private_response

Kind=Literal['collect','test','merge','push','deploy','delete']
Phase=Literal['tests','implementation','execution','review','approved']
router=APIRouter(prefix='/api/projects/{project_id}/git',tags=['git-development'],route_class=MemoryRoute,dependencies=[Depends(private_response)])
agent_router=APIRouter(prefix='/api/projects/{project_id}/agent-git',tags=['agent-git'],route_class=MemoryRoute,dependencies=[Depends(private_response)])


class Input(BaseModel):
    model_config=ConfigDict(extra='forbid',str_strip_whitespace=True)


class RepositoryInput(Input):
    catalog_key:str=Field(pattern=r'^[a-z0-9][a-z0-9_-]{0,79}$')


class TreeInput(Input):
    request_key:UUID
    repository_id:UUID
    task_id:UUID
    base_commit:str=Field(pattern=r'^(?:[0-9a-f]{40}|[0-9a-f]{64})$')
    branch:str|None=Field(default=None,max_length=180)


class AgentTreeInput(TreeInput):
    session_id:UUID


class OperationInput(Input):
    request_key:UUID
    change_id:UUID|None=None
    command_key:str|None=Field(default=None,pattern=r'^[a-z0-9][a-z0-9_-]{0,79}$')
    target:str|None=Field(default=None,max_length=180)

    def for_kind(self,kind):
        required={'collect':set(),'test':{'change_id','command_key'},'merge':{'change_id','target'},
            'push':{'change_id','target'},'deploy':{'change_id','target'},'delete':{'change_id'}}[kind]
        if self.model_fields_set-required-{'request_key'} or any(getattr(self,k) is None for k in required):
            raise GitError('operation_fields_invalid')
        return {k:v for k,v in self.model_dump(mode='json').items() if k in required|{'request_key'}}


class AgentOperationInput(OperationInput):
    session_id:UUID
    def for_kind(self,kind):
        data=self.model_dump(mode='json',exclude={'session_id'})
        return OperationInput(**{k:v for k,v in data.items() if k in self.model_fields_set}).for_kind(kind)


class Decision(Input):
    decision:Literal['approve','reject']
    reason:str=Field(min_length=1,max_length=1000)


class WorkflowInput(Input):
    expected_revision:int=Field(ge=1,strict=True)
    phase:Phase
    reason:str=Field(min_length=1,max_length=1000)
    change_id:UUID|None=None
    test_id:UUID|None=None


class AgentWorkflowInput(WorkflowInput):
    session_id:UUID


class ReviewNew(Input):
    change_id:UUID


class AgentReviewNew(ReviewNew):
    session_id:UUID


class ReviewInput(Input):
    expected_revision:int=Field(ge=1,strict=True)
    action:Literal['comment','resolve','changes_requested','reviewed']
    file_path:str|None=Field(default=None,max_length=500)
    hunk_id:str|None=Field(default=None,pattern=r'^[0-9a-f]{64}$')
    text:str=Field(default='',max_length=2000)
    correction:bool=False
    resolves_id:UUID|None=None

    @model_validator(mode='after')
    def shape(self):
        if self.action!='comment' and (self.file_path or self.hunk_id or self.correction): raise ValueError()
        if self.action!='resolve' and self.resolves_id: raise ValueError()
        return self


class AgentReviewInput(ReviewInput):
    session_id:UUID


async def authorize(db,actor,project_id,permission='agent.read',csrf=None,write=False):
    if write: check_csrf(actor[2],csrf)
    project=await human_project(db,actor[0],project_id,permission)
    if write:
        project=await db.scalar(select(Project).where(Project.id==project_id).with_for_update().execution_options(populate_existing=True))
        if project.status=='archived': raise HTTPException(status_code=409,detail='Projet archivé.')
    return project


async def mutation(db,project_id,actor_id,operation):
    try: result=await operation
    except GitError as exc:
        await db.rollback();audit(db,'git.denied','denied',project_id=project_id,actor_id=actor_id,details={'error':exc.code});await db.commit()
        raise HTTPException(status_code=404 if isinstance(exc,GitMissing) else 409,detail=exc.code) from None
    except ValueError:
        await db.rollback();audit(db,'git.denied','denied',project_id=project_id,actor_id=actor_id);await db.commit()
        raise HTTPException(status_code=409,detail='execution_unavailable') from None
    await db.commit();return result


async def dispatch(db,row,*,force=False):
    if row.status=='queued' and (row.kind not in SENSITIVE or force):
        try:
            from app.worker import git_execute
            git_execute.delay(row.id)
        except Exception:
            row.result={'error':'broker_unavailable'}
            audit(db,'git.dispatch','failed',project_id=row.project_id,target_type='git_operation',target_id=row.id,details={'error':'broker_unavailable'})
            await db.commit()
    return view(row)


@router.get('/catalog')
async def allowed(project_id:str,db=Depends(db_session),actor=Depends(human)):
    await authorize(db,actor,project_id,'repository.manage')
    return {'repositories':catalog()}


@router.post('/repositories',status_code=201)
async def register(project_id:str,payload:RepositoryInput,x_csrf_token:str|None=Header(default=None),db=Depends(db_session),actor=Depends(human)):
    await authorize(db,actor,project_id,'repository.manage',x_csrf_token,True)
    return view(await mutation(db,project_id,actor[0].id,attach(db,project_id,payload.catalog_key,actor[0].id)))


@router.get('/repositories')
async def repositories(project_id:str,db=Depends(db_session),actor=Depends(human)):
    await authorize(db,actor,project_id)
    rows=(await db.scalars(select(Repository).where(Repository.project_id==project_id).order_by(Repository.catalog_key))).all()
    return {'repositories':[view(r) for r in rows]}


@router.post('/repositories/{repository_id}/revoke')
async def revoke(project_id:str,repository_id:UUID,x_csrf_token:str|None=Header(default=None),db=Depends(db_session),actor=Depends(human)):
    await authorize(db,actor,project_id,'repository.manage',x_csrf_token,True)
    row=await db.scalar(select(Repository).where(Repository.id==str(repository_id),Repository.project_id==project_id).with_for_update())
    if not row: raise HTTPException(status_code=404)
    if row.is_active: row.is_active=False;event(db,project_id,actor[0].id,'repository.revoke',row)
    await db.commit();return view(row)


@router.post('/worktrees',status_code=202)
async def create(project_id:str,payload:TreeInput,x_csrf_token:str|None=Header(default=None),db=Depends(db_session),actor=Depends(human)):
    await authorize(db,actor,project_id,'worktree.create',x_csrf_token,True)
    row=await mutation(db,project_id,actor[0].id,prepare_tree(db,project_id,payload.model_dump(mode='json'),actor[0].id))
    return await dispatch(db,row)


@router.get('/worktrees')
async def trees(project_id:str,repository_id:UUID|None=None,task_id:UUID|None=None,limit:int=Query(20,ge=1,le=100),offset:int=Query(0,ge=0,le=10000),db=Depends(db_session),actor=Depends(human)):
    await authorize(db,actor,project_id)
    query=select(GitWorktree).where(GitWorktree.project_id==project_id)
    if repository_id: query=query.where(GitWorktree.repository_id==str(repository_id))
    if task_id: query=query.where(GitWorktree.task_id==str(task_id))
    rows=(await db.scalars(query.order_by(GitWorktree.created_at.desc(),GitWorktree.id).offset(offset).limit(limit+1))).all()
    return {'worktrees':[view(r) for r in rows[:limit]],'next_offset':offset+limit if len(rows)>limit else None}


@router.get('/worktrees/{worktree_id}')
async def detail(project_id:str,worktree_id:UUID,db=Depends(db_session),actor=Depends(human)):
    await authorize(db,actor,project_id)
    try: return await tree_detail(db,project_id,str(worktree_id))
    except GitMissing as exc: raise HTTPException(status_code=404,detail=exc.code) from None


@router.post('/worktrees/{worktree_id}/operations/{kind}',status_code=202)
async def request(project_id:str,worktree_id:UUID,kind:Kind,payload:OperationInput,x_csrf_token:str|None=Header(default=None),db=Depends(db_session),actor=Depends(human)):
    await authorize(db,actor,project_id,PERMISSIONS[kind],x_csrf_token,True)
    async def make(): return await request_operation(db,project_id,str(worktree_id),kind,payload.for_kind(kind),actor[0].id)
    row=await mutation(db,project_id,actor[0].id,make())
    return await dispatch(db,row)


@router.post('/operations/{operation_id}/decision')
async def decide(project_id:str,operation_id:UUID,payload:Decision,x_csrf_token:str|None=Header(default=None),db=Depends(db_session),actor=Depends(human)):
    await authorize(db,actor,project_id,'agent.read',x_csrf_token,True)
    row=await db.scalar(select(GitOperation).where(GitOperation.id==str(operation_id),GitOperation.project_id==project_id))
    if not row: raise HTTPException(status_code=404)
    await human_project(db,actor[0],project_id,PERMISSIONS[row.kind])
    # Une décision approuve ; l'exécution doit encore être demandée explicitement.
    return view(await mutation(db,project_id,actor[0].id,decide_operation(db,project_id,str(operation_id),payload.decision,payload.reason,actor[0].id)))


@router.post('/operations/{operation_id}/dispatch')
async def enqueue(project_id:str,operation_id:UUID,x_csrf_token:str|None=Header(default=None),db=Depends(db_session),actor=Depends(human)):
    await authorize(db,actor,project_id,'agent.read',x_csrf_token,True)
    row=await db.scalar(select(GitOperation).where(GitOperation.id==str(operation_id),GitOperation.project_id==project_id))
    if not row: raise HTTPException(status_code=404)
    await human_project(db,actor[0],project_id,PERMISSIONS[row.kind])
    if row.status!='queued': raise HTTPException(status_code=409,detail='operation_not_queued')
    return await dispatch(db,row,force=True)


@router.post('/operations/{operation_id}/reconcile')
async def reconcile_operation(project_id:str,operation_id:UUID,x_csrf_token:str|None=Header(default=None),db=Depends(db_session),actor=Depends(human)):
    from app.git_worker import reconcile
    await authorize(db,actor,project_id,'agent.read',x_csrf_token,True)
    row=await db.scalar(select(GitOperation).where(GitOperation.id==str(operation_id),GitOperation.project_id==project_id))
    if not row: raise HTTPException(status_code=404)
    await human_project(db,actor[0],project_id,PERMISSIONS[row.kind])
    return view(await mutation(db,project_id,actor[0].id,reconcile(db,project_id,str(operation_id),actor[0].id)))


@router.post('/worktrees/{worktree_id}/workflow')
async def workflow(project_id:str,worktree_id:UUID,payload:WorkflowInput,x_csrf_token:str|None=Header(default=None),db=Depends(db_session),actor=Depends(human)):
    await authorize(db,actor,project_id,'code.approve' if payload.phase=='approved' else 'code.collect',x_csrf_token,True)
    return view(await mutation(db,project_id,actor[0].id,advance_workflow(db,project_id,str(worktree_id),payload.model_dump(mode='json'),actor[0].id)))


@router.post('/reviews',status_code=201)
async def start_review(project_id:str,payload:ReviewNew,x_csrf_token:str|None=Header(default=None),db=Depends(db_session),actor=Depends(human)):
    await authorize(db,actor,project_id,'code.review',x_csrf_token,True)
    return view(await mutation(db,project_id,actor[0].id,review_create(db,project_id,str(payload.change_id),actor[0].id)))


@router.post('/reviews/{review_id}')
async def review(project_id:str,review_id:UUID,payload:ReviewInput,x_csrf_token:str|None=Header(default=None),db=Depends(db_session),actor=Depends(human)):
    await authorize(db,actor,project_id,'code.review',x_csrf_token,True)
    return view(await mutation(db,project_id,actor[0].id,review_action(db,project_id,str(review_id),payload.model_dump(mode='json'),actor[0].id)))


async def technical(db,project_id,session_id,authorization,capability,permission):
    return await authorized_agent(db,project_id=project_id,session_id=str(session_id),authorization=authorization,capability=capability,permission=permission)


@agent_router.post('/worktrees',status_code=202)
async def agent_create(project_id:str,payload:AgentTreeInput,authorization:str|None=Header(default=None),db=Depends(db_session)):
    session=await technical(db,project_id,payload.session_id,authorization,'git.worktree','worktree.create')
    row=await mutation(db,project_id,session.created_by,prepare_tree(db,project_id,payload.model_dump(mode='json',exclude={'session_id'}),session.created_by,session))
    return await dispatch(db,row)


@agent_router.get('/worktrees/{worktree_id}')
async def agent_detail(project_id:str,worktree_id:UUID,session_id:UUID,authorization:str|None=Header(default=None),db=Depends(db_session)):
    session=await technical(db,project_id,session_id,authorization,'git.read','agent.read')
    try: return await tree_detail(db,project_id,str(worktree_id),session)
    except GitMissing as exc: raise HTTPException(status_code=404,detail=exc.code) from None


@agent_router.post('/worktrees/{worktree_id}/operations/{kind}',status_code=202)
async def agent_request(project_id:str,worktree_id:UUID,kind:Literal['collect','test'],payload:AgentOperationInput,authorization:str|None=Header(default=None),db=Depends(db_session)):
    session=await technical(db,project_id,payload.session_id,authorization,'git.'+kind,PERMISSIONS[kind])
    async def make(): return await request_operation(db,project_id,str(worktree_id),kind,payload.for_kind(kind),session.created_by,session)
    row=await mutation(db,project_id,session.created_by,make());return await dispatch(db,row)


@agent_router.post('/worktrees/{worktree_id}/workflow')
async def agent_workflow(project_id:str,worktree_id:UUID,payload:AgentWorkflowInput,authorization:str|None=Header(default=None),db=Depends(db_session)):
    session=await technical(db,project_id,payload.session_id,authorization,'git.collect','code.collect')
    if payload.phase=='approved': raise HTTPException(status_code=403)
    return view(await mutation(db,project_id,session.created_by,advance_workflow(db,project_id,str(worktree_id),payload.model_dump(mode='json',exclude={'session_id'}),session.created_by,session)))


@agent_router.post('/reviews',status_code=201)
async def agent_start_review(project_id:str,payload:AgentReviewNew,authorization:str|None=Header(default=None),db=Depends(db_session)):
    session=await technical(db,project_id,payload.session_id,authorization,'git.review','code.review')
    return view(await mutation(db,project_id,session.created_by,review_create(db,project_id,str(payload.change_id),session.created_by,session)))


@agent_router.post('/reviews/{review_id}')
async def agent_review(project_id:str,review_id:UUID,payload:AgentReviewInput,authorization:str|None=Header(default=None),db=Depends(db_session)):
    session=await technical(db,project_id,payload.session_id,authorization,'git.review','code.review')
    return view(await mutation(db,project_id,session.created_by,review_action(db,project_id,str(review_id),payload.model_dump(mode='json',exclude={'session_id'}),session.created_by,session)))
