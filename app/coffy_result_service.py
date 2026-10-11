"""Résultats CoFFY : versions humaines, décisions, avis et commentaires distincts."""

from sqlalchemy import select

from app.coffy_models import (ScheduledLLMFeedback, ScheduledLLMResultComment,
                              ScheduledLLMResultVersion, ScheduledLLMRun, ScheduledLLMTaskVersion)
from app.identity_models import Project, ProjectMember, utcnow
from app.identity_service import audit
from app.llm_trace import redact
from app.coffy_json import strict_json_value

DECIDER_ROLES = {"business": "product_owner", "technical": "architect", "pilotage": "project_manager"}


def validate_content(content, *, strict_json=False):
    try:
        valid = isinstance(content, str) and bool(content.strip()) and len(content.encode("utf-8")) <= 200000
    except UnicodeEncodeError:
        valid = False
    if not valid:
        raise ValueError("Résultat vide ou supérieur à 200 000 octets.")
    if redact(content) != content:
        raise ValueError("Le résultat contient une donnée sensible masquée.")
    if strict_json:
        strict_json_value(content)
    return content


def result_view(row):
    return {key: getattr(row, key) for key in ("id", "run_id", "version", "parent_version_id",
                                                 "content", "status", "actor_id", "created_at")}


def comment_view(row):
    return {key: getattr(row, key) for key in ("id", "result_version_id", "author_id", "body",
                                                 "status", "created_at", "resolved_by", "resolved_at")}


def feedback_view(row):
    return {key: getattr(row, key) for key in ("id", "result_version_id", "author_id", "score", "note", "created_at")}


async def lock_run(db, project_id, task_id, run_id):
    run = await db.scalar(select(ScheduledLLMRun).where(
        ScheduledLLMRun.id == run_id, ScheduledLLMRun.task_id == task_id,
        ScheduledLLMRun.project_id == project_id).with_for_update().execution_options(populate_existing=True))
    if run is None:
        raise LookupError("Exécution introuvable.")
    project = await db.get(Project, project_id)
    if project is None or project.status == "archived":
        raise ValueError("Projet archivé.")
    return run


async def current_result(db, run):
    if run.status != "success" or run.result_current_version < 1:
        raise ValueError("Aucun résultat révisable pour cette exécution.")
    row = await db.scalar(select(ScheduledLLMResultVersion).where(
        ScheduledLLMResultVersion.run_id == run.id,
        ScheduledLLMResultVersion.version == run.result_current_version))
    if row is None:
        raise LookupError("Version de résultat introuvable.")
    return row


async def version_for_run(db, run, version_id):
    row = await db.scalar(select(ScheduledLLMResultVersion).where(
        ScheduledLLMResultVersion.id == version_id, ScheduledLLMResultVersion.run_id == run.id))
    if row is None:
        raise LookupError("Version de résultat introuvable.")
    return row


async def revise(db, run, *, expected_version, content, actor):
    current = await current_result(db, run)
    if current.version != expected_version:
        raise ValueError("Version périmée : rechargez le résultat.")
    task_version = await db.get(ScheduledLLMTaskVersion, run.task_version_id)
    content = validate_content(content, strict_json=task_version.strict_json)
    run.result_current_version += 1
    row = ScheduledLLMResultVersion(run_id=run.id, version=run.result_current_version,
        parent_version_id=current.id, content=content, status="À revoir", actor_id=actor.id)
    db.add(row)
    await db.flush()
    audit(db, "scheduled_task.result.revise", "success", actor_id=actor.id,
          project_id=run.project_id, target_type="scheduled_llm_result_version", target_id=row.id,
          details={"run_id": run.id, "parent_version_id": current.id, "version": row.version,
                   "status": row.status})
    return row


