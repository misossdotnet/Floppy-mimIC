"""Gouvernance des profils LLM cloud/locaux et adaptateur unique d'appel."""

import os
import re
from dataclasses import dataclass
from time import monotonic
from urllib.parse import urlsplit, urlunsplit
from uuid import uuid4

from pydantic import BaseModel, Field, ValidationError, field_validator, model_validator
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.identity_models import User, utcnow
from app.identity_service import audit
from app.llm_models import LLMConnectionCheck, LLMProfileVersion, LLMSettings


PROVIDERS = {
    "openai": "OpenAI (cloud)",
    "azure_openai": "Azure OpenAI (cloud)",
    "anthropic": "Anthropic (cloud)",
    "ollama": "Ollama (local)",
    "openai_compatible": "API compatible OpenAI (locale ou privée)",
}
CUSTOM_BASE_PROVIDERS = {"azure_openai", "ollama", "openai_compatible"}
CLOUD_PROVIDERS = {"openai", "azure_openai", "anthropic"}
SECRET_PATTERN = re.compile(r"MIMIC_LLM_KEY_[A-Z][A-Z0-9_]{0,79}\Z")
MODEL_PATTERN = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:/-]{0,159}\Z")
API_VERSION_PATTERN = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,63}\Z")


def canonical_base_url(value: str) -> str:
    value = value.strip()
    if not value or len(value) > 500 or any(c.isspace() for c in value) or any(c in value for c in "\\%@"):
        raise ValueError("Adresse de base invalide.")
    parsed = urlsplit(value)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname or parsed.username or parsed.password:
        raise ValueError("L'adresse doit être HTTP(S), sans identifiants intégrés.")
    if parsed.query or parsed.fragment or ".." in parsed.path or "//" in parsed.path:
        raise ValueError("L'adresse ne doit contenir ni requête, ni fragment, ni chemin ambigu.")
    try:
        port = parsed.port
    except ValueError as exc:
        raise ValueError("Port d'adresse invalide.") from exc
    host = parsed.hostname.lower()
    if ":" in host:
        host = f"[{host}]"
    netloc = host + (f":{port}" if port is not None else "")
    path = parsed.path.rstrip("/")
    return urlunsplit((parsed.scheme.lower(), netloc, path, "", ""))


def allowed_base_urls() -> list[str]:
    raw = os.environ.get("MIMIC_LLM_ALLOWED_BASE_URLS", "")
    return sorted({canonical_base_url(item) for item in raw.split(",") if item.strip()})


class LLMProfileInput(BaseModel):
    name: str = Field(min_length=1, max_length=120)
    provider: str
    model_name: str = Field(min_length=1, max_length=160)
    base_url: str | None = None
    api_version: str | None = None
    credential_env: str | None = None
    temperature: float | None = Field(default=None, ge=0, le=2)
    max_output_tokens: int = Field(ge=1, le=32768)
    timeout_seconds: int = Field(ge=1, le=300)
    max_retries: int = Field(ge=0, le=3)
    enabled: bool = True

    @field_validator("name", "model_name", mode="before")
    @classmethod
    def strip_required(cls, value):
        return value.strip() if isinstance(value, str) else value

    @field_validator("base_url", "api_version", "credential_env", mode="before")
    @classmethod
    def empty_to_none(cls, value):
        return value.strip() or None if isinstance(value, str) else value

    @model_validator(mode="after")
    def validate_provider(self):
        if self.provider not in PROVIDERS:
            raise ValueError("Fournisseur non pris en charge.")
        if not MODEL_PATTERN.fullmatch(self.model_name):
            raise ValueError("Identifiant de modèle invalide.")
        if self.model_name.startswith(("openai/", "azure/", "anthropic/", "ollama/", "ollama_chat/")):
            raise ValueError("Saisissez le modèle sans préfixe de fournisseur.")
        if self.provider in CUSTOM_BASE_PROVIDERS:
            if not self.base_url:
                raise ValueError("Une adresse de base autorisée est requise.")
            self.base_url = canonical_base_url(self.base_url)
            if self.base_url not in allowed_base_urls():
                raise ValueError("Cette adresse ne figure pas dans MIMIC_LLM_ALLOWED_BASE_URLS.")
            if self.provider == "azure_openai" and not self.base_url.startswith("https://"):
                raise ValueError("Azure OpenAI exige HTTPS.")
        elif self.base_url:
            raise ValueError("Ce fournisseur utilise son adresse officielle ; retirez l'adresse personnalisée.")
        if self.provider == "azure_openai":
            if not self.api_version or not API_VERSION_PATTERN.fullmatch(self.api_version):
                raise ValueError("Une version d'API Azure valide est requise.")
        elif self.api_version:
            raise ValueError("La version d'API ne s'applique qu'à Azure OpenAI.")
        if self.provider in CLOUD_PROVIDERS and not self.credential_env:
            raise ValueError("Une référence de clé est requise pour un fournisseur cloud.")
        if self.credential_env and not SECRET_PATTERN.fullmatch(self.credential_env):
            raise ValueError("La référence doit commencer par MIMIC_LLM_KEY_.")
        return self


