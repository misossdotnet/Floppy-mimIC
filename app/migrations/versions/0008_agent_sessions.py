"""Sessions techniques bornées pour le relais MCP.

Revision ID: 0008_agent_sessions
Revises: 0007_versioning_regeneration
"""

from alembic import op
import sqlalchemy as sa

revision = "0008_agent_sessions"
down_revision = "0007_versioning_regeneration"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table("agent_session",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("project_id", sa.String(36), sa.ForeignKey("project.id"), nullable=False),
        sa.Column("created_by", sa.String(36), sa.ForeignKey("app_user.id"), nullable=False),
        sa.Column("client_code", sa.String(40), nullable=False),
        sa.Column("skill_code", sa.String(40), nullable=False),
        sa.Column("capabilities", sa.JSON(), nullable=False),
        sa.Column("token_hash", sa.String(64), nullable=False, unique=True),
        sa.Column("status", sa.String(16), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("last_heartbeat_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("max_expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("revoked_at", sa.DateTime(timezone=True)),
        sa.CheckConstraint("status IN ('active', 'expired', 'revoked')", name="ck_agent_session_status"),
    )
    op.create_index("ix_agent_session_project_status", "agent_session", ["project_id", "status"])
    op.execute("INSERT INTO permission (code) VALUES ('agent.session.connect') ON CONFLICT DO NOTHING")
    op.execute("INSERT INTO role_permission (role_code, permission_code) "
               "SELECT code, 'agent.session.connect' FROM role "
               "WHERE code IN ('product_owner', 'project_manager', 'architect') ON CONFLICT DO NOTHING")


def downgrade():
    op.execute("DELETE FROM role_permission WHERE permission_code = 'agent.session.connect'")
    op.execute("DELETE FROM permission WHERE code = 'agent.session.connect'")
    op.drop_index("ix_agent_session_project_status", table_name="agent_session")
    op.drop_table("agent_session")
