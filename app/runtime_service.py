"""Routage explicite et quotas atomiques ; aucune instruction agent n'élargit une limite."""
import hashlib
import json
from datetime import timedelta
from decimal import Decimal, ROUND_CEILING
from uuid import uuid4

from sqlalchemy import func, select, text
from app.agent_catalog import CLIENT_CATALOG
from app.agent_models import AgentSession, AgentTask, AgentTaskClaim, AgentTaskRun, AgentTeam, AgentTeamSkill
from app.coffy_models import ScheduledLLMRun, ScheduledLLMTask
from app.identity_models import AuditEvent, utcnow
from app.identity_service import audit
from app.llm_models import LLMConnectionCheck, LLMProfileVersion
from app.llm_service import default_profile, invocation_ready
from app.llm_trace import redact
from app.runtime_models import RuntimeBinding, RuntimeQuota, RuntimeSelection, RuntimeUsage, RuntimeVersion


class RuntimeDenied(ValueError):
    def __init__(self, code):
        self.code = code
        super().__init__(code)


def model_key(profile):
    return hashlib.sha256(json.dumps([profile.provider, profile.model_name], ensure_ascii=True).encode()).hexdigest()


def budget_cost(pricing,input_tokens,output_tokens):
    """Charge prudente : un montant positif inférieur au micro ne devient pas gratuit."""
    if not pricing or any(type(n) is not int or n < 0 for n in (input_tokens,output_tokens)):
        return None
    amount=(Decimal(pricing['input_per_million'])*input_tokens +
            Decimal(pricing['output_per_million'])*output_tokens)/Decimal(1000000)
    return amount.quantize(Decimal('.000001'),rounding=ROUND_CEILING)


async def lock_project(db, project_id):
    # Même lock pour configuration, admission et clôture ; aucune attente réseau sous lock.
    key = int.from_bytes(hashlib.sha256(('runtime:' + project_id).encode()).digest()[:8], 'big', signed=True)
    await db.execute(text('SELECT pg_advisory_xact_lock(:key)'), {'key': key})


async def validate_scope(db, project_id, scope, scope_id):
    if scope == 'project':
        valid = scope_id == project_id
    elif scope == 'agent':
        valid = await db.scalar(select(AgentTeamSkill.id).join(AgentTeam).where(
            AgentTeamSkill.id == scope_id, AgentTeam.project_id == project_id))
    elif scope in ('session', 'task'):
        model = AgentSession if scope == 'session' else AgentTask
        valid = await db.scalar(select(model.id).where(model.id == scope_id, model.project_id == project_id))
        if scope == 'task' and not valid:
            valid = await db.scalar(select(ScheduledLLMTask.id).where(
                ScheduledLLMTask.id == scope_id, ScheduledLLMTask.project_id == project_id,
                ScheduledLLMTask.status != 'deleted'))
    elif scope == 'model':
        profiles = (await db.scalars(select(LLMProfileVersion))).all()
        valid = any(model_key(p) == scope_id for p in profiles)
    else:
        valid = False
    if not valid:
        raise RuntimeDenied('scope_unavailable')


async def usable_profile(db, identity, *, checked=True):
    profile = await db.get(LLMProfileVersion, identity) if identity else await default_profile(db)
    if profile is None or not invocation_ready(profile):
        raise RuntimeDenied('profile_unavailable')
    current = await db.scalar(select(LLMProfileVersion).where(
        LLMProfileVersion.profile_id == profile.profile_id, LLMProfileVersion.is_current.is_(True)))
    check = await db.scalar(select(LLMConnectionCheck).where(LLMConnectionCheck.profile_version_id == profile.id)
                            .order_by(LLMConnectionCheck.created_at.desc(), LLMConnectionCheck.id).limit(1))
    if current is None or not current.enabled or checked and (check is None or check.outcome != 'success'):
        raise RuntimeDenied('profile_unavailable')
    return profile


