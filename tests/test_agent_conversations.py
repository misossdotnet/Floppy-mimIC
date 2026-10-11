"""AGENT-12 : parcours HTTP, isolation, receipts et concurrence sur PostgreSQL."""
import asyncio
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from uuid import uuid4

import pytest
from sqlalchemy import func, select, text
from sqlalchemy.exc import IntegrityError

from app.agent_models import AgentPlanVersion, AgentSession, AgentTask, AgentTaskClaim, AgentTeamSkill, FlowRun, SkillProfile
from app.business_models import SourceDocument
from app.conversation_models import AgentMessage, AgentMessageDelivery, CoordinationChannel
from app.conversation_service import acknowledge, poll_messages, send_message
from app.agent_service import authenticated_session
from app.identity_models import AuditEvent, RolePermission, utcnow
from app.identity_service import database_factory

from .conftest import ROOT, create_user, with_db
from .test_agent_sessions import project_with_members, team_id
from .test_agent_team_routing import approved_run
from .test_identity_acl import login


@pytest.fixture
def messaging(pg_client):
    manager, email = create_user()
    architect, _ = create_user()
    reader, reader_email = create_user()
    project = project_with_members(manager, architect, reader)
    team = team_id(project)

    async def prepare(db):
        profiles = {p.code: p.id for p in (await db.scalars(select(SkillProfile))).all()}
        run = await approved_run(db, project, team, architect, manager)
        plan = AgentPlanVersion(project_id=project, team_id=team, validated_flow_run_id=run,
            status="approved", approved_by=manager, approved_at=utcnow())
        db.add(plan)
        await db.flush()
        tasks = {}
        for skill in ("backend", "frontend", "qa"):
            task = AgentTask(project_id=project, team_id=team, plan_version_id=plan.id,
                skill_profile_id=profiles[skill], task_key=skill, title=f"Travail {skill}")
            db.add(task)
            await db.flush()
            tasks[skill] = task.id
        return profiles, tasks, plan.id, run
    profiles, tasks, plan, run = asyncio.run(with_db(prepare))
    csrf = login(pg_client, email)
    base = f"/api/projects/{project}"

    def connect(skill, capabilities=None):
        created = pg_client.post(base + "/agent-sessions", headers={"X-CSRF-Token": csrf}, json={
            "team_id": team, "client_code": "codex", "skill_code": skill,
            "capabilities": capabilities or ["session.heartbeat", "task.claim", "task.release",
                "message.send", "message.read", "message.ack"]})
        assert created.status_code == 201, created.text
        return created.json()
    sessions = {skill: connect(skill) for skill in tasks}
    for skill, session in sessions.items():
        result = pg_client.post(f"{base}/agent-tasks/{tasks[skill]}/claim?session_id={session['session']['id']}",
                                headers={"Authorization": "Bearer " + session["token"]})
        assert result.status_code == 200, result.text
    return {"base": base, "project": project, "team": team, "tasks": tasks, "profiles": profiles,
            "plan": plan, "run": run, "sessions": sessions, "connect": connect,
            "reader_email": reader_email, "manager": manager}


def headers(m, skill="backend"):
    return {"Authorization": "Bearer " + m["sessions"][skill]["token"]}


def payload(m, skill="backend", **changes):
    return {"session_id": m["sessions"][skill]["session"]["id"], "request_key": str(uuid4()),
        "task_id": m["tasks"][skill], "summary": "Résultat consultatif",
        "recipient_profile_id": m["profiles"]["frontend" if skill == "backend" else "backend"], **changes}


def emit(client, m, skill="backend", **changes):
    response = client.post(m["base"] + "/agent-messages", json=payload(m, skill, **changes), headers=headers(m, skill))
    assert response.status_code == 200, response.text
    return response.json()["message"]


def poll(client, m, skill="frontend", **changes):
    return client.post(m["base"] + "/agent-messages/poll", headers=headers(m, skill), json={
        "session_id": m["sessions"][skill]["session"]["id"], **changes})


