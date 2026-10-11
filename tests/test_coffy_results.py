"""COFFY-04 : résultat versionné, revue humaine et avis séparés."""

import asyncio
from uuid import uuid4

from sqlalchemy import select

from app.coffy_execution import execute_run
from app.coffy_models import ScheduledLLMResultVersion
from app.identity_models import AuditEvent, ProjectMember
from app.identity_service import database_factory
from app.llm_models import LLMProfileVersion
from app.llm_service import LLMInvocationError, LLMResult
from .conftest import create_user, with_db
from .test_coffy_execution import _create
from .test_coffy_tasks import fields, setup
from .test_identity_acl import login


def completed(client, monkeypatch, *, strict_json=False, output="Proposition à relire", domain="pilotage"):
    state = setup(client)
    async def no_dispatch(*args, **kwargs):
        return None
    monkeypatch.setattr("app.coffy_api.dispatch_pending", no_dispatch)
    headers = {"X-CSRF-Token": state["csrf"]}
    if domain == "pilotage":
        task_id = _create(client, state, strict_json=strict_json)
    else:
        created_task = client.post(state["base"], json=fields(state,
            result_domain=domain, strict_json=strict_json), headers=headers)
        assert created_task.status_code == 201, created_task.text
        task_id = created_task.json()["id"]
        active = client.post(f'{state["base"]}/{task_id}/activate',
            json={"expected_revision": 1}, headers=headers)
        assert active.status_code == 200, active.text
    created = client.post(f'{state["base"]}/{task_id}/run-now',
        json={"expected_revision": 2, "request_key": str(uuid4())}, headers=headers)
    assert created.status_code == 202, created.text
    run_id = created.json()["id"]
    async def invoke(profile, messages, **kwargs):
        return LLMResult(output, profile.id, profile.provider, profile.model_name, 7, 5)
    async def execute():
        factory = database_factory()
        try:
            await execute_run(factory, run_id, invoke=invoke)
        finally:
            await factory.kw["bind"].dispose()
    asyncio.run(execute())
    return state, task_id, run_id


def result_base(state, task_id, run_id):
    return f'{state["base"]}/{task_id}/runs/{run_id}/results'


def test_resultat_versions_commentaire_avis_et_decision_humaine(pg_client, monkeypatch):
    state, task_id, run_id = completed(pg_client, monkeypatch)
    base = result_base(state, task_id, run_id)
    headers = {"X-CSRF-Token": state["csrf"]}
    initial = pg_client.get(base).json()
    assert initial["current_version"] == 1
    assert initial["versions"][0]["status"] == "Brouillon IA"
    first_id = initial["versions"][0]["id"]
    assert "Tentatives LLM" in pg_client.get(f'/projects/{state["project_id"]}/scheduled-tasks/{task_id}/runs/{run_id}').text
    feedback = pg_client.post(f"{base}/feedback", json={"result_version_id": first_id,
        "score": 4, "note": "Utile, à préciser"}, headers=headers)
    assert feedback.status_code == 201, feedback.text
    assert pg_client.post(f"{base}/feedback", json={"result_version_id": first_id,
        "score": 5}, headers=headers).status_code == 409
    comment = pg_client.post(f"{base}/comments", json={"result_version_id": first_id,
        "body": "Vérifier la source"}, headers=headers)
    assert comment.status_code == 201
    assert pg_client.get(base).json()["versions"][-1]["status"] == "Brouillon IA"
    assert pg_client.post(f'{base}/decision/validate', json={"expected_version": 1}, headers=headers).status_code == 409
    submitted = pg_client.post(f'{base}/decision/submit', json={"expected_version": 1}, headers=headers)
    assert submitted.status_code == 201 and submitted.json()["status"] == "À revoir"
    assert pg_client.post(f'{base}/decision/submit', json={"expected_version": 1}, headers=headers).status_code == 409
    validated = pg_client.post(f'{base}/decision/validate', json={"expected_version": 2}, headers=headers)
    assert validated.status_code == 201 and validated.json()["status"] == "Validé"
    revised = pg_client.post(f'{base}/revise', json={"expected_version": 3,
        "content": "Proposition corrigée par humain"}, headers=headers)
    assert revised.status_code == 201 and revised.json()["status"] == "À revoir"
    history = pg_client.get(base).json()
    assert [row["status"] for row in history["versions"]] == ["Brouillon IA", "À revoir", "Validé", "À revoir"]
    assert history["current_version"] == 4
    assert history["feedback"][0]["score"] == 4
    assert history["comments"][0]["body"] == "Vérifier la source"
    assert pg_client.post(f'{base}/comments/{comment.json()["id"]}/resolve', headers=headers).status_code == 200
    rejected = pg_client.post(f'{base}/decision/reject', json={"expected_version": 4,
        "reason": "Source insuffisante"}, headers=headers)
    assert rejected.status_code == 201 and rejected.json()["status"] == "Rejeté"
    assert pg_client.get(base).json()["comments"][-1]["body"] == "Source insuffisante"
    async def evidence(db):
        rows = (await db.scalars(select(ScheduledLLMResultVersion).where(
            ScheduledLLMResultVersion.run_id == run_id))).all()
        assert len(rows) == 5
        events = (await db.scalars(select(AuditEvent).where(AuditEvent.project_id == state["project_id"]))).all()
        actions = {row.action for row in events}
        assert {"scheduled_task.result.create", "scheduled_task.result.feedback",
                "scheduled_task.result.comment", "scheduled_task.result.submit",
                "scheduled_task.result.validate", "scheduled_task.result.revise",
                "scheduled_task.result.reject", "scheduled_task.result.comment.resolve"} <= actions
        assert all("Proposition corrigée" not in str(row.details) for row in events)
    asyncio.run(with_db(evidence))


