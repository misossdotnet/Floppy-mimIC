"""AGENT-08 : DAG hiérarchique, limites atomiques, tentatives et décisions humaines."""
import asyncio
from datetime import timedelta
from uuid import uuid4

import pytest
from sqlalchemy import select

from app.agent_coordinator import leaf_dependencies
from app.agent_models import AgentSession, AgentTask, AgentTaskRun
from app.agent_routing import reserve_task
from app.identity_models import utcnow
from app.identity_service import database_factory
from .conftest import with_db
from .test_agent_hierarchy_progress import prepare, task, approve, listing, session, claim, progress
from .test_identity_acl import login


@pytest.mark.parametrize('tasks', [
    [task('a', dependency_keys=['absent'])], [task('a', dependency_keys=['a'])],
    [task('a', dependency_keys=['b']), task('b', dependency_keys=['a'])],
    [task('a'), task('b', 'a', dependency_keys=['a'])],
    [task('a', dependency_keys=['b']), task('b', 'a')],
    [task('a'), task('b', dependency_keys=['a','a'])],
])
def test_cycles_et_references_refuses(tasks):
    with pytest.raises(ValueError): leaf_dependencies(tasks)


def test_dependances_heritees_et_parents_expanses():
    assert leaf_dependencies([task('a'), task('b', 'a'), task('c', 'a'),
        task('d', dependency_keys=['a']), task('e', 'd')]) == {'b': set(), 'c': set(), 'e': {'b', 'c'}}


def plan(client, s, tasks, **limits):
    result = client.post(f"{s['base']}/agent-flows/runs/{s['run']}/plans",
        json={'tasks': tasks, **limits}, headers={'X-CSRF-Token': s['csrf']})
    assert result.status_code == 201, result.text
    pid = result.json()['plan_id']
    approve(client, s, pid)
    return pid


def next_claim(client, s, agent, key=None):
    return client.post(f"{s['base']}/agent-tasks/claim-next", params={'session_id':agent[0]},
        headers=agent[1], json={'request_key': key or str(uuid4())})


def failure(client, s, agent, tid, cid, code='execution_error'):
    return client.post(f"{s['base']}/agent-tasks/{tid}/fail", params={'session_id': agent[0]},
                       headers=agent[1], json={'claim_id': cid, 'error_code': code})


def attempts(client, s, tid):
    result = client.get(f"{s['base']}/agent-tasks/{tid}/attempts")
    assert result.status_code == 200
    return result.json()['attempts']


def release_delay(tid):
    async def change(db):
        (await db.get(AgentTask, tid)).retry_not_before = utcnow() - timedelta(seconds=1)
    asyncio.run(with_db(change))


def test_dag_routage_et_idempotence_apres_liberation(pg_client):
    s = prepare(pg_client)
    for tasks in ([task('a', dependency_keys=['z'])], [task('a', dependency_keys=['b']), task('b','a')]):
        result = pg_client.post(f"{s['base']}/agent-flows/runs/{s['run']}/plans",
            json={'tasks':tasks}, headers={'X-CSRF-Token':s['csrf']})
        assert result.status_code == 409
    pid = plan(pg_client, s, [task('group'), task('api','group',priority='P1'), task('ui','group',priority='P0'),
                             task('qa', dependency_keys=['group'])], max_parallel_tasks=1)
    tasks = listing(pg_client, s, pid)
    agent = session(pg_client, s)
    assert tasks['qa']['effective_dependency_keys'] == ['api','ui']
    assert claim_rejected(pg_client, s, agent, tasks['qa']['id']) == 409
    key = str(uuid4())
    first = next_claim(pg_client, s, agent, key)
    assert first.status_code == 200, first.text
    data = first.json()
    assert data['task_id'] == tasks['ui']['id']
    assert next_claim(pg_client, s, agent, key).json()['claim_id'] == data['claim_id']
    assert next_claim(pg_client, s, agent).status_code == 409
    assert pg_client.post(f"{s['base']}/agent-tasks/{data['task_id']}/release",
        params={'session_id':agent[0]},headers=agent[1]).json()['released']
    replay = next_claim(pg_client, s, agent, key).json()
    assert replay['claim_id'] == data['claim_id'] and replay['released_at']
    assert len(attempts(pg_client,s,data['task_id'])) == 1
    for k in ('ui','api'):
        cid = claim(pg_client,s,agent,tasks[k]['id'])
        assert progress(pg_client,s,agent,tasks[k]['id'],cid,100).status_code == 200
    last = next_claim(pg_client,s,agent)
    assert last.status_code == 200 and last.json()['task_id'] == tasks['qa']['id']


