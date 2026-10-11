"""AGENT-21/22 : routage, budgets cumulatifs, concurrence et refus avant fournisseur."""
import asyncio
import json
import subprocess
import sys
from decimal import Decimal
from uuid import uuid4

import pytest
from sqlalchemy import select, text
from app.agent_models import AgentSession, AgentTask, AgentTeamSkill
from app.business_models import DeliverableVersion
from app.generation_service import start_generation, input_messages
from app.identity_models import AuditEvent, ProjectMember, User
from app.identity_service import database_factory
from app.llm_models import LLMCallAttempt, LLMConnectionCheck, LLMProfileVersion
from app.llm_service import LLMResult, LLMInvocationError
from app.llm_trace import traced_invoke
from app.runtime_models import RuntimeSelection, RuntimeUsage
from app import runtime_service as service
from .conftest import ROOT, with_db, create_user
from .test_identity_acl import login
from .test_agent_hierarchy_progress import claim, progress, session
from .test_llm_run_traceability import setup_runtime, delegate


def test_cout_positif_inferieur_au_micro_reste_compte():
    price={'input_per_million':'0.01','output_per_million':'0.01'}
    assert service.budget_cost(price,1,1)==Decimal('.000001')
    assert service.budget_cost(None,1,1) is None
    assert service.budget_cost(price,None,1) is None
    assert service.budget_cost(price,0,0)==0


@pytest.fixture
def configured(pg_client, monkeypatch):
    values=setup_runtime(pg_client,monkeypatch)
    s,_,_,agent,profile,_,_=values
    async def initialize(db):
        db.add(LLMConnectionCheck(profile_version_id=profile,actor_id=s['manager_id'],outcome='success',latency_ms=1))
        architect=await db.scalar(select(User).join(ProjectMember).where(ProjectMember.project_id==s['project'],
                                                                      ProjectMember.role_code=='architect'))
        a=await db.get(AgentSession,agent[0])
        identity=await db.scalar(select(AgentTeamSkill.id).where(AgentTeamSkill.team_id==s['team'],
                                                              AgentTeamSkill.skill_profile_id==a.skill_profile_id))
        return architect.email,identity
    s['architect_email'],s['agent_id']=asyncio.run(with_db(initialize))
    return values


def post(client,s,path,payload,role='manager'):
    csrf=login(client,s[role+'_email'])
    return client.post(s['base']+'/runtimes/'+path,json=payload,headers={'X-CSRF-Token':csrf})


def runtime(client,s,profile=None,**values):
    payload=dict(name='Runtime autorisé',kind='llm' if profile else 'client',profile_version_id=profile,
                 client_code=None if profile else 'codex',reason='Configuration humaine')
    payload.update(values)
    response=post(client,s,'versions',payload,'architect')
    assert response.status_code==201,response.text
    return response.json()


def bind(client,s,row,scope='project',scope_id=None,version=0):
    response=post(client,s,'bindings',dict(scope=scope,scope_id=scope_id or s['project'],
        expected_version=version,runtime_version_id=row['id'] if row else None,reason='Routage autorisé'),'architect')
    assert response.status_code==201,response.text
    return response.json()


def quota(client,s,scope='project',scope_id=None,version=0,**values):
    response=post(client,s,'quotas',dict(scope=scope,scope_id=scope_id or s['project'],expected_version=version,
                                      reason='Limites humaines',**values))
    assert response.status_code==201,response.text
    return response.json()


def usage(client,s,scope='project',scope_id=None):
    return client.get(s['base']+'/runtimes/usage',params={'scope':scope,'scope_id':scope_id or s['project']}).json()


