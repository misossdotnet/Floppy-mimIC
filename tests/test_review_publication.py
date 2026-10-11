"""UC-005/006/011 : décision humaine, commentaire, publication et export privé."""

import asyncio

from sqlalchemy import select

from app.business_models import (Deliverable, DeliverableComment, DeliverableVersion,
                                 ExportArtifact, PublicationRecord)
from app.identity_models import AuditEvent, ProjectMember
from app.object_storage import StorageUnavailable

from .conftest import create_user, with_db
from .test_identity_acl import login


def test_revue_publication_export_et_remplacement(pg_client, monkeypatch):
    monkeypatch.setenv("MIMIC_REDIS_URL", "redis://127.0.0.1:56379/15")
    from app import worker
    dispatched = []
    monkeypatch.setattr(worker.build_export, "delay", lambda artifact_id: dispatched.append(artifact_id))

    admin_id, admin_email = create_user(admin=True)
    po_id, po_email = create_user()
    cp_id, cp_email = create_user()
    architect_id, architect_email = create_user()
    reader_id, reader_email = create_user()

    def session(email):
        pg_client.cookies.clear()
        return login(pg_client, email)

    admin_token = session(admin_email)
    project_id = pg_client.post("/projects", data={"name": "Revue", "csrf_token": admin_token}).headers["location"].split("/")[-1]
    foreign_id = pg_client.post("/projects", data={"name": "Autre", "csrf_token": admin_token}).headers["location"].split("/")[-1]

    async def seed(db):
        for user_id, role in [(po_id, "product_owner"), (cp_id, "project_manager"),
                              (architect_id, "architect"), (reader_id, "reader")]:
            db.add(ProjectMember(project_id=project_id, user_id=user_id,
                                 role_code=role, is_active=True))
        deliverable = Deliverable(project_id=project_id, type="use_cases", title="Cas d'usage",
                                  current_version=1, created_by=admin_id)
        db.add(deliverable)
        await db.flush()
        version = DeliverableVersion(deliverable_id=deliverable.id, version=1,
                                     title=deliverable.title, content="Contenu à relire",
                                     status="Brouillon IA", actor_id=None)
        db.add(version)
        await db.flush()
        return deliverable.id, version.id

    deliverable_id, draft_id = asyncio.run(with_db(seed))
    base = f"/projects/{project_id}/reviews"
    assert pg_client.get(base).status_code == 200
    assert pg_client.get(f"/projects/{foreign_id}/reviews/{draft_id}").status_code == 404
    assert pg_client.post(f"{base}/{draft_id}/decision/validate", data={
        "csrf_token": admin_token}).status_code == 409
    assert pg_client.post(f"/projects/{project_id}/publications", data={
        "version_id": draft_id, "csrf_token": admin_token}).status_code == 409
    assert pg_client.post(f"{base}/{draft_id}/decision/submit", data={
        "csrf_token": "faux"}).status_code == 403
    submitted = pg_client.post(f"{base}/{draft_id}/decision/submit", data={"csrf_token": admin_token})
    assert submitted.status_code == 303
    review_id = submitted.headers["location"].split("/")[-1]
    assert pg_client.post(f"{base}/{draft_id}/decision/submit", data={
        "csrf_token": admin_token}).status_code == 409

    po_token = session(po_email)
    assert pg_client.get(f"{base}/{review_id}").status_code == 200
    assert pg_client.post(f"{base}/{review_id}/comments", data={
        "text": "À vérifier", "csrf_token": po_token}).status_code == 303

    async def comment_id(db):
        return (await db.scalar(select(DeliverableComment).where(
            DeliverableComment.version_id == review_id))).id

    note_id = asyncio.run(with_db(comment_id))
    assert pg_client.post(f"{base}/{draft_id}/comments/{note_id}/resolve", data={
        "csrf_token": po_token}).status_code == 404
    assert pg_client.post(f"{base}/{review_id}/comments/{note_id}/resolve", data={
        "csrf_token": po_token}).status_code == 303
    assert pg_client.post(f"{base}/{review_id}/comments/{note_id}/resolve", data={
        "csrf_token": po_token}).status_code == 409
    assert "Résolu" in pg_client.get(f"{base}/{review_id}").text
    assert pg_client.post(f"/projects/{project_id}/publications", data={
        "version_id": review_id, "csrf_token": po_token}).status_code == 403

    architect_token = session(architect_email)
    assert pg_client.post(f"{base}/{review_id}/decision/validate", data={
        "csrf_token": architect_token}).status_code == 403
    cp_token = session(cp_email)
    assert pg_client.post(f"{base}/{review_id}/decision/validate", data={
        "csrf_token": cp_token}).status_code == 403
    reader_token = session(reader_email)
    assert pg_client.post(f"{base}/{review_id}/comments", data={
        "text": "Non", "csrf_token": reader_token}).status_code == 403

    po_token = session(po_email)
    accepted = pg_client.post(f"{base}/{review_id}/decision/validate", data={"csrf_token": po_token})
    assert accepted.status_code == 303
    valid_id = accepted.headers["location"].split("/")[-1]
    assert pg_client.post(f"{base}/{review_id}/decision/reject", data={
        "reason": "Trop tard", "csrf_token": po_token}).status_code == 409

    cp_token = session(cp_email)
    assert pg_client.post(f"/projects/{project_id}/exports", data={
        "version_id": valid_id, "format": "json", "csrf_token": cp_token}).status_code == 409
    assert pg_client.post(f"/projects/{project_id}/publications", data={
        "version_id": valid_id, "csrf_token": cp_token}).status_code == 303
    assert pg_client.post(f"/projects/{project_id}/publications", data={
        "version_id": valid_id, "csrf_token": cp_token}).status_code == 409

    async def published(db):
        return await db.scalar(select(PublicationRecord).where(
            PublicationRecord.deliverable_id == deliverable_id,
            PublicationRecord.active.is_(True)))

    publication = asyncio.run(with_db(published))
    published_id = publication.version_id
    assert pg_client.post(f"/projects/{project_id}/exports", data={
        "version_id": published_id, "format": "pdf", "csrf_token": cp_token}).status_code == 422
    assert pg_client.post(f"/projects/{project_id}/exports", data={
        "version_id": published_id, "format": "markdown", "csrf_token": cp_token}).status_code == 303
    assert len(dispatched) == 1
    export_id = dispatched[-1]
    assert pg_client.get(f"/projects/{project_id}/exports/{export_id}/download").status_code == 409
    asyncio.run(worker._export_artifact(export_id))
    asyncio.run(worker._export_artifact(export_id))
    ready = asyncio.run(with_db(lambda db: db.get(ExportArtifact, export_id)))
    assert ready.status == "ready" and ready.object_key.startswith(f"projects/{project_id}/exports/")
    assert ready.sha256 and ready.size_bytes
    assert pg_client.post(f"/projects/{project_id}/exports", data={
        "version_id": published_id, "format": "json", "csrf_token": cp_token}).status_code == 303
    failed_export_id = dispatched[-1]
    with monkeypatch.context() as patch:
        def unavailable(cls):
            raise StorageUnavailable("storage_unavailable")
        patch.setattr(worker.ObjectStorage, "from_env", classmethod(unavailable))
        asyncio.run(worker._export_artifact(failed_export_id))
    failed_export = asyncio.run(with_db(lambda db: db.get(ExportArtifact, failed_export_id)))
    assert (failed_export.status, failed_export.error_code) == ("failed", "storage_unavailable")
    reader_token = session(reader_email)
    downloaded = pg_client.get(f"/projects/{project_id}/exports/{export_id}/download")
    assert downloaded.status_code == 200 and b"Contenu \xc3\xa0 relire" in downloaded.content
    assert downloaded.headers["cache-control"] == "no-store"
    assert pg_client.get(f"/projects/{foreign_id}/exports/{export_id}/download").status_code == 404

    cp_token = session(cp_email)
    revised = pg_client.post(f"/projects/{project_id}/versions/{published_id}/revisions", data={
        "content": "Seconde édition", "expected_version": 4, "csrf_token": cp_token})
    assert revised.status_code == 303
    second_review = revised.headers["location"].split("/")[-1]
    po_token = session(po_email)
    second_valid = pg_client.post(f"{base}/{second_review}/decision/validate", data={
        "csrf_token": po_token})
    assert second_valid.status_code == 303
    cp_token = session(cp_email)
    assert pg_client.post(f"/projects/{project_id}/publications", data={
        "version_id": second_valid.headers["location"].split("/")[-1],
        "csrf_token": cp_token}).status_code == 303
    assert "Obsolète" in pg_client.get(f"{base}/{published_id}").text
    assert pg_client.get(f"/projects/{project_id}/exports/{export_id}/download").status_code == 409
    assert pg_client.post(f"/projects/{project_id}/exports", data={
        "version_id": published_id, "format": "json", "csrf_token": cp_token}).status_code == 409

    async def final(db):
        versions = (await db.scalars(select(DeliverableVersion).where(
            DeliverableVersion.deliverable_id == deliverable_id).order_by(DeliverableVersion.version))).all()
        publications = (await db.scalars(select(PublicationRecord).where(
            PublicationRecord.deliverable_id == deliverable_id))).all()
        actions = (await db.scalars(select(AuditEvent.action).where(
            AuditEvent.project_id == project_id))).all()
        return versions, publications, actions

    versions, publications, actions = asyncio.run(with_db(final))
    assert [v.status for v in versions] == ["Brouillon IA", "À revoir", "Validé", "Publié",
                                           "À revoir", "Validé", "Publié"]
    assert len(publications) == 2 and sum(p.active for p in publications) == 1
    assert {"deliverable.submit", "deliverable.validate", "deliverable.comment",
            "deliverable.comment.resolve", "deliverable.publish", "export.request",
            "export.complete", "export.download"} <= set(actions)


