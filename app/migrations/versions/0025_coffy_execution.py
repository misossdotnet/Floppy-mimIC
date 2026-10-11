"""COFFY-03 : occurrences, runs et appels LLM rattachés aux runs CoFFY."""

from alembic import op
import sqlalchemy as sa

revision = "0025_coffy_execution"
down_revision = "0024_coffy_definitions"
branch_labels = None
depends_on = None


def upgrade():
    op.create_index("ix_coffy_task_due", "scheduled_llm_task", ["status", "next_run_at"])
    op.create_table("scheduled_llm_occurrence",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("project_id", sa.String(36), sa.ForeignKey("project.id"), nullable=False),
        sa.Column("task_id", sa.String(36), sa.ForeignKey("scheduled_llm_task.id"), nullable=False),
        sa.Column("task_version_id", sa.String(36), sa.ForeignKey("scheduled_llm_task_version.id"), nullable=False),
        sa.Column("scheduled_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("origin", sa.String(12), nullable=False),
        sa.Column("request_key", sa.String(36)),
        sa.Column("status", sa.String(12), nullable=False),
        sa.Column("skip_reason", sa.String(40)),
        sa.Column("coalesced_count", sa.Integer(), nullable=False),
        sa.Column("count_truncated", sa.Boolean(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("task_id", "task_version_id", "scheduled_at", name="uq_coffy_scheduled_occurrence"),
        sa.UniqueConstraint("task_id", "request_key", name="uq_coffy_manual_request"),
        sa.CheckConstraint("origin IN ('scheduled','manual')", name="ck_coffy_occurrence_origin"),
        sa.CheckConstraint("status IN ('reserved','executed','skipped')", name="ck_coffy_occurrence_status"),
        sa.CheckConstraint("coalesced_count >= 0", name="ck_coffy_occurrence_count"),
    )
    op.create_index("ix_scheduled_llm_occurrence_project_id", "scheduled_llm_occurrence", ["project_id"])
    op.create_index("ix_scheduled_llm_occurrence_task_id", "scheduled_llm_occurrence", ["task_id"])
    op.create_table("scheduled_llm_run",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("occurrence_id", sa.String(36), sa.ForeignKey("scheduled_llm_occurrence.id"), nullable=False, unique=True),
        sa.Column("project_id", sa.String(36), sa.ForeignKey("project.id"), nullable=False),
        sa.Column("task_id", sa.String(36), sa.ForeignKey("scheduled_llm_task.id"), nullable=False),
        sa.Column("task_version_id", sa.String(36), sa.ForeignKey("scheduled_llm_task_version.id"), nullable=False),
        sa.Column("llm_profile_version_id", sa.String(36), sa.ForeignKey("llm_profile_version.id"), nullable=False),
        sa.Column("requested_by", sa.String(36), sa.ForeignKey("app_user.id")),
        sa.Column("status", sa.String(12), nullable=False),
        sa.Column("error_code", sa.String(40)),
        sa.Column("parameters", sa.JSON(), nullable=False),
        sa.Column("pricing_snapshot", sa.JSON()),
        sa.Column("raw_output", sa.Text()),
        sa.Column("input_tokens", sa.Integer()),
        sa.Column("output_tokens", sa.Integer()),
        sa.Column("estimated_cost", sa.Numeric(12, 6)),
        sa.Column("cost_currency", sa.String(3)),
        sa.Column("pricing_source", sa.String(120)),
        sa.Column("pricing_date", sa.Date()),
        sa.Column("dispatch_attempts", sa.Integer(), nullable=False),
        sa.Column("last_dispatched_at", sa.DateTime(timezone=True)),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("started_at", sa.DateTime(timezone=True)),
        sa.Column("completed_at", sa.DateTime(timezone=True)),
        sa.CheckConstraint("status IN ('pending','queued','running','retrying','success','failed','timeout','cancelled')", name="ck_coffy_run_status"),
    )
    op.create_index("uq_coffy_active_run", "scheduled_llm_run", ["task_id"], unique=True,
                    postgresql_where=sa.text("status IN ('pending','queued','running','retrying')"))
    op.create_index("ix_coffy_run_project_created", "scheduled_llm_run", ["project_id", "created_at"])
    op.add_column("llm_call_attempt", sa.Column("scheduled_llm_run_id", sa.String(36), nullable=True))
    op.create_foreign_key("fk_llm_call_coffy_run", "llm_call_attempt", "scheduled_llm_run",
                          ["scheduled_llm_run_id"], ["id"])
    op.create_index("ix_llm_call_attempt_scheduled_llm_run_id", "llm_call_attempt", ["scheduled_llm_run_id"])
    op.create_unique_constraint("uq_llm_coffy_call", "llm_call_attempt", ["scheduled_llm_run_id", "attempt_number"])
    op.drop_constraint("ck_llm_call_owner", "llm_call_attempt", type_="check")
    op.create_check_constraint("ck_llm_call_owner", "llm_call_attempt",
        "(CASE WHEN analysis_run_id IS NOT NULL THEN 1 ELSE 0 END + "
        "CASE WHEN generation_run_id IS NOT NULL THEN 1 ELSE 0 END + "
        "CASE WHEN scheduled_llm_run_id IS NOT NULL THEN 1 ELSE 0 END) = 1")
    op.execute("INSERT INTO permission(code) VALUES ('scheduled_task.run') ON CONFLICT DO NOTHING")
    op.execute("INSERT INTO role_permission(role_code,permission_code) SELECT code, 'scheduled_task.run' FROM role WHERE code='project_manager' ON CONFLICT DO NOTHING")


def downgrade():
    op.execute("DELETE FROM role_permission WHERE permission_code='scheduled_task.run'")
    op.execute("DELETE FROM permission WHERE code='scheduled_task.run'")
    op.execute("DELETE FROM runtime_usage WHERE call_id IN (SELECT id FROM llm_call_attempt WHERE scheduled_llm_run_id IS NOT NULL)")
    op.execute("DELETE FROM llm_call_attempt WHERE scheduled_llm_run_id IS NOT NULL")
    op.execute("UPDATE scheduled_llm_task SET status='paused', next_run_at=NULL WHERE status='active'")
    op.drop_constraint("ck_llm_call_owner", "llm_call_attempt", type_="check")
    op.create_check_constraint("ck_llm_call_owner", "llm_call_attempt",
                               "(analysis_run_id IS NULL) <> (generation_run_id IS NULL)")
    op.drop_constraint("uq_llm_coffy_call", "llm_call_attempt", type_="unique")
    op.drop_index("ix_llm_call_attempt_scheduled_llm_run_id", table_name="llm_call_attempt")
    op.drop_constraint("fk_llm_call_coffy_run", "llm_call_attempt", type_="foreignkey")
    op.drop_column("llm_call_attempt", "scheduled_llm_run_id")
    op.drop_index("ix_coffy_run_project_created", table_name="scheduled_llm_run")
    op.drop_index("uq_coffy_active_run", table_name="scheduled_llm_run")
    op.drop_table("scheduled_llm_run")
    op.drop_index("ix_scheduled_llm_occurrence_task_id", table_name="scheduled_llm_occurrence")
    op.drop_index("ix_scheduled_llm_occurrence_project_id", table_name="scheduled_llm_occurrence")
    op.drop_table("scheduled_llm_occurrence")
    op.drop_index("ix_coffy_task_due", table_name="scheduled_llm_task")
