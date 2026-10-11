import asyncio
import sys
from types import SimpleNamespace

import pytest
from sqlalchemy import select

from app.identity_models import AuditEvent
from app.llm_models import LLMConnectionCheck, LLMProfileVersion, LLMSettings
from app.llm_service import LLMInvocationError, LLMResult, invoke_profile, parse_form

from .conftest import create_user, with_db
from .test_identity_acl import csrf, login


def local_profile_payload(token: str, **changes) -> dict:
    values = {"name": "Ollama projet", "provider": "ollama", "model_name": "llama3.2",
              "base_url": "http://127.0.0.1:11434", "api_version": "", "credential_env": "",
              "temperature": "0.2", "max_output_tokens": "512", "timeout_seconds": "30",
              "max_retries": "1", "enabled": "true", "csrf_token": token}
    values.update(changes)
    return values


def test_profil_versionne_verification_defaut_et_rbac(pg_client, monkeypatch):
    monkeypatch.setenv("MIMIC_LLM_ALLOWED_BASE_URLS", "http://127.0.0.1:11434")
    _, admin_email = create_user(admin=True)
    token = login(pg_client, admin_email)
    response = pg_client.post("/admin/llm", data=local_profile_payload(token))
    assert response.status_code == 303

    async def get_profile(db):
        return await db.scalar(select(LLMProfileVersion).where(LLMProfileVersion.is_current.is_(True)))

    profile = asyncio.run(with_db(get_profile))
    assert profile.version == 1
    blocked = pg_client.post(f"/admin/llm/{profile.profile_id}/default",
                             data={"csrf_token": token})
    assert blocked.status_code == 200 and "Vérifiez la connexion" in blocked.text

    async def fake_invoke(profile, messages, **kwargs):
        return LLMResult("OK", profile.id, profile.provider, profile.model_name, 5, 1)

    monkeypatch.setattr("app.llm_service.invoke_profile", fake_invoke)
    assert pg_client.post(f"/admin/llm/{profile.profile_id}/check",
                          data={"csrf_token": token}).status_code == 303
    assert pg_client.post(f"/admin/llm/{profile.profile_id}/default",
                          data={"csrf_token": token}).status_code == 303

    async def selected(db):
        settings = await db.get(LLMSettings, 1)
        return settings.default_version_id

    assert asyncio.run(with_db(selected)) == profile.id
    async def failed_invoke(profile, messages, **kwargs):
        raise LLMInvocationError("timeout")

    monkeypatch.setattr("app.llm_service.invoke_profile", failed_invoke)
    assert pg_client.post(f"/admin/llm/{profile.profile_id}/check",
                          data={"csrf_token": token}).status_code == 303
    assert asyncio.run(with_db(selected)) is None
    monkeypatch.setattr("app.llm_service.invoke_profile", fake_invoke)
    assert pg_client.post(f"/admin/llm/{profile.profile_id}/check",
                          data={"csrf_token": token}).status_code == 303
    assert pg_client.post(f"/admin/llm/{profile.profile_id}/default",
                          data={"csrf_token": token}).status_code == 303
    edited = pg_client.post(f"/admin/llm/{profile.profile_id}",
                            data=local_profile_payload(token, version="1", model_name="llama3.3"))
    assert edited.status_code == 303
    assert asyncio.run(with_db(selected)) is None
    revised = asyncio.run(with_db(get_profile))
    assert revised.version == 2 and revised.model_name == "llama3.3"
    assert pg_client.post(f"/admin/llm/{profile.profile_id}/default",
                          data={"csrf_token": token}).status_code == 200

    invalid = pg_client.post("/admin/llm", data=local_profile_payload(
        token, provider="openai", base_url="", credential_env="sk-secret-a-ne-pas-afficher"))
    assert invalid.status_code == 200
    assert "sk-secret-a-ne-pas-afficher" not in invalid.text

    _, non_admin_email = create_user()
    from fastapi.testclient import TestClient
    from app.main import app

    with TestClient(app, follow_redirects=False) as member:
        member_token = login(member, non_admin_email)
        assert member.get("/admin/llm").status_code == 403
        assert member.post("/admin/llm", data=local_profile_payload(member_token)).status_code == 403

    async def history(db):
        versions = (await db.scalars(select(LLMProfileVersion).where(
            LLMProfileVersion.profile_id == profile.profile_id).order_by(LLMProfileVersion.version))).all()
        checks = (await db.scalars(select(LLMConnectionCheck))).all()
        actions = (await db.scalars(select(AuditEvent.action))).all()
        return versions, checks, actions

    versions, checks, actions = asyncio.run(with_db(history))
    assert [p.version for p in versions] == [1, 2]
    assert [check.outcome for check in checks].count("success") == 2
    assert [check.outcome for check in checks].count("failure") == 1
    assert {"llm.profile.create", "llm.connection.check", "llm.default.choose",
            "llm.profile.revise", "llm.default.clear"}.issubset(set(actions))


def test_client_llm_borne_parametres_et_masque_erreurs(monkeypatch):
    monkeypatch.setenv("MIMIC_LLM_ALLOWED_BASE_URLS", "http://127.0.0.1:11434")
    data = parse_form({"name": "Local", "provider": "ollama", "model_name": "llama3.2",
        "base_url": "http://127.0.0.1:11434", "max_output_tokens": 512,
        "timeout_seconds": 30, "max_retries": 1})
    profile = LLMProfileVersion(id="profile-version-test", profile_id="profile-test", version=1,
                                is_current=True, created_by="user-test", **data.model_dump())
    seen = {}

    async def success(**kwargs):
        seen.update(kwargs)
        return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content="Proposition"))],
                               usage=SimpleNamespace(prompt_tokens=4, completion_tokens=2))

    monkeypatch.setitem(sys.modules, "litellm", SimpleNamespace(acompletion=success))
    result = asyncio.run(invoke_profile(profile, [{"role": "user", "content": "Bonjour"}],
                                        output_limit=1000, timeout_limit=90, retries=3))
    assert result.text == "Proposition" and result.input_tokens == 4
    assert seen["model"] == "ollama_chat/llama3.2"
    assert (seen["max_tokens"], seen["timeout"], seen["num_retries"]) == (512, 30, 1)

    async def failure(**kwargs):
        raise TimeoutError("clé-secrète-et-contenu-du-prompt")

    monkeypatch.setitem(sys.modules, "litellm", SimpleNamespace(acompletion=failure))
    with pytest.raises(LLMInvocationError) as captured:
        asyncio.run(invoke_profile(profile, [{"role": "user", "content": "Bonjour"}]))
    assert captured.value.category == "timeout"
    assert "clé-secrète" not in str(captured.value)


def test_parametres_et_destinations_llm_refuses(monkeypatch):
    monkeypatch.setenv("MIMIC_LLM_ALLOWED_BASE_URLS", "http://127.0.0.1:11434")
    base = {"name": "Local", "provider": "ollama", "model_name": "llama3.2",
            "base_url": "http://127.0.0.1:11434", "max_output_tokens": 512,
            "timeout_seconds": 30, "max_retries": 1}
    for change in ({"base_url": "http://127.0.0.1:9999"},
                   {"base_url": "http://127.0.0.1:11434@evil.example"},
                   {"credential_env": "sk-cle-brute"}, {"max_retries": 99}):
        with pytest.raises(ValueError):
            parse_form({**base, **change})
