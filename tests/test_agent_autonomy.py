"""AGENT-09 : portes humaines par tentative, isolation et consommation atomique."""
import asyncio
import os
import subprocess
import sys
from datetime import timedelta
from uuid import uuid4

import pytest
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from app.agent_autonomy import decide_approval, request_approval
from app.agent_coordinator import control_plan
from app.agent_models import AgentActionApproval, AgentSession, AgentTask
from app.agent_routing import reserve_task
from app.identity_models import AuditEvent, utcnow
from app.identity_service import database_factory
from .conftest import ROOT, create_user, with_db
from .test_agent_coordinator import plan, next_claim, failure, attempts, release_delay
from .test_agent_hierarchy_progress import prepare, task, listing, session, claim, progress
from .test_agent_sessions import project_with_members
from .test_identity_acl import login


def history(client, s, tid):
    result = client.get(f"{s['base']}/agent-tasks/{tid}/approvals")
    assert result.status_code == 200, result.text
    return result.json()['approvals']


def request(client, s, tid, number=1):
    return client.post(f"{s['base']}/agent-tasks/{tid}/approval-request",
        json={'attempt_number': number}, headers={'X-CSRF-Token': s['csrf']})


def decide(client, s, tid, aid, choice='approve', headers=None):
    return client.post(f"{s['base']}/agent-tasks/{tid}/approvals/{aid}/{choice}",
        headers=headers if headers is not None else {'X-CSRF-Token': s['csrf']})


def direct(client, s, agent, tid):
    return client.post(f"{s['base']}/agent-tasks/{tid}/claim", params={'session_id':agent[0]}, headers=agent[1])


@pytest.mark.parametrize('mode,expected', [('bounded',False), ('sensitive',False), ('per_action',True)])
def test_modes_et_plan_immuable(pg_client, mode, expected):
    s = prepare(pg_client)
    pid = plan(pg_client, s, [task('a')], autonomy_mode=mode)
    view = listing(pg_client,s,pid)['a']; tid = view['id']
    assert view['approval_required'] is expected
    data = pg_client.get(f"{s['base']}/agent-tasks",params={'plan_id':pid}).json()
    assert data['autonomy_mode'] == mode
    assert pg_client.patch(f"{s['base']}/agent-flows/plans/{pid}",json={'autonomy_mode':'bounded'},
        headers={'X-CSRF-Token':s['csrf']}).status_code in {404,405}
    assert pg_client.get(f"{s['base']}/agent-tasks",params={'plan_id':pid}).json()['autonomy_mode']==mode
    agent = session(pg_client,s)
    first = direct(pg_client,s,agent,tid)
    assert first.status_code == (409 if expected else 200), first.text
    if expected:
        pending = history(pg_client,s,tid)[0]
        assert pending['status']=='pending' and pending['requested_session_id']==agent[0]
        assert request(pg_client,s,tid).json()['id']==pending['id']
        assert decide(pg_client,s,tid,pending['id']).status_code==200
        cid = claim(pg_client,s,agent,tid)
        assert progress(pg_client,s,agent,tid,cid,100).status_code==200
    else:
        assert history(pg_client,s,tid)==[]
        assert request(pg_client,s,tid).status_code==409


def test_borne_refuse_sensible_et_validation_des_classes(pg_client):
    s=prepare(pg_client)
    url=f"{s['base']}/agent-flows/runs/{s['run']}/plans"
    headers={'X-CSRF-Token':s['csrf']}
    for tasks in ([task('a',action_class='sensitive')], [task('p',action_class='sensitive'),task('a','p')]):
        assert pg_client.post(url,json={'tasks':tasks},headers=headers).status_code==409
    for body in ({'tasks':[task('a')],'autonomy_mode':'free'},
                 {'tasks':[task('a',action_class='secret')],'autonomy_mode':'sensitive'},
                 {'tasks':[task('a')],'approval':True}):
        assert pg_client.post(url,json=body,headers=headers).status_code==422
    pid=plan(pg_client,s,[task('a')])
    assert listing(pg_client,s,pid)['a']['action_class']=='standard'


