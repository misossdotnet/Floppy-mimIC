"""API projet CoFFY : configuration, échéances et exécutions."""

from typing import Literal
from uuid import UUID

from fastapi import APIRouter, Depends, Header, HTTPException, Request, Response
from pydantic import BaseModel, ConfigDict, Field, field_validator
from sqlalchemy import func, select

from app.agent_api import check_csrf, db_session, human, human_project
from app.coffy_models import (ScheduledLLMOccurrence, ScheduledLLMRun, ScheduledLLMSubject,
                              ScheduledLLMSubjectRevision, ScheduledLLMTask, ScheduledLLMTaskVersion,
                              ScheduledLLMResultVersion, ScheduledLLMResultComment, ScheduledLLMFeedback)
from app.coffy_execution import dispatch_pending, manual_run, occurrence_view, run_view
from app.coffy_result_service import (add_comment, add_feedback, comment_view, current_result,
    decide, feedback_view, lock_run, resolve_comment, result_view, revise, version_for_run)
from app.identity_models import ProjectMember
from app.llm_models import LLMCallAttempt
from app.coffy_service import (create_subject, create_task, current_version, revise_subject,
                               revise_task, subject_view, task_view, transition_task, version_view)

router = APIRouter(prefix="/api/projects/{project_id}/scheduled-tasks", tags=["scheduled-tasks"])
subject_router = APIRouter(prefix="/api/projects/{project_id}/scheduled-subjects", tags=["scheduled-subjects"])