async def runtime_guard(db, runtime):
    current = await db.scalar(select(RuntimeVersion).where(RuntimeVersion.runtime_id == runtime.runtime_id,
                                                         RuntimeVersion.is_current.is_(True)))
    if current is None or not current.enabled or not runtime.enabled:
        raise RuntimeDenied('runtime_disabled')
    if runtime.kind == 'llm':
        await usable_profile(db, runtime.profile_version_id)


async def revise_runtime(db, project_id, actor, data):
    await lock_project(db, project_id)
    old = None
    if data.runtime_id:
        old = await db.scalar(select(RuntimeVersion).where(RuntimeVersion.runtime_id == str(data.runtime_id),
            RuntimeVersion.project_id == project_id, RuntimeVersion.is_current.is_(True)))
        if old is None:
            raise RuntimeDenied('runtime_unavailable')
    if data.expected_version != (old.version if old else 0):
        raise RuntimeDenied('revision_stale')
    if data.kind == 'llm' and data.enabled:
        await usable_profile(db, str(data.profile_version_id))
    if data.kind == 'client' and data.client_code not in CLIENT_CATALOG:
        raise RuntimeDenied('client_unavailable')
    if old:
        old.is_current = False
        await db.flush()
    runtime = RuntimeVersion(project_id=project_id, runtime_id=old.runtime_id if old else str(uuid4()),
        version=old.version + 1 if old else 1, name=clean(data.name), kind=data.kind,
        profile_version_id=str(data.profile_version_id) if data.profile_version_id else None,
        client_code=data.client_code, timeout_seconds=data.timeout_seconds, include_memory=data.include_memory,
        enabled=data.enabled, reason=clean(data.reason), created_by=actor.id)
    db.add(runtime)
    await db.flush()
    audit(db, 'runtime.revise', 'success', actor_id=actor.id, project_id=project_id,
          target_type='runtime_version', target_id=runtime.id, details={'version': runtime.version})
    return runtime


def clean(value):
    if redact(value) != value or any(ord(c) < 32 for c in value):
        raise RuntimeDenied('sensitive_input')
    if not value.strip():
        raise RuntimeDenied('invalid_input')
    return value.strip()


async def revise_policy(db, project_id, actor, data, *, quota=False):
    await validate_scope(db, project_id, data.scope, data.scope_id)
    await lock_project(db, project_id)
    model = RuntimeQuota if quota else RuntimeBinding
    old = await db.scalar(select(model).where(model.project_id == project_id, model.scope == data.scope,
                                            model.scope_id == data.scope_id, model.is_current.is_(True)))
    if data.expected_version != (old.version if old else 0):
        raise RuntimeDenied('revision_stale')
    if not quota and data.runtime_version_id:
        runtime = await db.get(RuntimeVersion, str(data.runtime_version_id))
        if not runtime or runtime.project_id != project_id:
            raise RuntimeDenied('runtime_unavailable')
        await runtime_guard(db, runtime)
    reason = clean(data.reason)
    if old:
        old.is_current = False
        await db.flush()
    values = {key: getattr(data, key) for key in ('max_tokens','max_seconds','max_concurrency','max_cost','currency')} if quota else {
        'runtime_version_id': str(data.runtime_version_id) if data.runtime_version_id else None}
    row = model(project_id=project_id, scope=data.scope, scope_id=data.scope_id,
                version=old.version + 1 if old else 1, reason=reason, created_by=actor.id, **values)
    db.add(row)
    await db.flush()
    audit(db, 'quota.revise' if quota else 'runtime.bind', 'success', actor_id=actor.id, project_id=project_id,
          target_type=model.__tablename__, target_id=row.id, details={'scope': row.scope, 'scope_id': row.scope_id, 'version': row.version})
    return row