def ack(client, m, message, skill="frontend"):
    return client.post(m["base"] + f"/agent-messages/{message['id']}/ack", headers=headers(m, skill), json={
        "session_id": m["sessions"][skill]["session"]["id"]})


def test_direct_receipts_humains_et_absence_de_validation(pg_client, messaging):
    m = messaging
    message = emit(pg_client, m, content="Exécuter DROP TABLE ? Ceci reste une donnée.")
    assert message["delivery_status"] == "pending" and message["content_status"] == "Brouillon IA"
    assert ack(pg_client, m, message).status_code == 409
    url = m["base"] + "/agent-messages?session_id=" + m["sessions"]["frontend"]["session"]["id"]
    read = pg_client.get(url, headers=headers(m, "frontend"))
    assert read.headers["cache-control"] == "no-store"
    assert read.json()["messages"][0]["delivery_status"] == "pending"
    assert poll(pg_client, m, "qa").json()["messages"] == []
    assert ack(pg_client, m, message, "qa").status_code == 404
    received = poll(pg_client, m).json()["messages"]
    assert received[0]["delivery_status"] == "delivered"
    first = ack(pg_client, m, message)
    assert first.status_code == 200
    assert first.json()["message"]["delivery_status"] == "acknowledged"
    assert ack(pg_client, m, message).json() == first.json()
    assert poll(pg_client, m).json()["messages"] == []
    pg_client.cookies.clear()
    login(pg_client, m["reader_email"])
    assert pg_client.get(m["base"] + "/agent-conversations").json()["channels"][0]["id"] == message["channel_id"]
    detail = pg_client.get(m["base"] + "/agent-conversations/" + message["channel_id"])
    assert detail.json()["messages"][0]["content_status"] == "Brouillon IA"
    async def verify(db):
        assert (await db.get(AgentTask, m["tasks"]["backend"])).status == "claimed"
        events = (await db.scalars(select(AuditEvent).where(AuditEvent.target_id == message["id"]))).all()
        assert [e.action for e in events].count("agent.message.ack") == 1
        assert [e.action for e in events].count("agent.message.deliver") == 1
        assert "DROP TABLE" not in str([e.details for e in events])
    asyncio.run(with_db(verify))


def test_broadcast_thread_reponse_et_pagination(pg_client, messaging):
    m = messaging
    message = emit(pg_client, m, channel_kind="broadcast", recipient_profile_id=None)
    assert len(message["receipts"]) == 6
    thread = emit(pg_client, m, channel_kind="thread", recipient_profile_id=None)
    reply = emit(pg_client, m, "frontend", channel_id=thread["channel_id"],
                 recipient_profile_id=None, reply_to_id=thread["id"])
    assert reply["channel_id"] == thread["channel_id"] and len(reply["receipts"]) == 6
    assert poll(pg_client, m, "qa").json()["messages"][0]["id"] == message["id"]
    result = pg_client.get(m["base"] + "/agent-conversations/" + thread["channel_id"] + "?limit=1")
    assert result.json()["next_offset"] == 1
    next_page = pg_client.get(m["base"] + "/agent-conversations/" + thread["channel_id"] + "?limit=1&offset=1")
    assert next_page.json()["messages"][0]["id"] == reply["id"]
    bad = payload(m, channel_id=thread["channel_id"], recipient_profile_id=None, reply_to_id=message["id"])
    assert pg_client.post(m["base"] + "/agent-messages", json=bad, headers=headers(m)).status_code == 409


