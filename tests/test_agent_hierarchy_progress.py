"""AGENT-06/07 : forêt bornée, claims de feuilles et progression sans validation."""

import asyncio
from datetime import timedelta
from uuid import uuid4

import pytest
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from app.agent_models import AgentPlanVersion, AgentSession, AgentTask, AgentTaskClaim, FlowRun
from app.agent_progress import hierarchy_depths, report_progress
from app.identity_models import AuditEvent, RolePermission, utcnow
from app.identity_service import database_factory

from .conftest import create_user, with_db
from .test_agent_sessions import project_with_members, team_id
from .test_identity_acl import login


def task(key, parent=None, **fields):
    return {"task_key": key, "title": f"Tâche {key}", "skill_code": "backend",
            "parent_task_key": parent, **fields}


@pytest.mark.parametrize("tasks", [
    [task("a", "a")], [task("a", "b"), task("b", "a")],
    [task("a", "absent")], [task("a"), task("a")],
    [task("a"), task("b", "a"), task("c", "b"), task("d", "c")],
    [], [task(str(i)) for i in range(21)],
])
def test_foret_invalide_refusee(tasks):
    with pytest.raises(ValueError):
        hierarchy_depths(tasks)


def test_trois_niveaux_et_ordre_libre():
    assert hierarchy_depths([task("c", "b"), task("a"), task("b", "a"), task("d")]) == {
        "a": 1, "b": 2, "c": 3, "d": 1}


def prepare(client):
    manager_id, manager_email = create_user()
    architect_id, architect_email = create_user()
    reader_id, reader_email = create_user()
    project = project_with_members(manager_id, architect_id, reader_id)
    base = f"/api/projects/{project}"
    team = team_id(project)
    csrf = login(client, architect_email)
    flow = client.post(f"{base}/agent-flows", json={"name": "Hiérarchie", "team_id": team},
                       headers={"X-CSRF-Token": csrf})
    assert flow.status_code == 201, flow.text
    version = flow.json()["version_id"]
    for action in ("review", "validate"):
        assert client.post(f"{base}/agent-flows/{version}/{action}",
                           headers={"X-CSRF-Token": csrf}).status_code == 200
    client.cookies.clear()
    csrf = login(client, manager_email)
    run = client.post(f"{base}/agent-flows/{version}/runs", headers={"X-CSRF-Token": csrf}).json()["run_id"]
    return {"project": project, "base": base, "team": team, "run": run,
            "csrf": csrf, "manager_id": manager_id, "manager_email": manager_email,
            "reader_email": reader_email}


def propose(client, setup, tasks):
    return client.post(f"{setup['base']}/agent-flows/runs/{setup['run']}/plans",
                       json={"tasks": tasks}, headers={"X-CSRF-Token": setup["csrf"]})


def approve(client, setup, plan):
    for action in ("review", "approve"):
        assert client.post(f"{setup['base']}/agent-flows/plans/{plan}/{action}",
                           headers={"X-CSRF-Token": setup["csrf"]}).status_code == 200


def session(client, setup, capabilities=None):
    result = client.post(f"{setup['base']}/agent-sessions", json={
        "team_id": setup["team"], "client_code": "codex", "skill_code": "backend",
        "capabilities": capabilities or ["session.heartbeat", "session.disconnect", "task.claim", "task.release"]},
        headers={"X-CSRF-Token": setup["csrf"]})
    assert result.status_code == 201, result.text
    data = result.json()
    return data["session"]["id"], {"Authorization": f"Bearer {data['token']}"}


def listing(client, setup, plan):
    result = client.get(f"{setup['base']}/agent-tasks", params={"plan_id": plan})
    assert result.status_code == 200, result.text
    return {item["task_key"]: item for item in result.json()["tasks"]}


def claim(client, setup, agent, task_id):
    sid, headers = agent
    result = client.post(f"{setup['base']}/agent-tasks/{task_id}/claim",
                         params={"session_id": sid}, headers=headers)
    assert result.status_code == 200, result.text
    return result.json()["claim_id"]


def progress(client, setup, agent, task_id, claim_id, percent, **fields):
    sid, headers = agent
    return client.post(f"{setup['base']}/agent-tasks/{task_id}/progress",
        params={"session_id": sid}, headers=headers,
        json={"claim_id": claim_id, "progress_percent": percent, **fields})