async def decide(db, run, *, expected_version, action, reason, actor):
    current = await current_result(db, run)
    if current.version != expected_version:
        raise ValueError("Version périmée : rechargez le résultat.")
    transitions = {"submit": ("Brouillon IA", "À revoir"),
                   "validate": ("À revoir", "Validé"), "reject": ("À revoir", "Rejeté")}
    if action not in transitions or current.status != transitions[action][0]:
        raise ValueError("Transition documentaire interdite.")
    if action in {"validate", "reject"}:
        task_version = await db.get(ScheduledLLMTaskVersion, run.task_version_id)
        role = await db.scalar(select(ProjectMember.role_code).where(
            ProjectMember.project_id == run.project_id, ProjectMember.user_id == actor.id,
            ProjectMember.is_active.is_(True)))
        if not actor.is_active or role != DECIDER_ROLES[task_version.result_domain]:
            raise PermissionError("Le rôle de validation ne correspond pas au domaine du résultat.")
    reason = reason.strip()
    if action == "reject" and not 1 <= len(reason) <= 2000:
        raise ValueError("Le motif du rejet doit contenir de 1 à 2 000 caractères.")
    run.result_current_version += 1
    row = ScheduledLLMResultVersion(run_id=run.id, version=run.result_current_version,
        parent_version_id=current.id, content=current.content,
        status=transitions[action][1], actor_id=actor.id)
    db.add(row)
    await db.flush()
    if action == "reject":
        await add_comment(db, run, row, body=reason, actor=actor, rejection_reason=True)
    audit(db, f"scheduled_task.result.{action}", "success", actor_id=actor.id,
          project_id=run.project_id, target_type="scheduled_llm_result_version", target_id=row.id,
          details={"run_id": run.id, "parent_version_id": current.id,
                   "version": row.version, "status": row.status, "reason_comment": action == "reject"})
    return row


async def add_comment(db, run, version, *, body, actor, rejection_reason=False):
    body = body.strip()
    if not 1 <= len(body) <= 5000:
        raise ValueError("Le commentaire doit contenir de 1 à 5 000 caractères.")
    row = ScheduledLLMResultComment(result_version_id=version.id, author_id=actor.id,
                                    body=body, status="open")
    db.add(row)
    await db.flush()
    audit(db, "scheduled_task.result.comment", "success", actor_id=actor.id,
          project_id=run.project_id, target_type="scheduled_llm_result_comment", target_id=row.id,
          details={"run_id": run.id, "result_version_id": version.id,
                   "rejection_reason": rejection_reason})
    return row


async def resolve_comment(db, run, comment, *, actor):
    if comment.status != "open":
        raise ValueError("Ce commentaire est déjà résolu.")
    comment.status, comment.resolved_by, comment.resolved_at = "resolved", actor.id, utcnow()
    audit(db, "scheduled_task.result.comment.resolve", "success", actor_id=actor.id,
          project_id=run.project_id, target_type="scheduled_llm_result_comment", target_id=comment.id,
          details={"run_id": run.id, "result_version_id": comment.result_version_id})


async def add_feedback(db, run, version, *, score, note, actor):
    note = note.strip()
    if type(score) is not int or not 1 <= score <= 5 or len(note) > 2000:
        raise ValueError("Avis invalide : note de 1 à 5 et texte de 2 000 caractères maximum.")
    existing = await db.scalar(select(ScheduledLLMFeedback.id).where(
        ScheduledLLMFeedback.result_version_id == version.id,
        ScheduledLLMFeedback.author_id == actor.id))
    if existing:
        raise ValueError("Un avis existe déjà pour cette version.")
    row = ScheduledLLMFeedback(result_version_id=version.id, author_id=actor.id,
                               score=score, note=note)
    db.add(row)
    await db.flush()
    audit(db, "scheduled_task.result.feedback", "success", actor_id=actor.id,
          project_id=run.project_id, target_type="scheduled_llm_feedback", target_id=row.id,
          details={"run_id": run.id, "result_version_id": version.id, "score": score})
    return row