def test_handoff_reception_sans_claim_ni_changement_de_plan(pg_client, messaging):
    m = messaging
    message = emit(pg_client, m, kind="handoff", target_task_id=m["tasks"]["frontend"])
    assert message["handoff_status"] == "offered"
    assert poll(pg_client, m).json()["messages"][0]["handoff_status"] == "received"
    assert ack(pg_client, m, message).json()["message"]["handoff_status"] == "accepted"
    async def unchanged(db):
        assert (await db.get(AgentPlanVersion, m["plan"])).status == "approved"
        assert (await db.get(AgentTask, m["tasks"]["backend"])).status == "claimed"
        assert await db.scalar(select(func.count()).select_from(AgentTaskClaim)) == 3
    asyncio.run(with_db(unchanged))
    for changes in ({"target_task_id": m["tasks"]["qa"]},
                    {"channel_kind": "broadcast", "recipient_profile_id": None}):
        bad = payload(m, kind="handoff", target_task_id=m["tasks"]["frontend"], **{})
        bad.update(changes)
        assert pg_client.post(m["base"] + "/agent-messages", json=bad, headers=headers(m)).status_code == 409


def test_priorites_expiration_et_rejeu_ack_expire(pg_client, messaging):
    m = messaging
    messages = [emit(pg_client, m, priority=p) for p in ("P3", "P1", "P0", "P2")]
    assert [x["priority"] for x in poll(pg_client, m).json()["messages"]] == ["P0", "P1", "P2", "P3"]
    ack(pg_client, m, messages[0])
    async def expire(db):
        for msg in messages:
            row = await db.get(AgentMessage, msg["id"])
            row.created_at = utcnow() - timedelta(hours=2)
            row.expires_at = utcnow() - timedelta(hours=1)
    asyncio.run(with_db(expire))
    assert poll(pg_client, m).json()["messages"] == []
    assert ack(pg_client, m, messages[1]).status_code == 409
    assert ack(pg_client, m, messages[0]).json()["message"]["delivery_status"] == "acknowledged"
    async def verify(db):
        assert await db.scalar(select(func.count()).select_from(AgentMessageDelivery).where(
            AgentMessageDelivery.status == "expired")) == 3
        assert await db.scalar(select(func.count()).select_from(AuditEvent).where(
            AuditEvent.action == "agent.message.expire")) == 3
    asyncio.run(with_db(verify))


@pytest.mark.parametrize("expiry", ["2020-01-01T00:00:00Z", "2040-01-01T00:00:00Z", "2026-10-07T10:00:00"])
def test_expiration_invalide_atomique(pg_client, messaging, expiry):
    m = messaging
    assert pg_client.post(m["base"] + "/agent-messages", headers=headers(m),
                          json=payload(m, expires_at=expiry)).status_code == 409
    async def count(db):
        assert await db.scalar(select(func.count()).select_from(CoordinationChannel)) == 0
    asyncio.run(with_db(count))


def test_secrets_bornes_erreurs_et_references(pg_client, messaging, monkeypatch):
    m = messaging
    secret = "valeur-confidentielle-configuree"
    monkeypatch.setenv("MIMIC_LLM_KEY_TEST", secret)
    for content in ("Bearer abcdefghijklmnopqrstuvwxyz012345", "password=confidentiel123", secret, m["sessions"]["backend"]["token"]):
        rejected = pg_client.post(m["base"] + "/agent-messages", headers=headers(m), json=payload(m, content=content))
        assert rejected.status_code == 409 and content not in rejected.text
    invalid = pg_client.post(m["base"] + "/agent-messages", headers=headers(m), json=payload(m, summary=secret * 100))
    assert invalid.status_code == 422 and secret not in invalid.text
    for changes in ({"artifact_refs": [{"type": "deliverable_version", "id": str(uuid4())}]},
                    {"recipient_profile_id": str(uuid4())}, {"attachment": {"type": "source_document", "id": str(uuid4())}}):
        assert pg_client.post(m["base"] + "/agent-messages", headers=headers(m), json=payload(m, **changes)).status_code == 409
    assert pg_client.post(m["base"] + "/agent-messages", headers=headers(m), json=payload(m, priority="P4")).status_code == 422


