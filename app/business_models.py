"""Sources, analyses et propositions de contexte du premier lot métier."""

from datetime import date, datetime
from uuid import uuid4

from sqlalchemy import Boolean, CheckConstraint, Date, DateTime, ForeignKey, ForeignKeyConstraint, Index, Integer, JSON, Numeric, String, Text, UniqueConstraint, text
from sqlalchemy.orm import Mapped, mapped_column

from app.identity_models import Base, utcnow


class SourceDocument(Base):
    __tablename__ = "source_document"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid4()))
    project_id: Mapped[str] = mapped_column(ForeignKey("project.id"), index=True, nullable=False)
    created_by: Mapped[str] = mapped_column(ForeignKey("app_user.id"), nullable=False)
    name: Mapped[str] = mapped_column(String(180), nullable=False)
    kind: Mapped[str] = mapped_column(String(12), nullable=False)
    mime_type: Mapped[str] = mapped_column(String(120), nullable=False)
    object_key: Mapped[str | None] = mapped_column(String(130), unique=True)
    sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    size_bytes: Mapped[int] = mapped_column(Integer, nullable=False)
    # Le besoin saisi est borné ; les binaires de fichier restent exclusivement en S3.
    direct_text: Mapped[str | None] = mapped_column(Text)
    extracted_text: Mapped[str | None] = mapped_column(Text)
    status: Mapped[str] = mapped_column(String(24), default="uploaded", nullable=False)
    error_code: Mapped[str | None] = mapped_column(String(40))
    attempt: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow, nullable=False)


class AnalysisRun(Base):
    __tablename__ = "analysis_run"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid4()))
    project_id: Mapped[str] = mapped_column(ForeignKey("project.id"), index=True, nullable=False)
    source_id: Mapped[str] = mapped_column(ForeignKey("source_document.id"), index=True, nullable=False)
    requested_by: Mapped[str] = mapped_column(ForeignKey("app_user.id"), nullable=False)
    llm_profile_version_id: Mapped[str] = mapped_column(ForeignKey("llm_profile_version.id"), nullable=False)
    status: Mapped[str] = mapped_column(String(24), default="queued", nullable=False)
    prompt_version: Mapped[str] = mapped_column(String(24), default="context-v1", nullable=False)
    prompt_template: Mapped[str | None] = mapped_column(Text)
    context_snapshot: Mapped[dict | None] = mapped_column(JSON)
    parameters: Mapped[dict | None] = mapped_column(JSON)
    pricing_snapshot: Mapped[dict | None] = mapped_column(JSON)
    estimated_cost: Mapped[float | None] = mapped_column(Numeric(12, 6))
    cost_currency: Mapped[str | None] = mapped_column(String(3))
    pricing_source: Mapped[str | None] = mapped_column(String(120))
    pricing_date: Mapped[date | None] = mapped_column(Date)
    input_tokens: Mapped[int | None] = mapped_column(Integer)
    output_tokens: Mapped[int | None] = mapped_column(Integer)
    error_code: Mapped[str | None] = mapped_column(String(40))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class ContextItem(Base):
    __tablename__ = "context_item"
    __table_args__ = (UniqueConstraint("id", "project_id", name="uq_context_item_project"),)

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid4()))
    project_id: Mapped[str] = mapped_column(ForeignKey("project.id"), index=True, nullable=False)
    source_id: Mapped[str | None] = mapped_column(ForeignKey("source_document.id"), index=True)
    analysis_run_id: Mapped[str | None] = mapped_column(ForeignKey("analysis_run.id"))
    type: Mapped[str] = mapped_column(String(32), nullable=False)
    origin: Mapped[str] = mapped_column(String(12), nullable=False)
    status: Mapped[str] = mapped_column(String(24), nullable=False)
    current_version: Mapped[int] = mapped_column(Integer, default=1, nullable=False)
    title: Mapped[str] = mapped_column(String(180), nullable=False)
    content: Mapped[str] = mapped_column(Text, nullable=False)
    created_by: Mapped[str | None] = mapped_column(ForeignKey("app_user.id"))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)


