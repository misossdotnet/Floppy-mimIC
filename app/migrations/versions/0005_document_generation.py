"""Livrables documentaires et tentatives de génération.

Revision ID: 0005_document_generation
Revises: 0004_context_review
"""

from alembic import op
import sqlalchemy as sa

revision = "0005_document_generation"
down_revision = "0004_context_review"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table("deliverable",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("project_id", sa.String(36), sa.ForeignKey("project.id"), nullable=False),
        sa.Column("type", sa.String(40), nullable=False),
        sa.Column("title", sa.String(180), nullable=False),
        sa.Column("current_version", sa.Integer(), nullable=False),
        sa.Column("created_by", sa.String(36), sa.ForeignKey("app_user.id"), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_deliverable_project_id", "deliverable", ["project_id"])
    op.create_table("generation_run",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("project_id", sa.String(36), sa.ForeignKey("project.id"), nullable=False),
        sa.Column("deliverable_id", sa.String(36), sa.ForeignKey("deliverable.id"), nullable=False),
        sa.Column("requested_by", sa.String(36), sa.ForeignKey("app_user.id"), nullable=False),
        sa.Column("llm_profile_version_id", sa.String(36), sa.ForeignKey("llm_profile_version.id"), nullable=False),
        sa.Column("attempt_number", sa.Integer(), nullable=False),
        sa.Column("status", sa.String(24), nullable=False),
        sa.Column("prompt_version", sa.String(24), nullable=False),
        sa.Column("prompt_template", sa.Text(), nullable=False),
        sa.Column("context_snapshot", sa.JSON(), nullable=False),
        sa.Column("parameters", sa.JSON(), nullable=False),
        sa.Column("input_tokens", sa.Integer()),
        sa.Column("output_tokens", sa.Integer()),
        sa.Column("estimated_cost", sa.Numeric(12, 6)),
        sa.Column("cost_currency", sa.String(3)),
        sa.Column("pricing_source", sa.String(120)),
        sa.Column("pricing_date", sa.Date()),
        sa.Column("error_code", sa.String(40)),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("completed_at", sa.DateTime(timezone=True)),
        sa.UniqueConstraint("deliverable_id", "attempt_number", name="uq_generation_attempt"),
    )
    op.create_index("ix_generation_run_project_id", "generation_run", ["project_id"])
    op.create_index("ix_generation_run_deliverable_id", "generation_run", ["deliverable_id"])
    op.create_table("deliverable_version",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("deliverable_id", sa.String(36), sa.ForeignKey("deliverable.id"), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("title", sa.String(180), nullable=False),
        sa.Column("content", sa.Text(), nullable=False),
        sa.Column("status", sa.String(24), nullable=False),
        sa.Column("generation_run_id", sa.String(36), sa.ForeignKey("generation_run.id")),
        sa.Column("parent_version_id", sa.String(36), sa.ForeignKey("deliverable_version.id")),
        sa.Column("actor_id", sa.String(36), sa.ForeignKey("app_user.id")),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("deliverable_id", "version", name="uq_deliverable_version"),
    )
    op.create_index("ix_deliverable_version_deliverable_id", "deliverable_version", ["deliverable_id"])
    op.create_index("ix_deliverable_version_generation_run_id", "deliverable_version", ["generation_run_id"])


def downgrade():
    op.drop_table("deliverable_version")
    op.drop_table("generation_run")
    op.drop_table("deliverable")
