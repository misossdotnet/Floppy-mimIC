"""Dépendances, limites approuvées et tentatives techniques du coordinateur.

Revision ID: 0013_agent_task_coordinator
Revises: 0012_agent_task_hierarchy
"""
from uuid import uuid4
from alembic import op
import sqlalchemy as sa

revision = "0013_agent_task_coordinator"
down_revision = "0012_agent_task_hierarchy"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column("agent_plan_version", sa.Column("max_parallel_tasks", sa.Integer(), server_default="4", nullable=False))
    op.add_column("agent_plan_version", sa.Column("max_attempts", sa.Integer(), server_default="3", nullable=False))
    op.create_check_constraint("ck_agent_plan_parallel", "agent_plan_version", "max_parallel_tasks BETWEEN 1 AND 8")
    op.create_check_constraint("ck_agent_plan_attempts", "agent_plan_version", "max_attempts BETWEEN 1 AND 5")
    op.add_column("agent_task", sa.Column("dependency_keys", sa.JSON(), server_default="[]", nullable=False))
    op.add_column("agent_task", sa.Column("attempt_count", sa.Integer(), server_default="0", nullable=False))
    op.add_column("agent_task", sa.Column("retry_not_before", sa.DateTime(timezone=True)))
    op.add_column("agent_task", sa.Column("retry_from_run_id", sa.String(36)))
    op.create_check_constraint("ck_agent_task_attempt_count", "agent_task", "attempt_count >= 0")
    op.drop_constraint("ck_agent_task_status", "agent_task", type_="check")
    op.create_check_constraint("ck_agent_task_status", "agent_task",
        "status IN ('ready','claimed','running','completed','cancelled','failed')")
    op.drop_constraint("ck_flow_run_status", "flow_run", type_="check")
    op.create_check_constraint("ck_flow_run_status", "flow_run",
        "status IN ('waiting_human','running','paused','completed','cancelled')")
    op.add_column("agent_task_claim", sa.Column("request_key", sa.String(36)))
    op.create_unique_constraint("uq_agent_claim_request", "agent_task_claim", ["session_id", "request_key"])
    op.create_table("agent_task_run",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("task_id", sa.String(36), sa.ForeignKey("agent_task.id"), nullable=False),
        sa.Column("claim_id", sa.String(36), sa.ForeignKey("agent_task_claim.id"), nullable=False),
        sa.Column("parent_run_id", sa.String(36), sa.ForeignKey("agent_task_run.id")),
        sa.Column("attempt_number", sa.Integer(), nullable=False),
        sa.Column("status", sa.String(16), nullable=False),
        sa.Column("error_code", sa.String(24)),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("ended_at", sa.DateTime(timezone=True)),
        sa.UniqueConstraint("task_id", "attempt_number", name="uq_agent_task_run_attempt"),
        sa.UniqueConstraint("claim_id", name="uq_agent_task_run_claim"),
        sa.CheckConstraint("attempt_number > 0", name="ck_agent_task_run_attempt"),
        sa.CheckConstraint("status IN ('running','completed','failed','cancelled')", name="ck_agent_task_run_status"))
    op.create_index("ix_agent_task_run_task_id", "agent_task_run", ["task_id"])
    # Historiques préexistants conservés : compteur réel, même s'il dépasse le défaut.
    connection = op.get_bind()
    attempts = {}
    previous = {}
    runs = sa.table("agent_task_run", *(sa.column(name) for name in (
        "id", "task_id", "claim_id", "parent_run_id", "attempt_number", "status", "error_code", "started_at", "ended_at")))
    claims = connection.execute(sa.text("SELECT * FROM agent_task_claim ORDER BY task_id, claimed_at, id")).mappings().all()
    for claim in claims:
        tid = claim["task_id"]
        attempts[tid] = attempts.get(tid, 0) + 1
        reason = claim["release_reason"]
        status = "running" if claim["released_at"] is None else ("completed" if reason == "completed" else "cancelled")
        rid = str(uuid4())
        connection.execute(runs.insert().values(id=rid, task_id=tid, claim_id=claim["id"],
            parent_run_id=previous.get(tid), attempt_number=attempts[tid], status=status,
            started_at=claim["claimed_at"], ended_at=claim["released_at"]))
        previous[tid] = rid
    for tid, count in attempts.items():
        connection.execute(sa.text("UPDATE agent_task SET attempt_count = :count WHERE id = :id"), {"count": count, "id": tid})


def downgrade():
    # Retour destructif réservé à une base jetable/restauration coordonnée.
    op.drop_index("ix_agent_task_run_task_id", table_name="agent_task_run")
    op.drop_table("agent_task_run")
    op.drop_constraint("uq_agent_claim_request", "agent_task_claim", type_="unique")
    op.drop_column("agent_task_claim", "request_key")
    op.execute("UPDATE agent_task SET status = 'ready' WHERE status = 'failed'")
    op.execute("UPDATE flow_run SET status = 'running' WHERE status = 'paused'")
    op.drop_constraint("ck_agent_task_status", "agent_task", type_="check")
    op.create_check_constraint("ck_agent_task_status", "agent_task", "status IN ('ready','claimed','running','completed','cancelled')")
    op.drop_constraint("ck_flow_run_status", "flow_run", type_="check")
    op.create_check_constraint("ck_flow_run_status", "flow_run", "status IN ('waiting_human','running','completed','cancelled')")
    op.drop_constraint("ck_agent_task_attempt_count", "agent_task", type_="check")
    for name in ("dependency_keys", "attempt_count", "retry_not_before", "retry_from_run_id"):
        op.drop_column("agent_task", name)
    for name in ("parallel", "attempts"):
        op.drop_constraint(f"ck_agent_plan_{name}", "agent_plan_version", type_="check")
    op.drop_column("agent_plan_version", "max_parallel_tasks")
    op.drop_column("agent_plan_version", "max_attempts")
