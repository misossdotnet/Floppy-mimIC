"""Écrans projet CoFFY, adossés aux mêmes services et permissions que l'API."""

import json
from uuid import UUID, uuid4

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import RedirectResponse
from pydantic import ValidationError
from sqlalchemy import func, select

from app.agent_api import db_session
from app.coffy_api import Configuration, TaskEdit, SubjectFields, SubjectEdit, authorize, authorize_result
from app.coffy_models import (ScheduledLLMOccurrence, ScheduledLLMRun, ScheduledLLMSubject,
                              ScheduledLLMSubjectRevision, ScheduledLLMTask, ScheduledLLMTaskVersion,
                              ScheduledLLMResultVersion, ScheduledLLMResultComment, ScheduledLLMFeedback)
from app.coffy_execution import dispatch_pending, manual_run
from app.coffy_result_service import (DECIDER_ROLES, add_comment, add_feedback, decide,
    lock_run, resolve_comment, revise, version_for_run)
from app.coffy_service import (create_subject, create_task, current_version, revise_subject,
                               revise_task, transition_task)
from app.identity_service import project_permission
from app.llm_models import LLMCallAttempt, LLMProfileVersion
from app.schedule_core import next_due
from app.identity_models import ProjectMember, User, utcnow

router = APIRouter(prefix="/projects/{project_id}/scheduled-tasks", tags=["scheduled-task-pages"])


async def web_actor(request: Request, db=Depends(db_session)):
    from app.main import principal
    return await principal(request, db)


async def render(request, db, actor, project, *, task=None, form=None, error=None, status=200):
    from app.main import page
    tasks = (await db.scalars(select(ScheduledLLMTask).where(
        ScheduledLLMTask.project_id == project.id, ScheduledLLMTask.status != "deleted")
        .order_by(ScheduledLLMTask.updated_at.desc()))).all()
    current = [(row, await current_version(db, row)) for row in tasks]
    subjects = (await db.scalars(select(ScheduledLLMSubject).where(
        ScheduledLLMSubject.project_id == project.id, ScheduledLLMSubject.status != "deleted")
        .order_by(ScheduledLLMSubject.title))).all()
    all_subjects = (await db.scalars(select(ScheduledLLMSubject).where(
        ScheduledLLMSubject.project_id == project.id))).all()
    subject_names = {row.id: row.title for row in all_subjects}
    subject_history = {}
    if subjects:
        revisions = (await db.scalars(select(ScheduledLLMSubjectRevision).where(
            ScheduledLLMSubjectRevision.subject_id.in_([row.id for row in subjects]))
            .order_by(ScheduledLLMSubjectRevision.revision.desc()))).all()
        for revision in revisions:
            subject_history.setdefault(revision.subject_id, []).append(revision)
    profiles = (await db.scalars(select(LLMProfileVersion).where(
        LLMProfileVersion.is_current.is_(True)).order_by(LLMProfileVersion.name))).all()
    history = (await db.scalars(select(ScheduledLLMTaskVersion).where(
        ScheduledLLMTaskVersion.task_id == task.id).order_by(ScheduledLLMTaskVersion.version.desc()))).all() if task else []
    runs = (await db.scalars(select(ScheduledLLMRun).where(
        ScheduledLLMRun.task_id == task.id, ScheduledLLMRun.project_id == project.id)
        .order_by(ScheduledLLMRun.created_at.desc()).limit(30))).all() if task else []
    attempt_counts = dict((await db.execute(select(LLMCallAttempt.scheduled_llm_run_id, func.count())
        .where(LLMCallAttempt.scheduled_llm_run_id.in_([run.id for run in runs]))
        .group_by(LLMCallAttempt.scheduled_llm_run_id))).all()) if runs else {}
    occurrences = (await db.scalars(select(ScheduledLLMOccurrence).where(
        ScheduledLLMOccurrence.task_id == task.id, ScheduledLLMOccurrence.project_id == project.id)
        .order_by(ScheduledLLMOccurrence.scheduled_at.desc()).limit(30))).all() if task else []
    preview = []
    if history and history[0].schedule_mode != "manual":
        cursor = utcnow()
        try:
            for _ in range(5):
                cursor = next_due(history[0], cursor)
                if cursor is None:
                    break
                preview.append(cursor)
        except ValueError:
            # Un ancien brouillon invalide reste consultable et corrigeable.
            preview = []
    if form is None:
        version = await current_version(db, task) if task else None
        form = {key: getattr(version, key) for key in ("title", "short_description", "long_description", "system_prompt", "user_prompt", "result_domain", "llm_profile_version_id", "parameters", "strict_json", "schedule_mode", "schedule_expression", "timezone_name", "subject_ids")} if version else {
            "title": "", "short_description": "", "long_description": "", "system_prompt": "", "user_prompt": "",
            "result_domain": "pilotage", "llm_profile_version_id": "", "parameters": {}, "strict_json": False,
            "schedule_mode": "manual", "schedule_expression": "", "timezone_name": "", "subject_ids": []}
    manage = project.status != "archived" and await project_permission(db, actor[0], project.id, "scheduled_task.manage")
    can_run = project.status != "archived" and await project_permission(db, actor[0], project.id, "scheduled_task.run")
    response = page(request, "coffy_tasks", actor[0], actor[2], project=project, tasks=current,
                    task=task, history=history, runs=runs, attempt_counts=attempt_counts,
                    occurrences=occurrences, preview=preview,
                    manual_request_key=str(uuid4()), subjects=subjects, subject_history=subject_history,
                    subject_names=subject_names, profiles=profiles, form=form,
                    parameters_text=json.dumps(form.get("parameters") or {}, ensure_ascii=False),
                    error_message=error, permissions={"platform_admin": actor[0].is_platform_admin,
                    "coffy_manage": manage, "coffy_run": can_run}, active_page="coffy")
    response.status_code = status
    return response


