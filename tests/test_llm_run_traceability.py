"""LLM-08/09 : profils figés, appels distincts, tarifs datés et worker agentique gouverné."""
import asyncio
import json
import subprocess
import sys
from decimal import Decimal
from types import SimpleNamespace
from uuid import uuid4

import pytest
from sqlalchemy import select

from app.agent_models import AgentTaskRun
from app.business_models import AnalysisRun, ContextItem, DeliverableVersion, GenerationRun, SourceDocument
from app.generation_service import start_generation, retry_generation
from app.identity_models import AuditEvent, ProjectMember, User
from app.llm_models import LLMCallAttempt, LLMConnectionCheck, LLMProfileVersion
from app.llm_service import LLMInvocationError, LLMResult, invocation_ready, profile_ready, settings
from app.llm_trace import analysis_inputs, estimate, pricing_for, redact
from .conftest import ROOT, with_db
from .test_agent_coordinator import plan
from .test_agent_hierarchy_progress import prepare, task, session, claim, listing, progress
from .test_identity_acl import login

PRICING = [{"provider": "ollama", "model_name": "test", "date": "2026-01-01",
            "source": "Barème fictif de test", "currency": "EUR",
            "input_per_million": "2.5", "output_per_million": "10"}]


def test_tarifs_et_secrets(monkeypatch):
    profile = SimpleNamespace(provider="ollama", model_name="test")
    monkeypatch.setenv("MIMIC_LLM_PRICING_JSON", json.dumps(PRICING))
    pricing = pricing_for(profile)
    assert estimate(pricing, 100, 20) == Decimal("0.000450")
    assert estimate(pricing, 0, 0) == 0
    for tokens in (None, -1, True, "10"):
        assert estimate(pricing, tokens, 20) is None
    for value in ("invalid", "{}", json.dumps([{**PRICING[0], "date": "2999-01-01"}]),
                  json.dumps([{**PRICING[0], "input_per_million": "NaN"}])):
        monkeypatch.setenv("MIMIC_LLM_PRICING_JSON", value)
        assert pricing_for(profile) is None
    monkeypatch.setenv("MIMIC_LLM_KEY_TEST", "credential-confidentiel")
    masked = redact({"text": "credential-confidentiel Bearer abcdefghijklmnop12345 sk-abcdefghijklmnopqrstuvwxyz"})
    assert "credential-confidentiel" not in str(masked) and "abcdefghijklmnop" not in str(masked)


def test_profil_historique_et_destination_revoquee(monkeypatch):
    monkeypatch.setenv("MIMIC_LLM_ALLOWED_BASE_URLS", "http://127.0.0.1:11434")
    profile = SimpleNamespace(is_current=False, enabled=True, credential_env=None,
                              provider="ollama", base_url="http://127.0.0.1:11434")
    assert invocation_ready(profile) and not profile_ready(profile)
    monkeypatch.setenv("MIMIC_LLM_ALLOWED_BASE_URLS", "")
    assert not invocation_ready(profile)


def setup_runtime(client, monkeypatch, *, retries=0, autonomy_mode="bounded", approved=True):
    monkeypatch.setenv("MIMIC_REDIS_URL", "redis://127.0.0.1:56379/15")
    monkeypatch.setenv("MIMIC_LLM_ALLOWED_BASE_URLS", "http://127.0.0.1:11434")
    monkeypatch.setenv("MIMIC_LLM_PRICING_JSON", json.dumps(PRICING))
    from app import worker
    dispatched = []
    monkeypatch.setattr(worker.generate_document, "delay", lambda rid: dispatched.append(rid))
    s = prepare(client)
    tasks = [task("documentation")]
    if approved:
        pid = plan(client, s, tasks, autonomy_mode=autonomy_mode)
    else:
        pid = client.post(f"{s['base']}/agent-flows/runs/{s['run']}/plans",
                         json={"tasks": tasks}, headers={"X-CSRF-Token": s["csrf"]}).json()["plan_id"]
    tid = listing(client, s, pid)["documentation"]["id"]
    agent = session(client, s)

    async def save(db):
        p = LLMProfileVersion(profile_id=str(uuid4()), version=1, name="Test", provider="ollama",
            model_name="test", base_url="http://127.0.0.1:11434", max_output_tokens=1000,
            timeout_seconds=10, max_retries=retries, enabled=True, is_current=True, created_by=s["manager_id"])
        item = ContextItem(project_id=s["project"], type="requirement", title="Entrée autorisée",
            content="Besoin figé", status="À revoir", origin="manual", created_by=s["manager_id"])
        db.add_all([p, item]); await db.flush()
        (await settings(db)).default_version_id = p.id
        return p.id, item.id
    profile_id, item_id = asyncio.run(with_db(save))
    return s, pid, tid, agent, profile_id, item_id, dispatched


