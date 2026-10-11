"""UC-008/007 : diff, restauration non destructive et régénération gouvernée."""

import asyncio
from uuid import uuid4

from sqlalchemy import select

from app.business_models import ContextItem, Deliverable, DeliverableVersion, DiffRun, GenerationRun
from app.identity_models import AuditEvent, ProjectMember
from app.llm_models import LLMProfileVersion
from app.llm_service import LLMInvocationError, LLMResult, settings as llm_settings

from .conftest import create_user, with_db
from .test_identity_acl import login


def test_diff_restore_regeneration_et_refus(pg_client, monkeypatch):
    monkeypatch.setenv("MIMIC_REDIS_URL", "redis://127.0.0.1:56379/15")
    monkeypatch.setenv("MIMIC_LLM_ALLOWED_BASE_URLS", "http://127.0.0.1:11434")
    from app import worker
    diff_jobs, generation_jobs = [], []
    monkeypatch.setattr(worker.compute_diff, "delay", lambda run_id: diff_jobs.append(run_id))
    monkeypatch.setattr(worker.generate_document, "delay", lambda run_id: generation_jobs.append(run_id))

    admin_id, admin_email = create_user(admin=True)
    reader_id, reader_email = create_user()
    token = login(pg_client, admin_email)
    project_id = pg_client.post("/projects", data={"name": "Versions", "csrf_token": token}).headers["location"].split("/")[-1]
    foreign_id = pg_client.post("/projects", data={"name": "Secret", "csrf_token": token}).headers["location"].split("/")[-1]
    context_response = pg_client.post(f"/projects/{project_id}/context", data={
        "type": "requirement", "title": "Besoin", "content": "Texte initial",
        "csrf_token": token})
    item_id = context_response.headers["location"].split("item_id=")[-1]
    foreign_context = pg_client.post(f"/projects/{foreign_id}/context", data={
        "type": "requirement", "title": "Autre besoin", "content": "Confidentiel",
        "csrf_token": token})
    foreign_item_id = foreign_context.headers["location"].split("item_id=")[-1]

    async def seed(db):
        db.add(ProjectMember(project_id=project_id, user_id=reader_id,
                             role_code="reader", is_active=True))
        profile = LLMProfileVersion(profile_id=str(uuid4()), version=1, name="Local",
                                    provider="ollama", model_name="test",
                                    base_url="http://127.0.0.1:11434", api_version=None,
                                    credential_env=None, temperature=None,
                                    max_output_tokens=1000, timeout_seconds=10,
                                    max_retries=0, enabled=True, is_current=True,
                                    created_by=admin_id)
        db.add(profile)
        await db.flush()
        (await llm_settings(db)).default_version_id = profile.id
        item = await db.get(ContextItem, item_id)
        deliverable = Deliverable(project_id=project_id, type="use_cases",
                                  title="Cas d'usage", current_version=1,
                                  created_by=admin_id)
        db.add(deliverable)
        await db.flush()
        run = GenerationRun(project_id=project_id, deliverable_id=deliverable.id,
                            requested_by=admin_id, llm_profile_version_id=profile.id,
                            attempt_number=1, status="completed", prompt_version="document-v1",
                            prompt_template="Consigne contrôlée", context_snapshot=[{
                                "id": item.id, "version": item.current_version,
                                "type": item.type, "title": item.title,
                                "content": item.content, "status": item.status}],
                            parameters={"style": "concise", "max_output_tokens": 1000})
        db.add(run)
        await db.flush()
        version = DeliverableVersion(deliverable_id=deliverable.id, version=1,
                                     title=deliverable.title, content="# Cas\n\nAncien",
                                     status="Brouillon IA", generation_run_id=run.id)
        db.add(version)
        foreign_deliverable = Deliverable(project_id=foreign_id, type="use_cases",
                                          title="Confidentiel", current_version=1,
                                          created_by=admin_id)
        db.add(foreign_deliverable)
        await db.flush()
        foreign_version = DeliverableVersion(deliverable_id=foreign_deliverable.id,
                                             version=1, title="Confidentiel",
                                             content="Secret", status="Brouillon IA")
        db.add(foreign_version)
        other_deliverable = Deliverable(project_id=project_id, type="scope",
                                        title="Autre livrable", current_version=1,
                                        created_by=admin_id)
        db.add(other_deliverable)
        await db.flush()
        other_version = DeliverableVersion(deliverable_id=other_deliverable.id,
                                           version=1, title="Autre livrable",
                                           content="Autre contenu", status="Brouillon IA")
        db.add(other_version)
        await db.flush()
        return deliverable.id, version.id, foreign_version.id, other_version.id, profile.id

    deliverable_id, v1, foreign_version_id, other_version_id, profile_id = asyncio.run(with_db(seed))
    base = f"/projects/{project_id}/versions"
    assert pg_client.get(base).status_code == 200
    assert pg_client.get(f"/projects/{foreign_id}/versions").status_code == 200
    assert pg_client.get(f"/projects/{foreign_id}/reviews/{v1}").status_code == 404
    assert pg_client.post(f"{base}/{v1}/revisions", data={
        "content": "# Cas\n\nNouveau", "expected_version": 1,
        "csrf_token": token}).status_code == 303

    async def current(db):
        return await db.scalar(select(DeliverableVersion).where(
            DeliverableVersion.deliverable_id == deliverable_id)
            .order_by(DeliverableVersion.version.desc()).limit(1))

    v2 = asyncio.run(with_db(current)).id
    assert pg_client.post(f"{base}/diffs", data={
        "left": v1, "right": v2, "csrf_token": "faux"}).status_code == 403
    assert pg_client.post(f"{base}/diffs", data={
        "left": v1, "right": v1, "csrf_token": token}).status_code == 422
    assert pg_client.post(f"{base}/diffs", data={
        "left": v1, "right": foreign_version_id, "csrf_token": token}).status_code == 404
    assert pg_client.post(f"{base}/diffs", data={
        "left": v1, "right": other_version_id, "csrf_token": token}).status_code == 422
    requested = pg_client.post(f"{base}/diffs", data={
        "left": v1, "right": v2, "csrf_token": token})
    assert requested.status_code == 303 and len(diff_jobs) == 1
    diff_id = diff_jobs[-1]
    assert "Calcul en cours" in pg_client.get(requested.headers["location"]).text
    asyncio.run(worker._compute_diff(diff_id))
    asyncio.run(worker._compute_diff(diff_id))
    diff = asyncio.run(with_db(lambda db: db.get(DiffRun, diff_id)))
    assert diff.status == "completed" and "-Ancien" in diff.result and "+Nouveau" in diff.result
    assert "document-v1" in pg_client.get(requested.headers["location"]).text
    assert pg_client.get(f"{base}?diff_id={uuid4()}").status_code == 404
    with monkeypatch.context() as patch:
        patch.setattr(worker.compute_diff, "delay", lambda _id: (_ for _ in ()).throw(RuntimeError("broker")))
        dispatch_failure = pg_client.post(f"{base}/diffs", data={
            "left": v1, "right": v2, "csrf_token": token})
    assert dispatch_failure.status_code == 303
    assert "dispatch_unavailable" in pg_client.get(dispatch_failure.headers["location"]).text
    failed_diff_request = pg_client.post(f"{base}/diffs", data={
        "left": v1, "right": v2, "csrf_token": token})
    failed_diff_id = diff_jobs[-1]
    with monkeypatch.context() as patch:
        patch.setattr(worker, "content_diff", lambda _left, _right: (_ for _ in ()).throw(RuntimeError("diff")))
        asyncio.run(worker._compute_diff(failed_diff_id))
    assert "diff_failed" in pg_client.get(failed_diff_request.headers["location"]).text

    pg_client.cookies.clear()
    reader_token = login(pg_client, reader_email)
    assert pg_client.get(base).status_code == 200
    assert pg_client.post(f"{base}/{v1}/restore", data={
        "expected_version": 2, "confirm": "yes", "csrf_token": reader_token}).status_code == 403
    assert pg_client.post(f"{base}/{v2}/regenerations", data={
        "reason": "Essai", "prompt_adjustment": "Modifier", "context_ids": item_id,
        "expected_version": 2, "csrf_token": reader_token}).status_code == 403
    assert pg_client.get(f"/projects/{foreign_id}/versions").status_code == 404
    pg_client.cookies.clear()
    token = login(pg_client, admin_email)

    assert pg_client.post(f"{base}/{v1}/restore", data={
        "expected_version": 2, "confirm": "no", "csrf_token": token}).status_code == 422
    restored = pg_client.post(f"{base}/{v1}/restore", data={
        "expected_version": 2, "confirm": "yes", "csrf_token": token})
    assert restored.status_code == 303
    v3 = restored.headers["location"].split("/")[-1]
    assert pg_client.post(f"{base}/{v1}/restore", data={
        "expected_version": 2, "confirm": "yes", "csrf_token": token}).status_code == 409
    assert pg_client.post(f"{base}/{v3}/restore", data={
        "expected_version": 3, "confirm": "yes", "csrf_token": token}).status_code == 409
    assert "Régénérer depuis cette version" in pg_client.get(restored.headers["location"]).text

    regen_url = f"{base}/{v3}/regenerations"
    base_data = {"reason": "Corriger le document", "context_ids": item_id,
                 "expected_version": 3, "csrf_token": token}
    assert pg_client.post(regen_url, data=base_data).status_code == 422
    assert pg_client.post(regen_url, data={**base_data, "context_ids": foreign_item_id,
                                                "prompt_adjustment": "Clarifier"}).status_code == 422
    assert pg_client.post(regen_url, data={**base_data, "reason": " " ,
                                                "prompt_adjustment": "Clarifier"}).status_code == 422
    assert pg_client.post(regen_url, data={**base_data, "prompt_adjustment": "Clarifier",
                                                "expected_version": 2}).status_code == 409
    response = pg_client.post(regen_url, data={**base_data,
                                               "prompt_adjustment": "Clarifier le parcours"})
    assert response.status_code == 303 and len(generation_jobs) == 1
    first_regen_id = generation_jobs[-1]
    assert pg_client.post(regen_url, data={**base_data,
                                           "prompt_adjustment": "Deuxième appel"}).status_code == 409

    async def fake_invoke(profile, messages, **kwargs):
        assert profile.id == profile_id
        assert "Ancien" in messages[1]["content"]
        assert "Clarifier le parcours" in messages[1]["content"]
        assert kwargs["output_limit"] == 1000
        return LLMResult("# Cas\n\nVersion régénérée", profile.id, "ollama", "test", 88, 44)

    monkeypatch.setattr(worker, "invoke_profile", fake_invoke)
    asyncio.run(worker._generate_document(first_regen_id))
    asyncio.run(worker._generate_document(first_regen_id))
    first_regen = asyncio.run(with_db(lambda db: db.get(GenerationRun, first_regen_id)))
    assert (first_regen.status, first_regen.parent_version_id,
            first_regen.prompt_version, first_regen.reason) == (
                "completed", v3, "document-regen-v1", "Corriger le document")
    assert first_regen.context_snapshot[0]["version"] == 1
    v4_obj = asyncio.run(with_db(current))
    assert (v4_obj.version, v4_obj.status, v4_obj.parent_version_id,
            v4_obj.actor_id) == (4, "Brouillon IA", v3, None)
    assert "Corriger le document" in pg_client.get(f"/projects/{project_id}/reviews/{v4_obj.id}").text

    assert pg_client.post(f"/projects/{project_id}/context/{item_id}/versions", data={
        "title": "Besoin", "content": "Texte corrigé", "expected_version": 1,
        "csrf_token": token}).status_code == 303
    changed_context = pg_client.post(f"{base}/{v4_obj.id}/regenerations", data={
        "reason": "Contexte corrigé", "context_ids": item_id,
        "expected_version": 4, "csrf_token": token})
    assert changed_context.status_code == 303
    failed_id = generation_jobs[-1]
    async def timeout(profile, messages, **kwargs):
        raise LLMInvocationError("timeout")
    monkeypatch.setattr(worker, "invoke_profile", timeout)
    asyncio.run(worker._generate_document(failed_id))
    failed = asyncio.run(with_db(lambda db: db.get(GenerationRun, failed_id)))
    assert (failed.status, failed.error_code, failed.context_snapshot[0]["version"]) == (
        "failed", "timeout", 2)
    assert asyncio.run(with_db(current)).id == v4_obj.id
    retry = pg_client.post(f"/projects/{project_id}/generations/{failed_id}/retry", data={
        "csrf_token": token})
    assert retry.status_code == 303 and len(generation_jobs) == 3
    retry_id = generation_jobs[-1]
    assert pg_client.post(f"/projects/{project_id}/generations/{failed_id}/retry", data={
        "csrf_token": token}).status_code == 409

    async def success(profile, messages, **kwargs):
        assert "Texte corrigé" in messages[1]["content"]
        return LLMResult("# Cas\n\nContexte corrigé", profile.id, "ollama", "test", 64, 32)
    monkeypatch.setattr(worker, "invoke_profile", success)
    asyncio.run(worker._generate_document(retry_id))
    v5_obj = asyncio.run(with_db(current))
    assert (v5_obj.version, v5_obj.status, v5_obj.parent_version_id) == (5, "Brouillon IA", v4_obj.id)

    stale = pg_client.post(f"{base}/{v5_obj.id}/regenerations", data={
        "reason": "Encore", "prompt_adjustment": "Rendre plus concis",
        "context_ids": item_id, "expected_version": 5, "csrf_token": token})
    assert stale.status_code == 303
    stale_id = generation_jobs[-1]
    assert pg_client.post(f"{base}/{v5_obj.id}/revisions", data={
        "content": "Correction humaine", "expected_version": 5,
        "csrf_token": token}).status_code == 303
    asyncio.run(worker._generate_document(stale_id))
    stale_run = asyncio.run(with_db(lambda db: db.get(GenerationRun, stale_id)))
    assert (stale_run.status, stale_run.error_code) == ("failed", "invalid_state")
    assert pg_client.post(f"/projects/{project_id}/generations/{stale_id}/retry", data={
        "csrf_token": token}).status_code == 409
    assert asyncio.run(with_db(current)).version == 6

    async def final(db):
        versions = (await db.scalars(select(DeliverableVersion).where(
            DeliverableVersion.deliverable_id == deliverable_id)
            .order_by(DeliverableVersion.version))).all()
        events = (await db.scalars(select(AuditEvent).where(
            AuditEvent.project_id == project_id))).all()
        return versions, events

    versions, events = asyncio.run(with_db(final))
    assert [v.status for v in versions] == ["Brouillon IA", "À revoir", "À revoir",
                                           "Brouillon IA", "Brouillon IA", "À revoir"]
    actions = {event.action for event in events}
    assert {"version.diff.request", "version.diff.complete", "deliverable.restore",
            "generation.regenerate.request", "generation.complete", "generation.retry"} <= actions
    assert "Corriger le document" not in str([event.details for event in events])