def test_heritage_attente_ciblee_et_audit(pg_client):
    s=prepare(pg_client)
    pid=plan(pg_client,s,[task('p',action_class='sensitive'),task('secure','p',priority='P0'),
                          task('free',priority='P1')],autonomy_mode='sensitive',max_parallel_tasks=1)
    views=listing(pg_client,s,pid); tid=views['secure']['id']
    assert views['secure']['effective_action_class']=='sensitive'
    assert not views['p']['approval_required'] and request(pg_client,s,views['p']['id']).status_code==409
    agent=session(pg_client,s)
    for _ in range(2):
        ready=pg_client.get(f"{s['base']}/agent-tasks/ready",params={'session_id':agent[0]},headers=agent[1])
        assert [t['task_key'] for t in ready.json()['tasks']]==['free']
    h=history(pg_client,s,tid); assert len(h)==1 and h[0]['status']=='pending'
    aid=h[0]['id']
    view=listing(pg_client,s,pid)['secure']
    assert 'approval_pending' in view['blocked_by'] and view['attempt_count']==0
    assert attempts(pg_client,s,tid)==[]
    free=next_claim(pg_client,s,agent).json()
    assert free['task_id']==views['free']['id']
    assert decide(pg_client,s,tid,aid).status_code==200
    assert direct(pg_client,s,agent,tid).status_code==409 # Accord ne contourne pas les créneaux.
    assert history(pg_client,s,tid)[0]['status']=='approved'
    assert progress(pg_client,s,agent,free['task_id'],free['claim_id'],100).status_code==200
    key=str(uuid4()); secured=next_claim(pg_client,s,agent,key)
    assert secured.status_code==200 and secured.json()['task_id']==tid
    consumed=history(pg_client,s,tid)[0]
    assert consumed['status']=='consumed' and consumed['consumed_claim_id']==secured.json()['claim_id']
    assert consumed['decided_by']==s['manager_id'] and consumed['decision_result']=='approved'
    assert attempts(pg_client,s,tid)[0]['approval_id']==aid
    assert decide(pg_client,s,tid,aid).json()==consumed
    assert decide(pg_client,s,tid,aid,'reject').status_code==409
    assert next_claim(pg_client,s,agent,key).json()['claim_id']==secured.json()['claim_id']
    async def check(db):
        events=(await db.scalars(select(AuditEvent).where(AuditEvent.project_id==s['project'])
            .order_by(AuditEvent.created_at))).all()
        actions=[e.action for e in events if e.target_id==aid and e.outcome=='success']
        assert actions==['agent.approval.request','agent.approval.approve','agent.approval.consume']
        assert not any(e.action.startswith('deliverable.') for e in events)
        assert any(e.action=='agent.plan.propose' and e.details['autonomy_mode']=='sensitive' for e in events)
    asyncio.run(with_db(check))


def test_refus_terminal_et_dependants_bloques(pg_client):
    s=prepare(pg_client)
    pid=plan(pg_client,s,[task('a'),task('b',dependency_keys=['a'])],autonomy_mode='per_action')
    tasks=listing(pg_client,s,pid); tid=tasks['a']['id']
    aid=request(pg_client,s,tid).json()['id']
    for _ in range(2): assert decide(pg_client,s,tid,aid,'reject').status_code==200
    assert decide(pg_client,s,tid,aid).status_code==409
    observed=listing(pg_client,s,pid)
    assert observed['a']['status']=='cancelled' and observed['a']['attempt_count']==0
    assert 'dependency:a' in observed['b']['blocked_by']
    assert request(pg_client,s,tid).status_code==409
    agent=session(pg_client,s)
    assert direct(pg_client,s,agent,tid).status_code==409
    assert next_claim(pg_client,s,agent).status_code==409
    assert history(pg_client,s,tid)[0]['status']=='rejected' and attempts(pg_client,s,tid)==[]