def parse_form(values: dict) -> LLMProfileInput:
    try:
        return LLMProfileInput.model_validate(values)
    except ValidationError as exc:
        raise ValueError(exc.errors()[0]["msg"].removeprefix("Value error, ")) from exc


def credential_available(profile: LLMProfileVersion) -> bool:
    return not profile.credential_env or bool(os.environ.get(profile.credential_env))


def profile_ready(profile: LLMProfileVersion) -> bool:
    return profile.is_current and invocation_ready(profile)


def invocation_ready(profile: LLMProfileVersion) -> bool:
    """Une version figée reste utilisable après révision ; secrets/adresses restent contrôlés."""
    if not profile.enabled or not credential_available(profile):
        return False
    if profile.provider in CUSTOM_BASE_PROVIDERS:
        return profile.base_url in allowed_base_urls()
    return True


async def current_profiles(db: AsyncSession) -> list[LLMProfileVersion]:
    return (await db.scalars(select(LLMProfileVersion).where(
        LLMProfileVersion.is_current.is_(True)).order_by(LLMProfileVersion.name))).all()


async def current_profile(db: AsyncSession, profile_id: str, *, lock: bool = False) -> LLMProfileVersion | None:
    query = select(LLMProfileVersion).where(
        LLMProfileVersion.profile_id == profile_id, LLMProfileVersion.is_current.is_(True))
    if lock:
        query = query.with_for_update().execution_options(populate_existing=True)
    return await db.scalar(query)


async def settings(db: AsyncSession, *, lock: bool = False) -> LLMSettings:
    row = await db.get(LLMSettings, 1, with_for_update=lock)
    if row is None:
        row = LLMSettings(id=1, default_version_id=None)
        db.add(row)
        await db.flush()
    return row


async def create_profile(db: AsyncSession, actor: User, data: LLMProfileInput) -> LLMProfileVersion:
    profile = LLMProfileVersion(profile_id=str(uuid4()), version=1, created_by=actor.id,
                                is_current=True, **data.model_dump())
    db.add(profile)
    await db.flush()
    audit(db, "llm.profile.create", "success", actor_id=actor.id,
          target_type="llm_profile", target_id=profile.profile_id,
          details={"version": 1, "provider": profile.provider})
    return profile


async def revise_profile(db: AsyncSession, actor: User, profile_id: str,
                         data: LLMProfileInput, *, expected_version: int) -> LLMProfileVersion | None:
    old = await current_profile(db, profile_id, lock=True)
    if old is None:
        return None
    if old.version != expected_version:
        raise ValueError("Le profil a été modifié entre-temps ; rechargez la page.")
    old.is_current = False
    await db.flush()
    new = LLMProfileVersion(profile_id=profile_id, version=old.version + 1,
                            created_by=actor.id, is_current=True, **data.model_dump())
    db.add(new)
    await db.flush()
    current_settings = await settings(db, lock=True)
    if current_settings.default_version_id == old.id:
        # Une nouvelle version doit réussir sa propre vérification avant d'être sélectionnée.
        current_settings.default_version_id = None
        current_settings.updated_by = actor.id
        current_settings.updated_at = utcnow()
    audit(db, "llm.profile.revise", "success", actor_id=actor.id,
          target_type="llm_profile", target_id=profile_id,
          details={"previous_version": old.version, "new_version": new.version,
                   "enabled": new.enabled, "provider": new.provider})
    return new


async def choose_default(db: AsyncSession, actor: User, profile_id: str) -> LLMProfileVersion | None:
    profile = await current_profile(db, profile_id, lock=True)
    if profile is None:
        return None
    if not profile_ready(profile):
        raise ValueError("Profil indisponible : activez-le, configurez sa clé et son adresse autorisée.")
    check = await db.scalar(select(LLMConnectionCheck).where(
        LLMConnectionCheck.profile_version_id == profile.id,
    ).order_by(LLMConnectionCheck.created_at.desc()).limit(1))
    if check is None or check.outcome != "success":
        raise ValueError("Vérifiez la connexion de cette version avant de la choisir par défaut.")
    current_settings = await settings(db, lock=True)
    previous = current_settings.default_version_id
    current_settings.default_version_id = profile.id
    current_settings.updated_by = actor.id
    current_settings.updated_at = utcnow()
    audit(db, "llm.default.choose", "success", actor_id=actor.id,
          target_type="llm_profile", target_id=profile.profile_id,
          details={"previous_version_id": previous, "new_version_id": profile.id})
    return profile


async def default_profile(db: AsyncSession) -> LLMProfileVersion | None:
    current_settings = await settings(db)
    if not current_settings.default_version_id:
        return None
    profile = await db.get(LLMProfileVersion, current_settings.default_version_id)
    return profile if profile and profile_ready(profile) else None


