"""Parcours BUS-05 sur PostgreSQL local isolé."""

import asyncio

from sqlalchemy import select

from app.business_models import ContextComment, ContextItem, ContextItemVersion, ContextRelation
from app.identity_models import AuditEvent, ProjectMember

from .conftest import create_user, with_db
from .test_identity_acl import login


def test_referentiel_relations_historique_commentaires_et_retrait(pg_client):
    _, admin_email = create_user(admin=True)
    token = login(pg_client, admin_email)
    project = pg_client.post("/projects", data={"name": "Référentiel", "csrf_token": token})
    project_id = project.headers["location"].split("/")[-1]
    other = pg_client.post("/projects", data={"name": "Autre projet", "csrf_token": token})
    other_id = other.headers["location"].split("/")[-1]

    def create(project_id, type, title):
        result = pg_client.post(f"/projects/{project_id}/context", data={
            "type": type, "title": title, "content": f"Contenu {title}", "csrf_token": token})
        assert result.status_code == 303
        return result.headers["location"].split("item_id=")[-1]

    source_id = create(project_id, "requirement", "Exigence")
    target_id = create(project_id, "component", "Composant")
    foreign_id = create(other_id, "component", "Hors projet")
    base = f"/projects/{project_id}/context/{source_id}"
    assert pg_client.post(f"{base}/relations", data={"target_id": target_id,
        "type": "covers", "expected_version": 1, "csrf_token": "faux"}).status_code == 403
    assert pg_client.post(f"{base}/relations", data={"target_id": foreign_id,
        "type": "covers", "expected_version": 1, "csrf_token": token}).status_code == 404
    assert pg_client.post(f"{base}/relations", data={"target_id": target_id,
        "type": "inconnu", "expected_version": 1, "csrf_token": token}).status_code == 422
    assert pg_client.post(f"{base}/relations", data={"target_id": target_id,
        "type": "covers", "expected_version": 1, "csrf_token": token}).status_code == 303
    assert pg_client.post(f"{base}/relations", data={"target_id": target_id,
        "type": "covers", "expected_version": 1, "csrf_token": token}).status_code == 409

    assert pg_client.post(f"{base}/comments", data={"body": "À clarifier",
        "version": 1, "csrf_token": token}).status_code == 303
    assert pg_client.post(f"{base}/versions", data={"title": "Exigence corrigée",
        "content": "Contenu corrigé", "expected_version": 1, "csrf_token": token}).status_code == 303
    assert pg_client.post(f"{base}/comments", data={"body": "Trop tard",
        "version": 1, "csrf_token": token}).status_code == 409
    assert pg_client.post(f"{base}/decisions", data={"decision": "reject",
        "expected_version": 2, "csrf_token": token}).status_code == 422
    assert pg_client.post(f"{base}/decisions", data={"decision": "reject",
        "expected_version": 2, "reason": "Règle inexacte", "csrf_token": token}).status_code == 303

    async def snapshot(db):
        item = await db.get(ContextItem, source_id)
        versions = (await db.scalars(select(ContextItemVersion).where(
            ContextItemVersion.item_id == source_id).order_by(ContextItemVersion.version))).all()
        comments = (await db.scalars(select(ContextComment).where(
            ContextComment.item_version_id.in_([v.id for v in versions]))
            .order_by(ContextComment.created_at))).all()
        relation = await db.scalar(select(ContextRelation).where(ContextRelation.source_id == source_id))
        return item, versions, comments, relation

    item, versions, comments, relation = asyncio.run(with_db(snapshot))
    assert item.status == "Rejeté"
    assert [(v.version, v.status, v.title) for v in versions] == [
        (1, "À revoir", "Exigence"), (2, "À revoir", "Exigence corrigée"),
        (3, "Rejeté", "Exigence corrigée")]
    assert [c.body for c in comments] == ["À clarifier", "Règle inexacte"]
    assert comments[0].item_version_id == versions[0].id
    assert comments[1].item_version_id == versions[2].id
    detail = pg_client.get(f"/projects/{project_id}/context?item_id={source_id}")
    assert detail.status_code == 200
    assert "Version 1" in detail.text and "Exigence corrigée" in detail.text
    assert "À clarifier" in detail.text and "Composant" in detail.text

    assert pg_client.post(f"{base}/comments/{comments[0].id}/resolve",
        data={"csrf_token": token}).status_code == 303
    assert pg_client.post(f"{base}/comments/{comments[0].id}/resolve",
        data={"csrf_token": token}).status_code == 409
    assert pg_client.post(f"{base}/relations/{relation.id}/remove",
        data={"csrf_token": token}).status_code == 303
    assert pg_client.post(f"{base}/relations/{relation.id}/remove",
        data={"csrf_token": token}).status_code == 409
    assert pg_client.post(f"{base}/relations", data={"target_id": target_id,
        "type": "covers", "expected_version": 3, "csrf_token": token}).status_code == 303
    assert pg_client.post(f"{base}/obsolete", data={"expected_version": 2,
        "csrf_token": token}).status_code == 409
    assert pg_client.post(f"{base}/obsolete", data={"expected_version": 3,
        "csrf_token": token}).status_code == 303
    assert pg_client.post(f"{base}/versions", data={"title": "Nouvelle",
        "content": "Réemploi", "expected_version": 4, "csrf_token": token}).status_code == 422
    assert pg_client.post(f"{base}/relations", data={"target_id": target_id,
        "type": "covers", "expected_version": 4, "csrf_token": token}).status_code == 422

    async def final(db):
        item = await db.get(ContextItem, source_id)
        saved_relation = await db.get(ContextRelation, relation.id)
        comment = await db.get(ContextComment, comments[0].id)
        events = (await db.scalars(select(AuditEvent.action).where(AuditEvent.project_id == project_id))).all()
        count = len((await db.scalars(select(ContextItemVersion).where(
            ContextItemVersion.item_id == source_id))).all())
        relations = (await db.scalars(select(ContextRelation).where(
            ContextRelation.source_id == source_id).order_by(ContextRelation.created_at))).all()
        return item.status, item.current_version, saved_relation.is_active, comment.status, events, count, relations

    status, version, active, comment_status, events, count, relations = asyncio.run(with_db(final))
    assert (status, version, active, comment_status, count) == ("Obsolète", 4, False, "resolved", 4)
    assert len(relations) == 2 and relations[0].id != relations[1].id and relations[1].is_active
    assert {"context.relation.create", "context.relation.remove", "context.comment.create",
            "context.comment.resolve", "context.reject", "context.obsolete"} <= set(events)


