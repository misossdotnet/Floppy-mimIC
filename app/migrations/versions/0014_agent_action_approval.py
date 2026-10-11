"""Modes d'autonomie et décisions humaines à usage unique.

Revision ID: 0014_agent_action_approval
Revises: 0013_agent_task_coordinator
"""
from alembic import op
import sqlalchemy as sa

revision = "0014_agent_action_approval"
down_revision = "0013_agent_task_coordinator"
branch_labels = None
depends_on = None


def upgrade():
    # Compatibilité : les plans existants restent bornés, leurs tâches standard.
    op.add_column("agent_plan_version", sa.Column("autonomy_mode", sa.String(16), server_default="bounded", nullable=False))
    op.create_check_constraint("ck_agent_plan_autonomy", "agent_plan_version", "autonomy_mode IN ('bounded', 'sensitive', 'per_action')")
    op.add_column("agent_task", sa.Column("action_class", sa.String(16), server_default="standard", nullable=False))
    op.create_check_constraint("ck_agent_task_action_class", "agent_task", "action_class IN ('standard', 'sensitive')")
    op.create_table("agent_action_approval",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("task_id", sa.String(36), sa.ForeignKey("agent_task.id"), nullable=False),
        sa.Column("attempt_number", sa.Integer(), nullable=False),
        sa.Column("autonomy_mode", sa.String(16), nullable=False),
        sa.Column("action_class", sa.String(16), nullable=False),
        sa.Column("status", sa.String(16), nullable=False),
        sa.Column("requested_by", sa.String(36), sa.ForeignKey("app_user.id")),
        sa.Column("requested_session_id", sa.String(36), sa.ForeignKey("agent_session.id")),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("decision_result", sa.String(16)),
        sa.Column("decided_by", sa.String(36), sa.ForeignKey("app_user.id")),
        sa.Column("decided_at", sa.DateTime(timezone=True)),
        sa.Column("consumed_claim_id", sa.String(36), sa.ForeignKey("agent_task_claim.id"), unique=True),
        sa.Column("invalidated_at", sa.DateTime(timezone=True)),
        sa.Column("invalidation_reason", sa.String(24)),
        sa.CheckConstraint("attempt_number > 0", name="ck_agent_approval_attempt"),
        sa.CheckConstraint("status IN ('pending', 'approved', 'rejected', 'consumed', 'invalidated')", name="ck_agent_approval_status"),
        sa.CheckConstraint("autonomy_mode IN ('sensitive', 'per_action')", name="ck_agent_approval_mode"),
        sa.CheckConstraint("action_class IN ('standard', 'sensitive')", name="ck_agent_approval_class"),
        sa.CheckConstraint("(requested_by IS NULL) <> (requested_session_id IS NULL)", name="ck_agent_approval_requester"),
        sa.CheckConstraint("(decision_result IS NULL AND decided_by IS NULL AND decided_at IS NULL) OR "
            "(decision_result IS NOT NULL AND decision_result IN ('approved', 'rejected') AND decided_by IS NOT NULL AND decided_at IS NOT NULL)", name="ck_agent_approval_decision"),
        sa.CheckConstraint("(status = 'pending' AND decision_result IS NULL) OR "
            "(status IN ('approved', 'consumed') AND decision_result IS NOT NULL AND decision_result = 'approved') OR "
            "(status = 'rejected' AND decision_result IS NOT NULL AND decision_result = 'rejected') OR status = 'invalidated'", name="ck_agent_approval_decision_status"),
        sa.CheckConstraint("(status = 'consumed') = (consumed_claim_id IS NOT NULL)", name="ck_agent_approval_consumption"),
        sa.CheckConstraint("(status = 'invalidated') = (invalidated_at IS NOT NULL AND invalidation_reason IS NOT NULL)", name="ck_agent_approval_invalidation"))
    op.create_index("ix_agent_action_approval_task_id", "agent_action_approval", ["task_id"])
    op.create_index("uq_agent_approval_active", "agent_action_approval", ["task_id", "attempt_number"], unique=True,
                    postgresql_where=sa.text("status IN ('pending', 'approved')"))
    op.add_column("agent_task_run", sa.Column("approval_id", sa.String(36)))
    op.create_foreign_key("fk_agent_run_approval", "agent_task_run", "agent_action_approval", ["approval_id"], ["id"])
    op.create_unique_constraint("uq_agent_run_approval", "agent_task_run", ["approval_id"])


def downgrade():
    # Destructif : uniquement sur une base jetable ou restauration coordonnée.
    op.drop_constraint("uq_agent_run_approval", "agent_task_run", type_="unique")
    op.drop_constraint("fk_agent_run_approval", "agent_task_run", type_="foreignkey")
    op.drop_column("agent_task_run", "approval_id")
    op.drop_table("agent_action_approval")
    op.drop_constraint("ck_agent_task_action_class", "agent_task", type_="check")
    op.drop_column("agent_task", "action_class")
    op.drop_constraint("ck_agent_plan_autonomy", "agent_plan_version", type_="check")
    op.drop_column("agent_plan_version", "autonomy_mode")
