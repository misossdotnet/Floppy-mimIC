"""AGENT-13 : incidents, rôles, notifications, concurrence et audit sur PostgreSQL."""
import asyncio
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from uuid import uuid4

import pytest
from sqlalchemy import func, select, text

from app.agent_models import AgentSession, AgentTask, FlowRun, SkillProfile
from app.identity_models import AuditEvent, Project, utcnow
from app.incident_models import IncidentEvent, IncidentNotification, TechnicalIncident
from .conftest import ROOT, create_user, with_db
from .test_agent_conversations import messaging, emit, payload as message_payload, headers
from .test_identity_acl import login
from .test_project_memory import setup, as_user


def incident_payload(m, **changes):
    return {'request_key': str(uuid4()), 'team_id': m['team'],
        'responsible_profile_id': m['profiles']['backend'], 'task_id': m['tasks']['backend'],
        'title': 'Erreur de compilation', 'description': 'Le compilateur refuse le module ; diagnostic à revoir.',
        'priority': 'P1', **changes}


def human_create(client, m, **changes):
    csrf = login_token(client)
    response = client.post(m['base']+'/incidents', json=incident_payload(m, **changes), headers={'X-CSRF-Token': csrf})
    assert response.status_code == 201, response.text
    return response.json()['incident']


def login_token(client):
    from app.identity_service import csrf_for
    return csrf_for(client.cookies['mimic-session'], 'session')


def change(client, m, incident, action, revision=None, **values):
    return client.post(m['base']+f"/incidents/{incident['id']}/{action}", json={
        'expected_revision': revision or incident['revision'], **values}, headers={'X-CSRF-Token': login_token(client)})


def test_cycle_escalade_correction_resolution_et_audit(pg_client, messaging):
    m=messaging; row=human_create(pg_client,m)
    assert row['status']=='open' and row['content_status']=='À revoir'
    notification=pg_client.get(m['base']+'/incidents/notifications').json()['notifications'][0]
    assert pg_client.get(m['base']+'/incidents').json()['incidents'][0]['id']==row['id']
    row=change(pg_client,m,row,'acknowledge').json()
    row=change(pg_client,m,row,'assign',responsible_profile_id=m['profiles']['frontend'],reason='Diagnostic interface').json()
    row=change(pg_client,m,row,'escalate',responsible_profile_id=m['profiles']['qa'],reason='Vérification indépendante').json()
    assert row['status']=='escalated'
    row=change(pg_client,m,row,'priority',priority='P0',reason='Blocage de livraison').json()
    row=change(pg_client,m,row,'edit',title='Erreur confirmée',description='Signalement corrigé.',reason='Diagnostic précisé').json()
    result=change(pg_client,m,row,'resolve',reason='Compilation et tests vérifiés')
    assert result.status_code==200,result.text
    resolved=result.json()
    assert resolved['status']=='resolved' and resolved['resolved_at']
    assert len(resolved['events'])==7 and resolved['events'][0]['snapshot']['title']=='Erreur de compilation'
    assert change(pg_client,m,row,'resolve',reason='Compilation et tests vérifiés').json()==resolved
    assert change(pg_client,m,resolved,'resolve',reason='Autre décision').status_code==409
    assert change(pg_client,m,resolved,'edit',title='Autre',description='Contenu',reason='Correction').status_code==409
    async def verify(db):
        events=list(await db.scalars(select(AuditEvent).where(AuditEvent.target_id==row['id'], AuditEvent.outcome=='success')))
        assert [e.action for e in events]==['incident.report','incident.acknowledge','incident.assign','incident.escalate',
                                            'incident.priority','incident.edit','incident.resolve']
        assert all('Compilation et tests vérifiés' not in str(e.details) for e in events)
        assert (await db.get(AgentTask,m['tasks']['backend'])).status=='claimed'
        assert (await db.get(FlowRun,m['run'])).status=='running'
        assert (await db.get(IncidentNotification,notification['id'])).status=='pending'
    asyncio.run(with_db(verify))


