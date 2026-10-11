"""Admission, dispatch et clôture des runs CoFFY ; aucun résultat n'est validé ici."""

from datetime import timedelta

from sqlalchemy import select

from app.coffy_models import ScheduledLLMOccurrence, ScheduledLLMRun, ScheduledLLMTask
from app.coffy_service import current_version
from app.identity_models import Project, User, utcnow
from app.identity_service import audit, project_permission
from app.llm_models import LLMCallAttempt, LLMProfileVersion
from app.llm_service import LLMInvocationError, invoke_profile
from app.llm_trace import apply_usage, pricing_for, redact, traced_invoke
from app.runtime_models import RuntimeUsage
from app.runtime_service import RuntimeDenied, lock_project, usable_profile
from app.schedule_core import due_window, next_due

ACTIVE_RUNS = ("pending", "queued", "running", "retrying")
TERMINAL_RUNS = ("success", "failed", "timeout", "cancelled")


def run_view(run, *, include_output=False):
    fields = ("id", "occurrence_id", "project_id", "task_id", "task_version_id",
              "llm_profile_version_id", "requested_by", "status", "error_code", "parameters",
              "input_tokens", "output_tokens", "estimated_cost", "cost_currency", "pricing_source",
              "pricing_date", "dispatch_attempts", "created_at", "started_at", "completed_at")
    data = {key: getattr(run, key) for key in fields}
    data["duration_seconds"] = (run.completed_at - run.started_at).total_seconds() if run.started_at and run.completed_at else None
    if include_output:
        data["raw_output"] = run.raw_output
        data["output_status"] = "Brouillon IA — non validé" if run.raw_output is not None else None
    return data


def occurrence_view(row):
    return {key: getattr(row, key) for key in ("id", "project_id", "task_id", "task_version_id",
        "scheduled_at", "origin", "status", "skip_reason", "coalesced_count", "count_truncated", "created_at")}


async def _owner_allowed(db, task):
    creator = await db.get(User, task.created_by)
    return bool(creator and creator.is_active and await project_permission(
        db, creator, task.project_id, "scheduled_task.manage"))


async def guard_run(db, reference):
    """Garde réappliquée avant tentative et avant toute conservation de sortie."""
    run = await db.get(ScheduledLLMRun, reference.id)
    if run is None or run.status not in {"running", "retrying"}:
        raise RuntimeDenied("execution_unavailable")
    task = await db.get(ScheduledLLMTask, run.task_id)
    project = await db.get(Project, run.project_id)
    if task is None or task.status != "active" or project is None or project.status == "archived":
        raise RuntimeDenied("task_inactive")
    if not await _owner_allowed(db, task):
        raise RuntimeDenied("permission_revoked")
    if run.requested_by:
        requester = await db.get(User, run.requested_by)
        if requester is None or not requester.is_active or not await project_permission(
                db, requester, run.project_id, "scheduled_task.run"):
            raise RuntimeDenied("permission_revoked")
    return await usable_profile(db, run.llm_profile_version_id)


async def _active_run(db, task_id):
    return await db.scalar(select(ScheduledLLMRun.id).where(
        ScheduledLLMRun.task_id == task_id, ScheduledLLMRun.status.in_(ACTIVE_RUNS)).limit(1))


async def _new_run(db, task, version, profile, occurrence, actor_id):
    values = version.parameters
    parameters = {"max_output_tokens": values.get("max_output_tokens", profile.max_output_tokens),
                  "timeout_seconds": values.get("timeout_seconds", profile.timeout_seconds),
                  "max_retries": values.get("max_retries", profile.max_retries)}
    if "temperature" in values:
        parameters["temperature"] = values["temperature"]
    run = ScheduledLLMRun(occurrence_id=occurrence.id, project_id=task.project_id, task_id=task.id,
        task_version_id=version.id, llm_profile_version_id=profile.id, requested_by=actor_id,
        status="pending", parameters=parameters, pricing_snapshot=pricing_for(profile),
        dispatch_attempts=0)
    db.add(run)
    await db.flush()
    audit(db, "scheduled_task.run.reserve", "success", actor_id=actor_id, project_id=task.project_id,
          target_type="scheduled_llm_run", target_id=run.id,
          details={"occurrence_id": occurrence.id, "task_version_id": version.id,
                   "profile_version_id": profile.id, "origin": occurrence.origin})
    return run