def claim_rejected(client,s,agent,tid):
    return client.post(f"{s['base']}/agent-tasks/{tid}/claim",params={'session_id':agent[0]},headers=agent[1]).status_code


def test_limite_concurrente_sur_deux_feuilles(pg_client):
    s=prepare(pg_client)
    pid=plan(pg_client,s,[task('a'),task('b')],max_parallel_tasks=1)
    tasks=listing(pg_client,s,pid)
    agents=[session(pg_client,s),session(pg_client,s)]
    async def race():
        factory=database_factory()
        try:
            async def run(agent,key):
                async with factory() as db:
                    technical=await db.get(AgentSession,agent[0])
                    try:
                        await reserve_task(db,session=technical,task_id=tasks[key]['id'])
                        await db.commit()
                        return True
                    except ValueError:
                        await db.rollback()
                        return False
            return await asyncio.wait_for(asyncio.gather(run(agents[0],'a'),run(agents[1],'b')),10)
        finally: await factory.kw['bind'].dispose()
    assert sum(asyncio.run(race())) == 1
    assert sum(t['attempt_count'] for t in listing(pg_client,s,pid).values()) == 1


def test_echec_retry_borne_et_historique(pg_client):
    s=prepare(pg_client)
    pid=plan(pg_client,s,[task('a')],max_attempts=2)
    tid=listing(pg_client,s,pid)['a']['id']
    agent=session(pg_client,s)
    cid=claim(pg_client,s,agent,tid)
    assert failure(pg_client,s,agent,tid,cid,'secret').status_code == 422
    assert progress(pg_client,s,agent,tid,cid,40).status_code == 200
    assert failure(pg_client,s,agent,tid,cid).status_code == 200
    assert failure(pg_client,s,agent,tid,cid).status_code == 200
    old=attempts(pg_client,s,tid)[0]
    retry_url=f"{s['base']}/agent-tasks/{tid}/retry"
    payload={'attempt_id':old['id']}
    assert pg_client.post(retry_url,json=payload).status_code == 403
    assert pg_client.post(retry_url,json={'attempt_id':str(uuid4())},headers={'X-CSRF-Token':s['csrf']}).status_code == 409
    for _ in range(2): assert pg_client.post(retry_url,json=payload,headers={'X-CSRF-Token':s['csrf']}).status_code == 200
    assert claim_rejected(pg_client,s,agent,tid) == 409
    release_delay(tid)
    cid2=claim(pg_client,s,agent,tid)
    assert cid2 != cid
    assert failure(pg_client,s,agent,tid,cid).status_code == 409
    assert failure(pg_client,s,agent,tid,cid2,'timeout').status_code == 200
    history=attempts(pg_client,s,tid)
    assert history[0] == old and history[1]['parent_run_id'] == old['id']
    assert [a['attempt_number'] for a in history] == [1,2]
    assert pg_client.post(retry_url,json={'attempt_id':history[-1]['id']},headers={'X-CSRF-Token':s['csrf']}).status_code == 409
    assert claim_rejected(pg_client,s,agent,tid) == 409