def test_refus_rbac_csrf_archive_et_isolation(pg_client,messaging):
    m=messaging; values=incident_payload(m); row=human_create(pg_client,m)
    assert pg_client.post(m['base']+'/incidents',json=values).status_code==403
    assert change(pg_client,m,row,'resolve',reason='').status_code==409
    assert change(pg_client,m,row,'escalate',responsible_profile_id=str(uuid4()),reason='Escalade').status_code==409
    assert change(pg_client,m,row,'priority',priority='P3',title='Champ déplacé',reason='Motif').status_code==409
    async def archive(db): (await db.get(Project,m['project'])).status='archived'
    asyncio.run(with_db(archive))
    assert pg_client.get(m['base']+f"/incidents/{row['id']}").status_code==200
    assert change(pg_client,m,row,'resolve',reason='Vérifié').status_code==409
    pg_client.cookies.clear(); login(pg_client,m['reader_email'])
    assert pg_client.post(m['base']+'/incidents',json=values,headers={'X-CSRF-Token':login_token(pg_client)}).status_code==403
    assert change(pg_client,m,row,'acknowledge').status_code==403
    assert pg_client.get(m['base']+'/incidents/notifications').json()['notifications']==[]
    _,outsider=create_user();pg_client.cookies.clear();login(pg_client,outsider)
    assert pg_client.get(m['base']+'/incidents').status_code==404
    assert pg_client.get(m['base']+f"/incidents/{row['id']}").status_code==404


def connect_incident(client,m,skill):
    return m['connect'](skill,['session.heartbeat','incident.report','incident.read','incident.ack'])


def test_signalement_agent_pause_notifications_reconnexion_et_thread(pg_client,messaging):
    m=messaging; session=connect_incident(pg_client,m,'backend'); qa=connect_incident(pg_client,m,'qa')
    values={'session_id':session['session']['id'],**incident_payload(m,responsible_profile_id=m['profiles']['qa'])}
    auth={'Authorization':'Bearer '+session['token']}; url=m['base']+'/agent-incidents'
    first=pg_client.post(url,json=values,headers=auth)
    assert first.status_code==201,first.text
    row=first.json()['incident']; assert row['content_status']=='Brouillon IA'
    assert pg_client.post(url,json=values,headers=auth).json()['created'] is False
    assert pg_client.post(url,json={**values,'priority':'P0'},headers=auth).status_code==409
    assert pg_client.post(url+f"/{row['id']}/resolve",json={'reason':'Agent'},headers=auth).status_code==404
    # Fil d'incident reste soumis aux gardes de coordination, sans commande implicite.
    message=emit(pg_client,m,channel_kind='thread',recipient_profile_id=None,attachment={'type':'technical_incident','id':row['id']})
    assert message['channel_id']
    async def pause(db): (await db.get(FlowRun,m['run'])).status='paused'
    asyncio.run(with_db(pause))
    paused=pg_client.post(url,json={**values,'request_key':str(uuid4())},headers=auth)
    assert paused.status_code==201,paused.text
    assert pg_client.get(url+f"/{row['id']}?session_id={qa['session']['id']}",headers={'Authorization':'Bearer '+qa['token']}).status_code==200
    box=pg_client.post(url+'/notifications/poll',json={'session_id':qa['session']['id']},headers={'Authorization':'Bearer '+qa['token']})
    assert box.status_code==200,box.text
    notice=box.json()['notifications'][0]
    assert pg_client.post(url+f"/notifications/{notice['id']}/ack",json={'session_id':session['session']['id']},headers=auth).status_code==404
    newer=connect_incident(pg_client,m,'qa')
    ack=pg_client.post(url+f"/notifications/{notice['id']}/ack",json={'session_id':newer['session']['id']},headers={'Authorization':'Bearer '+newer['token']})
    assert ack.status_code==200,ack.text
    assert ack.json()['acknowledged_by_session_id']==newer['session']['id']
    again=pg_client.post(url+f"/notifications/{notice['id']}/ack",json={'session_id':qa['session']['id']},headers={'Authorization':'Bearer '+qa['token']})
    assert again.json()==ack.json()


