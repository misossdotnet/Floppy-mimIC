"""Orchestration Git transactionnelle, preuves exactes et décisions humaines séparées."""
import asyncio
from datetime import timedelta
from uuid import uuid4

from sqlalchemy import func, select

from app.agent_models import AgentPlanVersion, AgentSession, AgentTask, AgentTaskClaim
from app.agent_routing import require_approved_execution
from app.git_adapter import (GitError, catalog, check_repo, digest, inspect_tree, branch, sha, safe_text,
    target_commit, remote_commit)
from app.git_models import (Repository, GitWorktree, GitOperation, CodeChange, CodeReview, CodeReviewEvent,
    CodeTestRun, CodeWorkflow, CodeWorkflowEvent)
from app.identity_models import Project, User, utcnow
from app.identity_service import audit, project_permission

PERMISSIONS={'create':'worktree.create','collect':'code.collect','test':'code.test',
    'merge':'git.merge','push':'git.push','deploy':'git.deploy','delete':'worktree.delete'}
CAPABILITIES={'create':'git.worktree','collect':'git.collect','test':'git.test'}
SENSITIVE={'merge','push','deploy','delete'}
WORKFLOW_TRANSITIONS={'analysis':('tests',),'tests':('implementation',),'implementation':('execution',),
    'execution':('review','implementation'),'review':('approved','implementation'),'approved':('implementation',)}


class GitMissing(GitError):
    pass


def event(db,project_id,actor_id,action,target,**details):
    if action in ('operation.failed','operation.uncertain'):
        details={**details,'status':target.status}
    outcome='failed' if action in ('operation.failed','operation.uncertain') else 'success'
    audit(db,'git.'+action,outcome,project_id=project_id,actor_id=actor_id,
        target_type=target.__tablename__,target_id=target.id,details=details)


def view(row):
    # Aucun chemin hôte, catalogue brut, jeton ou argv dans les réponses.
    if isinstance(row, Repository): keys=('id','project_id','catalog_key','is_active','created_at')
    elif isinstance(row, GitWorktree): keys=('id','project_id','repository_id','task_id','branch','base_commit','status','session_id','created_at','removed_at')
    elif isinstance(row, GitOperation): keys=('id','worktree_id','kind','status','parameters','result','actor_id','session_id','decided_by','decision_reason','expires_at','created_at','started_at','ended_at')
    elif isinstance(row, CodeChange): keys=('id','worktree_id','revision','head_commit','content_fingerprint','manifest','created_by','session_id','created_at')
    elif isinstance(row, CodeTestRun): keys=('id','change_id','command_key','status','exit_code','duration_ms','log','created_at')
    elif isinstance(row, CodeReview): keys=('id','change_id','reviewer_id','session_id','status','revision','created_at')
    elif isinstance(row, CodeWorkflow): keys=('id','worktree_id','phase','revision','approved_change_id','approved_by','created_at')
    else: keys=('id','revision','action','file_path','hunk_id','text','correction','resolves_id','created_at')
    result={k:getattr(row,k) for k in keys}
    if isinstance(row,CodeReview): result['content_status']='Brouillon IA' if row.session_id else 'À revoir'
    return result


async def lock_repository(db,project_id,repository_id,*,active=True):
    row=await db.scalar(select(Repository).where(Repository.id==repository_id,Repository.project_id==project_id)
        .with_for_update().execution_options(populate_existing=True))
    if not row: raise GitMissing('repository_missing')
    if active and not row.is_active: raise GitError('repository_revoked')
    entry=catalog(row.catalog_key)
    if entry['_fingerprint']!=row.policy_fingerprint: raise GitError('repository_policy_changed')
    return row,entry


async def project_mutation(db,project_id):
    row=await db.scalar(select(Project).where(Project.id==project_id).with_for_update().execution_options(populate_existing=True))
    if not row or row.status=='archived': raise GitError('project_archived')


