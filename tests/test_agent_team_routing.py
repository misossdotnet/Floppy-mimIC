"""Catalogue, équipes, permissions et réservation interne bornée."""

import asyncio
from datetime import timedelta
from uuid import uuid4

from sqlalchemy import select

from app.agent_models import (AgentPlanVersion, AgentSession, AgentTask, AgentTaskClaim,
                              AgentTeam, FlowDefinition, FlowRun, FlowVersion, SkillProfile)
from app.agent_routing import release_session_claims, reserve_task
from app.identity_models import AuditEvent, RolePermission, utcnow
from app.identity_service import database_factory

from .conftest import create_user, with_db
from .test_agent_sessions import project_with_members, team_id
from .test_identity_acl import login


async def approved_run(db, project_id, team_id, architect_id, manager_id):
    definition = FlowDefinition(project_id=project_id, team_id=team_id,
                                name="Flux de test", created_by=architect_id)
    db.add(definition)
    await db.flush()
    version = FlowVersion(flow_definition_id=definition.id, version_number=1,
        graph={"nodes": [{"id": "agent_team", "type": "agent_team", "team_id": team_id}],
               "edges": []}, status="validated", validated_by=architect_id,
        validated_at=utcnow())
    db.add(version)
    await db.flush()
    run = FlowRun(project_id=project_id, team_id=team_id, flow_version_id=version.id,
                  status="running", launched_by=manager_id)
    db.add(run)
    await db.flush()
    return run.id


def test_catalogue_equipes_et_permissions_api(pg_client):
    manager_id, manager_email = create_user()
    architect_id, architect_email = create_user()
    reader_id, reader_email = create_user()
    outsider_id, outsider_email = create_user()
    project_id = project_with_members(manager_id, architect_id, reader_id)
    foreign_id = project_with_members(outsider_id, manager_id, reader_id)
    team_base = f"/api/projects/{project_id}/agent-teams"
    catalog_url = f"/api/projects/{project_id}/agent-catalog"
    pg_client.cookies.clear()
    login(pg_client, reader_email)
    catalog = pg_client.get(catalog_url)
    assert catalog.status_code == 200
    assert set(catalog.json()["clients"]) == {"claude_code", "codex", "opencode", "cursor",
                                               "windsurf", "github_copilot", "kiro", "compatible"}
    assert "task.claim" in catalog.json()["capabilities"]
    assert len(catalog.json()["skill_profiles"]) == 7
    assert all(profile["version_number"] == 1 for profile in catalog.json()["skill_profiles"])
    assert pg_client.post(team_base, json={"name": "Nouvelle", "skill_codes": ["backend"]},
                          headers={"X-CSRF-Token": login(pg_client, reader_email)}).status_code == 403
    pg_client.cookies.clear()
    login(pg_client, outsider_email)
    assert pg_client.get(team_base).status_code == 404

    pg_client.cookies.clear()
    architect_csrf = login(pg_client, architect_email)
    assert pg_client.post(team_base, json={"name": "Nouvelle", "skill_codes": ["backend"]}).status_code == 403
    assert pg_client.post(team_base, json={"name": "Nouvelle", "skill_codes": ["inconnu"]},
                          headers={"X-CSRF-Token": architect_csrf}).status_code == 422
    created = pg_client.post(team_base, json={"name": "Nouvelle", "skill_codes": ["backend"]},
                             headers={"X-CSRF-Token": architect_csrf})
    assert created.status_code == 201
    new_team_id = created.json()["id"]
    assert created.json()["skill_codes"] == ["backend"]
    assert pg_client.post(team_base, json={"name": "Nouvelle", "skill_codes": ["backend"]},
                          headers={"X-CSRF-Token": architect_csrf}).status_code == 422
    session_base = f"/api/projects/{project_id}/agent-sessions"
    assert pg_client.post(session_base, json={"team_id": new_team_id, "client_code": "codex",
        "skill_code": "frontend", "capabilities": ["session.heartbeat"]},
        headers={"X-CSRF-Token": architect_csrf}).status_code == 422
    assert pg_client.post(f"/api/projects/{foreign_id}/agent-sessions", json={
        "team_id": new_team_id, "client_code": "codex", "skill_code": "backend",
        "capabilities": ["session.heartbeat"]}, headers={"X-CSRF-Token": architect_csrf}).status_code == 404