@pytest.mark.parametrize('scope',['project','agent','session','task'])
def test_priorite_selection_figee_et_non_contournable(pg_client,monkeypatch,configured,scope):
    s,_,tid,agent,profile,item,_=configured
    cid=claim(pg_client,s,agent,tid)
    row=runtime(pg_client,s,profile,include_memory=True)
    identities={'project':s['project'],'agent':s['agent_id'],'session':agent[0],'task':tid}
    for level in ('project','agent','session','task'):
        bind(pg_client,s,row,level,identities[level])
        if level==scope: break
    selected=delegate(pg_client,s,tid,agent,cid,item)
    assert selected.status_code==202,selected.text
    assert delegate(pg_client,s,tid,agent,cid,item).json()==selected.json()
    assert delegate(pg_client,s,tid,agent,cid,item,profile_version_id=str(uuid4())).status_code==409
    runtime(pg_client,s,profile,runtime_id=row['runtime_id'],expected_version=1,name='Runtime révisé')
    choices=usage(pg_client,s)['selections']
    assert choices[0]['origin']==scope and choices[0]['runtime_version_id']==row['id']
    from app import worker
    async def invoke(p,messages,**kwargs):
        assert 'reference_memory' in messages[1]['content']
        return LLMResult('# Brouillon',p.id,p.provider,p.model_name,12,8)
    monkeypatch.setattr(worker,'invoke_profile',invoke)
    asyncio.run(worker._generate_document(selected.json()['generation_run_id']))
    actual=usage(pg_client,s)
    assert actual['totals']['tokens']==20 and actual['totals']['active']==0
    assert actual['calls'][0]['policy_ids']==[]
    async def draft(db):
        version=await db.scalar(select(DeliverableVersion).where(DeliverableVersion.generation_run_id==selected.json()['generation_run_id']))
        assert version.status=='Brouillon IA'
    asyncio.run(with_db(draft))


@pytest.mark.parametrize('scope',['project','agent','task','session','model'])
def test_quota_tokens_cinq_perimetres_refus_avant_appel(pg_client,monkeypatch,configured,scope):
    from app import worker
    s,_,tid,agent,profile,item,_=configured
    async def model(db): return service.model_key(await db.get(LLMProfileVersion,profile))
    identities={'project':s['project'],'agent':s['agent_id'],'task':tid,'session':agent[0],'model':asyncio.run(with_db(model))}
    quota(pg_client,s,scope,identities[scope],max_tokens=1)
    cid=claim(pg_client,s,agent,tid)
    result=delegate(pg_client,s,tid,agent,cid,item)
    assert result.status_code==202,result.text
    async def forbidden(*args,**kwargs): pytest.fail('Un appel fournisseur a contourné le quota.')
    monkeypatch.setattr(worker,'invoke_profile',forbidden)
    asyncio.run(worker._generate_document(result.json()['generation_run_id']))
    data=usage(pg_client,s,scope,identities[scope])
    assert data['calls']==[]
    trace=pg_client.get(s['base']+'/llm-runs/generation/'+result.json()['generation_run_id']).json()
    assert trace['error_code']=='quota_tokens' and trace['status']=='failed'
    async def audited(db):
        assert await db.scalar(select(AuditEvent.id).where(AuditEvent.action=='quota.admission',AuditEvent.outcome=='denied'))
    asyncio.run(with_db(audited))


@pytest.mark.parametrize('limit,code',[({'max_seconds':'.5'},'quota_duration'),
    ({'max_cost':'.000001','currency':'EUR'},'quota_cost'),
    ({'max_cost':'1','currency':'USD'},'pricing_unavailable')])
def test_limites_duree_cout_devise(pg_client,monkeypatch,configured,limit,code):
    from app import worker
    s,_,tid,agent,_,item,_=configured
    quota(pg_client,s,**limit)
    result=delegate(pg_client,s,tid,agent,claim(pg_client,s,agent,tid),item)
    async def forbidden(*args,**kwargs): pytest.fail('Refus attendu avant fournisseur.')
    monkeypatch.setattr(worker,'invoke_profile',forbidden)
    rid=result.json()['generation_run_id']
    asyncio.run(worker._generate_document(rid))
    assert pg_client.get(s['base']+'/llm-runs/generation/'+rid).json()['error_code']==code