def test_referentiel_refus_rbac_et_acces_interprojet(pg_client):
    _, admin_email = create_user(admin=True)
    admin_token = login(pg_client, admin_email)
    project = pg_client.post("/projects", data={"name": "Projet visible", "csrf_token": admin_token})
    project_id = project.headers["location"].split("/")[-1]
    other = pg_client.post("/projects", data={"name": "Projet masqué", "csrf_token": admin_token})
    other_id = other.headers["location"].split("/")[-1]
    item = pg_client.post(f"/projects/{project_id}/context", data={"type": "actor",
        "title": "Acteur", "content": "Description", "csrf_token": admin_token})
    item_id = item.headers["location"].split("item_id=")[-1]
    base = f"/projects/{project_id}/context/{item_id}"
    reader_id, reader_email = create_user()
    outsider_id, outsider_email = create_user()

    async def members(db):
        db.add(ProjectMember(project_id=project_id, user_id=reader_id,
                             role_code="reader", is_active=True))
    asyncio.run(with_db(members))

    pg_client.cookies.clear()
    reader_token = login(pg_client, reader_email)
    assert pg_client.get(f"/projects/{project_id}/context?item_id={item_id}").status_code == 200
    assert pg_client.get(f"/projects/{other_id}/context?item_id={item_id}").status_code == 404
    assert pg_client.post(f"{base}/obsolete", data={"expected_version": 1,
        "csrf_token": reader_token}).status_code == 403
    assert pg_client.post(f"{base}/comments", data={"body": "Avis",
        "version": 1, "csrf_token": reader_token}).status_code == 403
    assert pg_client.post(f"{base}/relations", data={"target_id": item_id,
        "type": "covers", "expected_version": 1, "csrf_token": reader_token}).status_code == 403
    pg_client.cookies.clear()
    login(pg_client, outsider_email)
    assert pg_client.get(f"/projects/{project_id}/context?item_id={item_id}").status_code == 404
    assert pg_client.post(f"{base}/obsolete", data={"expected_version": 1,
        "csrf_token": admin_token}).status_code == 403