def test_roles_csrf_et_portee_du_resultat(pg_client, monkeypatch):
    state, task_id, run_id = completed(pg_client, monkeypatch)
    base = result_base(state, task_id, run_id)
    first_id = pg_client.get(base).json()["versions"][0]["id"]
    reader_id, reader_email = create_user()
    po_id, po_email = create_user()
    async def memberships(db):
        db.add(ProjectMember(project_id=state["project_id"], user_id=reader_id, role_code="reader", is_active=True))
        db.add(ProjectMember(project_id=state["project_id"], user_id=po_id, role_code="product_owner", is_active=True))
    asyncio.run(with_db(memberships))
    reader_csrf = login(pg_client, reader_email)
    assert pg_client.get(base).status_code == 200
    assert pg_client.post(f"{base}/feedback", json={"result_version_id": first_id, "score": 5},
        headers={"X-CSRF-Token": reader_csrf}).status_code == 403
    po_csrf = login(pg_client, po_email)
    assert pg_client.post(f'{base}/decision/submit', json={"expected_version": 1},
        headers={"X-CSRF-Token": po_csrf}).status_code == 201
    assert pg_client.post(f'{base}/decision/validate', json={"expected_version": 2},
        headers={"X-CSRF-Token": po_csrf}).status_code == 403
    manager_csrf = login(pg_client, state["email"])
    assert pg_client.post(f'{base}/decision/validate', json={"expected_version": 2},
        headers={"X-CSRF-Token": manager_csrf}).status_code == 201
    assert pg_client.post(f'{base}/revise', json={"expected_version": 3, "content": "Modification"},
        headers={"X-CSRF-Token": "invalid"}).status_code == 403
    other = pg_client.post("/projects", data={"name": "Autre CoFFY", "description": "", "csrf_token": manager_csrf})
    other_id = other.headers["location"].rsplit("/", 1)[-1]
    assert pg_client.get(f'/api/projects/{other_id}/scheduled-tasks/{task_id}/runs/{run_id}/results').status_code == 404


