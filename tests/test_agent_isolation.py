"""AGENT-27 : frontières projet/équipe et partage global exact.

Le modèle courant ne porte pas encore d'organisation imbriquée. Ces tests
exercent les ACL réelles, sans simuler un héritage organisationnel absent.
"""

import asyncio
from uuid import uuid4

import pytest
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from app.agent_models import AgentPlanVersion, AgentTask, SkillProfile
from app.identity_models import AuditEvent, Organization, Project, ProjectMember, User, utcnow
from .conftest import create_user, with_db
from .test_agent_conversations import emit, headers, messaging
from .test_agent_hierarchy_progress import session
from .test_agent_sessions import project_with_members, team_id
from .test_agent_team_routing import approved_run
from .test_identity_acl import login
from .test_project_memory import (as_user, create, decide, fields, global_setup,
                                  refs, setup, share)


def test_deux_projets_meme_responsable_sessions_et_messages_isoles(pg_client, messaging):
    m = messaging
    message = emit(pg_client, m)
    architect_id = asyncio.run(with_db(lambda db: db.scalar(select(ProjectMember.user_id).where(
        ProjectMember.project_id == m["project"], ProjectMember.role_code == "architect"))))
    other_project = project_with_members(m["manager"], architect_id, create_user()[0])
    other_team = team_id(other_project)
    manager_email = asyncio.run(with_db(lambda db: db.scalar(select(User.email).where(User.id == m["manager"]))))
    csrf = login(pg_client, manager_email)
    other_base = f"/api/projects/{other_project}"
    created = pg_client.post(other_base + "/agent-sessions", json={
        "team_id": other_team, "client_code": "codex", "skill_code": "frontend",
        "capabilities": ["session.heartbeat", "task.claim", "message.read", "message.ack", "memory.read"]},
        headers={"X-CSRF-Token": csrf})
    assert created.status_code == 201, created.text
    foreign = created.json()
    foreign_id = foreign["session"]["id"]
    foreign_auth = {"Authorization": "Bearer " + foreign["token"]}
    own_frontend = m["sessions"]["frontend"]["session"]["id"]

    assert pg_client.get(m["base"] + "/agent-messages", params={"session_id": foreign_id},
                         headers=foreign_auth).status_code == 401
    assert pg_client.post(other_base + "/agent-messages/poll", json={"session_id": own_frontend},
                          headers=headers(m, "frontend")).status_code == 401
    assert pg_client.post(other_base + "/agent-messages/poll", json={"session_id": foreign_id},
                          headers=foreign_auth).json()["messages"] == []
    assert pg_client.post(other_base + f"/agent-messages/{message['id']}/ack",
                          json={"session_id": foreign_id}, headers=foreign_auth).status_code == 404
    assert pg_client.post(other_base + f"/agent-tasks/{m['tasks']['frontend']}/claim",
                          params={"session_id": foreign_id}, headers=foreign_auth).status_code == 409
    assert pg_client.get(other_base + "/agent-memory/context", params={"session_id": foreign_id},
                         headers=foreign_auth).json()["references"] == []

    async def unchanged(db):
        from app.conversation_models import AgentMessageDelivery
        delivery = await db.scalar(select(AgentMessageDelivery).where(
            AgentMessageDelivery.message_id == message["id"]))
        assert delivery.project_id == m["project"] and delivery.status == "pending"
        assert await db.scalar(select(AgentTask.attempt_count).where(
            AgentTask.id == m["tasks"]["frontend"])) == 1
    asyncio.run(with_db(unchanged))


