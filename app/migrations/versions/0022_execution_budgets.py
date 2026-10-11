"""AGENT-23 : budgets périodiques, alertes et suspensions persistantes."""
from alembic import op
revision = '0022_execution_budgets'
down_revision = '0021_runtime_quotas'
branch_labels = None
depends_on = None


def upgrade():
    op.execute("\nCREATE TABLE execution_budget (\n\tid VARCHAR(36) NOT NULL, \n\tproject_id VARCHAR(36) NOT NULL, \n\tscope VARCHAR(10) NOT NULL, \n\tscope_id VARCHAR(64) NOT NULL, \n\tversion INTEGER NOT NULL, \n\tperiod VARCHAR(10) NOT NULL, \n\tmetric VARCHAR(10) NOT NULL, \n\tamount NUMERIC(16, 6) NOT NULL, \n\tcurrency VARCHAR(3), \n\twarning_percent INTEGER NOT NULL, \n\tenabled BOOLEAN NOT NULL, \n\tis_current BOOLEAN NOT NULL, \n\treason VARCHAR(500) NOT NULL, \n\tcreated_by VARCHAR(36) NOT NULL, \n\tcreated_at TIMESTAMP WITH TIME ZONE NOT NULL, \n\tPRIMARY KEY (id), \n\tUNIQUE (project_id, scope, scope_id, version), \n\tCHECK (scope IN ('project','agent','session','task','model') AND period IN ('daily','weekly','monthly') AND version > 0), \n\tCHECK (metric IN ('tokens','seconds','cost') AND amount > 0 AND warning_percent BETWEEN 1 AND 99), \n\tCHECK ((metric = 'cost' AND currency IS NOT NULL) OR (metric <> 'cost' AND currency IS NULL)), \n\tFOREIGN KEY(project_id) REFERENCES project (id), \n\tFOREIGN KEY(created_by) REFERENCES app_user (id)\n)\n\n")
    op.execute('CREATE UNIQUE INDEX uq_budget_current ON execution_budget (project_id, scope, scope_id) WHERE is_current')
    op.execute("\nCREATE TABLE budget_alert (\n\tid VARCHAR(36) NOT NULL, \n\tproject_id VARCHAR(36) NOT NULL, \n\tbudget_id VARCHAR(36) NOT NULL, \n\tperiod_start TIMESTAMP WITH TIME ZONE NOT NULL, \n\tlevel VARCHAR(12) NOT NULL, \n\tcreated_at TIMESTAMP WITH TIME ZONE NOT NULL, \n\tacknowledged_by VARCHAR(36), \n\tacknowledged_at TIMESTAMP WITH TIME ZONE, \n\tPRIMARY KEY (id), \n\tUNIQUE (budget_id, period_start, level), \n\tCHECK (level IN ('warning','exhausted','unavailable')), \n\tFOREIGN KEY(project_id) REFERENCES project (id), \n\tFOREIGN KEY(budget_id) REFERENCES execution_budget (id), \n\tFOREIGN KEY(acknowledged_by) REFERENCES app_user (id)\n)\n\n")
    op.execute('\nCREATE TABLE budget_suspension (\n\tid VARCHAR(36) NOT NULL, \n\tproject_id VARCHAR(36) NOT NULL, \n\talert_id VARCHAR(36) NOT NULL, \n\tplan_id VARCHAR(36) NOT NULL, \n\tcreated_at TIMESTAMP WITH TIME ZONE NOT NULL, \n\tresumed_by VARCHAR(36), \n\tresumed_at TIMESTAMP WITH TIME ZONE, \n\tPRIMARY KEY (id), \n\tUNIQUE (alert_id, plan_id), \n\tFOREIGN KEY(project_id) REFERENCES project (id), \n\tFOREIGN KEY(alert_id) REFERENCES budget_alert (id), \n\tFOREIGN KEY(plan_id) REFERENCES agent_plan_version (id), \n\tFOREIGN KEY(resumed_by) REFERENCES app_user (id)\n)\n\n')
    op.execute("INSERT INTO permission(code) VALUES ('budget.configure') ON CONFLICT DO NOTHING")
    op.execute("INSERT INTO role_permission(role_code,permission_code) SELECT code, 'budget.configure' FROM role WHERE code='project_manager' ON CONFLICT DO NOTHING")


def downgrade():
    op.execute("DELETE FROM role_permission WHERE permission_code='budget.configure'")
    op.execute("DELETE FROM permission WHERE code='budget.configure'")
    op.drop_table('budget_suspension')
    op.drop_table('budget_alert')
    op.drop_table('execution_budget')
