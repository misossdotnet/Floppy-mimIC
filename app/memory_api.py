"""API mémoire humaine/technique : aucune décision confiée au Bearer."""
from typing import Literal
from uuid import UUID

from fastapi import APIRouter, Depends, Header, HTTPException, Query, Response
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from fastapi.routing import APIRoute
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import select

from app.agent_api import authorized_agent, check_csrf, db_session, human, human_project
from app.memory_models import ProjectMemory, ProjectMemoryGrant, ProjectMemoryVersion
from app.memory_service import (VALIDATION_PERMISSIONS, create_memory, decide_memory, grant_memory,
    grant_view, memory_context, memory_view, project_grants, revise_memory, revoke_grant, version_view)
from app.identity_service import audit

Kind=Literal['convention','architecture_decision','business_constraint','result']
Action=Literal['submit','approve','reject','retire']


class MemoryRoute(APIRoute):
    def get_route_handler(self):
        handler=super().get_route_handler()
        async def bounded_errors(request):
            try: return await handler(request)
            except RequestValidationError as exc:
                # Ne pas réémettre le contenu source dans les erreurs de schéma.
                return JSONResponse({'detail':[{'loc':e['loc'],'type':e['type'],'msg':'Champ invalide'} for e in exc.errors()]},
                                    status_code=422,headers={'Cache-Control':'no-store'})
        return bounded_errors


def private_response(response:Response):
    response.headers['Cache-Control']='no-store'
    response.headers['Referrer-Policy']='no-referrer'


router=APIRouter(prefix='/api/projects/{project_id}/memories',tags=['memory'],route_class=MemoryRoute,dependencies=[Depends(private_response)])
agent_router=APIRouter(prefix='/api/projects/{project_id}/agent-memory',tags=['agent-memory'],route_class=MemoryRoute,dependencies=[Depends(private_response)])
global_router=APIRouter(prefix='/api/global-memories',tags=['global-memory'],route_class=MemoryRoute,dependencies=[Depends(private_response)])


class SourceRef(BaseModel):
    model_config=ConfigDict(extra='forbid')
    type: Literal['source_document','context_item_version','deliverable_version','agent_task_run']
    id: UUID


class MemoryContent(BaseModel):
    model_config=ConfigDict(extra='forbid',str_strip_whitespace=True)
    title: str=Field(min_length=1,max_length=180)
    content: str=Field(min_length=1,max_length=4000)
    source_note: str=Field(min_length=1,max_length=500)
    source_refs: list[SourceRef]=Field(default_factory=list,max_length=10)
    confidence: float | None=Field(default=None,ge=0,le=1,strict=True)
    change_note: str=Field(default='',max_length=1000)


class MemoryNew(MemoryContent):
    kind: Kind


class MemoryEdit(MemoryContent):
    expected_version: int=Field(ge=1,strict=True)


class MemoryDecision(BaseModel):
    model_config=ConfigDict(extra='forbid',str_strip_whitespace=True)
    expected_version: int=Field(ge=1,strict=True)
    reason: str=Field(default='',max_length=1000)


class MemoryShare(BaseModel):
    model_config=ConfigDict(extra='forbid')
    project_id: UUID
    expected_version: int=Field(ge=1,strict=True)


async def authorize_memory(db,actor,project_id,*,memory_id=None,write=False,csrf=None,permission=None):
    if write: check_csrf(actor[2],csrf)
    if project_id is None:
        if not actor[0].is_platform_admin:
            audit(db,'memory.global.manage','denied',actor_id=actor[0].id)
            await db.commit(); raise HTTPException(status_code=403)
    else:
        project=await human_project(db,actor[0],project_id,permission or ('memory.propose' if write else 'memory.read'))
        if write and project.status=='archived': raise HTTPException(status_code=409,detail='Projet archivé.')
    memory=None
    if memory_id:
        memory=await db.scalar(select(ProjectMemory).where(ProjectMemory.id==memory_id,
            ProjectMemory.project_id==project_id,ProjectMemory.scope=='project' if project_id else ProjectMemory.scope=='global'))
        if memory is None: raise HTTPException(status_code=404)
    return memory


async def mutation(db,operation):
    try: result=await operation
    except ValueError as exc: raise HTTPException(status_code=409,detail=str(exc)) from exc
    await db.commit()
    return result


