"""Profils de compétence versionnés et équipes rattachées aux projets.

Revision ID: 0009_agent_teams_catalog
Revises: 0008_agent_sessions
"""

from datetime import datetime, timezone
from uuid import uuid4

from alembic import op
import sqlalchemy as sa

revision = "0009_agent_teams_catalog"
down_revision = "0008_agent_sessions"
branch_labels = None
depends_on = None

SKILL_LABELS = {"backend": "Backend", "frontend": "Frontend", "qa": "QA",
                "devops": "DevOps", "security": "Sécurité", "tech-lead": "Tech Lead", "cto": "CTO"}
INITIAL_CAPABILITIES = ["message.send", "session.disconnect", "session.heartbeat",
                        "task.claim", "task.release"]


def upgrade():
    op.create_table("skill_profile",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("code", sa.String(40), nullable=False),
        sa.Column("version_number", sa.Integer(), nullable=False),
        sa.Column("label", sa.String(80), nullable=False),
        sa.Column("allowed_capabilities", sa.JSON(), nullable=False),
        sa.Column("is_active", sa.Boolean(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("code", "version_number", name="uq_skill_profile_version"),
    )
    op.create_table("agent_team",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("project_id", sa.String(36), sa.ForeignKey("project.id"), nullable=False),
        sa.Column("name", sa.String(120), nullable=False),
        sa.Column("created_by", sa.String(36), sa.ForeignKey("app_user.id"), nullable=False),
        sa.Column("is_active", sa.Boolean(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("project_id", "name", name="uq_agent_team_project_name"),
    )
    op.create_index("ix_agent_team_project_id", "agent_team", ["project_id"])
    op.create_table("agent_team_skill",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("team_id", sa.String(36), sa.ForeignKey("agent_team.id"), nullable=False),
        sa.Column("skill_profile_id", sa.String(36), sa.ForeignKey("skill_profile.id"), nullable=False),
        sa.UniqueConstraint("team_id", "skill_profile_id", name="uq_agent_team_skill"),
    )
    op.create_index("ix_agent_team_skill_team_id", "agent_team_skill", ["team_id"])
    op.add_column("agent_session", sa.Column("team_id", sa.String(36), sa.ForeignKey("agent_team.id")))
    op.add_column("agent_session", sa.Column("skill_profile_id", sa.String(36), sa.ForeignKey("skill_profile.id")))

    now = datetime.now(timezone.utc)
    profiles = sa.table("skill_profile", sa.column("id", sa.String), sa.column("code", sa.String),
        sa.column("version_number", sa.Integer), sa.column("label", sa.String),
        sa.column("allowed_capabilities", sa.JSON), sa.column("is_active", sa.Boolean),
        sa.column("created_at", sa.DateTime(timezone=True)))
    op.bulk_insert(profiles, [{"id": str(uuid4()), "code": code, "version_number": 1,
        "label": label, "allowed_capabilities": INITIAL_CAPABILITIES,
        "is_active": True, "created_at": now} for code, label in SKILL_LABELS.items()])

    # Les anciens jetons n'ont pas de rattachement d'équipe vérifiable : révocation sûre.
    connection = op.get_bind()
    legacy = connection.execute(sa.text("SELECT id, project_id FROM agent_session WHERE status = 'active'")).all()
    connection.execute(sa.text("UPDATE agent_session SET status = 'revoked', revoked_at = :now WHERE status = 'active'"), {"now": now})
    for session_id, project_id in legacy:
        connection.execute(sa.text("INSERT INTO audit_event "
            "(id, actor_id, project_id, action, target_type, target_id, outcome, details, created_at) "
            "VALUES (:id, NULL, :project_id, 'agent.session.revoke', 'agent_session', :target_id, "
            "'success', :details, :now)"), {"id": str(uuid4()), "project_id": project_id,
                "target_id": session_id, "details": '{}', "now": now})


def downgrade():
    op.drop_column("agent_session", "skill_profile_id")
    op.drop_column("agent_session", "team_id")
    op.drop_index("ix_agent_team_skill_team_id", table_name="agent_team_skill")
    op.drop_table("agent_team_skill")
    op.drop_index("ix_agent_team_project_id", table_name="agent_team")
    op.drop_table("agent_team")
    op.drop_table("skill_profile")
