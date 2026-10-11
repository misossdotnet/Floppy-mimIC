"""AGENT-23/25 : budgets périodiques, portes et projections sur PostgreSQL."""
import asyncio
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from uuid import uuid4
import pytest
from sqlalchemy import func, select
from app import budget_service as service
from app.agent_models import AgentPlanVersion, AgentTask
from app.budget_api import BudgetInput
from app.budget_models import ExecutionBudget, BudgetAlert, BudgetSuspension
from app.identity_models import AuditEvent, Project, User, utcnow
from app.llm_models import LLMProfileVersion, LLMCallAttempt
from app.llm_service import LLMResult
from app.runtime_models import RuntimeUsage
from app.runtime_service import RuntimeDenied, reserve
from .conftest import with_db
from .test_identity_acl import login
from .test_runtime_quotas import configured, runtime, bind
from .test_agent_hierarchy_progress import claim, session, progress
from .test_llm_run_traceability import delegate


def save(client,s,**values):
    csrf=login(client,s['manager_email'])
    data=dict(
        scope='project',scope_id=s['project'],period='daily',metric='tokens',amount=100000,
        warning_percent=80,reason='Budget de recette')
    data.update(values)
    return client.post(s['base']+'/budgets/versions',json=data,headers={'X-CSRF-Token':csrf})


def read(client,s):
    r=client.get(s['base']+'/budgets');assert r.status_code==200,r.text
    return r.json()


def action(client,s,path):
    csrf=login(client,s['manager_email'])
    return client.post(s['base']+'/budgets/'+path,json={'reason':'Décision humaine de recette'},headers={'X-CSRF-Token':csrf})


def run(client,monkeypatch,values,tokens):
    from app import worker
    s,_,tid,agent,_,item,_=values
    cid=claim(client,s,agent,tid)
    r=delegate(client,s,tid,agent,cid,item);assert r.status_code==202,r.text
    async def invoke(p,*args,**kwargs): return LLMResult('Brouillon contrôlé',p.id,p.provider,p.model_name,tokens,0)
    monkeypatch.setattr(worker,'invoke_profile',invoke)
    asyncio.run(worker._generate_document(r.json()['generation_run_id']))
    return r.json()['generation_run_id']


@pytest.mark.parametrize('period,instant,start,end',[
    ('daily','2026-10-25T23:30:00+01:00','2026-10-25T00:00:00+00:00','2026-10-26T00:00:00+00:00'),
    ('weekly','2027-01-01T12:00:00+00:00','2026-12-28T00:00:00+00:00','2027-01-04T00:00:00+00:00'),
    ('monthly','2026-12-31T23:59:59+00:00','2026-12-01T00:00:00+00:00','2027-01-01T00:00:00+00:00'),
    ('monthly','2028-02-29T00:00:00+00:00','2028-02-01T00:00:00+00:00','2028-03-01T00:00:00+00:00')])
def test_bornes_utc(period,instant,start,end):
    assert service.period_bounds(period,datetime.fromisoformat(instant))==(datetime.fromisoformat(start),datetime.fromisoformat(end))


@pytest.mark.parametrize('values',[{'amount':0},{'amount':'NaN'},{'warning_percent':100},{'warning_percent':True},
    {'metric':'tokens','amount':'1.2'},{'metric':'cost'},{'currency':'EUR'},{'period':'yearly'}])
def test_schema_strict(values):
    data=dict(scope='project',scope_id=str(uuid4()),period='daily',metric='tokens',amount=10,reason='Test')
    data.update(values)
    with pytest.raises(ValueError): BudgetInput.model_validate(data)


