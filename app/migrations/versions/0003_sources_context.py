"""Sources, analyses IA et contexte versionné.

Revision ID: 0003_sources_context
Revises: 0002_llm_configuration
"""

from alembic import op
import sqlalchemy as sa

revision = "0003_sources_context"
down_revision = "0002_llm_configuration"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table("source_document",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("project_id", sa.String(36), sa.ForeignKey("project.id"), nullable=False),
        sa.Column("created_by", sa.String(36), sa.ForeignKey("app_user.id"), nullable=False),
        sa.Column("name", sa.String(180), nullable=False),
        sa.Column("kind", sa.String(12), nullable=False),
        sa.Column("mime_type", sa.String(120), nullable=False),
        sa.Column("object_key", sa.String(130), unique=True),
        sa.Column("sha256", sa.String(64), nullable=False),
        sa.Column("size_bytes", sa.Integer(), nullable=False),
        sa.Column("direct_text", sa.Text()),
        sa.Column("extracted_text", sa.Text()),
        sa.Column("status", sa.String(24), nullable=False),
        sa.Column("error_code", sa.String(40)),
        sa.Column("attempt", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_source_document_project_id", "source_document", ["project_id"])
    op.create_table("analysis_run",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("project_id", sa.String(36), sa.ForeignKey("project.id"), nullable=False),
        sa.Column("source_id", sa.String(36), sa.ForeignKey("source_document.id"), nullable=False),
        sa.Column("requested_by", sa.String(36), sa.ForeignKey("app_user.id"), nullable=False),
        sa.Column("llm_profile_version_id", sa.String(36), sa.ForeignKey("llm_profile_version.id"), nullable=False),
        sa.Column("status", sa.String(24), nullable=False),
        sa.Column("prompt_version", sa.String(24), nullable=False),
        sa.Column("input_tokens", sa.Integer()),
        sa.Column("output_tokens", sa.Integer()),
        sa.Column("error_code", sa.String(40)),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("completed_at", sa.DateTime(timezone=True)),
    )
    op.create_index("ix_analysis_run_project_id", "analysis_run", ["project_id"])
    op.create_index("ix_analysis_run_source_id", "analysis_run", ["source_id"])
    op.create_table("context_item",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("project_id", sa.String(36), sa.ForeignKey("project.id"), nullable=False),
        sa.Column("source_id", sa.String(36), sa.ForeignKey("source_document.id")),
        sa.Column("analysis_run_id", sa.String(36), sa.ForeignKey("analysis_run.id")),
        sa.Column("type", sa.String(32), nullable=False),
        sa.Column("origin", sa.String(12), nullable=False),
        sa.Column("status", sa.String(24), nullable=False),
        sa.Column("current_version", sa.Integer(), nullable=False),
        sa.Column("title", sa.String(180), nullable=False),
        sa.Column("content", sa.Text(), nullable=False),
        sa.Column("created_by", sa.String(36), sa.ForeignKey("app_user.id")),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("id", "project_id", name="uq_context_item_project"),
    )
    op.create_index("ix_context_item_project_id", "context_item", ["project_id"])
    op.create_index("ix_context_item_source_id", "context_item", ["source_id"])
    op.create_table("context_item_version",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("item_id", sa.String(36), sa.ForeignKey("context_item.id"), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("title", sa.String(180), nullable=False),
        sa.Column("content", sa.Text(), nullable=False),
        sa.Column("status", sa.String(24), nullable=False),
        sa.Column("actor_id", sa.String(36), sa.ForeignKey("app_user.id")),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("item_id", "version", name="uq_context_item_version"),
    )
    op.create_index("ix_context_item_version_item_id", "context_item_version", ["item_id"])
    op.execute("INSERT INTO role_permission (role_code, permission_code) SELECT r.code, p.code FROM role r CROSS JOIN permission p WHERE r.code = 'architect' AND p.code = 'context.edit' ON CONFLICT DO NOTHING")


def downgrade():
    op.execute("DELETE FROM role_permission WHERE role_code = 'architect' AND permission_code = 'context.edit'")
    op.drop_table("context_item_version")
    op.drop_table("context_item")
    op.drop_table("analysis_run")
    op.drop_table("source_document")