def delegate(client, s, tid, agent, cid, item_id, **values):
    return client.post(f"{s['base']}/agent-tasks/{tid}/llm", params={"session_id": agent[0]},
        headers=agent[1], json={"claim_id": cid, "document_type": "technical_doc",
                              "context_ids": [item_id], **values})


def test_delegation_profile_snapshot_tokens_cout_et_rejeu(pg_client, monkeypatch):
    from app import worker
    s, pid, tid, agent, profile_id, item_id, dispatched = setup_runtime(pg_client, monkeypatch)
    cid = claim(pg_client, s, agent, tid)
    result = delegate(pg_client, s, tid, agent, cid, item_id)
    assert result.status_code == 202, result.text
    rid = result.json()["generation_run_id"]
    assert delegate(pg_client, s, tid, agent, cid, item_id).json()["generation_run_id"] == rid
    assert delegate(pg_client, s, tid, agent, cid, item_id, style="detailed").status_code == 409
    assert len(dispatched) == 1
    assert progress(pg_client, s, agent, tid, cid, 100).status_code == 409

    async def revise(db):
        old = await db.get(LLMProfileVersion, profile_id)
        old.is_current = False
        await db.flush()
        db.add(LLMProfileVersion(profile_id=old.profile_id, version=2, name="Révision", provider="ollama",
            model_name="changed", base_url=old.base_url, max_output_tokens=2, timeout_seconds=1,
            max_retries=0, enabled=True, is_current=True, created_by=s["manager_id"]))
        (await db.get(ContextItem, item_id)).content = "Contexte changé"
    asyncio.run(with_db(revise))
    monkeypatch.setenv("MIMIC_LLM_PRICING_JSON", "[]")
    calls = []
    async def invoke(profile, messages, **kwargs):
        calls.append(profile.id)
        assert profile.id == profile_id and "Besoin figé" in messages[1]["content"]
        assert not profile.is_current and kwargs["retries"] == 0
        return LLMResult("# Proposition à relire", profile.id, profile.provider, profile.model_name, 100, 20)
    monkeypatch.setattr(worker, "invoke_profile", invoke)
    asyncio.run(worker._generate_document(rid)); asyncio.run(worker._generate_document(rid))
    assert len(calls) == 1
    trace = pg_client.get(f"{s['base']}/llm-runs/generation/{rid}")
    assert trace.status_code == 200 and trace.headers["Cache-Control"] == "no-store"
    data = trace.json()
    assert data["estimated_cost"] == "0.000450" and "2026-01-01" in data["cost_label"]
    assert data["calls"][0]["input_tokens"] == 100
    assert listing(pg_client, s, pid)["documentation"]["status"] == "completed"
    async def check(db):
        run = await db.get(GenerationRun, rid)
        version = await db.scalar(select(DeliverableVersion).where(DeliverableVersion.generation_run_id == rid))
        attempt = await db.scalar(select(AgentTaskRun).where(AgentTaskRun.generation_run_id == rid))
        assert version.status == "Brouillon IA" and attempt.llm_profile_version_id == profile_id
        events = (await db.scalars(select(AuditEvent).where(AuditEvent.project_id == s["project"]))).all()
        assert any(e.action == "llm.call.complete" for e in events)
        assert not any(e.action == "deliverable.publish" for e in events)
    asyncio.run(with_db(check))