@router.get("")
async def list_page(request: Request, project_id: str, db=Depends(db_session), actor=Depends(web_actor)):
    project = await authorize(db, actor, project_id)
    return await render(request, db, actor, project)


@router.get("/{task_id}")
async def detail_page(request: Request, project_id: str, task_id: str, db=Depends(db_session), actor=Depends(web_actor)):
    project = await authorize(db, actor, project_id)
    task = await db.scalar(select(ScheduledLLMTask).where(ScheduledLLMTask.id == task_id,
        ScheduledLLMTask.project_id == project_id, ScheduledLLMTask.status != "deleted"))
    if task is None:
        raise HTTPException(status_code=404)
    return await render(request, db, actor, project, task=task)


async def render_run(request, db, actor, project, task_id, run_id, *, error=None, status=200):
    from app.main import page
    task = await db.scalar(select(ScheduledLLMTask).where(
        ScheduledLLMTask.id == task_id, ScheduledLLMTask.project_id == project.id))
    run = await db.scalar(select(ScheduledLLMRun).where(
        ScheduledLLMRun.id == run_id, ScheduledLLMRun.task_id == task_id,
        ScheduledLLMRun.project_id == project.id))
    if task is None or run is None:
        raise HTTPException(status_code=404)
    task_version = await db.get(ScheduledLLMTaskVersion, run.task_version_id)
    attempts = (await db.scalars(select(LLMCallAttempt).where(
        LLMCallAttempt.scheduled_llm_run_id == run.id)
        .order_by(LLMCallAttempt.attempt_number))).all()
    versions = (await db.scalars(select(ScheduledLLMResultVersion).where(
        ScheduledLLMResultVersion.run_id == run.id)
        .order_by(ScheduledLLMResultVersion.version.desc()))).all()
    ids = [row.id for row in versions]
    comments = (await db.scalars(select(ScheduledLLMResultComment).where(
        ScheduledLLMResultComment.result_version_id.in_(ids))
        .order_by(ScheduledLLMResultComment.created_at))).all() if ids else []
    feedback = (await db.scalars(select(ScheduledLLMFeedback).where(
        ScheduledLLMFeedback.result_version_id.in_(ids))
        .order_by(ScheduledLLMFeedback.created_at))).all() if ids else []
    author_ids = {row.author_id for row in comments + feedback}
    people = {user.id: user.display_name for user in (await db.scalars(
        select(User).where(User.id.in_(author_ids)))).all()} if author_ids else {}
    role = await db.scalar(select(ProjectMember.role_code).where(
        ProjectMember.project_id == project.id, ProjectMember.user_id == actor[0].id,
        ProjectMember.is_active.is_(True)))
    active_member = project.status != "archived" and role in {"product_owner", "project_manager", "architect"}
    can_edit = active_member and await project_permission(db, actor[0], project.id, "scheduled_task.result.edit")
    can_feedback = active_member and await project_permission(db, actor[0], project.id, "scheduled_task.feedback")
    can_decide = active_member and role == DECIDER_ROLES[task_version.result_domain] and await project_permission(
        db, actor[0], project.id, "scheduled_task.review")
    response = page(request, "coffy_run", actor[0], actor[2], project=project, task=task,
        task_version=task_version, run=run, attempts=attempts, versions=versions,
        current_result=versions[0] if versions else None, comments=comments, feedback=feedback,
        people=people,
        error_message=error, active_page="coffy",
        permissions={"platform_admin": actor[0].is_platform_admin,
                     "result_edit": can_edit, "result_feedback": can_feedback,
                     "result_decide": can_decide})
    response.status_code = status
    response.headers["Cache-Control"] = "no-store"
    return response


