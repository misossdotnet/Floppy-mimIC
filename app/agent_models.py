"""Sessions techniques du relais d'agents, distinctes des comptes humains."""

from datetime import datetime
from uuid import uuid4

from sqlalchemy import Boolean, CheckConstraint, DateTime, ForeignKey, ForeignKeyConstraint, Index, Integer, JSON, String, UniqueConstraint, text
from sqlalchemy.orm import Mapped, mapped_column

from app.identity_models import Base, utcnow


class SkillProfile(Base):
    __tablename__ = "skill_profile"
    __table_args__ = (UniqueConstraint("code", "version_number", name="uq_skill_profile_version"),)

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid4()))
    code: Mapped[str] = mapped_column(String(40), nullable=False)
    version_number: Mapped[int] = mapped_column(Integer, nullable=False)
    label: Mapped[str] = mapped_column(String(80), nullable=False)
    allowed_capabilities: Mapped[list[str]] = mapped_column(JSON, nullable=False)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)


class AgentTeam(Base):
    __tablename__ = "agent_team"
    __table_args__ = (UniqueConstraint("project_id", "name", name="uq_agent_team_project_name"),)

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid4()))
    project_id: Mapped[str] = mapped_column(ForeignKey("project.id"), nullable=False, index=True)
    name: Mapped[str] = mapped_column(String(120), nullable=False)
    created_by: Mapped[str] = mapped_column(ForeignKey("app_user.id"), nullable=False)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)


class AgentTeamSkill(Base):
    __tablename__ = "agent_team_skill"
    __table_args__ = (UniqueConstraint("team_id", "skill_profile_id", name="uq_agent_team_skill"),)

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid4()))
    team_id: Mapped[str] = mapped_column(ForeignKey("agent_team.id"), nullable=False, index=True)
    skill_profile_id: Mapped[str] = mapped_column(ForeignKey("skill_profile.id"), nullable=False)


class AgentSession(Base):
    __tablename__ = "agent_session"
    __table_args__ = (
        CheckConstraint("status IN ('active', 'expired', 'revoked')", name="ck_agent_session_status"),
        Index("ix_agent_session_project_status", "project_id", "status"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid4()))
    project_id: Mapped[str] = mapped_column(ForeignKey("project.id"), nullable=False)
    team_id: Mapped[str | None] = mapped_column(ForeignKey("agent_team.id"))
    skill_profile_id: Mapped[str | None] = mapped_column(ForeignKey("skill_profile.id"))
    created_by: Mapped[str] = mapped_column(ForeignKey("app_user.id"), nullable=False)
    client_code: Mapped[str] = mapped_column(String(40), nullable=False)
    skill_code: Mapped[str] = mapped_column(String(40), nullable=False)
    capabilities: Mapped[list[str]] = mapped_column(JSON, nullable=False)
    token_hash: Mapped[str] = mapped_column(String(64), unique=True, nullable=False)
    status: Mapped[str] = mapped_column(String(16), default="active", nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)
    last_heartbeat_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    max_expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class AgentPlanVersion(Base):
    """Plan humain rattaché à une exécution d'un flux validé."""
    __tablename__ = "agent_plan_version"
    __table_args__ = (UniqueConstraint("id", "project_id", name="uq_agent_plan_project"),
        CheckConstraint("status IN ('proposed', 'review', 'approved', 'rejected')",
                                     name="ck_agent_plan_version_status"),
        CheckConstraint("max_parallel_tasks BETWEEN 1 AND 8", name="ck_agent_plan_parallel"),
        CheckConstraint("max_attempts BETWEEN 1 AND 5", name="ck_agent_plan_attempts"),
        CheckConstraint("autonomy_mode IN ('bounded', 'sensitive', 'per_action')", name="ck_agent_plan_autonomy"),)

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid4()))
    project_id: Mapped[str] = mapped_column(ForeignKey("project.id"), nullable=False, index=True)
    team_id: Mapped[str] = mapped_column(ForeignKey("agent_team.id"), nullable=False)
    flow_run_id: Mapped[str | None] = mapped_column(String(36))
    validated_flow_run_id: Mapped[str | None] = mapped_column(ForeignKey("flow_run.id"))
    status: Mapped[str] = mapped_column(String(16), default="proposed", nullable=False)
    max_parallel_tasks: Mapped[int] = mapped_column(Integer, default=4, server_default="4", nullable=False)
    max_attempts: Mapped[int] = mapped_column(Integer, default=3, server_default="3", nullable=False)
    autonomy_mode: Mapped[str] = mapped_column(String(16), default="bounded", server_default="bounded", nullable=False)
    approved_by: Mapped[str | None] = mapped_column(ForeignKey("app_user.id"))
    approved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)