@pytest.mark.parametrize("mode", ["pause", "cancel", "revoke", "permission", "expiry", "disabled_user"])
def test_interruption_pendant_appel_aucune_version(pg_client, monkeypatch, mode):
    from app import worker
    from app.agent_models import AgentSession
    from app.identity_models import utcnow
    from datetime import timedelta
    s, pid, tid, agent, _, item_id, _ = setup_runtime(pg_client, monkeypatch)
    cid = claim(pg_client, s, agent, tid)
    rid = delegate(pg_client, s, tid, agent, cid, item_id).json()["generation_run_id"]
    async def invoke(profile, messages, **kwargs):
        async def stop(db):
            if mode in {"pause", "cancel"}:
                from app.agent_coordinator import control_plan
                await control_plan(db, s["project"], pid, mode, s["manager_id"])
            elif mode == "revoke":
                from app.agent_service import revoke_session
                await revoke_session(db, await db.get(AgentSession, agent[0]))
            elif mode == "expiry":
                (await db.get(AgentSession, agent[0])).expires_at = utcnow() - timedelta(seconds=1)
            elif mode == "disabled_user":
                (await db.get(User, s["manager_id"])).is_active = False
            else:
                member = await db.scalar(select(ProjectMember).where(ProjectMember.project_id == s["project"],
                                                                      ProjectMember.user_id == s["manager_id"]))
                member.is_active = False
        await with_db(stop)
        return LLMResult("# Réponse", profile.id, profile.provider, profile.model_name, 100, 20)
    monkeypatch.setattr(worker, "invoke_profile", invoke)
    asyncio.run(worker._generate_document(rid))
    async def check(db):
        run = await db.get(GenerationRun, rid)
        assert run.status == "failed" and run.error_code == "execution_unavailable"
        assert run.input_tokens == 100 and run.estimated_cost == Decimal("0.000450")
        assert not await db.scalar(select(DeliverableVersion.id).where(DeliverableVersion.generation_run_id == rid))
    asyncio.run(with_db(check))


def test_retries_traces_et_reponse_invalide_conserve_usage(pg_client, monkeypatch):
    from app import worker
    s, _, tid, agent, _, item_id, _ = setup_runtime(pg_client, monkeypatch, retries=1)
    cid = claim(pg_client, s, agent, tid)
    rid = delegate(pg_client, s, tid, agent, cid, item_id).json()["generation_run_id"]
    count = 0
    async def invoke(profile, messages, **kwargs):
        nonlocal count
        count += 1
        if count == 1:
            raise LLMInvocationError("timeout")
        return LLMResult("x" * 40001, profile.id, profile.provider, profile.model_name, 100, 20)
    monkeypatch.setattr(worker, "invoke_profile", invoke)
    asyncio.run(worker._generate_document(rid))
    trace = pg_client.get(f"{s['base']}/llm-runs/generation/{rid}").json()
    assert trace["error_code"] == "invalid_response"
    assert [c["status"] for c in trace["calls"]] == ["failed", "completed"]
    assert trace["calls"][1]["input_tokens"] == 100
    assert trace["estimated_cost"] is None and trace["cost_label"] == "Non disponible"
    async def check(db):
        with pytest.raises(ValueError, match="coordinateur"):
            await retry_generation(db, await db.get(GenerationRun, rid), actor=await db.get(User, s["manager_id"]))
    asyncio.run(with_db(check))