async def attach(db,project_id,key,actor_id):
    await project_mutation(db,project_id)
    # Verrou global sur clé physique ; pas de partage interprojet implicite.
    await db.execute(select(func.pg_advisory_xact_lock(int(digest({'repo':key})[:15],16))))
    prior=await db.scalar(select(Repository).where(Repository.catalog_key==key).with_for_update())
    entry=catalog(key)
    await db.execute(select(func.pg_advisory_xact_lock(int(digest({'physical':str(entry['_path'])})[:15],16))))
    await asyncio.to_thread(check_repo,entry)
    if prior:
        if prior.project_id!=project_id: raise GitError('repository_already_attached')
        if await db.scalar(select(Repository.id).where(Repository.physical_identity==digest(str(entry['_path'])),Repository.id!=prior.id)):
            raise GitError('repository_already_attached')
        if not prior.is_active or prior.policy_fingerprint!=entry['_fingerprint']:
            prior.is_active=True;prior.policy_fingerprint=entry['_fingerprint'];prior.physical_identity=digest(str(entry['_path']))
            event(db,project_id,actor_id,'repository.authorize',prior)
        return prior
    if await db.scalar(select(Repository.id).where(Repository.physical_identity==digest(str(entry['_path'])))):
        raise GitError('repository_already_attached')
    row=Repository(project_id=project_id,catalog_key=key,policy_fingerprint=entry['_fingerprint'],physical_identity=digest(str(entry['_path'])),created_by=actor_id)
    db.add(row);await db.flush();event(db,project_id,actor_id,'repository.attach',row)
    return row


async def task_scope(db,project_id,task_id,session=None,*,execution=True):
    task=await db.scalar(select(AgentTask).where(AgentTask.id==task_id,AgentTask.project_id==project_id))
    if not task or (session and task.team_id!=session.team_id): raise GitMissing('task_missing')
    plan=await db.scalar(select(AgentPlanVersion).where(AgentPlanVersion.id==task.plan_version_id)
        .with_for_update(key_share=True).execution_options(populate_existing=True))
    if execution:
        await require_approved_execution(db,plan,project_id,task.team_id)
        if task.status in ('failed','cancelled'): raise GitError('task_inactive')
        if await db.scalar(select(AgentTask.id).where(AgentTask.parent_task_id==task.id).limit(1)): raise GitError('leaf_task_required')
        if session:
            await db.refresh(task)
            claim=await db.scalar(select(AgentTaskClaim).where(AgentTaskClaim.task_id==task.id,
                AgentTaskClaim.session_id==session.id,AgentTaskClaim.released_at.is_(None)))
            if (task.skill_profile_id!=session.skill_profile_id or task.status!='claimed' or
                task.claimed_session_id!=session.id or not claim or claim.lease_expires_at<=utcnow() or
                not task.lease_expires_at or task.lease_expires_at<=utcnow()): raise GitError('current_claim_required')
    return task


async def worktree_scope(db,project_id,worktree_id,session=None,*,execution=True,active=True):
    tree=await db.scalar(select(GitWorktree).where(GitWorktree.id==worktree_id,GitWorktree.project_id==project_id))
    if not tree: raise GitMissing('worktree_missing')
    task=await task_scope(db,project_id,tree.task_id,session,execution=execution)
    repo,entry=await lock_repository(db,project_id,tree.repository_id,active=active)
    await db.refresh(tree)
    return tree,repo,entry,task


async def previous(db,actor_id,request_key,payload):
    await db.execute(select(func.pg_advisory_xact_lock(int(digest({'actor':actor_id,'key':request_key})[:15],16))))
    prior=await db.scalar(select(GitOperation).where(GitOperation.actor_id==actor_id,GitOperation.request_key==request_key))
    if prior and prior.request_fingerprint!=digest(payload): raise GitError('request_key_conflict')
    return prior