def test_hierarchie_refus_atomiques_et_consultation_rbac(pg_client):
    s = prepare(pg_client)
    invalid = [
        [task("a", "a")], [task("a", "b"), task("b", "a")],
        [task("a", str(uuid4()))],
        [task("a"), task("b", "a"), task("c", "b"), task("d", "c")],
        [task("a"), task("a")],
    ]
    for tasks in invalid:
        assert propose(pg_client, s, tasks).status_code == 409
    for fields in ({"priority": "P4"}, {"due_at": "2026-10-10T12:00:00"}):
        assert propose(pg_client, s, [task("a", **fields)]).status_code == 422
    async def no_plan(db):
        return (await db.scalars(select(AgentPlanVersion).where(
            AgentPlanVersion.validated_flow_run_id == s["run"]))).all()
    assert asyncio.run(with_db(no_plan)) == []
    result = propose(pg_client, s, [task("c", "b", priority="P0", due_at="2026-10-10T12:00:00+02:00"),
                                   task("b", "a"), task("a")])
    assert result.status_code == 201, result.text
    plan = result.json()["plan_id"]
    tasks = listing(pg_client, s, plan)
    assert [tasks[k]["depth"] for k in ("a", "b", "c")] == [1, 2, 3]
    assert tasks["c"]["parent_task_id"] == tasks["b"]["id"]
    assert tasks["c"]["priority"] == "P0" and tasks["c"]["due_at"]
    assert tasks["a"]["priority"] == "P2"
    assert tasks["c"]["skill_profile_id"]
    pg_client.cookies.clear()
    reader_csrf = login(pg_client, s["reader_email"])
    assert len(listing(pg_client, s, plan)) == 3
    assert pg_client.post(f"{s['base']}/agent-flows/runs/{s['run']}/plans",
        json={"tasks": [task("x")]}, headers={"X-CSRF-Token": reader_csrf}).status_code == 403
    _, outsider_email = create_user()
    pg_client.cookies.clear()
    login(pg_client, outsider_email)
    assert pg_client.get(f"{s['base']}/agent-tasks?plan_id={plan}").status_code == 404
    pg_client.cookies.clear()
    s["csrf"] = login(pg_client, s["manager_email"])
    assert pg_client.get(f"{s['base']}/agent-tasks?plan_id={uuid4()}").status_code == 404
    assert pg_client.post(f"{s['base']}/agent-flows/runs/{s['run']}/plans",
        json={"tasks": [task("x")]}).status_code == 403


def test_progression_agregation_et_achevement_sans_validation(pg_client):
    s = prepare(pg_client)
    plan = propose(pg_client, s, [task("a"), task("b", "a"), task("c", "b"),
                                  task("d", "b"), task("e", "a")]).json()["plan_id"]
    tasks = listing(pg_client, s, plan)
    agent = session(pg_client, s)
    url = f"{s['base']}/agent-tasks/ready?session_id={agent[0]}"
    assert pg_client.get(url, headers=agent[1]).json()["tasks"] == []
    assert progress(pg_client, s, agent, tasks["c"]["id"], str(uuid4()), 50).status_code == 409
    approve(pg_client, s, plan)
    assert {item["task_key"] for item in pg_client.get(url, headers=agent[1]).json()["tasks"]} == {"c", "d", "e"}
    assert pg_client.post(f"{s['base']}/agent-tasks/{tasks['a']['id']}/claim",
        params={"session_id": agent[0]}, headers=agent[1]).status_code == 409
    cid = claim(pg_client, s, agent, tasks["c"]["id"])
    assert listing(pg_client, s, plan)["a"]["status"] == "running"
    for invalid in (-1, 101, 3.5, True, "50"):
        assert progress(pg_client, s, agent, tasks["c"]["id"], cid, invalid).status_code == 422
    assert progress(pg_client, s, agent, tasks["c"]["id"], cid, 50, status="Validé").status_code == 422
    assert progress(pg_client, s, agent, tasks["c"]["id"], cid, 50).status_code == 200
    observed = listing(pg_client, s, plan)
    assert observed["b"]["progress_percent"] == 25 and observed["a"]["progress_percent"] == 12
    assert progress(pg_client, s, agent, tasks["c"]["id"], cid, 49).status_code == 409
    async def count(db):
        return len((await db.scalars(select(AuditEvent).where(
            AuditEvent.project_id == s["project"], AuditEvent.action == "agent.task.progress",
            AuditEvent.outcome == "success"))).all())
    before = asyncio.run(with_db(count))
    assert progress(pg_client, s, agent, tasks["c"]["id"], cid, 50).status_code == 200
    assert asyncio.run(with_db(count)) == before
    assert progress(pg_client, s, agent, tasks["c"]["id"], cid, 100).status_code == 200
    before = asyncio.run(with_db(count))
    assert progress(pg_client, s, agent, tasks["c"]["id"], cid, 100).status_code == 200
    assert asyncio.run(with_db(count)) == before
    for key in ("d", "e"):
        claim_id = claim(pg_client, s, agent, tasks[key]["id"])
        assert progress(pg_client, s, agent, tasks[key]["id"], claim_id, 100).status_code == 200
    assert all(t["status"] == "completed" and t["progress_percent"] == 100
               for t in listing(pg_client, s, plan).values())
    async def verify(db):
        claims = (await db.scalars(select(AgentTaskClaim).where(
            AgentTaskClaim.task_id.in_([t["id"] for t in tasks.values()])))).all()
        assert all(c.released_at and c.release_reason == "completed" for c in claims)
        assert (await db.get(FlowRun, s["run"])).status == "running"
        events = (await db.scalars(select(AuditEvent).where(AuditEvent.project_id == s["project"]))).all()
        assert any(e.action == "agent.task.aggregate" for e in events)
        assert not any(e.action.startswith("deliverable.") for e in events)
    asyncio.run(with_db(verify))


