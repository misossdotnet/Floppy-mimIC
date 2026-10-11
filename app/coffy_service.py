"""Configuration versionnée et transitions humaines des tâches CoFFY."""

from sqlalchemy import select

from app.coffy_models import ScheduledLLMSubject, ScheduledLLMSubjectRevision, ScheduledLLMTask, ScheduledLLMTaskVersion, ScheduledLLMVersionSubject
from app.identity_models import utcnow
from app.identity_service import audit
from app.llm_models import LLMProfileVersion
from app.schedule_core import next_due, validate_rule


def task_view(task, version=None):
    result = {key: getattr(task, key) for key in ("id", "project_id", "status", "current_version", "revision", "next_run_at", "created_by", "created_at", "updated_at")}
    if version:
        result["configuration"] = version_view(version)
    return result


def version_view(row):
    return {key: getattr(row, key) for key in ("id", "task_id", "version", "title", "short_description", "long_description", "system_prompt", "user_prompt", "result_domain", "llm_profile_version_id", "parameters", "strict_json", "schedule_mode", "schedule_expression", "timezone_name", "subject_ids", "created_by", "created_at")}


def subject_view(subject):
    return {key: getattr(subject, key) for key in ("id", "project_id", "title", "description", "status", "revision", "created_by", "created_at", "updated_at")}


async def current_version(db, task):
    return await db.scalar(select(ScheduledLLMTaskVersion).where(
        ScheduledLLMTaskVersion.task_id == task.id,
        ScheduledLLMTaskVersion.version == task.current_version))


async def lock_task(db, project_id, task_id):
    return await db.scalar(select(ScheduledLLMTask).where(
        ScheduledLLMTask.id == task_id, ScheduledLLMTask.project_id == project_id,
        ScheduledLLMTask.status != "deleted").with_for_update().execution_options(populate_existing=True))


async def validate_configuration(db, project_id, values):
    profile = await db.get(LLMProfileVersion, values["llm_profile_version_id"])
    if profile is None:
        raise ValueError("Version de profil LLM introuvable.")
    params = values["parameters"]
    if set(params) - {"max_output_tokens", "timeout_seconds", "temperature", "max_retries"}:
        raise ValueError("Paramètre de tâche non autorisé.")
    for key in ("max_output_tokens", "timeout_seconds"):
        if key in params and (type(params[key]) is not int or not 1 <= params[key] <= getattr(profile, key)):
            raise ValueError(f"Limite {key} invalide pour ce profil.")
    if "temperature" in params and (type(params["temperature"]) not in {float, int} or not 0 <= params["temperature"] <= 2):
        raise ValueError("Température invalide.")
    if "max_retries" in params and (type(params["max_retries"]) is not int or not 0 <= params["max_retries"] <= profile.max_retries):
        raise ValueError("Nombre de relances invalide.")
    validate_rule(values["schedule_mode"], values["schedule_expression"], values["timezone_name"])
    ids = values["subject_ids"]
    if len(ids) != len(set(ids)):
        raise ValueError("Sujet sélectionné plusieurs fois.")
    if ids:
        rows = (await db.scalars(select(ScheduledLLMSubject).where(
            ScheduledLLMSubject.id.in_(ids), ScheduledLLMSubject.project_id == project_id,
            ScheduledLLMSubject.status == "active").order_by(ScheduledLLMSubject.id).with_for_update())).all()
        if len(rows) != len(ids):
            raise LookupError("Sujet introuvable dans ce projet ou inactif.")


async def add_version(db, task, values, actor_id, action):
    row = ScheduledLLMTaskVersion(task_id=task.id, version=task.current_version,
                                  created_by=actor_id, **values)
    db.add(row)
    await db.flush()
    for subject_id in values["subject_ids"]:
        db.add(ScheduledLLMVersionSubject(version_id=row.id, subject_id=subject_id))
    audit(db, f"scheduled_task.{action}", "success", actor_id=actor_id,
          project_id=task.project_id, target_type="scheduled_llm_task", target_id=task.id,
          details={"version": row.version, "revision": task.revision, "status": task.status,
                   "subject_count": len(values["subject_ids"])})
    await db.flush()
    return row