def test_absence_tarif_metriques_inconnues_et_charges_conservees(pg_client,monkeypatch,configured):
    from app import worker
    s,_,tid,agent,_,item,_=configured
    monkeypatch.setenv('MIMIC_LLM_PRICING_JSON','[]')
    rid=delegate(pg_client,s,tid,agent,claim(pg_client,s,agent,tid),item).json()['generation_run_id']
    async def invoke(p,*args,**kwargs): return LLMResult('Proposition',p.id,p.provider,p.model_name,None,None)
    monkeypatch.setattr(worker,'invoke_profile',invoke)
    asyncio.run(worker._generate_document(rid))
    data=usage(pg_client,s)
    assert data['totals']['tokens'] is None and data['totals']['cost'] is None
    assert data['totals']['charged_tokens']>1000 and data['totals']['charged_cost'] is None
    page=pg_client.get(f"/projects/{s['project']}/agents/runtimes")
    assert page.status_code==200 and 'Non disponible' in page.text
    first=quota(pg_client,s,max_tokens=1)
    second=quota(pg_client,s,version=first['version'],max_tokens=100000)
    assert second['version']==2 and usage(pg_client,s)['totals']['charged_tokens']==data['totals']['charged_tokens']


def test_rbacs_csrf_interprojet_historique_et_formulaires(pg_client,configured):
    s,_,tid,agent,profile,_,_=configured
    payload=dict(name='Sûr',kind='llm',profile_version_id=profile,reason='Décision')
    assert post(pg_client,s,'versions',payload).status_code==403
    csrf=login(pg_client,s['architect_email'])
    assert pg_client.post(s['base']+'/runtimes/versions',json=payload).status_code==403
    row=runtime(pg_client,s,profile)
    from bs4 import BeautifulSoup
    screen=pg_client.get(f"/projects/{s['project']}/agents/runtimes")
    model_option=BeautifulSoup(screen.text,'html.parser').select_one('select[name="filter_scope"] option[value^="model:"]')
    assert model_option is not None
    assert pg_client.get(f"/projects/{s['project']}/agents/runtimes",params={'filter_scope':model_option['value']}).status_code==200
    assert post(pg_client,s,'versions',{**payload,'runtime_id':row['runtime_id'],'expected_version':0},'architect').status_code==409
    assert post(pg_client,s,'bindings',dict(scope='task',scope_id=str(uuid4()),runtime_version_id=row['id'],reason='Interprojet'),'architect').status_code==409
    assert post(pg_client,s,'versions',{**payload,'command':'sh -c danger'},'architect').status_code==422
    assert post(pg_client,s,'quotas',dict(scope='project',scope_id=s['project'],reason='Tarif',max_cost=1)).status_code==422
    assert pg_client.get(s['base']+'/runtimes/usage',params={'scope':'session','scope_id':str(uuid4())}).status_code==404
    _,outsider=create_user();login(pg_client,outsider)
    assert pg_client.get(s['base']+'/runtimes').status_code in (403,404)
    assert pg_client.get(f"/projects/{s['project']}/agents/runtimes").status_code in (403,404)
    login(pg_client,s['reader_email'])
    page=pg_client.get(f"/projects/{s['project']}/agents/runtimes")
    assert page.status_code==200 and 'name="action"' not in page.text
    assert post(pg_client,s,'quotas',dict(scope='project',scope_id=s['project'],reason='Refus',max_tokens=1),'reader').status_code==403
    csrf=login(pg_client,s['architect_email'])
    bad=pg_client.post(f"/projects/{s['project']}/agents/runtimes",data=dict(action='runtime',csrf_token=csrf,name='<nom conservé>',kind='llm',
        timeout_seconds='999',reason='Configuration humaine'))
    assert bad.status_code==409 and '&lt;nom conservé&gt;' in bad.text and 'role="alert"' in bad.text
    assert pg_client.post(f"/projects/{s['project']}/agents/runtimes",data=dict(action='runtime')).status_code==403