def test_rejet_motive_versionne(pg_client):
    admin_id, email = create_user(admin=True)
    architect_id, architect_email = create_user()
    token = login(pg_client, email)
    project_id = pg_client.post("/projects", data={"name": "Rejet", "csrf_token": token}).headers["location"].split("/")[-1]

    async def seed(db):
        db.add(ProjectMember(project_id=project_id, user_id=architect_id,
                             role_code="architect", is_active=True))
        d = Deliverable(project_id=project_id, type="architecture", title="Architecture",
                        current_version=1, created_by=admin_id)
        db.add(d)
        await db.flush()
        v = DeliverableVersion(deliverable_id=d.id, version=1, title=d.title,
                               content="Proposition", status="À revoir", actor_id=admin_id)
        db.add(v)
        await db.flush()
        return v.id

    version_id = asyncio.run(with_db(seed))
    base = f"/projects/{project_id}/reviews/{version_id}/decision/reject"
    pg_client.cookies.clear()
    architect_token = login(pg_client, architect_email)
    assert pg_client.post(base, data={"reason": "   ", "csrf_token": architect_token}).status_code == 422
    response = pg_client.post(base, data={"reason": "Architecture à corriger", "csrf_token": architect_token})
    assert response.status_code == 303
    rejected_id = response.headers["location"].split("/")[-1]
    assert "Architecture à corriger" in pg_client.get(response.headers["location"]).text
    assert pg_client.post(f"/projects/{project_id}/publications", data={
        "version_id": rejected_id, "csrf_token": architect_token}).status_code == 403