def test_pause_reprise_annulation_et_rbac(pg_client):
    s=prepare(pg_client)
    pid=plan(pg_client,s,[task('parent'),task('a','parent'),task('b','parent'),task('qa',dependency_keys=['parent'])])
    tasks=listing(pg_client,s,pid)
    agent=session(pg_client,s)
    cid=claim(pg_client,s,agent,tasks['a']['id'])
    control=f"{s['base']}/agent-flows/plans/{pid}"
    assert pg_client.post(control+'/pause').status_code == 403
    for _ in range(2): assert pg_client.post(control+'/pause',headers={'X-CSRF-Token':s['csrf']}).status_code == 200
    assert progress(pg_client,s,agent,tasks['a']['id'],cid,100).status_code == 409
    assert next_claim(pg_client,s,agent).status_code == 409
    assert pg_client.post(control+'/resume',headers={'X-CSRF-Token':s['csrf']}).status_code == 200
    assert progress(pg_client,s,agent,tasks['a']['id'],cid,100).status_code == 409
    cid2=claim(pg_client,s,agent,tasks['a']['id'])
    assert progress(pg_client,s,agent,tasks['a']['id'],cid2,100).status_code == 200
    cancel=f"{s['base']}/agent-tasks/{tasks['parent']['id']}/cancel"
    for _ in range(2): assert pg_client.post(cancel,headers={'X-CSRF-Token':s['csrf']}).status_code == 200
    observed=listing(pg_client,s,pid)
    assert observed['a']['status']=='completed' and observed['b']['status']=='cancelled'
    assert observed['parent']['status']=='cancelled' and observed['qa']['blocked_by']
    pg_client.cookies.clear()
    reader_csrf=login(pg_client,s['reader_email'])
    assert pg_client.post(control+'/cancel',headers={'X-CSRF-Token':reader_csrf}).status_code == 403
    assert attempts(pg_client,s,tasks['a']['id'])
    pg_client.cookies.clear()
    s['csrf']=login(pg_client,s['manager_email'])
    assert pg_client.post(control+'/cancel',headers={'X-CSRF-Token':s['csrf']}).status_code == 200
    assert pg_client.post(control+'/resume',headers={'X-CSRF-Token':s['csrf']}).status_code == 409


def test_expiration_budget_et_isolation_des_commandes(pg_client):
    from .conftest import create_user
    from .test_agent_sessions import project_with_members
    s=prepare(pg_client)
    pid=plan(pg_client,s,[task('a',priority='P0'),task('b')],max_attempts=1,max_parallel_tasks=1)
    tasks=listing(pg_client,s,pid)
    agent=session(pg_client,s)
    cid=claim(pg_client,s,agent,tasks['a']['id'])
    async def expire(db):
        (await db.get(AgentTask,tasks['a']['id'])).lease_expires_at=utcnow()-timedelta(seconds=1)
    asyncio.run(with_db(expire))
    next_result=next_claim(pg_client,s,agent)
    assert next_result.status_code==200 and next_result.json()['task_id']==tasks['b']['id']
    observed=listing(pg_client,s,pid)
    assert observed['a']['status']=='failed' and observed['a']['attempt_count']==1
    assert failure(pg_client,s,agent,tasks['a']['id'],cid).status_code==409
    a1,_=create_user(); r1,_=create_user()
    foreign=project_with_members(s['manager_id'],a1,r1)
    assert pg_client.post(f"/api/projects/{foreign}/agent-flows/plans/{pid}/cancel",
        headers={'X-CSRF-Token':s['csrf']}).status_code==404
    assert pg_client.get(f"/api/projects/{foreign}/agent-tasks/{tasks['a']['id']}/attempts").status_code==404
    assert pg_client.post(f"/api/projects/{foreign}/agent-tasks/{tasks['a']['id']}/cancel",
        headers={'X-CSRF-Token':s['csrf']}).status_code==404
    assert pg_client.post(f"/api/projects/{foreign}/agent-tasks/claim-next",params={'session_id':agent[0]},
        headers=agent[1],json={'request_key':str(uuid4())}).status_code==401


