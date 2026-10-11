"""AGENT-25 : lecture minimisée, états effectifs, ACL et liens des projections."""
import asyncio
from datetime import timedelta
from types import SimpleNamespace
from uuid import uuid4

import pytest
from bs4 import BeautifulSoup
from sqlalchemy import delete, func, select

from app.agent_models import AgentActionApproval, AgentPlanVersion, AgentSession, AgentTask, AgentTaskRun, FlowRun
from app.business_models import AnalysisRun, Deliverable, DeliverableVersion, SourceDocument
from app.conversation_models import AgentMessage, AgentMessageDelivery
from app.git_models import CodeChange, CodeReview, CodeWorkflow, GitOperation, GitWorktree, Repository
from app.identity_models import AuditEvent, Project, ProjectMember, RolePermission, utcnow
from app.pilotage_service import task_blockers
from app.runtime_models import RuntimeQuota, RuntimeSelection, RuntimeUsage
from app.llm_models import LLMProfileVersion
from .conftest import create_user, with_db
from .test_agent_conversations import messaging, emit, poll, ack
from .test_agent_incidents import human_create
from .test_identity_acl import login
from .test_project_memory import setup


def urls(m):
    return f"/projects/{m['project']}/pilotage", f"/api/projects/{m['project']}/pilotage"


def read(client, m, **params):
    response = client.get(urls(m)[1], params=params)
    assert response.status_code == 200, response.text
    assert response.headers['cache-control'] == 'no-store'
    return response.json()


def test_vide_refus_et_validation_sans_echo_du_parametre(pg_client):
    m = setup(pg_client); html, api = urls(m)
    data = read(pg_client, m)
    assert data['activities'] == [] and data['total'] == 0
    assert data['counters']['active_agents'] == 0
    assert data['availability'] == {'budgets':'available','budget_alerts':'available',
                                    'coffy':'available'}
    assert data['consumption']['cost'] is None
    page = pg_client.get(html)
    assert page.status_code == 200 and 'Mode liste' in page.text and 'Non disponible' in page.text
    nav=BeautifulSoup(page.text,'html.parser').select_one(f'nav[aria-label="Navigation principale"] a[href="{html}"]')
    assert nav and nav.get('aria-current')=='page'
    assert html in pg_client.get(f"/projects/{m['project']}").text
    assert 'Aucune activité ne correspond' in page.text and '<script' not in page.text
    for params in ({'offset':-1}, {'offset':10001}, {'kind':'token=secret-injecte'}, {'state':'inconnu'}):
        response = pg_client.get(api, params=params)
        assert response.status_code == 422
        assert 'secret-injecte' not in response.text
        assert response.headers['cache-control'] == 'no-store'
    unknown = str(uuid4())
    assert pg_client.get(html, params={'project':unknown}).status_code == 404
    assert pg_client.get(f'/api/projects/{unknown}/pilotage').status_code == 404
    _, other = create_user(); login(pg_client, other)
    assert pg_client.get(api).status_code == 404
    assert pg_client.get(html).status_code == 404
    pg_client.cookies.clear()
    assert pg_client.get(api).status_code == 401
    assert pg_client.get(html).headers['location'] == '/login'


def test_etats_effectifs_et_get_sans_effet_sur_claims_receipts_accords_audit(pg_client, messaging):
    m = messaging
    message = emit(pg_client, m, content='CONTENU_PRIVE_NON_PROJETE', summary='RESUME_NON_PROJETE')
    async def prepare(db):
        session = await db.get(AgentSession, m['sessions']['backend']['session']['id'])
        session.expires_at = utcnow()-timedelta(seconds=1)
        return session.token_hash
    token_hash = asyncio.run(with_db(prepare))
    async def snapshot(db):
        session = await db.get(AgentSession, m['sessions']['backend']['session']['id'])
        task = await db.get(AgentTask, m['tasks']['backend'])
        receipt = await db.scalar(select(AgentMessageDelivery).where(AgentMessageDelivery.message_id==message['id']))
        return (session.status, task.status, receipt.status,
            await db.scalar(select(func.count()).select_from(AgentActionApproval)),
            await db.scalar(select(func.count()).select_from(AuditEvent)))
    before = asyncio.run(with_db(snapshot))
    data = read(pg_client, m)
    assert data['counters']['active_agents'] == 2 and data['counters']['expired_sessions'] == 1
    assert data['counters']['blocked_tasks'] == 1 and data['counters']['unread_messages'] == 1
    assert any('Session déconnectée ou expirée' in c['details'] for c in data['activities'])
    page = pg_client.get(urls(m)[0])
    assert page.status_code == 200
    for secret in ('CONTENU_PRIVE_NON_PROJETE','RESUME_NON_PROJETE',token_hash,m['sessions']['backend']['token']):
        assert secret not in page.text and secret not in str(data)
    assert asyncio.run(with_db(snapshot)) == before
    assert poll(pg_client, m).status_code == 200
    data = read(pg_client, m, kind='messages')
    assert data['total'] == 1 and data['activities'][0]['details'] == ['Livré, non acquitté']
    assert ack(pg_client, m, message).status_code == 200
    assert read(pg_client, m)['counters']['unread_messages'] == 0
    expired = emit(pg_client, m, skill='qa')
    async def expire(db):
        row=await db.get(AgentMessage, expired['id'])
        row.created_at=utcnow()-timedelta(minutes=2)
        row.expires_at=utcnow()-timedelta(seconds=1)
    asyncio.run(with_db(expire))
    assert read(pg_client, m, kind='messages')['total'] == 0