def test_versions_refus_securite_et_formulaires(pg_client,configured):
    s,*_=configured
    b=save(pg_client,s);assert b.status_code==201,b.text
    assert save(pg_client,s).status_code==409
    assert save(pg_client,s,expected_version=1,amount=200000).json()['version']==2
    csrf=login(pg_client,s['reader_email'])
    route=s['base']+'/budgets/versions'
    payload=dict(scope='project',scope_id=s['project'],period='daily',metric='tokens',amount=1,reason='Test')
    assert pg_client.post(route,json=payload,headers={'X-CSRF-Token':csrf}).status_code==403
    assert pg_client.get(s['base']+'/budgets').headers['cache-control']=='no-store'
    assert 'Versionner le budget' not in pg_client.get(f"/projects/{s['project']}/agents/runtimes").text
    login(pg_client,s['manager_email'])
    assert pg_client.post(route,json=payload).status_code==403
    assert pg_client.get(f'/api/projects/{uuid4()}/budgets').status_code==404
    csrf=login(pg_client,s['manager_email'])
    page=pg_client.post(f"/projects/{s['project']}/agents/runtimes",data=dict(action='budget',csrf_token=csrf,
        scope_option=f"project:{s['project']}:2",period='monthly',metric='seconds',amount='300',warning_percent='80',
        enabled='on',reason='Budget mensuel'))
    assert page.status_code==303,page.text
    assert 'Budgets périodiques' in pg_client.get(page.headers['location']).text
    async def history(db):
        assert await db.scalar(select(func.count()).select_from(ExecutionBudget))==3
        (await db.get(Project,s['project'])).status='archived'
    asyncio.run(with_db(history))
    assert save(pg_client,s,expected_version=3).status_code==409


@pytest.mark.parametrize('scope',['project','agent','session','task','model'])
def test_admission_cinq_perimetres_et_erreur_auditee(pg_client,monkeypatch,configured,scope):
    s,_,tid,agent,profile,item,_=configured
    async def model(db): return service.view(await db.get(LLMProfileVersion,profile))
    from app.runtime_service import model_key
    from types import SimpleNamespace
    key=model_key(SimpleNamespace(**asyncio.run(with_db(model))))
    ids={'project':s['project'],'agent':s['agent_id'],'session':agent[0],'task':tid,'model':key}
    assert save(pg_client,s,scope=scope,scope_id=ids[scope],amount=1).status_code==201
    rid=run(pg_client,monkeypatch,configured,10)
    trace=pg_client.get(s['base']+'/llm-runs/generation/'+rid).json()
    assert trace['error_code']=='budget_exhausted' and trace['status']=='failed'
    # La trace d'admission minimisée est persistée par llm_trace.
    async def usage(db):
        assert await db.scalar(select(AuditEvent.id).where(AuditEvent.action=='quota.admission',AuditEvent.outcome=='denied'))
        return await db.scalar(select(func.count()).select_from(RuntimeUsage))
    assert asyncio.run(with_db(usage))==0


@pytest.mark.parametrize('mode,sensitive,expected',[
    ('bounded',False,0),('sensitive',False,0),('sensitive',True,1),('per_action',False,1)])
def test_seuil_selon_autonomie_et_acquittement_distinct(pg_client,monkeypatch,configured,mode,sensitive,expected):
    s,pid,tid,*_=configured
    # La consommation est obtenue par un vrai worker simulé, avant activation du seuil.
    rid=run(pg_client,monkeypatch,configured,85000)
    async def policy(db):
        (await db.get(AgentPlanVersion,pid)).autonomy_mode=mode
        if sensitive: (await db.get(AgentTask,tid)).action_class='sensitive'
    asyncio.run(with_db(policy))
    assert save(pg_client,s).status_code==201
    data=read(pg_client,s);assert len(data['alerts'])==1 and data['alerts'][0]['level']=='warning'
    assert data['suspended']==expected
    aid=data['alerts'][0]['id']
    assert action(pg_client,s,f'alerts/{aid}/acknowledge').status_code==200
    assert action(pg_client,s,f'alerts/{aid}/acknowledge').status_code==200
    assert read(pg_client,s)['suspended']==expected
    if expected:
        hold=data['suspensions'][0]['id']
        assert action(pg_client,s,f'suspensions/{hold}/resume').status_code==409
        assert save(pg_client,s,expected_version=1,amount=200000).status_code==201
        assert action(pg_client,s,f'suspensions/{hold}/resume').status_code==200
        assert action(pg_client,s,f'suspensions/{hold}/resume').status_code==200
        assert read(pg_client,s)['suspended']==0
    async def audit(db):
        assert await db.scalar(select(func.count()).select_from(AuditEvent).where(
            AuditEvent.action=='budget.acknowledge',AuditEvent.target_id==aid))==1
    asyncio.run(with_db(audit))