def test_pause_reprise_et_annulation_invalident_accords(pg_client):
    s=prepare(pg_client)
    pid=plan(pg_client,s,[task('p'),task('a','p'),task('b','p'),task('c')],autonomy_mode='per_action')
    tasks=listing(pg_client,s,pid)
    ids={k:request(pg_client,s,tasks[k]['id']).json()['id'] for k in ('a','b','c')}
    assert decide(pg_client,s,tasks['a']['id'],ids['a']).status_code==200
    control=f"{s['base']}/agent-flows/plans/{pid}"
    assert pg_client.post(control+'/pause',headers={'X-CSRF-Token':s['csrf']}).status_code==200
    for k in ids:
        h=history(pg_client,s,tasks[k]['id'])[0]
        assert h['status']=='invalidated' and h['invalidated_at'] and h['invalidation_reason']=='paused'
        assert decide(pg_client,s,tasks[k]['id'],ids[k],'reject').status_code==409
    preserved=decide(pg_client,s,tasks['a']['id'],ids['a']).json()
    assert preserved['status']=='invalidated' and preserved['decision_result']=='approved'
    assert pg_client.post(control+'/resume',headers={'X-CSRF-Token':s['csrf']}).status_code==200
    agent=session(pg_client,s)
    assert next_claim(pg_client,s,agent).status_code==409
    new=history(pg_client,s,tasks['a']['id'])[-1]
    assert new['id']!=ids['a'] and new['attempt_number']==1 and new['status']=='pending'
    assert decide(pg_client,s,tasks['a']['id'],new['id']).status_code==200
    cancel=f"{s['base']}/agent-tasks/{tasks['p']['id']}/cancel"
    assert pg_client.post(cancel,headers={'X-CSRF-Token':s['csrf']}).status_code==200
    assert history(pg_client,s,tasks['a']['id'])[-1]['invalidation_reason']=='cancelled'
    assert history(pg_client,s,tasks['c']['id'])[-1]['status']=='pending'
    assert pg_client.post(control+'/cancel',headers={'X-CSRF-Token':s['csrf']}).status_code==200
    assert history(pg_client,s,tasks['c']['id'])[-1]['invalidation_reason']=='cancelled'


@pytest.mark.parametrize('ending',['release','expire','fail'])
def test_nouvelle_tentative_exige_nouvel_accord(pg_client, ending):
    s=prepare(pg_client)
    pid=plan(pg_client,s,[task('a')],autonomy_mode='per_action',max_attempts=2)
    tid=listing(pg_client,s,pid)['a']['id']; agent=session(pg_client,s)
    aid=request(pg_client,s,tid).json()['id']; assert decide(pg_client,s,tid,aid).status_code==200
    cid=claim(pg_client,s,agent,tid)
    assert progress(pg_client,s,agent,tid,cid,25).status_code==200
    if ending=='release':
        assert pg_client.post(f"{s['base']}/agent-tasks/{tid}/release",params={'session_id':agent[0]},headers=agent[1]).status_code==200
    elif ending=='expire':
        async def expire(db):
            (await db.get(AgentTask,tid)).lease_expires_at=utcnow()-timedelta(seconds=1)
        asyncio.run(with_db(expire))
        assert next_claim(pg_client,s,agent).status_code==409
    else:
        assert failure(pg_client,s,agent,tid,cid).status_code==200
        old=attempts(pg_client,s,tid)[0]
        assert pg_client.post(f"{s['base']}/agent-tasks/{tid}/retry",json={'attempt_id':old['id']},
            headers={'X-CSRF-Token':s['csrf']}).status_code==200
    release_delay(tid)
    assert direct(pg_client,s,agent,tid).status_code==409
    h=history(pg_client,s,tid)
    assert [a['status'] for a in h]==['consumed','pending']
    assert [a['attempt_number'] for a in h]==[1,2]
    assert request(pg_client,s,tid,1).status_code==409
    assert decide(pg_client,s,tid,aid).json()['status']=='consumed'
    assert direct(pg_client,s,agent,tid).status_code==409
    assert decide(pg_client,s,tid,h[-1]['id']).status_code==200
    cid2=claim(pg_client,s,agent,tid); assert cid2!=cid
    assert progress(pg_client,s,agent,tid,cid,100).status_code==409
    assert progress(pg_client,s,agent,tid,cid2,100).status_code==200
    assert listing(pg_client,s,pid)['a']['attempt_count']==2
    assert [a['approval_id'] for a in attempts(pg_client,s,tid)]==[a['id'] for a in h]


