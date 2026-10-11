"""Mémoire versionnée, références actives et partage global explicite.

Revision ID: 0016_project_memory
Revises: 0015_agent_sprints
"""
from alembic import op
import sqlalchemy as sa

revision='0016_project_memory'
down_revision='0015_agent_sprints'
branch_labels=None
depends_on=None

PERMISSIONS={'memory.read':('product_owner','project_manager','architect','reader'),
    'memory.propose':('product_owner','project_manager','architect'),
    'memory.validate.business':('product_owner',),'memory.validate.technical':('architect',),
    'memory.validate.result':('project_manager',),'memory.global.manage':()}


def capabilities(add):
    connection=op.get_bind()
    table=sa.table('skill_profile',sa.column('id',sa.String),sa.column('allowed_capabilities',sa.JSON))
    for row in connection.execute(sa.select(table)).mappings().all():
        caps=set(row['allowed_capabilities'])
        if add: caps.update(('memory.read','memory.propose'))
        else: caps.difference_update(('memory.read','memory.propose'))
        connection.execute(table.update().where(table.c.id==row['id']).values(allowed_capabilities=sorted(caps)))


def upgrade():
    op.create_table('project_memory',
        sa.Column('id',sa.String(36),primary_key=True),sa.Column('scope',sa.String(16),nullable=False),
        sa.Column('project_id',sa.String(36),sa.ForeignKey('project.id')),
        sa.Column('kind',sa.String(32),nullable=False),sa.Column('current_version_number',sa.Integer,nullable=False),
        sa.Column('reference_version_id',sa.String(36)),
        sa.Column('created_by',sa.String(36),sa.ForeignKey('app_user.id'),nullable=False),
        sa.Column('created_at',sa.DateTime(timezone=True),nullable=False),
        sa.CheckConstraint("scope IN ('project','global')",name='ck_memory_scope'),
        sa.CheckConstraint("(scope = 'project') = (project_id IS NOT NULL)",name='ck_memory_project'),
        sa.CheckConstraint("kind IN ('convention','architecture_decision','business_constraint','result')",name='ck_memory_kind'),
        sa.CheckConstraint("scope <> 'global' OR kind = 'convention'",name='ck_memory_global_kind'),
        sa.CheckConstraint('current_version_number >= 0',name='ck_memory_current'))
    op.create_index('ix_project_memory_project_id','project_memory',['project_id'])
    op.create_table('project_memory_version',
        sa.Column('id',sa.String(36),primary_key=True),sa.Column('memory_id',sa.String(36),sa.ForeignKey('project_memory.id'),nullable=False),
        sa.Column('version_number',sa.Integer,nullable=False),sa.Column('parent_version_id',sa.String(36)),
        sa.Column('title',sa.String(180),nullable=False),sa.Column('content',sa.String(4000),nullable=False),
        sa.Column('source_note',sa.String(500),nullable=False),sa.Column('source_refs',sa.JSON,nullable=False),
        sa.Column('confidence',sa.Float),sa.Column('status',sa.String(24),nullable=False),sa.Column('action',sa.String(16),nullable=False),
        sa.Column('change_note',sa.String(1000),nullable=False),
        sa.Column('actor_id',sa.String(36),sa.ForeignKey('app_user.id')),sa.Column('session_id',sa.String(36),sa.ForeignKey('agent_session.id')),
        sa.Column('created_at',sa.DateTime(timezone=True),nullable=False),
        sa.UniqueConstraint('memory_id','version_number',name='uq_memory_version'),
        sa.UniqueConstraint('id','memory_id',name='uq_memory_version_scope'),
        sa.ForeignKeyConstraint(['parent_version_id','memory_id'],['project_memory_version.id','project_memory_version.memory_id'],name='fk_memory_parent'),
        sa.CheckConstraint('version_number > 0',name='ck_memory_version_number'),
        sa.CheckConstraint("status IN ('Brouillon IA','À revoir','Validé','Rejeté','Obsolète')",name='ck_memory_version_status'),
        sa.CheckConstraint('(actor_id IS NOT NULL) <> (session_id IS NOT NULL)',name='ck_memory_author'),
        sa.CheckConstraint('confidence IS NULL OR (confidence >= 0 AND confidence <= 1)',name='ck_memory_confidence'))
    op.create_index('ix_project_memory_version_memory_id','project_memory_version',['memory_id'])
    op.create_foreign_key('fk_memory_reference','project_memory','project_memory_version',['reference_version_id','id'],['id','memory_id'])
    op.create_table('project_memory_grant',
        sa.Column('id',sa.String(36),primary_key=True),sa.Column('project_id',sa.String(36),sa.ForeignKey('project.id'),nullable=False),
        sa.Column('memory_id',sa.String(36),sa.ForeignKey('project_memory.id'),nullable=False),sa.Column('version_id',sa.String(36),nullable=False),
        sa.Column('created_by',sa.String(36),sa.ForeignKey('app_user.id'),nullable=False),sa.Column('created_at',sa.DateTime(timezone=True),nullable=False),
        sa.Column('revoked_by',sa.String(36),sa.ForeignKey('app_user.id')),sa.Column('revoked_at',sa.DateTime(timezone=True)),
        sa.ForeignKeyConstraint(['version_id','memory_id'],['project_memory_version.id','project_memory_version.memory_id'],name='fk_memory_grant_version'),
        sa.CheckConstraint('(revoked_at IS NULL) = (revoked_by IS NULL)',name='ck_memory_grant_revoke'))
    op.create_index('ix_project_memory_grant_project_id','project_memory_grant',['project_id'])
    op.create_index('uq_memory_grant_active','project_memory_grant',['project_id','memory_id'],unique=True,postgresql_where=sa.text('revoked_at IS NULL'))
    connection=op.get_bind()
    for permission,roles in PERMISSIONS.items():
        connection.execute(sa.text('INSERT INTO permission (code) VALUES (:code) ON CONFLICT DO NOTHING'),{'code':permission})
        for role in roles:
            # Une base neuve n'a pas encore reçu le bootstrap des rôles.
            connection.execute(sa.text('INSERT INTO role_permission (role_code, permission_code) SELECT code,:code FROM role WHERE code=:role ON CONFLICT DO NOTHING'),{'role':role,'code':permission})
    capabilities(True)


def downgrade():
    # Destructif pour mémoire/partages uniquement, sur base jetable ou restauration coordonnée.
    op.drop_table('project_memory_grant')
    op.drop_constraint('fk_memory_reference','project_memory',type_='foreignkey')
    op.drop_table('project_memory_version'); op.drop_table('project_memory')
    connection=op.get_bind()
    for permission in PERMISSIONS:
        connection.execute(sa.text('DELETE FROM role_permission WHERE permission_code=:code'),{'code':permission})
        connection.execute(sa.text('DELETE FROM permission WHERE code=:code'),{'code':permission})
    capabilities(False)