@router.get("/{task_id}/runs/{run_id}")
async def run_detail_page(request: Request, project_id: str, task_id: str, run_id: str,
                          db=Depends(db_session), actor=Depends(web_actor)):
    project = await authorize(db, actor, project_id)
    return await render_run(request, db, actor, project, task_id, run_id)


@router.post("/{task_id}/runs/{run_id}/result-actions/{action}")
async def result_action_page(request: Request, project_id: str, task_id: str, run_id: str,
                             action: str, db=Depends(db_session), actor=Depends(web_actor)):
    if action not in {"edit", "submit", "validate", "reject", "comment", "resolve", "feedback"}:
        raise HTTPException(status_code=404)
    raw = await request.form()
    permission = ("scheduled_task.review" if action in {"validate", "reject"} else
                  "scheduled_task.feedback" if action in {"comment", "resolve", "feedback"} else
                  "scheduled_task.result.edit")
    project = await authorize_result(db, actor, project_id, permission, raw.get("csrf_token"))
    try:
        run = await lock_run(db, project_id, task_id, run_id)
        if action == "edit":
            await revise(db, run, expected_version=int(raw.get("expected_version", "0")),
                         content=raw.get("content", ""), actor=actor[0])
        elif action in {"submit", "validate", "reject"}:
            await decide(db, run, expected_version=int(raw.get("expected_version", "0")),
                         action=action, reason=raw.get("reason", ""), actor=actor[0])
        elif action == "comment":
            version = await version_for_run(db, run, raw.get("result_version_id", ""))
            await add_comment(db, run, version, body=raw.get("body", ""), actor=actor[0])
        elif action == "feedback":
            version = await version_for_run(db, run, raw.get("result_version_id", ""))
            await add_feedback(db, run, version, score=int(raw.get("score", "0")),
                               note=raw.get("note", ""), actor=actor[0])
        else:
            row = await db.scalar(select(ScheduledLLMResultComment).join(
                ScheduledLLMResultVersion, ScheduledLLMResultVersion.id == ScheduledLLMResultComment.result_version_id)
                .where(ScheduledLLMResultComment.id == raw.get("comment_id", ""),
                       ScheduledLLMResultVersion.run_id == run.id).with_for_update())
            if row is None:
                raise LookupError("Commentaire introuvable.")
            await resolve_comment(db, run, row, actor=actor[0])
    except (ValueError, LookupError, PermissionError) as exc:
        return await render_run(request, db, actor, project, task_id, run_id,
                                error=str(exc), status=403 if isinstance(exc, PermissionError) else 409)
    await db.commit()
    return RedirectResponse(f"/projects/{project_id}/scheduled-tasks/{task_id}/runs/{run_id}", status_code=303)


def form_values(raw):
    return {"title": raw.get("title", ""), "short_description": raw.get("short_description", ""),
            "long_description": raw.get("long_description", ""), "system_prompt": raw.get("system_prompt", ""),
            "user_prompt": raw.get("user_prompt", ""), "result_domain": raw.get("result_domain", ""),
            "llm_profile_version_id": raw.get("llm_profile_version_id", ""),
            "parameters": json.loads(raw.get("parameters", "{}") or "{}"),
            "strict_json": raw.get("strict_json") == "on", "schedule_mode": raw.get("schedule_mode", "manual"),
            "schedule_expression": raw.get("schedule_expression") or None,
            "timezone_name": raw.get("timezone_name") or None,
            "subject_ids": raw.getlist("subject_ids")}


async def save_page(request, db, actor, project_id, task_id=None):
    raw = await request.form()
    project = await authorize(db, actor, project_id, write=True, csrf=raw.get("csrf_token"))
    task = None
    if task_id:
        task = await db.scalar(select(ScheduledLLMTask).where(ScheduledLLMTask.id == task_id,
            ScheduledLLMTask.project_id == project_id, ScheduledLLMTask.status != "deleted"))
        if task is None:
            raise HTTPException(status_code=404)
    values = None
    try:
        values = form_values(raw)
        if task_id:
            payload = TaskEdit(**values, expected_revision=int(raw.get("expected_revision", "0")))
            task, _ = await revise_task(db, project_id, task_id, payload.expected_revision, payload.values(), actor[0].id)
        else:
            payload = Configuration(**values)
            task, _ = await create_task(db, project_id, payload.values(), actor[0].id)
    except (ValueError, LookupError, ValidationError) as exc:
        if values is None:
            values = {key: raw.get(key, "") for key in ("title", "short_description", "long_description", "system_prompt", "user_prompt", "result_domain", "llm_profile_version_id", "schedule_mode", "schedule_expression", "timezone_name")}
            values.update(parameters={}, subject_ids=raw.getlist("subject_ids"), strict_json=raw.get("strict_json") == "on")
        error = "Vérifiez les champs de la tâche et le JSON des limites." if isinstance(exc, (ValidationError, json.JSONDecodeError)) else str(exc)
        return await render(request, db, actor, project, task=task, form=values, error=error, status=409)
    await db.commit()
    return RedirectResponse(f"/projects/{project_id}/scheduled-tasks/{task.id}", status_code=303)


