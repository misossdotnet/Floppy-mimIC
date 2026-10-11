"""AGENT-12 : conversations et receipts dans un schéma dédié.

Le SQL est figé ; aucun import des modèles évolutifs pendant la migration.
"""
from alembic import op
import sqlalchemy as sa

revision = "0018_agent_conversations"
down_revision = "0017_llm_run_traceability"
branch_labels = None
depends_on = None


def capabilities(add):
    connection = op.get_bind()
    table = sa.table("skill_profile", sa.column("id", sa.String), sa.column("allowed_capabilities", sa.JSON))
    for row in connection.execute(sa.select(table)).mappings():
        caps = set(row["allowed_capabilities"])
        if add and "message.send" in caps:
            caps.update(("message.read", "message.ack"))
        elif not add:
            caps.difference_update(("message.read", "message.ack"))
        connection.execute(table.update().where(table.c.id == row["id"]).values(allowed_capabilities=sorted(caps)))


def upgrade():
    op.execute("CREATE SCHEMA IF NOT EXISTS conversation")
    op.execute("""CREATE TABLE conversation.coordination_channel (
	id VARCHAR(36) NOT NULL, 
	project_id VARCHAR(36) NOT NULL, 
	plan_version_id VARCHAR(36) NOT NULL, 
	team_id VARCHAR(36) NOT NULL, 
	task_id VARCHAR(36) NOT NULL, 
	kind VARCHAR(16) NOT NULL, 
	participant_profile_ids JSON NOT NULL, 
	attachment JSON, 
	created_by_session_id VARCHAR(36) NOT NULL, 
	created_at TIMESTAMP WITH TIME ZONE NOT NULL, 
	PRIMARY KEY (id), 
	CONSTRAINT uq_channel_scope UNIQUE (id, project_id, plan_version_id, team_id), 
	CONSTRAINT ck_channel_kind CHECK (kind IN ('direct','broadcast','thread')), 
	CONSTRAINT fk_channel_task_scope FOREIGN KEY(task_id, plan_version_id, project_id, team_id) REFERENCES public.agent_task (id, plan_version_id, project_id, team_id), 
	FOREIGN KEY(project_id) REFERENCES public.project (id), 
	FOREIGN KEY(plan_version_id) REFERENCES public.agent_plan_version (id), 
	FOREIGN KEY(team_id) REFERENCES public.agent_team (id), 
	FOREIGN KEY(created_by_session_id) REFERENCES public.agent_session (id)
)""")
    op.execute("CREATE INDEX ix_channel_project_created ON conversation.coordination_channel (project_id, created_at)")
    op.execute("""CREATE TABLE conversation.agent_message (
	id VARCHAR(36) NOT NULL, 
	project_id VARCHAR(36) NOT NULL, 
	plan_version_id VARCHAR(36) NOT NULL, 
	team_id VARCHAR(36) NOT NULL, 
	channel_id VARCHAR(36) NOT NULL, 
	sender_session_id VARCHAR(36) NOT NULL, 
	sender_profile_id VARCHAR(36) NOT NULL, 
	task_run_id VARCHAR(36) NOT NULL, 
	request_key VARCHAR(36) NOT NULL, 
	request_fingerprint VARCHAR(64) NOT NULL, 
	reply_to_id VARCHAR(36), 
	kind VARCHAR(16) NOT NULL, 
	target_task_id VARCHAR(36), 
	summary VARCHAR(1000) NOT NULL, 
	content VARCHAR(4000) NOT NULL, 
	artifact_refs JSON NOT NULL, 
	priority VARCHAR(2) NOT NULL, 
	created_at TIMESTAMP WITH TIME ZONE NOT NULL, 
	expires_at TIMESTAMP WITH TIME ZONE NOT NULL, 
	PRIMARY KEY (id), 
	CONSTRAINT uq_message_request UNIQUE (sender_session_id, request_key), 
	CONSTRAINT uq_message_channel UNIQUE (id, channel_id), 
	CONSTRAINT uq_message_project UNIQUE (id, project_id), 
	CONSTRAINT fk_message_channel_scope FOREIGN KEY(channel_id, project_id, plan_version_id, team_id) REFERENCES conversation.coordination_channel (id, project_id, plan_version_id, team_id), 
	CONSTRAINT fk_message_reply_channel FOREIGN KEY(reply_to_id, channel_id) REFERENCES conversation.agent_message (id, channel_id), 
	CONSTRAINT fk_handoff_task_scope FOREIGN KEY(target_task_id, plan_version_id, project_id, team_id) REFERENCES public.agent_task (id, plan_version_id, project_id, team_id), 
	CONSTRAINT ck_message_kind CHECK (kind IN ('message','handoff')), 
	CONSTRAINT ck_message_handoff CHECK ((kind = 'handoff') = (target_task_id IS NOT NULL)), 
	CONSTRAINT ck_message_priority CHECK (priority IN ('P0','P1','P2','P3')), 
	CONSTRAINT ck_message_expiry CHECK (expires_at > created_at), 
	FOREIGN KEY(project_id) REFERENCES public.project (id), 
	FOREIGN KEY(plan_version_id) REFERENCES public.agent_plan_version (id), 
	FOREIGN KEY(team_id) REFERENCES public.agent_team (id), 
	FOREIGN KEY(sender_session_id) REFERENCES public.agent_session (id), 
	FOREIGN KEY(sender_profile_id) REFERENCES public.skill_profile (id), 
	FOREIGN KEY(task_run_id) REFERENCES public.agent_task_run (id)
)""")
    op.execute("CREATE INDEX ix_message_channel_created ON conversation.agent_message (channel_id, created_at, id)")
    op.execute("""CREATE TABLE conversation.agent_message_delivery (
	id VARCHAR(36) NOT NULL, 
	project_id VARCHAR(36) NOT NULL, 
	message_id VARCHAR(36) NOT NULL, 
	recipient_profile_id VARCHAR(36) NOT NULL, 
	status VARCHAR(16) NOT NULL, 
	delivered_at TIMESTAMP WITH TIME ZONE, 
	delivered_by_session_id VARCHAR(36), 
	acknowledged_at TIMESTAMP WITH TIME ZONE, 
	acknowledged_by_session_id VARCHAR(36), 
	PRIMARY KEY (id), 
	CONSTRAINT uq_delivery_recipient UNIQUE (message_id, recipient_profile_id), 
	CONSTRAINT fk_delivery_project FOREIGN KEY(message_id, project_id) REFERENCES conversation.agent_message (id, project_id), 
	CONSTRAINT ck_delivery_status CHECK (status IN ('pending','delivered','acknowledged','expired')), 
	CONSTRAINT ck_delivery_actor CHECK ((delivered_at IS NULL) = (delivered_by_session_id IS NULL)), 
	CONSTRAINT ck_ack_actor CHECK ((acknowledged_at IS NULL) = (acknowledged_by_session_id IS NULL)), 
	CONSTRAINT ck_delivery_pending CHECK (status <> 'pending' OR delivered_at IS NULL), 
	CONSTRAINT ck_delivery_received CHECK (status NOT IN ('delivered','acknowledged') OR delivered_at IS NOT NULL), 
	CONSTRAINT ck_delivery_ack CHECK ((status = 'acknowledged') = (acknowledged_at IS NOT NULL)), 
	FOREIGN KEY(project_id) REFERENCES public.project (id), 
	FOREIGN KEY(recipient_profile_id) REFERENCES public.skill_profile (id), 
	FOREIGN KEY(delivered_by_session_id) REFERENCES public.agent_session (id), 
	FOREIGN KEY(acknowledged_by_session_id) REFERENCES public.agent_session (id)
)""")
    op.execute("CREATE INDEX ix_delivery_inbox ON conversation.agent_message_delivery (project_id, recipient_profile_id, status)")
    capabilities(True)


def downgrade():
    # Destructif pour la coordination uniquement, base jetable/restauration coordonnée.
    op.drop_table("agent_message_delivery", schema="conversation")
    op.drop_table("agent_message", schema="conversation")
    op.drop_table("coordination_channel", schema="conversation")
    capabilities(False)
    # Conserver le schéma : il peut être préexistant ou contenir d'autres objets.
