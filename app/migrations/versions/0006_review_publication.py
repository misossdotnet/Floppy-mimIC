"""Revue humaine, référence publiée et export privé.

Revision ID: 0006_review_publication
Revises: 0005_document_generation
"""

from alembic import op
import sqlalchemy as sa

revision = "0006_review_publication"
down_revision = "0005_document_generation"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table("deliverable_comment",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("version_id", sa.String(36), sa.ForeignKey("deliverable_version.id"), nullable=False),
        sa.Column("author_id", sa.String(36), sa.ForeignKey("app_user.id"), nullable=False),
        sa.Column("body", sa.Text(), nullable=False),
        sa.Column("status", sa.String(16), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("resolved_by", sa.String(36), sa.ForeignKey("app_user.id")),
        sa.Column("resolved_at", sa.DateTime(timezone=True)),
    )
    op.create_index("ix_deliverable_comment_version_id", "deliverable_comment", ["version_id"])
    op.create_table("publication_record",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("deliverable_id", sa.String(36), sa.ForeignKey("deliverable.id"), nullable=False),
        sa.Column("version_id", sa.String(36), sa.ForeignKey("deliverable_version.id"), nullable=False, unique=True),
        sa.Column("active", sa.Boolean(), nullable=False),
        sa.Column("published_by", sa.String(36), sa.ForeignKey("app_user.id"), nullable=False),
        sa.Column("published_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("replaced_at", sa.DateTime(timezone=True)),
    )
    op.create_index("ix_publication_record_deliverable_id", "publication_record", ["deliverable_id"])
    op.create_index("uq_publication_active", "publication_record", ["deliverable_id"], unique=True,
                    postgresql_where=sa.text("active"))
    op.create_table("export_artifact",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("publication_id", sa.String(36), sa.ForeignKey("publication_record.id"), nullable=False),
        sa.Column("format", sa.String(12), nullable=False),
        sa.Column("status", sa.String(16), nullable=False),
        sa.Column("object_key", sa.String(130), unique=True),
        sa.Column("sha256", sa.String(64)),
        sa.Column("size_bytes", sa.Integer()),
        sa.Column("error_code", sa.String(40)),
        sa.Column("requested_by", sa.String(36), sa.ForeignKey("app_user.id"), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("completed_at", sa.DateTime(timezone=True)),
    )
    op.create_index("ix_export_artifact_publication_id", "export_artifact", ["publication_id"])


def downgrade():
    op.drop_table("export_artifact")
    op.drop_table("publication_record")
    op.drop_table("deliverable_comment")