def test_depassement_suspend_et_retour_admis_reste_brouillon(pg_client,monkeypatch,configured):
    s,pid,tid,*_=configured
    assert save(pg_client,s).status_code==201
    rid=run(pg_client,monkeypatch,configured,150000)
    data=read(pg_client,s)
    assert data['suspended']==1 and Decimal(str(data['budgets'][0]['charged']))==150000
    assert pg_client.get(s['base']+'/llm-runs/generation/'+rid).json()['status']=='completed'
    async def gate(db):
        with pytest.raises(RuntimeDenied,match='budget_suspended'):
            await service.guard_plan(db,await db.get(AgentPlanVersion,pid))
    asyncio.run(with_db(gate))
    assert data['alerts'][0]['level']=='exhausted'
    assert save(pg_client,s,expected_version=1,amount=200000).status_code==201
    assert Decimal(str(read(pg_client,s)['budgets'][0]['charged']))==150000
    assert read(pg_client,s)['suspended']==1


def test_lecture_pilotage_sans_effet_et_filtre(pg_client,monkeypatch,configured):
    s,*_=configured
    run(pg_client,monkeypatch,configured,150000)
    assert save(pg_client,s).status_code==201
    async def state(db):
        return (
            await db.scalar(select(func.count()).select_from(AuditEvent)),
            await db.scalar(select(func.count()).select_from(BudgetSuspension)))
    before=asyncio.run(with_db(state))
    data=pg_client.get(s['base']+'/pilotage',params={'kind':'budget_alerts'}).json()
    assert data['availability']['budgets']=='available'
    assert data['counters']['budget_alerts']==1
    assert data['activities'][0]['kind']=='budget_alerts'
    assert 'Brouillon contrôlé' not in str(data)
    assert pg_client.get(f"/projects/{s['project']}/pilotage").status_code==200
    read(pg_client,s)
    assert asyncio.run(with_db(state))==before


@pytest.mark.parametrize('metric,extra,code',[
    ('tokens',{},'budget_metrics_unavailable'),('cost',{'currency':'EUR'},'budget_metrics_unavailable'),
    ('seconds',{},None)])
def test_client_non_mesurable_et_duree(pg_client,configured,metric,extra,code):
    s,_,tid,_,_,_,_=configured
    row=runtime(pg_client,s);bind(pg_client,s,row)
    assert save(pg_client,s,metric=metric,amount=300,**extra).status_code==201
    s['csrf']=login(pg_client,s['manager_email'])
    a=session(pg_client,s,['session.heartbeat','task.claim','task.runtime'])
    cid=claim(pg_client,s,a,tid)
    r=pg_client.post(s['base']+'/agent-tasks/'+tid+'/runtime',params={'session_id':a[0]},headers=a[1],json={'claim_id':cid})
    assert r.status_code==(409 if code else 202),r.text
    if not code:
        assert progress(pg_client,s,a,tid,cid,100).status_code==200


def test_concurrence_evaluation_idempotente_et_periode_suivante(pg_client,monkeypatch,configured):
    s,pid,*_=configured
    run(pg_client,monkeypatch,configured,90000)
    assert save(pg_client,s).status_code==201
    from app.identity_service import database_factory
    async def scenario():
        factory=database_factory()
        async def evaluate():
            async with factory() as db:
                await service.evaluate(db,s['project']);await db.commit()
        try:
            await asyncio.gather(evaluate(),evaluate())
            async with factory() as db:
                assert await db.scalar(select(func.count()).select_from(BudgetAlert))==1
                b=await db.scalar(select(ExecutionBudget).where(ExecutionBudget.is_current.is_(True)))
                next_day=service.period_bounds('daily',utcnow())[1]
                assert (await service.consumption(db,b,next_day))['charged']==0
        finally: await factory.kw['bind'].dispose()
    asyncio.run(scenario())