def test_expiration_ne_resout_pas_incident_et_ack_idempotent(pg_client,messaging):
    m=messaging; row=human_create(pg_client,m)
    url=m['base']+'/incidents/notifications'
    notices=pg_client.get(url).json()['notifications']; n=notices[0]
    ack=pg_client.post(url+f"/{n['id']}/ack",headers={'X-CSRF-Token':login_token(pg_client)})
    assert ack.status_code==200
    assert pg_client.post(url+f"/{n['id']}/ack",headers={'X-CSRF-Token':login_token(pg_client)}).json()==ack.json()
    # Une nouvelle notification à laisser expirer, y compris dans l'historique.
    change(pg_client,m,row,'acknowledge')
    async def expire(db):
        for notice in await db.scalars(select(IncidentNotification)):
            notice.created_at=utcnow()-timedelta(days=2);notice.expires_at=utcnow()-timedelta(days=1)
    asyncio.run(with_db(expire))
    read=pg_client.get(url).json()['notifications']
    assert [v['status'] for v in read]==['acknowledged','expired']
    exp=read[1]
    refusal=pg_client.post(url+f"/{exp['id']}/ack",headers={'X-CSRF-Token':login_token(pg_client)})
    assert refusal.status_code==409
    assert pg_client.post(url+'/poll',headers={'X-CSRF-Token':login_token(pg_client)}).json()['notifications']==[]
    async def verify(db):
        assert (await db.get(TechnicalIncident,row['id'])).status=='acknowledged'
        assert (await db.get(IncidentNotification,exp['id'])).status=='expired'
        assert await db.scalar(select(func.count()).select_from(AuditEvent).where(
            AuditEvent.target_id==exp['id'],AuditEvent.action=='incident.notification.expire'))==1
    asyncio.run(with_db(verify))


@pytest.mark.parametrize('changes',[
    {'title':'x'*181},{'description':''},{'priority':'P4'},{'team_id':'incorrect'},{'extra':'refus'}])
def test_entrees_bornees(pg_client,messaging,changes):
    m=messaging
    result=pg_client.post(m['base']+'/incidents',json=incident_payload(m,**changes),headers={'X-CSRF-Token':login_token(pg_client)})
    assert result.status_code==422,result.text
    assert 'input' not in str(result.json())


def test_secret_source_etrangere_et_revision_perimee(pg_client,messaging):
    m=messaging
    values=incident_payload(m,description='token=credential-test-123456')
    result=pg_client.post(m['base']+'/incidents',json=values,headers={'X-CSRF-Token':login_token(pg_client)})
    assert result.status_code==409 and 'credential-test-123456' not in result.text
    row=human_create(pg_client,m)
    assert change(pg_client,m,row,'resolve',reason='Tests',source_refs=[{'type':'agent_task_run','id':str(uuid4())}]).status_code==409
    good=change(pg_client,m,row,'acknowledge'); assert good.status_code==200
    assert change(pg_client,m,row,'resolve',reason='Tests').status_code==409
    assert good.json()['events'][0]['snapshot']['status']=='open'


def test_concurrence_creation_et_decision(pg_client,messaging):
    m=messaging; values=incident_payload(m)
    from app.incident_service import create_incident, change_incident
    from app.identity_service import database_factory
    async def report():
        factory=database_factory()
        try:
            async with factory() as db:
                row,created=await create_incident(db,m['project'],values,m['manager'])
                await db.commit()
                return row.id,created
        finally: await factory.kw['bind'].dispose()
    with ThreadPoolExecutor(max_workers=2) as pool:
        results=list(pool.map(lambda _:asyncio.run(report()),range(2)))
    assert results[0][0]==results[1][0] and sum(r[1] for r in results)==1
    iid=results[0][0]
    async def resolve():
        factory=database_factory()
        try:
            async with factory() as db:
                row=await change_incident(db,m['project'],iid,'resolve',
                    {'expected_revision':1,'reason':'Tests vérifiés','source_refs':[]},m['manager'])
                await db.commit()
                return row.revision
        finally: await factory.kw['bind'].dispose()
    with ThreadPoolExecutor(max_workers=2) as pool:
        revisions=list(pool.map(lambda _:asyncio.run(resolve()),range(2)))
    assert revisions==[2,2]
    async def verify(db):
        assert await db.scalar(select(func.count()).select_from(IncidentEvent).where(IncidentEvent.incident_id==iid))==2
    asyncio.run(with_db(verify))