async def prepare_tree(db,project_id,values,actor_id,session=None):
    await project_mutation(db,project_id)
    payload={'project_id':project_id,**values,'session_id':session.id if session else None}
    prior=await previous(db,actor_id,values['request_key'],payload)
    if prior: return prior
    await task_scope(db,project_id,values['task_id'],session)
    repo,entry=await lock_repository(db,project_id,values['repository_id'])
    identity=str(uuid4()); name=values.get('branch') or 'codex/'+values['task_id'][:8]+'/'+identity[:8]
    branch(name,task_branch=True);sha(values['base_commit'])
    if await db.scalar(select(GitWorktree.id).where(GitWorktree.repository_id==repo.id,GitWorktree.branch==name)):
        raise GitError('branch_already_used')
    tree=GitWorktree(id=identity,project_id=project_id,repository_id=repo.id,task_id=values['task_id'],
        branch=name,base_commit=values['base_commit'],created_by=actor_id,session_id=session.id if session else None)
    db.add(tree);await db.flush()
    workflow=CodeWorkflow(worktree_id=tree.id);db.add(workflow);await db.flush()
    db.add(CodeWorkflowEvent(workflow_id=workflow.id,revision=1,phase='analysis',reason='Préparation du worktree',actor_id=actor_id,session_id=tree.session_id))
    operation=GitOperation(project_id=project_id,worktree_id=tree.id,kind='create',actor_id=actor_id,
        session_id=tree.session_id,capability='git.worktree' if session else None,request_key=values['request_key'],
        request_fingerprint=digest(payload),policy_fingerprint=repo.policy_fingerprint)
    db.add(operation);await db.flush();event(db,project_id,actor_id,'worktree.request',tree,operation_id=operation.id)
    return operation


async def latest_change(db,tree):
    return await db.scalar(select(CodeChange).where(CodeChange.worktree_id==tree.id).order_by(CodeChange.revision.desc()).limit(1))


async def current_change(db,tree,entry,change_id):
    change=await db.scalar(select(CodeChange).where(CodeChange.id==change_id,CodeChange.worktree_id==tree.id))
    if not change: raise GitMissing('change_missing')
    latest=await latest_change(db,tree)
    actual=await asyncio.to_thread(inspect_tree,entry,tree)
    if latest.id!=change.id or actual['head']!=change.head_commit or actual['fingerprint']!=change.content_fingerprint:
        raise GitError('change_stale')
    return change,actual


async def code_gate(db,tree,change,actual):
    workflow=await db.scalar(select(CodeWorkflow).where(CodeWorkflow.worktree_id==tree.id))
    if (not workflow or workflow.phase!='approved' or workflow.approved_change_id!=change.id or
        actual['dirty'] or actual['unsafe']): raise GitError('human_code_approval_required')
    approver=await db.get(User,workflow.approved_by) if workflow.approved_by else None
    if not approver or not approver.is_active or not await project_permission(db,approver,tree.project_id,'code.approve'): raise GitError('code_approver_revoked')
    await review_gate(db,change)
    await green_gate(db,change,catalog((await db.get(Repository,tree.repository_id)).catalog_key))
    return workflow


async def request_operation(db,project_id,worktree_id,kind,values,actor_id,session=None):
    await project_mutation(db,project_id)
    payload={'project_id':project_id,'worktree_id':worktree_id,'kind':kind,**values,'session_id':session.id if session else None}
    prior=await previous(db,actor_id,values['request_key'],payload)
    if prior: return prior
    if session and kind in SENSITIVE: raise GitError('human_action_required')
    tree,repo,entry,_=await worktree_scope(db,project_id,worktree_id,session,
        execution=kind!='delete',active=kind!='delete')
    if tree.status!='ready': raise GitError('worktree_not_ready')
    params={k:v for k,v in values.items() if k!='request_key' and v is not None}
    if kind!='collect':
        change,actual=await current_change(db,tree,entry,params['change_id'])
        params.update(head=actual['head'],fingerprint=actual['fingerprint'])
        if kind=='test':
            if params['command_key'] not in entry.get('tests',{}): raise GitError('test_command_unknown')
            if actual['unsafe']: raise GitError('unsafe_code')
        elif kind=='delete':
            if actual['dirty']: raise GitError('dirty_worktree')
        else:
            await code_gate(db,tree,change,actual)
            if kind=='merge': params['target_head']=await asyncio.to_thread(target_commit,entry,params['target'])
            elif kind=='push': params['target_head']=await asyncio.to_thread(remote_commit,entry,params['target'])
            elif params['target'] not in entry.get('deployments',{}): raise GitError('deployment_unknown')
    operation=GitOperation(project_id=project_id,worktree_id=tree.id,kind=kind,actor_id=actor_id,
        session_id=session.id if session else None,capability=CAPABILITIES.get(kind) if session else None,
        request_key=values['request_key'],request_fingerprint=digest(payload),policy_fingerprint=repo.policy_fingerprint,
        parameters=params,status='awaiting_approval' if kind in SENSITIVE else 'queued',
        expires_at=utcnow()+timedelta(hours=1) if kind in SENSITIVE else None)
    db.add(operation);await db.flush();event(db,project_id,actor_id,'operation.request',operation,kind=kind)
    return operation