class AgentTask(Base):
    __tablename__ = "agent_task"
    __table_args__ = (
        UniqueConstraint("plan_version_id", "task_key", name="uq_agent_task_plan_key"),
        UniqueConstraint("id", "plan_version_id", "project_id", name="uq_agent_task_plan_project"),
        CheckConstraint("status IN ('ready', 'claimed', 'running', 'completed', 'cancelled', 'failed')",
                        name="ck_agent_task_status"),
        UniqueConstraint("id", "plan_version_id", "project_id", "team_id", name="uq_agent_task_scope"),
        ForeignKeyConstraint(["parent_task_id", "plan_version_id", "project_id", "team_id"],
                             ["agent_task.id", "agent_task.plan_version_id", "agent_task.project_id", "agent_task.team_id"],
                             name="fk_agent_task_parent_scope"),
        CheckConstraint("depth BETWEEN 1 AND 3", name="ck_agent_task_depth"),
        CheckConstraint("(parent_task_id IS NULL AND depth = 1) OR (parent_task_id IS NOT NULL AND depth > 1)",
                        name="ck_agent_task_parent_depth"),
        CheckConstraint("parent_task_id IS NULL OR parent_task_id <> id", name="ck_agent_task_not_self"),
        CheckConstraint("priority IN ('P0', 'P1', 'P2', 'P3')", name="ck_agent_task_priority"),
        CheckConstraint("progress_percent BETWEEN 0 AND 100", name="ck_agent_task_progress"),
        CheckConstraint("status <> 'completed' OR progress_percent = 100", name="ck_agent_task_completed_progress"),
        CheckConstraint("attempt_count >= 0", name="ck_agent_task_attempt_count"),
        CheckConstraint("action_class IN ('standard', 'sensitive')", name="ck_agent_task_action_class"),
        Index("ix_agent_task_parent", "parent_task_id"),
        Index("ix_agent_task_route", "project_id", "skill_profile_id", "status"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid4()))
    project_id: Mapped[str] = mapped_column(ForeignKey("project.id"), nullable=False)
    plan_version_id: Mapped[str] = mapped_column(ForeignKey("agent_plan_version.id"), nullable=False)
    team_id: Mapped[str] = mapped_column(ForeignKey("agent_team.id"), nullable=False)
    skill_profile_id: Mapped[str] = mapped_column(ForeignKey("skill_profile.id"), nullable=False)
    task_key: Mapped[str] = mapped_column(String(80), nullable=False)
    title: Mapped[str] = mapped_column(String(200), nullable=False)
    action_class: Mapped[str] = mapped_column(String(16), default="standard", server_default="standard", nullable=False)
    dependency_keys: Mapped[list[str]] = mapped_column(JSON, default=list, server_default="[]", nullable=False)
    attempt_count: Mapped[int] = mapped_column(Integer, default=0, server_default="0", nullable=False)
    retry_not_before: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    retry_from_run_id: Mapped[str | None] = mapped_column(String(36))
    parent_task_id: Mapped[str | None] = mapped_column(String(36))
    depth: Mapped[int] = mapped_column(Integer, default=1, server_default="1", nullable=False)
    priority: Mapped[str] = mapped_column(String(2), default="P2", server_default="P2", nullable=False)
    due_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    progress_percent: Mapped[int] = mapped_column(Integer, default=0, server_default="0", nullable=False)
    status: Mapped[str] = mapped_column(String(16), default="ready", nullable=False)
    claimed_session_id: Mapped[str | None] = mapped_column(ForeignKey("agent_session.id"))
    lease_expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)


class AgentTaskClaim(Base):
    __tablename__ = "agent_task_claim"
    __table_args__ = (UniqueConstraint("session_id", "request_key", name="uq_agent_claim_request"),)

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid4()))
    task_id: Mapped[str] = mapped_column(ForeignKey("agent_task.id"), nullable=False, index=True)
    session_id: Mapped[str] = mapped_column(ForeignKey("agent_session.id"), nullable=False)
    request_key: Mapped[str | None] = mapped_column(String(36))
    claimed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)
    lease_expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    released_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    release_reason: Mapped[str | None] = mapped_column(String(24))


class FlowDefinition(Base):
    __tablename__ = "flow_definition"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid4()))
    project_id: Mapped[str] = mapped_column(ForeignKey("project.id"), nullable=False, index=True)
    team_id: Mapped[str] = mapped_column(ForeignKey("agent_team.id"), nullable=False)
    name: Mapped[str] = mapped_column(String(120), nullable=False)
    created_by: Mapped[str] = mapped_column(ForeignKey("app_user.id"), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)


