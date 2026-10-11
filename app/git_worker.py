"""Jobs Git : droits recontrôlés, démarrage durable, aucun retry aveugle d'effet."""
import asyncio

from sqlalchemy import select

from app.agent_models import AgentSession,AgentTeam,SkillProfile,AgentTeamSkill
from app.git_adapter import (GitError,catalog,create_tree,inspect_tree,run_test,apply_sensitive,check_tree,
    tree_path,git,target_commit,remote_commit)
from app.git_models import GitOperation,GitWorktree,Repository,CodeTestRun
from app.git_service import (PERMISSIONS,SENSITIVE,code_gate,current_change,event,snapshot,task_scope,view,worktree_scope)
from app.identity_models import Project,User,utcnow
from app.identity_service import database_factory,project_permission


async def lock_operation(db,operation_id):
    initial=await db.get(GitOperation,operation_id)
    if not initial: return None
    if initial.session_id:
        await db.scalar(select(AgentSession).where(AgentSession.id==initial.session_id).with_for_update())
    await db.scalar(select(Project).where(Project.id==initial.project_id).with_for_update().execution_options(populate_existing=True))
    return await db.scalar(select(GitOperation).where(GitOperation.id==operation_id).with_for_update().execution_options(populate_existing=True))


async def guard(db,row):
    actor=await db.get(User,row.actor_id);project=await db.get(Project,row.project_id)
    if not actor or not actor.is_active or not await project_permission(db,actor,row.project_id,PERMISSIONS[row.kind]):
        raise GitError('actor_permission_revoked')
    if project.status=='archived': raise GitError('project_archived')
    session=None
    if row.session_id:
        session=await db.scalar(select(AgentSession).where(AgentSession.id==row.session_id).with_for_update().execution_options(populate_existing=True))
        if not session or session.status!='active' or min(session.expires_at,session.max_expires_at)<=utcnow() or row.capability not in session.capabilities:
            raise GitError('agent_session_unavailable')
        team=await db.get(AgentTeam,session.team_id);profile=await db.get(SkillProfile,session.skill_profile_id)
        team_skill=await db.scalar(select(AgentTeamSkill.id).where(AgentTeamSkill.team_id==session.team_id,AgentTeamSkill.skill_profile_id==session.skill_profile_id))
        if not team or not team.is_active or not profile or not profile.is_active or not team_skill or row.capability not in profile.allowed_capabilities: raise GitError('agent_profile_unavailable')
        if session.created_by!=row.actor_id or not await project_permission(db,actor,row.project_id,'agent.session.connect'):
            raise GitError('agent_permission_revoked')
    tree,repo,entry,_=await worktree_scope(db,row.project_id,row.worktree_id,session,
        execution=row.kind!='delete',active=row.kind!='delete')
    if row.policy_fingerprint!=repo.policy_fingerprint: raise GitError('operation_policy_changed')
    if row.kind in SENSITIVE:
        decider=await db.get(User,row.decided_by) if row.decided_by else None
        if row.session_id or not decider or not decider.is_active or not await project_permission(db,decider,row.project_id,PERMISSIONS[row.kind]):
            raise GitError('human_approval_required')
        if not row.expires_at or row.expires_at<=utcnow(): raise GitError('approval_expired')
    if row.kind!='create' and tree.status!='ready': raise GitError('worktree_not_ready')
    return tree,entry


async def perform(db,row,tree,entry):
    if row.kind=='create':
        row.result={'effect_started':True}
        result=await asyncio.to_thread(create_tree,entry,tree)
        tree.status='ready'
    elif row.kind=='collect':
        actual=await asyncio.to_thread(inspect_tree,entry,tree)
        change=await snapshot(db,tree,actual,row.actor_id,row.session_id)
        result={'change_id':change.id,'revision':change.revision}
    else:
        change,actual=await current_change(db,tree,entry,row.parameters['change_id'])
        if actual['head']!=row.parameters['head'] or actual['fingerprint']!=row.parameters['fingerprint']: raise GitError('operation_stale')
        if row.kind=='test':
            if actual['unsafe']: raise GitError('unsafe_code')
            evidence=await asyncio.to_thread(run_test,entry,tree,row.parameters['command_key'],row.id)
            after=await asyncio.to_thread(inspect_tree,entry,tree)
            if after['fingerprint']!=actual['fingerprint']: raise GitError('workspace_changed_during_test')
            test=CodeTestRun(project_id=row.project_id,change_id=change.id,operation_id=row.id,
                command_key=row.parameters['command_key'],**evidence)
            db.add(test);await db.flush()
            result={'test_id':test.id,'status':test.status}
        else:
            if actual['dirty']: raise GitError('dirty_worktree')
            if row.kind!='delete': await code_gate(db,tree,change,actual)
            if row.kind=='merge' and await asyncio.to_thread(target_commit,entry,row.parameters['target'])!=row.parameters['target_head']:
                raise GitError('target_changed')
            params={**row.parameters,'operation_id':row.id}
            row.result={'effect_started':True}
            result=await asyncio.to_thread(apply_sensitive,entry,tree,row.kind,params)
            if row.kind=='delete': tree.status='removed';tree.removed_at=utcnow()
    return result


