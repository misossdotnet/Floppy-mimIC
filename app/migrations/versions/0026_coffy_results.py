"""COFFY-04 : résultats versionnés, commentaires, avis et droits humains."""

from alembic import op
import sqlalchemy as sa

revision = "0026_coffy_results"
down_revision = "0025_coffy_execution"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column("scheduled_llm_run", sa.Column("result_current_version", sa.Integer(),
                                                  nullable=False, server_default="0"))
    op.create_table("scheduled_llm_result_version",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("run_id", sa.String(36), sa.ForeignKey("scheduled_llm_run.id"), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("parent_version_id", sa.String(36), sa.ForeignKey("scheduled_llm_result_version.id")),
        sa.Column("content", sa.Text(), nullable=False),
        sa.Column("status", sa.String(24), nullable=False),
        sa.Column("actor_id", sa.String(36), sa.ForeignKey("app_user.id")),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("run_id", "version", name="uq_coffy_result_version"),
        sa.CheckConstraint("version > 0", name="ck_coffy_result_version_positive"),
        sa.CheckConstraint("status IN ('Brouillon IA','À revoir','Validé','Rejeté')", name="ck_coffy_result_status"))
    op.create_index("ix_scheduled_llm_result_version_run_id", "scheduled_llm_result_version", ["run_id"])
    op.create_table("scheduled_llm_result_comment",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("result_version_id", sa.String(36), sa.ForeignKey("scheduled_llm_result_version.id"), nullable=False),
        sa.Column("author_id", sa.String(36), sa.ForeignKey("app_user.id"), nullable=False),
        sa.Column("body", sa.Text(), nullable=False),
        sa.Column("status", sa.String(16), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("resolved_by", sa.String(36), sa.ForeignKey("app_user.id")),
        sa.Column("resolved_at", sa.DateTime(timezone=True)),
        sa.CheckConstraint("status IN ('open','resolved')", name="ck_coffy_comment_status"))
    op.create_index("ix_scheduled_llm_result_comment_result_version_id", "scheduled_llm_result_comment", ["result_version_id"])
    op.create_table("scheduled_llm_feedback",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("result_version_id", sa.String(36), sa.ForeignKey("scheduled_llm_result_version.id"), nullable=False),
        sa.Column("author_id", sa.String(36), sa.ForeignKey("app_user.id"), nullable=False),
        sa.Column("score", sa.Integer(), nullable=False),
        sa.Column("note", sa.String(2000), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("result_version_id", "author_id", name="uq_coffy_feedback_author_version"),
        sa.CheckConstraint("score BETWEEN 1 AND 5", name="ck_coffy_feedback_score"))
    op.create_index("ix_scheduled_llm_feedback_result_version_id", "scheduled_llm_feedback", ["result_version_id"])
    op.execute("""INSERT INTO scheduled_llm_result_version
        (id, run_id, version, parent_version_id, content, status, actor_id, created_at)
        SELECT gen_random_uuid()::text, id, 1, NULL, raw_output, 'Brouillon IA', NULL,
               COALESCE(completed_at, now()) FROM scheduled_llm_run
        WHERE status='success' AND raw_output IS NOT NULL""")
    op.execute("""UPDATE scheduled_llm_run SET result_current_version=1
        WHERE status='success' AND raw_output IS NOT NULL""")
    for code in ("scheduled_task.result.edit", "scheduled_task.feedback", "scheduled_task.review"):
        op.execute(sa.text("INSERT INTO permission(code) VALUES (:code) ON CONFLICT DO NOTHING").bindparams(code=code))
        op.execute(sa.text("""INSERT INTO role_permission(role_code, permission_code)
            SELECT code, :permission FROM role WHERE code IN ('product_owner','project_manager','architect')
            ON CONFLICT DO NOTHING""").bindparams(permission=code))


def downgrade():
    op.execute("DELETE FROM role_permission WHERE permission_code IN ('scheduled_task.result.edit','scheduled_task.feedback','scheduled_task.review')")
    op.execute("DELETE FROM permission WHERE code IN ('scheduled_task.result.edit','scheduled_task.feedback','scheduled_task.review')")
    op.drop_index("ix_scheduled_llm_feedback_result_version_id", table_name="scheduled_llm_feedback")
    op.drop_table("scheduled_llm_feedback")
    op.drop_index("ix_scheduled_llm_result_comment_result_version_id", table_name="scheduled_llm_result_comment")
    op.drop_table("scheduled_llm_result_comment")
    op.drop_index("ix_scheduled_llm_result_version_run_id", table_name="scheduled_llm_result_version")
    op.drop_table("scheduled_llm_result_version")
    op.drop_column("scheduled_llm_run", "result_current_version")
