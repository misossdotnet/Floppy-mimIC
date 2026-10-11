"""Gouvernance périodique sous le verrou d'admission AGENT-22, sans réseau."""
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from sqlalchemy import select
from app.agent_models import AgentPlanVersion, AgentTask, AgentTeamSkill, AgentSession
from app.budget_models import ExecutionBudget, BudgetAlert, BudgetSuspension
from app.identity_models import utcnow
from app.identity_service import audit
from app.runtime_models import RuntimeUsage
from app.runtime_service import RuntimeDenied, clean, lock_project, scope_query, validate_scope, view


def period_bounds(period, now):
    now = now.astimezone(timezone.utc)
    start = now.replace(hour=0, minute=0, second=0, microsecond=0)
    if period == 'daily':
        return start, start + timedelta(days=1)
    if period == 'weekly':
        start -= timedelta(days=start.weekday())
        return start, start + timedelta(days=7)
    if period != 'monthly':
        raise ValueError('Période inconnue.')
    start = start.replace(day=1)
    return start, datetime(start.year + (start.month == 12), start.month % 12 + 1, 1, tzinfo=timezone.utc)


async def current_budgets(db, project_id):
    return (await db.scalars(select(ExecutionBudget).where(ExecutionBudget.project_id == project_id,
        ExecutionBudget.is_current.is_(True), ExecutionBudget.enabled.is_(True)))).all()


async def consumption(db, budget, now):
    start, end = period_bounds(budget.period, now)
    rows = (await db.scalars(scope_query(budget.project_id, budget.scope, budget.scope_id).where(
        RuntimeUsage.started_at >= start, RuntimeUsage.started_at < end))).all()
    field = {'tokens':'charged_tokens', 'seconds':'charged_seconds', 'cost':'charged_cost'}[budget.metric]
    unknown = any(getattr(r, field) is None or budget.metric == 'tokens' and r.call_id is None
        or budget.metric == 'cost' and r.currency != budget.currency for r in rows)
    charged = None if unknown else sum((Decimal(getattr(r, field)) for r in rows), Decimal(0))
    actual_field = {'tokens':'actual_tokens', 'seconds':'actual_seconds', 'cost':'actual_cost'}[budget.metric]
    measured = None if not rows or any(getattr(r, actual_field) is None for r in rows) or unknown else sum(
        (Decimal(getattr(r, actual_field)) for r in rows), Decimal(0))
    level = ('unavailable' if charged is None else 'exhausted' if charged >= budget.amount else
             'warning' if charged * 100 >= budget.amount * budget.warning_percent else None)
    return dict(period_start=start, period_end=end, charged=charged, measured=measured, level=level,
                remaining=max(Decimal(0), budget.amount-charged) if charged is not None else None)


async def revise(db, project_id, actor, data):
    await validate_scope(db, project_id, data.scope, data.scope_id)
    reason = clean(data.reason)
    await lock_project(db, project_id)
    old = await db.scalar(select(ExecutionBudget).where(ExecutionBudget.project_id == project_id,
        ExecutionBudget.scope == data.scope, ExecutionBudget.scope_id == data.scope_id,
        ExecutionBudget.is_current.is_(True)))
    if data.expected_version != (old.version if old else 0):
        raise RuntimeDenied('revision_stale')
    if old:
        old.is_current = False
        await db.flush()
    row = ExecutionBudget(project_id=project_id, version=old.version+1 if old else 1, created_by=actor.id,
        reason=reason, **data.model_dump(exclude={'expected_version','reason'}))
    db.add(row)
    await db.flush()
    audit(db, 'budget.revise', 'success', project_id=project_id, actor_id=actor.id,
        target_type='execution_budget', target_id=row.id, details={'version':row.version})
    await evaluate(db, project_id)
    return row


