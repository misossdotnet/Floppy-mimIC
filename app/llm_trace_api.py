"""Lecture de provenance limitée au projet, sans cache de prompts sensibles."""
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, Response
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.agent_api import db_session, human, human_project
from app.business_models import AnalysisRun, GenerationRun
from app.llm_models import LLMCallAttempt
from app.llm_trace import cost_label, redact
from app.identity_service import project_permission

router = APIRouter(prefix="/api/projects/{project_id}/llm-runs", tags=["llm-traceability"])


@router.get("/{kind}/{run_id}")
async def trace_run(project_id: str, kind: Literal["analysis", "generation"], run_id: str,
                    response: Response, db: AsyncSession = Depends(db_session), actor=Depends(human)):
    await human_project(db, actor[0], project_id, "project.read")
    model = AnalysisRun if kind == "analysis" else GenerationRun
    run = await db.scalar(select(model).where(model.id == run_id, model.project_id == project_id))
    if run is None:
        raise HTTPException(status_code=404)
    column = LLMCallAttempt.analysis_run_id if kind == "analysis" else LLMCallAttempt.generation_run_id
    calls = (await db.scalars(select(LLMCallAttempt).where(column == run.id)
                              .order_by(LLMCallAttempt.attempt_number))).all()
    response.headers["Cache-Control"] = "no-store"
    parameters = run.parameters
    memory_hidden = False
    if parameters and parameters.get('memory'):
        from app.runtime_service import memory_available
        memory_hidden = (not await project_permission(db, actor[0], project_id, 'memory.read') or
                         not await memory_available(db,project_id,parameters['memory']))
        if memory_hidden:
            parameters = {**parameters,'memory':{'references':[],'unavailable':'Mémoire retirée ou accès révoqué.'}}
    return redact({"id": run.id, "status": run.status, "error_code": run.error_code,
            "llm_profile_version_id": run.llm_profile_version_id, "prompt_version": run.prompt_version,
            "prompt_template": run.prompt_template, "context_snapshot": run.context_snapshot,
            "parameters": parameters, "pricing_snapshot": run.pricing_snapshot,
            "messages_unavailable": memory_hidden,
            "input_tokens": run.input_tokens, "output_tokens": run.output_tokens,
            "estimated_cost": str(run.estimated_cost) if run.estimated_cost is not None else None,
            "cost_label": cost_label(run),
            "calls": [{key: None if key == 'messages' and memory_hidden else getattr(call, key) for key in (
                "id", "attempt_number", "profile_version_id", "messages", "parameters", "pricing_snapshot",
                "status", "error_code", "input_tokens", "output_tokens", "estimated_cost", "started_at", "ended_at")}
                for call in calls]})
