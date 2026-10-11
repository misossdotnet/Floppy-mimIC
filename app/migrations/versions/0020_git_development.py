"""AGENT-15 à 20 : dépôts et preuves Git ; SQL et droits figés."""
from alembic import op
import sqlalchemy as sa
revision='0020_git_development'
down_revision='0019_technical_incidents'
branch_labels=None
depends_on=None
PERMISSIONS={'code.approve': ['architect'], 'code.collect': ['product_owner', 'project_manager', 'architect'], 'code.review': ['product_owner', 'project_manager', 'architect'], 'code.test': ['product_owner', 'project_manager', 'architect'], 'git.deploy': ['project_manager'], 'git.merge': ['project_manager'], 'git.push': ['project_manager'], 'repository.manage': ['architect'], 'worktree.create': ['project_manager'], 'worktree.delete': ['project_manager']}
CAPABILITIES={'git.read','git.worktree','git.collect','git.test','git.review'}

def capabilities(add):
    connection=op.get_bind()
    table=sa.table('skill_profile',sa.column('id',sa.String),sa.column('allowed_capabilities',sa.JSON))
    for row in connection.execute(sa.select(table)).mappings():
        caps=set(row['allowed_capabilities'])
        if add: caps.update(CAPABILITIES)
        else: caps.difference_update(CAPABILITIES)
        connection.execute(table.update().where(table.c.id==row['id']).values(allowed_capabilities=sorted(caps)))