class Configuration(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    title: str = Field(min_length=1, max_length=160)
    short_description: str = Field(default="", max_length=500)
    long_description: str = Field(default="", max_length=20000)
    system_prompt: str = Field(min_length=1, max_length=20000)
    user_prompt: str = Field(min_length=1, max_length=20000)
    result_domain: Literal["business", "technical", "pilotage"]
    llm_profile_version_id: UUID
    parameters: dict = Field(default_factory=dict)
    strict_json: bool = False
    schedule_mode: Literal["manual", "once", "interval", "cron"] = "manual"
    schedule_expression: str | None = Field(default=None, max_length=160)
    timezone_name: str | None = Field(default=None, max_length=80)
    subject_ids: list[UUID] = Field(default_factory=list, max_length=20)

    @field_validator("system_prompt", "user_prompt")
    @classmethod
    def non_empty_prompt(cls, value):
        if not value.strip():
            raise ValueError("Prompt vide.")
        return value

    def values(self):
        return {**self.model_dump(), "llm_profile_version_id": str(self.llm_profile_version_id),
                "subject_ids": [str(item) for item in self.subject_ids]}


class TaskEdit(Configuration):
    expected_revision: int = Field(ge=1, strict=True)

    def values(self):
        result = super().values()
        result.pop("expected_revision")
        return result


class ExpectedRevision(BaseModel):
    model_config = ConfigDict(extra="forbid")
    expected_revision: int = Field(ge=1, strict=True)
    confirm: bool = False


class ManualRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    expected_revision: int = Field(ge=1, strict=True)
    request_key: UUID


class ResultEdit(BaseModel):
    model_config = ConfigDict(extra="forbid")
    expected_version: int = Field(ge=1, strict=True)
    content: str = Field(min_length=1, max_length=200000)


class ResultDecision(BaseModel):
    model_config = ConfigDict(extra="forbid")
    expected_version: int = Field(ge=1, strict=True)
    reason: str = Field(default="", max_length=2000)


class ResultCommentInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    result_version_id: UUID
    body: str = Field(min_length=1, max_length=5000)


class ResultFeedbackInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    result_version_id: UUID
    score: int = Field(ge=1, le=5, strict=True)
    note: str = Field(default="", max_length=2000)


class SubjectFields(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    title: str = Field(min_length=1, max_length=120)
    description: str = Field(default="", max_length=1000)


class SubjectEdit(SubjectFields):
    expected_revision: int = Field(ge=1, strict=True)
    status: Literal["active", "inactive", "archived", "deleted"]


async def authorize(db, actor, project_id, *, write=False, subject_write=False, run_write=False, csrf=None):
    if write or subject_write or run_write:
        check_csrf(actor[2], csrf)
    permission = ("scheduled_task.subject.manage" if subject_write else
                  "scheduled_task.run" if run_write else
                  "scheduled_task.manage" if write else "scheduled_task.read")
    project = await human_project(db, actor[0], project_id, permission)
    if (write or subject_write or run_write) and project.status == "archived":
        raise HTTPException(status_code=409, detail="Projet archivé.")
    return project


def domain_error(exc):
    return HTTPException(status_code=403 if isinstance(exc, PermissionError) else
                         404 if isinstance(exc, LookupError) else 409, detail=str(exc))


async def authorize_result(db, actor, project_id, permission, csrf):
    check_csrf(actor[2], csrf)
    project = await human_project(db, actor[0], project_id, permission)
    if project.status == "archived":
        raise HTTPException(status_code=409, detail="Projet archivé.")
    role = await db.scalar(select(ProjectMember.role_code).where(
        ProjectMember.project_id == project_id, ProjectMember.user_id == actor[0].id,
        ProjectMember.is_active.is_(True)))
    if role is None or role == "reader":
        raise HTTPException(status_code=403, detail="Rôle projet insuffisant.")
    return project


@router.get("")
async def list_tasks(project_id: str, db=Depends(db_session), actor=Depends(human)):
    await authorize(db, actor, project_id)
    rows = (await db.scalars(select(ScheduledLLMTask).where(
        ScheduledLLMTask.project_id == project_id, ScheduledLLMTask.status != "deleted")
        .order_by(ScheduledLLMTask.updated_at.desc(), ScheduledLLMTask.id))).all()
    return {"tasks": [task_view(row, await current_version(db, row)) for row in rows]}


@router.post("", status_code=201)
async def new_task(project_id: str, payload: Configuration, x_csrf_token: str | None = Header(default=None),
                   db=Depends(db_session), actor=Depends(human)):
    await authorize(db, actor, project_id, write=True, csrf=x_csrf_token)
    try:
        task, version = await create_task(db, project_id, payload.values(), actor[0].id)
    except (ValueError, LookupError) as exc:
        raise domain_error(exc) from exc
    await db.commit()
    return task_view(task, version)


@router.get("/{task_id}")
async def get_task(project_id: str, task_id: UUID, db=Depends(db_session), actor=Depends(human)):
    await authorize(db, actor, project_id)
    row = await db.scalar(select(ScheduledLLMTask).where(
        ScheduledLLMTask.id == str(task_id), ScheduledLLMTask.project_id == project_id,
        ScheduledLLMTask.status != "deleted"))
    if row is None:
        raise HTTPException(status_code=404)
    return task_view(row, await current_version(db, row))


@router.put("/{task_id}")
async def edit_task(project_id: str, task_id: UUID, payload: TaskEdit,
                    x_csrf_token: str | None = Header(default=None), db=Depends(db_session), actor=Depends(human)):
    await authorize(db, actor, project_id, write=True, csrf=x_csrf_token)
    try:
        task, version = await revise_task(db, project_id, str(task_id), payload.expected_revision,
                                          payload.values(), actor[0].id)
    except (ValueError, LookupError) as exc:
        raise domain_error(exc) from exc
    await db.commit()
    return task_view(task, version)


@router.get("/{task_id}/history")
async def task_history(project_id: str, task_id: UUID, db=Depends(db_session), actor=Depends(human)):
    await authorize(db, actor, project_id)
    exists = await db.scalar(select(ScheduledLLMTask.id).where(
        ScheduledLLMTask.id == str(task_id), ScheduledLLMTask.project_id == project_id))
    if exists is None:
        raise HTTPException(status_code=404)
    rows = (await db.scalars(select(ScheduledLLMTaskVersion).where(
        ScheduledLLMTaskVersion.task_id == str(task_id)).order_by(ScheduledLLMTaskVersion.version))).all()
    return {"versions": [version_view(row) for row in rows]}


@router.get("/{task_id}/runs")
async def task_runs(project_id: str, task_id: UUID, response: Response,
                    db=Depends(db_session), actor=Depends(human)):
    await authorize(db, actor, project_id)
    exists = await db.scalar(select(ScheduledLLMTask.id).where(
        ScheduledLLMTask.id == str(task_id), ScheduledLLMTask.project_id == project_id))
    if exists is None:
        raise HTTPException(status_code=404)
    rows = (await db.scalars(select(ScheduledLLMRun).where(
        ScheduledLLMRun.project_id == project_id, ScheduledLLMRun.task_id == str(task_id))
        .order_by(ScheduledLLMRun.created_at.desc(), ScheduledLLMRun.id).limit(100))).all()
    counts = dict((await db.execute(select(LLMCallAttempt.scheduled_llm_run_id, func.count())
        .where(LLMCallAttempt.scheduled_llm_run_id.in_([row.id for row in rows]))
        .group_by(LLMCallAttempt.scheduled_llm_run_id))).all()) if rows else {}
    response.headers["Cache-Control"] = "no-store"
    return {"runs": [{**run_view(row), "retry_count": max(0, counts.get(row.id, 0) - 1)} for row in rows]}


@router.get("/{task_id}/occurrences")
async def task_occurrences(project_id: str, task_id: UUID, db=Depends(db_session), actor=Depends(human)):
    await authorize(db, actor, project_id)
    exists = await db.scalar(select(ScheduledLLMTask.id).where(
        ScheduledLLMTask.id == str(task_id), ScheduledLLMTask.project_id == project_id))
    if exists is None:
        raise HTTPException(status_code=404)
    rows = (await db.scalars(select(ScheduledLLMOccurrence).where(
        ScheduledLLMOccurrence.project_id == project_id, ScheduledLLMOccurrence.task_id == str(task_id))
        .order_by(ScheduledLLMOccurrence.scheduled_at.desc(), ScheduledLLMOccurrence.id).limit(100))).all()
    return {"occurrences": [occurrence_view(row) for row in rows]}


@router.get("/{task_id}/runs/{run_id}")
async def task_run_detail(project_id: str, task_id: UUID, run_id: UUID, response: Response,
                          db=Depends(db_session), actor=Depends(human)):
    await authorize(db, actor, project_id)
    row = await db.scalar(select(ScheduledLLMRun).where(ScheduledLLMRun.id == str(run_id),
        ScheduledLLMRun.task_id == str(task_id), ScheduledLLMRun.project_id == project_id))
    if row is None:
        raise HTTPException(status_code=404)
    occurrence = await db.get(ScheduledLLMOccurrence, row.occurrence_id)
    calls = (await db.scalars(select(LLMCallAttempt).where(
        LLMCallAttempt.scheduled_llm_run_id == row.id).order_by(LLMCallAttempt.attempt_number))).all()
    current = await db.scalar(select(ScheduledLLMResultVersion).where(
        ScheduledLLMResultVersion.run_id == row.id,
        ScheduledLLMResultVersion.version == row.result_current_version)) if row.result_current_version else None
    response.headers["Cache-Control"] = "no-store"
    return {"run": {**run_view(row, include_output=True), "retry_count": max(0, len(calls)-1),
                    "result_current_version": row.result_current_version,
                    "result_status": current.status if current else None},
            "occurrence": occurrence_view(occurrence),
            "attempts": [{key: getattr(call, key) for key in ("id", "attempt_number", "status",
                "error_code", "input_tokens", "output_tokens", "estimated_cost", "started_at", "ended_at",
                "parameters")} | {"duration_seconds": (call.ended_at-call.started_at).total_seconds()
                    if call.ended_at else None}
                for call in calls]}


@router.get("/{task_id}/runs/{run_id}/results")
async def result_history(project_id: str, task_id: UUID, run_id: UUID, response: Response,
                         db=Depends(db_session), actor=Depends(human)):
    await authorize(db, actor, project_id)
    run = await db.scalar(select(ScheduledLLMRun).where(ScheduledLLMRun.id == str(run_id),
        ScheduledLLMRun.task_id == str(task_id), ScheduledLLMRun.project_id == project_id))
    if run is None:
        raise HTTPException(status_code=404)
    versions = (await db.scalars(select(ScheduledLLMResultVersion).where(
        ScheduledLLMResultVersion.run_id == run.id).order_by(ScheduledLLMResultVersion.version))).all()
    ids = [row.id for row in versions]
    comments = (await db.scalars(select(ScheduledLLMResultComment).where(
        ScheduledLLMResultComment.result_version_id.in_(ids))
        .order_by(ScheduledLLMResultComment.created_at))).all() if ids else []
    feedback = (await db.scalars(select(ScheduledLLMFeedback).where(
        ScheduledLLMFeedback.result_version_id.in_(ids))
        .order_by(ScheduledLLMFeedback.created_at))).all() if ids else []
    response.headers["Cache-Control"] = "no-store"
    return {"current_version": run.result_current_version,
            "versions": [result_view(row) for row in versions],
            "comments": [comment_view(row) for row in comments],
            "feedback": [feedback_view(row) for row in feedback]}


@router.post("/{task_id}/runs/{run_id}/results/revise", status_code=201)
async def revise_result(project_id: str, task_id: UUID, run_id: UUID, payload: ResultEdit,
                        x_csrf_token: str | None = Header(default=None),
                        db=Depends(db_session), actor=Depends(human)):
    await authorize_result(db, actor, project_id, "scheduled_task.result.edit", x_csrf_token)
    try:
        run = await lock_run(db, project_id, str(task_id), str(run_id))
        row = await revise(db, run, expected_version=payload.expected_version,
                           content=payload.content, actor=actor[0])
    except (ValueError, LookupError) as exc:
        raise domain_error(exc) from exc
    await db.commit()
    return result_view(row)


@router.post("/{task_id}/runs/{run_id}/results/decision/{action}", status_code=201)
async def decide_result(project_id: str, task_id: UUID, run_id: UUID,
                        action: Literal["submit", "validate", "reject"], payload: ResultDecision,
                        x_csrf_token: str | None = Header(default=None),
                        db=Depends(db_session), actor=Depends(human)):
    permission = "scheduled_task.result.edit" if action == "submit" else "scheduled_task.review"
    await authorize_result(db, actor, project_id, permission, x_csrf_token)
    try:
        run = await lock_run(db, project_id, str(task_id), str(run_id))
        row = await decide(db, run, expected_version=payload.expected_version,
                           action=action, reason=payload.reason, actor=actor[0])
    except (ValueError, LookupError, PermissionError) as exc:
        raise domain_error(exc) from exc
    await db.commit()
    return result_view(row)


@router.post("/{task_id}/runs/{run_id}/results/comments", status_code=201)
async def comment_result(project_id: str, task_id: UUID, run_id: UUID, payload: ResultCommentInput,
                         x_csrf_token: str | None = Header(default=None),
                         db=Depends(db_session), actor=Depends(human)):
    await authorize_result(db, actor, project_id, "scheduled_task.feedback", x_csrf_token)
    try:
        run = await lock_run(db, project_id, str(task_id), str(run_id))
        version = await version_for_run(db, run, str(payload.result_version_id))
        row = await add_comment(db, run, version, body=payload.body, actor=actor[0])
    except (ValueError, LookupError) as exc:
        raise domain_error(exc) from exc
    await db.commit()
    return comment_view(row)


@router.post("/{task_id}/runs/{run_id}/results/comments/{comment_id}/resolve")
async def resolve_result_comment(project_id: str, task_id: UUID, run_id: UUID, comment_id: UUID,
                                 x_csrf_token: str | None = Header(default=None),
                                 db=Depends(db_session), actor=Depends(human)):
    await authorize_result(db, actor, project_id, "scheduled_task.feedback", x_csrf_token)
    try:
        run = await lock_run(db, project_id, str(task_id), str(run_id))
        row = await db.scalar(select(ScheduledLLMResultComment).join(
            ScheduledLLMResultVersion, ScheduledLLMResultVersion.id == ScheduledLLMResultComment.result_version_id)
            .where(ScheduledLLMResultComment.id == str(comment_id),
                   ScheduledLLMResultVersion.run_id == run.id).with_for_update())
        if row is None:
            raise LookupError("Commentaire introuvable.")
        await resolve_comment(db, run, row, actor=actor[0])
    except (ValueError, LookupError) as exc:
        raise domain_error(exc) from exc
    await db.commit()
    return comment_view(row)


@router.post("/{task_id}/runs/{run_id}/results/feedback", status_code=201)
async def feedback_result(project_id: str, task_id: UUID, run_id: UUID, payload: ResultFeedbackInput,
                          x_csrf_token: str | None = Header(default=None),
                          db=Depends(db_session), actor=Depends(human)):
    await authorize_result(db, actor, project_id, "scheduled_task.feedback", x_csrf_token)
    try:
        run = await lock_run(db, project_id, str(task_id), str(run_id))
        version = await version_for_run(db, run, str(payload.result_version_id))
        row = await add_feedback(db, run, version, score=payload.score,
                                 note=payload.note, actor=actor[0])
    except (ValueError, LookupError) as exc:
        raise domain_error(exc) from exc
    await db.commit()
    return feedback_view(row)


@router.post("/{task_id}/run-now", status_code=202)
async def run_now(request: Request, project_id: str, task_id: UUID, payload: ManualRequest,
                  x_csrf_token: str | None = Header(default=None), db=Depends(db_session), actor=Depends(human)):
    await authorize(db, actor, project_id, run_write=True, csrf=x_csrf_token)
    try:
        row, created = await manual_run(db, project_id, str(task_id), str(payload.request_key),
                                        payload.expected_revision, actor[0].id)
    except (ValueError, LookupError) as exc:
        raise domain_error(exc) from exc
    await db.commit()
    if created:
        await dispatch_pending(request.app.state.sessions, run_ids=[row.id])
    await db.refresh(row)
    return run_view(row)


@router.post("/{task_id}/{action}")
async def task_action(project_id: str, task_id: UUID,
                      action: Literal["activate", "pause", "resume", "archive", "delete"],
                      payload: ExpectedRevision, x_csrf_token: str | None = Header(default=None),
                      db=Depends(db_session), actor=Depends(human)):
    await authorize(db, actor, project_id, write=True, csrf=x_csrf_token)
    if action == "delete" and not payload.confirm:
        raise HTTPException(status_code=409, detail="Confirmez le retrait logique de la tâche.")
    try:
        row = await transition_task(db, project_id, str(task_id), action,
                                    payload.expected_revision, actor[0].id)
    except (ValueError, LookupError) as exc:
        raise domain_error(exc) from exc
    await db.commit()
    return task_view(row, await current_version(db, row))


@subject_router.get("")
async def list_subjects(project_id: str, db=Depends(db_session), actor=Depends(human)):
    await authorize(db, actor, project_id)
    rows = (await db.scalars(select(ScheduledLLMSubject).where(
        ScheduledLLMSubject.project_id == project_id, ScheduledLLMSubject.status != "deleted")
        .order_by(ScheduledLLMSubject.title, ScheduledLLMSubject.id))).all()
    return {"subjects": [subject_view(row) for row in rows]}


@subject_router.post("", status_code=201)
async def new_subject(project_id: str, payload: SubjectFields,
                      x_csrf_token: str | None = Header(default=None), db=Depends(db_session), actor=Depends(human)):
    await authorize(db, actor, project_id, subject_write=True, csrf=x_csrf_token)
    row = await create_subject(db, project_id, payload.model_dump(), actor[0].id)
    await db.commit()
    return subject_view(row)


@subject_router.put("/{subject_id}")
async def edit_subject(project_id: str, subject_id: UUID, payload: SubjectEdit,
                       x_csrf_token: str | None = Header(default=None), db=Depends(db_session), actor=Depends(human)):
    await authorize(db, actor, project_id, subject_write=True, csrf=x_csrf_token)
    try:
        row = await revise_subject(db, project_id, str(subject_id), payload.expected_revision,
                                   payload.model_dump(exclude={"expected_revision"}), actor[0].id)
    except (ValueError, LookupError) as exc:
        raise domain_error(exc) from exc
    await db.commit()
    return subject_view(row)


@subject_router.get("/{subject_id}/history")
async def subject_history(project_id: str, subject_id: UUID, db=Depends(db_session), actor=Depends(human)):
    await authorize(db, actor, project_id)
    exists = await db.scalar(select(ScheduledLLMSubject.id).where(
        ScheduledLLMSubject.id == str(subject_id), ScheduledLLMSubject.project_id == project_id))
    if exists is None:
        raise HTTPException(status_code=404)
    rows = (await db.scalars(select(ScheduledLLMSubjectRevision).where(
        ScheduledLLMSubjectRevision.subject_id == str(subject_id)).order_by(ScheduledLLMSubjectRevision.revision))).all()
    return {"revisions": [{key: getattr(row, key) for key in ("revision", "title", "description", "status", "actor_id", "created_at")} for row in rows]}