async def select_runtime(db, attempt, session, task, *, explicit_profile=None, client=False):
    await lock_project(db, task.project_id)
    old = await db.scalar(select(RuntimeSelection).where(RuntimeSelection.attempt_id == attempt.id))
    if old:
        previous_runtime = await db.get(RuntimeVersion, old.runtime_version_id) if old.runtime_version_id else None
        if client and (not previous_runtime or previous_runtime.kind != 'client' or previous_runtime.client_code != session.client_code):
            raise RuntimeDenied('client_incompatible')
        if not client and not old.profile_version_id:
            raise RuntimeDenied('client_incompatible')
        if explicit_profile and explicit_profile != old.profile_version_id:
            raise RuntimeDenied('selection_conflict')
        await guard_selection(db, old)
        return old
    agent_id = await db.scalar(select(AgentTeamSkill.id).where(AgentTeamSkill.team_id == task.team_id,
                                                            AgentTeamSkill.skill_profile_id == task.skill_profile_id))
    await ensure_task_recoverable(db,task.id)
    context = {'project': task.project_id, 'agent': agent_id, 'session': session.id, 'task': task.id,'attempt':attempt.id}
    binding = None
    for scope in ('task', 'session', 'agent', 'project'):
        binding = await db.scalar(select(RuntimeBinding).where(RuntimeBinding.project_id == task.project_id,
            RuntimeBinding.scope == scope, RuntimeBinding.scope_id == context[scope],
            RuntimeBinding.is_current.is_(True), RuntimeBinding.runtime_version_id.is_not(None)))
        if binding:
            break
    runtime = await db.get(RuntimeVersion, binding.runtime_version_id) if binding else None
    if runtime:
        await runtime_guard(db, runtime)
        if explicit_profile and explicit_profile != runtime.profile_version_id:
            raise RuntimeDenied('selection_conflict')
    if client:
        if not runtime or runtime.kind != 'client' or runtime.client_code != session.client_code:
            raise RuntimeDenied('client_incompatible')
        profile = None
    else:
        if runtime and runtime.kind != 'llm':
            raise RuntimeDenied('client_incompatible')
        profile = await usable_profile(db, runtime.profile_version_id if runtime else explicit_profile,
                                       checked=bool(runtime or explicit_profile))
        context['model'] = model_key(profile)
    memory = None
    if runtime and runtime.include_memory:
        from app.memory_service import memory_context
        memory = await memory_context(db, task.project_id)
        memory = redact(json.loads(json.dumps(memory, default=str)))
    row = RuntimeSelection(attempt_id=attempt.id, project_id=task.project_id,
        runtime_version_id=runtime.id if runtime else None, binding_id=binding.id if binding else None,
        profile_version_id=profile.id if profile else None,
        origin=binding.scope if binding else 'explicit' if explicit_profile else 'default', context=context,
        memory_snapshot=memory, deadline=utcnow() + timedelta(seconds=runtime.timeout_seconds if runtime else 80))
    db.add(row)
    await db.flush()
    audit(db, 'runtime.select', 'success', actor_id=session.created_by, project_id=task.project_id, target_type='runtime_selection',
          target_id=row.id, details={'attempt_id': attempt.id,'origin': row.origin,'runtime_version_id': row.runtime_version_id,
                                   'profile_version_id': row.profile_version_id})
    return row


async def memory_available(db,project_id,snapshot):
    from app.memory_models import ProjectMemory, ProjectMemoryVersion, ProjectMemoryGrant
    for reference in snapshot.get('references',[]):
        memory = await db.get(ProjectMemory, reference['memory_id'])
        version = await db.get(ProjectMemoryVersion, reference['id'])
        if not memory or not version or version.memory_id != memory.id or version.status != 'Validé' or memory.reference_version_id != version.id:
            return False
        if memory.scope == 'project' and memory.project_id != project_id:
            return False
        if memory.scope == 'global':
            grant = await db.scalar(select(ProjectMemoryGrant.id).where(ProjectMemoryGrant.project_id == project_id,
                ProjectMemoryGrant.version_id == version.id, ProjectMemoryGrant.revoked_at.is_(None)))
            if not grant:
                return False
    return True