class LLMInvocationError(Exception):
    def __init__(self, category: str, *, input_tokens: int | None = None, output_tokens: int | None = None):
        super().__init__(category)
        self.category = category
        self.input_tokens = input_tokens
        self.output_tokens = output_tokens


@dataclass(frozen=True)
class LLMResult:
    text: str
    profile_version_id: str
    provider: str
    model_name: str
    input_tokens: int | None
    output_tokens: int | None


def _error_category(exc: Exception) -> str:
    name = type(exc).__name__.lower()
    if "timeout" in name:
        return "timeout"
    if "rate" in name or "quota" in name:
        return "rate_limit"
    if "auth" in name or "permission" in name:
        return "authentication"
    if "connect" in name or "network" in name:
        return "connection"
    if "badrequest" in name or "invalid" in name:
        return "invalid_request"
    return "provider_error"


async def invoke_profile(profile: LLMProfileVersion, messages: list[dict], *,
                         output_limit: int | None = None, timeout_limit: int | None = None,
                         retries: int | None = None, temperature_override: float | None = None) -> LLMResult:
    """Point unique des appels. Le résultat reste une proposition non validée."""
    if not invocation_ready(profile):
        raise LLMInvocationError("configuration_unavailable")
    if not messages or not all(m.get("role") in {"system", "user", "assistant"} and
                               isinstance(m.get("content"), str) for m in messages):
        raise LLMInvocationError("invalid_request")
    try:
        from litellm import acompletion
    except ImportError as exc:
        raise LLMInvocationError("adapter_unavailable") from exc
    prefix = {"openai": "openai", "azure_openai": "azure", "anthropic": "anthropic",
              "ollama": "ollama_chat", "openai_compatible": "openai"}[profile.provider]
    kwargs = {
        "model": f"{prefix}/{profile.model_name}",
        "messages": messages,
        "max_tokens": min(max(output_limit or profile.max_output_tokens, 1), profile.max_output_tokens),
        "timeout": min(max(timeout_limit or profile.timeout_seconds, 1), profile.timeout_seconds),
        "num_retries": min(max(retries if retries is not None else profile.max_retries, 0), profile.max_retries),
    }
    temperature = profile.temperature if temperature_override is None else temperature_override
    if temperature is not None:
        kwargs["temperature"] = temperature
    if profile.base_url:
        kwargs["api_base"] = profile.base_url
    if profile.api_version:
        kwargs["api_version"] = profile.api_version
    if profile.credential_env:
        kwargs["api_key"] = os.environ[profile.credential_env]
    elif profile.provider == "openai_compatible":
        kwargs["api_key"] = "local-no-key"
    try:
        response = await acompletion(**kwargs)
        usage = getattr(response, "usage", None)
        input_tokens = getattr(usage, "prompt_tokens", None)
        output_tokens = getattr(usage, "completion_tokens", None)
        input_tokens = input_tokens if type(input_tokens) is int and input_tokens >= 0 else None
        output_tokens = output_tokens if type(output_tokens) is int and output_tokens >= 0 else None
        content = response.choices[0].message.content
        if not isinstance(content, str) or not content:
            raise LLMInvocationError("empty_response", input_tokens=input_tokens, output_tokens=output_tokens)
        return LLMResult(text=content, profile_version_id=profile.id,
                         provider=profile.provider, model_name=profile.model_name,
                         input_tokens=input_tokens, output_tokens=output_tokens)
    except LLMInvocationError:
        raise
    except Exception as exc:
        # Le message fournisseur peut contenir un prompt ou un secret : ne jamais le propager.
        raise LLMInvocationError(_error_category(exc)) from None


async def check_connection(db: AsyncSession, actor: User,
                           profile: LLMProfileVersion) -> LLMConnectionCheck:
    started = monotonic()
    category = None
    try:
        await invoke_profile(profile, [{"role": "user", "content": "Réponds uniquement OK."}],
                             output_limit=16, timeout_limit=30, retries=0)
        outcome = "success"
    except LLMInvocationError as exc:
        outcome, category = "failure", exc.category
    elapsed = min(int((monotonic() - started) * 1000), 2_147_483_647)
    check = LLMConnectionCheck(profile_version_id=profile.id, actor_id=actor.id,
                               outcome=outcome, error_category=category, latency_ms=elapsed)
    db.add(check)
    if outcome == "failure":
        current_settings = await settings(db, lock=True)
        if current_settings.default_version_id == profile.id:
            current_settings.default_version_id = None
            current_settings.updated_by = actor.id
            current_settings.updated_at = utcnow()
            audit(db, "llm.default.clear", "success", actor_id=actor.id,
                  target_type="llm_profile", target_id=profile.profile_id,
                  details={"version": profile.version, "reason": "connection_check_failed"})
    audit(db, "llm.connection.check", outcome, actor_id=actor.id,
          target_type="llm_profile", target_id=profile.profile_id,
          details={"version": profile.version, "category": category, "latency_ms": elapsed})
    await db.flush()
    return check
