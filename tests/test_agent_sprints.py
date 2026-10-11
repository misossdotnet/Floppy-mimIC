"""AGENT-10 : révisions, board sans mutation et métriques temporelles de feuilles."""
import asyncio
import os
import subprocess
import sys
from datetime import timedelta
from uuid import uuid4
from zoneinfo import ZoneInfo

import pytest
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from app.agent_models import (AgentActionApproval, AgentSprint, AgentSprintTask, AgentTask, AgentTaskRun)
from app.agent_sprint_service import create_sprint, revise_sprint
from app.agent_sprint_web import parse_local
from app.identity_models import AuditEvent, utcnow
from app.identity_service import database_factory
from .conftest import ROOT, create_user, with_db
from .test_agent_autonomy import request, decide
from .test_agent_coordinator import plan, failure, attempts, release_delay
from .test_agent_hierarchy_progress import prepare, task, listing, session, claim, progress
from .test_agent_sessions import project_with_members
from .test_identity_acl import login


def setup(client,tasks=None,**limits):
    s=prepare(client); s['plan']=plan(client,s,tasks or [task('a'),task('b')],**limits)
    s['tasks']=listing(client,s,s['plan']); s['url']=f"{s['base']}/agent-sprints"
    return s


def payload(s,keys=('a',),**fields):
    now=utcnow()
    return {'plan_id':s['plan'],'name':'Sprint test','goal':'Suivre le travail',
            'starts_at':(now-timedelta(days=2)).isoformat(),'ends_at':(now+timedelta(days=7)).isoformat(),
            'items':[{'task_id':s['tasks'][k]['id']} for k in keys],**fields}


def create(client,s,keys=('a',),**fields):
    result=client.post(s['url'],json=payload(s,keys,**fields),headers={'X-CSRF-Token':s['csrf']})
    assert result.status_code==201,result.text
    return result.json()['id']


def board(client,s,sid,**filters):
    result=client.get(f"{s['url']}/{sid}",params=filters)
    assert result.status_code==200,result.text
    return result.json()


def control(client,s,sid,action,revision):
    return client.post(f"{s['url']}/{sid}/{action}",json={'expected_revision':revision},headers={'X-CSRF-Token':s['csrf']})


def test_calendrier_selection_et_refus_atomiques(pg_client):
    s=setup(pg_client,[task('parent'),task('a','parent'),task('b')]); headers={'X-CSRF-Token':s['csrf']}
    now=utcnow()
    for fields in ({'ends_at':(now-timedelta(days=3)).isoformat()},
                   {'ends_at':(now+timedelta(days=100)).isoformat()},
                   {'items':[{'task_id':s['tasks']['parent']['id']}]},
                   {'items':[{'task_id':s['tasks']['a']['id']}]*2},
                   {'items':[{'task_id':str(uuid4())}]},
                   {'items':[{'task_id':s['tasks']['a']['id'],'target_due_at':(now+timedelta(days=20)).isoformat()}]}):
        assert pg_client.post(s['url'],json=payload(s,**fields),headers=headers).status_code==409
    for fields in ({'starts_at':'2026-10-05T09:00:00'},{'status':'active'},{'name':' '},{'items':[{'task_id':True}]}):
        assert pg_client.post(s['url'],json=payload(s,**fields),headers=headers).status_code==422
    assert pg_client.get(s['url']).json()['sprints']==[]
    sid=create(pg_client,s,('a','b'))
    assert len(board(pg_client,s,sid)['cards'])==2
    assert pg_client.post(s['url'],json=payload(s,plan_id=str(uuid4())),headers=headers).status_code==404


