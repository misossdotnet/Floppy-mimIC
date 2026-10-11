"""AGENT-15 à 20 : Git réel, TDD rouge/vert, portes exactes et refus de sécurité."""
import asyncio
from concurrent.futures import ThreadPoolExecutor
import json
import os
from pathlib import Path
import subprocess
import sys
from types import SimpleNamespace
from uuid import uuid4

import pytest
from sqlalchemy import select,func

from app.git_adapter import (GitError,branch,catalog,confined,digest,logical_path,inspect_tree,git,process,minimize)
from app.git_models import Repository,GitWorktree,GitOperation,CodeChange,CodeTestRun
from app.git_service import prepare_tree
from app.git_worker import run_operation
from app.identity_models import AuditEvent,Project,ProjectMember,User,utcnow
from app.identity_service import database_factory

from .conftest import with_db,create_user,ROOT
from .test_identity_acl import login
from .test_agent_conversations import messaging


def local_git(path,*args):
    env={**os.environ,'GIT_CONFIG_NOSYSTEM':'1','GIT_CONFIG_GLOBAL':os.devnull}
    r=subprocess.run(['git','-C',str(path),*args],env=env,check=True,capture_output=True,text=True)
    return r.stdout.strip()


@pytest.fixture
def git_space(tmp_path,monkeypatch):
    repos=tmp_path/'repos';trees=tmp_path/'trees';repos.mkdir();trees.mkdir()
    checkout=repos/'sample';checkout.mkdir()
    local_git(checkout,'init','-b','main');local_git(checkout,'config','user.name','Recette');local_git(checkout,'config','user.email','recette@example.org')
    (checkout/'maths.py').write_text('def add(a,b):\n    return a-b\n')
    local_git(checkout,'add','.');local_git(checkout,'commit','-m','Départ');base=local_git(checkout,'rev-parse','HEAD')
    local_git(checkout,'branch','integration')
    remote=repos/'remote.git';remote.mkdir();local_git(remote,'init','--bare')
    output=tmp_path/'deployment-proof';adapter=tmp_path/'deploy.py'
    adapter.write_text('#!/usr/bin/env python3\nimport os\nfrom pathlib import Path\nPath('+repr(str(output))+').write_text(os.environ["MIMIC_COMMIT"])\nprint("Recette livrée",os.environ["MIMIC_WORKTREE"],"token=secret-fictif-deploiement-123456")\n');adapter.chmod(0o755)
    image=os.environ.get('MIMIC_TEST_IMAGE','sha256:'+'0'*64)
    entry={'path':'sample','remote':str(remote),'tests':{'unit':{'image':image,'argv':['python','-m','unittest','discover','-s','tests']}},'deployments':{'recette':{'argv':[str(adapter)]}},'test_paths':['tests']}
    monkeypatch.setenv('MIMIC_GIT_ROOT',str(repos));monkeypatch.setenv('MIMIC_WORKTREE_ROOT',str(trees));monkeypatch.setenv('MIMIC_GIT_CATALOG',json.dumps({'sample':entry}))
    return {'repos':repos,'trees':trees,'checkout':checkout,'remote':remote,'base':base,'entry':entry,'output':output}


@pytest.fixture
def development(pg_client,messaging,git_space,monkeypatch):
    m=messaging;g=git_space
    from app.worker import git_execute
    queued=[];monkeypatch.setattr(git_execute,'delay',lambda id:queued.append(id))
    async def identities(db):
        member=await db.scalar(select(ProjectMember).where(ProjectMember.project_id==m['project'],ProjectMember.role_code=='architect'))
        return (await db.get(User,member.user_id)).email,(await db.get(User,m['manager'])).email
    architect,manager=asyncio.run(with_db(identities));base=m['base']+'/git'
    pg_client.cookies.clear();csrf=login(pg_client,architect)
    r=pg_client.post(base+'/repositories',headers={'X-CSRF-Token':csrf},json={'catalog_key':'sample'})
    assert r.status_code==201,r.text
    repo=r.json()['id'];pg_client.cookies.clear();csrf=login(pg_client,manager)
    def post(url,data): return pg_client.post(base+url,headers={'X-CSRF-Token':csrf},json=data)
    request={'request_key':str(uuid4()),'repository_id':repo,'task_id':m['tasks']['backend'],'base_commit':g['base']}
    created=post('/worktrees',request);assert created.status_code==202,created.text
    op=created.json();asyncio.run(run_operation(op['id']))
    tree=op['worktree_id'];detail=pg_client.get(base+'/worktrees/'+tree).json()
    assert detail['worktree']['status']=='ready',detail
    path=g['trees']/m['project']/tree
    return {**m,**g,'base':m['base'],'base_commit':g['base'],'repo':repo,'tree':tree,'path':path,'url':base,'csrf':csrf,'architect_email':architect,'manager_email':manager,'queued':queued,'create_request':request}