async def create_task(db, project_id, values, actor_id):
    await validate_configuration(db, project_id, values)
    task = ScheduledLLMTask(project_id=project_id, created_by=actor_id, status="draft",
                            current_version=1, revision=1)
    db.add(task)
    await db.flush()
    row = await add_version(db, task, values, actor_id, "create")
    return task, row


async def revise_task(db, project_id, task_id, expected_revision, values, actor_id):
    task = await lock_task(db, project_id, task_id)
    if task is None:
        raise LookupError("Tâche introuvable.")
    if task.revision != expected_revision:
        raise ValueError("Révision périmée : rechargez la tâche.")
    await validate_configuration(db, project_id, values)
    if task.status == "active":
        from app.runtime_service import usable_profile
        await usable_profile(db, values["llm_profile_version_id"])
    if task.status == "archived":
        task.status = "draft"
    task.current_version += 1
    task.revision += 1
    task.updated_at = utcnow()
    row = await add_version(db, task, values, actor_id, "revise")
    task.next_run_at = next_due(row, utcnow()) if task.status == "active" else None
    return task, row


async def transition_task(db, project_id, task_id, action, expected_revision, actor_id):
    task = await lock_task(db, project_id, task_id)
    if task is None:
        raise LookupError("Tâche introuvable.")
    if task.revision != expected_revision:
        raise ValueError("Révision périmée : rechargez la tâche.")
    transitions = {"activate": ({"draft"}, "active"),
                   "pause": ({"active"}, "paused"), "resume": ({"paused"}, "active"),
                   "archive": ({"draft", "active", "paused"}, "archived"),
                   "delete": ({"draft", "archived"}, "deleted")}
    allowed, target = transitions[action]
    if task.status not in allowed:
        raise ValueError("Transition de statut interdite.")
    now = utcnow()
    if action in {"activate", "resume"}:
        from app.runtime_service import usable_profile
        version = await current_version(db, task)
        await usable_profile(db, version.llm_profile_version_id)
        future = next_due(version, now)
        if action == "activate" and version.schedule_mode == "once" and future is None:
            raise ValueError("L'instant unique doit être futur à l'activation.")
    else:
        future = None
    task.status, task.revision, task.next_run_at, task.updated_at = target, task.revision + 1, future, now
    audit(db, f"scheduled_task.{action}", "success", actor_id=actor_id, project_id=project_id,
          target_type="scheduled_llm_task", target_id=task.id,
          details={"version": task.current_version, "revision": task.revision, "status": task.status})
    await db.flush()
    return task


async def create_subject(db, project_id, values, actor_id):
    row = ScheduledLLMSubject(project_id=project_id, created_by=actor_id, **values)
    db.add(row)
    await db.flush()
    db.add(ScheduledLLMSubjectRevision(subject_id=row.id, revision=1, title=row.title,
                                       description=row.description, status=row.status, actor_id=actor_id))
    audit(db, "scheduled_task.subject.create", "success", actor_id=actor_id, project_id=project_id,
          target_type="scheduled_llm_subject", target_id=row.id, details={"revision": 1})
    return row


async def revise_subject(db, project_id, subject_id, expected_revision, values, actor_id):
    row = await db.scalar(select(ScheduledLLMSubject).where(
        ScheduledLLMSubject.id == subject_id, ScheduledLLMSubject.project_id == project_id,
        ScheduledLLMSubject.status != "deleted").with_for_update().execution_options(populate_existing=True))
    if row is None:
        raise LookupError("Sujet introuvable.")
    if row.revision != expected_revision:
        raise ValueError("Révision périmée : rechargez le sujet.")
    row.title, row.description, row.status = values["title"], values["description"], values["status"]
    row.revision += 1
    row.updated_at = utcnow()
    db.add(ScheduledLLMSubjectRevision(subject_id=row.id, revision=row.revision, title=row.title,
                                       description=row.description, status=row.status, actor_id=actor_id))
    audit(db, "scheduled_task.subject.revise", "success", actor_id=actor_id, project_id=project_id,
          target_type="scheduled_llm_subject", target_id=row.id,
          details={"revision": row.revision, "status": row.status})
    await db.flush()
    return row