def test_autorisations_et_isolation(pg_client):
    s=prepare(pg_client)
    pid=plan(pg_client,s,[task('a'),task('b')],autonomy_mode='per_action')
    tasks=listing(pg_client,s,pid); tid=tasks['a']['id']
    aid=request(pg_client,s,tid).json()['id']; agent=session(pg_client,s)
    assert decide(pg_client,s,tid,aid,headers={}).status_code==403
    assert decide(pg_client,s,tasks['b']['id'],aid).status_code==404
    assert request(pg_client,s,tid,2).status_code==409
    a1,_=create_user(); r1,_=create_user(); foreign=project_with_members(s['manager_id'],a1,r1)
    assert pg_client.post(f"/api/projects/{foreign}/agent-tasks/{tid}/approvals/{aid}/approve",
        headers={'X-CSRF-Token':s['csrf']}).status_code==404
    assert pg_client.get(f"/api/projects/{foreign}/agent-tasks/{tid}/approvals").status_code==404
    pg_client.cookies.clear()
    assert decide(pg_client,s,tid,aid,headers=agent[1]).status_code==401
    reader_csrf=login(pg_client,s['reader_email'])
    assert decide(pg_client,s,tid,aid,headers={'X-CSRF-Token':reader_csrf}).status_code==403
    s['csrf']=reader_csrf
    assert request(pg_client,s,tid).status_code==403
    assert history(pg_client,s,tid)[0]['status']=='pending'
    pg_client.cookies.clear(); s['csrf']=login(pg_client,s['manager_email'])
    assert decide(pg_client,s,tid,aid).status_code==200


def test_concurrence_un_seul_accord_et_claim(pg_client):
    s=prepare(pg_client)
    pid=plan(pg_client,s,[task('a')],autonomy_mode='per_action')
    tid=listing(pg_client,s,pid)['a']['id']
    agents=[session(pg_client,s),session(pg_client,s)]
    async def race_requests():
        factory=database_factory()
        try:
            async def run():
                async with factory() as db:
                    approval=await request_approval(db,s['project'],tid,1,s['manager_id'])
                    await db.commit(); return approval.id
            return await asyncio.wait_for(asyncio.gather(run(),run()),10)
        finally: await factory.kw['bind'].dispose()
    aids=asyncio.run(race_requests()); assert aids[0]==aids[1]
    assert decide(pg_client,s,tid,aids[0]).status_code==200
    async def race_claims():
        factory=database_factory()
        try:
            async def run(agent):
                async with factory() as db:
                    technical=await db.get(AgentSession,agent[0])
                    try:
                        await reserve_task(db,session=technical,task_id=tid)
                        await db.commit(); return True
                    except ValueError: await db.rollback(); return False
            return await asyncio.wait_for(asyncio.gather(*(run(a) for a in agents)),10)
        finally: await factory.kw['bind'].dispose()
    assert sum(asyncio.run(race_claims()))==1
    assert len(history(pg_client,s,tid))==len(attempts(pg_client,s,tid))==1
    assert listing(pg_client,s,pid)['a']['attempt_count']==1


def test_decision_concurrente_pause(pg_client):
    s=prepare(pg_client)
    pid=plan(pg_client,s,[task('a')],autonomy_mode='per_action')
    tid=listing(pg_client,s,pid)['a']['id']; aid=request(pg_client,s,tid).json()['id']
    async def race():
        factory=database_factory()
        try:
            async def approve():
                async with factory() as db:
                    try:
                        await decide_approval(db,s['project'],tid,aid,'approve',s['manager_id'])
                        await db.commit()
                    except ValueError: await db.rollback()
            async def pause():
                async with factory() as db:
                    await control_plan(db,s['project'],pid,'pause',s['manager_id']); await db.commit()
            await asyncio.wait_for(asyncio.gather(approve(),pause()),10)
        finally: await factory.kw['bind'].dispose()
    asyncio.run(race())
    h=history(pg_client,s,tid)[0]
    assert h['status']=='invalidated' and h['decision_result'] in {None,'approved'}
    assert attempts(pg_client,s,tid)==[] and next_claim(pg_client,s,session(pg_client,s)).status_code==409


