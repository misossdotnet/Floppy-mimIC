"""COFFY-02 : définitions et sujets versionnés, sans ordonnanceur."""

from alembic import op
import sqlalchemy as sa

revision = "0024_coffy_definitions"
down_revision = "0023_organization_scope"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table("scheduled_llm_task",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("project_id", sa.String(36), sa.ForeignKey("project.id"), nullable=False),
        sa.Column("created_by", sa.String(36), sa.ForeignKey("app_user.id"), nullable=False),
        sa.Column("status", sa.String(16), nullable=False),
        sa.Column("current_version", sa.Integer(), nullable=False),
        sa.Column("revision", sa.Integer(), nullable=False),
        sa.Column("next_run_at", sa.DateTime(timezone=True)),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint("status IN ('draft','active','paused','archived','deleted')", name="ck_scheduled_task_status"),
    )
    op.create_index("ix_scheduled_task_project_status", "scheduled_llm_task", ["project_id", "status"])
    op.create_table("scheduled_llm_subject",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("project_id", sa.String(36), sa.ForeignKey("project.id"), nullable=False),
        sa.Column("title", sa.String(120), nullable=False),
        sa.Column("description", sa.String(1000), nullable=False),
        sa.Column("status", sa.String(16), nullable=False),
        sa.Column("revision", sa.Integer(), nullable=False),
        sa.Column("created_by", sa.String(36), sa.ForeignKey("app_user.id"), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint("status IN ('active','inactive','archived','deleted')", name="ck_scheduled_subject_status"),
        sa.UniqueConstraint("project_id", "id", name="uq_scheduled_subject_project_id"),
    )
    op.create_index("ix_scheduled_llm_subject_project_id", "scheduled_llm_subject", ["project_id"])
    op.create_table("scheduled_llm_task_version",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("task_id", sa.String(36), sa.ForeignKey("scheduled_llm_task.id"), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("title", sa.String(160), nullable=False),
        sa.Column("short_description", sa.String(500), nullable=False),
        sa.Column("long_description", sa.Text(), nullable=False),
        sa.Column("system_prompt", sa.Text(), nullable=False),
        sa.Column("user_prompt", sa.Text(), nullable=False),
        sa.Column("result_domain", sa.String(16), nullable=False),
        sa.Column("llm_profile_version_id", sa.String(36), sa.ForeignKey("llm_profile_version.id"), nullable=False),
        sa.Column("parameters", sa.JSON(), nullable=False),
        sa.Column("strict_json", sa.Boolean(), nullable=False),
        sa.Column("schedule_mode", sa.String(16), nullable=False),
        sa.Column("schedule_expression", sa.String(160)),
        sa.Column("timezone_name", sa.String(80)),
        sa.Column("subject_ids", sa.JSON(), nullable=False),
        sa.Column("created_by", sa.String(36), sa.ForeignKey("app_user.id"), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("task_id", "version", name="uq_scheduled_task_version"),
        sa.CheckConstraint("result_domain IN ('business','technical','pilotage')", name="ck_scheduled_task_domain"),
        sa.CheckConstraint("schedule_mode IN ('manual','once','interval','cron')", name="ck_scheduled_task_schedule"),
    )
    op.create_index("ix_scheduled_llm_task_version_task_id", "scheduled_llm_task_version", ["task_id"])
    op.create_table("scheduled_llm_version_subject",
        sa.Column("version_id", sa.String(36), sa.ForeignKey("scheduled_llm_task_version.id"), primary_key=True),
        sa.Column("subject_id", sa.String(36), sa.ForeignKey("scheduled_llm_subject.id"), primary_key=True),
    )
    op.create_table("scheduled_llm_subject_revision",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("subject_id", sa.String(36), sa.ForeignKey("scheduled_llm_subject.id"), nullable=False),
        sa.Column("revision", sa.Integer(), nullable=False),
        sa.Column("title", sa.String(120), nullable=False),
        sa.Column("description", sa.String(1000), nullable=False),
        sa.Column("status", sa.String(16), nullable=False),
        sa.Column("actor_id", sa.String(36), sa.ForeignKey("app_user.id"), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("subject_id", "revision", name="uq_scheduled_subject_revision"),
    )
    op.create_index("ix_scheduled_llm_subject_revision_subject_id", "scheduled_llm_subject_revision", ["subject_id"])
    for code in ("scheduled_task.read", "scheduled_task.manage", "scheduled_task.subject.manage"):
        op.execute(sa.text("INSERT INTO permission (code) VALUES (:code) ON CONFLICT DO NOTHING").bindparams(code=code))
    for role in ("product_owner", "project_manager", "architect", "reader"):
        op.execute(sa.text("INSERT INTO role_permission (role_code, permission_code) SELECT code, 'scheduled_task.read' FROM role WHERE code=:role ON CONFLICT DO NOTHING").bindparams(role=role))
    for code in ("scheduled_task.manage", "scheduled_task.subject.manage"):
        op.execute(sa.text("INSERT INTO role_permission (role_code, permission_code) SELECT code, :code FROM role WHERE code='project_manager' ON CONFLICT DO NOTHING").bindparams(code=code))


def downgrade():
    op.drop_index("ix_scheduled_llm_subject_revision_subject_id", table_name="scheduled_llm_subject_revision")
    op.drop_table("scheduled_llm_subject_revision")
    op.drop_table("scheduled_llm_version_subject")
    op.drop_index("ix_scheduled_llm_task_version_task_id", table_name="scheduled_llm_task_version")
    op.drop_table("scheduled_llm_task_version")
    op.drop_index("ix_scheduled_llm_subject_project_id", table_name="scheduled_llm_subject")
    op.drop_table("scheduled_llm_subject")
    op.drop_index("ix_scheduled_task_project_status", table_name="scheduled_llm_task")
    op.drop_table("scheduled_llm_task")
    op.execute("DELETE FROM role_permission WHERE permission_code IN ('scheduled_task.read','scheduled_task.manage','scheduled_task.subject.manage')")
    op.execute("DELETE FROM permission WHERE code IN ('scheduled_task.read','scheduled_task.manage','scheduled_task.subject.manage')")
