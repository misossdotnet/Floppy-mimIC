"""Dépôts, preuves Git, revue et autorisations d'effets ; sans validation documentaire."""
from datetime import datetime

from sqlalchemy import Boolean, CheckConstraint, DateTime, ForeignKey, ForeignKeyConstraint, Integer, JSON, String, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from uuid import uuid4

from app.identity_models import Base, utcnow

def new_id():
    return str(uuid4())


class Repository(Base):
    __tablename__ = 'repository'
    __table_args__ = (UniqueConstraint('catalog_key'), UniqueConstraint('id', 'project_id'),)
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    project_id: Mapped[str] = mapped_column(ForeignKey('project.id'), index=True)
    catalog_key: Mapped[str] = mapped_column(String(80))
    physical_identity: Mapped[str] = mapped_column(String(64), unique=True)
    policy_fingerprint: Mapped[str] = mapped_column(String(64))
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    created_by: Mapped[str] = mapped_column(ForeignKey('app_user.id'))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class GitWorktree(Base):
    __tablename__ = 'git_worktree'
    __table_args__ = (UniqueConstraint('repository_id', 'branch'), UniqueConstraint('id', 'project_id'),
        ForeignKeyConstraint(['repository_id', 'project_id'], ['repository.id', 'repository.project_id']),
        CheckConstraint("status IN ('creating','ready','failed','removed','uncertain')"),)
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    project_id: Mapped[str] = mapped_column(ForeignKey('project.id'), index=True)
    repository_id: Mapped[str] = mapped_column(String(36))
    task_id: Mapped[str] = mapped_column(ForeignKey('agent_task.id'), index=True)
    branch: Mapped[str] = mapped_column(String(180))
    base_commit: Mapped[str] = mapped_column(String(64))
    status: Mapped[str] = mapped_column(String(16), default='creating')
    created_by: Mapped[str] = mapped_column(ForeignKey('app_user.id'))
    session_id: Mapped[str | None] = mapped_column(ForeignKey('agent_session.id'))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    removed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class CodeChange(Base):
    __tablename__ = 'code_change'
    __table_args__ = (UniqueConstraint('worktree_id', 'revision'), UniqueConstraint('id', 'project_id'),
        ForeignKeyConstraint(['worktree_id', 'project_id'], ['git_worktree.id', 'git_worktree.project_id']),
        CheckConstraint('revision > 0'),)
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    project_id: Mapped[str] = mapped_column(ForeignKey('project.id'), index=True)
    worktree_id: Mapped[str] = mapped_column(String(36))
    revision: Mapped[int] = mapped_column(Integer)
    head_commit: Mapped[str] = mapped_column(String(64))
    content_fingerprint: Mapped[str] = mapped_column(String(64))
    manifest: Mapped[dict] = mapped_column(JSON)
    created_by: Mapped[str] = mapped_column(ForeignKey('app_user.id'))
    session_id: Mapped[str | None] = mapped_column(ForeignKey('agent_session.id'))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class CodeReview(Base):
    __tablename__ = 'code_review'
    __table_args__ = (ForeignKeyConstraint(['change_id', 'project_id'], ['code_change.id', 'code_change.project_id']),
        CheckConstraint("status IN ('pending','commented','changes_requested','reviewed')"),
        CheckConstraint('revision > 0'),)
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    project_id: Mapped[str] = mapped_column(ForeignKey('project.id'), index=True)
    change_id: Mapped[str] = mapped_column(String(36), index=True)
    reviewer_id: Mapped[str] = mapped_column(ForeignKey('app_user.id'))
    session_id: Mapped[str | None] = mapped_column(ForeignKey('agent_session.id'))
    status: Mapped[str] = mapped_column(String(24), default='pending')
    revision: Mapped[int] = mapped_column(Integer, default=1)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class CodeReviewEvent(Base):
    __tablename__ = 'code_review_event'
    __table_args__ = (UniqueConstraint('review_id', 'revision'),)
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    review_id: Mapped[str] = mapped_column(ForeignKey('code_review.id'), index=True)
    revision: Mapped[int] = mapped_column(Integer)
    action: Mapped[str] = mapped_column(String(24))
    file_path: Mapped[str | None] = mapped_column(String(500))
    hunk_id: Mapped[str | None] = mapped_column(String(64))
    text: Mapped[str] = mapped_column(String(2000), default='')
    correction: Mapped[bool] = mapped_column(Boolean, default=False)
    resolves_id: Mapped[str | None] = mapped_column(ForeignKey('code_review_event.id'))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class CodeWorkflow(Base):
    __tablename__ = 'code_workflow'
    __table_args__ = (UniqueConstraint('worktree_id'), CheckConstraint('revision > 0'),
        CheckConstraint("phase IN ('analysis','tests','implementation','execution','review','approved')"),)
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    worktree_id: Mapped[str] = mapped_column(ForeignKey('git_worktree.id'))
    phase: Mapped[str] = mapped_column(String(24), default='analysis')
    revision: Mapped[int] = mapped_column(Integer, default=1)
    approved_change_id: Mapped[str | None] = mapped_column(ForeignKey('code_change.id'))
    approved_by: Mapped[str | None] = mapped_column(ForeignKey('app_user.id'))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class CodeWorkflowEvent(Base):
    __tablename__ = 'code_workflow_event'
    __table_args__ = (UniqueConstraint('workflow_id', 'revision'),)
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    workflow_id: Mapped[str] = mapped_column(ForeignKey('code_workflow.id'), index=True)
    revision: Mapped[int] = mapped_column(Integer)
    phase: Mapped[str] = mapped_column(String(24))
    reason: Mapped[str] = mapped_column(String(1000))
    actor_id: Mapped[str] = mapped_column(ForeignKey('app_user.id'))
    session_id: Mapped[str | None] = mapped_column(ForeignKey('agent_session.id'))
    change_id: Mapped[str | None] = mapped_column(ForeignKey('code_change.id'))
    test_id: Mapped[str | None] = mapped_column(String(36))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class GitOperation(Base):
    __tablename__ = 'git_operation'
    __table_args__ = (UniqueConstraint('actor_id', 'request_key'),
        ForeignKeyConstraint(['worktree_id', 'project_id'], ['git_worktree.id', 'git_worktree.project_id']),
        CheckConstraint("kind IN ('create','collect','test','merge','push','deploy','delete')"),
        CheckConstraint("status IN ('awaiting_approval','queued','running','succeeded','failed','rejected','uncertain')"),)
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    project_id: Mapped[str] = mapped_column(ForeignKey('project.id'), index=True)
    worktree_id: Mapped[str] = mapped_column(String(36), index=True)
    kind: Mapped[str] = mapped_column(String(16))
    status: Mapped[str] = mapped_column(String(24), default='queued')
    request_key: Mapped[str] = mapped_column(String(36))
    request_fingerprint: Mapped[str] = mapped_column(String(64))
    policy_fingerprint: Mapped[str] = mapped_column(String(64))
    parameters: Mapped[dict] = mapped_column(JSON, default=dict)
    result: Mapped[dict] = mapped_column(JSON, default=dict)
    actor_id: Mapped[str] = mapped_column(ForeignKey('app_user.id'))
    session_id: Mapped[str | None] = mapped_column(ForeignKey('agent_session.id'))
    capability: Mapped[str | None] = mapped_column(String(40))
    decision_reason: Mapped[str | None] = mapped_column(String(1000))
    decided_by: Mapped[str | None] = mapped_column(ForeignKey('app_user.id'))
    decided_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    ended_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class CodeTestRun(Base):
    __tablename__ = 'code_test_run'
    __table_args__ = (UniqueConstraint('operation_id'),
        ForeignKeyConstraint(['change_id', 'project_id'], ['code_change.id', 'code_change.project_id']),
        CheckConstraint("status IN ('passed','failed','error')"),)
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    project_id: Mapped[str] = mapped_column(ForeignKey('project.id'), index=True)
    change_id: Mapped[str] = mapped_column(String(36), index=True)
    operation_id: Mapped[str] = mapped_column(ForeignKey('git_operation.id'))
    command_key: Mapped[str] = mapped_column(String(80))
    status: Mapped[str] = mapped_column(String(16))
    exit_code: Mapped[int | None] = mapped_column(Integer)
    duration_ms: Mapped[int] = mapped_column(Integer)
    log: Mapped[str] = mapped_column(String(8000))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