def test_migration_retour_preserve_coordination_et_taches(pg_client,messaging):
    m=messaging;row=human_create(pg_client,m)
    msg=emit(pg_client,m,channel_kind='thread',recipient_profile_id=None,attachment={'type':'technical_incident','id':row['id']})
    subprocess.run([sys.executable,'-m','alembic','downgrade','0018_agent_conversations'],cwd=ROOT,check=True)
    async def check(db):
        assert await db.scalar(text("SELECT to_regclass('conversation.technical_incident')")) is None
        assert await db.scalar(text('SELECT count(*) FROM conversation.agent_message'))==1
        assert await db.scalar(text('SELECT count(*) FROM agent_task'))==3
        assert await db.scalar(text('SELECT attachment FROM conversation.coordination_channel')) is None
    asyncio.run(with_db(check))
    subprocess.run([sys.executable,'-m','alembic','upgrade','head'],cwd=ROOT,check=True)
    async def verify(db):
        assert await db.scalar(select(func.count()).select_from(TechnicalIncident))==0
        assert 'incident.report' in (await db.get(SkillProfile,m['profiles']['backend'])).allowed_capabilities
        assert 'incident.report' not in (await db.get(AgentSession,m['sessions']['backend']['session']['id'])).capabilities
    asyncio.run(with_db(verify))


def test_capacites_et_boites_isolees_entre_equipes_du_meme_profil(pg_client,messaging):
    m=messaging; row=human_create(pg_client,m)
    url=m['base']+'/agent-incidents'
    own=m['sessions']['backend']
    denied=pg_client.post(url,json={'session_id':own['session']['id'],**incident_payload(m)},headers=headers(m))
    assert denied.status_code==403  # Session antérieure sans capacité nouvellement négociée.
    from app.agent_models import AgentTeam, AgentTeamSkill
    async def other_team(db):
        team=AgentTeam(project_id=m['project'],name='Autre équipe',created_by=m['manager'])
        db.add(team);await db.flush()
        db.add(AgentTeamSkill(team_id=team.id,skill_profile_id=m['profiles']['backend']))
        return team.id
    tid=asyncio.run(with_db(other_team))
    csrf=login_token(pg_client)
    session=pg_client.post(m['base']+'/agent-sessions',headers={'X-CSRF-Token':csrf},json={
        'team_id':tid,'client_code':'codex','skill_code':'backend','capabilities':['session.heartbeat','incident.read','incident.ack']}).json()
    auth={'Authorization':'Bearer '+session['token']};body={'session_id':session['session']['id']}
    assert pg_client.post(url+'/notifications/poll',json=body,headers=auth).json()['notifications']==[]
    assert pg_client.get(url+f"/{row['id']}?session_id={body['session_id']}",headers=auth).status_code==404
    async def notice(db):
        return await db.scalar(select(IncidentNotification.id).where(IncidentNotification.incident_id==row['id'],
            IncidentNotification.recipient_profile_id==m['profiles']['backend']))
    nid=asyncio.run(with_db(notice))
    assert pg_client.post(url+f'/notifications/{nid}/ack',json=body,headers=auth).status_code==404


def test_architecte_signale_sans_resoudre_et_preuve_resolution(pg_client,messaging):
    m=messaging
    from app.identity_models import ProjectMember,User
    async def people(db):
        architect=await db.scalar(select(User).join(ProjectMember,ProjectMember.user_id==User.id).where(
            ProjectMember.project_id==m['project'],ProjectMember.role_code=='architect'))
        manager=await db.get(User,m['manager'])
        attempt=await db.scalar(select(AgentTaskRun.id).where(AgentTaskRun.task_id==m['tasks']['backend']))
        return architect.email,manager.email,attempt
    from app.agent_models import AgentTaskRun
    ae,me,attempt=asyncio.run(with_db(people))
    pg_client.cookies.clear();login(pg_client,ae)
    row=human_create(pg_client,m)
    assert change(pg_client,m,row,'resolve',reason='Tests').status_code==403
    pg_client.cookies.clear();login(pg_client,me)
    result=change(pg_client,m,row,'resolve',reason='Tests',source_refs=[{'type':'agent_task_run','id':attempt}])
    assert result.status_code==200,result.text
    assert result.json()['events'][-1]['source_refs']==[{'type':'agent_task_run','id':attempt}]
