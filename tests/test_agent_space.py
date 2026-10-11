"""AGENT-14 : routes HTML réelles, listes, filtres, formulaires et contrôle d'accès."""
import asyncio
from uuid import uuid4

from sqlalchemy import select

from app.identity_models import Project, utcnow
from app.memory_models import ProjectMemoryVersion
from .conftest import create_user, with_db
from .test_project_memory import setup, as_user, create, decide
from .test_agent_conversations import messaging, emit
from .test_agent_incidents import human_create, login_token
from .test_identity_acl import login


def test_navigation_listes_vides_acl_et_filtre_projet(pg_client):
    s=setup(pg_client); base=f"/projects/{s['project']}/agents"
    response=pg_client.get(base)
    assert response.status_code==200 and 'Espace Équipe IA' in response.text
    for view in ('memory','coordination','incidents'):
        page=pg_client.get(base+'/'+view)
        assert page.status_code==200,page.text
        assert 'Mode liste' in page.text and 'name="project"' in page.text
        assert '<script' not in page.text and 'no-store'==page.headers['cache-control']
    assert pg_client.get(base+'/memory?kind=&status=&q=').status_code==200
    assert pg_client.get(base+'/coordination?kind=').status_code==200
    assert pg_client.get(base+'/incidents?status=&priority=&profile_id=').status_code==200
    foreign=str(uuid4())
    assert pg_client.get(base+f'/memory?project={foreign}').status_code==404
    assert pg_client.get(base+f'/memory?selected={foreign}').status_code==404
    as_user(pg_client,s,'reader')
    assert 'Créer une proposition À revoir' not in pg_client.get(base+'/memory').text
    assert 'Signaler un incident technique' not in pg_client.get(base+'/incidents').text
    _,outsider=create_user();pg_client.cookies.clear();login(pg_client,outsider)
    for view in ('memory','coordination','incidents'):
        assert pg_client.get(base+'/'+view).status_code==404
    pg_client.cookies.clear()
    assert pg_client.get(base+'/memory').headers['location']=='/login'


def test_memoire_formulaires_version_reference_filtrage_et_erreurs(pg_client):
    s=setup(pg_client);base=f"/projects/{s['project']}/agents/memory"
    raw={'csrf_token':s['csrf'],'kind':'result','title':'Convention <script>alert(1)</script>',
         'content':'Source consultative','source_note':'Revue interne'}
    saved=pg_client.post(base,data=raw)
    assert saved.status_code==303,saved.text
    mid=saved.headers['location'].split('selected=')[1]
    detail=pg_client.get(saved.headers['location'])
    assert detail.status_code==200,detail.text
    assert '&lt;script&gt;' in detail.text and '<script>alert(1)</script>' not in detail.text
    assert 'Non disponible' in detail.text and 'Aucune référence active.' in detail.text
    approved=pg_client.post(base+f'/{mid}/approve',data={'csrf_token':s['csrf'],'expected_version':1,'reason':'Revu'})
    assert approved.status_code==303,approved.text
    assert 'v2 · Convention' in pg_client.get(approved.headers['location']).text
    corrected=pg_client.post(base+f'/{mid}/edit',data={**raw,'expected_version':2,'content':'Correction à relire'})
    assert corrected.status_code==303,corrected.text
    detail=pg_client.get(corrected.headers['location'])
    assert 'Correction à relire' in detail.text and 'Source consultative' in detail.text
    assert pg_client.get(base+'?kind=result&status=À+revoir&q=Convention').status_code==200
    assert 'Aucune mémoire' in pg_client.get(base+'?q=Inexistant').text
    conflict=pg_client.post(base+f'/{mid}/edit',data={**raw,'expected_version':1,'content':'Saisie conservée'})
    assert conflict.status_code==409,conflict.text
    assert 'Révision' not in conflict.text or 'Version périmée' in conflict.text
    assert 'Saisie conservée' in conflict.text and 'role="alert"' in conflict.text
    assert pg_client.post(base,data={**raw,'csrf_token':'invalide'}).status_code==403
    as_user(pg_client,s,'reader')
    assert pg_client.post(base,data={**raw,'csrf_token':s['csrf']}).status_code==403
    assert pg_client.post(base+f'/{mid}/approve',data={'csrf_token':s['csrf'],'expected_version':3}).status_code==403