def test_course_annulation_et_achevement(pg_client):
    from app.agent_coordinator import cancel_subtree
    from app.agent_progress import report_progress
    from app.identity_models import AuditEvent
    s=prepare(pg_client)
    pid=plan(pg_client,s,[task('a')])
    tid=listing(pg_client,s,pid)['a']['id']
    agent=session(pg_client,s); cid=claim(pg_client,s,agent,tid)
    async def race():
        factory=database_factory()
        try:
            async def finish():
                async with factory() as db:
                    technical=await db.get(AgentSession,agent[0])
                    try:
                        await report_progress(db,session=technical,task_id=tid,claim_id=cid,progress_percent=100)
                        await db.commit()
                    except ValueError: await db.rollback()
            async def cancel():
                async with factory() as db:
                    await cancel_subtree(db,s['project'],tid,s['manager_id'])
                    await db.commit()
            await asyncio.wait_for(asyncio.gather(finish(),cancel()),10)
        finally: await factory.kw['bind'].dispose()
    asyncio.run(race())
    observed=listing(pg_client,s,pid)['a']
    history=attempts(pg_client,s,tid)
    assert observed['status'] in {'completed','cancelled'}
    assert history[0]['status']==observed['status'] and history[0]['ended_at']
    async def verify(db):
        events=(await db.scalars(select(AuditEvent).where(AuditEvent.project_id==s['project']))).all()
        assert not any(e.action.startswith('deliverable.') for e in events)
        assert any(e.action in {'agent.task.cancel','agent.task.progress'} for e in events)
    asyncio.run(with_db(verify))


def test_migration_historique_des_claims(pg_client):
    import subprocess,sys,os
    from .conftest import ROOT
    s=prepare(pg_client)
    pid=plan(pg_client,s,[task('a')])
    tid=listing(pg_client,s,pid)['a']['id']
    agent=session(pg_client,s); cid=claim(pg_client,s,agent,tid)
    assert progress(pg_client,s,agent,tid,cid,100).status_code==200
    assert os.environ['MIMIC_DATABASE_URL'].endswith('/mimic_test')
    command=[sys.executable,'-m','alembic']
    try:
        subprocess.run([*command,'downgrade','0012_agent_task_hierarchy'],cwd=ROOT,check=True)
    finally:
        subprocess.run([*command,'upgrade','head'],cwd=ROOT,check=True)
    observed=listing(pg_client,s,pid)['a']
    history=attempts(pg_client,s,tid)
    assert observed['attempt_count']==1
    assert len(history)==1 and history[0]['claim_id']==cid and history[0]['status']=='completed'


def test_pause_concurrente_avec_heartbeat_multi_claims(pg_client):
    from app.agent_coordinator import control_plan
    from app.agent_routing import extend_session_claims
    s=prepare(pg_client)
    pid=plan(pg_client,s,[task('a'),task('b')],max_parallel_tasks=2)
    tasks=listing(pg_client,s,pid)
    agent=session(pg_client,s)
    for key in ('a','b'): claim(pg_client,s,agent,tasks[key]['id'])
    async def race():
        factory=database_factory()
        try:
            async def heartbeat():
                async with factory() as db:
                    technical=await db.get(AgentSession,agent[0])
                    await extend_session_claims(db,technical)
                    await db.commit()
            async def pause():
                async with factory() as db:
                    await control_plan(db,s['project'],pid,'pause',s['manager_id'])
                    await db.commit()
            await asyncio.wait_for(asyncio.gather(heartbeat(),pause()),10)
        finally: await factory.kw['bind'].dispose()
    asyncio.run(race())
    observed=listing(pg_client,s,pid)
    assert all(t['status']=='ready' for t in observed.values())
    assert all(attempts(pg_client,s,t['id'])[0]['status']=='cancelled' for t in observed.values())
