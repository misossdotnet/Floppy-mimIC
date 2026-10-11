"""AGENT-26 : parcours transverses sur le plan de contrôle réel.

Les tests d'effets Git, incidents et documentaires restent dans leurs suites métier.
"""

import asyncio

import pytest
from sqlalchemy import select

from app.identity_models import AuditEvent
from .conftest import with_db
from .test_agent_autonomy import decide, direct, history
from .test_agent_hierarchy_progress import approve, listing, prepare, progress, propose, session, task


# Chaque étape est un contrat observable. La dernière étape sensible ne représente
# que la préparation : un effet externe nécessite encore son service propriétaire.
JOURNEYS = [
    ("feature_complexe", ("cadrage", "interfaces", "tests", "implementation", "revue"), False),
    ("refactoring", ("invariants", "caracterisation", "modification", "regression"), False),
    ("debugging_incident", ("diagnostic", "reproduction", "correction", "verification"), False),
    ("mvp_saas", ("hypotheses", "architecture", "lot_mvp", "acceptation"), False),
    ("release", ("cible", "qualite", "revue", "preparation_deploiement"), True),
    ("documentation", ("sources", "redaction", "revue_documentaire"), False),
    ("recherche_technique", ("question", "sources", "options", "decision"), False),
    ("migration_donnees", ("inventaire", "mapping", "repetition", "preparation_effet"), True),
    ("audit_configuration", ("perimetre", "collecte", "ecarts", "proposition"), False),
]


@pytest.mark.parametrize("scenario,stages,sensitive", JOURNEYS, ids=[row[0] for row in JOURNEYS])
def test_parcours_plan_dependances_et_portes_humaines(pg_client, scenario, stages, sensitive):
    setup = prepare(pg_client)
    agent = session(pg_client, setup)
    tasks = [task(stage, dependency_keys=[stages[index - 1]] if index else [],
                  **({"action_class": "sensitive"} if sensitive and index == len(stages) - 1 else {}))
             for index, stage in enumerate(stages)]
    if sensitive:
        response = pg_client.post(f"{setup['base']}/agent-flows/runs/{setup['run']}/plans",
                                  json={"tasks": tasks, "autonomy_mode": "sensitive"},
                                  headers={"X-CSRF-Token": setup["csrf"]})
    else:
        response = propose(pg_client, setup, tasks)
    assert response.status_code == 201, response.text
    plan_id = response.json()["plan_id"]
    views = listing(pg_client, setup, plan_id)
    assert set(views) == set(stages)
    assert all(view["attempt_count"] == 0 for view in views.values())
    ready_url = f"{setup['base']}/agent-tasks/ready"
    params = {"session_id": agent[0]}
    assert pg_client.get(ready_url, params=params, headers=agent[1]).json()["tasks"] == []
    assert direct(pg_client, setup, agent, views[stages[0]]["id"]).status_code == 409

    approve(pg_client, setup, plan_id)
    for index, stage in enumerate(stages):
        current = views[stage]["id"]
        if index + 1 < len(stages):
            assert direct(pg_client, setup, agent, views[stages[index + 1]]["id"]).status_code == 409
        ready = pg_client.get(ready_url, params=params, headers=agent[1])
        assert ready.status_code == 200, ready.text
        if sensitive and index == len(stages) - 1:
            assert ready.json()["tasks"] == []
            assert direct(pg_client, setup, agent, current).status_code == 409
            pending = history(pg_client, setup, current)
            assert len(pending) == 1 and pending[0]["status"] == "pending"
            assert listing(pg_client, setup, plan_id)[stage]["attempt_count"] == 0
            assert decide(pg_client, setup, current, pending[0]["id"]).status_code == 200
        else:
            assert [row["task_key"] for row in ready.json()["tasks"]] == [stage]
        claimed = direct(pg_client, setup, agent, current)
        assert claimed.status_code == 200, (scenario, claimed.text)
        claim_id = claimed.json()["claim_id"]
        assert progress(pg_client, setup, agent, current, claim_id, 100).status_code == 200

    result = listing(pg_client, setup, plan_id)
    assert all(result[stage]["status"] == "completed" for stage in stages)
    assert all(result[stage]["attempt_count"] == 1 for stage in stages)
    if sensitive:
        final = result[stages[-1]]["id"]
        assert history(pg_client, setup, final)[0]["status"] == "consumed"

    async def audit(db):
        events = (await db.scalars(select(AuditEvent).where(AuditEvent.project_id == setup["project"]))).all()
        actions = [event.action for event in events]
        assert "agent.plan.approve" in actions
        assert not any(action.startswith(("deliverable.publish", "git.operation.complete")) for action in actions)
        if sensitive:
            assert "agent.approval.approve" in actions
            assert "agent.approval.consume" in actions

    asyncio.run(with_db(audit))