def test_runtime_desactive_et_retrait_destination(pg_client,monkeypatch,configured):
    from app import worker
    s,_,tid,agent,profile,item,_=configured
    row=runtime(pg_client,s,profile);bind(pg_client,s,row)
    rid=delegate(pg_client,s,tid,agent,claim(pg_client,s,agent,tid),item).json()['generation_run_id']
    runtime(pg_client,s,profile,runtime_id=row['runtime_id'],expected_version=1,enabled=False)
    async def forbidden(*args,**kwargs): pytest.fail('Runtime désactivé.')
    monkeypatch.setattr(worker,'invoke_profile',forbidden)
    asyncio.run(worker._generate_document(rid))
    assert usage(pg_client,s)['calls']==[]
    monkeypatch.setenv('MIMIC_LLM_ALLOWED_BASE_URLS','')
    assert post(pg_client,s,'versions',dict(name='Interdit',kind='llm',profile_version_id=profile,reason='Contrôle'),'architect').status_code==409


def test_client_capacite_compatibilite_duree_et_tokens_inconnus(pg_client,configured):
    s,_,tid,agent,_,_,_=configured
    row=runtime(pg_client,s);bind(pg_client,s,row)
    cid=claim(pg_client,s,agent,tid)
    assert progress(pg_client,s,agent,tid,cid,100).status_code==409
    route=s['base']+'/agent-tasks/'+tid+'/runtime'
    assert pg_client.post(route,params={'session_id':agent[0]},headers=agent[1],json={'claim_id':cid}).status_code==403
    # La capacité n'élargit que les sessions nouvellement négociées.
    csrf=login(pg_client,s['manager_email']);s['csrf']=csrf
    assert pg_client.post(s['base']+'/agent-tasks/'+tid+'/release',params={'session_id':agent[0]},headers=agent[1]).status_code==200
    agent=session(pg_client,s,['session.heartbeat','task.claim','task.runtime','task.release']);cid=claim(pg_client,s,agent,tid)
    begin=pg_client.post(route,params={'session_id':agent[0]},headers=agent[1],json={'claim_id':cid})
    assert begin.status_code==202,begin.text
    assert pg_client.post(route,params={'session_id':agent[0]},headers=agent[1],json={'claim_id':cid}).json()==begin.json()
    assert progress(pg_client,s,agent,tid,cid,100).status_code==200
    data=usage(pg_client,s)
    assert data['totals']['active']==0 and data['totals']['tokens'] is None and data['totals']['cost'] is None
    assert Decimal(data['calls'][0]['actual_seconds'])>0


@pytest.mark.parametrize('limit',[{'max_tokens':100000},{'max_cost':1,'currency':'EUR'}])
def test_client_non_mesure_refuse_quotas_financiers(pg_client,configured,limit):
    s,_,tid,_,_,_,_=configured
    row=runtime(pg_client,s);bind(pg_client,s,row);quota(pg_client,s,**limit)
    s['csrf']=login(pg_client,s['manager_email'])
    agent=session(pg_client,s,['session.heartbeat','task.claim','task.runtime'])
    cid=claim(pg_client,s,agent,tid)
    assert pg_client.post(s['base']+'/agent-tasks/'+tid+'/runtime',params={'session_id':agent[0]},headers=agent[1],json={'claim_id':cid}).status_code==409
    assert usage(pg_client,s)['calls']==[]


def test_course_concurrence_retries_et_cloture_incertitude(pg_client,monkeypatch,configured):
    s,_,_,_,profile,item,_=configured
    quota(pg_client,s,max_concurrency=1,max_tokens=100000)
    async def scenario():
        factory=database_factory()
        try:
            async with factory() as db:
                p=await db.get(LLMProfileVersion,profile);actor=await db.get(User,s['manager_id'])
                runs=[await start_generation(db,project_id=s['project'],actor=actor,kind='technical_doc',style='concise',item_ids=[item],profile=p) for _ in range(2)]
                await db.commit()
            entered=asyncio.Event();resume=asyncio.Event();seen=[]
            async def invoke(p,messages,**kwargs):
                seen.append(p.id);entered.set();await resume.wait()
                return LLMResult('Proposition',p.id,p.provider,p.model_name,10,20)
            first=asyncio.create_task(traced_invoke(factory,runs[0],p,input_messages(runs[0]),invoke=invoke))
            await asyncio.wait_for(entered.wait(),5)
            try:
                with pytest.raises(LLMInvocationError,match='quota_concurrency'):
                    await traced_invoke(factory,runs[1],p,input_messages(runs[1]),invoke=invoke)
                async with factory() as db:
                    row=await db.scalar(select(RuntimeUsage).where(RuntimeUsage.status=='active'))
                    retained=row.charged_tokens
                    with pytest.raises(service.RuntimeDenied,match='stop_confirmation_required'):
                        await service.reconcile(db,s['project'],row.id,actor,'Arrêt vérifié',False)
                    await service.reconcile(db,s['project'],row.id,actor,'Arrêt vérifié',True)
                    await db.commit()
                resume.set()
                with pytest.raises(LLMInvocationError,match='execution_unknown'): await first
                async with factory() as db:
                    row=await db.get(RuntimeUsage,row.id)
                    assert row.status=='unknown' and row.charged_tokens==retained and row.actual_tokens is None
                    total=await service.totals(db,s['project'],'project',s['project'])
                    assert total['active']==0 and total['tokens'] is None
                assert seen==[profile]
            finally:
                resume.set();await asyncio.gather(first,return_exceptions=True)
        finally: await factory.kw['bind'].dispose()
    asyncio.run(scenario())