class FlowVersion(Base):
    __tablename__ = "flow_version"
    __table_args__ = (CheckConstraint("status IN ('draft', 'review', 'validated')", name="ck_flow_version_status"),)
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid4()))
    flow_definition_id: Mapped[str] = mapped_column(ForeignKey("flow_definition.id"), nullable=False)
    version_number: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    graph: Mapped[dict] = mapped_column(JSON, nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="draft")
    validated_by: Mapped[str | None] = mapped_column(ForeignKey("app_user.id"))
    validated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)


class FlowRun(Base):
    __tablename__ = "flow_run"
    __table_args__ = (CheckConstraint("status IN ('waiting_human', 'running', 'paused', 'completed', 'cancelled')", name="ck_flow_run_status"),)
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid4()))
    project_id: Mapped[str] = mapped_column(ForeignKey("project.id"), nullable=False, index=True)
    team_id: Mapped[str] = mapped_column(ForeignKey("agent_team.id"), nullable=False)
    flow_version_id: Mapped[str] = mapped_column(ForeignKey("flow_version.id"), nullable=False)
    status: Mapped[str] = mapped_column(String(20), nullable=False, default="waiting_human")
    launched_by: Mapped[str] = mapped_column(ForeignKey("app_user.id"), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)


class AgentTaskRun(Base):
    """Tentative technique distincte et terminale après clôture du claim."""
    __tablename__ = "agent_task_run"
    __table_args__ = (
        UniqueConstraint("task_id", "attempt_number", name="uq_agent_task_run_attempt"),
        UniqueConstraint("claim_id", name="uq_agent_task_run_claim"),
        UniqueConstraint("generation_run_id", name="uq_agent_task_generation_run"),
        CheckConstraint("attempt_number > 0", name="ck_agent_task_run_attempt"),
        CheckConstraint("status IN ('running', 'completed', 'failed', 'cancelled')", name="ck_agent_task_run_status"),
    )
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid4()))
    task_id: Mapped[str] = mapped_column(ForeignKey("agent_task.id"), nullable=False, index=True)
    llm_profile_version_id: Mapped[str | None] = mapped_column(ForeignKey("llm_profile_version.id"))
    generation_run_id: Mapped[str | None] = mapped_column(ForeignKey("generation_run.id"))
    claim_id: Mapped[str] = mapped_column(ForeignKey("agent_task_claim.id"), nullable=False)
    approval_id: Mapped[str | None] = mapped_column(ForeignKey("agent_action_approval.id"), unique=True)
    parent_run_id: Mapped[str | None] = mapped_column(ForeignKey("agent_task_run.id"))
    attempt_number: Mapped[int] = mapped_column(Integer, nullable=False)
    status: Mapped[str] = mapped_column(String(16), default="running", nullable=False)
    error_code: Mapped[str | None] = mapped_column(String(24))
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)
    ended_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class AgentActionApproval(Base):
    """Décision humaine à usage unique pour la prochaine tentative d'une feuille."""
    __tablename__ = "agent_action_approval"
    __table_args__ = (
        CheckConstraint("attempt_number > 0", name="ck_agent_approval_attempt"),
        CheckConstraint("status IN ('pending', 'approved', 'rejected', 'consumed', 'invalidated')", name="ck_agent_approval_status"),
        CheckConstraint("autonomy_mode IN ('sensitive', 'per_action')", name="ck_agent_approval_mode"),
        CheckConstraint("action_class IN ('standard', 'sensitive')", name="ck_agent_approval_class"),
        CheckConstraint("(requested_by IS NULL) <> (requested_session_id IS NULL)", name="ck_agent_approval_requester"),
        CheckConstraint("(decision_result IS NULL AND decided_by IS NULL AND decided_at IS NULL) OR "
                        "(decision_result IS NOT NULL AND decision_result IN ('approved', 'rejected') AND decided_by IS NOT NULL AND decided_at IS NOT NULL)",
                        name="ck_agent_approval_decision"),
        CheckConstraint("(status = 'pending' AND decision_result IS NULL) OR "
                        "(status IN ('approved', 'consumed') AND decision_result IS NOT NULL AND decision_result = 'approved') OR "
                        "(status = 'rejected' AND decision_result IS NOT NULL AND decision_result = 'rejected') OR status = 'invalidated'",
                        name="ck_agent_approval_decision_status"),
        CheckConstraint("(status = 'consumed') = (consumed_claim_id IS NOT NULL)", name="ck_agent_approval_consumption"),
        CheckConstraint("(status = 'invalidated') = (invalidated_at IS NOT NULL AND invalidation_reason IS NOT NULL)",
                        name="ck_agent_approval_invalidation"),
        Index("uq_agent_approval_active", "task_id", "attempt_number", unique=True,
              postgresql_where=text("status IN ('pending', 'approved')")),
    )
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid4()))
    task_id: Mapped[str] = mapped_column(ForeignKey("agent_task.id"), nullable=False, index=True)
    attempt_number: Mapped[int] = mapped_column(Integer, nullable=False)
    autonomy_mode: Mapped[str] = mapped_column(String(16), nullable=False)
    action_class: Mapped[str] = mapped_column(String(16), nullable=False)
    status: Mapped[str] = mapped_column(String(16), default="pending", nullable=False)
    requested_by: Mapped[str | None] = mapped_column(ForeignKey("app_user.id"))
    requested_session_id: Mapped[str | None] = mapped_column(ForeignKey("agent_session.id"))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)
    decision_result: Mapped[str | None] = mapped_column(String(16))
    decided_by: Mapped[str | None] = mapped_column(ForeignKey("app_user.id"))
    decided_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    consumed_claim_id: Mapped[str | None] = mapped_column(ForeignKey("agent_task_claim.id"), unique=True)
    invalidated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    invalidation_reason: Mapped[str | None] = mapped_column(String(24))