def test_claim_et_message_restent_bloques_avant_flux_valide(pg_client):
    manager_id, manager_email = create_user()
    architect_id, architect_email = create_user()
    reader_id, _ = create_user()
    project_id = project_with_members(manager_id, architect_id, reader_id)
    base = f"/api/projects/{project_id}"
    csrf = login(pg_client, manager_email)
    payload = {"team_id": team_id(project_id), "client_code": "codex", "skill_code": "backend",
               "capabilities": ["session.heartbeat", "task.claim", "task.release", "message.send"]}
    created = pg_client.post(f"{base}/agent-sessions", json=payload,
                             headers={"X-CSRF-Token": csrf})
    assert created.status_code == 201
    session_id, token = created.json()["session"]["id"], created.json()["token"]
    headers = {"Authorization": f"Bearer {token}"}
    task_id = str(uuid4())
    assert pg_client.post(f"{base}/agent-tasks/{task_id}/claim?session_id={session_id}",
                          headers=headers).status_code == 409
    assert pg_client.post(f"{base}/agent-messages", json={"session_id": session_id,
        "task_id": task_id, "request_key": str(uuid4()), "summary": "Synthèse"}, headers=headers).status_code == 409
    assert pg_client.post(f"{base}/agent-tasks/{task_id}/claim?session_id={session_id}").status_code == 401
    no_message = pg_client.post(f"{base}/agent-sessions", json={**payload,
        "capabilities": ["session.heartbeat"]}, headers={"X-CSRF-Token": csrf}).json()
    limited = {"Authorization": f"Bearer {no_message['token']}"}
    assert pg_client.post(f"{base}/agent-messages", json={
        "session_id": no_message["session"]["id"], "task_id": task_id,
        "request_key": str(uuid4()), "summary": "Synthèse"}, headers=limited).status_code == 403
    assert pg_client.post(f"{base}/agent-tasks/{task_id}/claim?session_id={no_message['session']['id']}",
                          headers=limited).status_code == 403

    async def remove_permissions(db):
        for code in ("agent.task.claim", "agent.message.send"):
            grant = await db.get(RolePermission, ("project_manager", code))
            await db.delete(grant)
    asyncio.run(with_db(remove_permissions))
    assert pg_client.post(f"{base}/agent-tasks/{task_id}/claim?session_id={session_id}",
                          headers=headers).status_code == 403
    assert pg_client.post(f"{base}/agent-messages", json={"session_id": session_id,
        "task_id": task_id, "request_key": str(uuid4()), "summary": "Synthèse"}, headers=headers).status_code == 403
    assert pg_client.delete(f"{base}/agent-sessions/{session_id}",
                            headers={"X-CSRF-Token": csrf}).status_code == 200
    async def denied_events(db):
        return (await db.scalars(select(AuditEvent).where(
            AuditEvent.project_id == project_id, AuditEvent.outcome == "denied"))).all()
    assert {"agent.task.claim", "agent.message.send"} <= {
        event.action for event in asyncio.run(with_db(denied_events))}