def test_retry_reserve_de_nouveau_et_timeout_borne(pg_client,monkeypatch,configured):
    from app import worker
    s,_,tid,agent,profile,item,_=configured
    async def retry(db): (await db.get(LLMProfileVersion,profile)).max_retries=1
    asyncio.run(with_db(retry))
    row=runtime(pg_client,s,profile,timeout_seconds=3);bind(pg_client,s,row)
    quota(pg_client,s,max_tokens=100000,max_seconds=10)
    rid=delegate(pg_client,s,tid,agent,claim(pg_client,s,agent,tid),item).json()['generation_run_id']
    seen=[]
    async def invoke(p,*args,**kwargs):
        seen.append(kwargs['timeout_limit'])
        if len(seen)==1: raise LLMInvocationError('timeout',input_tokens=10,output_tokens=0)
        return LLMResult('Proposition',p.id,p.provider,p.model_name,10,20)
    monkeypatch.setattr(worker,'invoke_profile',invoke)
    asyncio.run(worker._generate_document(rid))
    data=usage(pg_client,s)
    assert len(data['calls'])==2 and data['totals']['tokens']==40
    assert all(1<=n<=3 for n in seen)


def test_migration_peuplee_preserve_traces_et_sessions(pg_client,monkeypatch,configured):
    from app import worker
    s,_,tid,agent,profile,item,_=configured
    row=runtime(pg_client,s,profile);bind(pg_client,s,row);quota(pg_client,s,max_tokens=100000)
    rid=delegate(pg_client,s,tid,agent,claim(pg_client,s,agent,tid),item).json()['generation_run_id']
    async def invoke(p,*args,**kwargs): return LLMResult('Brouillon',p.id,p.provider,p.model_name,10,20)
    monkeypatch.setattr(worker,'invoke_profile',invoke)
    asyncio.run(worker._generate_document(rid))
    command=[sys.executable,'-m','alembic']
    try:
        subprocess.run([*command,'downgrade','0020_git_development'],cwd=ROOT,check=True)
        async def check(db):
            assert await db.scalar(text('SELECT count(*) FROM generation_run WHERE id=:id'),{'id':rid})==1
            assert await db.scalar(text('SELECT count(*) FROM llm_call_attempt WHERE generation_run_id=:id'),{'id':rid})==1
            assert await db.scalar(text('SELECT count(*) FROM agent_session WHERE id=:id'),{'id':agent[0]})==1
        asyncio.run(with_db(check))
    finally: subprocess.run([*command,'upgrade','head'],cwd=ROOT,check=True)
    assert usage(pg_client,s)['calls']==[]