def test_admissions_concurrentes_plafond_atomique(pg_client,configured):
    s,_,_,_,profile,*_=configured
    budget=save(pg_client,s,amount=100).json()
    from app.identity_service import database_factory
    async def scenario():
        factory=database_factory()
        async def admit():
            async with factory() as db:
                call=LLMCallAttempt(profile_version_id=profile,attempt_number=1,messages=[],parameters={},status='running')
                # Une trace a toujours un parent documentaire exact.
                from app.generation_service import start_generation
                from app.business_models import ContextItem
                item=await db.scalar(select(ContextItem).where(ContextItem.project_id==s['project']))
                run_=await start_generation(db,project_id=s['project'],actor=await db.get(User,s['manager_id']),
                    kind='technical_doc',style='concise',item_ids=[item.id],profile=await db.get(LLMProfileVersion,profile))
                call.generation_run_id=run_.id
                db.add(call);await db.flush()
                try:
                    row,_=await reserve(db,s['project'],{'project':s['project']},call=call,input_bound=60,timeout=1)
                    assert budget['id'] in row.policy_ids
                    await db.commit();return 'accepted'
                except RuntimeDenied as exc:
                    await db.rollback();return exc.code
        try:
            assert sorted(await asyncio.gather(admit(),admit()))==['accepted','budget_exhausted']
        finally: await factory.kw['bind'].dispose()
    asyncio.run(scenario())


def test_periode_debut_charge_inconnue_devise_et_bornes(pg_client,monkeypatch,configured):
    s,*_=configured
    run(pg_client,monkeypatch,configured,200)
    b=save(pg_client,s).json()
    async def scenario(db):
        row=await db.scalar(select(RuntimeUsage).where(RuntimeUsage.project_id==s['project']))
        budget=await db.get(ExecutionBudget,b['id'])
        start,end=service.period_bounds('daily',utcnow())
        row.started_at=start-timedelta(microseconds=1);await db.flush()
        assert (await service.consumption(db,budget,start))['charged']==0
        row.started_at=start;await db.flush()
        assert (await service.consumption(db,budget,start))['charged']==200
        row.started_at=end;await db.flush()
        assert (await service.consumption(db,budget,start))['charged']==0
        row.started_at=start;row.charged_cost=None;await db.flush()
        budget.metric='cost';budget.currency='EUR'
        assert (await service.consumption(db,budget,start))['charged'] is None
        row.charged_cost=Decimal('1');row.currency='USD'
        assert (await service.consumption(db,budget,start))['charged'] is None
        await service.evaluate(db,s['project'])
        assert await db.scalar(select(BudgetAlert.id).where(BudgetAlert.level=='unavailable'))
    asyncio.run(with_db(scenario))


def test_api_schemas_sans_echo_et_refus_technique(pg_client,configured):
    s,_,_,agent,*_=configured
    csrf=login(pg_client,s['architect_email'])
    payload=dict(scope='project',scope_id=s['project'],period='daily',metric='tokens',amount=100,reason='Budget')
    route=s['base']+'/budgets/versions'
    assert pg_client.post(route,json=payload,headers={'X-CSRF-Token':csrf}).status_code==403
    csrf=login(pg_client,s['manager_email'])
    r=pg_client.post(route,json={**payload,'amount':'secret-brut-de-recette'},headers={'X-CSRF-Token':csrf})
    assert r.status_code==422 and 'secret-brut-de-recette' not in r.text
    assert r.headers['cache-control']=='no-store'
    assert action(pg_client,s,f'alerts/{uuid4()}/acknowledge').status_code==404
    pg_client.cookies.clear()
    assert pg_client.post(route,json=payload,headers=agent[1]).status_code==401