async def decide_operation(db,project_id,operation_id,decision,reason,actor_id):
    row=await db.scalar(select(GitOperation).where(GitOperation.id==operation_id,GitOperation.project_id==project_id).with_for_update())
    if not row: raise GitMissing('operation_missing')
    safe_text(reason)
    if not reason.strip(): raise GitError('reason_required')
    wanted='queued' if decision=='approve' else 'rejected'
    if row.decided_by==actor_id and row.decision_reason==reason and (row.status==wanted or (decision=='approve' and row.decided_at and row.status!='rejected')): return row
    if row.kind not in SENSITIVE or row.status!='awaiting_approval' or row.expires_at<=utcnow(): raise GitError('approval_unavailable')
    tree,repo,entry,_=await worktree_scope(db,project_id,row.worktree_id,execution=False,active=row.kind!='delete')
    if row.policy_fingerprint!=repo.policy_fingerprint: raise GitError('approval_stale')
    if decision=='approve':
        change,actual=await current_change(db,tree,entry,row.parameters['change_id'])
        if actual['fingerprint']!=row.parameters['fingerprint'] or actual['dirty']: raise GitError('approval_stale')
        if row.kind!='delete': await code_gate(db,tree,change,actual)
        if row.kind=='merge' and await asyncio.to_thread(target_commit,entry,row.parameters['target'])!=row.parameters['target_head']: raise GitError('target_changed')
        if row.kind=='push' and await asyncio.to_thread(remote_commit,entry,row.parameters['target'])!=row.parameters['target_head']: raise GitError('remote_changed')
    row.status=wanted;row.decided_by=actor_id;row.decision_reason=reason;row.decided_at=utcnow()
    event(db,project_id,actor_id,'operation.'+decision,row,kind=row.kind)
    return row


async def snapshot(db,tree,actual,actor_id,session_id=None):
    prior=await latest_change(db,tree)
    row=CodeChange(project_id=tree.project_id,worktree_id=tree.id,revision=prior.revision+1 if prior else 1,
        head_commit=actual['head'],content_fingerprint=actual['fingerprint'],manifest=actual,
        created_by=actor_id,session_id=session_id)
    db.add(row);await db.flush();event(db,tree.project_id,actor_id,'collect',row,worktree_id=tree.id,revision=row.revision)
    return row


async def review_create(db,project_id,change_id,actor_id,session=None):
    await project_mutation(db,project_id)
    change=await db.scalar(select(CodeChange).where(CodeChange.id==change_id,CodeChange.project_id==project_id))
    if not change: raise GitMissing('change_missing')
    tree=await db.get(GitWorktree,change.worktree_id)
    await task_scope(db,project_id,tree.task_id,session,execution=False)
    await lock_repository(db,project_id,tree.repository_id,active=False)
    prior=await db.scalar(select(CodeReview).where(CodeReview.change_id==change.id,CodeReview.reviewer_id==actor_id,
        CodeReview.session_id==session.id if session else CodeReview.session_id.is_(None)))
    if prior: return prior
    row=CodeReview(project_id=project_id,change_id=change.id,reviewer_id=actor_id,session_id=session.id if session else None)
    db.add(row);await db.flush();event(db,project_id,actor_id,'review.create',row,session_id=row.session_id)
    db.add(CodeReviewEvent(review_id=row.id,revision=1,action='start'))
    return row