def test_preapprobation_ne_contourne_pas_dependance(pg_client):
    s=prepare(pg_client)
    pid=plan(pg_client,s,[task('a'),task('b',dependency_keys=['a'])],autonomy_mode='per_action')
    tasks=listing(pg_client,s,pid); agent=session(pg_client,s)
    bid=tasks['b']['id']; aid=request(pg_client,s,bid).json()['id']
    assert decide(pg_client,s,bid,aid).status_code==200
    assert direct(pg_client,s,agent,bid).status_code==409
    assert history(pg_client,s,bid)[0]['status']=='approved' and attempts(pg_client,s,bid)==[]
    first=request(pg_client,s,tasks['a']['id']).json()['id']
    assert decide(pg_client,s,tasks['a']['id'],first).status_code==200
    cid=claim(pg_client,s,agent,tasks['a']['id'])
    assert progress(pg_client,s,agent,tasks['a']['id'],cid,100).status_code==200
    assert claim(pg_client,s,agent,bid)


def test_decisions_opposees_concurrentes(pg_client):
    s=prepare(pg_client)
    pid=plan(pg_client,s,[task('a')],autonomy_mode='per_action')
    tid=listing(pg_client,s,pid)['a']['id']; aid=request(pg_client,s,tid).json()['id']
    async def race():
        factory=database_factory()
        try:
            async def run(choice):
                async with factory() as db:
                    try:
                        await decide_approval(db,s['project'],tid,aid,choice,s['manager_id'])
                        await db.commit(); return True
                    except ValueError: await db.rollback(); return False
            return await asyncio.wait_for(asyncio.gather(run('approve'),run('reject')),10)
        finally: await factory.kw['bind'].dispose()
    assert sum(asyncio.run(race()))==1
    h=history(pg_client,s,tid)[0]
    assert h['status']==h['decision_result'] and h['status'] in {'approved','rejected'}
    assert attempts(pg_client,s,tid)==[]


def test_claim_concurrent_pause_ne_reactive_pas_accord(pg_client):
    s=prepare(pg_client)
    pid=plan(pg_client,s,[task('a')],autonomy_mode='per_action')
    tid=listing(pg_client,s,pid)['a']['id']; aid=request(pg_client,s,tid).json()['id']
    assert decide(pg_client,s,tid,aid).status_code==200
    agent=session(pg_client,s)
    async def race():
        factory=database_factory()
        try:
            async def reserve():
                async with factory() as db:
                    technical=await db.get(AgentSession,agent[0])
                    try:
                        await reserve_task(db,session=technical,task_id=tid); await db.commit()
                    except ValueError: await db.rollback()
            async def pause():
                async with factory() as db:
                    await control_plan(db,s['project'],pid,'pause',s['manager_id']); await db.commit()
            await asyncio.wait_for(asyncio.gather(reserve(),pause()),10)
        finally: await factory.kw['bind'].dispose()
    asyncio.run(race())
    h=history(pg_client,s,tid)[0]; runs=attempts(pg_client,s,tid)
    assert h['status'] in {'consumed','invalidated'}
    assert len(runs)==(1 if h['status']=='consumed' else 0)
    if runs: assert runs[0]['status']=='cancelled'
    assert listing(pg_client,s,pid)['a']['status']=='ready'
    assert next_claim(pg_client,s,agent).status_code==409


def test_migration_et_contraintes_decision(pg_client):
    s=prepare(pg_client)
    pid=plan(pg_client,s,[task('a')]); tid=listing(pg_client,s,pid)['a']['id']
    agent=session(pg_client,s); cid=claim(pg_client,s,agent,tid)
    before=attempts(pg_client,s,tid)[0]
    assert os.environ['MIMIC_DATABASE_URL'].endswith('/mimic_test')
    command=[sys.executable,'-m','alembic']
    try: subprocess.run([*command,'downgrade','0013_agent_task_coordinator'],cwd=ROOT,check=True)
    finally: subprocess.run([*command,'upgrade','head'],cwd=ROOT,check=True)
    assert attempts(pg_client,s,tid)[0]==before and before['claim_id']==cid and before['approval_id'] is None
    assert listing(pg_client,s,pid)['a']['action_class']=='standard'
    assert pg_client.get(f"{s['base']}/agent-tasks",params={'plan_id':pid}).json()['autonomy_mode']=='bounded'
    async def invalid(db):
        db.add(AgentActionApproval(task_id=tid,attempt_number=2,autonomy_mode='per_action',action_class='standard',
            status='approved',requested_by=s['manager_id']))
        await db.flush()
    with pytest.raises(IntegrityError): asyncio.run(with_db(invalid))