def test_deux_equipes_meme_projet_profil_identique_sans_receipt_croise(pg_client, messaging):
    m = messaging
    message = emit(pg_client, m)
    architect_id = asyncio.run(with_db(lambda db: db.scalar(select(ProjectMember.user_id).where(
        ProjectMember.project_id == m["project"], ProjectMember.role_code == "architect"))))
    architect_email = asyncio.run(with_db(lambda db: db.scalar(select(User.email).where(User.id == architect_id))))
    pg_client.cookies.clear()
    csrf = login(pg_client, architect_email)
    response = pg_client.post(m["base"] + "/agent-teams", json={
        "name": "Équipe parallèle", "skill_codes": ["backend", "frontend"]},
        headers={"X-CSRF-Token": csrf})
    assert response.status_code == 201, response.text
    second_team = response.json()["id"]
    async def second_task(db):
        run_id = await approved_run(db, m["project"], second_team, architect_id, m["manager"])
        plan = AgentPlanVersion(project_id=m["project"], team_id=second_team,
                                validated_flow_run_id=run_id, status="approved",
                                approved_by=m["manager"], approved_at=utcnow())
        db.add(plan)
        await db.flush()
        profile = await db.scalar(select(SkillProfile).where(SkillProfile.code == "frontend"))
        task = AgentTask(project_id=m["project"], team_id=second_team,
                         plan_version_id=plan.id, skill_profile_id=profile.id,
                         task_key="frontend", title="Frontend parallèle")
        db.add(task)
        await db.flush()
        return task.id
    task_id = asyncio.run(with_db(second_task))
    manager_email = asyncio.run(with_db(lambda db: db.scalar(select(User.email).where(User.id == m["manager"]))))
    pg_client.cookies.clear()
    csrf = login(pg_client, manager_email)
    connected = pg_client.post(m["base"] + "/agent-sessions", json={
        "team_id": second_team, "client_code": "codex", "skill_code": "frontend",
        "capabilities": ["session.heartbeat", "task.claim", "message.read", "message.ack"]},
        headers={"X-CSRF-Token": csrf})
    assert connected.status_code == 201, connected.text
    second = connected.json()
    sid = second["session"]["id"]
    auth = {"Authorization": "Bearer " + second["token"]}

    assert pg_client.get(m["base"] + "/agent-messages", params={"session_id": sid},
                         headers=auth).json()["messages"] == []
    assert pg_client.post(m["base"] + "/agent-messages/poll", json={"session_id": sid},
                          headers=auth).json()["messages"] == []
    assert pg_client.post(m["base"] + f"/agent-messages/{message['id']}/ack",
                          json={"session_id": sid}, headers=auth).status_code == 404
    assert pg_client.post(m["base"] + f"/agent-tasks/{m['tasks']['frontend']}/claim",
                          params={"session_id": sid}, headers=auth).status_code == 409
    first = m["sessions"]["frontend"]
    assert pg_client.post(m["base"] + f"/agent-tasks/{task_id}/claim",
                          params={"session_id": first["session"]["id"]},
                          headers={"Authorization": "Bearer " + first["token"]}).status_code == 409
    claimed = pg_client.post(m["base"] + f"/agent-tasks/{task_id}/claim",
                             params={"session_id": sid}, headers=auth)
    assert claimed.status_code == 200, claimed.text
    assert claimed.json()["task_id"] == task_id


def test_memoires_globales_partagees_par_projet_et_version_exacte(pg_client):
    s = setup(pg_client)
    sibling = project_with_members(s["manager_id"], s["architect_id"], s["reader_id"])
    own = create(pg_client, s, content="Mémoire privée du premier projet")
    assert decide(pg_client, s, own["id"], "approve", 1).status_code == 200
    g = global_setup(pg_client, s)
    global_memory = create(pg_client, g, kind="convention", content="Convention globale version un")
    assert decide(pg_client, g, global_memory["id"], "approve", 1).status_code == 200
    assert share(pg_client, g, global_memory["id"], s["project"], 2).status_code == 200

    as_user(pg_client, s, "reader")
    assert {r["memory_id"] for r in refs(pg_client, s)["references"]} == {own["id"], global_memory["id"]}
    other_url = f"/api/projects/{sibling}/memories"
    assert pg_client.get(other_url + "/context").json()["references"] == []
    assert pg_client.get(other_url + f"/{own['id']}").status_code == 404
    assert pg_client.get(other_url + f"/{global_memory['id']}").status_code == 404
    assert pg_client.get("/api/global-memories").status_code == 403

    as_user(pg_client, g, "admin")
    assert share(pg_client, g, global_memory["id"], sibling, 2).status_code == 200
    assert edit_global(pg_client, g, global_memory["id"], 2).status_code == 200
    assert decide(pg_client, g, global_memory["id"], "approve", 3).status_code == 200
    assert {r["memory_id"] for r in refs(pg_client, s)["references"]} == {own["id"]}
    assert pg_client.get(other_url + "/context").json()["references"] == []
    assert share(pg_client, g, global_memory["id"], sibling, 2).status_code == 409
    new_grant = share(pg_client, g, global_memory["id"], sibling, 4)
    assert new_grant.status_code == 200
    assert {r["memory_id"] for r in pg_client.get(other_url + "/context").json()["references"]} == {global_memory["id"]}
    assert {r["memory_id"] for r in refs(pg_client, s)["references"]} == {own["id"]}
    async def audit(db):
        grants = (await db.scalars(select(AuditEvent).where(
            AuditEvent.action == "memory.share", AuditEvent.target_type == "project_memory_grant"))).all()
        assert [e.project_id for e in grants].count(s["project"]) == 1
        assert [e.project_id for e in grants].count(sibling) == 2
        assert all("Convention globale version" not in str(e.details) for e in grants)
    asyncio.run(with_db(audit))


