"""Tâches préparatoires et historique de réservations techniques.

Revision ID: 0010_agent_task_reservations
Revises: 0009_agent_teams_catalog
"""

from alembic import op
import sqlalchemy as sa

revision = "0010_agent_task_reservations"
down_revision = "0009_agent_teams_catalog"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table("agent_plan_version",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("project_id", sa.String(36), sa.ForeignKey("project.id"), nullable=False),
        sa.Column("team_id", sa.String(36), sa.ForeignKey("agent_team.id"), nullable=False),
        sa.Column("flow_run_id", sa.String(36)),
        sa.Column("status", sa.String(16), nullable=False),
        sa.Column("approved_by", sa.String(36), sa.ForeignKey("app_user.id")),
        sa.Column("approved_at", sa.DateTime(timezone=True)),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint("status IN ('proposed', 'review', 'approved', 'rejected')",
                           name="ck_agent_plan_version_status"),
    )
    op.create_index("ix_agent_plan_version_project_id", "agent_plan_version", ["project_id"])
    op.create_table("agent_task",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("project_id", sa.String(36), sa.ForeignKey("project.id"), nullable=False),
        sa.Column("plan_version_id", sa.String(36), sa.ForeignKey("agent_plan_version.id"), nullable=False),
        sa.Column("team_id", sa.String(36), sa.ForeignKey("agent_team.id"), nullable=False),
        sa.Column("skill_profile_id", sa.String(36), sa.ForeignKey("skill_profile.id"), nullable=False),
        sa.Column("task_key", sa.String(80), nullable=False),
        sa.Column("title", sa.String(200), nullable=False),
        sa.Column("status", sa.String(16), nullable=False),
        sa.Column("claimed_session_id", sa.String(36), sa.ForeignKey("agent_session.id")),
        sa.Column("lease_expires_at", sa.DateTime(timezone=True)),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("plan_version_id", "task_key", name="uq_agent_task_plan_key"),
        sa.CheckConstraint("status IN ('ready', 'claimed', 'completed', 'cancelled')",
                           name="ck_agent_task_status"),
    )
    op.create_index("ix_agent_task_route", "agent_task", ["project_id", "skill_profile_id", "status"])
    op.create_table("agent_task_claim",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("task_id", sa.String(36), sa.ForeignKey("agent_task.id"), nullable=False),
        sa.Column("session_id", sa.String(36), sa.ForeignKey("agent_session.id"), nullable=False),
        sa.Column("claimed_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("lease_expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("released_at", sa.DateTime(timezone=True)),
        sa.Column("release_reason", sa.String(24)),
    )
    op.create_index("ix_agent_task_claim_task_id", "agent_task_claim", ["task_id"])
    for code in ("agent.task.claim", "agent.message.send"):
        op.execute(f"INSERT INTO permission (code) VALUES ('{code}') ON CONFLICT DO NOTHING")
        op.execute("INSERT INTO role_permission (role_code, permission_code) "
                   f"SELECT code, '{code}' FROM role "
                   "WHERE code IN ('product_owner', 'project_manager', 'architect') ON CONFLICT DO NOTHING")


def downgrade():
    for code in ("agent.task.claim", "agent.message.send"):
        op.execute(f"DELETE FROM role_permission WHERE permission_code = '{code}'")
        op.execute(f"DELETE FROM permission WHERE code = '{code}'")
    op.drop_index("ix_agent_task_claim_task_id", table_name="agent_task_claim")
    op.drop_table("agent_task_claim")
    op.drop_index("ix_agent_task_route", table_name="agent_task")
    op.drop_table("agent_task")
    op.drop_index("ix_agent_plan_version_project_id", table_name="agent_plan_version")
    op.drop_table("agent_plan_version")