def test_compteur_agents_distincts_et_broadcast_par_destinataire(pg_client, messaging):
    m = messaging
    m['connect']('frontend')
    emit(pg_client, m, channel_kind='broadcast', recipient_profile_id=None)
    data = read(pg_client, m, kind='messages')
    assert data['counters']['active_agents'] == 3 and data['counters']['active_sessions'] == 4
    assert data['counters']['unread_messages'] == 1
    assert data['counters']['unacknowledged_deliveries'] == 6
    assert data['total'] == 6


def test_revues_courantes_worktree_alertes_quotas_et_liens(pg_client, messaging):
    m = messaging
    incident = human_create(pg_client, m, priority='P0')
    async def prepare(db):
        repo = Repository(project_id=m['project'],catalog_key='recette',physical_identity='a'*64,
                          policy_fingerprint='b'*64,created_by=m['manager'])
        db.add(repo); await db.flush()
        tree = GitWorktree(project_id=m['project'],repository_id=repo.id,task_id=m['tasks']['backend'],
            branch='codex/recette',base_commit='a'*40,status='ready',created_by=m['manager'])
        db.add(tree); await db.flush()
        db.add(CodeWorkflow(worktree_id=tree.id))
        changes=[]
        for revision in (1,2):
            change=CodeChange(project_id=m['project'],worktree_id=tree.id,revision=revision,head_commit='b'*40,
                content_fingerprint='c'*64,manifest={'secret':'MANIFESTE_NON_PROJETE'},created_by=m['manager'])
            db.add(change); await db.flush(); changes.append(change)
        old=CodeReview(project_id=m['project'],change_id=changes[0].id,reviewer_id=m['manager'])
        current=CodeReview(project_id=m['project'],change_id=changes[1].id,reviewer_id=m['manager'],status='changes_requested')
        db.add_all([old,current,GitOperation(project_id=m['project'],worktree_id=tree.id,kind='push',status='uncertain',
            request_key=str(uuid4()),request_fingerprint='d'*64,policy_fingerprint='b'*64,actor_id=m['manager'],
            parameters={'secret':'PARAMETRE_NON_PROJETE'},result={'secret':'RESULTAT_NON_PROJETE'})])
        db.add(RuntimeQuota(project_id=m['project'],scope='project',scope_id=m['project'],version=1,
            max_tokens=1000,reason='Quota de recette',created_by=m['manager']))
        attempt=await db.scalar(select(AgentTaskRun).where(AgentTaskRun.task_id==m['tasks']['frontend']))
        selection=RuntimeSelection(attempt_id=attempt.id,project_id=m['project'],origin='default',
            context={'task':attempt.task_id},deadline=utcnow()+timedelta(minutes=1))
        db.add(selection); await db.flush()
        db.add(RuntimeUsage(project_id=m['project'],external_selection_id=selection.id,context={'task':attempt.task_id},
            policy_ids=[],status='unknown',charged_tokens=100,charged_seconds=60,charged_cost=None))
        await db.flush()
        return tree.id,current.id
    tree, review = asyncio.run(with_db(prepare))
    data = read(pg_client, m)
    assert data['counters']['worktrees'] == 1 and data['counters']['reviews'] == 1
    assert data['counters']['quotas'] == 1 and data['counters']['alerts'] == 3
    assert data['consumption']['cost'] is None and data['consumption']['tokens'] is None
    assert all(s not in str(data) for s in ('MANIFESTE_NON_PROJETE','PARAMETRE_NON_PROJETE','RESULTAT_NON_PROJETE'))
    for kind in ('worktrees','reviews','quotas','alerts'):
        rows=read(pg_client,m,kind=kind)['activities']
        assert rows and all(c['kind']==kind for c in rows)
        for row in rows:
            linked=pg_client.get(row['href'])
            assert linked.status_code == 200, (row['href'],linked.text)
    async def ia_review(db):
        (await db.get(CodeReview,review)).session_id=m['sessions']['qa']['session']['id']
    asyncio.run(with_db(ia_review))
    consultative=read(pg_client,m,kind='reviews',state='En cours')['activities']
    assert len(consultative)==1 and 'Avis IA consultatif' in consultative[0]['details']
    assert consultative[0]['document_status'] is None
    async def finish(db):
        (await db.get(CodeReview,review)).status='reviewed'
        (await db.get(GitWorktree,tree)).status='removed'
        (await db.get(GitWorktree,tree)).removed_at=utcnow()
    asyncio.run(with_db(finish))
    data=read(pg_client,m)
    assert data['counters']['worktrees']==0 and data['counters']['reviews']==0


