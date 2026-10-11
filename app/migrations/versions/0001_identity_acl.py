"""Comptes, sessions, projets, rôles et audit initial.

Revision ID: 0001_identity_acl
Revises:
"""

from alembic import op
import sqlalchemy as sa


revision = "0001_identity_acl"
down_revision = None
branch_labels = None
depends_on = None


def upgrade():
    op.create_table("app_user",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("email", sa.String(320), nullable=False),
        sa.Column("display_name", sa.String(120), nullable=False),
        sa.Column("password_hash", sa.Text()),
        sa.Column("is_active", sa.Boolean(), nullable=False),
        sa.Column("is_platform_admin", sa.Boolean(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("last_login_at", sa.DateTime(timezone=True)),
    )
    op.create_index("ix_app_user_email", "app_user", ["email"], unique=True)
    op.create_table("role",
        sa.Column("code", sa.String(40), primary_key=True),
        sa.Column("label", sa.String(100), nullable=False),
    )
    op.create_table("permission", sa.Column("code", sa.String(80), primary_key=True))
    op.create_table("role_permission",
        sa.Column("role_code", sa.String(40), sa.ForeignKey("role.code", ondelete="CASCADE"), primary_key=True),
        sa.Column("permission_code", sa.String(80), sa.ForeignKey("permission.code", ondelete="CASCADE"), primary_key=True),
    )
    op.create_table("project",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("name", sa.String(120), nullable=False),
        sa.Column("description", sa.Text(), nullable=False),
        sa.Column("status", sa.String(32), nullable=False),
        sa.Column("owner_id", sa.String(36), sa.ForeignKey("app_user.id"), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_table("project_member",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("project_id", sa.String(36), sa.ForeignKey("project.id", ondelete="CASCADE"), nullable=False),
        sa.Column("user_id", sa.String(36), sa.ForeignKey("app_user.id"), nullable=False),
        sa.Column("role_code", sa.String(40), sa.ForeignKey("role.code"), nullable=False),
        sa.Column("is_active", sa.Boolean(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("project_id", "user_id", name="uq_project_member_user"),
    )
    op.create_index("ix_project_member_project_id", "project_member", ["project_id"])
    op.create_index("ix_project_member_user_id", "project_member", ["user_id"])
    op.create_table("user_invitation",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("user_id", sa.String(36), sa.ForeignKey("app_user.id"), nullable=False),
        sa.Column("token_hash", sa.String(64), nullable=False, unique=True),
        sa.Column("invited_by", sa.String(36), sa.ForeignKey("app_user.id"), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("used_at", sa.DateTime(timezone=True)),
    )
    op.create_index("ix_user_invitation_user_id", "user_invitation", ["user_id"])
    op.create_table("login_session",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("user_id", sa.String(36), sa.ForeignKey("app_user.id"), nullable=False),
        sa.Column("token_hash", sa.String(64), nullable=False, unique=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("revoked_at", sa.DateTime(timezone=True)),
    )
    op.create_index("ix_login_session_user_id", "login_session", ["user_id"])
    op.create_table("login_throttle",
        sa.Column("identifier_hash", sa.String(64), primary_key=True),
        sa.Column("failures", sa.Integer(), nullable=False),
        sa.Column("window_started_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("blocked_until", sa.DateTime(timezone=True)),
    )
    op.create_table("audit_event",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("actor_id", sa.String(36), sa.ForeignKey("app_user.id")),
        sa.Column("project_id", sa.String(36), sa.ForeignKey("project.id")),
        sa.Column("action", sa.String(80), nullable=False),
        sa.Column("target_type", sa.String(40)),
        sa.Column("target_id", sa.String(36)),
        sa.Column("outcome", sa.String(24), nullable=False),
        sa.Column("details", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )


def downgrade():
    for name in ("audit_event", "login_throttle", "login_session", "user_invitation",
                 "project_member", "project", "role_permission", "permission", "role", "app_user"):
        op.drop_table(name)