def test_claim_perime_reprise_et_refus_de_progression(pg_client):
    s = prepare(pg_client)
    plan = propose(pg_client, s, [task("a"), task("b", "a")]).json()["plan_id"]
    approve(pg_client, s, plan)
    tasks = listing(pg_client, s, plan)
    tid = tasks["b"]["id"]
    first, second = session(pg_client, s), session(pg_client, s)
    cid = claim(pg_client, s, first, tid)
    assert progress(pg_client, s, first, tid, cid, 40).status_code == 200
    assert progress(pg_client, s, second, tid, cid, 50).status_code == 409
    assert progress(pg_client, s, first, tasks["a"]["id"], cid, 100).status_code == 409
    async def expire(db):
        (await db.get(AgentTask, tid)).lease_expires_at = utcnow() - timedelta(seconds=1)
    asyncio.run(with_db(expire))
    assert progress(pg_client, s, first, tid, cid, 60).status_code == 409
    assert pg_client.post(f"{s['base']}/agent-tasks/{tid}/claim",
        params={"session_id": second[0]}, headers=second[1]).status_code == 409
    async def pass_retry_delay(db):
        (await db.get(AgentTask, tid)).retry_not_before = utcnow() - timedelta(seconds=1)
    asyncio.run(with_db(pass_retry_delay))
    replacement = claim(pg_client, s, second, tid)
    assert replacement != cid
    assert listing(pg_client, s, plan)["a"]["progress_percent"] == 40
    assert progress(pg_client, s, first, tid, cid, 100).status_code == 409
    assert progress(pg_client, s, second, tid, cid, 100).status_code == 409
    assert progress(pg_client, s, second, tid, replacement, 60).status_code == 200
    assert pg_client.post(f"{s['base']}/agent-sessions/{second[0]}/disconnect", headers=second[1]).status_code == 200
    assert progress(pg_client, s, second, tid, replacement, 100).status_code == 401
    assert listing(pg_client, s, plan)["b"]["progress_percent"] == 60
    cid3 = claim(pg_client, s, first, tid)
    assert progress(pg_client, s, first, tid, cid3, 60).status_code == 200
    limited = session(pg_client, s, ["session.heartbeat"])
    assert progress(pg_client, s, limited, tid, cid3, 100).status_code == 403
    async def revoke_permission(db):
        await db.delete(await db.get(RolePermission, ("project_manager", "agent.task.claim")))
    asyncio.run(with_db(revoke_permission))
    assert progress(pg_client, s, first, tid, cid3, 100).status_code == 403


def test_progression_concurrente_de_freres(pg_client):
    s = prepare(pg_client)
    plan = propose(pg_client, s, [task("a"), task("b", "a"), task("c", "a")]).json()["plan_id"]
    approve(pg_client, s, plan)
    tasks = listing(pg_client, s, plan)
    agents = [session(pg_client, s), session(pg_client, s)]
    claims = [claim(pg_client, s, a, tasks[k]["id"]) for a, k in zip(agents, ("b", "c"))]
    async def race():
        factory = database_factory()
        try:
            async def attempt(agent, key, cid):
                async with factory() as db:
                    technical = await db.get(AgentSession, agent[0])
                    await report_progress(db, session=technical, task_id=tasks[key]["id"],
                                          claim_id=cid, progress_percent=100)
                    await db.commit()
            await asyncio.wait_for(asyncio.gather(*(attempt(a, k, cid)
                for a, k, cid in zip(agents, ("b", "c"), claims))), timeout=10)
        finally:
            await factory.kw["bind"].dispose()
    asyncio.run(race())
    parent = listing(pg_client, s, plan)["a"]
    assert parent["status"] == "completed" and parent["progress_percent"] == 100