@pytest.mark.parametrize('revoked',[False,True])
def test_memoire_validee_bornee_et_partage_revoque(pg_client,monkeypatch,configured,revoked):
    from app import worker
    from app.memory_service import create_memory,decide_memory,grant_memory
    from app.memory_models import ProjectMemoryGrant
    from app.identity_models import utcnow
    from .test_project_memory import fields
    s,_,tid,agent,profile,item,_=configured
    async def memories(db):
        local=await create_memory(db,s['project'],'convention',fields(content='Convention locale autorisée'),s['manager_id'])
        await decide_memory(db,local.id,s['project'],1,'approve',s['manager_id'])
        await create_memory(db,s['project'],'result',fields(content='Proposition non validée exclue'),s['manager_id'])
        global_=await create_memory(db,None,'convention',fields(content='Convention globale partagée'),s['manager_id'])
        await decide_memory(db,global_.id,None,1,'approve',s['manager_id'],scope='global')
        grant,_,_=await grant_memory(db,global_.id,s['project'],2,s['manager_id'])
        return grant.id
    grant_id=asyncio.run(with_db(memories))
    row=runtime(pg_client,s,profile,include_memory=True);bind(pg_client,s,row)
    rid=delegate(pg_client,s,tid,agent,claim(pg_client,s,agent,tid),item).json()['generation_run_id']
    async def invoke(p,messages,**kwargs):
        assert not revoked
        assert 'Convention locale autorisée' in messages[1]['content'] and 'Convention globale partagée' in messages[1]['content']
        assert 'Proposition non validée exclue' not in messages[1]['content']
        return LLMResult('Brouillon',p.id,p.provider,p.model_name,10,20)
    monkeypatch.setattr(worker,'invoke_profile',invoke)
    if revoked:
        async def revoke(db):
            grant=await db.get(ProjectMemoryGrant,grant_id)
            grant.revoked_at,grant.revoked_by=utcnow(),s['manager_id']
        asyncio.run(with_db(revoke))
    asyncio.run(worker._generate_document(rid))
    trace=pg_client.get(s['base']+'/llm-runs/generation/'+rid).json()
    assert trace['status']==('failed' if revoked else 'completed')
    if revoked:
        assert trace['calls']==[] and trace['messages_unavailable']
        assert 'Convention globale partagée' not in json.dumps(trace)
    # L'API de consommation ne restitue aucun contenu de mémoire ou prompt.
    assert 'Convention locale autorisée' not in json.dumps(usage(pg_client,s))


def test_tarif_absent_interdit_admission_financiere(pg_client,monkeypatch,configured):
    from app import worker
    s,_,tid,agent,_,item,_=configured
    monkeypatch.setenv('MIMIC_LLM_PRICING_JSON','[]')
    quota(pg_client,s,max_cost=1,currency='EUR')
    rid=delegate(pg_client,s,tid,agent,claim(pg_client,s,agent,tid),item).json()['generation_run_id']
    async def forbidden(*args,**kwargs): pytest.fail('Tarif indispensable au quota financier.')
    monkeypatch.setattr(worker,'invoke_profile',forbidden)
    asyncio.run(worker._generate_document(rid))
    assert pg_client.get(s['base']+'/llm-runs/generation/'+rid).json()['error_code']=='pricing_unavailable'


def test_depassement_fournisseur_conserve_et_nouvelle_version_ne_reset_pas(pg_client,monkeypatch,configured):
    from app import worker
    s,_,tid,agent,profile,item,_=configured
    quota(pg_client,s,max_tokens=100000)
    rid=delegate(pg_client,s,tid,agent,claim(pg_client,s,agent,tid),item).json()['generation_run_id']
    async def invoke(p,*args,**kwargs): return LLMResult('Brouillon',p.id,p.provider,p.model_name,150000,20)
    monkeypatch.setattr(worker,'invoke_profile',invoke)
    asyncio.run(worker._generate_document(rid))
    assert usage(pg_client,s)['totals']['tokens']==150020
    async def reject(db):
        p=await db.get(LLMProfileVersion,profile)
        run=await start_generation(db,project_id=s['project'],actor=await db.get(User,s['manager_id']),
                                  kind='technical_doc',style='concise',item_ids=[item],profile=p)
        call=LLMCallAttempt(generation_run_id=run.id,profile_version_id=p.id,attempt_number=1,messages=[],parameters={},status='running')
        db.add(call);await db.flush()
        with pytest.raises(service.RuntimeDenied,match='quota_tokens'):
            await service.reserve_call(db,run,p,call,input_messages(run))
        assert service.model_key(p)==service.model_key(type('P',(),{'provider':p.provider,'model_name':p.model_name,'version':999})())
    asyncio.run(with_db(reject))


