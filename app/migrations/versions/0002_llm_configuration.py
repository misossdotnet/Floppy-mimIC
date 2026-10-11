"""Profils LLM cloud/locaux versionnés et diagnostics.

Revision ID: 0002_llm_configuration
Revises: 0001_identity_acl
"""

from alembic import op
import sqlalchemy as sa


revision = "0002_llm_configuration"
down_revision = "0001_identity_acl"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table("llm_profile_version",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("profile_id", sa.String(36), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("name", sa.String(120), nullable=False),
        sa.Column("provider", sa.String(32), nullable=False),
        sa.Column("model_name", sa.String(160), nullable=False),
        sa.Column("base_url", sa.String(500)),
        sa.Column("api_version", sa.String(64)),
        sa.Column("credential_env", sa.String(100)),
        sa.Column("temperature", sa.Float()),
        sa.Column("max_output_tokens", sa.Integer(), nullable=False),
        sa.Column("timeout_seconds", sa.Integer(), nullable=False),
        sa.Column("max_retries", sa.Integer(), nullable=False),
        sa.Column("enabled", sa.Boolean(), nullable=False),
        sa.Column("is_current", sa.Boolean(), nullable=False),
        sa.Column("created_by", sa.String(36), sa.ForeignKey("app_user.id"), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("profile_id", "version", name="uq_llm_profile_version"),
    )
    op.create_index("ix_llm_profile_version_profile_id", "llm_profile_version", ["profile_id"])
    op.create_index("uq_llm_profile_current", "llm_profile_version", ["profile_id"],
                    unique=True, postgresql_where=sa.text("is_current"))
    op.create_table("llm_settings",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("default_version_id", sa.String(36), sa.ForeignKey("llm_profile_version.id")),
        sa.Column("updated_by", sa.String(36), sa.ForeignKey("app_user.id")),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.execute("INSERT INTO llm_settings (id, default_version_id, updated_by, updated_at) VALUES (1, NULL, NULL, CURRENT_TIMESTAMP)")
    op.create_table("llm_connection_check",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("profile_version_id", sa.String(36), sa.ForeignKey("llm_profile_version.id"), nullable=False),
        sa.Column("actor_id", sa.String(36), sa.ForeignKey("app_user.id"), nullable=False),
        sa.Column("outcome", sa.String(24), nullable=False),
        sa.Column("error_category", sa.String(40)),
        sa.Column("latency_ms", sa.Integer()),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_llm_connection_check_profile_version_id", "llm_connection_check", ["profile_version_id"])


def downgrade():
    op.drop_table("llm_connection_check")
    op.drop_table("llm_settings")
    op.drop_table("llm_profile_version")