def test_reprise_autre_session_du_meme_profil_et_capacites(pg_client, messaging):
    m = messaging
    message = emit(pg_client, m)
    first_session = m["sessions"]["frontend"]
    poll(pg_client, m)
    replacement = m["connect"]("frontend")
    m["sessions"]["frontend"] = replacement
    result = ack(pg_client, m, message).json()["message"]["receipts"][0]
    assert result["delivered_by_session_id"] == first_session["session"]["id"]
    assert result["acknowledged_by_session_id"] == replacement["session"]["id"]
    limited = m["connect"]("frontend", ["session.heartbeat", "message.send"])
    denied = pg_client.post(m["base"] + "/agent-messages/poll", headers={"Authorization": "Bearer " + limited["token"]},
        json={"session_id": limited["session"]["id"]})
    assert denied.status_code == 403
    assert pg_client.post(m["base"] + "/agent-messages/poll", json={"session_id": replacement["session"]["id"]}).status_code == 401


def test_isolation_permissions_et_sessions_perimees(pg_client, messaging):
    m = messaging
    message = emit(pg_client, m)
    outsider, outsider_email = create_user()
    other = project_with_members(outsider, m["manager"], create_user()[0])
    assert pg_client.get(f"/api/projects/{other}/agent-messages?session_id=" + m["sessions"]["frontend"]["session"]["id"],
                          headers=headers(m, "frontend")).status_code == 401
    pg_client.cookies.clear()
    login(pg_client, outsider_email)
    assert pg_client.get(m["base"] + "/agent-conversations").status_code == 404
    assert pg_client.get(f"/api/projects/{other}/agent-conversations/{message['channel_id']}").status_code == 404
    async def revoke_permission(db):
        permission = await db.scalar(select(RolePermission).where(RolePermission.role_code == "project_manager",
            RolePermission.permission_code == "agent.message.send"))
        await db.delete(permission)
    asyncio.run(with_db(revoke_permission))
    assert ack(pg_client, m, message).status_code == 403
    async def expire(db):
        session = await db.get(AgentSession, m["sessions"]["frontend"]["session"]["id"])
        session.expires_at = utcnow() - timedelta(seconds=1)
    asyncio.run(with_db(expire))
    assert poll(pg_client, m).status_code == 401


def test_flux_pause_claim_perime_et_auteur_non_titulaire(pg_client, messaging):
    m = messaging
    bad = payload(m, "frontend", task_id=m["tasks"]["backend"])
    assert pg_client.post(m["base"] + "/agent-messages", json=bad, headers=headers(m, "frontend")).status_code == 409
    existing = emit(pg_client, m)
    async def pause(db):
        (await db.get(FlowRun, m["run"])).status = "paused"
    asyncio.run(with_db(pause))
    assert pg_client.post(m["base"] + "/agent-messages", json=payload(m), headers=headers(m)).status_code == 409
    # Les échanges existants restent lisibles/acquittables ; l'ack ne démarre rien.
    assert poll(pg_client, m).status_code == 200
    assert ack(pg_client, m, existing).status_code == 200
    async def stale(db):
        (await db.get(FlowRun, m["run"])).status = "running"
        claim = await db.scalar(select(AgentTaskClaim).where(AgentTaskClaim.task_id == m["tasks"]["backend"]))
        claim.lease_expires_at = utcnow() - timedelta(seconds=1)
    asyncio.run(with_db(stale))
    assert pg_client.post(m["base"] + "/agent-messages", json=payload(m), headers=headers(m)).status_code == 409


