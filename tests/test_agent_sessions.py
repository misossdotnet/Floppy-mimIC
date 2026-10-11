"""Enregistrement, authentification et isolation des sessions techniques."""

import asyncio
from datetime import timedelta

from sqlalchemy import select

from app.agent_models import AgentSession, AgentTeam, AgentTeamSkill, SkillProfile
from app.identity_models import AuditEvent, Project, ProjectMember
from app.identity_models import utcnow

from .conftest import create_user, with_db
from .test_identity_acl import login


def project_with_members(manager_id: str, member_id: str, reader_id: str) -> str:
    async def save(db):
        project = Project(name="Projet agents", description="", owner_id=manager_id)
        db.add(project)
        await db.flush()
        for user_id, role in ((manager_id, "project_manager"), (member_id, "architect"),
                              (reader_id, "reader")):
            db.add(ProjectMember(project_id=project.id, user_id=user_id, role_code=role))
        team = AgentTeam(project_id=project.id, name="Équipe de test", created_by=manager_id)
        db.add(team)
        await db.flush()
        profiles = (await db.scalars(select(SkillProfile))).all()
        for profile in profiles:
            db.add(AgentTeamSkill(team_id=team.id, skill_profile_id=profile.id))
        return project.id
    return asyncio.run(with_db(save))


def team_id(project_id: str) -> str:
    async def load(db):
        return (await db.scalar(select(AgentTeam).where(AgentTeam.project_id == project_id))).id
    return asyncio.run(with_db(load))


def test_session_mcp_authentification_expiration_et_audit(pg_client):
    manager_id, manager_email = create_user()
    member_id, member_email = create_user()
    reader_id, reader_email = create_user()
    project_id = project_with_members(manager_id, member_id, reader_id)
    _, outsider_email = create_user()
    manager_csrf = login(pg_client, manager_email)
    base = f"/api/projects/{project_id}/agent-sessions"
    payload = {"team_id": team_id(project_id), "client_code": "codex", "skill_code": "backend",
               "capabilities": ["session.heartbeat", "session.disconnect"]}
    assert pg_client.post(base, json=payload).status_code == 403
    unknown = pg_client.post(base, json={**payload, "capabilities": ["shell.execute"]},
                             headers={"X-CSRF-Token": manager_csrf})
    assert unknown.status_code == 422
    created = pg_client.post(base, json=payload, headers={"X-CSRF-Token": manager_csrf})
    assert created.status_code == 201, created.text
    assert created.headers["cache-control"] == "no-store"
    session_id = created.json()["session"]["id"]
    token = created.json()["token"]
    assert token not in pg_client.get(base).text
    assert len(token) > 40
    beat_url = f"{base}/{session_id}/heartbeat"
    assert pg_client.post(beat_url).status_code == 401
    assert pg_client.post(beat_url, headers={"Authorization": f"Bearer {token}"}).status_code == 200

    other_id = project_with_members(manager_id, member_id, reader_id)
    assert pg_client.post(f"/api/projects/{other_id}/agent-sessions/{session_id}/heartbeat",
                          headers={"Authorization": f"Bearer {token}"}).status_code == 401

    async def expire(db):
        session = await db.get(AgentSession, session_id)
        session.expires_at = utcnow() - timedelta(seconds=1)
    asyncio.run(with_db(expire))
    assert pg_client.post(beat_url, headers={"Authorization": f"Bearer {token}"}).status_code == 401

    async def verify(db):
        session = await db.get(AgentSession, session_id)
        events = (await db.scalars(select(AuditEvent).where(AuditEvent.target_id == session_id))).all()
        return session, events
    session, events = asyncio.run(with_db(verify))
    assert session.status == "expired"
    assert session.token_hash != token
    assert {"agent.session.connect", "agent.session.heartbeat", "agent.session.expire"} <= {e.action for e in events}
    assert token not in str([e.details for e in events])

    pg_client.cookies.clear()
    login(pg_client, reader_email)
    assert pg_client.get(base).status_code == 200
    assert pg_client.post(base, json=payload, headers={"X-CSRF-Token": login(pg_client, reader_email)}).status_code == 403
    pg_client.cookies.clear()
    login(pg_client, outsider_email)
    assert pg_client.get(base).status_code == 404


def test_revocation_humaine_et_deconnexion_client(pg_client):
    manager_id, manager_email = create_user()
    member_id, member_email = create_user()
    reader_id, _ = create_user()
    project_id = project_with_members(manager_id, member_id, reader_id)
    base = f"/api/projects/{project_id}/agent-sessions"
    payload = {"team_id": team_id(project_id), "client_code": "compatible", "skill_code": "qa",
               "capabilities": ["session.heartbeat", "session.disconnect"]}
    manager_csrf = login(pg_client, manager_email)
    created = pg_client.post(base, json=payload, headers={"X-CSRF-Token": manager_csrf}).json()
    session_id, token = created["session"]["id"], created["token"]
    pg_client.cookies.clear()
    member_csrf = login(pg_client, member_email)
    assert pg_client.delete(f"{base}/{session_id}", headers={"X-CSRF-Token": member_csrf}).status_code == 403
    assert pg_client.post(f"{base}/{session_id}/disconnect",
                          headers={"Authorization": f"Bearer {token}"}).status_code == 200
    assert pg_client.post(f"{base}/{session_id}/heartbeat",
                          headers={"Authorization": f"Bearer {token}"}).status_code == 401
    pg_client.cookies.clear()
    manager_csrf = login(pg_client, manager_email)
    assert pg_client.delete(f"{base}/{session_id}", headers={"X-CSRF-Token": manager_csrf}).status_code == 200
    another = pg_client.post(base, json=payload, headers={"X-CSRF-Token": manager_csrf}).json()
    active_id, active_token = another["session"]["id"], another["token"]
    assert pg_client.delete(f"{base}/{active_id}", headers={"X-CSRF-Token": manager_csrf}).status_code == 200
    assert pg_client.post(f"{base}/{active_id}/heartbeat",
                          headers={"Authorization": f"Bearer {active_token}"}).status_code == 401


def test_retrait_d_acl_invalide_la_session_technique(pg_client):
    manager_id, manager_email = create_user()
    member_id, member_email = create_user()
    reader_id, _ = create_user()
    project_id = project_with_members(manager_id, member_id, reader_id)
    base = f"/api/projects/{project_id}/agent-sessions"
    member_csrf = login(pg_client, member_email)
    created = pg_client.post(base, json={"team_id": team_id(project_id), "client_code": "codex", "skill_code": "backend",
        "capabilities": ["session.heartbeat"]}, headers={"X-CSRF-Token": member_csrf}).json()
    session_id, token = created["session"]["id"], created["token"]

    async def revoke_membership(db):
        membership = await db.scalar(select(ProjectMember).where(
            ProjectMember.project_id == project_id, ProjectMember.user_id == member_id))
        membership.is_active = False
    asyncio.run(with_db(revoke_membership))
    assert pg_client.post(f"{base}/{session_id}/heartbeat",
                          headers={"Authorization": f"Bearer {token}"}).status_code == 401

    pg_client.cookies.clear()
    login(pg_client, manager_email)
    sessions = pg_client.get(base).json()["sessions"]
    assert next(item for item in sessions if item["id"] == session_id)["status"] == "revoked"