def test_revision_historique_et_cycle_sans_effet_execution(pg_client):
    s=setup(pg_client); sid=create(pg_client,s); initial=board(pg_client,s,sid)
    values=payload(s,('b',),name='Nouvel objectif'); values.pop('plan_id'); values['expected_revision']=1
    url=f"{s['url']}/{sid}"
    result=pg_client.put(url,json=values,headers={'X-CSRF-Token':s['csrf']})
    assert result.status_code==200 and result.json()['revision']==2
    assert pg_client.put(url,json=values,headers={'X-CSRF-Token':s['csrf']}).status_code==409
    history=pg_client.get(url+'/history').json()['revisions']
    assert [r['revision'] for r in history]==[1,2]
    assert history[0]['snapshot']['name']=='Sprint test'
    assert history[0]['snapshot']['items'][0]['task_id']==s['tasks']['a']['id']
    assert control(pg_client,s,sid,'close',2).status_code==409
    assert control(pg_client,s,sid,'start',2).json()['status']=='active'
    assert control(pg_client,s,sid,'start',2).json()['revision']==3
    assert pg_client.put(url,json={**values,'expected_revision':3},headers={'X-CSRF-Token':s['csrf']}).status_code==409
    assert control(pg_client,s,sid,'close',3).json()['status']=='closed'
    assert control(pg_client,s,sid,'close',3).json()['revision']==4
    assert control(pg_client,s,sid,'start',4).status_code==409
    after=listing(pg_client,s,s['plan'])
    assert all(t['status']=='ready' and t['attempt_count']==0 for t in after.values())
    assert initial['counts']['todo']==1 and board(pg_client,s,sid)['counts']['todo']==1
    async def audit_check(db):
        events=(await db.scalars(select(AuditEvent).where(AuditEvent.target_id==sid).order_by(AuditEvent.created_at))).all()
        assert [e.action for e in events]==['agent.sprint.create','agent.sprint.revise','agent.sprint.start','agent.sprint.close']
        assert not any(e.action.startswith('deliverable.') for e in events)
    asyncio.run(with_db(audit_check))


def test_brouillon_vide_futur_et_annulation(pg_client):
    s=setup(pg_client); now=utcnow()
    empty=create(pg_client,s,())
    assert control(pg_client,s,empty,'start',1).status_code==409
    future=create(pg_client,s,('a',),starts_at=(now+timedelta(days=10)).isoformat(),ends_at=(now+timedelta(days=12)).isoformat())
    assert control(pg_client,s,future,'start',1).status_code==409
    data=board(pg_client,s,future)
    assert data['metrics']['throughput_per_day'] is None and data['metrics']['cycle_time_average_seconds'] is None
    assert control(pg_client,s,future,'cancel',1).json()['status']=='cancelled'
    assert control(pg_client,s,future,'cancel',1).json()['revision']==2
    assert control(pg_client,s,future,'start',2).status_code==409


def test_blocages_autonomie_et_expiration_sans_mutation(pg_client):
    s=setup(pg_client,[task('a'),task('c'),task('b',dependency_keys=['a','c'])],autonomy_mode='per_action')
    sid=create(pg_client,s,('a','b')); tid=s['tasks']['a']['id']
    async def state(db):
        return len((await db.scalars(select(AuditEvent))).all()),len((await db.scalars(select(AgentActionApproval))).all())
    before=asyncio.run(with_db(state))
    data=board(pg_client,s,sid); assert data['counts']['blocked']==2
    assert data['metrics']['blocked_by_reason']['approval_required']==2
    assert data['metrics']['blocked_by_reason']['dependency']==1 # Deux prérequis, une feuille bloquée.
    assert 'dependency:a' in next(c for c in data['cards'] if c['task_key']=='b')['blocked_by']
    assert asyncio.run(with_db(state))==before
    aid=request(pg_client,s,tid).json()['id']; assert decide(pg_client,s,tid,aid).status_code==200
    agent=session(pg_client,s); cid=claim(pg_client,s,agent,tid)
    assert board(pg_client,s,sid)['counts']['in_progress']==1
    async def expire(db):
        (await db.get(AgentTask,tid)).lease_expires_at=utcnow()-timedelta(seconds=1)
    asyncio.run(with_db(expire))
    expired=board(pg_client,s,sid); a=next(c for c in expired['cards'] if c['task_key']=='a')
    assert a['column']=='blocked' and 'lease_expired' in a['blocked_by'] and a['action_owner']=='service'
    assert listing(pg_client,s,s['plan'])['a']['status']=='claimed'
    assert attempts(pg_client,s,tid)[0]['status']=='running' and attempts(pg_client,s,tid)[0]['claim_id']==cid


