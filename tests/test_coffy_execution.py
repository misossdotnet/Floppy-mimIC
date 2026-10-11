"""COFFY-03 : réservation idempotente, gardes et traces sans appel fournisseur réel."""

import asyncio
from datetime import timedelta
from decimal import Decimal
from uuid import uuid4

from sqlalchemy import select

from app.budget_models import ExecutionBudget
from app.coffy_execution import dispatch_pending, execute_run, reserve_due
from app.coffy_models import ScheduledLLMOccurrence, ScheduledLLMRun, ScheduledLLMTask, ScheduledLLMTaskVersion
from app.identity_models import AuditEvent, utcnow
from app.identity_service import database_factory
from app.llm_models import LLMCallAttempt
from app.llm_models import LLMProfileVersion
from app.llm_service import LLMResult
from app.runtime_models import RuntimeQuota, RuntimeUsage
from .conftest import with_db
from .test_coffy_tasks import fields, setup


def _create(client, state, mode="manual", expression=None, strict_json=False):
    headers = {"X-CSRF-Token": state["csrf"]}
    payload = fields(state, schedule_mode=mode, schedule_expression=expression, strict_json=strict_json)
    created = client.post(state["base"], json=payload, headers=headers)
    assert created.status_code == 201, created.text
    task_id = created.json()["id"]
    active = client.post(f'{state["base"]}/{task_id}/activate',
                         json={"expected_revision": 1}, headers=headers)
    assert active.status_code == 200, active.text
    return task_id


def test_run_manuel_idempotent_trace_et_sortie_non_validee(pg_client, monkeypatch):
    state = setup(pg_client)
    task_id = _create(pg_client, state)
    async def no_dispatch(*args, **kwargs):
        return None
    monkeypatch.setattr("app.coffy_api.dispatch_pending", no_dispatch)
    key = str(uuid4())
    url = f'{state["base"]}/{task_id}/run-now'
    headers = {"X-CSRF-Token": state["csrf"]}
    payload = {"expected_revision": 2, "request_key": key}
    first = pg_client.post(url, json=payload, headers=headers)
    assert first.status_code == 202, first.text
    again = pg_client.post(url, json=payload, headers=headers)
    assert again.status_code == 202 and again.json()["id"] == first.json()["id"]
    assert pg_client.post(url, json={"expected_revision": 2, "request_key": str(uuid4())},
                          headers=headers).status_code == 409
    calls = []
    async def invoke(profile, messages, **kwargs):
        calls.append(messages)
        return LLMResult("Proposition à relire", profile.id, profile.provider, profile.model_name, 12, 8)
    async def exercise():
        factory = database_factory()
        try:
            await execute_run(factory, first.json()["id"], invoke=invoke)
            await execute_run(factory, first.json()["id"], invoke=invoke)
        finally:
            await factory.kw["bind"].dispose()
    asyncio.run(exercise())
    assert len(calls) == 1
    detail = pg_client.get(f'{state["base"]}/{task_id}/runs/{first.json()["id"]}')
    assert detail.status_code == 200
    assert detail.json()["run"]["status"] == "success"
    assert detail.json()["run"]["output_status"] == "Brouillon IA — non validé"
    assert detail.json()["run"]["task_version_id"]
    assert detail.json()["run"]["llm_profile_version_id"] == state["profile_id"]
    assert len(detail.json()["attempts"]) == 1
    assert detail.json()["attempts"][0]["status"] == "completed"
    async def evidence(db):
        usages = (await db.scalars(select(RuntimeUsage))).all()
        assert len(usages) == 1 and usages[0].context["task"] == task_id
        assert usages[0].context["coffy_run"] == first.json()["id"]
        events = (await db.scalars(select(AuditEvent).where(
            AuditEvent.target_id == first.json()["id"]))).all()
        assert any(row.action == "scheduled_task.run.complete" for row in events)
    asyncio.run(with_db(evidence))


def test_tick_rattrapage_borne_et_double_worker(pg_client):
    state = setup(pg_client)
    task_id = _create(pg_client, state, "interval", "60")
    async def backdate(db):
        task = await db.get(ScheduledLLMTask, task_id)
        version = await db.scalar(select(ScheduledLLMTaskVersion).where(
            ScheduledLLMTaskVersion.task_id == task_id))
        version.created_at = utcnow() - timedelta(minutes=6)
        task.next_run_at = version.created_at + timedelta(minutes=1)
    asyncio.run(with_db(backdate))
    async def exercise():
        factory = database_factory()
        calls = []
        try:
            async with factory() as db:
                first = await reserve_due(db)
                await db.commit()
            async with factory() as db:
                second = await reserve_due(db)
                await db.commit()
            assert len(first) == 1 and second == []
            sent = []
            await dispatch_pending(factory, send=sent.append, run_ids=first)
            async def invoke(profile, messages, **kwargs):
                calls.append(True)
                return LLMResult("{}", profile.id, profile.provider, profile.model_name, 10, 5)
            await execute_run(factory, first[0], invoke=invoke)
            await execute_run(factory, first[0], invoke=invoke)
            assert sent == first and len(calls) == 1
            async with factory() as db:
                occurrences = (await db.scalars(select(ScheduledLLMOccurrence).where(
                    ScheduledLLMOccurrence.task_id == task_id))).all()
                assert len(occurrences) == 2
                assert {row.status for row in occurrences} == {"skipped", "executed"}
                assert next(row for row in occurrences if row.status == "skipped").coalesced_count >= 1
                run = await db.get(ScheduledLLMRun, first[0])
                assert run.status == "success" and run.task_version_id == occurrences[1].task_version_id
        finally:
            await factory.kw["bind"].dispose()
    asyncio.run(exercise())