def test_analyse_source_figee_et_usage_sur_echec_metier(pg_client, monkeypatch):
    from app import worker
    s, _, _, _, profile_id, _, _ = setup_runtime(pg_client, monkeypatch)
    monkeypatch.setenv("MIMIC_LLM_KEY_TEST", "confidentiel-llm-test")
    async def save(db):
        source = SourceDocument(project_id=s["project"], created_by=s["manager_id"], kind="need", name="Besoin",
            mime_type="text/plain", sha256="a"*64, size_bytes=50,
            extracted_text="Source initiale confidentiel-llm-test", status="analyzing")
        db.add(source); await db.flush()
        profile = await db.get(LLMProfileVersion, profile_id)
        run = AnalysisRun(project_id=s["project"], source_id=source.id, requested_by=s["manager_id"],
                          llm_profile_version_id=profile.id, **analysis_inputs(source, profile))
        db.add(run); await db.flush()
        source.extracted_text = "Source altérée"
        return run.id
    rid = asyncio.run(with_db(save))
    async def invoke(profile, messages, **kwargs):
        assert "Source initiale" in messages[1]["content"] and "confidentiel-llm-test" not in messages[1]["content"]
        return LLMResult("JSON invalide", profile.id, profile.provider, profile.model_name, 100, 20)
    monkeypatch.setattr(worker, "invoke_profile", invoke)
    asyncio.run(worker._analyze_source(rid))
    data = pg_client.get(f"{s['base']}/llm-runs/analysis/{rid}").json()
    assert data["status"] == "failed" and data["input_tokens"] == 100 and data["estimated_cost"] == "0.000450"
    assert "confidentiel-llm-test" not in json.dumps(data)


def test_refus_plan_accord_profil_et_trace_interprojet(pg_client, monkeypatch):
    s, pid, tid, agent, _, item_id, _ = setup_runtime(pg_client, monkeypatch, approved=False)
    assert delegate(pg_client, s, tid, agent, str(uuid4()), item_id).status_code == 409
    from .test_agent_hierarchy_progress import approve
    approve(pg_client, s, pid)
    cid = claim(pg_client, s, agent, tid)
    assert delegate(pg_client, s, tid, agent, cid, item_id, profile_version_id=str(uuid4())).status_code == 409
    assert delegate(pg_client, s, tid, agent, cid, str(uuid4())).status_code == 409
    rid = delegate(pg_client, s, tid, agent, cid, item_id).json()["generation_run_id"]
    foreign = str(uuid4())
    assert pg_client.get(f"/api/projects/{foreign}/llm-runs/generation/{rid}").status_code == 404
    assert pg_client.get(f"{s['base']}/llm-runs/generation/{uuid4()}").status_code == 404
    assert pg_client.post(f"/api/projects/{foreign}/agent-tasks/{tid}/llm", params={"session_id":agent[0]},
        headers=agent[1], json={"claim_id":cid,"document_type":"technical_doc","context_ids":[item_id]}).status_code == 401
    pg_client.cookies.clear(); login(pg_client, s["reader_email"])
    assert pg_client.get(f"{s['base']}/llm-runs/generation/{rid}").status_code == 200
    assert pg_client.post(f"{s['base']}/agent-tasks/{tid}/llm", params={"session_id":agent[0]},
        json={"claim_id":cid,"document_type":"technical_doc","context_ids":[item_id]}).status_code == 401


def test_accord_par_action_et_dispatch_echoue(pg_client, monkeypatch):
    from app import worker
    s, pid, tid, agent, _, item_id, _ = setup_runtime(pg_client, monkeypatch, autonomy_mode="per_action")
    url = f"{s['base']}/agent-tasks/{tid}"
    denied = pg_client.post(url+"/claim", params={"session_id":agent[0]}, headers=agent[1])
    assert denied.status_code == 409
    assert delegate(pg_client,s,tid,agent,str(uuid4()),item_id).status_code == 409
    aid = pg_client.get(url+"/approvals").json()["approvals"][0]["id"]
    decision = pg_client.post(url+f"/approvals/{aid}/approve", headers={"X-CSRF-Token":s["csrf"]})
    assert decision.status_code == 200, decision.text
    cid = claim(pg_client,s,agent,tid)
    def unavailable(*args):
        raise RuntimeError("credential-confidentiel")
    monkeypatch.setattr(worker.generate_document, "delay", unavailable)
    response = delegate(pg_client,s,tid,agent,cid,item_id)
    assert response.status_code == 202 and response.json()["status"] == "failed"
    rid = response.json()["generation_run_id"]
    trace = pg_client.get(f"{s['base']}/llm-runs/generation/{rid}").json()
    assert trace["error_code"] == "dispatch_unavailable" and trace["calls"] == []
    assert listing(pg_client,s,pid)["documentation"]["status"] == "failed"