async def applicable_plans(db, budget):
    plans = (await db.scalars(select(AgentPlanVersion).where(AgentPlanVersion.project_id == budget.project_id,
        AgentPlanVersion.status == 'approved'))).all()
    if budget.scope == 'project':
        return plans
    tasks = (await db.scalars(select(AgentTask).where(AgentTask.project_id == budget.project_id))).all()
    if budget.scope == 'task':
        ids = {t.plan_version_id for t in tasks if t.id == budget.scope_id}
    elif budget.scope == 'agent':
        skill = await db.get(AgentTeamSkill, budget.scope_id)
        ids = {t.plan_version_id for t in tasks if skill and t.team_id == skill.team_id and t.skill_profile_id == skill.skill_profile_id}
    elif budget.scope == 'session':
        session = await db.get(AgentSession, budget.scope_id)
        ids = {t.plan_version_id for t in tasks if session and t.team_id == session.team_id and t.skill_profile_id == session.skill_profile_id}
    else:
        # Le modèle n'est connu qu'à l'admission ; suspend uniquement les plans qui l'ont utilisé.
        usages = (await db.scalars(scope_query(budget.project_id, budget.scope, budget.scope_id))).all()
        task_ids = {r.context.get('task') for r in usages}
        ids = {t.plan_version_id for t in tasks if t.id in task_ids}
    return [p for p in plans if p.id in ids]


async def suspends(db, plan, level):
    if not level:
        return False
    if level != 'warning' or plan.autonomy_mode == 'per_action':
        return True
    return plan.autonomy_mode == 'sensitive' and bool(await db.scalar(select(AgentTask.id).where(
        AgentTask.plan_version_id == plan.id, AgentTask.action_class == 'sensitive').limit(1)))


async def evaluate(db, project_id, *, now=None):
    """Mutation explicite ; unicités et verrou garantissent les rejeux/concurrence."""
    await lock_project(db, project_id)
    now = now or utcnow()
    await db.flush()
    for budget in await current_budgets(db, project_id):
        usage = await consumption(db, budget, now)
        if not usage['level']:
            continue
        alert = await db.scalar(select(BudgetAlert).where(BudgetAlert.budget_id == budget.id,
            BudgetAlert.period_start == usage['period_start'], BudgetAlert.level == usage['level']))
        if not alert:
            alert = BudgetAlert(project_id=project_id, budget_id=budget.id,
                period_start=usage['period_start'], level=usage['level'])
            db.add(alert)
            await db.flush()
            audit(db, 'budget.alert', 'success', project_id=project_id, target_type='budget_alert', target_id=alert.id,
                details={'budget_id':budget.id,'level':alert.level,'period_start':alert.period_start.isoformat()})
        for plan in await applicable_plans(db, budget):
            if not await suspends(db, plan, alert.level):
                continue
            old = await db.scalar(select(BudgetSuspension.id).where(BudgetSuspension.alert_id == alert.id,
                BudgetSuspension.plan_id == plan.id))
            if old:
                continue
            hold = BudgetSuspension(project_id=project_id, alert_id=alert.id, plan_id=plan.id)
            db.add(hold)
            await db.flush()
            audit(db, 'budget.suspend', 'success', project_id=project_id, target_type='budget_suspension', target_id=hold.id,
                details={'plan_id':plan.id,'alert_id':alert.id,'autonomy_mode':plan.autonomy_mode})


async def guard_plan(db, plan):
    await lock_project(db, plan.project_id)
    if await db.scalar(select(BudgetSuspension.id).where(BudgetSuspension.plan_id == plan.id,
            BudgetSuspension.resumed_at.is_(None)).limit(1)):
        raise RuntimeDenied('budget_suspended')
    for budget in await current_budgets(db, plan.project_id):
        if any(p.id == plan.id for p in await applicable_plans(db, budget)):
            if await suspends(db, plan, (await consumption(db, budget, utcnow()))['level']):
                raise RuntimeDenied('budget_suspended')