def test_flux_plan_approuves_ouvrent_le_routage_et_le_claim(pg_client):
    manager_id, manager_email = create_user()
    architect_id, architect_email = create_user()
    reader_id, reader_email = create_user()
    project_id = project_with_members(manager_id, architect_id, reader_id)
    base = f"/api/projects/{project_id}"
    team = team_id(project_id)
    pg_client.cookies.clear()
    reader_csrf = login(pg_client, reader_email)
    assert pg_client.post(f"{base}/agent-flows", json={"name": "Flux", "team_id": team},
        headers={"X-CSRF-Token": reader_csrf}).status_code == 403
    pg_client.cookies.clear()
    architect_csrf = login(pg_client, architect_email)
    created = pg_client.post(f"{base}/agent-flows", json={"name": "Flux", "team_id": team},
        headers={"X-CSRF-Token": architect_csrf})
    assert created.status_code == 201
    version_id = created.json()["version_id"]
    assert pg_client.post(f"{base}/agent-flows/{version_id}/validate",
        headers={"X-CSRF-Token": architect_csrf}).status_code == 409
    assert pg_client.post(f"{base}/agent-flows/{version_id}/review",
        headers={"X-CSRF-Token": architect_csrf}).status_code == 200
    pg_client.cookies.clear()
    manager_csrf = login(pg_client, manager_email)
    assert pg_client.post(f"{base}/agent-flows/{version_id}/validate",
        headers={"X-CSRF-Token": manager_csrf}).status_code == 403
    assert pg_client.post(f"{base}/agent-flows/{version_id}/runs",
        headers={"X-CSRF-Token": manager_csrf}).status_code == 409
    pg_client.cookies.clear()
    architect_csrf = login(pg_client, architect_email)
    assert pg_client.post(f"{base}/agent-flows/{version_id}/validate",
        headers={"X-CSRF-Token": architect_csrf}).status_code == 200
    pg_client.cookies.clear()
    manager_csrf = login(pg_client, manager_email)
    run = pg_client.post(f"{base}/agent-flows/{version_id}/runs",
        headers={"X-CSRF-Token": manager_csrf})
    assert run.status_code == 201
    run_id = run.json()["run_id"]
    session = pg_client.post(f"{base}/agent-sessions", json={"team_id": team,
        "client_code": "codex", "skill_code": "backend",
        "capabilities": ["task.claim", "task.release", "session.heartbeat"]},
        headers={"X-CSRF-Token": manager_csrf})
    assert session.status_code == 201
    session_id = session.json()["session"]["id"]
    auth = {"Authorization": f"Bearer {session.json()['token']}"}
    plan = pg_client.post(f"{base}/agent-flows/runs/{run_id}/plans", json={"tasks": [
        {"task_key": "api", "title": "Implémenter l'API", "skill_code": "backend"},
        {"task_key": "ui", "title": "Implémenter l'interface", "skill_code": "frontend"}]},
        headers={"X-CSRF-Token": manager_csrf})
    assert plan.status_code == 201
    plan_id = plan.json()["plan_id"]
    async def tasks_for_plan(db):
        return {task.task_key: task.id for task in (await db.scalars(select(AgentTask).where(
            AgentTask.plan_version_id == plan_id))).all()}
    task_ids = asyncio.run(with_db(tasks_for_plan))
    task_id = task_ids["api"]
    claim_url = f"{base}/agent-tasks/{task_id}/claim?session_id={session_id}"
    assert pg_client.get(f"{base}/agent-tasks/ready?session_id={session_id}",
        headers=auth).json()["tasks"] == []
    assert pg_client.post(claim_url, headers=auth).status_code == 409
    assert pg_client.post(f"{base}/agent-flows/plans/{plan_id}/approve",
        headers={"X-CSRF-Token": manager_csrf}).status_code == 409
    assert pg_client.post(f"{base}/agent-flows/plans/{plan_id}/review",
        headers={"X-CSRF-Token": manager_csrf}).status_code == 200
    assert pg_client.post(claim_url, headers=auth).status_code == 409
    assert pg_client.post(f"{base}/agent-flows/plans/{plan_id}/approve",
        headers={"X-CSRF-Token": manager_csrf}).status_code == 200
    assert pg_client.post(f"{base}/agent-tasks/{task_ids['ui']}/claim?session_id={session_id}",
        headers=auth).status_code == 409
    assert [item["id"] for item in pg_client.get(
        f"{base}/agent-tasks/ready?session_id={session_id}", headers=auth).json()["tasks"]] == [task_id]
    first = pg_client.post(claim_url, headers=auth)
    assert first.status_code == 200
    assert pg_client.post(claim_url, headers=auth).json()["claim_id"] == first.json()["claim_id"]
    async def expire_lease(db):
        task = await db.get(AgentTask, task_id)
        task.lease_expires_at = utcnow() - timedelta(seconds=1)
    asyncio.run(with_db(expire_lease))
    assert pg_client.get(f"{base}/agent-tasks/ready?session_id={session_id}", headers=auth).json()["tasks"] == []
    async def pass_retry_delay(db):
        (await db.get(AgentTask, task_id)).retry_not_before = utcnow() - timedelta(seconds=1)
    asyncio.run(with_db(pass_retry_delay))
    assert pg_client.post(claim_url, headers=auth).json()["claim_id"] != first.json()["claim_id"]
    assert pg_client.post(f"{base}/agent-tasks/{task_id}/release?session_id={session_id}",
        headers=auth).json() == {"released": True}
    assert pg_client.post(f"{base}/agent-tasks/{task_id}/release?session_id={session_id}",
        headers=auth).json() == {"released": False}
    async def audit_actions(db):
        return {event.action for event in (await db.scalars(select(AuditEvent).where(
            AuditEvent.project_id == project_id))).all()}
    assert {"flow.validate", "flow.execute", "agent.plan.approve", "agent.task.claim",
            "agent.task.release"} <= asyncio.run(with_db(audit_actions))