def test_organisations_imbriquees_sans_heritage_acl_ni_partage(pg_client):
    root = setup(pg_client)
    child_manager, child_email = create_user()
    child_architect, _ = create_user()
    child_reader, _ = create_user()
    child_project = project_with_members(child_manager, child_architect, child_reader)
    sibling_project = project_with_members(root["manager_id"], root["architect_id"], root["reader_id"])

    async def hierarchy(db):
        top = Organization(name="Groupe")
        db.add(top)
        await db.flush()
        branch = Organization(name="Direction", parent_id=top.id)
        sibling = Organization(name="Autre direction", parent_id=top.id)
        db.add_all([branch, sibling])
        await db.flush()
        leaf = Organization(name="Filiale", parent_id=branch.id)
        db.add(leaf)
        await db.flush()
        (await db.get(Project, root["project"])).organization_id = top.id
        (await db.get(Project, child_project)).organization_id = leaf.id
        (await db.get(Project, sibling_project)).organization_id = sibling.id
        return top.id, branch.id, leaf.id, sibling.id
    top, branch, leaf, sibling = asyncio.run(with_db(hierarchy))
    assert len({top, branch, leaf, sibling}) == 4

    own = create(pg_client, root, content="Privé à la racine")
    assert decide(pg_client, root, own["id"], "approve", 1).status_code == 200
    root_agent = session(pg_client, root, ["session.heartbeat", "memory.read"])
    assert pg_client.get(f"/api/projects/{child_project}/agent-memory/context",
                         params={"session_id": root_agent[0]}, headers=root_agent[1]).status_code == 401
    assert pg_client.get(f"/api/projects/{child_project}/memories").status_code == 404
    assert pg_client.get(f"/api/projects/{child_project}/agent-teams").status_code == 404
    pg_client.cookies.clear()
    login(pg_client, child_email)
    assert pg_client.get(root["url"]).status_code == 404
    assert pg_client.get(f"/api/projects/{child_project}/memories").status_code == 200
    assert pg_client.get(f"/api/projects/{child_project}/memories/{own['id']}").status_code == 404

    global_admin = global_setup(pg_client, root)
    global_memory = create(pg_client, global_admin, kind="convention", content="Partage ciblé")
    assert decide(pg_client, global_admin, global_memory["id"], "approve", 1).status_code == 200
    assert share(pg_client, global_admin, global_memory["id"], root["project"], 2).status_code == 200
    assert pg_client.get(f"/api/projects/{child_project}/memories/context").json()["references"] == []
    assert pg_client.get(f"/api/projects/{sibling_project}/memories/context").json()["references"] == []
    assert {r["memory_id"] for r in refs(pg_client, root)["references"]} == {own["id"], global_memory["id"]}

    # L'administrateur choisit explicitement chaque projet ; la parenté ne propage rien.
    assert share(pg_client, global_admin, global_memory["id"], child_project, 2).status_code == 200
    assert {r["memory_id"] for r in pg_client.get(
        f"/api/projects/{child_project}/memories/context").json()["references"]} == {global_memory["id"]}
    assert pg_client.get(f"/api/projects/{sibling_project}/memories/context").json()["references"] == []


def test_organisation_refuse_parent_soi_meme_et_reference_absente(pg_client):
    root = setup(pg_client)
    wrong_id = str(uuid4())

    async def self_parent(db):
        db.add(Organization(id=wrong_id, name="Cycle direct", parent_id=wrong_id))
        await db.flush()
    with pytest.raises(IntegrityError):
        asyncio.run(with_db(self_parent))

    async def missing_organization(db):
        (await db.get(Project, root["project"])).organization_id = wrong_id
        await db.flush()
    with pytest.raises(IntegrityError):
        asyncio.run(with_db(missing_organization))

    async def unchanged(db):
        return (await db.get(Project, root["project"])).organization_id
    assert asyncio.run(with_db(unchanged)) is None


def edit_global(client, global_setup_data, memory_id, version):
    return client.post(global_setup_data["url"] + f"/{memory_id}/versions",
                       json={"expected_version": version, **fields(content="Convention globale version deux")},
                       headers={"X-CSRF-Token": global_setup_data["csrf"]})
