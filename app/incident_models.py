"""Incidents versionnés et notifications internes ; états techniques distincts de la revue."""
from datetime import datetime
from uuid import uuid4

from sqlalchemy import CheckConstraint, DateTime, ForeignKey, ForeignKeyConstraint, Integer, JSON, String, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from app.identity_models import Base, utcnow


class TechnicalIncident(Base):
    __tablename__ = 'technical_incident'
    __table_args__ = (
        UniqueConstraint('created_by', 'request_key', name='uq_incident_request'),
        UniqueConstraint('id', 'project_id', name='uq_incident_project'),
        CheckConstraint("status IN ('open','acknowledged','escalated','resolved')", name='ck_incident_status'),
        CheckConstraint("priority IN ('P0','P1','P2','P3')", name='ck_incident_priority'),
        ForeignKeyConstraint(['team_id', 'responsible_profile_id'], ['agent_team_skill.team_id', 'agent_team_skill.skill_profile_id'], name='fk_incident_team_profile'),
        CheckConstraint("content_status IN ('Brouillon IA','À revoir')", name='ck_incident_content_status'),
        CheckConstraint('revision > 0', name='ck_incident_revision'),
        CheckConstraint("(status = 'resolved') = (resolved_at IS NOT NULL)", name='ck_incident_resolved'),
        {'schema': 'conversation'},
    )
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid4()))
    project_id: Mapped[str] = mapped_column(ForeignKey('project.id'), nullable=False, index=True)
    team_id: Mapped[str] = mapped_column(ForeignKey('agent_team.id'), nullable=False)
    task_id: Mapped[str | None] = mapped_column(ForeignKey('agent_task.id'))
    responsible_profile_id: Mapped[str] = mapped_column(ForeignKey('skill_profile.id'), nullable=False)
    title: Mapped[str] = mapped_column(String(180), nullable=False)
    description: Mapped[str] = mapped_column(String(4000), nullable=False)
    content_status: Mapped[str] = mapped_column(String(24), nullable=False)
    priority: Mapped[str] = mapped_column(String(2), nullable=False)
    status: Mapped[str] = mapped_column(String(16), default='open', nullable=False)
    revision: Mapped[int] = mapped_column(Integer, default=1, nullable=False)
    request_key: Mapped[str] = mapped_column(String(36), nullable=False)
    request_fingerprint: Mapped[str] = mapped_column(String(64), nullable=False)
    created_by: Mapped[str] = mapped_column(ForeignKey('app_user.id'), nullable=False)
    created_by_session_id: Mapped[str | None] = mapped_column(ForeignKey('agent_session.id'))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)
    resolved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class IncidentEvent(Base):
    __tablename__ = 'incident_event'
    __table_args__ = (
        UniqueConstraint('incident_id', 'revision', name='uq_incident_event_revision'),
        ForeignKeyConstraint(['incident_id', 'project_id'], ['conversation.technical_incident.id', 'conversation.technical_incident.project_id'], name='fk_incident_event_project'),
        {'schema': 'conversation'},
    )
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid4()))
    incident_id: Mapped[str] = mapped_column(String(36), nullable=False, index=True)
    project_id: Mapped[str] = mapped_column(ForeignKey('project.id'), nullable=False)
    revision: Mapped[int] = mapped_column(Integer, nullable=False)
    action: Mapped[str] = mapped_column(String(20), nullable=False)
    actor_id: Mapped[str] = mapped_column(ForeignKey('app_user.id'), nullable=False)
    session_id: Mapped[str | None] = mapped_column(ForeignKey('agent_session.id'))
    fingerprint: Mapped[str] = mapped_column(String(64), nullable=False)
    reason: Mapped[str] = mapped_column(String(1000), nullable=False)
    source_refs: Mapped[list[dict]] = mapped_column(JSON, nullable=False)
    snapshot: Mapped[dict] = mapped_column(JSON, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)


class IncidentNotification(Base):
    __tablename__ = 'incident_notification'
    __table_args__ = (
        ForeignKeyConstraint(['incident_id', 'project_id'], ['conversation.technical_incident.id', 'conversation.technical_incident.project_id'], name='fk_incident_notification_project'),
        CheckConstraint('(recipient_user_id IS NOT NULL) <> (recipient_profile_id IS NOT NULL)', name='ck_incident_notification_recipient'),
        CheckConstraint("status IN ('pending','acknowledged','expired')", name='ck_incident_notification_status'),
        CheckConstraint("(status = 'acknowledged') = (acknowledged_at IS NOT NULL)", name='ck_incident_notification_ack'),
        CheckConstraint('expires_at > created_at', name='ck_incident_notification_expiry'),
        {'schema': 'conversation'},
    )
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid4()))
    incident_id: Mapped[str] = mapped_column(String(36), nullable=False)
    project_id: Mapped[str] = mapped_column(ForeignKey('project.id'), nullable=False, index=True)
    event_id: Mapped[str] = mapped_column(ForeignKey('conversation.incident_event.id'), nullable=False)
    recipient_user_id: Mapped[str | None] = mapped_column(ForeignKey('app_user.id'))
    recipient_profile_id: Mapped[str | None] = mapped_column(ForeignKey('skill_profile.id'))
    priority: Mapped[str] = mapped_column(String(2), nullable=False)
    status: Mapped[str] = mapped_column(String(16), default='pending', nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    acknowledged_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    acknowledged_by: Mapped[str | None] = mapped_column(ForeignKey('app_user.id'))
    acknowledged_by_session_id: Mapped[str | None] = mapped_column(ForeignKey('agent_session.id'))