async def guard_selection(db, selection):
    if selection.deadline <= utcnow():
        raise RuntimeDenied('runtime_timeout')
    if selection.runtime_version_id:
        await runtime_guard(db, await db.get(RuntimeVersion, selection.runtime_version_id))
    if selection.profile_version_id:
        try:
            await usable_profile(db, selection.profile_version_id, checked=selection.origin != 'default')
        except RuntimeDenied:
            if selection.origin == 'default':
                raise RuntimeDenied('configuration_unavailable') from None
            raise
    if selection.memory_snapshot and not await memory_available(db,selection.project_id,selection.memory_snapshot):
        raise RuntimeDenied('memory_unavailable')
    uncertain = await db.scalar(select(RuntimeUsage.id).where(RuntimeUsage.project_id == selection.project_id,
        RuntimeUsage.context['task'].as_string() == selection.context['task'], RuntimeUsage.status == 'unknown'))
    # Une tentative suivante peut reprendre après décision humaine ; celle clôturée ne peut utiliser un retour tardif.
    if uncertain:
        rows = (await db.scalars(select(RuntimeUsage).where(RuntimeUsage.status == 'unknown',
            RuntimeUsage.context['task'].as_string() == selection.context['task']))).all()
        from app.llm_models import LLMCallAttempt
        attempt = await db.get(AgentTaskRun, selection.attempt_id)
        for row in rows:
            call = await db.get(LLMCallAttempt, row.call_id) if row.call_id else None
            if row.external_selection_id == selection.id or call and call.generation_run_id == attempt.generation_run_id:
                raise RuntimeDenied('execution_unknown')


def scope_query(project_id, scope, scope_id):
    query = select(RuntimeUsage).where(RuntimeUsage.project_id == project_id)
    return query if scope == 'project' else query.where(RuntimeUsage.context[scope].as_string() == scope_id)


async def totals(db, project_id, scope, scope_id):
    where = scope_query(project_id, scope, scope_id).whereclause
    fields = [RuntimeUsage.charged_tokens, RuntimeUsage.charged_seconds, RuntimeUsage.charged_cost,
              RuntimeUsage.actual_tokens, RuntimeUsage.actual_seconds, RuntimeUsage.actual_cost]
    values = (await db.execute(select(func.count(), func.count().filter(RuntimeUsage.status == 'active'),
        *[func.coalesce(func.sum(f),0) for f in fields],
        *[func.count().filter(f.is_(None)) for f in fields], func.count(func.distinct(RuntimeUsage.currency))).where(where))).one()
    count, active = values[:2]
    result = {'count': count, 'active': active}
    result['unmetered'] = await db.scalar(select(func.count()).select_from(RuntimeUsage).where(where, RuntimeUsage.call_id.is_(None)))
    for i, name in enumerate(('charged_tokens','charged_seconds','charged_cost','tokens','seconds','cost')):
        result[name] = values[2+i] if not values[8+i] else None
    currencies = (await db.scalars(select(RuntimeUsage.currency).where(where, RuntimeUsage.currency.is_not(None)).distinct())).all()
    result['currency'] = currencies[0] if len(currencies) == 1 else None
    if len(currencies) > 1:
        result['cost'] = result['charged_cost'] = None
    if not count:
        result['tokens'] = result['seconds'] = result['cost'] = None
    return result