def test_filtres_pagination_stable_et_changement_projet_archive(pg_client, messaging):
    m=messaging
    async def prepare(db):
        (await db.get(FlowRun,m['run'])).status='paused'
        for i in range(25):
            db.add(AgentTask(project_id=m['project'],plan_version_id=m['plan'],team_id=m['team'],
                skill_profile_id=m['profiles']['qa'],task_key=f'extra-{i}',title=f'À examiner {i}'))
        other=Project(name='Autre projet autorisé',owner_id=m['manager'],description='')
        db.add(other); await db.flush()
        db.add(ProjectMember(project_id=other.id,user_id=m['manager'],role_code='project_manager'))
        return other.id
    other=asyncio.run(with_db(prepare))
    first=read(pg_client,m,kind='tasks',state='Décision humaine')
    second=read(pg_client,m,kind='tasks',state='Décision humaine',offset=20)
    assert first['total']==28 and len(first['activities'])==20 and len(second['activities'])==8
    assert {r['id'] for r in first['activities']}.isdisjoint(r['id'] for r in second['activities'])
    assert read(pg_client,m,kind='tasks',state='Décision humaine')['activities']==first['activities']
    assert read(pg_client,m,kind='tasks',state='Terminé')['total']==0
    html,_=urls(m)
    page=pg_client.get(html,params={'kind':'tasks','state':'Décision humaine'})
    soup=BeautifulSoup(page.text,'html.parser')
    link=soup.find('a',string='Page suivante')
    assert link and 'kind=tasks' in link['href'] and 'offset=20' in link['href']
    assert 'Page précédente' in pg_client.get(link['href']).text
    switch=pg_client.get(html,params={'project':other,'offset':20,'kind':'tasks'})
    assert switch.status_code==303 and switch.headers['location']==f'/projects/{other}/pilotage'
    assert 'Aucune activité' in pg_client.get(switch.headers['location']).text
    async def archive(db): (await db.get(Project,m['project'])).status='archived'
    asyncio.run(with_db(archive))
    assert pg_client.get(html).status_code==200


def test_permissions_objets_compteurs_masques_et_audit_du_refus(pg_client, messaging):
    m=messaging
    login(pg_client,m['reader_email'])
    assert read(pg_client,m)['counters']['active_sessions']==3
    async def revoke_read(db):
        await db.execute(delete(RolePermission).where(RolePermission.role_code=='reader',RolePermission.permission_code=='agent.read'))
    asyncio.run(with_db(revoke_read))
    data=read(pg_client,m)
    assert data['agent_read'] is False and data['consumption'] is None
    assert all(value is None for value in data['counters'].values()) and data['activities']==[]
    async def revoke_pilotage(db):
        await db.execute(delete(RolePermission).where(RolePermission.role_code=='reader',RolePermission.permission_code=='pilotage.read'))
    asyncio.run(with_db(revoke_pilotage))
    assert pg_client.get(urls(m)[1]).status_code==403
    async def denied(db):
        assert await db.scalar(select(AuditEvent.id).where(AuditEvent.action=='pilotage.read',AuditEvent.outcome=='denied'))
    asyncio.run(with_db(denied))


def test_documents_et_minimisation_des_titres_sans_contenu(pg_client,monkeypatch):
    m=setup(pg_client)
    monkeypatch.setenv('MIMIC_TEST_TOKEN','credential-fictif-pilotage-unique')
    async def prepare(db):
        row=SourceDocument(project_id=m['project'],created_by=m['manager_id'],name='<script>credential-fictif-pilotage-unique</script>',
            kind='need',mime_type='text/plain',sha256='a'*64,size_bytes=10,status='error',
            direct_text='SOURCE_PRIVEE_NON_PROJETEE',extracted_text='PROMPT_PRIVE_NON_PROJETE')
        db.add(row); await db.flush(); return row.id
    identity=asyncio.run(with_db(prepare))
    data=read(pg_client,m,kind='documents',state='Échec')
    assert data['total']==1 and data['activities'][0]['id']==identity
    page=pg_client.get(urls(m)[0])
    assert '&lt;script&gt;' in page.text and '<script>' not in page.text
    assert '[SECRET MASQUÉ]' in page.text
    assert all(s not in page.text+str(data) for s in ('credential-fictif-pilotage-unique','SOURCE_PRIVEE_NON_PROJETEE','PROMPT_PRIVE_NON_PROJETE'))


