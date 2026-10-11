"""Recette distante opt-in : uniquement un dépôt Gitea privé explicitement provisionné."""
import asyncio
import json
import os
from pathlib import Path
import subprocess
from urllib.parse import urlsplit
from uuid import uuid4

import pytest
from sqlalchemy import select

from app.git_adapter import GitError,catalog,git,remote_commit
from app.git_worker import run_operation
from app.identity_models import AuditEvent
from .conftest import with_db
from .test_agent_conversations import messaging  # noqa: F401 - fixture
from .test_git_development import (development,local_git,approved_code,operation,approve_and_execute,
                                  detail,post,sign_in)

pytestmark=pytest.mark.skipif(
    os.environ.get('MIMIC_TEST_GITEA_WRITES')!='true' or not os.environ.get('MIMIC_TEST_GITEA_REMOTE')
    or not os.environ.get('MIMIC_GIT_SECRET_FORGE'),
    reason='Recette Gitea : dépôt privé de test, opt-in écriture et credential opérateur requis.')


def provision(path,*args):
    """Provisionnement opérateur ; sortie brute/credential jamais dans une assertion."""
    import base64
    token=os.environ['MIMIC_GIT_SECRET_FORGE']
    remote=os.environ['MIMIC_TEST_GITEA_REMOTE']
    env={'PATH':os.defpath,'HOME':os.devnull,'GIT_CONFIG_NOSYSTEM':'1','GIT_CONFIG_GLOBAL':os.devnull,
         'GIT_TERMINAL_PROMPT':'0','GIT_CONFIG_COUNT':'1',
         'GIT_CONFIG_KEY_0':'http.'+remote+'.extraHeader',
         'GIT_CONFIG_VALUE_0':'Authorization: Basic '+base64.b64encode(('x-access-token:'+token).encode()).decode()}
    result=subprocess.run(['git','-c','core.hooksPath='+os.devnull,'-c','credential.helper=',
        '-c','http.followRedirects=false','-C',str(path),*args],env=env,capture_output=True,timeout=30)
    if result.returncode:
        code=next((str(n) for n in (401,403,404) if str(n).encode() in result.stderr),'git_command_failed')
        pytest.fail('Provisionnement Git de recette refusé : '+code+' (diagnostic brut masqué).')
    return result.stdout.decode().strip()


@pytest.fixture
def git_space(tmp_path,monkeypatch):
    remote=os.environ['MIMIC_TEST_GITEA_REMOTE']
    parsed=urlsplit(remote)
    pieces=parsed.path.strip('/').split('/')
    if (parsed.scheme not in ('http','https') or parsed.username or parsed.password or parsed.query or parsed.fragment
        or len(pieces)!=2 or not pieces[1].startswith('mimic-agent15-20-recette-') or not pieces[1].endswith('.git')):
        pytest.fail('Destination explicitement réservée à un dépôt de recette mimic-agent15-20-recette-*.git.')
    repos=tmp_path/'repos';trees=tmp_path/'trees';repos.mkdir();trees.mkdir()
    checkout=repos/'sample.git'
    provision(repos,'clone','--bare',remote,str(checkout))
    original=local_git(checkout,'rev-parse','main')
    local_git(checkout,'config','user.name','Recette MimIC')
    local_git(checkout,'config','user.email','recette@example.org')
    seed=tmp_path/'seed';local_git(checkout,'worktree','add','--detach',str(seed),original)
    (seed/'maths.py').write_text('def add(a,b):\n    return a-b\n')
    local_git(seed,'add','maths.py');local_git(seed,'commit','-m','Départ de recette : addition à corriger')
    base=local_git(seed,'rev-parse','HEAD')
    local_git(checkout,'branch','integration',base)
    local_git(checkout,'worktree','remove',str(seed))
    output=tmp_path/'deployment-proof';adapter=tmp_path/'deploy.py'
    adapter.write_text('#!/usr/bin/env python3\nimport os\nfrom pathlib import Path\nPath('+repr(str(output))+').write_text(os.environ["MIMIC_COMMIT"])\nprint("Déploiement de recette",os.environ["MIMIC_COMMIT"])\n')
    adapter.chmod(0o755)
    entry={'path':'sample.git','remote':remote,'auth_env':'MIMIC_GIT_SECRET_FORGE',
        'tests':{'unit':{'image':os.environ['MIMIC_TEST_IMAGE'],'argv':['python','-m','unittest','discover','-s','tests']}},
        'deployments':{'recette':{'argv':[str(adapter)]}},'test_paths':['tests']}
    monkeypatch.setenv('MIMIC_GIT_ALLOW_HTTP','true')
    monkeypatch.setenv('MIMIC_GIT_ROOT',str(repos));monkeypatch.setenv('MIMIC_WORKTREE_ROOT',str(trees))
    monkeypatch.setenv('MIMIC_GIT_CATALOG',json.dumps({'sample':entry}))
    return {'repos':repos,'trees':trees,'checkout':checkout,'remote':remote,'base':base,'entry':entry,
            'output':output,'original_main':original,'temporary':tmp_path}