def test_echeances_filtres_et_metriques_sans_compter_parents_ou_retries(pg_client):
    s=setup(pg_client,[task('p'),task('a','p'),task('b','p'),task('c',due_at=(utcnow()-timedelta(hours=1)).isoformat())])
    due=utcnow()-timedelta(minutes=30)
    sid=create(pg_client,s,('a','b','c'),items=[{'task_id':s['tasks'][k]['id'],'target_due_at':due.isoformat()} for k in ('a','b')]+[{'task_id':s['tasks']['c']['id']}])
    agent=session(pg_client,s); tid=s['tasks']['a']['id']; cid=claim(pg_client,s,agent,tid)
    assert pg_client.post(f"{s['base']}/agent-tasks/{tid}/release",params={'session_id':agent[0]},headers=agent[1]).status_code==200
    cid=claim(pg_client,s,agent,tid); assert progress(pg_client,s,agent,tid,cid,100).status_code==200
    bid=s['tasks']['b']['id']; cid=claim(pg_client,s,agent,bid); assert progress(pg_client,s,agent,bid,cid,100).status_code==200
    async def timestamps(db):
        now=utcnow()
        for item in (await db.scalars(select(AgentSprintTask).where(AgentSprintTask.sprint_id==sid))).all():
            item.added_at=now-timedelta(days=2)
        for task_id,hours in ((tid,6),(bid,4)):
            rows=(await db.scalars(select(AgentTaskRun).where(AgentTaskRun.task_id==task_id).order_by(AgentTaskRun.attempt_number))).all()
            rows[0].started_at=now-timedelta(hours=hours)
            if len(rows)>1: rows[-1].started_at=now-timedelta(hours=1)
            for row in rows:
                if row.status=='completed': row.ended_at=now-timedelta(minutes=15)
                elif row.ended_at: row.ended_at=now-timedelta(hours=2)
    asyncio.run(with_db(timestamps))
    data=board(pg_client,s,sid)
    assert len(data['cards'])==3 and data['counts']=={'todo':1,'in_progress':0,'blocked':0,'done':2,'cancelled':0}
    assert data['metrics']['throughput_count']==2 and data['metrics']['cycle_time_sample_count']==2
    assert data['metrics']['cycle_time_average_seconds']==17100 # (5h45 + 3h45) / 2.
    assert data['metrics']['overdue_count']==1
    assert all(c['completed_late'] for c in data['cards'] if c['column']=='done')
    filtered=board(pg_client,s,sid,column='todo',overdue_only=True)
    assert len(filtered['cards'])==1 and filtered['cards'][0]['task_key']=='c'
    assert filtered['counts']==data['counts']
    for key in ('throughput_count','cycle_time_sample_count','cycle_time_average_seconds','overdue_count'):
        assert filtered['metrics'][key]==data['metrics'][key]
    assert listing(pg_client,s,s['plan'])['p']['status']=='completed'


def test_blocages_echec_delai_creneau_pause_et_annulation(pg_client):
    s=setup(pg_client,max_parallel_tasks=1,max_attempts=2); sid=create(pg_client,s,('a','b'))
    agent=session(pg_client,s); tid=s['tasks']['a']['id']; cid=claim(pg_client,s,agent,tid)
    rows={c['task_key']:c for c in board(pg_client,s,sid)['cards']}
    assert 'parallel_limit' in rows['b']['blocked_by'] and rows['a']['column']=='in_progress'
    assert failure(pg_client,s,agent,tid,cid).status_code==200
    rows={c['task_key']:c for c in board(pg_client,s,sid)['cards']}
    assert 'status:failed' in rows['a']['blocked_by'] and rows['a']['action_owner']=='human'
    control_url=f"{s['base']}/agent-flows/plans/{s['plan']}"
    assert pg_client.post(control_url+'/pause',headers={'X-CSRF-Token':s['csrf']}).status_code==200
    assert all('run_inactive' in c['blocked_by'] for c in board(pg_client,s,sid)['cards'])
    assert pg_client.post(control_url+'/resume',headers={'X-CSRF-Token':s['csrf']}).status_code==200
    old=attempts(pg_client,s,tid)[0]
    assert pg_client.post(f"{s['base']}/agent-tasks/{tid}/retry",json={'attempt_id':old['id']},headers={'X-CSRF-Token':s['csrf']}).status_code==200
    assert 'retry_delay' in next(c for c in board(pg_client,s,sid)['cards'] if c['task_key']=='a')['blocked_by']
    release_delay(tid)
    assert pg_client.post(f"{s['base']}/agent-tasks/{s['tasks']['b']['id']}/cancel",headers={'X-CSRF-Token':s['csrf']}).status_code==200
    data=board(pg_client,s,sid)
    assert data['counts']['cancelled']==1 and data['metrics']['throughput_count']==0


