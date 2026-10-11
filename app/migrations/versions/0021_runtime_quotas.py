"""AGENT-21/22 : runtimes et quotas ; SQL et permissions figés."""
from alembic import op
import sqlalchemy as sa
revision='0021_runtime_quotas'
down_revision='0020_git_development'
branch_labels=None
depends_on=None

def capabilities(add):
    table=sa.table('skill_profile',sa.column('id',sa.String),sa.column('allowed_capabilities',sa.JSON))
    connection=op.get_bind()
    for row in connection.execute(sa.select(table)).mappings():
        caps=set(row['allowed_capabilities'])
        if add: caps.add('task.runtime')
        else: caps.discard('task.runtime')
        connection.execute(table.update().where(table.c.id==row['id']).values(allowed_capabilities=sorted(caps)))

def upgrade():
    op.execute("\nCREATE TABLE runtime_version (\n\tid VARCHAR(36) NOT NULL, \n\truntime_id VARCHAR(36) NOT NULL, \n\tproject_id VARCHAR(36) NOT NULL, \n\tversion INTEGER NOT NULL, \n\tname VARCHAR(120) NOT NULL, \n\tkind VARCHAR(10) NOT NULL, \n\tprofile_version_id VARCHAR(36), \n\tclient_code VARCHAR(32), \n\ttimeout_seconds INTEGER NOT NULL, \n\tinclude_memory BOOLEAN NOT NULL, \n\tenabled BOOLEAN NOT NULL, \n\tis_current BOOLEAN NOT NULL, \n\treason VARCHAR(500) NOT NULL, \n\tcreated_by VARCHAR(36) NOT NULL, \n\tcreated_at TIMESTAMP WITH TIME ZONE NOT NULL, \n\tPRIMARY KEY (id), \n\tUNIQUE (runtime_id, version), \n\tCHECK (version > 0 AND timeout_seconds BETWEEN 1 AND 3600 AND (kind <> 'llm' OR timeout_seconds <= 80)), \n\tCHECK ((kind = 'llm' AND profile_version_id IS NOT NULL AND client_code IS NULL) OR (kind = 'client' AND profile_version_id IS NULL AND client_code IS NOT NULL)), \n\tFOREIGN KEY(project_id) REFERENCES project (id), \n\tFOREIGN KEY(profile_version_id) REFERENCES llm_profile_version (id), \n\tFOREIGN KEY(created_by) REFERENCES app_user (id)\n)\n\n")
    op.execute('CREATE UNIQUE INDEX uq_runtime_current ON runtime_version (runtime_id) WHERE is_current')
    op.execute("\nCREATE TABLE runtime_binding (\n\tid VARCHAR(36) NOT NULL, \n\tproject_id VARCHAR(36) NOT NULL, \n\tscope VARCHAR(10) NOT NULL, \n\tscope_id VARCHAR(64) NOT NULL, \n\tversion INTEGER NOT NULL, \n\truntime_version_id VARCHAR(36), \n\tis_current BOOLEAN NOT NULL, \n\treason VARCHAR(500) NOT NULL, \n\tcreated_by VARCHAR(36) NOT NULL, \n\tcreated_at TIMESTAMP WITH TIME ZONE NOT NULL, \n\tPRIMARY KEY (id), \n\tUNIQUE (project_id, scope, scope_id, version), \n\tCHECK (scope IN ('project','agent','session','task') AND version > 0), \n\tFOREIGN KEY(project_id) REFERENCES project (id), \n\tFOREIGN KEY(runtime_version_id) REFERENCES runtime_version (id), \n\tFOREIGN KEY(created_by) REFERENCES app_user (id)\n)\n\n")
    op.execute('CREATE UNIQUE INDEX uq_runtime_binding_current ON runtime_binding (project_id, scope, scope_id) WHERE is_current')
    op.execute("\nCREATE TABLE runtime_quota (\n\tid VARCHAR(36) NOT NULL, \n\tproject_id VARCHAR(36) NOT NULL, \n\tscope VARCHAR(10) NOT NULL, \n\tscope_id VARCHAR(64) NOT NULL, \n\tversion INTEGER NOT NULL, \n\tmax_tokens INTEGER, \n\tmax_seconds NUMERIC(16, 6), \n\tmax_concurrency INTEGER, \n\tmax_cost NUMERIC(16, 6), \n\tcurrency VARCHAR(3), \n\tis_current BOOLEAN NOT NULL, \n\treason VARCHAR(500) NOT NULL, \n\tcreated_by VARCHAR(36) NOT NULL, \n\tcreated_at TIMESTAMP WITH TIME ZONE NOT NULL, \n\tPRIMARY KEY (id), \n\tUNIQUE (project_id, scope, scope_id, version), \n\tCHECK (scope IN ('project','agent','session','task','model') AND version > 0), \n\tCHECK ((max_tokens IS NULL OR max_tokens > 0) AND (max_seconds IS NULL OR max_seconds > 0) AND (max_concurrency IS NULL OR max_concurrency > 0) AND (max_cost IS NULL OR max_cost > 0)), \n\tCHECK ((max_cost IS NULL AND currency IS NULL) OR (max_cost IS NOT NULL AND currency IS NOT NULL)), \n\tFOREIGN KEY(project_id) REFERENCES project (id), \n\tFOREIGN KEY(created_by) REFERENCES app_user (id)\n)\n\n")
    op.execute('CREATE UNIQUE INDEX uq_runtime_quota_current ON runtime_quota (project_id, scope, scope_id) WHERE is_current')
    op.execute("\nCREATE TABLE runtime_selection (\n\tid VARCHAR(36) NOT NULL, \n\tattempt_id VARCHAR(36) NOT NULL, \n\tproject_id VARCHAR(36) NOT NULL, \n\truntime_version_id VARCHAR(36), \n\tbinding_id VARCHAR(36), \n\tprofile_version_id VARCHAR(36), \n\torigin VARCHAR(10) NOT NULL, \n\tcontext JSON NOT NULL, \n\tmemory_snapshot JSON, \n\tdeadline TIMESTAMP WITH TIME ZONE NOT NULL, \n\tcreated_at TIMESTAMP WITH TIME ZONE NOT NULL, \n\tPRIMARY KEY (id), \n\tCHECK (origin IN ('task','session','agent','project','explicit','default')), \n\tUNIQUE (attempt_id), \n\tFOREIGN KEY(attempt_id) REFERENCES agent_task_run (id), \n\tFOREIGN KEY(project_id) REFERENCES project (id), \n\tFOREIGN KEY(runtime_version_id) REFERENCES runtime_version (id), \n\tFOREIGN KEY(binding_id) REFERENCES runtime_binding (id), \n\tFOREIGN KEY(profile_version_id) REFERENCES llm_profile_version (id)\n)\n\n")
    op.execute("\nCREATE TABLE runtime_usage (\n\tid VARCHAR(36) NOT NULL, \n\tproject_id VARCHAR(36) NOT NULL, \n\tcall_id VARCHAR(36), \n\texternal_selection_id VARCHAR(36), \n\tcontext JSON NOT NULL, \n\tpolicy_ids JSON NOT NULL, \n\tstatus VARCHAR(10) NOT NULL, \n\tcharged_tokens INTEGER NOT NULL, \n\tcharged_seconds NUMERIC(16, 6) NOT NULL, \n\tcharged_cost NUMERIC(16, 6), \n\tactual_tokens INTEGER, \n\tactual_seconds NUMERIC(16, 6), \n\tactual_cost NUMERIC(16, 6), \n\tcurrency VARCHAR(3), \n\terror_code VARCHAR(40), \n\treconciled_by VARCHAR(36), \n\tstarted_at TIMESTAMP WITH TIME ZONE NOT NULL, \n\tended_at TIMESTAMP WITH TIME ZONE, \n\tPRIMARY KEY (id), \n\tCHECK (status IN ('active','completed','failed','unknown')), \n\tCHECK ((call_id IS NOT NULL AND external_selection_id IS NULL) OR (call_id IS NULL AND external_selection_id IS NOT NULL)), \n\tCHECK (charged_tokens >= 0 AND charged_seconds >= 0 AND (charged_cost IS NULL OR charged_cost >= 0)), \n\tFOREIGN KEY(project_id) REFERENCES project (id), \n\tUNIQUE (call_id), \n\tFOREIGN KEY(call_id) REFERENCES llm_call_attempt (id), \n\tUNIQUE (external_selection_id), \n\tFOREIGN KEY(external_selection_id) REFERENCES runtime_selection (id), \n\tFOREIGN KEY(reconciled_by) REFERENCES app_user (id)\n)\n\n")
    op.execute('CREATE INDEX ix_runtime_usage_project ON runtime_usage (project_id, started_at)')
    op.execute("INSERT INTO permission(code) VALUES ('runtime.configure'),('quota.manage') ON CONFLICT DO NOTHING")
    op.execute("INSERT INTO role_permission(role_code,permission_code) SELECT r.code, v.permission FROM role r JOIN (VALUES ('architect','runtime.configure'),('project_manager','quota.manage')) AS v(role,permission) ON r.code=v.role ON CONFLICT DO NOTHING")
    capabilities(True)

def downgrade():
    capabilities(False)
    op.execute("DELETE FROM role_permission WHERE permission_code IN ('runtime.configure','quota.manage')")
    op.execute("DELETE FROM permission WHERE code IN ('runtime.configure','quota.manage')")
    op.drop_table('runtime_usage')
    op.drop_table('runtime_selection')
    op.drop_table('runtime_quota')
    op.drop_table('runtime_binding')
    op.drop_table('runtime_version')