class ContextItemVersion(Base):
    __tablename__ = "context_item_version"
    __table_args__ = (UniqueConstraint("item_id", "version", name="uq_context_item_version"),)

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid4()))
    item_id: Mapped[str] = mapped_column(ForeignKey("context_item.id"), index=True, nullable=False)
    version: Mapped[int] = mapped_column(Integer, nullable=False)
    title: Mapped[str] = mapped_column(String(180), nullable=False)
    content: Mapped[str] = mapped_column(Text, nullable=False)
    status: Mapped[str] = mapped_column(String(24), nullable=False)
    actor_id: Mapped[str | None] = mapped_column(ForeignKey("app_user.id"))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)


class ContextRelation(Base):
    __tablename__ = "context_relation"
    __table_args__ = (
        ForeignKeyConstraint(["source_id", "project_id"], ["context_item.id", "context_item.project_id"], name="fk_context_relation_source_project"),
        ForeignKeyConstraint(["target_id", "project_id"], ["context_item.id", "context_item.project_id"], name="fk_context_relation_target_project"),
        CheckConstraint("source_id <> target_id", name="ck_context_relation_distinct"),
        Index("uq_context_relation_active", "source_id", "target_id", "type", unique=True,
              postgresql_where=text("is_active")),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid4()))
    project_id: Mapped[str] = mapped_column(ForeignKey("project.id"), nullable=False, index=True)
    source_id: Mapped[str] = mapped_column(String(36), nullable=False, index=True)
    target_id: Mapped[str] = mapped_column(String(36), nullable=False, index=True)
    type: Mapped[str] = mapped_column(String(32), nullable=False)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    created_by: Mapped[str] = mapped_column(ForeignKey("app_user.id"), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)
    removed_by: Mapped[str | None] = mapped_column(ForeignKey("app_user.id"))
    removed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class ContextComment(Base):
    __tablename__ = "context_comment"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid4()))
    item_version_id: Mapped[str] = mapped_column(ForeignKey("context_item_version.id"), nullable=False, index=True)
    author_id: Mapped[str] = mapped_column(ForeignKey("app_user.id"), nullable=False)
    body: Mapped[str] = mapped_column(Text, nullable=False)
    status: Mapped[str] = mapped_column(String(16), default="open", nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)
    resolved_by: Mapped[str | None] = mapped_column(ForeignKey("app_user.id"))
    resolved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class Deliverable(Base):
    __tablename__ = "deliverable"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid4()))
    project_id: Mapped[str] = mapped_column(ForeignKey("project.id"), nullable=False, index=True)
    type: Mapped[str] = mapped_column(String(40), nullable=False)
    title: Mapped[str] = mapped_column(String(180), nullable=False)
    current_version: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    created_by: Mapped[str] = mapped_column(ForeignKey("app_user.id"), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)


