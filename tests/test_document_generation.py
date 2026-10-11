"""UC-004-01 : génération documentaire gouvernée et brouillon versionné."""

import asyncio
from uuid import uuid4

from sqlalchemy import select

from app.business_models import ContextItem, Deliverable, DeliverableVersion, GenerationRun
from app.identity_models import AuditEvent, ProjectMember
from app.llm_models import LLMProfileVersion
from app.llm_service import LLMInvocationError, LLMResult, settings as llm_settings

from .conftest import create_user, with_db
from .test_identity_acl import login


def test_generation_brouillon_provenance_correction_et_relance(pg_client, monkeypatch):
    monkeypatch.setenv("MIMIC_REDIS_URL", "redis://127.0.0.1:56379/15")
    monkeypatch.setenv("MIMIC_LLM_ALLOWED_BASE_URLS", "http://127.0.0.1:11434")
    from app import worker

    dispatched = []
    monkeypatch.setattr(worker.generate_document, "delay", lambda run_id: dispatched.append(run_id))
    admin_id, admin_email = create_user(admin=True)
    token = login(pg_client, admin_email)
    project = pg_client.post("/projects", data={"name": "Génération", "csrf_token": token})
    project_id = project.headers["location"].split("/")[-1]
    foreign = pg_client.post("/projects", data={"name": "Projet privé", "csrf_token": token})
    foreign_id = foreign.headers["location"].split("/")[-1]

    def make_item(project_id, title):
        response = pg_client.post(f"/projects/{project_id}/context", data={
            "type": "requirement", "title": title, "content": "Besoin de traçabilité.",
            "csrf_token": token})
        assert response.status_code == 303
        return response.headers["location"].split("item_id=")[-1]

    item_id = make_item(project_id, "Traçabilité")
    foreign_item = make_item(foreign_id, "Secret autre projet")
    url = f"/projects/{project_id}/generations"
    data = {"deliverable_type": "use_cases", "style": "concise", "context_ids": item_id,
            "csrf_token": token}
    assert pg_client.get(url).status_code == 200
    assert pg_client.post(url, data=data).status_code == 409

    async def profile(db):
        row = LLMProfileVersion(profile_id=str(uuid4()), version=1, name="Local", provider="ollama",
                                model_name="test", base_url="http://127.0.0.1:11434", api_version=None,
                                credential_env=None, temperature=None, max_output_tokens=1000,
                                timeout_seconds=10, max_retries=0, enabled=True, is_current=True,
                                created_by=admin_id)
        db.add(row)
        await db.flush()
        (await llm_settings(db)).default_version_id = row.id
        return row.id

    profile_id = asyncio.run(with_db(profile))
    assert pg_client.post(url, data={**data, "csrf_token": "faux"}).status_code == 403
    assert pg_client.post(url, data={**data, "deliverable_type": "unknown"}).status_code == 422
    assert pg_client.post(url, data={**data, "style": "unknown"}).status_code == 422
    assert pg_client.post(url, data={**data, "context_ids": foreign_item}).status_code == 422
    assert pg_client.post(url, data={key: value for key, value in data.items()
                                     if key != "context_ids"}).status_code == 422
    assert pg_client.post(url, data=data).status_code == 303
    assert len(dispatched) == 1
    run_id = dispatched[0]

    async def initial(db):
        return await db.get(GenerationRun, run_id)

    run = asyncio.run(with_db(initial))
    assert run.llm_profile_version_id == profile_id and run.status == "queued"
    assert run.context_snapshot[0]["id"] == item_id
    assert run.context_snapshot[0]["version"] == 1
    assert "Besoin de traçabilité" not in run.prompt_template
    assert run.parameters == {"style": "concise", "max_output_tokens": 1000}
    assert pg_client.post(f"/projects/{project_id}/context/{item_id}/versions", data={
        "title": "Traçabilité", "content": "Besoin modifié après lancement.",
        "expected_version": 1, "csrf_token": token}).status_code == 303

    async def fake_invoke(profile, messages, **kwargs):
        assert profile.id == profile_id
        assert "Traçabilité" in messages[1]["content"]
        if kwargs["output_limit"] == 1000 and "\"version\": 1" in messages[1]["content"]:
            assert "Besoin de traçabilité" in messages[1]["content"]
        assert kwargs["output_limit"] == 1000
        return LLMResult("# Cas d'usage\n\nUn acteur consulte la traçabilité.",
                         profile.id, "ollama", "test", 120, 40)

    monkeypatch.setattr(worker, "invoke_profile", fake_invoke)
    asyncio.run(worker._generate_document(run_id))
    asyncio.run(worker._generate_document(run_id))

    async def generated(db):
        run = await db.get(GenerationRun, run_id)
        deliverable = await db.get(Deliverable, run.deliverable_id)
        versions = (await db.scalars(select(DeliverableVersion).where(
            DeliverableVersion.deliverable_id == deliverable.id))).all()
        return run, deliverable, versions

    run, deliverable, versions = asyncio.run(with_db(generated))
    assert (run.status, run.input_tokens, run.output_tokens, deliverable.current_version) == (
        "completed", 120, 40, 1)
    assert len(versions) == 1 and versions[0].status == "Brouillon IA"
    assert versions[0].actor_id is None and versions[0].generation_run_id == run_id
    version_id = versions[0].id
    detail = pg_client.get(f"/projects/{project_id}/reviews/{version_id}")
    assert detail.status_code == 200 and "Cas d&#39;usage" in detail.text
    assert "Brouillon IA" in detail.text and "Traçabilité v1" in detail.text
    assert pg_client.get(f"/projects/{foreign_id}/reviews/{version_id}").status_code == 404
    assert pg_client.post(f"/projects/{project_id}/versions/{version_id}/revisions", data={
        "content": "Version humaine corrigée", "expected_version": 1,
        "csrf_token": token}).status_code == 303
    assert pg_client.post(f"/projects/{project_id}/versions/{version_id}/revisions", data={
        "content": "Concurrence", "expected_version": 1,
        "csrf_token": token}).status_code == 409

    async def revised(db):
        return (await db.scalars(select(DeliverableVersion).where(
            DeliverableVersion.deliverable_id == deliverable.id)
            .order_by(DeliverableVersion.version))).all()

    revisions = asyncio.run(with_db(revised))
    assert [(v.version, v.status) for v in revisions] == [(1, "Brouillon IA"), (2, "À revoir")]
    assert revisions[0].content.startswith("# Cas d'usage")
    assert revisions[1].parent_version_id == version_id
    assert "Version humaine corrigée" in pg_client.get(url).text or "v2" in pg_client.get(url).text

    reader_id, reader_email = create_user()
    async def member(db):
        db.add(ProjectMember(project_id=project_id, user_id=reader_id,
                             role_code="reader", is_active=True))
    asyncio.run(with_db(member))
    pg_client.cookies.clear()
    reader_token = login(pg_client, reader_email)
    assert pg_client.get(f"/projects/{project_id}/reviews/{revisions[1].id}").status_code == 200
    assert pg_client.get(f"/projects/{foreign_id}/generations").status_code == 404
    assert pg_client.post(f"/projects/{project_id}/versions/{revisions[1].id}/revisions", data={
        "content": "Interdit", "expected_version": 2,
        "csrf_token": reader_token}).status_code == 403
    assert pg_client.post(url, data={**data, "csrf_token": reader_token}).status_code == 403
    pg_client.cookies.clear()
    token = login(pg_client, admin_email)

    failed_request = pg_client.post(url, data={**data, "csrf_token": token})
    assert failed_request.status_code == 303 and len(dispatched) == 2
    failed_id = dispatched[-1]

    async def timeout(profile, messages, **kwargs):
        raise LLMInvocationError("timeout")

    monkeypatch.setattr(worker, "invoke_profile", timeout)
    asyncio.run(worker._generate_document(failed_id))
    assert pg_client.post(f"{url}/{failed_id}/retry", data={
        "csrf_token": token}).status_code == 303
    assert len(dispatched) == 3
    retry_id = dispatched[-1]
    assert pg_client.post(f"{url}/{failed_id}/retry", data={
        "csrf_token": token}).status_code == 409
    monkeypatch.setattr(worker, "invoke_profile", fake_invoke)
    asyncio.run(worker._generate_document(retry_id))

    async def final(db):
        first = await db.get(GenerationRun, failed_id)
        second = await db.get(GenerationRun, retry_id)
        versions = (await db.scalars(select(DeliverableVersion).where(
            DeliverableVersion.deliverable_id == second.deliverable_id))).all()
        actions = (await db.scalars(select(AuditEvent.action).where(
            AuditEvent.project_id == project_id))).all()
        return first, second, versions, actions

    first, second, versions, actions = asyncio.run(with_db(final))
    assert (first.status, first.error_code, second.attempt_number, second.status) == (
        "failed", "timeout", 2, "completed")
    assert len(versions) == 1 and versions[0].status == "Brouillon IA"
    assert {"generation.request", "generation.complete", "generation.retry",
            "deliverable.revise"} <= set(actions)

    invalid_request = pg_client.post(url, data={**data, "csrf_token": token})
    assert invalid_request.status_code == 303
    invalid_id = dispatched[-1]

    async def empty_response(profile, messages, **kwargs):
        return LLMResult("  ", profile.id, "ollama", "test", None, None)

    monkeypatch.setattr(worker, "invoke_profile", empty_response)
    asyncio.run(worker._generate_document(invalid_id))

    async def invalid_state(db):
        run = await db.get(GenerationRun, invalid_id)
        count = len((await db.scalars(select(DeliverableVersion).where(
            DeliverableVersion.deliverable_id == run.deliverable_id))).all())
        return run.status, run.error_code, count

    assert asyncio.run(with_db(invalid_state)) == ("failed", "invalid_response", 0)

    def unavailable(_run_id):
        raise RuntimeError("broker indisponible")

    monkeypatch.setattr(worker.generate_document, "delay", unavailable)
    assert pg_client.post(url, data={**data, "csrf_token": token}).status_code == 303

    async def dispatch_state(db):
        run = await db.scalar(select(GenerationRun).where(
            GenerationRun.project_id == project_id)
            .order_by(GenerationRun.created_at.desc()).limit(1))
        return run.status, run.error_code

    assert asyncio.run(with_db(dispatch_state)) == ("failed", "dispatch_unavailable")