def test_reservation_concurrente_et_liberation_idempotente(pg_client):
    manager_id, manager_email = create_user()
    architect_id, _ = create_user()
    reader_id, _ = create_user()
    project_id = project_with_members(manager_id, architect_id, reader_id)
    csrf = login(pg_client, manager_email)
    base = f"/api/projects/{project_id}/agent-sessions"
    payload = {"team_id": team_id(project_id), "client_code": "codex", "skill_code": "backend",
               "capabilities": ["session.heartbeat", "task.claim", "task.release"]}
    first = pg_client.post(base, json=payload, headers={"X-CSRF-Token": csrf}).json()
    second = pg_client.post(base, json=payload, headers={"X-CSRF-Token": csrf}).json()
    first_id, second_id = first["session"]["id"], second["session"]["id"]

    async def seed(db):
        profile = await db.scalar(select(SkillProfile).where(SkillProfile.code == "backend"))
        run_id = await approved_run(db, project_id, payload["team_id"], architect_id, manager_id)
        plan = AgentPlanVersion(project_id=project_id, team_id=payload["team_id"],
            flow_run_id=run_id, validated_flow_run_id=run_id,
            status="approved", approved_by=manager_id, approved_at=utcnow())
        db.add(plan)
        await db.flush()
        task = AgentTask(project_id=project_id, plan_version_id=plan.id,
            team_id=payload["team_id"], skill_profile_id=profile.id,
            task_key="api", title="Implémenter l'API")
        db.add(task)
        await db.flush()
        return task.id
    task_id = asyncio.run(with_db(seed))

    async def race():
        factory = database_factory()
        try:
            async def attempt(session_id):
                async with factory() as db:
                    session = await db.get(AgentSession, session_id)
                    try:
                        claim = await reserve_task(db, session=session, task_id=task_id)
                        await db.commit()
                        return (session_id, claim.id)
                    except ValueError:
                        await db.rollback()
                        return (session_id, None)
            return await asyncio.gather(attempt(first_id), attempt(second_id))
        finally:
            await factory.kw["bind"].dispose()
    results = asyncio.run(race())
    assert sum(claim_id is not None for _, claim_id in results) == 1
    winner_id = next(session_id for session_id, claim_id in results if claim_id)
    loser_id = second_id if winner_id == first_id else first_id

    async def release(db):
        session = await db.get(AgentSession, winner_id)
        first_count = await release_session_claims(db, session, "session_expired")
        second_count = await release_session_claims(db, session, "session_expired")
        return first_count, second_count
    assert asyncio.run(with_db(release)) == (1, 0)

    async def reclaim(db):
        session = await db.get(AgentSession, loser_id)
        claim = await reserve_task(db, session=session, task_id=task_id)
        replay = await reserve_task(db, session=session, task_id=task_id)
        return claim.id, replay.id
    async def pass_retry_delay(db):
        (await db.get(AgentTask, task_id)).retry_not_before = utcnow() - timedelta(seconds=1)
    asyncio.run(with_db(pass_retry_delay))
    claim_id, replay_id = asyncio.run(with_db(reclaim))
    assert claim_id == replay_id

    async def verify(db):
        task = await db.get(AgentTask, task_id)
        claims = (await db.scalars(select(AgentTaskClaim).where(AgentTaskClaim.task_id == task_id))).all()
        events = (await db.scalars(select(AuditEvent).where(AuditEvent.target_id == task_id))).all()
        return task, claims, events
    task, claims, events = asyncio.run(with_db(verify))
    assert task.status == "claimed" and task.claimed_session_id == loser_id
    assert len(claims) == 2 and sum(c.released_at is None for c in claims) == 1
    assert {"agent.task.claim", "agent.task.release"} <= {event.action for event in events}


