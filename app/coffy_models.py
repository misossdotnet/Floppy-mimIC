"""Définitions CoFFY : configuration versionnée, occurrences et runs."""

from datetime import date, datetime
from uuid import uuid4

from sqlalchemy import Boolean, CheckConstraint, Date, DateTime, ForeignKey, Index, Integer, JSON, Numeric, String, Text, UniqueConstraint, text
from sqlalchemy.orm import Mapped, mapped_column

from app.identity_models import Base, utcnow


class ScheduledLLMTask(Base):
    __tablename__ = "scheduled_llm_task"
    __table_args__ = (
        CheckConstraint("status IN ('draft','active','paused','archived','deleted')", name="ck_scheduled_task_status"),
        Index("ix_scheduled_task_project_status", "project_id", "status"),
        Index("ix_coffy_task_due", "status", "next_run_at"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid4()))
    project_id: Mapped[str] = mapped_column(ForeignKey("project.id"), nullable=False)
    created_by: Mapped[str] = mapped_column(ForeignKey("app_user.id"), nullable=False)
    status: Mapped[str] = mapped_column(String(16), default="draft", nullable=False)
    current_version: Mapped[int] = mapped_column(Integer, default=1, nullable=False)
    revision: Mapped[int] = mapped_column(Integer, default=1, nullable=False)
    next_run_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)


class ScheduledLLMTaskVersion(Base):
    __tablename__ = "scheduled_llm_task_version"
    __table_args__ = (
        UniqueConstraint("task_id", "version", name="uq_scheduled_task_version"),
        CheckConstraint("result_domain IN ('business','technical','pilotage')", name="ck_scheduled_task_domain"),
        CheckConstraint("schedule_mode IN ('manual','once','interval','cron')", name="ck_scheduled_task_schedule"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid4()))
    task_id: Mapped[str] = mapped_column(ForeignKey("scheduled_llm_task.id"), nullable=False, index=True)
    version: Mapped[int] = mapped_column(Integer, nullable=False)
    title: Mapped[str] = mapped_column(String(160), nullable=False)
    short_description: Mapped[str] = mapped_column(String(500), default="", nullable=False)
    long_description: Mapped[str] = mapped_column(Text, default="", nullable=False)
    system_prompt: Mapped[str] = mapped_column(Text, nullable=False)
    user_prompt: Mapped[str] = mapped_column(Text, nullable=False)
    result_domain: Mapped[str] = mapped_column(String(16), nullable=False)
    llm_profile_version_id: Mapped[str] = mapped_column(ForeignKey("llm_profile_version.id"), nullable=False)
    parameters: Mapped[dict] = mapped_column(JSON, default=dict, nullable=False)
    strict_json: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    schedule_mode: Mapped[str] = mapped_column(String(16), default="manual", nullable=False)
    schedule_expression: Mapped[str | None] = mapped_column(String(160))
    timezone_name: Mapped[str | None] = mapped_column(String(80))
    subject_ids: Mapped[list] = mapped_column(JSON, default=list, nullable=False)
    created_by: Mapped[str] = mapped_column(ForeignKey("app_user.id"), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)


class ScheduledLLMSubject(Base):
    __tablename__ = "scheduled_llm_subject"
    __table_args__ = (
        CheckConstraint("status IN ('active','inactive','archived','deleted')", name="ck_scheduled_subject_status"),
        UniqueConstraint("project_id", "id", name="uq_scheduled_subject_project_id"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid4()))
    project_id: Mapped[str] = mapped_column(ForeignKey("project.id"), nullable=False, index=True)
    title: Mapped[str] = mapped_column(String(120), nullable=False)
    description: Mapped[str] = mapped_column(String(1000), default="", nullable=False)
    status: Mapped[str] = mapped_column(String(16), default="active", nullable=False)
    revision: Mapped[int] = mapped_column(Integer, default=1, nullable=False)
    created_by: Mapped[str] = mapped_column(ForeignKey("app_user.id"), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)


class ScheduledLLMVersionSubject(Base):
    __tablename__ = "scheduled_llm_version_subject"
    version_id: Mapped[str] = mapped_column(ForeignKey("scheduled_llm_task_version.id"), primary_key=True)
    subject_id: Mapped[str] = mapped_column(ForeignKey("scheduled_llm_subject.id"), primary_key=True)


class ScheduledLLMSubjectRevision(Base):
    __tablename__ = "scheduled_llm_subject_revision"
    __table_args__ = (UniqueConstraint("subject_id", "revision", name="uq_scheduled_subject_revision"),)

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid4()))
    subject_id: Mapped[str] = mapped_column(ForeignKey("scheduled_llm_subject.id"), nullable=False, index=True)
    revision: Mapped[int] = mapped_column(Integer, nullable=False)
    title: Mapped[str] = mapped_column(String(120), nullable=False)
    description: Mapped[str] = mapped_column(String(1000), nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False)
    actor_id: Mapped[str] = mapped_column(ForeignKey("app_user.id"), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)