def upgrade():
    op.execute('\nCREATE TABLE repository (\n\tid VARCHAR(36) NOT NULL, \n\tproject_id VARCHAR(36) NOT NULL, \n\tcatalog_key VARCHAR(80) NOT NULL, \n\tphysical_identity VARCHAR(64) NOT NULL, \n\tpolicy_fingerprint VARCHAR(64) NOT NULL, \n\tis_active BOOLEAN NOT NULL, \n\tcreated_by VARCHAR(36) NOT NULL, \n\tcreated_at TIMESTAMP WITH TIME ZONE NOT NULL, \n\tPRIMARY KEY (id), \n\tUNIQUE (catalog_key), \n\tUNIQUE (id, project_id), \n\tFOREIGN KEY(project_id) REFERENCES project (id), \n\tUNIQUE (physical_identity), \n\tFOREIGN KEY(created_by) REFERENCES app_user (id)\n)\n\n')
    op.execute('CREATE INDEX ix_repository_project_id ON repository (project_id)')
    op.execute("\nCREATE TABLE git_worktree (\n\tid VARCHAR(36) NOT NULL, \n\tproject_id VARCHAR(36) NOT NULL, \n\trepository_id VARCHAR(36) NOT NULL, \n\ttask_id VARCHAR(36) NOT NULL, \n\tbranch VARCHAR(180) NOT NULL, \n\tbase_commit VARCHAR(64) NOT NULL, \n\tstatus VARCHAR(16) NOT NULL, \n\tcreated_by VARCHAR(36) NOT NULL, \n\tsession_id VARCHAR(36), \n\tcreated_at TIMESTAMP WITH TIME ZONE NOT NULL, \n\tremoved_at TIMESTAMP WITH TIME ZONE, \n\tPRIMARY KEY (id), \n\tUNIQUE (repository_id, branch), \n\tUNIQUE (id, project_id), \n\tFOREIGN KEY(repository_id, project_id) REFERENCES repository (id, project_id), \n\tCHECK (status IN ('creating','ready','failed','removed','uncertain')), \n\tFOREIGN KEY(project_id) REFERENCES project (id), \n\tFOREIGN KEY(task_id) REFERENCES agent_task (id), \n\tFOREIGN KEY(created_by) REFERENCES app_user (id), \n\tFOREIGN KEY(session_id) REFERENCES agent_session (id)\n)\n\n")
    op.execute('CREATE INDEX ix_git_worktree_project_id ON git_worktree (project_id)')
    op.execute('CREATE INDEX ix_git_worktree_task_id ON git_worktree (task_id)')
    op.execute('\nCREATE TABLE code_change (\n\tid VARCHAR(36) NOT NULL, \n\tproject_id VARCHAR(36) NOT NULL, \n\tworktree_id VARCHAR(36) NOT NULL, \n\trevision INTEGER NOT NULL, \n\thead_commit VARCHAR(64) NOT NULL, \n\tcontent_fingerprint VARCHAR(64) NOT NULL, \n\tmanifest JSON NOT NULL, \n\tcreated_by VARCHAR(36) NOT NULL, \n\tsession_id VARCHAR(36), \n\tcreated_at TIMESTAMP WITH TIME ZONE NOT NULL, \n\tPRIMARY KEY (id), \n\tUNIQUE (worktree_id, revision), \n\tUNIQUE (id, project_id), \n\tFOREIGN KEY(worktree_id, project_id) REFERENCES git_worktree (id, project_id), \n\tCHECK (revision > 0), \n\tFOREIGN KEY(project_id) REFERENCES project (id), \n\tFOREIGN KEY(created_by) REFERENCES app_user (id), \n\tFOREIGN KEY(session_id) REFERENCES agent_session (id)\n)\n\n')
    op.execute('CREATE INDEX ix_code_change_project_id ON code_change (project_id)')
    op.execute("\nCREATE TABLE git_operation (\n\tid VARCHAR(36) NOT NULL, \n\tproject_id VARCHAR(36) NOT NULL, \n\tworktree_id VARCHAR(36) NOT NULL, \n\tkind VARCHAR(16) NOT NULL, \n\tstatus VARCHAR(24) NOT NULL, \n\trequest_key VARCHAR(36) NOT NULL, \n\trequest_fingerprint VARCHAR(64) NOT NULL, \n\tpolicy_fingerprint VARCHAR(64) NOT NULL, \n\tparameters JSON NOT NULL, \n\tresult JSON NOT NULL, \n\tactor_id VARCHAR(36) NOT NULL, \n\tsession_id VARCHAR(36), \n\tcapability VARCHAR(40), \n\tdecision_reason VARCHAR(1000), \n\tdecided_by VARCHAR(36), \n\tdecided_at TIMESTAMP WITH TIME ZONE, \n\texpires_at TIMESTAMP WITH TIME ZONE, \n\tcreated_at TIMESTAMP WITH TIME ZONE NOT NULL, \n\tstarted_at TIMESTAMP WITH TIME ZONE, \n\tended_at TIMESTAMP WITH TIME ZONE, \n\tPRIMARY KEY (id), \n\tUNIQUE (actor_id, request_key), \n\tFOREIGN KEY(worktree_id, project_id) REFERENCES git_worktree (id, project_id), \n\tCHECK (kind IN ('create','collect','test','merge','push','deploy','delete')), \n\tCHECK (status IN ('awaiting_approval','queued','running','succeeded','failed','rejected','uncertain')), \n\tFOREIGN KEY(project_id) REFERENCES project (id), \n\tFOREIGN KEY(actor_id) REFERENCES app_user (id), \n\tFOREIGN KEY(session_id) REFERENCES agent_session (id), \n\tFOREIGN KEY(decided_by) REFERENCES app_user (id)\n)\n\n")
    op.execute('CREATE INDEX ix_git_operation_project_id ON git_operation (project_id)')
    op.execute('CREATE INDEX ix_git_operation_worktree_id ON git_operation (worktree_id)')
    op.execute("\nCREATE TABLE code_review (\n\tid VARCHAR(36) NOT NULL, \n\tproject_id VARCHAR(36) NOT NULL, \n\tchange_id VARCHAR(36) NOT NULL, \n\treviewer_id VARCHAR(36) NOT NULL, \n\tsession_id VARCHAR(36), \n\tstatus VARCHAR(24) NOT NULL, \n\trevision INTEGER NOT NULL, \n\tcreated_at TIMESTAMP WITH TIME ZONE NOT NULL, \n\tPRIMARY KEY (id), \n\tFOREIGN KEY(change_id, project_id) REFERENCES code_change (id, project_id), \n\tCHECK (status IN ('pending','commented','changes_requested','reviewed')), \n\tCHECK (revision > 0), \n\tFOREIGN KEY(project_id) REFERENCES project (id), \n\tFOREIGN KEY(reviewer_id) REFERENCES app_user (id), \n\tFOREIGN KEY(session_id) REFERENCES agent_session (id)\n)\n\n")
    op.execute('CREATE INDEX ix_code_review_change_id ON code_review (change_id)')
    op.execute('CREATE INDEX ix_code_review_project_id ON code_review (project_id)')
    op.execute("\nCREATE TABLE code_test_run (\n\tid VARCHAR(36) NOT NULL, \n\tproject_id VARCHAR(36) NOT NULL, \n\tchange_id VARCHAR(36) NOT NULL, \n\toperation_id VARCHAR(36) NOT NULL, \n\tcommand_key VARCHAR(80) NOT NULL, \n\tstatus VARCHAR(16) NOT NULL, \n\texit_code INTEGER, \n\tduration_ms INTEGER NOT NULL, \n\tlog VARCHAR(8000) NOT NULL, \n\tcreated_at TIMESTAMP WITH TIME ZONE NOT NULL, \n\tPRIMARY KEY (id), \n\tUNIQUE (operation_id), \n\tFOREIGN KEY(change_id, project_id) REFERENCES code_change (id, project_id), \n\tCHECK (status IN ('passed','failed','error')), \n\tFOREIGN KEY(project_id) REFERENCES project (id), \n\tFOREIGN KEY(operation_id) REFERENCES git_operation (id)\n)\n\n")
    op.execute('CREATE INDEX ix_code_test_run_change_id ON code_test_run (change_id)')
    op.execute('CREATE INDEX ix_code_test_run_project_id ON code_test_run (project_id)')
    op.execute("\nCREATE TABLE code_workflow (\n\tid VARCHAR(36) NOT NULL, \n\tworktree_id VARCHAR(36) NOT NULL, \n\tphase VARCHAR(24) NOT NULL, \n\trevision INTEGER NOT NULL, \n\tapproved_change_id VARCHAR(36), \n\tapproved_by VARCHAR(36), \n\tcreated_at TIMESTAMP WITH TIME ZONE NOT NULL, \n\tPRIMARY KEY (id), \n\tUNIQUE (worktree_id), \n\tCHECK (revision > 0), \n\tCHECK (phase IN ('analysis','tests','implementation','execution','review','approved')), \n\tFOREIGN KEY(worktree_id) REFERENCES git_worktree (id), \n\tFOREIGN KEY(approved_change_id) REFERENCES code_change (id), \n\tFOREIGN KEY(approved_by) REFERENCES app_user (id)\n)\n\n")
    op.execute('\nCREATE TABLE code_review_event (\n\tid VARCHAR(36) NOT NULL, \n\treview_id VARCHAR(36) NOT NULL, \n\trevision INTEGER NOT NULL, \n\taction VARCHAR(24) NOT NULL, \n\tfile_path VARCHAR(500), \n\thunk_id VARCHAR(64), \n\ttext VARCHAR(2000) NOT NULL, \n\tcorrection BOOLEAN NOT NULL, \n\tresolves_id VARCHAR(36), \n\tcreated_at TIMESTAMP WITH TIME ZONE NOT NULL, \n\tPRIMARY KEY (id), \n\tUNIQUE (review_id, revision), \n\tFOREIGN KEY(review_id) REFERENCES code_review (id), \n\tFOREIGN KEY(resolves_id) REFERENCES code_review_event (id)\n)\n\n')
    op.execute('CREATE INDEX ix_code_review_event_review_id ON code_review_event (review_id)')
    op.execute('\nCREATE TABLE code_workflow_event (\n\tid VARCHAR(36) NOT NULL, \n\tworkflow_id VARCHAR(36) NOT NULL, \n\trevision INTEGER NOT NULL, \n\tphase VARCHAR(24) NOT NULL, \n\treason VARCHAR(1000) NOT NULL, \n\tactor_id VARCHAR(36) NOT NULL, \n\tsession_id VARCHAR(36), \n\tchange_id VARCHAR(36), \n\ttest_id VARCHAR(36), \n\tcreated_at TIMESTAMP WITH TIME ZONE NOT NULL, \n\tPRIMARY KEY (id), \n\tUNIQUE (workflow_id, revision), \n\tFOREIGN KEY(workflow_id) REFERENCES code_workflow (id), \n\tFOREIGN KEY(actor_id) REFERENCES app_user (id), \n\tFOREIGN KEY(session_id) REFERENCES agent_session (id), \n\tFOREIGN KEY(change_id) REFERENCES code_change (id)\n)\n\n')
    op.execute('CREATE INDEX ix_code_workflow_event_workflow_id ON code_workflow_event (workflow_id)')
    connection=op.get_bind()
    for code,roles in PERMISSIONS.items():
        connection.execute(sa.text('INSERT INTO permission (code) VALUES (:code) ON CONFLICT DO NOTHING'),{'code':code})
        for role in roles:
            connection.execute(sa.text('INSERT INTO role_permission (role_code,permission_code) SELECT CAST(:role AS VARCHAR),CAST(:code AS VARCHAR) WHERE EXISTS (SELECT 1 FROM role WHERE code=CAST(:role AS VARCHAR)) ON CONFLICT DO NOTHING'),{'role':role,'code':code})
    capabilities(True)

def downgrade():
    # Ne touche jamais au filesystem Git. Retrait SQL réservé à la recette/restauration coordonnée.
    op.drop_table('code_workflow_event')
    op.drop_table('code_review_event')
    op.drop_table('code_workflow')
    op.drop_table('code_test_run')
    op.drop_table('code_review')
    op.drop_table('git_operation')
    op.drop_table('code_change')
    op.drop_table('git_worktree')
    op.drop_table('repository')
    connection=op.get_bind()
    for code in PERMISSIONS:
        connection.execute(sa.text('DELETE FROM role_permission WHERE permission_code=:code'),{'code':code})
        connection.execute(sa.text('DELETE FROM permission WHERE code=:code'),{'code':code})
    capabilities(False)

