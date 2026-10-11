"""Diff asynchrone et provenance des régénérations.

Revision ID: 0007_versioning_regeneration
Revises: 0006_review_publication
"""

from alembic import op
import sqlalchemy as sa

revision = "0007_versioning_regeneration"
down_revision = "0006_review_publication"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column("generation_run", sa.Column("parent_version_id", sa.String(36),
                                                sa.ForeignKey("deliverable_version.id",
                                                              name="generation_run_parent_version_id_fkey",
                                                              use_alter=True)))
    op.add_column("generation_run", sa.Column("reason", sa.Text()))
    op.add_column("generation_run", sa.Column("prompt_adjustment", sa.Text()))
    op.create_index("ix_generation_run_parent_version_id", "generation_run", ["parent_version_id"])
    op.create_table("diff_run",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("project_id", sa.String(36), sa.ForeignKey("project.id"), nullable=False),
        sa.Column("left_version_id", sa.String(36), sa.ForeignKey("deliverable_version.id"), nullable=False),
        sa.Column("right_version_id", sa.String(36), sa.ForeignKey("deliverable_version.id"), nullable=False),
        sa.Column("requested_by", sa.String(36), sa.ForeignKey("app_user.id"), nullable=False),
        sa.Column("status", sa.String(16), nullable=False),
        sa.Column("result", sa.Text()),
        sa.Column("error_code", sa.String(40)),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("completed_at", sa.DateTime(timezone=True)),
        sa.CheckConstraint("left_version_id <> right_version_id", name="ck_diff_distinct"),
    )
    op.create_index("ix_diff_run_project_id", "diff_run", ["project_id"])


def downgrade():
    op.drop_table("diff_run")
    op.drop_index("ix_generation_run_parent_version_id", table_name="generation_run")
    op.drop_column("generation_run", "prompt_adjustment")
    op.drop_column("generation_run", "reason")
    op.drop_column("generation_run", "parent_version_id")