def test_gitea_prive_authentification_et_aucun_secret_persiste(git_space,monkeypatch):
    g=git_space;entry=catalog('sample')
    assert remote_commit(entry,'main')==g['original_main']
    with pytest.raises(GitError) as error:
        git(g['checkout'],'ls-remote','--heads',g['remote'])
    assert error.value.code=='git_command_failed'
    with monkeypatch.context() as isolated:
        isolated.setenv('MIMIC_GIT_SECRET_FORGE','credential-invalide-recette-20261009')
        with pytest.raises(GitError) as error:
            remote_commit(entry,'main')
        assert error.value.code=='git_command_failed'
    assert os.environ['MIMIC_GIT_SECRET_FORGE'] not in (g['checkout']/'config').read_text()
    assert 'Authorization' not in (g['checkout']/'config').read_text()


@pytest.mark.skipif(os.environ.get('MIMIC_TEST_GITEA_READ_ONLY')!='true',reason='Jeton de lecture explicite requis.')
def test_gitea_jeton_lecture_refuse_push_sans_modifier_main(git_space):
    g=git_space;entry=catalog('sample')
    # Même SHA que la cible : aucun changement même si le credential était mal qualifié.
    code,out=git(g['checkout'],'push','--porcelain',g['remote'],
                 g['original_main']+':refs/heads/main',entry=entry,ok=False)
    assert code!=0 and b'403' in out
    assert remote_commit(entry,'main')==g['original_main']