async def reserve(db, project_id, context, *, call=None, selection=None, input_bound=0, output_bound=0,
                  timeout=20, pricing=None, actor_id=None):
    await lock_project(db, project_id)
    policies = (await db.scalars(select(RuntimeQuota).where(RuntimeQuota.project_id == project_id,
                                                           RuntimeQuota.is_current.is_(True)))).all()
    matching = [p for p in policies if context.get(p.scope) == p.scope_id]
    if not call and any(p.scope == 'model' and any((p.max_tokens,p.max_seconds,p.max_concurrency,p.max_cost)) for p in policies):
        raise RuntimeDenied('metrics_unavailable')
    token_bound = input_bound + output_bound
    cost_bound = budget_cost(pricing, input_bound, output_bound) if call else None
    for p in matching:
        used = await totals(db, project_id, p.scope, p.scope_id)
        if p.max_concurrency and used['active'] >= p.max_concurrency:
            raise RuntimeDenied('quota_concurrency')
        if p.max_tokens and (not call or used['unmetered']):
            raise RuntimeDenied('metrics_unavailable')
        if p.max_tokens and used['charged_tokens'] + token_bound > p.max_tokens:
            raise RuntimeDenied('quota_tokens')
        if p.max_seconds:
            timeout = min(timeout, float(p.max_seconds) - float(used['charged_seconds']))
        if p.max_cost:
            if cost_bound is None or used['charged_cost'] is None or pricing['currency'] != p.currency or (
                used['count'] and used['currency'] != p.currency):
                raise RuntimeDenied('pricing_unavailable')
            if used['charged_cost'] + cost_bound > p.max_cost:
                raise RuntimeDenied('quota_cost')
    from app.budget_service import admission
    timeout, budget_ids = await admission(db, project_id, context, call=call, token_bound=token_bound,
        timeout=timeout, cost_bound=cost_bound, pricing=pricing)
    timeout = int(timeout)
    if timeout < 1:
        raise RuntimeDenied('quota_duration')
    row = RuntimeUsage(project_id=project_id, call_id=call.id if call else None,
        external_selection_id=selection.id if selection and not call else None, context=context,
        policy_ids=[p.id for p in matching] + budget_ids, charged_tokens=token_bound, charged_seconds=Decimal(timeout),
        charged_cost=cost_bound, currency=pricing['currency'] if pricing else None)
    db.add(row)
    await db.flush()
    audit(db, 'quota.reserve', 'success', actor_id=actor_id, project_id=project_id,
          target_type='runtime_usage', target_id=row.id, details={'policy_ids': row.policy_ids,'call_id': row.call_id})
    from app.budget_service import evaluate
    await evaluate(db, project_id)
    return row, timeout


async def reserve_call(db, run, profile, call, messages):
    if isinstance(run, ScheduledLLMRun):
        context = {'project': run.project_id, 'task': run.task_id,
                   'model': model_key(profile), 'coffy_run': run.id}
        input_bound = len(json.dumps(messages, ensure_ascii=False).encode('utf-8')) + 1024
        return await reserve(db, run.project_id, context, call=call, input_bound=input_bound,
            output_bound=run.parameters['max_output_tokens'],
            timeout=min(profile.timeout_seconds, run.parameters.get('timeout_seconds', 20), 20),
            pricing=run.pricing_snapshot, actor_id=run.requested_by)
    attempt = await db.scalar(select(AgentTaskRun).where(AgentTaskRun.generation_run_id == run.id))
    selection = await db.scalar(select(RuntimeSelection).where(RuntimeSelection.attempt_id == attempt.id)) if attempt else None
    context = selection.context if selection else {'project': run.project_id, 'model': model_key(profile)}
    timeout = min(profile.timeout_seconds, 20)
    if selection:
        await guard_selection(db, selection)
        timeout = min(timeout, (selection.deadline - utcnow()).total_seconds())
    # Les octets encodés surestiment généralement les tokens, mais pas les ajouts invisibles du fournisseur.
    input_bound = len(json.dumps(messages, ensure_ascii=False).encode('utf-8')) + 1024
    return await reserve(db, run.project_id, context, call=call, input_bound=input_bound,
        output_bound=run.parameters['max_output_tokens'], timeout=timeout, pricing=run.pricing_snapshot,
        actor_id=run.requested_by)