def post(client,g,url,data):
    return client.post(g['url']+url,headers={'X-CSRF-Token':g['csrf']},json=data)


def operation(client,g,kind,**values):
    r=post(client,g,f"/worktrees/{g['tree']}/operations/{kind}",{'request_key':str(uuid4()),**values})
    assert r.status_code==202,r.text
    row=r.json()
    if row['status']=='queued': asyncio.run(run_operation(row['id']))
    return row


def detail(client,g): return client.get(g['url']+'/worktrees/'+g['tree']).json()


def collect(client,g):
    operation(client,g,'collect')
    return detail(client,g)['changes'][0]


def advance(client,g,phase,**extra):
    flow=detail(client,g)['workflow']
    return post(client,g,f"/worktrees/{g['tree']}/workflow",{'expected_revision':flow['revision'],'phase':phase,'reason':'Recette vérifiée',**extra})


def sign_in(client,g,who):
    client.cookies.clear();g['csrf']=login(client,g[who+'_email'])


def approved_code(client,g):
    assert advance(client,g,'tests').status_code==200
    (g['path']/'tests').mkdir();(g['path']/'tests'/'test_maths.py').write_text('import unittest\nfrom maths import add\nclass Maths(unittest.TestCase):\n    def test_add(self):\n        self.assertEqual(add(2,3),5)\n')
    (g['path']/'tests'/'test_boundary.py').write_text('import unittest,os,socket\nfrom pathlib import Path\nclass Boundary(unittest.TestCase):\n    def test_boundary(self):\n        self.assertEqual(os.getuid(),65534)\n        self.assertFalse(Path("/var/run/docker.sock").exists())\n        self.assertNotIn("MIMIC_AUTH_KEY",os.environ)\n        with self.assertRaises(OSError): Path("/workspace/escape").write_text("refus")\n        with self.assertRaises(OSError): Path("/etc/escape").write_text("refus")\n        with self.assertRaises(OSError): socket.create_connection(("1.1.1.1",80),timeout=.1)\n')
    red=collect(client,g);operation(client,g,'test',change_id=red['id'],command_key='unit')
    test=detail(client,g)['tests'][0];assert test['status']=='failed',test
    assert advance(client,g,'implementation',change_id=red['id'],test_id=test['id']).status_code==200
    (g['path']/'maths.py').write_text('def add(a,b):\n    return a+b\n')
    local_git(g['path'],'add','.');local_git(g['path'],'commit','-m','Tests et correction')
    green=collect(client,g);assert advance(client,g,'execution').status_code==200
    operation(client,g,'test',change_id=green['id'],command_key='unit')
    test=detail(client,g)['tests'][0];assert test['status']=='passed',test
    assert advance(client,g,'review',change_id=green['id'],test_id=test['id']).status_code==200
    review=post(client,g,'/reviews',{'change_id':green['id']}).json()
    anchor=next(f for f in green['manifest']['files'] if f['path']=='maths.py')
    hunk=anchor['hunks'][0]['id']
    comment=post(client,g,'/reviews/'+review['id'],{'expected_revision':1,'action':'comment','file_path':'maths.py','hunk_id':hunk,'text':'Vérifier addition','correction':True})
    assert comment.status_code==200,comment.text
    assert post(client,g,'/reviews/'+review['id'],{'expected_revision':2,'action':'reviewed'}).status_code==409
    note=detail(client,g)['reviews'][0]['events'][-1]
    assert post(client,g,'/reviews/'+review['id'],{'expected_revision':2,'action':'resolve','resolves_id':note['id'],'text':'Couvert par test'}).status_code==200
    assert post(client,g,'/reviews/'+review['id'],{'expected_revision':3,'action':'reviewed'}).status_code==200
    assert advance(client,g,'approved',change_id=green['id']).status_code==403
    sign_in(client,g,'architect')
    decision=advance(client,g,'approved',change_id=green['id']);assert decision.status_code==200,decision.text
    sign_in(client,g,'manager')
    return green