def test_budget_refuse_avant_fournisseur(pg_client, monkeypatch):
    state = setup(pg_client)
    task_id = _create(pg_client, state)
    async def no_dispatch(*args, **kwargs):
        return None
    monkeypatch.setattr("app.coffy_api.dispatch_pending", no_dispatch)
    async def limit(db):
        db.add(ExecutionBudget(project_id=state["project_id"], scope="task", scope_id=task_id,
            version=1, period="daily", metric="tokens", amount=Decimal(1), currency=None,
            warning_percent=80, enabled=True, is_current=True, reason="Test", created_by=state["manager_id"]))
    asyncio.run(with_db(limit))
    response = pg_client.post(f'{state["base"]}/{task_id}/run-now',
        json={"expected_revision": 2, "request_key": str(uuid4())},
        headers={"X-CSRF-Token": state["csrf"]})
    assert response.status_code == 202
    async def exercise():
        factory = database_factory()
        called = []
        async def invoke(*args, **kwargs):
            called.append(True)
        try:
            await execute_run(factory, response.json()["id"], invoke=invoke)
            async with factory() as db:
                run = await db.get(ScheduledLLMRun, response.json()["id"])
                assert run.status == "failed" and run.error_code == "budget_exhausted"
                assert not (await db.scalars(select(LLMCallAttempt).where(
                    LLMCallAttempt.scheduled_llm_run_id == run.id))).all()
            assert called == []
        finally:
            await factory.kw["bind"].dispose()
    asyncio.run(exercise())


def test_quota_refuse_et_profil_revoque(pg_client, monkeypatch):
    state = setup(pg_client)
    task_id = _create(pg_client, state)
    async def no_dispatch(*args, **kwargs):
        return None
    monkeypatch.setattr("app.coffy_api.dispatch_pending", no_dispatch)
    async def limit(db):
        db.add(RuntimeQuota(project_id=state["project_id"], scope="task", scope_id=task_id,
            version=1, max_tokens=1, is_current=True, reason="Test", created_by=state["manager_id"]))
    asyncio.run(with_db(limit))
    headers = {"X-CSRF-Token": state["csrf"]}
    first = pg_client.post(f'{state["base"]}/{task_id}/run-now',
        json={"expected_revision": 2, "request_key": str(uuid4())}, headers=headers)
    assert first.status_code == 202
    async def exercise(run_id):
        factory = database_factory()
        called = []
        async def invoke(*args, **kwargs):
            called.append(True)
        try:
            await execute_run(factory, run_id, invoke=invoke)
            async with factory() as db:
                return (await db.get(ScheduledLLMRun, run_id)).error_code, called
        finally:
            await factory.kw["bind"].dispose()
    assert asyncio.run(exercise(first.json()["id"])) == ("quota_tokens", [])
    async def remove_quota(db):
        quota = await db.scalar(select(RuntimeQuota).where(RuntimeQuota.scope_id == task_id))
        quota.is_current = False
    asyncio.run(with_db(remove_quota))
    second = pg_client.post(f'{state["base"]}/{task_id}/run-now',
        json={"expected_revision": 2, "request_key": str(uuid4())}, headers=headers)
    assert second.status_code == 202
    async def revoke(db):
        profile = await db.get(LLMProfileVersion, state["profile_id"])
        profile.enabled = False
    asyncio.run(with_db(revoke))
    assert asyncio.run(exercise(second.json()["id"])) == ("profile_unavailable", [])
    third = pg_client.post(f'{state["base"]}/{task_id}/run-now',
        json={"expected_revision": 2, "request_key": str(uuid4())}, headers=headers)
    assert third.status_code == 409  # L'API refuse avant de réserver le run.


def test_json_strict_invalide_ne_produit_pas_de_resultat(pg_client, monkeypatch):
    state = setup(pg_client)
    task_id = _create(pg_client, state, strict_json=True)
    async def no_dispatch(*args, **kwargs):
        return None
    monkeypatch.setattr("app.coffy_api.dispatch_pending", no_dispatch)
    response = pg_client.post(f'{state["base"]}/{task_id}/run-now',
        json={"expected_revision": 2, "request_key": str(uuid4())},
        headers={"X-CSRF-Token": state["csrf"]})
    assert response.status_code == 202
    async def invoke(profile, messages, **kwargs):
        return LLMResult("pas du JSON", profile.id, profile.provider, profile.model_name, 3, 3)
    async def exercise():
        factory = database_factory()
        try:
            await execute_run(factory, response.json()["id"], invoke=invoke)
        finally:
            await factory.kw["bind"].dispose()
    asyncio.run(exercise())
    detail = pg_client.get(f'{state["base"]}/{task_id}/runs/{response.json()["id"]}')
    assert detail.json()["run"]["status"] == "failed"
    assert detail.json()["run"]["error_code"] == "invalid_json"
    assert detail.json()["run"]["raw_output"] is None