class GenerationRun(Base):
    __tablename__ = "generation_run"
    __table_args__ = (UniqueConstraint("deliverable_id", "attempt_number", name="uq_generation_attempt"),)

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid4()))
    project_id: Mapped[str] = mapped_column(ForeignKey("project.id"), nullable=False, index=True)
    deliverable_id: Mapped[str] = mapped_column(ForeignKey("deliverable.id"), nullable=False, index=True)
    requested_by: Mapped[str] = mapped_column(ForeignKey("app_user.id"), nullable=False)
    llm_profile_version_id: Mapped[str] = mapped_column(ForeignKey("llm_profile_version.id"), nullable=False)
    attempt_number: Mapped[int] = mapped_column(Integer, nullable=False)
    status: Mapped[str] = mapped_column(String(24), nullable=False)
    prompt_version: Mapped[str] = mapped_column(String(24), nullable=False)
    prompt_template: Mapped[str] = mapped_column(Text, nullable=False)
    context_snapshot: Mapped[list] = mapped_column(JSON, nullable=False)
    parameters: Mapped[dict] = mapped_column(JSON, nullable=False)
    pricing_snapshot: Mapped[dict | None] = mapped_column(JSON)
    parent_version_id: Mapped[str | None] = mapped_column(
        ForeignKey("deliverable_version.id", name="generation_run_parent_version_id_fkey",
                   use_alter=True), index=True)
    reason: Mapped[str | None] = mapped_column(Text)
    prompt_adjustment: Mapped[str | None] = mapped_column(Text)
    input_tokens: Mapped[int | None] = mapped_column(Integer)
    output_tokens: Mapped[int | None] = mapped_column(Integer)
    estimated_cost: Mapped[float | None] = mapped_column(Numeric(12, 6))
    cost_currency: Mapped[str | None] = mapped_column(String(3))
    pricing_source: Mapped[str | None] = mapped_column(String(120))
    pricing_date: Mapped[date | None] = mapped_column(Date)
    error_code: Mapped[str | None] = mapped_column(String(40))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class DeliverableVersion(Base):
    __tablename__ = "deliverable_version"
    __table_args__ = (UniqueConstraint("deliverable_id", "version", name="uq_deliverable_version"),)

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid4()))
    deliverable_id: Mapped[str] = mapped_column(ForeignKey("deliverable.id"), nullable=False, index=True)
    version: Mapped[int] = mapped_column(Integer, nullable=False)
    title: Mapped[str] = mapped_column(String(180), nullable=False)
    content: Mapped[str] = mapped_column(Text, nullable=False)
    status: Mapped[str] = mapped_column(String(24), nullable=False)
    generation_run_id: Mapped[str | None] = mapped_column(ForeignKey("generation_run.id"), index=True)
    parent_version_id: Mapped[str | None] = mapped_column(ForeignKey("deliverable_version.id"))
    actor_id: Mapped[str | None] = mapped_column(ForeignKey("app_user.id"))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)


class DiffRun(Base):
    __tablename__ = "diff_run"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid4()))
    project_id: Mapped[str] = mapped_column(ForeignKey("project.id"), nullable=False, index=True)
    left_version_id: Mapped[str] = mapped_column(ForeignKey("deliverable_version.id"), nullable=False)
    right_version_id: Mapped[str] = mapped_column(ForeignKey("deliverable_version.id"), nullable=False)
    requested_by: Mapped[str] = mapped_column(ForeignKey("app_user.id"), nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="queued")
    result: Mapped[str | None] = mapped_column(Text)
    error_code: Mapped[str | None] = mapped_column(String(40))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class DeliverableComment(Base):
    __tablename__ = "deliverable_comment"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid4()))
    version_id: Mapped[str] = mapped_column(ForeignKey("deliverable_version.id"), nullable=False, index=True)
    author_id: Mapped[str] = mapped_column(ForeignKey("app_user.id"), nullable=False)
    body: Mapped[str] = mapped_column(Text, nullable=False)
    status: Mapped[str] = mapped_column(String(16), default="open", nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)
    resolved_by: Mapped[str | None] = mapped_column(ForeignKey("app_user.id"))
    resolved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class PublicationRecord(Base):
    __tablename__ = "publication_record"
    __table_args__ = (Index("uq_publication_active", "deliverable_id", unique=True,
                             postgresql_where=text("active")),)

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid4()))
    deliverable_id: Mapped[str] = mapped_column(ForeignKey("deliverable.id"), nullable=False, index=True)
    version_id: Mapped[str] = mapped_column(ForeignKey("deliverable_version.id"), nullable=False, unique=True)
    active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    published_by: Mapped[str] = mapped_column(ForeignKey("app_user.id"), nullable=False)
    published_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)
    replaced_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class ExportArtifact(Base):
    __tablename__ = "export_artifact"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid4()))
    publication_id: Mapped[str] = mapped_column(ForeignKey("publication_record.id"), nullable=False, index=True)
    format: Mapped[str] = mapped_column(String(12), nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="queued")
    object_key: Mapped[str | None] = mapped_column(String(130), unique=True)
    sha256: Mapped[str | None] = mapped_column(String(64))
    size_bytes: Mapped[int | None] = mapped_column(Integer)
    error_code: Mapped[str | None] = mapped_column(String(40))
    requested_by: Mapped[str] = mapped_column(ForeignKey("app_user.id"), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