def approve_and_execute(client,g,op):
    decided=post(client,g,'/operations/'+op['id']+'/decision',{'decision':'approve','reason':'Action et destination revues'})
    assert decided.status_code==200,decided.text
    count=len(g['queued'])
    # Une approbation et son rejeu ne dispatchent aucun effet.
    assert post(client,g,'/operations/'+op['id']+'/decision',{'decision':'approve','reason':'Action et destination revues'}).status_code==200
    assert len(g['queued'])==count
    assert post(client,g,'/operations/'+op['id']+'/dispatch',{}).status_code==200
    asyncio.run(run_operation(op['id']))
    saved=next(o for o in detail(client,g)['operations'] if o['id']==op['id'])
    assert saved['status']=='succeeded',saved
    asyncio.run(run_operation(op['id']))
    return saved


def test_tdd_revue_hunks_merge_push_deploiement_et_nettoyage(pg_client,development):
    g=development;green=approved_code(pg_client,g);head=green['head_commit']
    for kind,target in [('merge','integration'),('push','feature'),('deploy','recette')]:
        op=operation(pg_client,g,kind,change_id=green['id'],target=target)
        assert op['status']=='awaiting_approval'
        executed=approve_and_execute(pg_client,g,op)
        if kind=='deploy':
            assert str(g['path']) not in executed['result']['log']
            assert 'secret-fictif-deploiement' not in executed['result']['log']
    assert local_git(g['checkout'],'rev-parse','integration')==head
    assert local_git(g['checkout'],'rev-parse','main')==g['base_commit']
    assert local_git(g['remote'],'rev-parse','feature')==head
    assert g['output'].read_text()==head
    approve_and_execute(pg_client,g,operation(pg_client,g,'delete',change_id=green['id']))
    d=detail(pg_client,g);assert d['worktree']['status']=='removed' and not g['path'].exists()
    assert local_git(g['checkout'],'rev-parse',d['worktree']['branch'])==head
    assert len(d['changes'])==2 and d['workflow']['phase']=='approved'
    async def verify(db):
        from app.agent_models import AgentTask,FlowRun
        assert (await db.get(AgentTask,g['tasks']['backend'])).status=='claimed'
        assert (await db.get(FlowRun,g['run'])).status=='running'
        audits=(await db.scalars(select(AuditEvent).where(AuditEvent.project_id==g['project']))).all()
        assert len([e for e in audits if e.action=='git.operation.complete' and e.details.get('kind')=='merge'])==1
        assert str(g['path']) not in str([e.details for e in audits])
    asyncio.run(with_db(verify))


def test_rbac_csrf_archive_projets_et_catalogue(pg_client,development):
    g=development;base=g['url'];foreign=str(uuid4())
    assert pg_client.post(base+'/worktrees',json=g['create_request']).status_code==403
    assert post(pg_client,g,'/repositories',{'catalog_key':'sample'}).status_code==403
    assert post(pg_client,g,'/repositories',{'catalog_key':'sample','path':'/etc'}).status_code==422
    assert pg_client.get(g['base']+'/git/worktrees/'+foreign).status_code==404
    sign_in(pg_client,g,'architect')
    assert post(pg_client,g,'/repositories',{'catalog_key':'unknown'}).status_code==409
    _,outside=create_user();pg_client.cookies.clear();login(pg_client,outside)
    assert pg_client.get(base+'/worktrees').status_code==404
    pg_client.cookies.clear();login(pg_client,g['reader_email'])
    assert pg_client.get(base+'/worktrees').status_code==200
    assert pg_client.post(base+'/worktrees',headers={'X-CSRF-Token':login(pg_client,g['reader_email'])},json=g['create_request']).status_code==403
    sign_in(pg_client,g,'manager')
    async def archive(db): (await db.get(Project,g['project'])).status='archived'
    asyncio.run(with_db(archive))
    assert post(pg_client,g,f"/worktrees/{g['tree']}/operations/collect",{'request_key':str(uuid4())}).status_code==409
    assert pg_client.get(base+'/worktrees').status_code==200