async def finish_call(db, call, elapsed):
    # Ne pas flusher la trace avant le verrou : la réconciliation prend projet puis appel.
    with db.no_autoflush:
        row = await db.scalar(select(RuntimeUsage).where(RuntimeUsage.call_id == call.id))
        if row:
            await lock_project(db, row.project_id)
    if not row:
        return True
    await db.refresh(row)
    if row.status != 'active':
        return False
    row.status, row.ended_at, row.error_code = call.status, call.ended_at, call.error_code
    row.actual_seconds = Decimal(str(max(0, elapsed))).quantize(Decimal('.000001'))
    row.charged_seconds = row.actual_seconds
    if call.input_tokens is not None and call.output_tokens is not None:
        row.actual_tokens = row.charged_tokens = call.input_tokens + call.output_tokens
    row.actual_cost = call.estimated_cost
    actual_charge=budget_cost(call.pricing_snapshot,call.input_tokens,call.output_tokens)
    if actual_charge is not None:
        row.charged_cost = actual_charge
    audit(db, 'quota.complete', 'success', project_id=row.project_id,
          target_type='runtime_usage', target_id=row.id, details={'status':row.status})
    from app.budget_service import evaluate
    await evaluate(db, row.project_id, now=row.started_at)
    return True


async def begin_client(db, attempt, session, task):
    selection = await select_runtime(db, attempt, session, task, client=True)
    old = await db.scalar(select(RuntimeUsage).where(RuntimeUsage.external_selection_id == selection.id))
    if old:
        if old.status != 'active':
            raise RuntimeDenied('execution_unknown')
        return selection, old
    timeout = min((selection.deadline-utcnow()).total_seconds(),
                  (session.max_expires_at-utcnow()).total_seconds())
    usage, timeout = await reserve(db, task.project_id, selection.context, selection=selection,
                                 timeout=timeout, actor_id=session.created_by)
    selection.deadline = min(selection.deadline, utcnow()+timedelta(seconds=timeout))
    return selection, usage


async def finish_client(db, attempt, status):
    selection = await db.scalar(select(RuntimeSelection).where(RuntimeSelection.attempt_id == attempt.id))
    if not selection:
        return
    row = await db.scalar(select(RuntimeUsage).where(RuntimeUsage.external_selection_id == selection.id))
    if not row:
        return
    await lock_project(db, row.project_id)
    await db.refresh(row)
    if row.status == 'active':
        row.status = 'completed' if status == 'completed' else 'failed'
        row.ended_at = utcnow()
        row.actual_seconds = row.charged_seconds = Decimal(str((row.ended_at-row.started_at).total_seconds())).quantize(Decimal('.000001'))
        audit(db,'quota.complete','success',project_id=row.project_id,target_type='runtime_usage',target_id=row.id,
              details={'status':row.status, 'unmetered_client':True})
        from app.budget_service import evaluate
        await evaluate(db, row.project_id, now=row.started_at)


async def guard_progress(db, claim, task, session):
    attempt = await db.scalar(select(AgentTaskRun).where(AgentTaskRun.claim_id == claim.id))
    selection = await db.scalar(select(RuntimeSelection).where(RuntimeSelection.attempt_id == attempt.id)) if attempt else None
    if selection:
        await guard_selection(db, selection)
        return
    agent_id = await db.scalar(select(AgentTeamSkill.id).where(AgentTeamSkill.team_id == task.team_id,
                                                            AgentTeamSkill.skill_profile_id == task.skill_profile_id))
    context = {'project': task.project_id, 'task': task.id, 'session':session.id, 'agent':agent_id}
    from app.budget_service import current_budgets
    if any(context.get(b.scope) == b.scope_id or b.scope == 'model'
           for b in await current_budgets(db, task.project_id)):
        raise RuntimeDenied('runtime_start_required')
    for model in (RuntimeBinding, RuntimeQuota):
        policies = (await db.scalars(select(model).where(model.project_id == task.project_id, model.is_current.is_(True)))).all()
        if any((context.get(p.scope) == p.scope_id or model is RuntimeQuota and p.scope == 'model') and
               (any((p.max_tokens,p.max_seconds,p.max_concurrency,p.max_cost)) if model is RuntimeQuota else p.runtime_version_id)
               for p in policies):
            raise RuntimeDenied('runtime_start_required')


