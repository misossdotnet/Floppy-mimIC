"""Calendriers de sprint, sélection confinée et snapshots de planification.

Revision ID: 0015_agent_sprints
Revises: 0014_agent_action_approval
"""
from alembic import op
import sqlalchemy as sa

revision = "0015_agent_sprints"
down_revision = "0014_agent_action_approval"
branch_labels = None
depends_on = None


def upgrade():
    op.create_unique_constraint("uq_agent_plan_project", "agent_plan_version", ["id", "project_id"])
    op.create_unique_constraint("uq_agent_task_plan_project", "agent_task", ["id", "plan_version_id", "project_id"])
    op.create_table("agent_sprint",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("project_id", sa.String(36), sa.ForeignKey("project.id"), nullable=False),
        sa.Column("plan_version_id", sa.String(36), nullable=False),
        sa.Column("name", sa.String(120), nullable=False), sa.Column("goal", sa.String(1000), nullable=False),
        sa.Column("starts_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("ends_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("status", sa.String(16), nullable=False), sa.Column("revision", sa.Integer(), nullable=False),
        sa.Column("created_by", sa.String(36), sa.ForeignKey("app_user.id"), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("activated_at", sa.DateTime(timezone=True)), sa.Column("closed_at", sa.DateTime(timezone=True)),
        sa.UniqueConstraint("id", "plan_version_id", "project_id", name="uq_agent_sprint_scope"),
        sa.ForeignKeyConstraint(["plan_version_id", "project_id"], ["agent_plan_version.id", "agent_plan_version.project_id"], name="fk_agent_sprint_plan_scope"),
        sa.CheckConstraint("status IN ('draft', 'active', 'closed', 'cancelled')", name="ck_agent_sprint_status"),
        sa.CheckConstraint("ends_at > starts_at AND ends_at <= starts_at + INTERVAL '90 days'", name="ck_agent_sprint_window"),
        sa.CheckConstraint("revision > 0", name="ck_agent_sprint_revision"),
        sa.CheckConstraint("(status IN ('closed', 'cancelled')) = (closed_at IS NOT NULL)", name="ck_agent_sprint_closed"))
    op.create_index("ix_agent_sprint_project_id", "agent_sprint", ["project_id"])
    op.create_table("agent_sprint_task",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("sprint_id", sa.String(36), nullable=False), sa.Column("task_id", sa.String(36), nullable=False),
        sa.Column("project_id", sa.String(36), nullable=False), sa.Column("plan_version_id", sa.String(36), nullable=False),
        sa.Column("target_due_at", sa.DateTime(timezone=True)), sa.Column("added_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("sprint_id", "task_id", name="uq_agent_sprint_task"),
        sa.ForeignKeyConstraint(["sprint_id", "plan_version_id", "project_id"], ["agent_sprint.id", "agent_sprint.plan_version_id", "agent_sprint.project_id"], name="fk_agent_sprint_task_sprint_scope"),
        sa.ForeignKeyConstraint(["task_id", "plan_version_id", "project_id"], ["agent_task.id", "agent_task.plan_version_id", "agent_task.project_id"], name="fk_agent_sprint_task_task_scope"))
    for column in ("sprint_id", "task_id"):
        op.create_index(f"ix_agent_sprint_task_{column}", "agent_sprint_task", [column])
    op.create_table("agent_sprint_revision",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("sprint_id", sa.String(36), sa.ForeignKey("agent_sprint.id"), nullable=False),
        sa.Column("revision", sa.Integer(), nullable=False), sa.Column("snapshot", sa.JSON(), nullable=False),
        sa.Column("action", sa.String(16), nullable=False),
        sa.Column("actor_id", sa.String(36), sa.ForeignKey("app_user.id"), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("sprint_id", "revision", name="uq_agent_sprint_revision"))
    op.create_index("ix_agent_sprint_revision_sprint_id", "agent_sprint_revision", ["sprint_id"])


def downgrade():
    # Destructif : supprimer uniquement sur base jetable/restauration coordonnée.
    for table in ("agent_sprint_revision", "agent_sprint_task", "agent_sprint"):
        op.drop_table(table)
    op.drop_constraint("uq_agent_task_plan_project", "agent_task", type_="unique")
    op.drop_constraint("uq_agent_plan_project", "agent_plan_version", type_="unique")