def test_secrets_historique_chemins_et_collecte(pg_client,development):
    g=development
    secret='secret-fictif-recette-123456';(g['path']/'maths.py').write_text('api_key="'+secret+'"\n')
    local_git(g['path'],'add','.');local_git(g['path'],'commit','-m','Secret fictif')
    (g['path']/'maths.py').write_text('def add(a,b): return a+b\n');local_git(g['path'],'add','.');local_git(g['path'],'commit','-m','Retrait secret')
    change=collect(pg_client,g)
    assert change['manifest']['unsafe'] and secret not in json.dumps(change)
    assert post(pg_client,g,f"/worktrees/{g['tree']}/operations/test",{'request_key':str(uuid4()),'change_id':change['id'],'command_key':'unit'}).status_code==409
    (g['path']/'outside').symlink_to('/etc/passwd')
    operation(pg_client,g,'collect');last=detail(pg_client,g)['operations'][0]
    assert last['status']=='failed' and last['result']['error']=='symlink_forbidden'


def test_rejeux_conflits_revisions_et_accord_obsolete(pg_client,development):
    g=development
    replay=post(pg_client,g,'/worktrees',g['create_request']);assert replay.status_code==202
    assert len(pg_client.get(g['url']+'/worktrees').json()['worktrees'])==1
    assert post(pg_client,g,'/worktrees',{**g['create_request'],'branch':'codex/autre'}).status_code==409
    change=collect(pg_client,g)
    assert post(pg_client,g,f"/worktrees/{g['tree']}/workflow",{'expected_revision':2,'phase':'tests','reason':'Revue'}).status_code==409
    op=operation(pg_client,g,'delete',change_id=change['id'])
    (g['path']/'maths.py').write_text('Nouvelle saisie')
    assert post(pg_client,g,'/operations/'+op['id']+'/decision',{'decision':'approve','reason':'Relu'}).status_code==409
    assert (g['path']/'maths.py').read_text()=='Nouvelle saisie'


def test_agent_capacites_claim_avis_et_interdiction_actions(pg_client,development):
    g=development
    created=pg_client.post(g['base']+'/agent-sessions',headers={'X-CSRF-Token':g['csrf']},json={'team_id':g['team'],'client_code':'codex','skill_code':'backend','capabilities':['session.heartbeat','task.claim','git.read','git.worktree','git.collect','git.test','git.review']})
    assert created.status_code==201,created.text
    connected=created.json()
    old=g['sessions']['backend']
    revoked=pg_client.delete(g['base']+f"/agent-sessions/{old['session']['id']}",headers={'X-CSRF-Token':g['csrf']})
    assert revoked.status_code==200,revoked.text
    auth={'Authorization':'Bearer '+connected['token']};sid=connected['session']['id']
    claim=pg_client.post(g['base']+f"/agent-tasks/{g['tasks']['backend']}/claim?session_id={sid}",headers=auth)
    assert claim.status_code==200,claim.text
    base=g['base']+'/agent-git'
    assert pg_client.get(base+f"/worktrees/{g['tree']}?session_id={sid}",headers=auth).status_code==200
    request={'session_id':sid,'request_key':str(uuid4())}
    collect_op=pg_client.post(base+f"/worktrees/{g['tree']}/operations/collect",headers=auth,json=request)
    assert collect_op.status_code==202,collect_op.text
    asyncio.run(run_operation(collect_op.json()['id']));change=detail(pg_client,g)['changes'][0]
    review=pg_client.post(base+'/reviews',headers=auth,json={'session_id':sid,'change_id':change['id']})
    assert review.status_code==201 and review.json()['content_status']=='Brouillon IA'
    assert pg_client.post(base+f"/worktrees/{g['tree']}/workflow",headers=auth,json={'session_id':sid,'expected_revision':1,'phase':'approved','reason':'Agent'}).status_code==403
    assert pg_client.post(base+f"/worktrees/{g['tree']}/operations/push",headers=auth,json=request).status_code==422
    # Capability absente sur l'ancienne session frontend ; équipe étrangère invisible.
    frontend=g['sessions']['frontend']
    assert pg_client.get(base+f"/worktrees/{g['tree']}?session_id={frontend['session']['id']}",headers={'Authorization':'Bearer '+frontend['token']}).status_code==403