def test_parent_interplan_refuse_en_base_et_run_inactif(pg_client):
    s = prepare(pg_client)
    plan = propose(pg_client, s, [task("a"), task("b", "a")]).json()["plan_id"]
    approve(pg_client, s, plan)
    tasks = listing(pg_client, s, plan)
    agent = session(pg_client, s)
    cid = claim(pg_client, s, agent, tasks["b"]["id"])
    async def check(db):
        another = AgentPlanVersion(project_id=s["project"], team_id=s["team"], status="proposed")
        db.add(another)
        await db.flush()
        leaf = await db.get(AgentTask, tasks["b"]["id"])
        with pytest.raises(IntegrityError):
            async with db.begin_nested():
                db.add(AgentTask(project_id=s["project"], team_id=s["team"], plan_version_id=another.id,
                    skill_profile_id=leaf.skill_profile_id, task_key="bad", title="Autre plan",
                    parent_task_id=tasks["a"]["id"], depth=2))
                await db.flush()
        (await db.get(FlowRun, s["run"])).status = "cancelled"
    asyncio.run(with_db(check))
    assert progress(pg_client, s, agent, tasks["b"]["id"], cid, 100).status_code == 409
    assert listing(pg_client, s, plan)["b"]["progress_percent"] == 0


def test_isolation_et_parent_non_termine_par_annulation(pg_client):
    from app.agent_progress import aggregate_plan, lock_task_plan
    from .test_agent_team_routing import approved_run

    s = prepare(pg_client)
    plan = propose(pg_client, s, [task("a"), task("b", "a"), task("c", "a")]).json()["plan_id"]
    approve(pg_client, s, plan)
    tasks = listing(pg_client, s, plan)
    agent = session(pg_client, s)
    cid = claim(pg_client, s, agent, tasks["b"]["id"])
    assert progress(pg_client, s, agent, tasks["b"]["id"], cid, 100).status_code == 200
    member_id, _ = create_user()
    reader_id, _ = create_user()
    foreign = project_with_members(s["manager_id"], member_id, reader_id)
    async def foreign_plan_and_cancel(db):
        other_team = team_id_value
        run = await approved_run(db, foreign, other_team, member_id, s["manager_id"])
        other_plan = AgentPlanVersion(project_id=foreign, team_id=other_team,
            validated_flow_run_id=run, status="approved", approved_by=s["manager_id"], approved_at=utcnow())
        db.add(other_plan)
        await db.flush()
        await lock_task_plan(db, tasks["c"]["id"], s["project"])
        (await db.get(AgentTask, tasks["c"]["id"])).status = "cancelled"
        await db.flush()
        await aggregate_plan(db, plan, session_id=agent[0])
        return other_plan.id
    team_id_value = team_id(foreign)
    other_plan = asyncio.run(with_db(foreign_plan_and_cancel))
    assert pg_client.get(f"{s['base']}/agent-tasks?plan_id={other_plan}").status_code == 404
    assert pg_client.post(f"/api/projects/{foreign}/agent-tasks/{tasks['b']['id']}/progress",
        params={"session_id": agent[0]}, headers=agent[1],
        json={"claim_id": cid, "progress_percent": 100}).status_code == 401
    parent = listing(pg_client, s, plan)["a"]
    assert parent["status"] == "cancelled" and parent["progress_percent"] == 50


def test_migration_retour_et_reprise_des_taches_anciennes(pg_client):
    import os
    import subprocess
    import sys
    from sqlalchemy import text
    from .conftest import ROOT

    s = prepare(pg_client)
    plan = propose(pg_client, s, [task("a"), task("b")]).json()["plan_id"]
    tasks = listing(pg_client, s, plan)
    async def finish(db):
        leaf = await db.get(AgentTask, tasks["b"]["id"])
        leaf.status, leaf.progress_percent = "completed", 100
    asyncio.run(with_db(finish))
    # Cette fixture n'accepte que localhost/mimic_test, jamais la base métier.
    assert os.environ["MIMIC_DATABASE_URL"].endswith("/mimic_test")
    command = [sys.executable, "-m", "alembic"]
    try:
        subprocess.run([*command, "downgrade", "0011_agent_flow_gate"], cwd=ROOT, check=True)
        async def legacy(db):
            rows = (await db.execute(text("SELECT task_key, status FROM agent_task WHERE plan_version_id = :id"),
                                     {"id": plan})).all()
            assert dict(rows) == {"a": "ready", "b": "completed"}
        asyncio.run(with_db(legacy))
    finally:
        subprocess.run([*command, "upgrade", "head"], cwd=ROOT, check=True)
    migrated = listing(pg_client, s, plan)
    assert migrated["b"]["progress_percent"] == 100 and migrated["a"]["progress_percent"] == 0
    assert all(t["depth"] == 1 and t["parent_task_id"] is None and t["priority"] == "P2"
               and t["due_at"] is None for t in migrated.values())