def test_rejeu_envoi_et_concurrence_receipts(pg_client, messaging):
    m = messaging
    values = payload(m)
    first = pg_client.post(m["base"] + "/agent-messages", json=values, headers=headers(m)).json()
    repeated = pg_client.post(m["base"] + "/agent-messages", json=values, headers=headers(m)).json()
    assert repeated["created"] is False and repeated["message"] == first["message"]
    assert pg_client.post(m["base"] + "/agent-messages", json={**values, "summary": "Autre"}, headers=headers(m)).status_code == 409
    second_frontend = m["connect"]("frontend")
    original = m["sessions"]["frontend"]

    async def receive_then_ack(session):
        factory = database_factory()
        try:
            async with factory() as db:
                active = await authenticated_session(db, token=session["token"], session_id=session["session"]["id"], project_id=m["project"])
                await poll_messages(db, active, 50)
                await db.commit()
            async with factory() as db:
                active = await authenticated_session(db, token=session["token"], session_id=session["session"]["id"], project_id=m["project"])
                await acknowledge(db, active, first["message"]["id"])
                await db.commit()
        finally:
            await factory.kw["bind"].dispose()
    with ThreadPoolExecutor(max_workers=2) as executor:
        list(executor.map(lambda session: asyncio.run(receive_then_ack(session)), (original, second_frontend)))
    async def verify(db):
        assert await db.scalar(select(func.count()).select_from(AgentMessage)) == 1
        for action in ("agent.message.send", "agent.message.deliver", "agent.message.ack"):
            assert await db.scalar(select(func.count()).select_from(AuditEvent).where(AuditEvent.action == action,
                AuditEvent.target_id == first["message"]["id"])) == 1
    asyncio.run(with_db(verify))


def test_contraintes_schema_et_transaction_annulee(pg_client, messaging):
    m = messaging
    async def rollback(db):
        session = await authenticated_session(db, token=m["sessions"]["backend"]["token"],
            session_id=m["sessions"]["backend"]["session"]["id"], project_id=m["project"])
        values = payload(m)
        values.pop("session_id")
        # Valeurs normalisées comme dans le schéma Pydantic.
        from app.conversation_api import MessageInput
        values = MessageInput(**{**values, "session_id": session.id}).model_dump(mode="json", exclude={"session_id"})
        await send_message(db, session, values)
        await db.rollback()
        assert await db.scalar(select(func.count()).select_from(AgentMessage)) == 0
        assert await db.scalar(select(func.count()).select_from(CoordinationChannel)) == 0
    asyncio.run(with_db(rollback))
    message = emit(pg_client, m)
    async def check_constraint(db):
        with pytest.raises(IntegrityError):
            async with db.begin_nested():
                await db.execute(text("UPDATE conversation.agent_message_delivery SET status='acknowledged' WHERE message_id=:id"), {"id": message["id"]})
    asyncio.run(with_db(check_constraint))


def test_fil_artefact_sources_et_destinataires_figes(pg_client, messaging):
    m = messaging
    async def source(db):
        row = SourceDocument(project_id=m["project"], created_by=m["manager"], name="Besoin", kind="text",
            mime_type="text/plain", sha256="0" * 64, size_bytes=12, direct_text="Besoin utile")
        db.add(row)
        await db.flush()
        return row.id
    source_id = asyncio.run(with_db(source))
    reference = {"type": "source_document", "id": source_id}
    message = emit(pg_client, m, channel_kind="thread", recipient_profile_id=None,
                   attachment=reference, artifact_refs=[reference])
    assert message["artifact_refs"] == [reference]
    async def new_profile(db):
        old = await db.get(SkillProfile, m["profiles"]["qa"])
        version = SkillProfile(code="qa", version_number=2, label="QA v2", allowed_capabilities=old.allowed_capabilities)
        db.add(version)
        await db.flush()
        db.add(AgentTeamSkill(team_id=m["team"], skill_profile_id=version.id))
    asyncio.run(with_db(new_profile))
    m["sessions"]["qa"] = m["connect"]("qa")
    assert m["sessions"]["qa"]["session"]["skill_profile_id"] != m["profiles"]["qa"]
    assert poll(pg_client, m, "qa").json()["messages"] == []
    assert len(pg_client.get(m["base"] + "/agent-conversations/" + message["channel_id"]).json()["messages"][0]["receipts"]) == 6