def test_refus_quota_avant_appel_et_statut_documentaire_distinct(pg_client):
    m=setup(pg_client)
    async def prepare(db):
        p=LLMProfileVersion(profile_id=str(uuid4()),version=1,name='Recette',provider='ollama',model_name='test',
            max_output_tokens=100,timeout_seconds=10,max_retries=0,enabled=False,is_current=True,created_by=m['manager_id'])
        source=SourceDocument(project_id=m['project'],created_by=m['manager_id'],name='Source',kind='need',
            mime_type='text/plain',sha256='a'*64,size_bytes=0,status='extracted')
        doc=Deliverable(project_id=m['project'],type='specification',title='Proposition à relire',
            current_version=1,created_by=m['manager_id'])
        db.add_all([p,source,doc]); await db.flush()
        db.add(AnalysisRun(project_id=m['project'],source_id=source.id,requested_by=m['manager_id'],
            llm_profile_version_id=p.id,status='failed',error_code='quota_tokens',prompt_template='PROMPT_NON_PROJETE'))
        version=DeliverableVersion(deliverable_id=doc.id,version=1,title=doc.title,content='CONTENU_NON_PROJETE',status='Brouillon IA')
        db.add(version); await db.flush(); return version.id
    vid=asyncio.run(with_db(prepare))
    data=read(pg_client,m)
    assert data['counters']['alerts']==1 and data['consumption']['count']==0
    assert data['consumption']['cost'] is None
    assert 'PROMPT_NON_PROJETE' not in str(data) and 'CONTENU_NON_PROJETE' not in str(data)
    version=next(c for c in data['activities'] if c['id']==vid)
    assert version['document_status']=='Brouillon IA' and version['state']=='Décision humaine'
    assert pg_client.get(version['href']).status_code==200
    async def persisted(db): assert (await db.get(DeliverableVersion,vid)).status=='Brouillon IA'
    asyncio.run(with_db(persisted))


@pytest.mark.parametrize('case,expected',[
    ('ready',[]),('parent',[]),('completed',[]),('dependency',['dependency:pre']),
    ('human',['approval_required']),('approved',[]),('paused',['run_inactive']),
    ('lease',['lease_expired']),('expired',['session_expired']),('max_expired',['session_expired']),('runtime',['runtime_active']),
    ('retry',['retry_delay']),('limit',['attempt_limit']),('parallel',['parallel_limit'])])
def test_blocages_feuilles_sans_demande_accord(case,expected):
    now=utcnow()
    task=SimpleNamespace(id='task',task_key='work',parent_task_id=None,dependency_keys=[],status='ready',
        attempt_count=0,retry_not_before=None,action_class='standard',claimed_session_id='session',
        lease_expires_at=now+timedelta(seconds=30))
    plan=SimpleNamespace(status='approved',max_attempts=3,max_parallel_tasks=2,autonomy_mode='bounded')
    tasks=[task]; approvals={}; sessions={'session':SimpleNamespace(status='active',expires_at=now+timedelta(seconds=60))}
    run='running'; active=set()
    if case=='parent': tasks.append(SimpleNamespace(**{**vars(task),'id':'child','task_key':'child','parent_task_id':task.id}))
    if case=='completed': task.status='completed'
    if case=='dependency':
        task.dependency_keys=['pre']; tasks.append(SimpleNamespace(**{**vars(task),'id':'pre','task_key':'pre','dependency_keys':[]}))
    if case in ('human','approved'):
        plan.autonomy_mode='per_action'
        if case=='approved': approvals[('task',1)]=SimpleNamespace(status='approved')
    if case=='paused': run='paused'
    if case in ('lease','expired','max_expired'):
        task.status='claimed'
        if case=='lease': task.lease_expires_at=now-timedelta(seconds=1)
        elif case=='expired': sessions['session'].expires_at=now-timedelta(seconds=1)
        else: sessions['session'].max_expires_at=now-timedelta(seconds=1)
    if case=='runtime': active.add(task.id)
    if case=='retry': task.retry_not_before=now+timedelta(seconds=30)
    if case=='limit': task.attempt_count=3
    if case=='parallel': plan.max_parallel_tasks=1; tasks.append(SimpleNamespace(**{**vars(task),'id':'other','task_key':'other','status':'claimed'}))
    assert task_blockers(task,tasks,plan,run,sessions,approvals,active,now)==expected