def test_cloture_arrete_mesure_et_permet_reliquat_non_chevauchant(pg_client):
    s=setup(pg_client); sid=create(pg_client,s)
    assert control(pg_client,s,sid,'start',1).status_code==200
    closed=control(pg_client,s,sid,'close',2).json(); closed_at=closed['closed_at']
    assert closed_at
    values=payload(s,starts_at=closed_at)
    result=pg_client.post(s['url'],json=values,headers={'X-CSRF-Token':s['csrf']})
    assert result.status_code==201,result.text
    other=result.json()['id']; agent=session(pg_client,s); tid=s['tasks']['a']['id']
    cid=claim(pg_client,s,agent,tid); assert progress(pg_client,s,agent,tid,cid,100).status_code==200
    assert board(pg_client,s,sid)['metrics']['throughput_count']==0
    assert board(pg_client,s,sid)['counts']['done']==1 # État actuel, mesure historique arrêtée.
    assert board(pg_client,s,other)['metrics']['throughput_count']==1


def test_rbac_csrf_isolation_et_archivage(pg_client):
    s=setup(pg_client); sid=create(pg_client,s); agent=session(pg_client,s)
    assert pg_client.post(s['url'],json=payload(s,('b',))).status_code==403
    a,_=create_user(); r,_=create_user(); other=project_with_members(s['manager_id'],a,r)
    assert pg_client.get(f'/api/projects/{other}/agent-sprints/{sid}').status_code==404
    assert pg_client.post(f'/api/projects/{other}/agent-sprints',json=payload(s),headers={'X-CSRF-Token':s['csrf']}).status_code==404
    assert pg_client.get(f'/projects/{other}/agent-sprints/{sid}').status_code==404
    pg_client.cookies.clear()
    assert pg_client.get(s['url'],headers=agent[1]).status_code==401
    assert pg_client.get(f"/projects/{s['project']}/agent-sprints").status_code==303
    s['csrf']=login(pg_client,s['reader_email'])
    assert board(pg_client,s,sid)['sprint']['name']=='Sprint test'
    assert control(pg_client,s,sid,'start',1).status_code==403
    page=pg_client.get(f"/projects/{s['project']}/agent-sprints/{sid}")
    assert page.status_code==200 and 'Réviser le brouillon' not in page.text and 'Démarrer le sprint' not in page.text
    pg_client.cookies.clear(); s['csrf']=login(pg_client,s['manager_email'])
    async def archive(db):
        from app.identity_models import Project
        (await db.get(Project,s['project'])).status='archived'
    asyncio.run(with_db(archive))
    assert control(pg_client,s,sid,'start',1).status_code==409
    assert board(pg_client,s,sid)['sprint']['status']=='draft'


def test_concurrence_selection_et_revisions(pg_client):
    s=setup(pg_client)
    values=payload(s); values['starts_at']=parse_local(values['starts_at']); values['ends_at']=parse_local(values['ends_at'])
    async def race(create=True,sid=None):
        factory=database_factory()
        try:
            async def save():
                async with factory() as db:
                    try:
                        result=await create_sprint(db,s['project'],s['plan'],values,s['manager_id']) if create else await revise_sprint(db,s['project'],sid,1,values,s['manager_id'])
                        await db.commit(); return result.id
                    except ValueError: await db.rollback(); return None
            return await asyncio.wait_for(asyncio.gather(save(),save()),10)
        finally: await factory.kw['bind'].dispose()
    ids=asyncio.run(race()); assert sum(i is not None for i in ids)==1
    sid=next(i for i in ids if i)
    assert sum(i is not None for i in asyncio.run(race(False,sid)))==1
    assert [r['revision'] for r in pg_client.get(f"{s['url']}/{sid}/history").json()['revisions']]==[1,2]