def test_incidents_actions_revisions_notices_et_archive(pg_client,messaging):
    m=messaging;base=f"/projects/{m['project']}/agents/incidents";csrf=login_token(pg_client)
    raw={'csrf_token':csrf,'request_key':str(uuid4()),'team_id':m['team'],'responsible_profile_id':m['profiles']['backend'],
         'title':'Incident <img src=x onerror=alert(1)>','description':'Diagnostic proposé','priority':'P0'}
    created=pg_client.post(base,data=raw)
    assert created.status_code==303,created.text
    mid=created.headers['location'].split('selected=')[1]
    detail=pg_client.get(created.headers['location'])
    assert detail.status_code==200,detail.text
    assert '&lt;img' in detail.text and '<img src=x' not in detail.text
    assert 'Mes notifications internes' in detail.text and 'Escalader' in detail.text
    resolved=pg_client.post(base+f'/{mid}/resolve',data={'csrf_token':csrf,'expected_revision':1,'reason':'Tests vérifiés'})
    assert resolved.status_code==303,resolved.text
    read=pg_client.get(resolved.headers['location'])
    assert 'Résolu' in read.text and 'Tests vérifiés' in read.text
    assert 'name="expected_revision"' not in read.text
    assert 'Aucun incident' in pg_client.get(base+'?status=open').text
    assert mid in pg_client.get(base+'?status=resolved&priority=P0').text
    async def archive(db): (await db.get(Project,m['project'])).status='archived'
    asyncio.run(with_db(archive))
    assert 'Signaler un incident technique' not in pg_client.get(base).text
    assert pg_client.post(base,data={**raw,'request_key':str(uuid4())}).status_code==409


def test_coordination_lecture_sans_receipt_et_fil_incident(pg_client,messaging):
    m=messaging;row=human_create(pg_client,m)
    message=emit(pg_client,m,summary='Message <script>alert(1)</script>',channel_kind='thread',recipient_profile_id=None,
                 attachment={'type':'technical_incident','id':row['id']})
    base=f"/projects/{m['project']}/agents/coordination"
    page=pg_client.get(base+'?selected='+message['channel_id'])
    assert page.status_code==200,page.text
    assert '&lt;script&gt;' in page.text and 'Session de réception Aucune' in page.text
    assert f"incidents?selected={row['id']}" in page.text
    assert pg_client.get(m['base']+f"/agent-conversations/{message['channel_id']}").json()['messages'][0]['delivery_status']=='pending'
    assert 'Aucun canal' in pg_client.get(base+'?kind=direct').text
    assert pg_client.get(base+'?selected='+str(uuid4())).status_code==404


def test_pagination_memoire_et_projet_autorise(pg_client):
    s=setup(pg_client);base=f"/projects/{s['project']}/agents/memory"
    from app.memory_service import create_memory
    from .test_project_memory import fields
    async def prepare(db):
        for i in range(21):
            await create_memory(db,s['project'],'result',fields(title=f'Mémoire {i}'),s['manager_id'])
        other=Project(name='Autre projet autorisé',description='',owner_id=s['manager_id'])
        db.add(other);await db.flush()
        from app.identity_models import ProjectMember
        db.add(ProjectMember(project_id=other.id,user_id=s['manager_id'],role_code='project_manager'))
        return other.id
    other=asyncio.run(with_db(prepare))
    page=pg_client.get(base);assert 'Page suivante' in page.text
    assert 'Page précédente' in pg_client.get(base+'?offset=20').text
    switch=pg_client.get(base+'?project='+other)
    assert switch.status_code==303 and other in switch.headers['location']
    assert 'Autre projet autorisé' in pg_client.get(switch.headers['location']).text


def test_notifications_pagination_et_erreur_formulaire(pg_client,messaging):
    m=messaging;base=f"/projects/{m['project']}/agents/incidents"
    for i in range(21): human_create(pg_client,m,title=f'Incident {i}')
    page=pg_client.get(base)
    assert 'Notifications suivantes' in page.text
    assert 'Notifications précédentes' in pg_client.get(base+'?inbox_offset=20').text
    row=pg_client.get(m['base']+'/incidents').json()['incidents'][0]
    result=pg_client.post(base+f"/{row['id']}/resolve",data={'csrf_token':login_token(pg_client),
        'expected_revision':'texte-invalide','reason':'Raison conservée'})
    assert result.status_code==409 and 'Nombre ou version invalide' in result.text
    assert 'Raison conservée' in result.text


def test_globale_version_exacte_inutilisable_et_revocation_ecran(pg_client):
    from .test_project_memory import global_setup,share,edit
    s=setup(pg_client);g=global_setup(pg_client,s)
    mid=create(pg_client,g,kind='convention',title='Convention globale')['id']
    assert decide(pg_client,g,mid,'approve',1).status_code==200
    grant=share(pg_client,g,mid,s['project'],2).json()
    base=f"/projects/{s['project']}/agents/memory"
    as_user(pg_client,s,'reader')
    page=pg_client.get(base)
    assert 'Globale v2' in page.text and 'Référence utilisable' in page.text
    assert 'Révoquer ce partage' not in page.text
    as_user(pg_client,g,'admin')
    assert edit(pg_client,g,mid,2,content='Nouvelle convention').status_code==200
    assert decide(pg_client,g,mid,'approve',3).status_code==200
    as_user(pg_client,s,'manager')
    assert 'Partage inutilisable' in pg_client.get(base).text
    revoked=pg_client.post(f"/projects/{s['project']}/agents/memory-grants/{grant['id']}/revoke",data={'csrf_token':s['csrf']})
    assert revoked.status_code==303,revoked.text
    assert 'Révoquer ce partage' not in pg_client.get(base).text