async def manual_run(db, project_id, task_id, request_key, expected_revision, actor_id):
    task = await db.scalar(select(ScheduledLLMTask).where(
        ScheduledLLMTask.id == task_id, ScheduledLLMTask.project_id == project_id,
        ScheduledLLMTask.status != "deleted").with_for_update().execution_options(populate_existing=True))
    if task is None:
        raise LookupError("Tâche introuvable.")
    existing = await db.scalar(select(ScheduledLLMOccurrence).where(
        ScheduledLLMOccurrence.task_id == task_id, ScheduledLLMOccurrence.request_key == request_key))
    if existing:
        return await db.scalar(select(ScheduledLLMRun).where(ScheduledLLMRun.occurrence_id == existing.id)), False
    if task.revision != expected_revision:
        raise ValueError("Révision périmée : rechargez la tâche.")
    if task.status != "active" or await _active_run(db, task_id):
        raise ValueError("Tâche inactive ou exécution déjà en cours.")
    if not await _owner_allowed(db, task):
        raise ValueError("Responsable de tâche non habilité.")
    version = await current_version(db, task)
    profile = await usable_profile(db, version.llm_profile_version_id)
    occurrence = ScheduledLLMOccurrence(project_id=project_id, task_id=task_id,
        task_version_id=version.id, scheduled_at=utcnow(), origin="manual",
        request_key=request_key, status="reserved", coalesced_count=0, count_truncated=False)
    db.add(occurrence)
    await db.flush()
    return await _new_run(db, task, version, profile, occurrence, actor_id), True


async def _skip(db, task, version, at, reason, count=0, truncated=False):
    row = ScheduledLLMOccurrence(project_id=task.project_id, task_id=task.id,
        task_version_id=version.id, scheduled_at=at, origin="scheduled", status="skipped",
        skip_reason=reason, coalesced_count=count, count_truncated=truncated)
    db.add(row)
    await db.flush()
    audit(db, "scheduled_task.occurrence.skip", "success", project_id=task.project_id,
          target_type="scheduled_llm_occurrence", target_id=row.id,
          details={"reason": reason, "coalesced_count": count, "count_truncated": truncated})


async def reserve_due(db, *, now=None, limit=100):
    """Réserve au plus la dernière échéance manquée par tâche, sous SKIP LOCKED."""
    now = now or utcnow()
    tasks = (await db.scalars(select(ScheduledLLMTask).where(
        ScheduledLLMTask.status == "active", ScheduledLLMTask.next_run_at <= now)
        .order_by(ScheduledLLMTask.next_run_at, ScheduledLLMTask.id)
        .limit(limit).with_for_update(skip_locked=True).execution_options(populate_existing=True))).all()
    pending = []
    for task in tasks:
        version = await current_version(db, task)
        first = task.next_run_at
        try:
            latest, missed, truncated = due_window(version, first, now)
            task.next_run_at = next_due(version, now)
        except ValueError:
            task.status, task.next_run_at = "paused", None
            audit(db, "scheduled_task.schedule.invalid", "failed", project_id=task.project_id,
                  target_type="scheduled_llm_task", target_id=task.id,
                  details={"task_version_id": version.id})
            continue
        task.updated_at = now
        if latest is None:
            continue
        if missed:
            await _skip(db, task, version, first, "backlog_coalesced", missed, truncated)
        project = await db.get(Project, task.project_id)
        reason = None
        if project is None or project.status == "archived":
            reason = "project_archived"
        elif not await _owner_allowed(db, task):
            reason = "permission_revoked"
        elif await _active_run(db, task.id):
            reason = "run_in_progress"
        else:
            try:
                profile = await usable_profile(db, version.llm_profile_version_id)
            except RuntimeDenied:
                reason = "profile_unavailable"
        if reason:
            await _skip(db, task, version, latest, reason)
            continue
        occurrence = ScheduledLLMOccurrence(project_id=task.project_id, task_id=task.id,
            task_version_id=version.id, scheduled_at=latest, origin="scheduled", status="reserved",
            coalesced_count=0, count_truncated=False)
        db.add(occurrence)
        await db.flush()
        pending.append((await _new_run(db, task, version, profile, occurrence, None)).id)
    return pending