def test_formulaires_acquittement_reprise_erreur_et_masquage(pg_client,monkeypatch,configured):
    from bs4 import BeautifulSoup
    s,*_=configured
    assert save(pg_client,s).status_code==201
    run(pg_client,monkeypatch,configured,150000)
    data=read(pg_client,s)
    aid,hold=data['alerts'][0]['id'],data['suspensions'][0]['id']
    csrf=login(pg_client,s['manager_email'])
    url=f"/projects/{s['project']}/agents/runtimes"
    payload=dict(csrf_token=csrf,action='resume',identity=hold,reason='Reprise demandée en recette')
    error=pg_client.post(url,data=payload)
    assert error.status_code==409
    dom=BeautifulSoup(error.text,'html.parser')
    assert dom.select_one('[role=alert][autofocus]')
    form=next(f for f in dom.select('form') if f.select_one(f'input[name=identity][value="{hold}"]'))
    assert form.select_one('input[name=reason]')['value']==payload['reason']
    assert pg_client.post(url,data=dict(payload,action='acknowledge',identity=aid)).status_code==303
    assert read(pg_client,s)['suspended']==1 and read(pg_client,s)['unacknowledged']==0
    assert save(pg_client,s,expected_version=1,amount=200000).status_code==201
    payload['csrf_token']=login(pg_client,s['manager_email'])
    assert pg_client.post(url,data=payload).status_code==303
    assert read(pg_client,s)['suspended']==0
    login(pg_client,s['reader_email'])
    dom=BeautifulSoup(pg_client.get(url).text,'html.parser')
    assert not dom.select('#budgets form[method=post], #budget-alerts form[method=post]')


def test_claim_sans_admission_suspendu_visible_dans_pilotage(pg_client,monkeypatch):
    from tests import test_llm_run_traceability as trace
    from tests.test_agent_hierarchy_progress import task, listing
    original=trace.plan
    monkeypatch.setattr(trace,'plan',lambda client,s,tasks,**limits:original(client,s,tasks+[task('suite')],**limits))
    values=trace.setup_runtime(pg_client,monkeypatch)
    s,pid,_,agent,*_=values
    suite=listing(pg_client,s,pid)['suite']['id']
    claim(pg_client,s,agent,suite)
    assert save(pg_client,s).status_code==201
    run(pg_client,monkeypatch,values,150000)
    data=pg_client.get(s['base']+'/pilotage',params={'kind':'tasks'}).json()
    card=next(c for c in data['activities'] if c['id']==suite)
    assert 'Suspension budgétaire' in card['details']
    assert card['href']==f"/projects/{s['project']}/agents/runtimes#budget-alerts"
    assert card['state']=='Décision humaine'


def test_suspension_concurrente_au_verrou_de_plan_sans_interblocage(pg_client,monkeypatch,configured):
    from app.agent_progress import lock_task_plan
    from app.runtime_service import lock_project
    from app.identity_service import database_factory
    s,pid,tid,*_=configured
    run(pg_client,monkeypatch,configured,150000)
    assert save(pg_client,s,amount=200000).status_code==201
    async def scenario():
        factory=database_factory()
        plan_locked=asyncio.Event();runtime_locked=asyncio.Event()
        async def claim_guard():
            async with factory() as db:
                plan=await lock_task_plan(db,tid,s['project'])
                plan_locked.set();await runtime_locked.wait()
                try:
                    await service.guard_plan(db,plan)
                    pytest.fail('Une suspension concurrente doit bloquer le nouveau claim.')
                except RuntimeDenied as exc:
                    assert exc.code=='budget_suspended'
                await db.rollback()
        async def revise():
            await plan_locked.wait()
            async with factory() as db:
                await lock_project(db,s['project']);runtime_locked.set()
                actor=await db.get(User,s['manager_id'])
                data=BudgetInput(scope='project',scope_id=s['project'],expected_version=1,
                    period='daily',metric='tokens',amount=100000,reason='Révision concurrente')
                await service.revise(db,s['project'],actor,data)
                await db.commit()
        try:
            await asyncio.wait_for(asyncio.gather(claim_guard(),revise()),10)
            async with factory() as db:
                assert await db.scalar(select(func.count()).select_from(BudgetSuspension).where(
                    BudgetSuspension.plan_id==pid))==1
        finally: await factory.kw['bind'].dispose()
    asyncio.run(scenario())