def test_gitea_tdd_revue_push_cas_reprise_et_recuperation(pg_client,development,monkeypatch):
    g=development;entry=catalog('sample')
    change=approved_code(pg_client,g);head=change['head_commit']
    target='codex/recette-20261009/'+uuid4().hex
    push=operation(pg_client,g,'push',change_id=change['id'],target=target)
    assert push['status']=='awaiting_approval' and remote_commit(entry,target) is None
    # Accord refusé : pas d'effet distant, même en tentant le dispatch.
    assert post(pg_client,g,'/operations/'+push['id']+'/decision',{'decision':'reject','reason':'Vérifier la cible de recette'}).status_code==200
    assert post(pg_client,g,'/operations/'+push['id']+'/dispatch',{}).status_code==409
    assert remote_commit(entry,target) is None
    # Le lecteur ne peut décider ou déclencher un push.
    pg_client.cookies.clear()
    from .test_identity_acl import login
    g['csrf']=login(pg_client,g['reader_email'])
    assert post(pg_client,g,f"/worktrees/{g['tree']}/operations/push",{'request_key':str(uuid4()),
        'change_id':change['id'],'target':target}).status_code==403
    sign_in(pg_client,g,'manager')
    # Credential retiré puis invalide : erreurs catégorisées, aucune opération ni effet.
    count=len(detail(pg_client,g)['operations'])
    for credential in (None,'credential-invalide-recette-20261009'):
        with monkeypatch.context() as isolated:
            if credential is None: isolated.delenv('MIMIC_GIT_SECRET_FORGE')
            else: isolated.setenv('MIMIC_GIT_SECRET_FORGE',credential)
            refusal=post(pg_client,g,f"/worktrees/{g['tree']}/operations/push",{'request_key':str(uuid4()),
                'change_id':change['id'],'target':target})
            assert refusal.status_code==409
    assert len(detail(pg_client,g)['operations'])==count and remote_commit(entry,target) is None
    # Accord approuvé puis effet worker réel ; rejeu sans second effet.
    merged=approve_and_execute(pg_client,g,operation(pg_client,g,'merge',change_id=change['id'],target='integration'))
    push=operation(pg_client,g,'push',change_id=change['id'],target=target)
    executed=approve_and_execute(pg_client,g,push)
    assert remote_commit(entry,target)==head
    # Une redélivrance après démarrage reste incertaine et se réconcilie sans push.
    async def mark_interrupted(db):
        from app.git_models import GitOperation
        row=await db.get(GitOperation,push['id']);row.status='running'
    asyncio.run(with_db(mark_interrupted));asyncio.run(run_operation(push['id']))
    assert next(o for o in detail(pg_client,g)['operations'] if o['id']==push['id'])['status']=='uncertain'
    assert post(pg_client,g,'/operations/'+push['id']+'/reconcile',{}).status_code==200
    assert remote_commit(entry,target)==head
    # Une branche distante modifiée après accord invalide l'effet du worker.
    moving='codex/recette-20261009/cas-'+uuid4().hex
    provision(g['checkout'],'push',g['remote'],g['base_commit']+':refs/heads/'+moving)
    stale=operation(pg_client,g,'push',change_id=change['id'],target=moving)
    assert post(pg_client,g,'/operations/'+stale['id']+'/decision',{'decision':'approve','reason':'Cible relue avant changement externe'}).status_code==200
    # Provisionnement concurrent explicite, limité à la branche de recette.
    provision(g['checkout'],'push',g['remote'],head+':refs/heads/'+moving)
    asyncio.run(run_operation(stale['id']))
    stale_result=next(o for o in detail(pg_client,g)['operations'] if o['id']==stale['id'])
    assert stale_result['status']=='uncertain' and stale_result['result']['error']=='remote_changed'
    assert remote_commit(entry,moving)==head
    deployed=approve_and_execute(pg_client,g,operation(pg_client,g,'deploy',change_id=change['id'],target='recette'))
    assert g['output'].read_text()==head
    # Récupération réelle depuis Gitea, puis sauvegarde/restauration Git locale.
    restored=g['temporary']/'restored.git'
    provision(g['temporary'],'clone','--bare',g['remote'],str(restored))
    assert local_git(restored,'rev-parse',target)==head
    assert 'return a+b' in local_git(restored,'show',head+':maths.py')
    bundle=g['temporary']/'backup.bundle';local_git(restored,'bundle','create',str(bundle),'--all')
    recovered=g['temporary']/'recovered.git';provision(g['temporary'],'clone','--bare',str(bundle),str(recovered))
    assert local_git(recovered,'rev-parse',target)==head
    removed=approve_and_execute(pg_client,g,operation(pg_client,g,'delete',change_id=change['id']))
    assert not g['path'].exists() and local_git(g['checkout'],'rev-parse',detail(pg_client,g)['worktree']['branch'])==head
    assert remote_commit(entry,'main')==g['original_main']
    async def verify(db):
        events=(await db.scalars(select(AuditEvent).where(AuditEvent.project_id==g['project']))).all()
        assert len([e for e in events if e.action=='git.operation.complete' and e.target_id==push['id']])==1
        assert os.environ['MIMIC_GIT_SECRET_FORGE'] not in json.dumps([e.details for e in events])
    asyncio.run(with_db(verify))
    proof={'remote':g['remote'],'target':target,'cas_target':moving,'head':head,'main':g['original_main'],
        'project':g['project'],'worktree':g['tree'],'workflow':'approved','removed':True,
        'operations':{kind:row['id'] for kind,row in [('merge',merged),('push',executed),('deploy',deployed),('delete',removed)]}}
    destination=os.environ.get('MIMIC_TEST_GITEA_PROOF')
    if destination:
        Path(destination).write_text(json.dumps(proof,ensure_ascii=False,indent=2)+'\n')
