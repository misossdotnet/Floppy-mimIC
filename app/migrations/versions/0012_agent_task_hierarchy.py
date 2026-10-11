"""Hiérarchie bornée et progression technique, sans validation documentaire.

Revision ID: 0012_agent_task_hierarchy
Revises: 0011_agent_flow_gate
"""
from alembic import op
import sqlalchemy as sa

revision = "0012_agent_task_hierarchy"
down_revision = "0011_agent_flow_gate"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column("agent_task", sa.Column("parent_task_id", sa.String(36)))
    op.add_column("agent_task", sa.Column("depth", sa.Integer(), server_default="1", nullable=False))
    op.add_column("agent_task", sa.Column("priority", sa.String(2), server_default="P2", nullable=False))
    op.add_column("agent_task", sa.Column("due_at", sa.DateTime(timezone=True)))
    op.add_column("agent_task", sa.Column("progress_percent", sa.Integer(), server_default="0", nullable=False))
    op.execute("UPDATE agent_task SET progress_percent = 100 WHERE status = 'completed'")
    op.drop_constraint("ck_agent_task_status", "agent_task", type_="check")
    op.create_check_constraint("ck_agent_task_status", "agent_task",
        "status IN ('ready', 'claimed', 'running', 'completed', 'cancelled')")
    op.create_unique_constraint("uq_agent_task_scope", "agent_task",
                                ["id", "plan_version_id", "project_id", "team_id"])
    op.create_foreign_key("fk_agent_task_parent_scope", "agent_task", "agent_task",
        ["parent_task_id", "plan_version_id", "project_id", "team_id"],
        ["id", "plan_version_id", "project_id", "team_id"])
    for name, expression in {
        "depth": "depth BETWEEN 1 AND 3",
        "parent_depth": "(parent_task_id IS NULL AND depth = 1) OR (parent_task_id IS NOT NULL AND depth > 1)",
        "not_self": "parent_task_id IS NULL OR parent_task_id <> id",
        "priority": "priority IN ('P0', 'P1', 'P2', 'P3')",
        "progress": "progress_percent BETWEEN 0 AND 100",
        "completed_progress": "status <> 'completed' OR progress_percent = 100",
    }.items():
        op.create_check_constraint(f"ck_agent_task_{name}", "agent_task", expression)
    op.create_index("ix_agent_task_parent", "agent_task", ["parent_task_id"])


def downgrade():
    # Perte de hiérarchie/progression : base jetable ou restauration coordonnée uniquement.
    op.execute("UPDATE agent_task SET status = 'ready' WHERE status = 'running'")
    op.drop_index("ix_agent_task_parent", table_name="agent_task")
    for name in ("completed_progress", "progress", "priority", "not_self", "parent_depth", "depth"):
        op.drop_constraint(f"ck_agent_task_{name}", "agent_task", type_="check")
    op.drop_constraint("fk_agent_task_parent_scope", "agent_task", type_="foreignkey")
    op.drop_constraint("uq_agent_task_scope", "agent_task", type_="unique")
    op.drop_constraint("ck_agent_task_status", "agent_task", type_="check")
    op.create_check_constraint("ck_agent_task_status", "agent_task",
        "status IN ('ready', 'claimed', 'completed', 'cancelled')")
    for name in ("progress_percent", "due_at", "priority", "depth", "parent_task_id"):
        op.drop_column("agent_task", name)
