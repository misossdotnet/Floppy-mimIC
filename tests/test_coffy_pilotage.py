"""COFFY-05 : projection Pilotage sous ACL et cohorte filtrée (PostgreSQL opt-in)."""

import asyncio
from datetime import timedelta
from uuid import uuid4

from bs4 import BeautifulSoup
from sqlalchemy import delete, func, select

from app.coffy_models import (ScheduledLLMFeedback, ScheduledLLMOccurrence,
    ScheduledLLMResultVersion, ScheduledLLMRun, ScheduledLLMTaskVersion)
from app.identity_models import AuditEvent, ProjectMember, RolePermission, utcnow
from app.llm_models import LLMProfileVersion
from .conftest import create_user, with_db
from .test_coffy_tasks import fields, setup
from .test_identity_acl import login


def create_task(client, state, **changes):
    response = client.post(state['base'], json=fields(state, **changes),
        headers={'X-CSRF-Token': state['csrf']})
    assert response.status_code == 201, response.text
    return response.json()['id']


def pilotage(client, project, **params):
    response = client.get(f'/api/projects/{project}/pilotage', params=params)
    assert response.status_code == 200, response.text
    assert response.headers['cache-control'] == 'no-store'
    return response.json()


def test_coffy_agregats_filtres_cartes_et_avis(pg_client):
    state = setup(pg_client)
    subject = pg_client.post(state['subjects'], json={'title': 'Qualité', 'description': ''},
        headers={'X-CSRF-Token': state['csrf']})
    assert subject.status_code == 201
    subject_id = subject.json()['id']
    task_a = create_task(pg_client, state, subject_ids=[subject_id])
    task_b = create_task(pg_client, state, title='Veille locale')

    async def seed(db):
        second = LLMProfileVersion(profile_id=str(uuid4()), version=1, name='Local',
            provider='ollama', model_name='local-test', base_url=None, credential_env=None,
            temperature=0.4, max_output_tokens=1000, timeout_seconds=20, max_retries=0,
            enabled=True, is_current=True, created_by=state['manager_id'])
        db.add(second)
        await db.flush()
        versions = {row.task_id: row for row in (await db.scalars(select(ScheduledLLMTaskVersion)
            .where(ScheduledLLMTaskVersion.task_id.in_([task_a, task_b])))).all()}
        now = utcnow()
        sequence = 0

        async def make(task, profile, status, *, days=1, error=None, seconds=None):
            nonlocal sequence
            sequence += 1
            stamp = now - timedelta(days=days) + timedelta(seconds=sequence)
            occurrence = ScheduledLLMOccurrence(project_id=state['project_id'], task_id=task,
                task_version_id=versions[task].id, scheduled_at=stamp,
                origin='manual', request_key=str(uuid4()), status='executed',
                coalesced_count=0, count_truncated=False)
            db.add(occurrence)
            await db.flush()
            row = ScheduledLLMRun(occurrence_id=occurrence.id, project_id=state['project_id'],
                task_id=task, task_version_id=versions[task].id,
                llm_profile_version_id=profile, status=status, error_code=error,
                parameters={'max_output_tokens': 100}, created_at=stamp,
                started_at=stamp if seconds is not None else None,
                completed_at=stamp + timedelta(seconds=seconds) if seconds is not None else None,
                result_current_version=2 if status == 'success' and days == 1 else
                                       1 if status == 'success' else 0,
                raw_output='Réponse' if status == 'success' else None)
            db.add(row)
            await db.flush()
            if status == 'success':
                first = ScheduledLLMResultVersion(run_id=row.id, version=1,
                    content='Réponse', status='Brouillon IA')
                db.add(first)
                await db.flush()
                if days == 1:
                    second_result = ScheduledLLMResultVersion(run_id=row.id, version=2,
                        parent_version_id=first.id, content='Correction', status='À revoir',
                        actor_id=state['manager_id'])
                    db.add(second_result)
                    await db.flush()
                    db.add_all([
                        ScheduledLLMFeedback(result_version_id=first.id,
                            author_id=state['manager_id'], score=1, note='Ancien'),
                        ScheduledLLMFeedback(result_version_id=second_result.id,
                            author_id=state['manager_id'], score=5, note='Révisé'),
                    ])
                return row.id, first.id
            return row.id, None

        success, _ = await make(task_a, state['profile_id'], 'success', seconds=10)
        failed, _ = await make(task_a, state['profile_id'], 'failed', error='invalid_json', seconds=20)
        timeout, _ = await make(task_b, second.id, 'timeout', error='timeout', seconds=30)
        pending, _ = await make(task_a, state['profile_id'], 'pending')
        old, _ = await make(task_b, second.id, 'success', days=40, seconds=40)
        await make(task_b, second.id, 'pending', days=-1)
        db.add_all([
            ScheduledLLMOccurrence(project_id=state['project_id'], task_id=task_a,
                task_version_id=versions[task_a].id,
                scheduled_at=now-timedelta(days=1)+timedelta(seconds=100),
                created_at=now-timedelta(days=1), origin='scheduled', status='skipped',
                skip_reason='backlog_coalesced', coalesced_count=3, count_truncated=False),
            ScheduledLLMOccurrence(project_id=state['project_id'], task_id=task_b,
                task_version_id=versions[task_b].id,
                scheduled_at=now-timedelta(days=40)+timedelta(seconds=100),
                created_at=now-timedelta(days=40), origin='scheduled', status='skipped',
                skip_reason='backlog_coalesced', coalesced_count=2, count_truncated=False),
        ])
        return success, failed, timeout, pending, old, second.id

    success, failed, timeout, pending, old, local_profile = asyncio.run(with_db(seed))
    data = pilotage(pg_client, state['project_id'])
    c = data['coffy']
    assert c['volume'] == 4 and c['terminal_count'] == 3
    assert c['statuses']['success'] == 1 and c['statuses']['failed'] == 1
    assert c['statuses']['timeout'] == 1 and c['statuses']['pending'] == 1
    assert c['success_rate_percent'] == 33.3
    assert c['duration'] == {'count': 3, 'mean_seconds': 20.0,
                             'min_seconds': 10.0, 'max_seconds': 30.0}
    assert c['satisfaction'] == {'feedback_count': 1, 'rated_runs': 1,
        'mean_score': 5.0, 'positive_rate_percent': 100.0}
    assert c['skipped'] == {'occurrences': 1, 'coalesced_due': 3, 'count_truncated': False}
    assert {item['code'] for item in c['errors']} == {'invalid_json', 'timeout'}
    assert {item['provider']: item['runs'] for item in c['by_provider']} == {'openai': 3, 'ollama': 1}
    cards = pilotage(pg_client, state['project_id'], kind='coffy')['activities']
    assert len(cards) == 4 and old not in {item['id'] for item in cards}
    assert next(item for item in cards if item['id'] == success)['document_status'] == 'À revoir'
    assert next(item for item in cards if item['id'] == failed)['state'] == 'Échec'
    assert pg_client.get(next(item for item in cards if item['id'] == success)['href']).status_code == 200
    assert pilotage(pg_client, state['project_id'], kind='coffy', state='Échec')['total'] == 2
    assert pilotage(pg_client, state['project_id'], coffy_subject=subject_id)['coffy']['volume'] == 3
    assert pilotage(pg_client, state['project_id'], coffy_subject=subject_id)['coffy']['skipped']['occurrences'] == 1
    assert pilotage(pg_client, state['project_id'], coffy_provider='ollama')['coffy']['volume'] == 1
    assert pilotage(pg_client, state['project_id'], coffy_provider='ollama')['coffy']['skipped']['occurrences'] == 0
    assert pilotage(pg_client, state['project_id'], coffy_profile=local_profile)['coffy']['volume'] == 1
    assert pilotage(pg_client, state['project_id'], coffy_status='failed')['coffy']['success_rate_percent'] == 0.0
    assert pilotage(pg_client, state['project_id'], coffy_status='failed')['coffy']['skipped']['occurrences'] == 0
    assert pilotage(pg_client, state['project_id'], coffy_period='all')['coffy']['volume'] == 5
    assert pilotage(pg_client, state['project_id'], coffy_period='all')['coffy']['skipped']['occurrences'] == 2
    html = pg_client.get(f'/projects/{state["project_id"]}/pilotage')
    assert html.status_code == 200 and 'Avis et satisfaction' in html.text
    assert html.headers['cache-control'] == 'no-store'
    assert 'Taux de réussite' in html.text and 'Non disponible' in html.text
    assert 'Ancien' not in str(data) and 'Révisé' not in html.text
    soup = BeautifulSoup(html.text, 'html.parser')
    section = soup.find('section', {'aria-labelledby': 'pilotage-coffy-title'})
    assert section is not None
    assert all(control.find_parent('label') for control in section.find_all('select'))
    assert all(link.get('href', '').startswith('/projects/')
               for link in soup.select('.pilotage-list article a'))