async def list_memories(db,project_id,kind,limit,offset):
    query=select(ProjectMemory).where(ProjectMemory.project_id==project_id,ProjectMemory.scope=='project' if project_id else ProjectMemory.scope=='global')
    if kind: query=query.where(ProjectMemory.kind==kind)
    rows=(await db.scalars(query.order_by(ProjectMemory.created_at.desc(),ProjectMemory.id).limit(limit).offset(offset))).all()
    return {'memories':[await memory_view(db,m) for m in rows],'limit':limit,'offset':offset}


async def history(db,memory_id):
    versions=(await db.scalars(select(ProjectMemoryVersion).where(ProjectMemoryVersion.memory_id==memory_id)
        .order_by(ProjectMemoryVersion.version_number))).all()
    return {'versions':[version_view(v) for v in versions]}


async def decision(db,actor,project_id,memory_id,action,payload,csrf):
    memory=await authorize_memory(db,actor,project_id,memory_id=memory_id)
    permission='memory.propose' if action=='submit' else VALIDATION_PERMISSIONS[memory.kind]
    await authorize_memory(db,actor,project_id,write=True,csrf=csrf,permission=permission)
    memory=await mutation(db,decide_memory(db,memory_id,project_id,payload.expected_version,action,actor[0].id,
                                         payload.reason,scope='project' if project_id else 'global'))
    return await memory_view(db,memory)


@router.get('')
async def project_memory_list(project_id:str,kind:Kind|None=None,limit:int=Query(50,ge=1,le=100),offset:int=Query(0,ge=0),db=Depends(db_session),actor=Depends(human)):
    await authorize_memory(db,actor,project_id)
    return await list_memories(db,project_id,kind,limit,offset)


@router.post('',status_code=201)
async def new_project_memory(project_id:str,payload:MemoryNew,x_csrf_token:str|None=Header(None),db=Depends(db_session),actor=Depends(human)):
    await authorize_memory(db,actor,project_id,write=True,csrf=x_csrf_token)
    memory=await mutation(db,create_memory(db,project_id,payload.kind,payload.model_dump(),actor[0].id))
    return await memory_view(db,memory)


@router.get('/context')
async def project_memory_context(project_id:str,q:str|None=Query(None,max_length=200),kind:Kind|None=None,
        limit:int=Query(20,ge=1,le=20),max_chars:int=Query(12000,ge=500,le=12000),db=Depends(db_session),actor=Depends(human)):
    await authorize_memory(db,actor,project_id)
    return await memory_context(db,project_id,q=q,kind=kind,limit=limit,max_chars=max_chars)


@router.get('/global-references')
async def project_global_references(project_id:str,db=Depends(db_session),actor=Depends(human)):
    await authorize_memory(db,actor,project_id)
    return {'grants':[grant_view(*row) for row in await project_grants(db,project_id)]}


@router.post('/global-references/{grant_id}/revoke')
async def unshare_global_memory(project_id:str,grant_id:str,x_csrf_token:str|None=Header(None),db=Depends(db_session),actor=Depends(human)):
    await authorize_memory(db,actor,project_id,write=True,csrf=x_csrf_token,permission='agent.run.manage')
    if not await db.scalar(select(ProjectMemoryGrant.id).where(ProjectMemoryGrant.id==grant_id,ProjectMemoryGrant.project_id==project_id)):
        raise HTTPException(status_code=404)
    row=await mutation(db,revoke_grant(db,project_id,grant_id,actor[0].id))
    return grant_view(*row)


@router.get('/{memory_id}')
async def get_project_memory(project_id:str,memory_id:str,db=Depends(db_session),actor=Depends(human)):
    memory=await authorize_memory(db,actor,project_id,memory_id=memory_id)
    return await memory_view(db,memory)


@router.get('/{memory_id}/versions')
async def project_memory_versions(project_id:str,memory_id:str,db=Depends(db_session),actor=Depends(human)):
    await authorize_memory(db,actor,project_id,memory_id=memory_id)
    return await history(db,memory_id)


@router.post('/{memory_id}/versions')
async def edit_project_memory(project_id:str,memory_id:str,payload:MemoryEdit,x_csrf_token:str|None=Header(None),db=Depends(db_session),actor=Depends(human)):
    await authorize_memory(db,actor,project_id,memory_id=memory_id,write=True,csrf=x_csrf_token)
    memory=await mutation(db,revise_memory(db,memory_id,project_id,payload.expected_version,payload.model_dump(),actor_id=actor[0].id))
    return await memory_view(db,memory)


@router.post('/{memory_id}/{action}')
async def decide_project_memory(project_id:str,memory_id:str,action:Action,payload:MemoryDecision,x_csrf_token:str|None=Header(None),db=Depends(db_session),actor=Depends(human)):
    return await decision(db,actor,project_id,memory_id,action,payload,x_csrf_token)