class ScheduledLLMOccurrence(Base):
    __tablename__ = "scheduled_llm_occurrence"
    __table_args__ = (
        UniqueConstraint("task_id", "task_version_id", "scheduled_at", name="uq_coffy_scheduled_occurrence"),
        UniqueConstraint("task_id", "request_key", name="uq_coffy_manual_request"),
        CheckConstraint("origin IN ('scheduled','manual')", name="ck_coffy_occurrence_origin"),
        CheckConstraint("status IN ('reserved','executed','skipped')", name="ck_coffy_occurrence_status"),
        CheckConstraint("coalesced_count >= 0", name="ck_coffy_occurrence_count"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid4()))
    project_id: Mapped[str] = mapped_column(ForeignKey("project.id"), nullable=False, index=True)
    task_id: Mapped[str] = mapped_column(ForeignKey("scheduled_llm_task.id"), nullable=False, index=True)
    task_version_id: Mapped[str] = mapped_column(ForeignKey("scheduled_llm_task_version.id"), nullable=False)
    scheduled_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    origin: Mapped[str] = mapped_column(String(12), nullable=False)
    request_key: Mapped[str | None] = mapped_column(String(36))
    status: Mapped[str] = mapped_column(String(12), nullable=False)
    skip_reason: Mapped[str | None] = mapped_column(String(40))
    coalesced_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    count_truncated: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)


class ScheduledLLMRun(Base):
    __tablename__ = "scheduled_llm_run"
    __table_args__ = (
        CheckConstraint("status IN ('pending','queued','running','retrying','success','failed','timeout','cancelled')", name="ck_coffy_run_status"),
        Index("uq_coffy_active_run", "task_id", unique=True,
              postgresql_where=text("status IN ('pending','queued','running','retrying')")),
        Index("ix_coffy_run_project_created", "project_id", "created_at"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid4()))
    occurrence_id: Mapped[str] = mapped_column(ForeignKey("scheduled_llm_occurrence.id"), unique=True, nullable=False)
    project_id: Mapped[str] = mapped_column(ForeignKey("project.id"), nullable=False)
    task_id: Mapped[str] = mapped_column(ForeignKey("scheduled_llm_task.id"), nullable=False)
    task_version_id: Mapped[str] = mapped_column(ForeignKey("scheduled_llm_task_version.id"), nullable=False)
    llm_profile_version_id: Mapped[str] = mapped_column(ForeignKey("llm_profile_version.id"), nullable=False)
    requested_by: Mapped[str | None] = mapped_column(ForeignKey("app_user.id"))
    status: Mapped[str] = mapped_column(String(12), default="pending", nullable=False)
    error_code: Mapped[str | None] = mapped_column(String(40))
    parameters: Mapped[dict] = mapped_column(JSON, nullable=False)
    pricing_snapshot: Mapped[dict | None] = mapped_column(JSON)
    raw_output: Mapped[str | None] = mapped_column(Text)
    result_current_version: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    input_tokens: Mapped[int | None] = mapped_column(Integer)
    output_tokens: Mapped[int | None] = mapped_column(Integer)
    estimated_cost: Mapped[float | None] = mapped_column(Numeric(12, 6))
    cost_currency: Mapped[str | None] = mapped_column(String(3))
    pricing_source: Mapped[str | None] = mapped_column(String(120))
    pricing_date: Mapped[date | None] = mapped_column(Date)
    dispatch_attempts: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    last_dispatched_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class ScheduledLLMResultVersion(Base):
    __tablename__ = "scheduled_llm_result_version"
    __table_args__ = (
        UniqueConstraint("run_id", "version", name="uq_coffy_result_version"),
        CheckConstraint("version > 0", name="ck_coffy_result_version_positive"),
        CheckConstraint("status IN ('Brouillon IA','À revoir','Validé','Rejeté')", name="ck_coffy_result_status"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid4()))
    run_id: Mapped[str] = mapped_column(ForeignKey("scheduled_llm_run.id"), nullable=False, index=True)
    version: Mapped[int] = mapped_column(Integer, nullable=False)
    parent_version_id: Mapped[str | None] = mapped_column(ForeignKey("scheduled_llm_result_version.id"))
    content: Mapped[str] = mapped_column(Text, nullable=False)
    status: Mapped[str] = mapped_column(String(24), nullable=False)
    actor_id: Mapped[str | None] = mapped_column(ForeignKey("app_user.id"))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)


class ScheduledLLMResultComment(Base):
    __tablename__ = "scheduled_llm_result_comment"
    __table_args__ = (CheckConstraint("status IN ('open','resolved')", name="ck_coffy_comment_status"),)

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid4()))
    result_version_id: Mapped[str] = mapped_column(ForeignKey("scheduled_llm_result_version.id"), nullable=False, index=True)
    author_id: Mapped[str] = mapped_column(ForeignKey("app_user.id"), nullable=False)
    body: Mapped[str] = mapped_column(Text, nullable=False)
    status: Mapped[str] = mapped_column(String(16), default="open", nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)
    resolved_by: Mapped[str | None] = mapped_column(ForeignKey("app_user.id"))
    resolved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class ScheduledLLMFeedback(Base):
    __tablename__ = "scheduled_llm_feedback"
    __table_args__ = (
        UniqueConstraint("result_version_id", "author_id", name="uq_coffy_feedback_author_version"),
        CheckConstraint("score BETWEEN 1 AND 5", name="ck_coffy_feedback_score"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid4()))
    result_version_id: Mapped[str] = mapped_column(ForeignKey("scheduled_llm_result_version.id"), nullable=False, index=True)
    author_id: Mapped[str] = mapped_column(ForeignKey("app_user.id"), nullable=False)
    score: Mapped[int] = mapped_column(Integer, nullable=False)
    note: Mapped[str] = mapped_column(String(2000), default="", nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)