def test_envoi_concurrent_et_message_apres_achevement(pg_client, messaging):
    m = messaging
    from app.conversation_api import MessageInput
    values = MessageInput(**payload(m)).model_dump(mode="json", exclude={"session_id"})

    async def concurrent_send():
        factory = database_factory()
        try:
            async with factory() as db:
                own = m["sessions"]["backend"]
                session = await authenticated_session(db, token=own["token"], session_id=own["session"]["id"], project_id=m["project"])
                message, created = await send_message(db, session, values)
                identifier = message.id
                await db.commit()
                return identifier, created
        finally:
            await factory.kw["bind"].dispose()
    with ThreadPoolExecutor(max_workers=2) as executor:
        results = list(executor.map(lambda _: asyncio.run(concurrent_send()), range(2)))
    assert results[0][0] == results[1][0] and sum(r[1] for r in results) == 1
    async def complete(db):
        from app.agent_progress import report_progress
        own = m["sessions"]["backend"]
        session = await authenticated_session(db, token=own["token"], session_id=own["session"]["id"], project_id=m["project"])
        claim = await db.scalar(select(AgentTaskClaim).where(AgentTaskClaim.task_id == m["tasks"]["backend"]))
        await report_progress(db, session=session, task_id=m["tasks"]["backend"], claim_id=claim.id, progress_percent=100)
    asyncio.run(with_db(complete))
    assert emit(pg_client, m)["content_status"] == "Brouillon IA"


def test_migration_peuplee_aller_retour(pg_client, messaging):
    m = messaging
    message = emit(pg_client, m)
    command = [sys.executable, "-m", "alembic"]
    try:
        subprocess.run([*command, "downgrade", "0017_llm_run_traceability"], cwd=ROOT, check=True)
        async def preserved(db):
            assert await db.scalar(text("SELECT count(*) FROM agent_task WHERE plan_version_id=:id"), {"id": m["plan"]}) == 3
            assert await db.scalar(text("SELECT count(*) FROM agent_session")) == 3
            assert await db.scalar(text("SELECT count(*) FROM agent_task_run")) == 3
            assert await db.scalar(text("SELECT count(*) FROM pg_tables WHERE schemaname='conversation'")) == 0
            caps = (await db.get(SkillProfile, m["profiles"]["backend"])).allowed_capabilities
            assert "message.read" not in caps and "message.send" in caps
        asyncio.run(with_db(preserved))
    finally:
        subprocess.run([*command, "upgrade", "head"], cwd=ROOT, check=True)
    async def migrated(db):
        assert await db.scalar(select(func.count()).select_from(AgentMessage)) == 0
        assert "message.read" in (await db.get(SkillProfile, m["profiles"]["backend"])).allowed_capabilities
    asyncio.run(with_db(migrated))
    assert ack(pg_client, m, message).status_code == 404


def test_bail_expire_pendant_attente_du_verrou(pg_client, messaging):
    m = messaging
    from app.conversation_api import MessageInput
    values = MessageInput(**payload(m)).model_dump(mode="json", exclude={"session_id"})
    async def blocked_send():
        factory = database_factory()
        ready = asyncio.Event()
        try:
            async with factory() as db:
                task = await db.get(AgentTask, m["tasks"]["backend"])
                claim = await db.scalar(select(AgentTaskClaim).where(AgentTaskClaim.task_id == task.id))
                task.lease_expires_at = claim.lease_expires_at = utcnow() + timedelta(seconds=1)
                await db.commit()
            async def hold_plan():
                async with factory() as db:
                    await db.scalar(select(AgentPlanVersion).where(AgentPlanVersion.id == m["plan"]).with_for_update())
                    ready.set()
                    await asyncio.sleep(2)
                    await db.commit()
            holder = asyncio.create_task(hold_plan())
            await ready.wait()
            async with factory() as db:
                own = m["sessions"]["backend"]
                session = await authenticated_session(db, token=own["token"], session_id=own["session"]["id"], project_id=m["project"])
                lease = await db.scalar(select(AgentTaskClaim.lease_expires_at).where(
                    AgentTaskClaim.task_id == m["tasks"]["backend"]))
                assert lease > utcnow()
                with pytest.raises(ValueError):
                    await send_message(db, session, values)
                await db.rollback()
                assert await db.scalar(select(func.count()).select_from(AgentMessage)) == 0
            await holder
        finally:
            await factory.kw["bind"].dispose()
    asyncio.run(blocked_send())
