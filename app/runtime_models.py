"""Runtimes, choix immuables et registre cumulatif d'admission."""
from datetime import datetime
from decimal import Decimal
from uuid import uuid4

from sqlalchemy import Boolean, CheckConstraint, DateTime, ForeignKey, Index, Integer, JSON, Numeric, String, UniqueConstraint, text
from sqlalchemy.orm import Mapped, mapped_column
from app.identity_models import Base, utcnow


class RuntimeVersion(Base):
    __tablename__ = 'runtime_version'
    __table_args__ = (
        UniqueConstraint('runtime_id', 'version'),
        CheckConstraint("version > 0 AND timeout_seconds BETWEEN 1 AND 3600 AND (kind <> 'llm' OR timeout_seconds <= 80)"),
        CheckConstraint("(kind = 'llm' AND profile_version_id IS NOT NULL AND client_code IS NULL) OR (kind = 'client' AND profile_version_id IS NULL AND client_code IS NOT NULL)"),
        Index('uq_runtime_current', 'runtime_id', unique=True, postgresql_where=text('is_current')),
    )
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid4()))
    runtime_id: Mapped[str] = mapped_column(String(36), nullable=False)
    project_id: Mapped[str] = mapped_column(ForeignKey('project.id'), nullable=False)
    version: Mapped[int] = mapped_column(Integer, nullable=False)
    name: Mapped[str] = mapped_column(String(120), nullable=False)
    kind: Mapped[str] = mapped_column(String(10), nullable=False)
    profile_version_id: Mapped[str | None] = mapped_column(ForeignKey('llm_profile_version.id'))
    client_code: Mapped[str | None] = mapped_column(String(32))
    timeout_seconds: Mapped[int] = mapped_column(Integer, nullable=False)
    include_memory: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    enabled: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    is_current: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    reason: Mapped[str] = mapped_column(String(500), nullable=False)
    created_by: Mapped[str] = mapped_column(ForeignKey('app_user.id'), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)


class RuntimeBinding(Base):
    __tablename__ = 'runtime_binding'
    __table_args__ = (UniqueConstraint('project_id', 'scope', 'scope_id', 'version'),
        CheckConstraint("scope IN ('project','agent','session','task') AND version > 0"),
        Index('uq_runtime_binding_current', 'project_id','scope','scope_id',unique=True,
              postgresql_where=text('is_current')))
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid4()))
    project_id: Mapped[str] = mapped_column(ForeignKey('project.id'), nullable=False)
    scope: Mapped[str] = mapped_column(String(10), nullable=False)
    scope_id: Mapped[str] = mapped_column(String(64), nullable=False)
    version: Mapped[int] = mapped_column(Integer, nullable=False)
    runtime_version_id: Mapped[str | None] = mapped_column(ForeignKey('runtime_version.id'))
    is_current: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    reason: Mapped[str] = mapped_column(String(500), nullable=False)
    created_by: Mapped[str] = mapped_column(ForeignKey('app_user.id'), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)


class RuntimeQuota(Base):
    __tablename__ = 'runtime_quota'
    __table_args__ = (UniqueConstraint('project_id','scope','scope_id','version'),
        CheckConstraint("scope IN ('project','agent','session','task','model') AND version > 0"),
        CheckConstraint('(max_tokens IS NULL OR max_tokens > 0) AND (max_seconds IS NULL OR max_seconds > 0) AND (max_concurrency IS NULL OR max_concurrency > 0) AND (max_cost IS NULL OR max_cost > 0)'),
        CheckConstraint('(max_cost IS NULL AND currency IS NULL) OR (max_cost IS NOT NULL AND currency IS NOT NULL)'),
        Index('uq_runtime_quota_current','project_id','scope','scope_id',unique=True,
              postgresql_where=text('is_current')))
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid4()))
    project_id: Mapped[str] = mapped_column(ForeignKey('project.id'), nullable=False)
    scope: Mapped[str] = mapped_column(String(10), nullable=False)
    scope_id: Mapped[str] = mapped_column(String(64), nullable=False)
    version: Mapped[int] = mapped_column(Integer, nullable=False)
    max_tokens: Mapped[int | None] = mapped_column(Integer)
    max_seconds: Mapped[Decimal | None] = mapped_column(Numeric(16,6))
    max_concurrency: Mapped[int | None] = mapped_column(Integer)
    max_cost: Mapped[Decimal | None] = mapped_column(Numeric(16,6))
    currency: Mapped[str | None] = mapped_column(String(3))
    is_current: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    reason: Mapped[str] = mapped_column(String(500), nullable=False)
    created_by: Mapped[str] = mapped_column(ForeignKey('app_user.id'), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)


class RuntimeSelection(Base):
    __tablename__ = 'runtime_selection'
    __table_args__ = (CheckConstraint("origin IN ('task','session','agent','project','explicit','default')"),)
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid4()))
    attempt_id: Mapped[str] = mapped_column(ForeignKey('agent_task_run.id'), unique=True, nullable=False)
    project_id: Mapped[str] = mapped_column(ForeignKey('project.id'), nullable=False)
    runtime_version_id: Mapped[str | None] = mapped_column(ForeignKey('runtime_version.id'))
    binding_id: Mapped[str | None] = mapped_column(ForeignKey('runtime_binding.id'))
    profile_version_id: Mapped[str | None] = mapped_column(ForeignKey('llm_profile_version.id'))
    origin: Mapped[str] = mapped_column(String(10), nullable=False)
    context: Mapped[dict] = mapped_column(JSON, nullable=False)
    memory_snapshot: Mapped[dict | None] = mapped_column(JSON)
    deadline: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)


class RuntimeUsage(Base):
    __tablename__ = 'runtime_usage'
    __table_args__ = (CheckConstraint("status IN ('active','completed','failed','unknown')"),
        CheckConstraint('(call_id IS NOT NULL AND external_selection_id IS NULL) OR (call_id IS NULL AND external_selection_id IS NOT NULL)'),
        CheckConstraint('charged_tokens >= 0 AND charged_seconds >= 0 AND (charged_cost IS NULL OR charged_cost >= 0)'),
        Index('ix_runtime_usage_project','project_id','started_at'))
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid4()))
    project_id: Mapped[str] = mapped_column(ForeignKey('project.id'), nullable=False)
    call_id: Mapped[str | None] = mapped_column(ForeignKey('llm_call_attempt.id'), unique=True)
    external_selection_id: Mapped[str | None] = mapped_column(ForeignKey('runtime_selection.id'), unique=True)
    context: Mapped[dict] = mapped_column(JSON, nullable=False)
    policy_ids: Mapped[list] = mapped_column(JSON, nullable=False)
    status: Mapped[str] = mapped_column(String(10), default='active', nullable=False)
    charged_tokens: Mapped[int] = mapped_column(Integer, nullable=False)
    charged_seconds: Mapped[Decimal] = mapped_column(Numeric(16,6), nullable=False)
    charged_cost: Mapped[Decimal | None] = mapped_column(Numeric(16,6))
    actual_tokens: Mapped[int | None] = mapped_column(Integer)
    actual_seconds: Mapped[Decimal | None] = mapped_column(Numeric(16,6))
    actual_cost: Mapped[Decimal | None] = mapped_column(Numeric(16,6))
    currency: Mapped[str | None] = mapped_column(String(3))
    error_code: Mapped[str | None] = mapped_column(String(40))
    reconciled_by: Mapped[str | None] = mapped_column(ForeignKey('app_user.id'))
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)
    ended_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
