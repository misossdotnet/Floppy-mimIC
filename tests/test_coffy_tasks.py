"""COFFY-02 : versions immuables, portée projet, RBAC et audit."""

import asyncio
from uuid import uuid4

from sqlalchemy import select

from app.coffy_models import ScheduledLLMTask, ScheduledLLMTaskVersion
from app.identity_models import AuditEvent, Project, ProjectMember
from app.llm_models import LLMConnectionCheck, LLMProfileVersion
from .conftest import create_user, with_db
from .test_identity_acl import login


def setup(client):
    manager_id, manager_email = create_user()
    csrf = login(client, manager_email)
    response = client.post("/projects", data={"name": "Projet CoFFY", "description": "", "csrf_token": csrf})
    assert response.status_code == 303
    project_id = response.headers["location"].rsplit("/", 1)[-1]

    async def profile(db):
        row = LLMProfileVersion(profile_id=str(uuid4()), version=1, name="LLM test",
            provider="openai", model_name="test", base_url=None,
            credential_env=None, temperature=0.4, max_output_tokens=1000,
            timeout_seconds=60, max_retries=0, enabled=True, is_current=True, created_by=manager_id)
        db.add(row)
        await db.flush()
        db.add(LLMConnectionCheck(profile_version_id=row.id, actor_id=manager_id, outcome="success"))
        return row.id

    profile_id = asyncio.run(with_db(profile))
    return {"manager_id": manager_id, "email": manager_email, "csrf": csrf,
            "project_id": project_id, "profile_id": profile_id,
            "base": f"/api/projects/{project_id}/scheduled-tasks",
            "subjects": f"/api/projects/{project_id}/scheduled-subjects"}


def fields(state, **changes):
    return {"title": "Veille initiale", "short_description": "Résumé", "long_description": "Détail",
            "system_prompt": "Tu résumes.", "user_prompt": "Analyse le projet.",
            "result_domain": "pilotage", "llm_profile_version_id": state["profile_id"],
            "parameters": {"max_output_tokens": 500}, "strict_json": False,
            "schedule_mode": "manual", "subject_ids": [], **changes}


def test_versions_sujets_historique_et_activation(pg_client):
    state = setup(pg_client)
    headers = {"X-CSRF-Token": state["csrf"]}
    subject = pg_client.post(state["subjects"], json={"title": "Qualité", "description": "Contrôles"}, headers=headers)
    assert subject.status_code == 201, subject.text
    subject_id = subject.json()["id"]
    created = pg_client.post(state["base"], json=fields(state, subject_ids=[subject_id]), headers=headers)
    assert created.status_code == 201, created.text
    task_id = created.json()["id"]
    assert "Veille initiale" in pg_client.get(f'/projects/{state["project_id"]}/scheduled-tasks').text
    assert "Historique de configuration" in pg_client.get(f'/projects/{state["project_id"]}/scheduled-tasks/{task_id}').text
    assert created.json()["status"] == "draft"
    activated = pg_client.post(f'{state["base"]}/{task_id}/activate', json={"expected_revision": 1}, headers=headers)
    assert activated.status_code == 200 and activated.json()["status"] == "active"
    assert pg_client.get(f'{state["base"]}/{task_id}').json()["revision"] == 2
    updated = pg_client.put(f'{state["base"]}/{task_id}', json=fields(state, title="Veille révisée",
        user_prompt="Nouvelle demande.", subject_ids=[subject_id], expected_revision=2), headers=headers)
    assert updated.status_code == 200, updated.text
    assert updated.json()["current_version"] == 2
    assert pg_client.put(f'{state["base"]}/{task_id}', json=fields(state, expected_revision=1), headers=headers).status_code == 409
    history = pg_client.get(f'{state["base"]}/{task_id}/history').json()["versions"]
    assert [row["title"] for row in history] == ["Veille initiale", "Veille révisée"]
    assert history[0]["user_prompt"] == "Analyse le projet."
    assert pg_client.post(f'{state["base"]}/{task_id}/archive', json={"expected_revision": 3}, headers=headers).json()["status"] == "archived"
    restored = pg_client.put(f'{state["base"]}/{task_id}', json=fields(state, title="Reprise", expected_revision=4), headers=headers)
    assert restored.status_code == 200 and restored.json()["status"] == "draft"
    assert restored.json()["current_version"] == 3
    edited_subject = pg_client.put(f'{state["subjects"]}/{subject_id}', json={"title": "Qualité", "description": "Suivi", "status": "inactive", "expected_revision": 1}, headers=headers)
    assert edited_subject.status_code == 200
    assert pg_client.post(state["base"], json=fields(state, subject_ids=[subject_id]), headers=headers).status_code == 404
    assert pg_client.get(f'{state["base"]}/{task_id}/history').json()["versions"][0]["subject_ids"] == [subject_id]
    assert [row["description"] for row in pg_client.get(f'{state["subjects"]}/{subject_id}/history').json()["revisions"]] == ["Contrôles", "Suivi"]
    async def evidence(db):
        rows = (await db.scalars(select(ScheduledLLMTaskVersion).where(ScheduledLLMTaskVersion.task_id == task_id))).all()
        events = (await db.scalars(select(AuditEvent).where(AuditEvent.target_id == task_id))).all()
        assert len(rows) == 3
        assert [event.action for event in events] == ["scheduled_task.create", "scheduled_task.activate", "scheduled_task.revise", "scheduled_task.archive", "scheduled_task.revise"]
        assert all("prompt" not in str(event.details) for event in events)
    asyncio.run(with_db(evidence))