@router.post("")
async def new_page(request: Request, project_id: str, db=Depends(db_session), actor=Depends(web_actor)):
    return await save_page(request, db, actor, project_id)


@router.post("/{task_id}/edit")
async def edit_page(request: Request, project_id: str, task_id: str, db=Depends(db_session), actor=Depends(web_actor)):
    return await save_page(request, db, actor, project_id, task_id)


@router.post("/{task_id}/actions/{action}")
async def action_page(request: Request, project_id: str, task_id: str, action: str,
                      db=Depends(db_session), actor=Depends(web_actor)):
    raw = await request.form()
    project = await authorize(db, actor, project_id, write=True, csrf=raw.get("csrf_token"))
    if action not in {"activate", "pause", "resume", "archive", "delete"}:
        raise HTTPException(status_code=404)
    if action == "delete" and raw.get("confirm") != "on":
        return await render(request, db, actor, project, error="Confirmez le retrait logique de la tâche.", status=409)
    try:
        await transition_task(db, project_id, task_id, action, int(raw.get("expected_revision", "0")), actor[0].id)
    except LookupError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ValueError as exc:
        task = await db.scalar(select(ScheduledLLMTask).where(ScheduledLLMTask.id == task_id,
            ScheduledLLMTask.project_id == project_id))
        return await render(request, db, actor, project, task=task, error=str(exc), status=409)
    await db.commit()
    return RedirectResponse(f"/projects/{project_id}/scheduled-tasks/{task_id}", status_code=303)


@router.post("/{task_id}/run-now")
async def run_now_page(request: Request, project_id: str, task_id: str,
                       db=Depends(db_session), actor=Depends(web_actor)):
    raw = await request.form()
    project = await authorize(db, actor, project_id, run_write=True, csrf=raw.get("csrf_token"))
    try:
        row, created = await manual_run(db, project_id, task_id, str(UUID(raw.get("request_key", ""))),
                                        int(raw.get("expected_revision", "0")), actor[0].id)
    except LookupError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ValueError as exc:
        task = await db.scalar(select(ScheduledLLMTask).where(
            ScheduledLLMTask.id == task_id, ScheduledLLMTask.project_id == project_id))
        return await render(request, db, actor, project, task=task, error=str(exc), status=409)
    await db.commit()
    if created:
        await dispatch_pending(request.app.state.sessions, run_ids=[row.id])
    return RedirectResponse(f"/projects/{project_id}/scheduled-tasks/{task_id}", status_code=303)


@router.post("/subject-catalog")
async def new_subject_page(request: Request, project_id: str, db=Depends(db_session), actor=Depends(web_actor)):
    raw = await request.form()
    project = await authorize(db, actor, project_id, subject_write=True, csrf=raw.get("csrf_token"))
    try:
        values = SubjectFields(title=raw.get("title", ""), description=raw.get("description", ""))
    except ValidationError:
        return await render(request, db, actor, project, error="Titre ou description du sujet invalide.", status=409)
    await create_subject(db, project_id, values.model_dump(), actor[0].id)
    await db.commit()
    return RedirectResponse(f"/projects/{project_id}/scheduled-tasks", status_code=303)


@router.post("/subject-catalog/{subject_id}")
async def edit_subject_page(request: Request, project_id: str, subject_id: str,
                            db=Depends(db_session), actor=Depends(web_actor)):
    raw = await request.form()
    project = await authorize(db, actor, project_id, subject_write=True, csrf=raw.get("csrf_token"))
    try:
        values = SubjectEdit(title=raw.get("title", ""), description=raw.get("description", ""),
                             status=raw.get("status", ""), expected_revision=int(raw.get("expected_revision", "0")))
        await revise_subject(db, project_id, subject_id, values.expected_revision,
                             values.model_dump(exclude={"expected_revision"}), actor[0].id)
    except LookupError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except (ValidationError, ValueError) as exc:
        return await render(request, db, actor, project, error="Sujet invalide ou révision périmée." if isinstance(exc, ValidationError) else str(exc), status=409)
    await db.commit()
    return RedirectResponse(f"/projects/{project_id}/scheduled-tasks", status_code=303)