def test_erreur_broker_worker_recontrole_revocation_et_incertain(pg_client,development,monkeypatch):
    g=development
    from app.worker import git_execute
    def unavailable(id): raise RuntimeError('secret-fictif-non-affiche')
    monkeypatch.setattr(git_execute,'delay',unavailable)
    r=operation(pg_client,g,'collect') # appel manuel worker de recette
    assert next(o for o in detail(pg_client,g)['operations'] if o['id']==r['id'])['status']=='succeeded'
    row=post(pg_client,g,f"/worktrees/{g['tree']}/operations/collect",{'request_key':str(uuid4())}).json()
    assert row['status']=='queued' and row['result']=={'error':'broker_unavailable'}
    async def revoke(db): (await db.get(Repository,g['repo'])).is_active=False
    asyncio.run(with_db(revoke));asyncio.run(run_operation(row['id']))
    assert next(o for o in detail(pg_client,g)['operations'] if o['id']==row['id'])['result']['error']=='repository_revoked'
    async def running(db):
        (await db.get(Repository,g['repo'])).is_active=True
        (await db.get(GitOperation,row['id'])).status='running'
    asyncio.run(with_db(running));asyncio.run(run_operation(row['id']))
    assert next(o for o in detail(pg_client,g)['operations'] if o['id']==row['id'])['status']=='uncertain'
    async def outcomes(db):
        events=(await db.scalars(select(AuditEvent).where(AuditEvent.target_id==row['id'],AuditEvent.action.in_(['git.operation.failed','git.operation.uncertain'])))).all()
        assert len(events)==2 and all(e.outcome=='failed' for e in events)
        assert {e.details['status'] for e in events}=={'failed','uncertain'}
    asyncio.run(with_db(outcomes))


def test_revue_ancres_auteur_et_workflow_sans_preuve(pg_client,development):
    g=development;(g['path']/'maths.py').write_text('def add(a,b): return a+b\n');change=collect(pg_client,g)
    review=post(pg_client,g,'/reviews',{'change_id':change['id']}).json()
    for file,hunk in [('autre.py',None),('maths.py','0'*64)]:
        assert post(pg_client,g,'/reviews/'+review['id'],{'expected_revision':1,'action':'comment','file_path':file,'hunk_id':hunk,'text':'Note'}).status_code==409
    assert advance(pg_client,g,'review',change_id=change['id']).status_code==409
    assert advance(pg_client,g,'tests').status_code==200
    assert advance(pg_client,g,'implementation',change_id=change['id']).status_code==409
    sign_in(pg_client,g,'architect')
    assert post(pg_client,g,'/reviews/'+review['id'],{'expected_revision':1,'action':'reviewed'}).status_code==409


def test_ecrans_listes_formulaires_echappement_et_acl(pg_client,development):
    g=development;url=f"/projects/{g['project']}/agents/code"
    page=pg_client.get(url+'?selected='+g['tree']);assert page.status_code==200,page.text
    assert 'Mode liste' in page.text and '<script' not in page.text and 'name="project"' in page.text
    (g['path']/'maths.py').write_text('print("<script>alert(1)</script>")\n');collect(pg_client,g)
    page=pg_client.get(url+'?selected='+g['tree']);assert '&lt;script&gt;' in page.text
    assert str(g['path']) not in page.text
    csrf=g['csrf']
    result=pg_client.post(url+'/actions',data={'csrf_token':csrf,'action':'workflow','worktree_id':g['tree'],'expected_revision':1,'phase':'tests','reason':'Revue'})
    assert result.status_code==303,result.text
    error=pg_client.post(url+'/actions',data={'csrf_token':csrf,'action':'workflow','worktree_id':g['tree'],'expected_revision':1,'phase':'review','reason':'Revue'})
    assert error.status_code==409 and 'role="alert"' in error.text
    assert 'maxlength="1000">Revue</textarea>' in error.text
    assert 'href="?selected=' not in error.text
    assert 'action="'+url+'"' in error.text
    pg_client.cookies.clear();login(pg_client,g['reader_email'])
    assert 'name="action" value="collect"' not in pg_client.get(url+'?selected='+g['tree']).text
    assert pg_client.get(url+'?project='+str(uuid4())).status_code==404