def test_double_worker_et_migration_peuplee(pg_client, monkeypatch):
    from app import worker
    from sqlalchemy import text
    s, pid, tid, agent, profile_id, item_id, _ = setup_runtime(pg_client,monkeypatch)
    cid = claim(pg_client,s,agent,tid)
    rid = delegate(pg_client,s,tid,agent,cid,item_id).json()["generation_run_id"]
    seen = []
    async def invoke(profile,messages,**kwargs):
        seen.append(profile.id)
        await asyncio.sleep(.05)
        return LLMResult("# Brouillon",profile.id,profile.provider,profile.model_name,100,20)
    monkeypatch.setattr(worker,"invoke_profile",invoke)
    async def race():
        await asyncio.wait_for(asyncio.gather(worker._generate_document(rid),worker._generate_document(rid)),10)
    asyncio.run(race())
    assert seen == [profile_id]
    command = [sys.executable,"-m","alembic"]
    try:
        subprocess.run([*command,"downgrade","0016_project_memory"],cwd=ROOT,check=True)
        async def preserved(db):
            assert await db.scalar(text("SELECT count(*) FROM generation_run WHERE id = :id"),{"id":rid}) == 1
            assert await db.scalar(text("SELECT count(*) FROM deliverable_version WHERE generation_run_id = :id"),{"id":rid}) == 1
            assert await db.scalar(text("SELECT count(*) FROM llm_profile_version WHERE id = :id"),{"id":profile_id}) == 1
        asyncio.run(with_db(preserved))
    finally:
        subprocess.run([*command,"upgrade","head"],cwd=ROOT,check=True)
    assert listing(pg_client,s,pid)["documentation"]["status"] == "completed"


def test_selection_explicitement_verifiee(pg_client, monkeypatch):
    s, _, tid, agent, profile_id, item_id, _ = setup_runtime(pg_client, monkeypatch)
    cid = claim(pg_client,s,agent,tid)
    assert delegate(pg_client,s,tid,agent,cid,item_id,profile_version_id=profile_id).status_code == 409
    async def checked(db):
        db.add(LLMConnectionCheck(profile_version_id=profile_id,actor_id=s["manager_id"],outcome="success",latency_ms=1))
    asyncio.run(with_db(checked))
    result = delegate(pg_client,s,tid,agent,cid,item_id,profile_version_id=profile_id)
    assert result.status_code == 202 and result.json()["profile_version_id"] == profile_id


def test_desactivation_globale_bloque_version_historique(pg_client, monkeypatch):
    from app import worker
    s, _, tid, agent, profile_id, item_id, _ = setup_runtime(pg_client, monkeypatch)
    cid = claim(pg_client,s,agent,tid)
    rid = delegate(pg_client,s,tid,agent,cid,item_id).json()["generation_run_id"]
    async def disable(db):
        old = await db.get(LLMProfileVersion,profile_id)
        old.is_current = False
        await db.flush()
        db.add(LLMProfileVersion(profile_id=old.profile_id,version=2,name="Désactivé",provider="ollama",
            model_name="test",base_url=old.base_url,max_output_tokens=1000,timeout_seconds=10,max_retries=0,
            enabled=False,is_current=True,created_by=s["manager_id"]))
    asyncio.run(with_db(disable))
    async def forbidden(*args,**kwargs):
        pytest.fail("Aucun appel fournisseur ne doit être effectué.")
    monkeypatch.setattr(worker,"invoke_profile",forbidden)
    asyncio.run(worker._generate_document(rid))
    trace = pg_client.get(f"{s['base']}/llm-runs/generation/{rid}").json()
    assert trace["error_code"] == "configuration_unavailable" and trace["calls"] == []
