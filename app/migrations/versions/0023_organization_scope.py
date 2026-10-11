"""AGENT-27 : rattachement organisationnel sans héritage de permissions."""

from alembic import op
import sqlalchemy as sa


revision = "0023_organization_scope"
down_revision = "0022_execution_budgets"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table("organization",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("name", sa.String(120), nullable=False),
        sa.Column("parent_id", sa.String(36), sa.ForeignKey("organization.id")),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint("parent_id IS NULL OR parent_id <> id", name="ck_organization_not_self"),
    )
    op.create_index("ix_organization_parent_id", "organization", ["parent_id"])
    op.add_column("project", sa.Column("organization_id", sa.String(36), nullable=True))
    op.create_foreign_key("fk_project_organization", "project", "organization", ["organization_id"], ["id"])
    op.create_index("ix_project_organization_id", "project", ["organization_id"])


def downgrade():
    op.drop_index("ix_project_organization_id", table_name="project")
    op.drop_constraint("fk_project_organization", "project", type_="foreignkey")
    op.drop_column("project", "organization_id")
    op.drop_index("ix_organization_parent_id", table_name="organization")
    op.drop_table("organization")