class AgentSprint(Base):
    __tablename__ = "agent_sprint"
    __table_args__ = (
        UniqueConstraint("id", "plan_version_id", "project_id", name="uq_agent_sprint_scope"),
        ForeignKeyConstraint(["plan_version_id", "project_id"], ["agent_plan_version.id", "agent_plan_version.project_id"], name="fk_agent_sprint_plan_scope"),
        CheckConstraint("status IN ('draft', 'active', 'closed', 'cancelled')", name="ck_agent_sprint_status"),
        CheckConstraint("ends_at > starts_at AND ends_at <= starts_at + INTERVAL '90 days'", name="ck_agent_sprint_window"),
        CheckConstraint("revision > 0", name="ck_agent_sprint_revision"),
        CheckConstraint("(status IN ('closed', 'cancelled')) = (closed_at IS NOT NULL)", name="ck_agent_sprint_closed"),
    )
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid4()))
    project_id: Mapped[str] = mapped_column(ForeignKey("project.id"), nullable=False, index=True)
    plan_version_id: Mapped[str] = mapped_column(String(36), nullable=False)
    name: Mapped[str] = mapped_column(String(120), nullable=False)
    goal: Mapped[str] = mapped_column(String(1000), default="", nullable=False)
    starts_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    ends_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    status: Mapped[str] = mapped_column(String(16), default="draft", nullable=False)
    revision: Mapped[int] = mapped_column(Integer, default=1, nullable=False)
    created_by: Mapped[str] = mapped_column(ForeignKey("app_user.id"), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)
    activated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    closed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class AgentSprintTask(Base):
    __tablename__ = "agent_sprint_task"
    __table_args__ = (
        UniqueConstraint("sprint_id", "task_id", name="uq_agent_sprint_task"),
        ForeignKeyConstraint(["sprint_id", "plan_version_id", "project_id"], ["agent_sprint.id", "agent_sprint.plan_version_id", "agent_sprint.project_id"], name="fk_agent_sprint_task_sprint_scope"),
        ForeignKeyConstraint(["task_id", "plan_version_id", "project_id"], ["agent_task.id", "agent_task.plan_version_id", "agent_task.project_id"], name="fk_agent_sprint_task_task_scope"),
    )
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid4()))
    sprint_id: Mapped[str] = mapped_column(String(36), nullable=False, index=True)
    task_id: Mapped[str] = mapped_column(String(36), nullable=False, index=True)
    plan_version_id: Mapped[str] = mapped_column(String(36), nullable=False)
    project_id: Mapped[str] = mapped_column(String(36), nullable=False)
    target_due_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    added_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)


class AgentSprintRevision(Base):
    __tablename__ = "agent_sprint_revision"
    __table_args__ = (UniqueConstraint("sprint_id", "revision", name="uq_agent_sprint_revision"),)
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid4()))
    sprint_id: Mapped[str] = mapped_column(ForeignKey("agent_sprint.id"), nullable=False, index=True)
    revision: Mapped[int] = mapped_column(Integer, nullable=False)
    snapshot: Mapped[dict] = mapped_column(JSON, nullable=False)
    action: Mapped[str] = mapped_column(String(16), nullable=False)
    actor_id: Mapped[str] = mapped_column(ForeignKey("app_user.id"), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)
