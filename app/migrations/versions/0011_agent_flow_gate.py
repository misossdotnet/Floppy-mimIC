"""Flux mono-noeud et approbation humaine nécessaires aux réservations.

Revision ID: 0011_agent_flow_gate
Revises: 0010_agent_task_reservations
"""

from alembic import op
import sqlalchemy as sa

revision = "0011_agent_flow_gate"
down_revision = "0010_agent_task_reservations"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table("flow_definition",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("project_id", sa.String(36), sa.ForeignKey("project.id"), nullable=False),
        sa.Column("team_id", sa.String(36), sa.ForeignKey("agent_team.id"), nullable=False),
        sa.Column("name", sa.String(120), nullable=False),
        sa.Column("created_by", sa.String(36), sa.ForeignKey("app_user.id"), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False))
    op.create_index("ix_flow_definition_project_id", "flow_definition", ["project_id"])
    op.create_table("flow_version",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("flow_definition_id", sa.String(36), sa.ForeignKey("flow_definition.id"), nullable=False),
        sa.Column("version_number", sa.Integer(), nullable=False),
        sa.Column("graph", sa.JSON(), nullable=False),
        sa.Column("status", sa.String(16), nullable=False),
        sa.Column("validated_by", sa.String(36), sa.ForeignKey("app_user.id")),
        sa.Column("validated_at", sa.DateTime(timezone=True)),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint("status IN ('draft', 'review', 'validated')", name="ck_flow_version_status"))
    op.create_table("flow_run",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("project_id", sa.String(36), sa.ForeignKey("project.id"), nullable=False),
        sa.Column("team_id", sa.String(36), sa.ForeignKey("agent_team.id"), nullable=False),
        sa.Column("flow_version_id", sa.String(36), sa.ForeignKey("flow_version.id"), nullable=False),
        sa.Column("status", sa.String(20), nullable=False),
        sa.Column("launched_by", sa.String(36), sa.ForeignKey("app_user.id"), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint("status IN ('waiting_human', 'running', 'completed', 'cancelled')", name="ck_flow_run_status"))
    op.create_index("ix_flow_run_project_id", "flow_run", ["project_id"])
    op.add_column("agent_plan_version", sa.Column("validated_flow_run_id", sa.String(36),
                  sa.ForeignKey("flow_run.id")))


def downgrade():
    op.drop_column("agent_plan_version", "validated_flow_run_id")
    op.drop_index("ix_flow_run_project_id", table_name="flow_run")
    op.drop_table("flow_run")
    op.drop_table("flow_version")
    op.drop_index("ix_flow_definition_project_id", table_name="flow_definition")
    op.drop_table("flow_definition")