async def dispatch_pending(factory, *, send=None, now=None, run_ids=None, limit=100):
    """Envoie seulement des IDs ; un rejeu est absorbé par le verrou du worker."""
    now = now or utcnow()
    if send is None:
        from app.worker import celery_app
        send = lambda run_id: celery_app.send_task("mimic.coffy.execute", args=[run_id])
    async with factory() as db:
        query = select(ScheduledLLMRun.id).where(ScheduledLLMRun.status.in_(("pending", "queued")))
        if run_ids is not None:
            query = query.where(ScheduledLLMRun.id.in_(run_ids))
        else:
            query = query.where((ScheduledLLMRun.last_dispatched_at.is_(None)) |
                (ScheduledLLMRun.last_dispatched_at <= now - timedelta(seconds=60)))
        ids = (await db.scalars(query.order_by(ScheduledLLMRun.created_at).limit(limit))).all()
    for run_id in ids:
        error = None
        try:
            send(run_id)
        except Exception:
            error = "dispatch_unavailable"
        async with factory() as db:
            run = await db.scalar(select(ScheduledLLMRun).where(
                ScheduledLLMRun.id == run_id).with_for_update().execution_options(populate_existing=True))
            if run and run.status in {"pending", "queued"}:
                run.dispatch_attempts += 1
                run.last_dispatched_at = now
                run.status = "pending" if error else "queued"
                run.error_code = error
                audit(db, "scheduled_task.run.dispatch", "failed" if error else "success",
                      actor_id=run.requested_by, project_id=run.project_id,
                      target_type="scheduled_llm_run", target_id=run.id,
                      details={"error_code": error, "attempt": run.dispatch_attempts})
                await db.commit()


async def _finish(db, run, *, error=None, result=None):
    if run.status not in {"running", "retrying"}:
        return False
    if result is not None:
        output = redact(result.text)
        if not isinstance(output, str):
            error = "invalid_response"
        elif not output.strip():
            error = "empty_response"
        else:
            try:
                if len(output.encode("utf-8")) > 200000:
                    error = "output_too_large"
            except UnicodeEncodeError:
                error = "invalid_response"
        if error is None:
            from app.coffy_models import ScheduledLLMResultVersion, ScheduledLLMTaskVersion
            from app.coffy_json import strict_json_value
            version = await db.get(ScheduledLLMTaskVersion, run.task_version_id)
            if version.strict_json:
                try:
                    strict_json_value(output)
                except (ValueError, TypeError):
                    error = "invalid_json"
            if error is None:
                run.raw_output = output
                run.result_current_version = 1
                first = ScheduledLLMResultVersion(run_id=run.id, version=1, content=output,
                    status="Brouillon IA", actor_id=None)
                db.add(first)
                await db.flush()
                audit(db, "scheduled_task.result.create", "success", project_id=run.project_id,
                      target_type="scheduled_llm_result_version", target_id=first.id,
                      details={"run_id": run.id, "version": 1, "status": first.status})
    run.status = ("cancelled" if error in {"task_inactive", "permission_revoked"} else
                  "timeout" if error == "timeout" else "failed" if error else "success")
    run.error_code = error
    run.completed_at = utcnow()
    occurrence = await db.get(ScheduledLLMOccurrence, run.occurrence_id)
    occurrence.status = "executed"
    await apply_usage(db, run)
    audit(db, "scheduled_task.run.complete", "failed" if error else "success",
          actor_id=run.requested_by, project_id=run.project_id,
          target_type="scheduled_llm_run", target_id=run.id,
          details={"status": run.status, "error_code": error, "task_version_id": run.task_version_id,
                   "profile_version_id": run.llm_profile_version_id})
    return True