def test_deconnexion_et_expiration_liberent_les_taches(pg_client):
    manager_id, manager_email = create_user()
    architect_id, _ = create_user()
    reader_id, _ = create_user()
    project_id = project_with_members(manager_id, architect_id, reader_id)
    csrf = login(pg_client, manager_email)
    base = f"/api/projects/{project_id}/agent-sessions"
    payload = {"team_id": team_id(project_id), "client_code": "codex", "skill_code": "backend",
               "capabilities": ["session.heartbeat", "session.disconnect", "task.claim"]}
    sessions = [pg_client.post(base, json=payload, headers={"X-CSRF-Token": csrf}).json()
                for _ in range(2)]

    async def seed(db):
        profile = await db.scalar(select(SkillProfile).where(SkillProfile.code == "backend"))
        run_id = await approved_run(db, project_id, payload["team_id"], architect_id, manager_id)
        plan = AgentPlanVersion(project_id=project_id, team_id=payload["team_id"],
            flow_run_id=run_id, validated_flow_run_id=run_id,
            status="approved", approved_by=manager_id, approved_at=utcnow())
        db.add(plan)
        await db.flush()
        tasks = [AgentTask(project_id=project_id, plan_version_id=plan.id,
            team_id=payload["team_id"], skill_profile_id=profile.id,
            task_key=f"task-{i}", title=f"Tâche {i}") for i in range(2)]
        db.add_all(tasks)
        await db.flush()
        for session_data, task in zip(sessions, tasks):
            session = await db.get(AgentSession, session_data["session"]["id"])
            await reserve_task(db, session=session, task_id=task.id)
        return [task.id for task in tasks]
    task_ids = asyncio.run(with_db(seed))

    first_id = sessions[0]["session"]["id"]
    assert pg_client.post(f"{base}/{first_id}/disconnect", headers={
        "Authorization": f"Bearer {sessions[0]['token']}"}).status_code == 200
    async def expire_second(db):
        session = await db.get(AgentSession, sessions[1]["session"]["id"])
        session.expires_at = utcnow() - timedelta(seconds=1)
    asyncio.run(with_db(expire_second))
    second_id = sessions[1]["session"]["id"]
    assert pg_client.post(f"{base}/{second_id}/heartbeat", headers={
        "Authorization": f"Bearer {sessions[1]['token']}"}).status_code == 401

    async def verify(db):
        tasks = [await db.get(AgentTask, task_id) for task_id in task_ids]
        claims = (await db.scalars(select(AgentTaskClaim).where(
            AgentTaskClaim.task_id.in_(task_ids)))).all()
        return tasks, claims
    tasks, claims = asyncio.run(with_db(verify))
    assert all(task.status == "ready" and task.claimed_session_id is None for task in tasks)
    assert sorted(claim.release_reason for claim in claims) == ["session_expired", "session_revoked"]
