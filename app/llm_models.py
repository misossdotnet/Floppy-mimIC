"""Configurations LLM versionnées, sélection du défaut et diagnostics minimisés."""

from datetime import datetime
from uuid import uuid4

from sqlalchemy import Boolean, CheckConstraint, DateTime, Float, ForeignKey, Index, Integer, JSON, Numeric, String, UniqueConstraint, text
from sqlalchemy.orm import Mapped, mapped_column

from app.identity_models import Base, utcnow


class LLMProfileVersion(Base):
    __tablename__ = "llm_profile_version"
    __table_args__ = (
        UniqueConstraint("profile_id", "version", name="uq_llm_profile_version"),
        Index("uq_llm_profile_current", "profile_id", unique=True,
              postgresql_where=text("is_current")),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid4()))
    profile_id: Mapped[str] = mapped_column(String(36), index=True, nullable=False)
    version: Mapped[int] = mapped_column(Integer, nullable=False)
    name: Mapped[str] = mapped_column(String(120), nullable=False)
    provider: Mapped[str] = mapped_column(String(32), nullable=False)
    model_name: Mapped[str] = mapped_column(String(160), nullable=False)
    base_url: Mapped[str | None] = mapped_column(String(500))
    api_version: Mapped[str | None] = mapped_column(String(64))
    credential_env: Mapped[str | None] = mapped_column(String(100))
    temperature: Mapped[float | None] = mapped_column(Float)
    max_output_tokens: Mapped[int] = mapped_column(Integer, nullable=False)
    timeout_seconds: Mapped[int] = mapped_column(Integer, nullable=False)
    max_retries: Mapped[int] = mapped_column(Integer, nullable=False)
    enabled: Mapped[bool] = mapped_column(Boolean, nullable=False)
    is_current: Mapped[bool] = mapped_column(Boolean, nullable=False)
    created_by: Mapped[str] = mapped_column(ForeignKey("app_user.id"), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)


class LLMSettings(Base):
    __tablename__ = "llm_settings"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    default_version_id: Mapped[str | None] = mapped_column(ForeignKey("llm_profile_version.id"))
    updated_by: Mapped[str | None] = mapped_column(ForeignKey("app_user.id"))
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)


class LLMConnectionCheck(Base):
    __tablename__ = "llm_connection_check"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid4()))
    profile_version_id: Mapped[str] = mapped_column(ForeignKey("llm_profile_version.id"), index=True)
    actor_id: Mapped[str] = mapped_column(ForeignKey("app_user.id"))
    outcome: Mapped[str] = mapped_column(String(24), nullable=False)
    error_category: Mapped[str | None] = mapped_column(String(40))
    latency_ms: Mapped[int | None] = mapped_column(Integer)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)


class LLMCallAttempt(Base):
    """Un appel fournisseur réel ; les retries de l'adaptateur sont désactivés."""
    __tablename__ = "llm_call_attempt"
    __table_args__ = (
        CheckConstraint("(CASE WHEN analysis_run_id IS NOT NULL THEN 1 ELSE 0 END + CASE WHEN generation_run_id IS NOT NULL THEN 1 ELSE 0 END + CASE WHEN scheduled_llm_run_id IS NOT NULL THEN 1 ELSE 0 END) = 1", name="ck_llm_call_owner"),
        CheckConstraint("status IN ('running', 'completed', 'failed')", name="ck_llm_call_status"),
        CheckConstraint("attempt_number > 0", name="ck_llm_call_number"),
        UniqueConstraint("analysis_run_id", "attempt_number", name="uq_llm_analysis_call"),
        UniqueConstraint("generation_run_id", "attempt_number", name="uq_llm_generation_call"),
        UniqueConstraint("scheduled_llm_run_id", "attempt_number", name="uq_llm_coffy_call"),
    )
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid4()))
    analysis_run_id: Mapped[str | None] = mapped_column(ForeignKey("analysis_run.id"), index=True)
    generation_run_id: Mapped[str | None] = mapped_column(ForeignKey("generation_run.id"), index=True)
    scheduled_llm_run_id: Mapped[str | None] = mapped_column(ForeignKey("scheduled_llm_run.id"), index=True)
    profile_version_id: Mapped[str] = mapped_column(ForeignKey("llm_profile_version.id"), nullable=False)
    attempt_number: Mapped[int] = mapped_column(Integer, nullable=False)
    messages: Mapped[list] = mapped_column(JSON, nullable=False)
    parameters: Mapped[dict] = mapped_column(JSON, nullable=False)
    pricing_snapshot: Mapped[dict | None] = mapped_column(JSON)
    status: Mapped[str] = mapped_column(String(16), nullable=False)
    input_tokens: Mapped[int | None] = mapped_column(Integer)
    output_tokens: Mapped[int | None] = mapped_column(Integer)
    estimated_cost: Mapped[float | None] = mapped_column(Numeric(12, 6))
    error_code: Mapped[str | None] = mapped_column(String(40))
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)
    ended_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
