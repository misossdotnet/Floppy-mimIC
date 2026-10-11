"""Relations, retrait logique et commentaires du référentiel.

Revision ID: 0004_context_review
Revises: 0003_sources_context
"""

from alembic import op
import sqlalchemy as sa

revision = "0004_context_review"
down_revision = "0003_sources_context"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table("context_relation",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("project_id", sa.String(36), sa.ForeignKey("project.id"), nullable=False),
        sa.Column("source_id", sa.String(36), nullable=False),
        sa.Column("target_id", sa.String(36), nullable=False),
        sa.Column("type", sa.String(32), nullable=False),
        sa.Column("is_active", sa.Boolean(), nullable=False),
        sa.Column("created_by", sa.String(36), sa.ForeignKey("app_user.id"), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("removed_by", sa.String(36), sa.ForeignKey("app_user.id")),
        sa.Column("removed_at", sa.DateTime(timezone=True)),
        sa.ForeignKeyConstraint(["source_id", "project_id"], ["context_item.id", "context_item.project_id"], name="fk_context_relation_source_project"),
        sa.ForeignKeyConstraint(["target_id", "project_id"], ["context_item.id", "context_item.project_id"], name="fk_context_relation_target_project"),
        sa.CheckConstraint("source_id <> target_id", name="ck_context_relation_distinct"),
    )
    for column in ("project_id", "source_id", "target_id"):
        op.create_index(f"ix_context_relation_{column}", "context_relation", [column])
    op.create_index("uq_context_relation_active", "context_relation", ["source_id", "target_id", "type"],
                    unique=True, postgresql_where=sa.text("is_active"))
    op.create_table("context_comment",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("item_version_id", sa.String(36), sa.ForeignKey("context_item_version.id"), nullable=False),
        sa.Column("author_id", sa.String(36), sa.ForeignKey("app_user.id"), nullable=False),
        sa.Column("body", sa.Text(), nullable=False),
        sa.Column("status", sa.String(16), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("resolved_by", sa.String(36), sa.ForeignKey("app_user.id")),
        sa.Column("resolved_at", sa.DateTime(timezone=True)),
        sa.CheckConstraint("status IN ('open', 'resolved')", name="ck_context_comment_status"),
    )
    op.create_index("ix_context_comment_item_version_id", "context_comment", ["item_version_id"])
    op.execute("INSERT INTO permission (code) VALUES ('context.comment') ON CONFLICT DO NOTHING")
    op.execute("INSERT INTO role_permission (role_code, permission_code) SELECT code, 'context.comment' FROM role WHERE code IN ('product_owner', 'project_manager', 'architect') ON CONFLICT DO NOTHING")


def downgrade():
    op.execute("DELETE FROM role_permission WHERE permission_code = 'context.comment'")
    op.execute("DELETE FROM permission WHERE code = 'context.comment'")
    op.drop_table("context_comment")
    op.drop_table("context_relation")