def test_concurrence_cle_worktree_et_migration_retour(pg_client,development):
    g=development;values={**g['create_request'],'request_key':str(uuid4()),'branch':'codex/concurrent'}
    async def call():
        factory=database_factory()
        try:
            async with factory() as db:
                op=await prepare_tree(db,g['project'],values,g['manager']);await db.commit();return op.id
        finally: await factory.kw['bind'].dispose()
    with ThreadPoolExecutor(max_workers=2) as pool:
        ids=list(pool.map(lambda _:asyncio.run(call()),range(2)))
    assert ids[0]==ids[1]
    subprocess.run([sys.executable,'-m','alembic','downgrade','0019_technical_incidents'],cwd=ROOT,check=True,capture_output=True)
    assert g['path'].exists() # Aucun nettoyage filesystem lors du rollback SQL.
    subprocess.run([sys.executable,'-m','alembic','upgrade','head'],cwd=ROOT,check=True,capture_output=True)
    assert pg_client.get(g['url']+'/repositories').json()=={'repositories':[]}


@pytest.mark.parametrize('value',['../outside','/etc/passwd','tests/../../secret','tests\\file','tests/.git/config','a\nsecret','a//b'])
def test_chemins_interdits(value):
    with pytest.raises(GitError): logical_path(value)


@pytest.mark.parametrize('value',['main','codex/../main','codex/x;touch','codex/-bad/','codex/x\n'])
def test_branches_interdites(value):
    with pytest.raises(GitError): branch(value,task_branch=True)


def test_timeout_sorties_bornees_et_masquage():
    with pytest.raises(GitError,match='output_limit'): process([sys.executable,'-c','print("a"*10000)'],limit=1000)
    with pytest.raises(GitError,match='tool_timeout'): process([sys.executable,'-c','import time;time.sleep(1)'],timeout=.1)
    assert 'abcdefgh123456' not in minimize('token=abcdefgh123456')


def test_catalogue_refuse_symlink_filtre_remote_et_image_mutable(git_space,monkeypatch):
    g=git_space;entry=catalog('sample')
    local_git(g['checkout'],'config','filter.attack.smudge','touch /tmp/forbidden')
    from app.git_adapter import check_repo
    with pytest.raises(GitError,match='unsafe_git_config'): check_repo(entry)
    local_git(g['checkout'],'config','--remove-section','filter.attack')
    for update in ({'path':'../secret'},{'remote':'https://user:secret@example.org/repo.git'},
        {'tests':{'unit':{'image':'python:latest','argv':['python']}}}):
        monkeypatch.setenv('MIMIC_GIT_CATALOG',json.dumps({'sample':{**g['entry'],**update}}))
        with pytest.raises(GitError): catalog('sample')


def test_accord_unique_expiration_droits_et_configuration(pg_client,development,monkeypatch):
    from datetime import timedelta
    from app.identity_models import RolePermission
    g=development;change=collect(pg_client,g)
    rejected=operation(pg_client,g,'delete',change_id=change['id'])
    assert post(pg_client,g,'/operations/'+rejected['id']+'/decision',{'decision':'reject','reason':'Conserver ce travail'}).status_code==200
    assert post(pg_client,g,'/operations/'+rejected['id']+'/dispatch',{}).status_code==409
    values={'request_key':str(uuid4()),'change_id':change['id']}
    path=f"/worktrees/{g['tree']}/operations/delete"
    op=post(pg_client,g,path,values).json()
    assert post(pg_client,g,'/operations/'+op['id']+'/decision',{'decision':'approve','reason':'Nettoyage relu'}).status_code==200
    count=len(g['queued']);replay=post(pg_client,g,path,values)
    assert replay.status_code==202 and replay.json()['id']==op['id'] and len(g['queued'])==count
    async def expire(db): (await db.get(GitOperation,op['id'])).expires_at=utcnow()-timedelta(seconds=1)
    asyncio.run(with_db(expire));asyncio.run(run_operation(op['id']))
    assert next(o for o in detail(pg_client,g)['operations'] if o['id']==op['id'])['result']['error']=='approval_expired'
    assert g['path'].exists()
    op=operation(pg_client,g,'delete',change_id=change['id'])
    assert post(pg_client,g,'/operations/'+op['id']+'/decision',{'decision':'approve','reason':'Nettoyage relu'}).status_code==200
    monkeypatch.setenv('MIMIC_GIT_CATALOG',json.dumps({'sample':{**g['entry'],'test_paths':['tests','qa']}}))
    asyncio.run(run_operation(op['id']))
    assert next(o for o in detail(pg_client,g)['operations'] if o['id']==op['id'])['result']['error']=='repository_policy_changed'
    monkeypatch.setenv('MIMIC_GIT_CATALOG',json.dumps({'sample':g['entry']}))
    op=operation(pg_client,g,'delete',change_id=change['id'])
    assert post(pg_client,g,'/operations/'+op['id']+'/decision',{'decision':'approve','reason':'Nettoyage relu'}).status_code==200
    async def revoke(db): await db.delete(await db.get(RolePermission,('project_manager','worktree.delete')))
    asyncio.run(with_db(revoke));asyncio.run(run_operation(op['id']))
    assert next(o for o in detail(pg_client,g)['operations'] if o['id']==op['id'])['result']['error']=='actor_permission_revoked'
    assert g['path'].exists()