async def reconcile(db, project_id, identity, actor, reason, confirmed):
    if not confirmed:
        raise RuntimeDenied('stop_confirmation_required')
    reason = clean(reason)
    row = await db.scalar(select(RuntimeUsage).where(RuntimeUsage.project_id == project_id, RuntimeUsage.id == identity))
    if row is None:
        raise RuntimeDenied('usage_not_active')
    from app.llm_models import LLMCallAttempt
    from app.business_models import AnalysisRun,GenerationRun
    call=await db.get(LLMCallAttempt,row.call_id) if row.call_id else None
    selection=await db.get(RuntimeSelection,row.external_selection_id) if row.external_selection_id else None
    attempt=(await db.get(AgentTaskRun,selection.attempt_id) if selection else
        await db.scalar(select(AgentTaskRun).where(AgentTaskRun.generation_run_id==call.generation_run_id))
        if call and call.generation_run_id else None)
    task=None
    if attempt:
        claim=await db.get(AgentTaskClaim,attempt.claim_id)
        await db.scalar(select(AgentSession).where(AgentSession.id==claim.session_id).with_for_update())
        from app.agent_progress import lock_task_plan
        await lock_task_plan(db,attempt.task_id,project_id)
        task=await db.get(AgentTask,attempt.task_id)
        await db.refresh(task)
    await lock_project(db, project_id)
    await db.refresh(row)
    if row.status != 'active':
        raise RuntimeDenied('usage_not_active')
    row.status, row.ended_at, row.reconciled_by = 'unknown', utcnow(), actor.id
    row.error_code = 'human_reconciled'
    if call:
        run=await db.get(GenerationRun if call.generation_run_id else AnalysisRun,
                         call.generation_run_id or call.analysis_run_id,with_for_update=True)
        if run.status in ('queued','running'):
            run.status,run.error_code,run.completed_at='failed','execution_unknown',utcnow()
        call.status,call.error_code,call.ended_at='failed','execution_unknown',utcnow()
    if task:
        if task.status=='claimed' and task.claimed_session_id==claim.session_id:
            from app.agent_routing import _release_task
            await _release_task(db,task,'runtime_error')
            task.status='failed'
        elif task.status=='ready':
            task.status='failed'
        from app.agent_progress import aggregate_plan
        await db.flush()
        await aggregate_plan(db,task.plan_version_id,actor_id=actor.id)
    audit(db,'quota.reconcile','success',project_id=project_id,actor_id=actor.id,target_type='runtime_usage',target_id=row.id,
          details={'reason':reason, 'charge_preserved':True})
    return row


async def ensure_task_recoverable(db,task_id):
    rows=(await db.scalars(select(RuntimeUsage).where(RuntimeUsage.context['task'].as_string()==task_id,
                                                    RuntimeUsage.status.in_(('active','unknown'))))).all()
    for row in rows:
        if row.status=='active':
            raise RuntimeDenied('runtime_recovery_required')
        previous=row.context.get('attempt')
        if not previous or not await db.scalar(select(AuditEvent.id).where(AuditEvent.action=='agent.task.retry',
            AuditEvent.target_id==task_id,AuditEvent.actor_id.is_not(None),AuditEvent.details['attempt_id'].as_string()==previous)):
            raise RuntimeDenied('runtime_recovery_required')


def view(row):
    return {c.name:getattr(row,c.name) for c in row.__table__.columns}
