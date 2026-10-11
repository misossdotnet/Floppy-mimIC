"""Coordination technique persistante ; aucun statut de validation humaine."""
from datetime import datetime
from uuid import uuid4

from sqlalchemy import CheckConstraint, DateTime, ForeignKey, ForeignKeyConstraint, Index, JSON, String, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from app.identity_models import Base, utcnow


class CoordinationChannel(Base):
    __tablename__ = "coordination_channel"
    __table_args__ = (
        UniqueConstraint("id", "project_id", "plan_version_id", "team_id", name="uq_channel_scope"),
        CheckConstraint("kind IN ('direct','broadcast','thread')", name="ck_channel_kind"),
        ForeignKeyConstraint(["task_id", "plan_version_id", "project_id", "team_id"],
            ["agent_task.id", "agent_task.plan_version_id", "agent_task.project_id", "agent_task.team_id"], name="fk_channel_task_scope"),
        Index("ix_channel_project_created", "project_id", "created_at"),
        {"schema": "conversation"},
    )
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid4()))
    project_id: Mapped[str] = mapped_column(ForeignKey("project.id"), nullable=False)
    plan_version_id: Mapped[str] = mapped_column(ForeignKey("agent_plan_version.id"), nullable=False)
    team_id: Mapped[str] = mapped_column(ForeignKey("agent_team.id"), nullable=False)
    task_id: Mapped[str] = mapped_column(String(36), nullable=False)
    kind: Mapped[str] = mapped_column(String(16), nullable=False)
    participant_profile_ids: Mapped[list[str]] = mapped_column(JSON, nullable=False)
    attachment: Mapped[dict | None] = mapped_column(JSON)
    created_by_session_id: Mapped[str] = mapped_column(ForeignKey("agent_session.id"), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)


class AgentMessage(Base):
    __tablename__ = "agent_message"
    __table_args__ = (
        UniqueConstraint("sender_session_id", "request_key", name="uq_message_request"),
        UniqueConstraint("id", "channel_id", name="uq_message_channel"),
        UniqueConstraint("id", "project_id", name="uq_message_project"),
        ForeignKeyConstraint(["channel_id", "project_id", "plan_version_id", "team_id"],
            ["conversation.coordination_channel.id", "conversation.coordination_channel.project_id",
             "conversation.coordination_channel.plan_version_id", "conversation.coordination_channel.team_id"], name="fk_message_channel_scope"),
        ForeignKeyConstraint(["reply_to_id", "channel_id"],
            ["conversation.agent_message.id", "conversation.agent_message.channel_id"], name="fk_message_reply_channel"),
        ForeignKeyConstraint(["target_task_id", "plan_version_id", "project_id", "team_id"],
            ["agent_task.id", "agent_task.plan_version_id", "agent_task.project_id", "agent_task.team_id"], name="fk_handoff_task_scope"),
        CheckConstraint("kind IN ('message','handoff')", name="ck_message_kind"),
        CheckConstraint("(kind = 'handoff') = (target_task_id IS NOT NULL)", name="ck_message_handoff"),
        CheckConstraint("priority IN ('P0','P1','P2','P3')", name="ck_message_priority"),
        CheckConstraint("expires_at > created_at", name="ck_message_expiry"),
        Index("ix_message_channel_created", "channel_id", "created_at", "id"),
        {"schema": "conversation"},
    )
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid4()))
    project_id: Mapped[str] = mapped_column(ForeignKey("project.id"), nullable=False)
    plan_version_id: Mapped[str] = mapped_column(ForeignKey("agent_plan_version.id"), nullable=False)
    team_id: Mapped[str] = mapped_column(ForeignKey("agent_team.id"), nullable=False)
    channel_id: Mapped[str] = mapped_column(String(36), nullable=False)
    sender_session_id: Mapped[str] = mapped_column(ForeignKey("agent_session.id"), nullable=False)
    sender_profile_id: Mapped[str] = mapped_column(ForeignKey("skill_profile.id"), nullable=False)
    task_run_id: Mapped[str] = mapped_column(ForeignKey("agent_task_run.id"), nullable=False)
    request_key: Mapped[str] = mapped_column(String(36), nullable=False)
    request_fingerprint: Mapped[str] = mapped_column(String(64), nullable=False)
    reply_to_id: Mapped[str | None] = mapped_column(String(36))
    kind: Mapped[str] = mapped_column(String(16), nullable=False)
    target_task_id: Mapped[str | None] = mapped_column(String(36))
    summary: Mapped[str] = mapped_column(String(1000), nullable=False)
    content: Mapped[str] = mapped_column(String(4000), nullable=False)
    artifact_refs: Mapped[list[dict]] = mapped_column(JSON, nullable=False)
    priority: Mapped[str] = mapped_column(String(2), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class AgentMessageDelivery(Base):
    __tablename__ = "agent_message_delivery"
    __table_args__ = (
        UniqueConstraint("message_id", "recipient_profile_id", name="uq_delivery_recipient"),
        ForeignKeyConstraint(["message_id", "project_id"],
            ["conversation.agent_message.id", "conversation.agent_message.project_id"], name="fk_delivery_project"),
        CheckConstraint("status IN ('pending','delivered','acknowledged','expired')", name="ck_delivery_status"),
        CheckConstraint("(delivered_at IS NULL) = (delivered_by_session_id IS NULL)", name="ck_delivery_actor"),
        CheckConstraint("(acknowledged_at IS NULL) = (acknowledged_by_session_id IS NULL)", name="ck_ack_actor"),
        CheckConstraint("status <> 'pending' OR delivered_at IS NULL", name="ck_delivery_pending"),
        CheckConstraint("status NOT IN ('delivered','acknowledged') OR delivered_at IS NOT NULL", name="ck_delivery_received"),
        CheckConstraint("(status = 'acknowledged') = (acknowledged_at IS NOT NULL)", name="ck_delivery_ack"),
        Index("ix_delivery_inbox", "project_id", "recipient_profile_id", "status"),
        {"schema": "conversation"},
    )
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid4()))
    project_id: Mapped[str] = mapped_column(ForeignKey("project.id"), nullable=False)
    message_id: Mapped[str] = mapped_column(String(36), nullable=False)
    recipient_profile_id: Mapped[str] = mapped_column(ForeignKey("skill_profile.id"), nullable=False)
    status: Mapped[str] = mapped_column(String(16), default="pending", nullable=False)
    delivered_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    delivered_by_session_id: Mapped[str | None] = mapped_column(ForeignKey("agent_session.id"))
    acknowledged_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    acknowledged_by_session_id: Mapped[str | None] = mapped_column(ForeignKey("agent_session.id"))