@global_router.get('')
async def global_memory_list(kind:Kind|None=None,limit:int=Query(50,ge=1,le=100),offset:int=Query(0,ge=0),db=Depends(db_session),actor=Depends(human)):
    await authorize_memory(db,actor,None)
    return await list_memories(db,None,kind,limit,offset)


@global_router.post('',status_code=201)
async def new_global_memory(payload:MemoryNew,x_csrf_token:str|None=Header(None),db=Depends(db_session),actor=Depends(human)):
    await authorize_memory(db,actor,None,write=True,csrf=x_csrf_token)
    memory=await mutation(db,create_memory(db,None,payload.kind,payload.model_dump(),actor[0].id))
    return await memory_view(db,memory)


@global_router.get('/{memory_id}')
async def get_global_memory(memory_id:str,db=Depends(db_session),actor=Depends(human)):
    memory=await authorize_memory(db,actor,None,memory_id=memory_id)
    return await memory_view(db,memory)


@global_router.get('/{memory_id}/versions')
async def global_memory_versions(memory_id:str,db=Depends(db_session),actor=Depends(human)):
    await authorize_memory(db,actor,None,memory_id=memory_id)
    return await history(db,memory_id)


@global_router.post('/{memory_id}/versions')
async def edit_global_memory(memory_id:str,payload:MemoryEdit,x_csrf_token:str|None=Header(None),db=Depends(db_session),actor=Depends(human)):
    await authorize_memory(db,actor,None,memory_id=memory_id,write=True,csrf=x_csrf_token)
    memory=await mutation(db,revise_memory(db,memory_id,None,payload.expected_version,payload.model_dump(),actor_id=actor[0].id,scope='global'))
    return await memory_view(db,memory)


@global_router.post('/{memory_id}/grants')
async def share_global_memory(memory_id:str,payload:MemoryShare,x_csrf_token:str|None=Header(None),db=Depends(db_session),actor=Depends(human)):
    await authorize_memory(db,actor,None,memory_id=memory_id,write=True,csrf=x_csrf_token)
    project=await human_project(db,actor[0],str(payload.project_id),'project.manage')
    if project.status=='archived': raise HTTPException(status_code=409,detail='Projet archivé.')
    row=await mutation(db,grant_memory(db,memory_id,project.id,payload.expected_version,actor[0].id))
    return grant_view(*row)


@global_router.post('/{memory_id}/{action}')
async def decide_global_memory(memory_id:str,action:Action,payload:MemoryDecision,x_csrf_token:str|None=Header(None),db=Depends(db_session),actor=Depends(human)):
    return await decision(db,actor,None,memory_id,action,payload,x_csrf_token)


async def memory_agent(db,project_id,session_id,authorization,capability):
    return await authorized_agent(db,project_id=project_id,session_id=str(session_id),authorization=authorization,
                                  capability=capability,permission=capability)


@agent_router.get('/context')
async def agent_memory_context(project_id:str,session_id:UUID,q:str|None=Query(None,max_length=200),kind:Kind|None=None,
        limit:int=Query(20,ge=1,le=20),max_chars:int=Query(12000,ge=500,le=12000),authorization:str|None=Header(None),db=Depends(db_session)):
    await memory_agent(db,project_id,session_id,authorization,'memory.read')
    return await memory_context(db,project_id,q=q,kind=kind,limit=limit,max_chars=max_chars)


@agent_router.post('',status_code=201)
async def agent_memory_proposal(project_id:str,session_id:UUID,payload:MemoryNew,authorization:str|None=Header(None),db=Depends(db_session)):
    session=await memory_agent(db,project_id,session_id,authorization,'memory.propose')
    memory=await mutation(db,create_memory(db,project_id,payload.kind,payload.model_dump(),session.created_by,session_id=session.id))
    return await memory_view(db,memory)


@agent_router.post('/{memory_id}/versions')
async def agent_memory_revision(project_id:str,memory_id:str,session_id:UUID,payload:MemoryEdit,authorization:str|None=Header(None),db=Depends(db_session)):
    session=await memory_agent(db,project_id,session_id,authorization,'memory.propose')
    if not await db.scalar(select(ProjectMemory.id).where(ProjectMemory.id==memory_id,ProjectMemory.project_id==project_id,ProjectMemory.scope=='project')):
        raise HTTPException(status_code=404)
    memory=await mutation(db,revise_memory(db,memory_id,project_id,payload.expected_version,payload.model_dump(),session_id=session.id))
    return await memory_view(db,memory)