async def execute_run(factory, run_id, *, invoke=invoke_profile):
    async with factory() as db:
        run = await db.scalar(select(ScheduledLLMRun).where(
            ScheduledLLMRun.id == run_id).with_for_update().execution_options(populate_existing=True))
        if run is None or run.status not in {"pending", "queued"}:
            return
        run.status, run.error_code, run.started_at = "running", None, utcnow()
        audit(db, "scheduled_task.run.start", "success", actor_id=run.requested_by,
              project_id=run.project_id, target_type="scheduled_llm_run", target_id=run.id,
              details={"task_version_id": run.task_version_id,
                       "profile_version_id": run.llm_profile_version_id})
        await db.commit()
    async with factory() as db:
        run = await db.get(ScheduledLLMRun, run_id)
        from app.coffy_models import ScheduledLLMTaskVersion
        version = await db.get(ScheduledLLMTaskVersion, run.task_version_id)
        profile = await db.get(LLMProfileVersion, run.llm_profile_version_id)
        if version is None or profile is None:
            error = "configuration_unavailable"
        else:
            try:
                await guard_run(db, run)
                error = None
            except RuntimeDenied as exc:
                error = exc.code
    result = None
    if error is None:
        try:
            result = await traced_invoke(factory, run, profile,
                [{"role": "system", "content": version.system_prompt},
                 {"role": "user", "content": version.user_prompt}],
                invoke=invoke, guard=guard_run)
        except LLMInvocationError as exc:
            error = exc.category
        except Exception:
            error = "execution_failed"
    async with factory() as db:
        run = await db.scalar(select(ScheduledLLMRun).where(
            ScheduledLLMRun.id == run_id).with_for_update().execution_options(populate_existing=True))
        if run is None or run.status not in {"running", "retrying"}:
            return
        if error is None:
            try:
                await guard_run(db, run)
            except RuntimeDenied as exc:
                error = exc.code
        await _finish(db, run, error=error, result=result if error is None else None)
        await db.commit()


async def expire_stale(db, *, now=None, seconds=150):
    """Un appel perdu devient inconnu pour les quotas ; aucune relance fournisseur automatique."""
    now = now or utcnow()
    identities = (await db.scalars(select(ScheduledLLMRun.id).where(
        ScheduledLLMRun.status.in_(("running", "retrying")),
        ScheduledLLMRun.started_at <= now - timedelta(seconds=seconds))
        .order_by(ScheduledLLMRun.started_at).limit(100))).all()
    expired = 0
    for identity in identities:
        project_id = await db.scalar(select(ScheduledLLMRun.project_id).where(ScheduledLLMRun.id == identity))
        if project_id is None:
            continue
        await lock_project(db, project_id)
        run = await db.scalar(select(ScheduledLLMRun).where(
            ScheduledLLMRun.id == identity,
            ScheduledLLMRun.status.in_(("running", "retrying")),
            ScheduledLLMRun.started_at <= now - timedelta(seconds=seconds))
            .with_for_update(skip_locked=True).execution_options(populate_existing=True))
        if run is None:
            continue
        calls = (await db.scalars(select(LLMCallAttempt).where(
            LLMCallAttempt.scheduled_llm_run_id == run.id,
            LLMCallAttempt.status == "running").with_for_update())).all()
        for call in calls:
            call.status, call.error_code, call.ended_at = "failed", "execution_unknown", now
            usage = await db.scalar(select(RuntimeUsage).where(RuntimeUsage.call_id == call.id).with_for_update())
            if usage and usage.status == "active":
                usage.status, usage.error_code, usage.ended_at = "unknown", "execution_unknown", now
        await _finish(db, run, error="execution_unknown")
        expired += 1
    return expired