async def run_operation(operation_id):
    factory=database_factory()
    try:
        async with factory() as db:
            row=await lock_operation(db,operation_id)
            if not row or row.status not in ('queued','running'): return
            if row.status=='running':
                row.status='uncertain';row.result={'error':'interrupted_operation'}
                tree=await db.get(GitWorktree,row.worktree_id)
                if row.kind=='create': tree.status='uncertain'
                event(db,row.project_id,row.actor_id,'operation.uncertain',row,kind=row.kind)
                await db.commit();return
            try: await guard(db,row)
            except (GitError,ValueError) as exc:
                row.status='failed';row.result={'error':exc.code if isinstance(exc,GitError) else 'execution_unavailable'};row.ended_at=utcnow()
                tree=await db.get(GitWorktree,row.worktree_id)
                if row.kind=='create': tree.status='failed'
                event(db,row.project_id,row.actor_id,'operation.failed',row,kind=row.kind,error=row.result['error'])
                await db.commit();return
            row.status='running';row.started_at=utcnow();row.result={}
            event(db,row.project_id,row.actor_id,'operation.start',row,kind=row.kind)
            await db.commit()
            row=await lock_operation(db,operation_id)
            if row.status!='running': return
            try:
                tree,entry=await guard(db,row)
                result=await perform(db,row,tree,entry)
                row.status='succeeded';row.result=result
                event(db,row.project_id,row.actor_id,'operation.complete',row,kind=row.kind)
            except (GitError,ValueError,OSError,UnicodeError) as exc:
                # Pas de trace brute ni de rollback de fichiers. Effet potentiellement partiel.
                row.status='uncertain' if row.result.get('effect_started') else 'failed'
                row.result={'error':exc.code if isinstance(exc,GitError) else 'operation_failed','reconciliation_required':row.status=='uncertain'}
                if row.kind=='create': tree.status=row.status
                event(db,row.project_id,row.actor_id,'operation.failed',row,kind=row.kind)
            row.ended_at=utcnow();await db.commit()
    finally: await factory.kw['bind'].dispose()


async def reconcile(db,project_id,operation_id,actor_id):
    row=await db.scalar(select(GitOperation).where(GitOperation.id==operation_id,GitOperation.project_id==project_id).with_for_update())
    if not row: raise GitError('operation_missing')
    if row.status not in ('uncertain','failed'): raise GitError('reconciliation_unavailable')
    tree,_,entry,_=await worktree_scope(db,project_id,row.worktree_id,execution=False,active=row.kind!='delete')
    if row.kind=='create':
        actual=await asyncio.to_thread(inspect_tree,entry,tree)
        if actual['head']!=tree.base_commit or actual['dirty']: raise GitError('manual_reconciliation_required')
        tree.status='ready';result={'head':tree.base_commit}
    elif row.kind=='delete':
        path=tree_path(entry,tree,exists=False)
        listing=await asyncio.to_thread(git,entry['_path'],'worktree','list','--porcelain')
        if path.exists() or ('worktree '+str(path)).encode() in listing: raise GitError('manual_reconciliation_required')
        tree.status='removed';tree.removed_at=utcnow();result={'removed':True,'branch_retained':tree.branch}
    elif row.kind=='merge':
        if await asyncio.to_thread(target_commit,entry,row.parameters['target'])!=row.parameters['head']: raise GitError('manual_reconciliation_required')
        result={'head':row.parameters['head'],'target':row.parameters['target']}
    elif row.kind=='push':
        if await asyncio.to_thread(remote_commit,entry,row.parameters['target'])!=row.parameters['head']: raise GitError('manual_reconciliation_required')
        result={'head':row.parameters['head'],'target':row.parameters['target']}
    else: raise GitError('manual_reconciliation_required')
    row.status='succeeded';row.result={**result,'reconciled':True};row.ended_at=utcnow()
    event(db,project_id,actor_id,'operation.reconcile',row,kind=row.kind)
    return row