async def review_action(db,project_id,review_id,values,actor_id,session=None):
    await project_mutation(db,project_id)
    row=await db.scalar(select(CodeReview).where(CodeReview.id==review_id,CodeReview.project_id==project_id).with_for_update())
    if not row: raise GitMissing('review_missing')
    if row.reviewer_id!=actor_id or row.session_id!=(session.id if session else None): raise GitError('reviewer_required')
    if row.revision!=values['expected_revision']: raise GitError('review_revision_stale')
    change=await db.get(CodeChange,row.change_id);tree=await db.get(GitWorktree,change.worktree_id)
    await task_scope(db,project_id,tree.task_id,session,execution=False)
    await lock_repository(db,project_id,tree.repository_id,active=False)
    action=values['action']; text=values.get('text','');safe_text(text)
    file=values.get('file_path');hunk=values.get('hunk_id');resolves=values.get('resolves_id')
    if action=='comment':
        target=next((f for f in change.manifest['files'] if f['path']==file),None)
        if not target or not text.strip() or (hunk and hunk not in {h['id'] for h in target['hunks']}): raise GitError('review_anchor_invalid')
        row.status='changes_requested' if values.get('correction') else 'commented'
    elif action=='resolve':
        original=await db.scalar(select(CodeReviewEvent).where(CodeReviewEvent.id==resolves,CodeReviewEvent.review_id==row.id))
        if not original or original.action!='comment' or await db.scalar(select(CodeReviewEvent.id).where(CodeReviewEvent.resolves_id==resolves)):
            raise GitError('comment_unavailable')
        if not text.strip(): raise GitError('reason_required')
    elif action=='changes_requested':
        if not text.strip(): raise GitError('reason_required')
        row.status='changes_requested'
    elif action=='reviewed':
        if await open_corrections(db,row): raise GitError('corrections_open')
        row.status='reviewed'
    else: raise GitError('review_action_invalid')
    row.revision+=1
    db.add(CodeReviewEvent(review_id=row.id,revision=row.revision,action=action,text=text,
        file_path=file,hunk_id=hunk,correction=values.get('correction',False),resolves_id=resolves))
    event(db,project_id,actor_id,'review.'+action,row,revision=row.revision,session_id=row.session_id)
    return row


async def open_corrections(db,review):
    events=(await db.scalars(select(CodeReviewEvent).where(CodeReviewEvent.review_id==review.id))).all()
    resolved={e.resolves_id for e in events if e.action=='resolve'}
    return any(e.correction and e.id not in resolved for e in events)


async def green_gate(db,change,entry):
    keys=set(entry.get('tests',{}))
    if not keys: raise GitError('test_catalog_required')
    for key in keys:
        last=await db.scalar(select(CodeTestRun).where(CodeTestRun.change_id==change.id,CodeTestRun.command_key==key).order_by(CodeTestRun.created_at.desc()).limit(1))
        if not last or last.status!='passed': raise GitError('test_evidence_required')


async def review_gate(db,change):
    rows=(await db.scalars(select(CodeReview).where(CodeReview.change_id==change.id))).all()
    if not any(r.session_id is None and r.status=='reviewed' for r in rows): raise GitError('human_review_required')
    for row in rows:
        if row.status=='changes_requested' or await open_corrections(db,row): raise GitError('corrections_open')


