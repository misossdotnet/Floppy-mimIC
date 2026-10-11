"""Consultation et formulaires serveur du lot Git ; mêmes contrôles que l'API."""
from uuid import UUID,uuid4

from fastapi import APIRouter,Depends,HTTPException,Request,Query
from fastapi.responses import RedirectResponse
from pydantic import ValidationError
from sqlalchemy import select

from app import git_api as api
from app.agent_api import db_session
from app.agent_sprint_web import web_actor,display_date
from app.agent_space_web import projects_for
from app.git_adapter import catalog,minimize,GitError
from app.git_models import GitWorktree,Repository
from app.git_service import tree_detail,view,WORKFLOW_TRANSITIONS
from app.identity_models import User,Project
from app.identity_service import project_permission,csrf_for

router=APIRouter(prefix='/projects/{project_id}/agents/code',tags=['git-pages'])
ERROR_MESSAGES={
    'review_anchor_invalid':'Le fichier ou le hunk ne figure pas dans cette version du diff.',
    'review_revision_stale':'La revue a changé. Vérifiez son historique avant de réessayer.',
    'workflow_revision_stale':'Le workflow a changé. Vérifiez l’étape et les preuves actuelles.',
    'workflow_transition_invalid':'Cette étape ne suit pas l’étape actuelle du workflow.',
    'test_evidence_required':'Une preuve de test rouge ou verte de cette collecte est requise.',
    'changed_tests_required':'La collecte doit contenir les tests écrits pour cette correction.',
    'human_review_required':'Une revue humaine terminée est requise pour cette version.',
    'corrections_open':'Résolvez les corrections demandées avant de terminer la revue.',
    'approval_unavailable':'Cet accord est expiré ou a déjà fait l’objet d’une décision.',
    'approval_stale':'Le code ou la configuration a changé. Demandez un nouvel accord.',
    'operation_stale':'La collecte a changé. Préparez une nouvelle opération.',
    'dirty_worktree':'Commitez les modifications avant cette action.',
    'unsafe_code':'Un contenu sensible ou binaire bloque cette action.',
    'secret_or_control_character':'Retirez les secrets ou caractères de contrôle de la saisie.',
    'repository_policy_changed':'L’architecte doit réautoriser la nouvelle configuration du dépôt.',
    'repository_revoked':'Le rattachement du dépôt a été révoqué.',
}


async def render(request,db,actor,project,selected=None,offset=0,error=None,status_code=200,submitted=None):
    from app.main import page
    rows=(await db.scalars(select(GitWorktree).where(GitWorktree.project_id==project.id)
        .order_by(GitWorktree.created_at.desc(),GitWorktree.id).offset(offset).limit(21))).all()
    repos=(await db.scalars(select(Repository).where(Repository.project_id==project.id))).all()
    permissions={code:project.status!='archived' and await project_permission(db,actor[0],project.id,code) for code in
        ('repository.manage','worktree.create','code.collect','code.test','code.review','code.approve','git.merge','git.push','git.deploy','worktree.delete')}
    detail=await tree_detail(db,project.id,selected) if selected else None
    commands=[];deployments=[]
    if detail:
        repo=next(r for r in repos if r.id==detail['worktree']['repository_id'])
        try:
            entry=catalog(repo.catalog_key);commands=sorted(entry.get('tests',{}));deployments=sorted(entry.get('deployments',{}))
        except GitError: pass
    submitted=submitted or {}
    def field(name,action,identity=None,default=''):
        if submitted.get('action')!=action: return default
        if identity and identity not in (submitted.get('review_id'),submitted.get('operation_id')): return default
        return minimize(str(submitted.get(name,default)))[:2000]
    response=page(request,'agent_code',actor[0],actor[2],project=project,view='code',projects=await projects_for(db,actor[0]),
        trees=[view(r) for r in rows[:20]],repos=[view(r) for r in repos],detail=detail,permissions=permissions,
        catalog_keys=catalog() if permissions['repository.manage'] else [],commands=commands,deployments=deployments,
        next_offset=offset+20 if len(rows)>20 else None,offset=offset,request_key=str(uuid4()),error_message=error,display_date=display_date,
        field=field,next_phases=WORKFLOW_TRANSITIONS[detail['workflow']['phase']] if detail else ())
    response.status_code=status_code
    return response