def test_refus_roles_portee_csrf_et_limites(pg_client):
    state = setup(pg_client)
    headers = {"X-CSRF-Token": state["csrf"]}
    task = pg_client.post(state["base"], json=fields(state), headers=headers)
    assert task.status_code == 201
    task_id = task.json()["id"]
    assert pg_client.post(state["base"], json=fields(state), headers={"X-CSRF-Token": "invalid"}).status_code == 403
    assert pg_client.post(state["base"], json=fields(state, parameters={"max_output_tokens": 2000}), headers=headers).status_code == 409
    assert pg_client.post(state["base"], json=fields(state, subject_ids=[str(uuid4())]), headers=headers).status_code == 404
    other = pg_client.post("/projects", data={"name": "Autre projet", "description": "", "csrf_token": state["csrf"]})
    other_id = other.headers["location"].rsplit("/", 1)[-1]
    assert pg_client.get(f"/api/projects/{other_id}/scheduled-tasks/{task_id}").status_code == 404
    foreign = pg_client.post(f"/api/projects/{other_id}/scheduled-subjects",
                             json={"title": "Sujet étranger"}, headers=headers)
    assert foreign.status_code == 201
    assert pg_client.post(state["base"], json=fields(state, subject_ids=[foreign.json()["id"]]), headers=headers).status_code == 404
    reader_id, reader_email = create_user()
    async def membership(db):
        db.add(ProjectMember(project_id=state["project_id"], user_id=reader_id, role_code="reader", is_active=True))
    asyncio.run(with_db(membership))
    reader_csrf = login(pg_client, reader_email)
    assert pg_client.get(state["base"]).status_code == 200
    assert pg_client.get(f'{state["base"]}/{task_id}/history').status_code == 200
    assert pg_client.post(state["base"], json=fields(state), headers={"X-CSRF-Token": reader_csrf}).status_code == 403
    assert pg_client.post(state["subjects"], json={"title": "Refus"}, headers={"X-CSRF-Token": reader_csrf}).status_code == 403
    assert pg_client.post(f'{state["base"]}/{task_id}/run-now',
        json={"expected_revision": 1, "request_key": str(uuid4())},
        headers={"X-CSRF-Token": reader_csrf}).status_code == 403
    assert pg_client.get(f"/api/projects/{other_id}/scheduled-tasks").status_code == 404
    async def archive_project(db):
        project = await db.get(Project, state["project_id"])
        project.status = "archived"
    asyncio.run(with_db(archive_project))
    assert pg_client.get(state["base"]).status_code == 200
    assert pg_client.post(state["base"], json=fields(state), headers={"X-CSRF-Token": login(pg_client, state["email"])}).status_code == 409


def test_pause_reprise_et_retrait_logique_de_l_etat_actif_herite(pg_client):
    state = setup(pg_client)
    headers = {"X-CSRF-Token": state["csrf"]}
    task_id = pg_client.post(state["base"], json=fields(state), headers=headers).json()["id"]
    assert pg_client.post(f'{state["base"]}/{task_id}/activate', json={"expected_revision": 1}, headers=headers).status_code == 200
    pause = pg_client.post(f'{state["base"]}/{task_id}/pause', json={"expected_revision": 2}, headers=headers)
    assert pause.status_code == 200 and pause.json()["status"] == "paused"
    resume = pg_client.post(f'{state["base"]}/{task_id}/resume', json={"expected_revision": 3}, headers=headers)
    assert resume.status_code == 200 and resume.json()["status"] == "active"
    assert pg_client.post(f'{state["base"]}/{task_id}/delete', json={"expected_revision": 4, "confirm": True}, headers=headers).status_code == 409
    assert pg_client.post(f'{state["base"]}/{task_id}/archive', json={"expected_revision": 4}, headers=headers).status_code == 200
    assert pg_client.post(f'{state["base"]}/{task_id}/delete', json={"expected_revision": 5}, headers=headers).status_code == 409
    assert pg_client.post(f'{state["base"]}/{task_id}/delete', json={"expected_revision": 5, "confirm": True}, headers=headers).status_code == 200
    assert pg_client.get(f'{state["base"]}/{task_id}').status_code == 404
    assert len(pg_client.get(f'{state["base"]}/{task_id}/history').json()["versions"]) == 1