def test_depot_alias_physique_et_task_hors_projet(pg_client,development,monkeypatch):
    g=development
    monkeypatch.setenv('MIMIC_GIT_CATALOG',json.dumps({'sample':g['entry'],'alias':g['entry']}))
    admin,email=create_user(admin=True);pg_client.cookies.clear();csrf=login(pg_client,email)
    async def foreign(db):
        row=Project(name='Autre projet',owner_id=admin);db.add(row);await db.flush();return row.id
    foreign_id=asyncio.run(with_db(foreign))
    response=pg_client.post(f'/api/projects/{foreign_id}/git/repositories',headers={'X-CSRF-Token':csrf},json={'catalog_key':'alias'})
    assert response.status_code==409 and response.json()['detail']=='repository_already_attached'
    sign_in(pg_client,g,'manager')
    response=post(pg_client,g,'/worktrees',{**g['create_request'],'request_key':str(uuid4()),'task_id':str(uuid4())})
    assert response.status_code==404


def test_secret_message_commit_et_collecte_changeante(git_space,monkeypatch):
    import app.git_adapter as adapter
    g=git_space;entry=catalog('sample')
    tree=SimpleNamespace(project_id=str(uuid4()),id=str(uuid4()),branch='codex/lecture',base_commit=g['base'])
    adapter.create_tree(entry,tree);path=adapter.tree_path(entry,tree)
    (path/'maths.py').write_text('def add(a,b): return a+b\n')
    local_git(path,'add','.');local_git(path,'commit','-m','Correction\n\napi_key="secret-fictif-message-987654"')
    snapshot=inspect_tree(entry,tree)
    assert snapshot['unsafe'] and 'secret-fictif-message' not in json.dumps(snapshot)
    original=adapter.secure_bytes;changed=False
    def race(root,relative):
        nonlocal changed
        blob=original(root,relative)
        if not changed:
            changed=True;(path/'maths.py').write_text('def add(a,b): return 0\n')
        return blob
    monkeypatch.setattr(adapter,'secure_bytes',race)
    with pytest.raises(GitError,match='workspace_changed_during_collection'): inspect_tree(entry,tree)
    monkeypatch.setattr(adapter,'secure_bytes',original)
    (path/'outside').symlink_to('/etc/passwd')
    with pytest.raises(GitError): adapter.secure_bytes(path,'outside')


def test_pause_bloque_integration_mais_permet_nettoyage(pg_client,development):
    from app.agent_models import FlowRun
    g=development;change=approved_code(pg_client,g)
    pending=operation(pg_client,g,'merge',change_id=change['id'],target='integration')
    assert post(pg_client,g,'/operations/'+pending['id']+'/decision',{'decision':'approve','reason':'Intégration revue'}).status_code==200
    async def pause(db): (await db.get(FlowRun,g['run'])).status='paused'
    asyncio.run(with_db(pause));asyncio.run(run_operation(pending['id']))
    saved=next(o for o in detail(pg_client,g)['operations'] if o['id']==pending['id'])
    assert saved['status']=='failed' and local_git(g['checkout'],'rev-parse','integration')==g['base_commit']
    assert post(pg_client,g,f"/worktrees/{g['tree']}/operations/push",{'request_key':str(uuid4()),'change_id':change['id'],'target':'feature'}).status_code==409
    approve_and_execute(pg_client,g,operation(pg_client,g,'delete',change_id=change['id']))
    assert not g['path'].exists()
