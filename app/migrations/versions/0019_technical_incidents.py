"""AGENT-13 : incidents, historique et notifications dans conversation ; SQL figé."""
from alembic import op
import sqlalchemy as sa

revision = '0019_technical_incidents'
down_revision = '0018_agent_conversations'
branch_labels = None
depends_on = None

PERMISSIONS = {'incident.report': ['product_owner', 'architect', 'project_manager'],
               'incident.manage': ['project_manager']}
CAPABILITIES = {'incident.report', 'incident.read', 'incident.ack'}


def capabilities(add):
    connection = op.get_bind()
    table = sa.table('skill_profile', sa.column('id', sa.String), sa.column('allowed_capabilities', sa.JSON))
    for row in connection.execute(sa.select(table)).mappings():
        caps = set(row['allowed_capabilities'])
        if add: caps.update(CAPABILITIES)
        else: caps.difference_update(CAPABILITIES)
        connection.execute(table.update().where(table.c.id == row['id']).values(allowed_capabilities=sorted(caps)))


def upgrade():
    op.execute("""CREATE TABLE conversation.technical_incident (
	id VARCHAR(36) NOT NULL, 
	project_id VARCHAR(36) NOT NULL, 
	team_id VARCHAR(36) NOT NULL, 
	task_id VARCHAR(36), 
	responsible_profile_id VARCHAR(36) NOT NULL, 
	title VARCHAR(180) NOT NULL, 
	description VARCHAR(4000) NOT NULL, 
	content_status VARCHAR(24) NOT NULL, 
	priority VARCHAR(2) NOT NULL, 
	status VARCHAR(16) NOT NULL, 
	revision INTEGER NOT NULL, 
	request_key VARCHAR(36) NOT NULL, 
	request_fingerprint VARCHAR(64) NOT NULL, 
	created_by VARCHAR(36) NOT NULL, 
	created_by_session_id VARCHAR(36), 
	created_at TIMESTAMP WITH TIME ZONE NOT NULL, 
	resolved_at TIMESTAMP WITH TIME ZONE, 
	PRIMARY KEY (id), 
	CONSTRAINT uq_incident_request UNIQUE (created_by, request_key), 
	CONSTRAINT uq_incident_project UNIQUE (id, project_id), 
	CONSTRAINT ck_incident_status CHECK (status IN ('open','acknowledged','escalated','resolved')), 
	CONSTRAINT ck_incident_priority CHECK (priority IN ('P0','P1','P2','P3')), 
	CONSTRAINT fk_incident_team_profile FOREIGN KEY(team_id, responsible_profile_id) REFERENCES public.agent_team_skill (team_id, skill_profile_id), 
	CONSTRAINT ck_incident_content_status CHECK (content_status IN ('Brouillon IA','À revoir')), 
	CONSTRAINT ck_incident_revision CHECK (revision > 0), 
	CONSTRAINT ck_incident_resolved CHECK ((status = 'resolved') = (resolved_at IS NOT NULL)), 
	FOREIGN KEY(project_id) REFERENCES public.project (id), 
	FOREIGN KEY(team_id) REFERENCES public.agent_team (id), 
	FOREIGN KEY(task_id) REFERENCES public.agent_task (id), 
	FOREIGN KEY(responsible_profile_id) REFERENCES public.skill_profile (id), 
	FOREIGN KEY(created_by) REFERENCES public.app_user (id), 
	FOREIGN KEY(created_by_session_id) REFERENCES public.agent_session (id)
)""")
    op.execute('CREATE INDEX ix_conversation_technical_incident_project_id ON conversation.technical_incident (project_id)')
    op.execute("""CREATE TABLE conversation.incident_event (
	id VARCHAR(36) NOT NULL, 
	incident_id VARCHAR(36) NOT NULL, 
	project_id VARCHAR(36) NOT NULL, 
	revision INTEGER NOT NULL, 
	action VARCHAR(20) NOT NULL, 
	actor_id VARCHAR(36) NOT NULL, 
	session_id VARCHAR(36), 
	fingerprint VARCHAR(64) NOT NULL, 
	reason VARCHAR(1000) NOT NULL, 
	source_refs JSON NOT NULL, 
	snapshot JSON NOT NULL, 
	created_at TIMESTAMP WITH TIME ZONE NOT NULL, 
	PRIMARY KEY (id), 
	CONSTRAINT uq_incident_event_revision UNIQUE (incident_id, revision), 
	CONSTRAINT fk_incident_event_project FOREIGN KEY(incident_id, project_id) REFERENCES conversation.technical_incident (id, project_id), 
	FOREIGN KEY(project_id) REFERENCES public.project (id), 
	FOREIGN KEY(actor_id) REFERENCES public.app_user (id), 
	FOREIGN KEY(session_id) REFERENCES public.agent_session (id)
)""")
    op.execute('CREATE INDEX ix_conversation_incident_event_incident_id ON conversation.incident_event (incident_id)')
    op.execute("""CREATE TABLE conversation.incident_notification (
	id VARCHAR(36) NOT NULL, 
	incident_id VARCHAR(36) NOT NULL, 
	project_id VARCHAR(36) NOT NULL, 
	event_id VARCHAR(36) NOT NULL, 
	recipient_user_id VARCHAR(36), 
	recipient_profile_id VARCHAR(36), 
	priority VARCHAR(2) NOT NULL, 
	status VARCHAR(16) NOT NULL, 
	created_at TIMESTAMP WITH TIME ZONE NOT NULL, 
	expires_at TIMESTAMP WITH TIME ZONE NOT NULL, 
	acknowledged_at TIMESTAMP WITH TIME ZONE, 
	acknowledged_by VARCHAR(36), 
	acknowledged_by_session_id VARCHAR(36), 
	PRIMARY KEY (id), 
	CONSTRAINT fk_incident_notification_project FOREIGN KEY(incident_id, project_id) REFERENCES conversation.technical_incident (id, project_id), 
	CONSTRAINT ck_incident_notification_recipient CHECK ((recipient_user_id IS NOT NULL) <> (recipient_profile_id IS NOT NULL)), 
	CONSTRAINT ck_incident_notification_status CHECK (status IN ('pending','acknowledged','expired')), 
	CONSTRAINT ck_incident_notification_ack CHECK ((status = 'acknowledged') = (acknowledged_at IS NOT NULL)), 
	CONSTRAINT ck_incident_notification_expiry CHECK (expires_at > created_at), 
	FOREIGN KEY(project_id) REFERENCES public.project (id), 
	FOREIGN KEY(event_id) REFERENCES conversation.incident_event (id), 
	FOREIGN KEY(recipient_user_id) REFERENCES public.app_user (id), 
	FOREIGN KEY(recipient_profile_id) REFERENCES public.skill_profile (id), 
	FOREIGN KEY(acknowledged_by) REFERENCES public.app_user (id), 
	FOREIGN KEY(acknowledged_by_session_id) REFERENCES public.agent_session (id)
)""")
    op.execute('CREATE INDEX ix_conversation_incident_notification_project_id ON conversation.incident_notification (project_id)')
    connection = op.get_bind()
    for permission, roles in PERMISSIONS.items():
        connection.execute(sa.text('INSERT INTO permission (code) VALUES (:code) ON CONFLICT DO NOTHING'), {'code': permission})
        for role in roles:
            connection.execute(sa.text('INSERT INTO role_permission (role_code, permission_code) SELECT CAST(:role AS VARCHAR), CAST(:code AS VARCHAR) WHERE EXISTS (SELECT 1 FROM role WHERE code=CAST(:role AS VARCHAR)) ON CONFLICT DO NOTHING'), {'role': role, 'code': permission})
    capabilities(True)


def downgrade():
    # Les fils restent consultables ; seul le rattachement vers l'incident retiré est effacé.
    op.execute("UPDATE conversation.coordination_channel SET attachment=NULL WHERE attachment->>'type'='technical_incident'")
    for table in ('incident_notification', 'incident_event', 'technical_incident'):
        op.drop_table(table, schema='conversation')
    connection = op.get_bind()
    for permission in PERMISSIONS:
        connection.execute(sa.text('DELETE FROM role_permission WHERE permission_code=:code'), {'code': permission})
        connection.execute(sa.text('DELETE FROM permission WHERE code=:code'), {'code': permission})
    capabilities(False)