@router.get('')
async def code_page(request:Request,project_id:str,selected:UUID|None=None,project:UUID|None=None,
    offset:int=Query(0,ge=0,le=10000),db=Depends(db_session),actor=Depends(web_actor)):
    current=await api.authorize(db,actor,project_id)
    if project and str(project)!=project_id:
        await api.authorize(db,actor,str(project))
        return RedirectResponse(f'/projects/{project}/agents/code',303)
    try: return await render(request,db,actor,current,str(selected) if selected else None,offset)
    except GitError as exc: raise HTTPException(status_code=404,detail=exc.code) from None


@router.post('/actions')
async def code_action(request:Request,project_id:str,db=Depends(db_session),actor=Depends(web_actor)):
    raw=await request.form();csrf=raw.get('csrf_token');kind=raw.get('action');selected=raw.get('worktree_id')
    project=await api.authorize(db,actor,project_id,csrf=csrf,write=True)
    actor_id=actor[0].id
    try:
        shared={'db':db,'actor':actor,'x_csrf_token':csrf}
        if kind=='attach': await api.register(project_id,api.RepositoryInput(catalog_key=raw.get('catalog_key')),**shared)
        elif kind=='revoke': await api.revoke(project_id,UUID(raw.get('repository_id','')),**shared)
        elif kind=='create':
            row=await api.create(project_id,api.TreeInput(request_key=raw.get('request_key'),repository_id=raw.get('repository_id'),
                task_id=raw.get('task_id'),base_commit=raw.get('base_commit'),branch=raw.get('branch') or None),**shared)
            selected=row['worktree_id']
        elif kind in api.PERMISSIONS and kind!='create':
            data={'request_key':raw.get('request_key')}
            for key in ('change_id','command_key','target'):
                if raw.get(key): data[key]=raw[key]
            await api.request(project_id,UUID(selected),kind,api.OperationInput(**data),**shared)
        elif kind=='decision':
            await api.decide(project_id,UUID(raw.get('operation_id','')),api.Decision(decision=raw.get('decision'),reason=raw.get('reason')),**shared)
        elif kind in ('dispatch','reconcile'):
            await (api.enqueue if kind=='dispatch' else api.reconcile_operation)(project_id,UUID(raw.get('operation_id','')),**shared)
        elif kind=='workflow':
            await api.workflow(project_id,UUID(selected),api.WorkflowInput(expected_revision=int(raw.get('expected_revision','0')),
                phase=raw.get('phase'),reason=raw.get('reason'),change_id=raw.get('change_id') or None,test_id=raw.get('test_id') or None),**shared)
        elif kind=='review_start': await api.start_review(project_id,api.ReviewNew(change_id=raw.get('change_id')),**shared)
        elif kind=='review':
            await api.review(project_id,UUID(raw.get('review_id','')),api.ReviewInput(expected_revision=int(raw.get('expected_revision','0')),
                action=raw.get('review_action'),file_path=raw.get('file_path') or None,hunk_id=raw.get('hunk_id') or None,
                text=raw.get('text',''),correction=raw.get('correction')=='true',resolves_id=raw.get('resolves_id') or None),**shared)
        else: raise GitError('action_invalid')
    except (ValueError,ValidationError,HTTPException) as exc:
        # API déjà auditée ; recharger les objets expirés après un rollback.
        await db.rollback();actor=(await db.get(User,actor_id),actor[1],actor[2]);project=await db.get(Project,project_id)
        message=exc.detail if isinstance(exc,HTTPException) else 'Vérifiez les champs et les preuves requises.'
        message=ERROR_MESSAGES.get(message,message)
        status=exc.status_code if isinstance(exc,HTTPException) else 409
        if status in (401,403,404): raise HTTPException(status_code=status,detail=message) from None
        try: selected=str(UUID(selected)) if selected else None
        except ValueError: selected=None
        return await render(request,db,actor,project,selected,error=minimize(str(message)),status_code=status,
            submitted={key:raw.get(key,'') for key in ('action','review_id','operation_id','task_id','base_commit','branch','phase','reason','test_id','text','file_path','hunk_id','resolves_id')})
    return RedirectResponse(f'/projects/{project_id}/agents/code'+(f'?selected={selected}' if selected else ''),303)
