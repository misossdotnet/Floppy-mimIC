"""Mémoire persistante : versions immuables et référence active distincte."""
from datetime import datetime
from uuid import uuid4

from sqlalchemy import CheckConstraint, DateTime, Float, ForeignKey, ForeignKeyConstraint, Index, Integer, JSON, String, UniqueConstraint, text
from sqlalchemy.orm import Mapped, mapped_column

from app.identity_models import Base, utcnow


class ProjectMemory(Base):
    __tablename__ = 'project_memory'
    __table_args__ = (
        CheckConstraint("scope IN ('project','global')", name='ck_memory_scope'),
        CheckConstraint("(scope = 'project') = (project_id IS NOT NULL)", name='ck_memory_project'),
        CheckConstraint("kind IN ('convention','architecture_decision','business_constraint','result')", name='ck_memory_kind'),
        CheckConstraint("scope <> 'global' OR kind = 'convention'", name='ck_memory_global_kind'),
        CheckConstraint('current_version_number >= 0', name='ck_memory_current'),
        ForeignKeyConstraint(['reference_version_id','id'], ['project_memory_version.id','project_memory_version.memory_id'],
                             name='fk_memory_reference', use_alter=True),
    )
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid4()))
    scope: Mapped[str] = mapped_column(String(16), nullable=False)
    project_id: Mapped[str | None] = mapped_column(ForeignKey('project.id'), index=True)
    kind: Mapped[str] = mapped_column(String(32), nullable=False)
    current_version_number: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    reference_version_id: Mapped[str | None] = mapped_column(String(36))
    created_by: Mapped[str] = mapped_column(ForeignKey('app_user.id'), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)


class ProjectMemoryVersion(Base):
    __tablename__ = 'project_memory_version'
    __table_args__ = (
        UniqueConstraint('memory_id','version_number', name='uq_memory_version'),
        UniqueConstraint('id','memory_id', name='uq_memory_version_scope'),
        ForeignKeyConstraint(['parent_version_id','memory_id'], ['project_memory_version.id','project_memory_version.memory_id'], name='fk_memory_parent'),
        CheckConstraint('version_number > 0', name='ck_memory_version_number'),
        CheckConstraint("status IN ('Brouillon IA','À revoir','Validé','Rejeté','Obsolète')", name='ck_memory_version_status'),
        CheckConstraint('(actor_id IS NOT NULL) <> (session_id IS NOT NULL)', name='ck_memory_author'),
        CheckConstraint('confidence IS NULL OR (confidence >= 0 AND confidence <= 1)', name='ck_memory_confidence'),
    )
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid4()))
    memory_id: Mapped[str] = mapped_column(ForeignKey('project_memory.id'), nullable=False, index=True)
    version_number: Mapped[int] = mapped_column(Integer, nullable=False)
    parent_version_id: Mapped[str | None] = mapped_column(String(36))
    title: Mapped[str] = mapped_column(String(180), nullable=False)
    content: Mapped[str] = mapped_column(String(4000), nullable=False)
    source_note: Mapped[str] = mapped_column(String(500), nullable=False)
    source_refs: Mapped[list[dict]] = mapped_column(JSON, nullable=False)
    confidence: Mapped[float | None] = mapped_column(Float)
    status: Mapped[str] = mapped_column(String(24), nullable=False)
    action: Mapped[str] = mapped_column(String(16), nullable=False)
    change_note: Mapped[str] = mapped_column(String(1000), nullable=False)
    actor_id: Mapped[str | None] = mapped_column(ForeignKey('app_user.id'))
    session_id: Mapped[str | None] = mapped_column(ForeignKey('agent_session.id'))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)


class ProjectMemoryGrant(Base):
    __tablename__ = 'project_memory_grant'
    __table_args__ = (
        ForeignKeyConstraint(['version_id','memory_id'], ['project_memory_version.id','project_memory_version.memory_id'], name='fk_memory_grant_version'),
        Index('uq_memory_grant_active','project_id','memory_id', unique=True, postgresql_where=text('revoked_at IS NULL')),
        CheckConstraint('(revoked_at IS NULL) = (revoked_by IS NULL)', name='ck_memory_grant_revoke'),
    )
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid4()))
    project_id: Mapped[str] = mapped_column(ForeignKey('project.id'), nullable=False, index=True)
    memory_id: Mapped[str] = mapped_column(ForeignKey('project_memory.id'), nullable=False)
    version_id: Mapped[str] = mapped_column(String(36), nullable=False)
    created_by: Mapped[str] = mapped_column(ForeignKey('app_user.id'), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)
    revoked_by: Mapped[str | None] = mapped_column(ForeignKey('app_user.id'))
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
