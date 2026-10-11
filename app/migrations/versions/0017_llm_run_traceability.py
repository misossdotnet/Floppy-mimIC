"""LLM-08/09 : appels fournisseur, snapshots d'analyse et délégation agentique."""
from alembic import op
import sqlalchemy as sa

revision = "0017_llm_run_traceability"
down_revision = "0016_project_memory"
branch_labels = None
depends_on = None


def upgrade():
    for name, type_ in (("prompt_template", sa.Text()), ("context_snapshot", sa.JSON()),
                        ("parameters", sa.JSON()), ("pricing_snapshot", sa.JSON()),
                        ("estimated_cost", sa.Numeric(12, 6)), ("cost_currency", sa.String(3)),
                        ("pricing_source", sa.String(120)), ("pricing_date", sa.Date())):
        op.add_column("analysis_run", sa.Column(name, type_, nullable=True))
    op.add_column("generation_run", sa.Column("pricing_snapshot", sa.JSON(), nullable=True))
    op.add_column("agent_task_run", sa.Column("llm_profile_version_id", sa.String(36),
                  sa.ForeignKey("llm_profile_version.id"), nullable=True))
    op.add_column("agent_task_run", sa.Column("generation_run_id", sa.String(36),
                  sa.ForeignKey("generation_run.id"), nullable=True))
    op.create_unique_constraint("uq_agent_task_generation_run", "agent_task_run", ["generation_run_id"])
    op.create_table("llm_call_attempt",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("analysis_run_id", sa.String(36), sa.ForeignKey("analysis_run.id")),
        sa.Column("generation_run_id", sa.String(36), sa.ForeignKey("generation_run.id")),
        sa.Column("profile_version_id", sa.String(36), sa.ForeignKey("llm_profile_version.id"), nullable=False),
        sa.Column("attempt_number", sa.Integer(), nullable=False),
        sa.Column("messages", sa.JSON(), nullable=False), sa.Column("parameters", sa.JSON(), nullable=False),
        sa.Column("pricing_snapshot", sa.JSON()), sa.Column("status", sa.String(16), nullable=False),
        sa.Column("input_tokens", sa.Integer()), sa.Column("output_tokens", sa.Integer()),
        sa.Column("estimated_cost", sa.Numeric(12, 6)), sa.Column("error_code", sa.String(40)),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("ended_at", sa.DateTime(timezone=True)),
        sa.CheckConstraint("(analysis_run_id IS NULL) <> (generation_run_id IS NULL)", name="ck_llm_call_owner"),
        sa.CheckConstraint("status IN ('running', 'completed', 'failed')", name="ck_llm_call_status"),
        sa.CheckConstraint("attempt_number > 0", name="ck_llm_call_number"),
        sa.UniqueConstraint("analysis_run_id", "attempt_number", name="uq_llm_analysis_call"),
        sa.UniqueConstraint("generation_run_id", "attempt_number", name="uq_llm_generation_call"))
    for name in ("analysis_run_id", "generation_run_id"):
        op.create_index(f"ix_llm_call_attempt_{name}", "llm_call_attempt", [name])


def downgrade():
    op.drop_table("llm_call_attempt")
    op.drop_column("agent_task_run", "generation_run_id")
    op.drop_column("agent_task_run", "llm_profile_version_id")
    op.drop_column("generation_run", "pricing_snapshot")
    for name in ("prompt_template", "context_snapshot", "parameters", "pricing_snapshot",
                 "estimated_cost", "cost_currency", "pricing_source", "pricing_date"):
        op.drop_column("analysis_run", name)