def test_parcours_html_et_echappement(pg_client):
    from bs4 import BeautifulSoup
    s=setup(pg_client); path=f"/projects/{s['project']}/agent-sprints"
    assert pg_client.get(path).status_code==200
    page=pg_client.get(path,params={'plan_id':s['plan']}); assert page.status_code==200
    raw={'plan_id':s['plan'],'name':'<script>alert(1)</script>','goal':'Objectif sûr','csrf_token':s['csrf'],
         'starts_at':(utcnow()-timedelta(days=1)).astimezone(ZoneInfo('Europe/Paris')).strftime('%Y-%m-%dT%H:%M'),
         'ends_at':(utcnow()+timedelta(days=7)).astimezone(ZoneInfo('Europe/Paris')).strftime('%Y-%m-%dT%H:%M'),
         'task_ids':s['tasks']['a']['id']}
    invalid=pg_client.post(path,data={**raw,'ends_at':'invalide'})
    assert invalid.status_code==409 and 'Objectif sûr' in invalid.text
    result=pg_client.post(path,data=raw); assert result.status_code==303,result.text
    url=result.headers['location']; shown=pg_client.get(url,params={'column':''})
    assert shown.status_code==200 and '<script>alert(1)</script>' not in shown.text and '&lt;script&gt;' in shown.text
    soup=BeautifulSoup(shown.text,'html.parser')
    assert len(soup.select('.sprint-column'))==5 and soup.select_one('a.app-skip')
    assert soup.select_one('main#contenu')['tabindex']=='-1'
    assert soup.find('label',attrs={'for':'sprint-name'}) and soup.find('fieldset').find('legend')
    sid=url.rsplit('/',1)[-1]
    assert pg_client.post(url+'/edit',data={**raw,'name':'Révisé','expected_revision':1}).status_code==303
    assert pg_client.post(url+'/edit',data={**raw,'expected_revision':1}).status_code==409
    assert pg_client.post(url+'/start',data={'csrf_token':s['csrf'],'expected_revision':2}).status_code==303
    assert pg_client.post(url+'/cancel',data={'csrf_token':s['csrf'],'expected_revision':3}).status_code==409
    assert pg_client.post(url+'/cancel',data={'csrf_token':s['csrf'],'expected_revision':3,'confirmation':'true'}).status_code==303
    assert board(pg_client,s,sid)['sprint']['status']=='cancelled'


@pytest.mark.parametrize('value',['2026-10-25T02:30','2026-03-29T02:30'])
def test_heures_paris_ambigues_ou_inexistantes_refusees(value):
    with pytest.raises(ValueError): parse_local(value)


def test_migration_scope_et_historique_agent_preserve(pg_client):
    s=setup(pg_client); sid=create(pg_client,s); tid=s['tasks']['a']['id']
    agent=session(pg_client,s); cid=claim(pg_client,s,agent,tid); before=attempts(pg_client,s,tid)
    async def forge(db):
        db.add(AgentSprintTask(sprint_id=sid,task_id=tid,project_id=str(uuid4()),plan_version_id=s['plan']))
        await db.flush()
    with pytest.raises(IntegrityError): asyncio.run(with_db(forge))
    assert os.environ['MIMIC_DATABASE_URL'].endswith('/mimic_test')
    cmd=[sys.executable,'-m','alembic']
    try: subprocess.run([*cmd,'downgrade','0014_agent_action_approval'],cwd=ROOT,check=True)
    finally: subprocess.run([*cmd,'upgrade','head'],cwd=ROOT,check=True)
    assert attempts(pg_client,s,tid)==before and before[0]['claim_id']==cid
    assert pg_client.get(s['url']).json()['sprints']==[]