def test_coffy_pilotage_acl_et_get_sans_mutation(pg_client):
    state = setup(pg_client)
    create_task(pg_client, state)
    async def audit_count(db):
        return await db.scalar(select(func.count()).select_from(AuditEvent))
    before = asyncio.run(with_db(audit_count))
    pilotage(pg_client, state['project_id'])
    pg_client.get(f'/projects/{state["project_id"]}/pilotage')
    assert asyncio.run(with_db(audit_count)) == before
    reader_id, reader_email = create_user()
    async def membership(db):
        db.add(ProjectMember(project_id=state['project_id'], user_id=reader_id,
                             role_code='reader', is_active=True))
    asyncio.run(with_db(membership))
    login(pg_client, reader_email)
    assert pilotage(pg_client, state['project_id'])['coffy_read'] is True
    async def revoke(db):
        await db.execute(delete(RolePermission).where(RolePermission.role_code == 'reader',
            RolePermission.permission_code == 'scheduled_task.read'))
    asyncio.run(with_db(revoke))
    hidden = pilotage(pg_client, state['project_id'])
    assert hidden['coffy_read'] is False and hidden['coffy'] is None
    assert hidden['availability']['coffy'] == 'forbidden'
    other_id, other_email = create_user()
    login(pg_client, other_email)
    assert pg_client.get(f'/api/projects/{state["project_id"]}/pilotage').status_code == 404
    assert pg_client.get(f'/api/projects/{state["project_id"]}/pilotage',
        params={'coffy_period': 'invalid'}).status_code == 422