async def admission(db, project_id, context, *, call, token_bound, timeout, cost_bound, pricing):
    """Contrôle de la charge projetée avant création du registre ou appel fournisseur."""
    task = await db.get(AgentTask, context.get('task')) if context.get('task') and not context.get('coffy_run') else None
    if task:
        await guard_plan(db, await db.get(AgentPlanVersion, task.plan_version_id))
    budget_ids = []
    for b in await current_budgets(db, project_id):
        if context.get(b.scope) != b.scope_id:
            if not call and b.scope == 'model':
                raise RuntimeDenied('budget_metrics_unavailable')
            continue
        budget_ids.append(b.id)
        used = await consumption(db, b, utcnow())
        bound = {'tokens':token_bound if call else None, 'seconds':Decimal(str(timeout)), 'cost':cost_bound}[b.metric]
        if (bound is None or used['charged'] is None or b.metric == 'cost' and
                (not pricing or pricing['currency'] != b.currency)):
            raise RuntimeDenied('budget_metrics_unavailable')
        remaining = b.amount - used['charged']
        if b.metric == 'seconds':
            timeout = min(timeout, float(remaining))
            if timeout < 1:
                raise RuntimeDenied('budget_exhausted')
        elif bound > remaining:
            raise RuntimeDenied('budget_exhausted')
    return timeout, budget_ids


async def acknowledge(db, project_id, identity, actor, reason):
    reason = clean(reason)
    await lock_project(db, project_id)
    row = await db.scalar(select(BudgetAlert).where(BudgetAlert.id == identity, BudgetAlert.project_id == project_id))
    if not row:
        raise RuntimeDenied('alert_unavailable')
    if row.acknowledged_at is None:
        row.acknowledged_at, row.acknowledged_by = utcnow(), actor.id
        audit(db, 'budget.acknowledge', 'success', project_id=project_id, actor_id=actor.id,
            target_type='budget_alert', target_id=row.id, details={'reason':reason})
    return row


async def resume(db, project_id, identity, actor, reason):
    reason = clean(reason)
    await lock_project(db, project_id)
    row = await db.scalar(select(BudgetSuspension).where(BudgetSuspension.id == identity,
        BudgetSuspension.project_id == project_id))
    if not row:
        raise RuntimeDenied('suspension_unavailable')
    if row.resumed_at is not None:
        return row
    plan = await db.get(AgentPlanVersion, row.plan_id)
    for budget in await current_budgets(db, project_id):
        if any(p.id == plan.id for p in await applicable_plans(db, budget)) and await suspends(
                db, plan, (await consumption(db, budget, utcnow()))['level']):
            raise RuntimeDenied('budget_still_blocking')
    row.resumed_at, row.resumed_by = utcnow(), actor.id
    audit(db, 'budget.resume', 'success', project_id=project_id, actor_id=actor.id,
        target_type='budget_suspension', target_id=row.id, details={'reason':reason,'plan_id':row.plan_id})
    return row


async def snapshot(db, project_id):
    """Lecture uniquement ; les niveaux courants sont distincts des alertes historiques."""
    budgets = (await db.scalars(select(ExecutionBudget).where(ExecutionBudget.project_id == project_id,
        ExecutionBudget.is_current.is_(True)).order_by(ExecutionBudget.created_at, ExecutionBudget.id))).all()
    alerts = (await db.scalars(select(BudgetAlert).where(BudgetAlert.project_id == project_id)
        .order_by(BudgetAlert.created_at.desc(), BudgetAlert.id))).all()
    holds = (await db.scalars(select(BudgetSuspension).where(BudgetSuspension.project_id == project_id)
        .order_by(BudgetSuspension.created_at.desc(), BudgetSuspension.id))).all()
    history = (await db.scalars(select(ExecutionBudget).where(ExecutionBudget.project_id == project_id)
        .order_by(ExecutionBudget.created_at.desc(), ExecutionBudget.id))).all()
    return dict(history=[view(b) for b in history], budgets=[{**view(b), **await consumption(db,b,utcnow())} for b in budgets],
        alerts=[view(a) for a in alerts], suspensions=[view(h) for h in holds],
        unacknowledged=sum(a.acknowledged_at is None for a in alerts),
        suspended=sum(h.resumed_at is None for h in holds))