def test_deadline_timeout_reel_et_client_incompatible(pg_client,monkeypatch,configured):
    from app import worker
    s,_,tid,agent,profile,item,_=configured
    row=runtime(pg_client,s,profile,timeout_seconds=3);bind(pg_client,s,row)
    cid=claim(pg_client,s,agent,tid)
    rid=delegate(pg_client,s,tid,agent,cid,item).json()['generation_run_id']
    async def invoke(*args,**kwargs): await asyncio.sleep(5);pytest.fail('Timeout non appliqué.')
    monkeypatch.setattr(worker,'invoke_profile',invoke)
    asyncio.run(worker._generate_document(rid))
    assert pg_client.get(s['base']+'/llm-runs/generation/'+rid).json()['status']=='failed'
    data=usage(pg_client,s)
    assert data['totals']['active']==0 and len(data['calls'])==1
    assert Decimal(data['calls'][0]['actual_seconds'])<3


def test_client_modele_inconnu_et_changement_mode_refuses(pg_client,configured):
    s,_,tid,_,profile,item,_=configured
    s['csrf']=login(pg_client,s['manager_email'])
    agent=session(pg_client,s,['session.heartbeat','task.claim','task.runtime'])
    cid=claim(pg_client,s,agent,tid)
    row=runtime(pg_client,s,profile);bind(pg_client,s,row)
    rid=delegate(pg_client,s,tid,agent,cid,item).json()['generation_run_id']
    route=s['base']+'/agent-tasks/'+tid+'/runtime'
    assert pg_client.post(route,params={'session_id':agent[0]},headers=agent[1],json={'claim_id':cid}).status_code==409
    assert usage(pg_client,s)['calls']==[]


def test_client_long_renouvelable_sans_etendre_la_deadline(pg_client,configured):
    from app.agent_models import AgentTaskClaim
    from app.identity_models import utcnow
    from datetime import timedelta
    s,_,tid,_,_,_,_=configured
    csrf=login(pg_client,s['architect_email'])
    created=pg_client.post(f"/projects/{s['project']}/agents/runtimes",data=dict(action='runtime',csrf_token=csrf,
        name='Client long de recette',kind='client',client_code='codex',profile_version_id='',enabled='on',
        timeout_seconds='600',reason='Exécution client supervisée'))
    assert created.status_code==303,created.text
    row=runtime(pg_client,s,timeout_seconds=600);bind(pg_client,s,row)
    s['csrf']=login(pg_client,s['manager_email'])
    agent=session(pg_client,s,['session.heartbeat','task.claim','task.runtime'])
    cid=claim(pg_client,s,agent,tid)
    response=pg_client.post(s['base']+'/agent-tasks/'+tid+'/runtime',params={'session_id':agent[0]},headers=agent[1],json={'claim_id':cid})
    assert response.status_code==202,response.text
    async def inspect(db):
        chosen=await db.get(RuntimeSelection,response.json()['selection_id'])
        task=await db.get(AgentTask,tid)
        assert (chosen.deadline-utcnow()).total_seconds()>500
        assert task.lease_expires_at<chosen.deadline
        task.lease_expires_at=utcnow()+timedelta(seconds=10)
        (await db.get(AgentTaskClaim,cid)).lease_expires_at=task.lease_expires_at
        return chosen.deadline
    deadline=asyncio.run(with_db(inspect))
    assert pg_client.post(s['base']+'/agent-sessions/'+agent[0]+'/heartbeat',headers=agent[1]).status_code==200
    async def renewed(db):
        assert (await db.get(RuntimeSelection,response.json()['selection_id'])).deadline==deadline
        assert (await db.get(AgentTask,tid)).lease_expires_at>utcnow()+timedelta(seconds=90)
    asyncio.run(with_db(renewed))
    assert progress(pg_client,s,agent,tid,cid,100).status_code==200