async def advance_workflow(db,project_id,worktree_id,values,actor_id,session=None):
    await project_mutation(db,project_id)
    tree,_,entry,_=await worktree_scope(db,project_id,worktree_id,session,execution=session is not None)
    row=await db.scalar(select(CodeWorkflow).where(CodeWorkflow.worktree_id==tree.id).with_for_update())
    if row.revision!=values['expected_revision']: raise GitError('workflow_revision_stale')
    phase=values['phase'];reason=values['reason'];safe_text(reason)
    if not reason.strip(): raise GitError('reason_required')
    if phase not in WORKFLOW_TRANSITIONS[row.phase]: raise GitError('workflow_transition_invalid')
    change=None;test=None
    if row.phase=='tests' or phase in ('review','approved'):
        if not values.get('change_id'): raise GitError('change_required')
        change,actual=await current_change(db,tree,entry,values['change_id'])
        if actual['unsafe']: raise GitError('unsafe_code')
        if row.phase=='tests' or phase=='review':
            test=await db.scalar(select(CodeTestRun).where(CodeTestRun.id==values.get('test_id'),CodeTestRun.change_id==change.id))
            expected='failed' if row.phase=='tests' else 'passed'
            if not test or test.status!=expected: raise GitError('test_evidence_required')
            if row.phase=='tests' and not any(any(f['path']==p or f['path'].startswith(p+'/') for p in entry.get('test_paths',['tests'])) for f in actual['files']):
                raise GitError('changed_tests_required')
        if phase=='approved':
            if session: raise GitError('human_approval_required')
            if actual['dirty']: raise GitError('dirty_worktree')
            await review_gate(db,change)
            await green_gate(db,change,entry)
            # La preuve verte doit toujours correspondre à cette collecte exacte.
            test=await db.scalar(select(CodeTestRun).where(CodeTestRun.change_id==change.id,CodeTestRun.status=='passed').order_by(CodeTestRun.created_at.desc()).limit(1))
            if not test: raise GitError('test_evidence_required')
    row.phase=phase;row.revision+=1
    row.approved_change_id=change.id if phase=='approved' else None
    row.approved_by=actor_id if phase=='approved' else None
    db.add(CodeWorkflowEvent(workflow_id=row.id,revision=row.revision,phase=phase,reason=reason,actor_id=actor_id,
        session_id=session.id if session else None,change_id=change.id if change else None,test_id=test.id if test else None))
    event(db,project_id,actor_id,'workflow.'+phase,row,revision=row.revision,session_id=session.id if session else None)
    return row


async def tree_detail(db,project_id,worktree_id,session=None):
    tree=await db.scalar(select(GitWorktree).where(GitWorktree.id==worktree_id,GitWorktree.project_id==project_id))
    if not tree: raise GitMissing('worktree_missing')
    if session: await task_scope(db,project_id,tree.task_id,session,execution=False)
    changes=(await db.scalars(select(CodeChange).where(CodeChange.worktree_id==tree.id).order_by(CodeChange.revision.desc()))).all()
    workflow=await db.scalar(select(CodeWorkflow).where(CodeWorkflow.worktree_id==tree.id))
    events=(await db.scalars(select(CodeWorkflowEvent).where(CodeWorkflowEvent.workflow_id==workflow.id).order_by(CodeWorkflowEvent.revision))).all()
    ops=(await db.scalars(select(GitOperation).where(GitOperation.worktree_id==tree.id).order_by(GitOperation.created_at.desc()).limit(100))).all()
    tests=(await db.scalars(select(CodeTestRun).where(CodeTestRun.change_id.in_([c.id for c in changes])).order_by(CodeTestRun.created_at.desc()))).all()
    reviews=(await db.scalars(select(CodeReview).where(CodeReview.change_id.in_([c.id for c in changes])))).all()
    review_data=[]
    for r in reviews:
        ev=(await db.scalars(select(CodeReviewEvent).where(CodeReviewEvent.review_id==r.id).order_by(CodeReviewEvent.revision))).all()
        review_data.append({**view(r),'events':[view(e) for e in ev]})
    return {'worktree':view(tree),'workspace_ref':f'{tree.project_id}/{tree.id}','changes':[view(c) for c in changes],
        'workflow':{**view(workflow),'events':[{k:getattr(e,k) for k in ('revision','phase','reason','actor_id','session_id','change_id','test_id','created_at')} for e in events]},
        'operations':[view(o) for o in ops],'tests':[view(t) for t in tests],'reviews':review_data}