def test_json_strict_sur_sortie_et_correction(pg_client, monkeypatch):
    state, task_id, run_id = completed(pg_client, monkeypatch, strict_json=True, output='{"ok":true}')
    base = result_base(state, task_id, run_id)
    assert pg_client.get(base).json()["versions"][0]["status"] == "Brouillon IA"
    assert pg_client.post(f'{base}/revise', json={"expected_version": 1,
        "content": '{"x":1,"x":2}'}, headers={"X-CSRF-Token": state["csrf"]}).status_code == 409
    assert pg_client.get(base).json()["current_version"] == 1
    valid = pg_client.post(f'{base}/revise', json={"expected_version": 1,
        "content": '{"ok":false}'}, headers={"X-CSRF-Token": state["csrf"]})
    assert valid.status_code == 201 and valid.json()["status"] == "À revoir"


def test_json_strict_invalide_ne_cree_pas_de_version(pg_client, monkeypatch):
    state, task_id, run_id = completed(pg_client, monkeypatch,
        strict_json=True, output='{"x":1,"x":2}')
    detail = pg_client.get(f'{state["base"]}/{task_id}/runs/{run_id}').json()
    assert detail["run"]["status"] == "failed"
    assert detail["run"]["error_code"] == "invalid_json"
    assert pg_client.get(result_base(state, task_id, run_id)).json()["versions"] == []


def test_decision_selon_domaine_fige(pg_client, monkeypatch):
    for domain, allowed_role in (("business", "product_owner"), ("technical", "architect")):
        state, task_id, run_id = completed(pg_client, monkeypatch, domain=domain)
        base = result_base(state, task_id, run_id)
        manager_headers = {"X-CSRF-Token": state["csrf"]}
        submitted = pg_client.post(f'{base}/decision/submit',
            json={"expected_version": 1}, headers=manager_headers)
        assert submitted.status_code == 201
        denied = pg_client.post(f'{base}/decision/validate',
            json={"expected_version": 2}, headers=manager_headers)
        assert denied.status_code == 403
        reviewer_id, reviewer_email = create_user()
        async def member(db):
            db.add(ProjectMember(project_id=state["project_id"], user_id=reviewer_id,
                                 role_code=allowed_role, is_active=True))
        asyncio.run(with_db(member))
        reviewer_csrf = login(pg_client, reviewer_email)
        approved = pg_client.post(f'{base}/decision/validate',
            json={"expected_version": 2}, headers={"X-CSRF-Token": reviewer_csrf})
        assert approved.status_code == 201 and approved.json()["status"] == "Validé"


def test_retries_et_timeout_dans_historique(pg_client, monkeypatch):
    state = setup(pg_client)
    async def no_dispatch(*args, **kwargs):
        return None
    monkeypatch.setattr("app.coffy_api.dispatch_pending", no_dispatch)
    async def allow_retry(db):
        profile = await db.get(LLMProfileVersion, state["profile_id"])
        profile.max_retries = 1
    asyncio.run(with_db(allow_retry))
    task_id = _create(pg_client, state)
    headers = {"X-CSRF-Token": state["csrf"]}
    async def run_with(invoke):
        response = pg_client.post(f'{state["base"]}/{task_id}/run-now',
            json={"expected_revision": 2, "request_key": str(uuid4())}, headers=headers)
        assert response.status_code == 202
        run_id = response.json()["id"]
        factory = database_factory()
        try:
            await execute_run(factory, run_id, invoke=invoke)
        finally:
            await factory.kw["bind"].dispose()
        return run_id
    attempts = []
    async def transient(profile, messages, **kwargs):
        attempts.append(True)
        if len(attempts) == 1:
            raise LLMInvocationError("timeout")
        return LLMResult("Réponse", profile.id, profile.provider, profile.model_name, 2, 2)
    first_id = asyncio.run(run_with(transient))
    detail = pg_client.get(f'{state["base"]}/{task_id}/runs/{first_id}').json()
    assert detail["run"]["status"] == "success" and detail["run"]["retry_count"] == 1
    assert [row["error_code"] for row in detail["attempts"]] == ["timeout", None]
    async def timeout(*args, **kwargs):
        raise LLMInvocationError("timeout")
    second_id = asyncio.run(run_with(timeout))
    second = pg_client.get(f'{state["base"]}/{task_id}/runs/{second_id}').json()
    assert second["run"]["status"] == "timeout"
    assert second["run"]["retry_count"] == 1
    assert pg_client.get(result_base(state, task_id, second_id)).json()["versions"] == []