@pytest.mark.parametrize('mode',['incompatible','model_quota'])
def test_client_incompatible_ou_modele_non_verifiable(pg_client,configured,mode):
    s,_,tid,_,profile,_,_=configured
    row=runtime(pg_client,s,client_code='cursor' if mode=='incompatible' else 'codex');bind(pg_client,s,row)
    if mode=='model_quota':
        async def key(db): return service.model_key(await db.get(LLMProfileVersion,profile))
        quota(pg_client,s,'model',asyncio.run(with_db(key)),max_concurrency=1)
    s['csrf']=login(pg_client,s['manager_email'])
    agent=session(pg_client,s,['session.heartbeat','task.claim','task.runtime'])
    cid=claim(pg_client,s,agent,tid)
    response=pg_client.post(s['base']+'/agent-tasks/'+tid+'/runtime',params={'session_id':agent[0]},headers=agent[1],json={'claim_id':cid})
    assert response.status_code==409
    assert usage(pg_client,s)['calls']==[] and usage(pg_client,s)['selections']==[]


def test_interruption_agent_requiert_reconciliation_et_retry_humain(pg_client,monkeypatch,configured):
    from app import worker
    from app.agent_models import AgentTaskClaim,AgentTaskRun
    from app.agent_routing import reserve_task
    from app.agent_coordinator import retry_task
    from app.identity_models import utcnow
    from datetime import timedelta
    s,_,tid,agent,_,item,_=configured
    cid=claim(pg_client,s,agent,tid)
    rid=delegate(pg_client,s,tid,agent,cid,item).json()['generation_run_id']
    async def scenario():
        factory=database_factory();entered=asyncio.Event();resume=asyncio.Event()
        async def invoke(p,*args,**kwargs):
            entered.set();await resume.wait()
            return LLMResult('Retour tardif ignoré',p.id,p.provider,p.model_name,10,20)
        monkeypatch.setattr(worker,'invoke_profile',invoke)
        job=asyncio.create_task(worker._generate_document(rid))
        try:
            await asyncio.wait_for(entered.wait(),5)
            async with factory() as db:
                task=await db.get(AgentTask,tid)
                claim_=await db.get(AgentTaskClaim,cid)
                task.lease_expires_at=claim_.lease_expires_at=utcnow()-timedelta(seconds=1)
                await db.commit()
            async with factory() as db:
                a=await db.get(AgentSession,agent[0])
                with pytest.raises(service.RuntimeDenied,match='runtime_recovery_required'):
                    await reserve_task(db,session=a,task_id=tid,request_key=str(uuid4()))
                # Refus de claim conserve l'expiration auditable, sans nouvelle tentative.
                await db.commit()
            async with factory() as db:
                row=await db.scalar(select(RuntimeUsage).where(RuntimeUsage.status=='active'))
                manager=await db.get(User,s['manager_id'])
                await service.reconcile(db,s['project'],row.id,manager,'Worker arrêté',True)
                task=await db.get(AgentTask,tid)
                assert task.status=='failed'
                attempt=await db.scalar(select(AgentTaskRun).where(AgentTaskRun.claim_id==cid))
                assert (await db.get(GenerationRun,rid)).status=='failed'
                await db.commit()
            resume.set();await asyncio.wait_for(job,5)
            async with factory() as db:
                with pytest.raises(service.RuntimeDenied,match='runtime_recovery_required'):
                    await service.ensure_task_recoverable(db,tid)
                await retry_task(db,s['project'],tid,attempt.id,manager.id)
                (await db.get(AgentTask,tid)).retry_not_before=utcnow()-timedelta(seconds=1)
                await db.commit()
            async with factory() as db:
                await service.ensure_task_recoverable(db,tid)
                await reserve_task(db,session=await db.get(AgentSession,agent[0]),task_id=tid,request_key=str(uuid4()))
                await db.commit()
                assert await db.scalar(select(DeliverableVersion.id).where(DeliverableVersion.generation_run_id==rid)) is None
        finally:
            resume.set();await asyncio.gather(job,return_exceptions=True);await factory.kw['bind'].dispose()
    from app.business_models import GenerationRun
    asyncio.run(asyncio.wait_for(scenario(),15))
