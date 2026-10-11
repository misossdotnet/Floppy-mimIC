"""Socle Celery : jobs idempotents et messages sans contenu documentaire."""

import os
import asyncio
import hashlib
from urllib.parse import urlsplit

from celery import Celery
from sqlalchemy import select

from app.object_storage import ObjectStorage, StorageUnavailable
from app.business_models import (AnalysisRun, Deliverable, DeliverableVersion, DiffRun, ExportArtifact,
                                 GenerationRun, PublicationRecord, SourceDocument)
from app.context_service import create_item, parse_proposals, CONTEXT_TYPES
from app.generation_service import input_messages, validate_output
from app.review_service import EXPORT_FORMATS, export_bytes
from app.versioning_service import content_diff
from app.identity_models import utcnow
from app.identity_service import audit, database_factory
from app.ingestion_service import InvalidSource, extract_text, validate_text
from app.llm_models import LLMProfileVersion
from app.llm_service import LLMInvocationError, invoke_profile
from app.llm_trace import analysis_inputs, apply_usage, redact, traced_invoke
from app.agent_llm_service import guard_generation, finish_generation
from app.object_storage import new_object_key


def redis_url() -> str:
    url = os.environ.get("MIMIC_REDIS_URL", "")
    parsed = urlsplit(url)
    if parsed.scheme not in {"redis", "rediss"} or not parsed.hostname:
        raise RuntimeError("MIMIC_REDIS_URL doit désigner un broker Redis.")
    return url


celery_app = Celery("mimic", broker=redis_url(), backend=redis_url())
celery_app.conf.update(
    task_serializer="json", accept_content=["json"], result_serializer="json",
    task_acks_late=True, task_reject_on_worker_lost=True,
    worker_prefetch_multiplier=1, broker_connection_retry_on_startup=True,
    task_soft_time_limit=45, task_time_limit=60, result_expires=3600,
)
celery_app.conf.beat_schedule = {
    "mimic.shared.schedule.tick": {"task": "mimic.schedule.tick", "schedule": 60.0},
}


@celery_app.task(name="mimic.schedule.tick", soft_time_limit=45, time_limit=55)
def schedule_tick() -> None:
    from app.schedule_dispatcher import tick
    async def run():
        factory = database_factory()
        try:
            await tick(factory)
        finally:
            await factory.kw["bind"].dispose()
    asyncio.run(run())


@celery_app.task(name="mimic.coffy.execute", soft_time_limit=100, time_limit=110)
def execute_coffy(run_id: str) -> None:
    from app.coffy_execution import execute_run
    async def run():
        factory = database_factory()
        try:
            await execute_run(factory, run_id)
        finally:
            await factory.kw["bind"].dispose()
    asyncio.run(run())


async def _export_artifact(artifact_id: str) -> None:
    factory = database_factory()
    try:
        async with factory() as db:
            artifact = await db.scalar(select(ExportArtifact).where(
                ExportArtifact.id == artifact_id).with_for_update())
            if artifact is None or artifact.status != "queued":
                return
            publication = await db.get(PublicationRecord, artifact.publication_id)
            if publication is None or not publication.active:
                artifact.status, artifact.error_code, artifact.completed_at = "failed", "obsolete_reference", utcnow()
                if publication is not None:
                    deliverable = await db.get(Deliverable, publication.deliverable_id)
                    audit(db, "export.complete", "failed", actor_id=artifact.requested_by,
                          project_id=deliverable.project_id, target_type="export_artifact",
                          target_id=artifact.id, details={"error_code": "obsolete_reference",
                                                          "format": artifact.format,
                                                          "version_id": publication.version_id})
                await db.commit()
                return
            version = await db.get(DeliverableVersion, publication.version_id)
            deliverable = await db.get(Deliverable, publication.deliverable_id)
            artifact.status = "running"
            await db.commit()
            project_id = deliverable.project_id
            try:
                payload = export_bytes(project_id=project_id, version=version, format=artifact.format)
                key = new_object_key(project_id, "exports")
                digest = ObjectStorage.from_env().put_bytes(key, payload, EXPORT_FORMATS[artifact.format][0])
                error = None
            except (StorageUnavailable, ValueError):
                error = "storage_unavailable"
            except Exception:
                error = "export_failed"
            artifact = await db.scalar(select(ExportArtifact).where(
                ExportArtifact.id == artifact_id).with_for_update())
            publication = await db.get(PublicationRecord, artifact.publication_id)
            if not publication.active:
                error = "obsolete_reference"
            if error:
                artifact.status, artifact.error_code = "failed", error
            else:
                artifact.status, artifact.object_key = "ready", key
                artifact.sha256, artifact.size_bytes = digest, len(payload)
            artifact.completed_at = utcnow()
            audit(db, "export.complete", "failed" if error else "success",
                  actor_id=artifact.requested_by, project_id=project_id,
                  target_type="export_artifact", target_id=artifact.id,
                  details={"error_code": error, "format": artifact.format,
                           "version_id": publication.version_id})
            await db.commit()
    finally:
        await factory.kw["bind"].dispose()


@celery_app.task(name="mimic.export.artifact")
def build_export(artifact_id: str) -> None:
    asyncio.run(_export_artifact(artifact_id))


@celery_app.task(name="mimic.storage.verify", bind=True, max_retries=2)
def verify_stored_object(self, key: str, expected_sha256: str) -> dict:
    """Contrôle réexécutable ; le broker et le résultat ne portent que des métadonnées."""
    try:
        return ObjectStorage.from_env().verify(key, expected_sha256)
    except StorageUnavailable as exc:
        raise self.retry(exc=exc, countdown=min(2 ** self.request.retries, 4))


async def _extract_source(source_id: str) -> None:
    factory = database_factory()
    try:
        async with factory() as db:
            source = await db.scalar(select(SourceDocument).where(SourceDocument.id == source_id)
                                     .with_for_update())
            if source is None or source.status != "uploaded":
                return
            source.status = "extracting"
            source.attempt += 1
            await db.commit()
        try:
            if source.kind == "need":
                text = validate_text(source.direct_text or "")
            else:
                content = ObjectStorage.from_env().get_bytes(source.object_key)
                if hashlib.sha256(content).hexdigest() != source.sha256:
                    raise InvalidSource("Empreinte source incohérente.")
                text = extract_text(source.name, content)
            status, error = "extracted", None
        except StorageUnavailable:
            status, error, text = "error", "storage_unavailable", None
        except Exception:
            status, error, text = "error", "extraction_failed", None
        async with factory() as db:
            source = await db.scalar(select(SourceDocument).where(SourceDocument.id == source_id)
                                     .with_for_update())
            if source and source.status == "extracting":
                source.status, source.error_code, source.extracted_text = status, error, text
                audit(db, "source.extract", "success" if not error else "failed",
                      actor_id=source.created_by, project_id=source.project_id,
                      target_type="source_document", target_id=source.id,
                      details={"attempt": source.attempt, "error_code": error})
                await db.commit()
    finally:
        await factory.kw["bind"].dispose()


@celery_app.task(name="mimic.source.extract")
def extract_source(source_id: str) -> None:
    asyncio.run(_extract_source(source_id))


async def _analyze_source(run_id: str) -> None:
    factory = database_factory()
    try:
        async with factory() as db:
            run = await db.scalar(select(AnalysisRun).where(AnalysisRun.id == run_id).with_for_update())
            if run is None or run.status != "queued":
                return
            source = await db.get(SourceDocument, run.source_id)
            profile = await db.get(LLMProfileVersion, run.llm_profile_version_id)
            if source is None or source.status != "analyzing" or profile is None:
                run.status, run.error_code, run.completed_at = "failed", "invalid_state", utcnow()
                await db.commit()
                return
            run.status = "running"
            # Migration : les anciennes tentatives en attente figent leurs entrées au premier démarrage.
            if run.context_snapshot is None:
                for key, value in analysis_inputs(source, profile).items():
                    setattr(run, key, value)
            await db.commit()
        try:
            if not run.context_snapshot["text"]:
                raise ValueError("Source sans texte extrait.")
            result = await traced_invoke(factory, run, profile, [
                {"role": "system", "content": run.prompt_template},
                {"role": "user", "content": run.context_snapshot["text"]},
            ], invoke=invoke_profile)
            proposals = parse_proposals(redact(result.text))
            error = None
        except LLMInvocationError as exc:
            error, result, proposals = exc.category, None, []
        except ValueError:
            error, result, proposals = "invalid_response", None, []
        except Exception:
            error, result, proposals = "analysis_failed", None, []
        async with factory() as db:
            run = await db.scalar(select(AnalysisRun).where(AnalysisRun.id == run_id).with_for_update())
            source = await db.scalar(select(SourceDocument).where(SourceDocument.id == run.source_id)
                                     .with_for_update())
            if run.status != "running" or source.status != "analyzing":
                return
            if error:
                run.status, run.error_code = "failed", error
                source.status, source.error_code = "extracted", error
            else:
                for proposal in proposals:
                    await create_item(db, project_id=run.project_id, source_id=source.id,
                                      analysis_run_id=run.id, actor_id=None, origin="ai", **proposal)
                run.status = "completed"
                run.input_tokens, run.output_tokens = result.input_tokens, result.output_tokens
                source.status, source.error_code = "analyzed", None
            run.completed_at = utcnow()
            await apply_usage(db, run)
            audit(db, "source.analyze", "failed" if error else "success",
                  actor_id=run.requested_by, project_id=run.project_id,
                  target_type="analysis_run", target_id=run.id,
                  details={"source_id": source.id, "profile_version_id": run.llm_profile_version_id,
                           "error_code": error, "proposals": len(proposals)})
            await db.commit()
    finally:
        await factory.kw["bind"].dispose()


@celery_app.task(name="mimic.source.analyze", soft_time_limit=100, time_limit=110)
def analyze_source(run_id: str) -> None:
    asyncio.run(_analyze_source(run_id))


async def _generate_document(run_id: str) -> None:
    factory = database_factory()
    try:
        async with factory() as db:
            candidate = await db.get(GenerationRun, run_id)
            if candidate is None or candidate.status != "queued":
                return
            try:
                await guard_generation(db, candidate)
                guard_error = None
            except ValueError as exc:
                from app.runtime_service import RuntimeDenied
                guard_error = exc.code if isinstance(exc, RuntimeDenied) else "execution_unavailable"
            run = await db.scalar(select(GenerationRun).where(
                GenerationRun.id == run_id).with_for_update().execution_options(populate_existing=True))
            if run is None or run.status != "queued":
                return
            deliverable = await db.get(Deliverable, run.deliverable_id)
            profile = await db.get(LLMProfileVersion, run.llm_profile_version_id)
            parent = await db.get(DeliverableVersion, run.parent_version_id) if run.parent_version_id else None
            expected_version = parent.version if parent else 0
            valid_parent = (parent is not None and parent.deliverable_id == run.deliverable_id) if run.parent_version_id else True
            if (deliverable is None or deliverable.current_version != expected_version or
                not valid_parent or profile is None or guard_error):
                run.status, run.error_code, run.completed_at = "failed", guard_error or "invalid_state", utcnow()
                await finish_generation(db, run, run.error_code)
                audit(db, "generation.complete", "failed", actor_id=run.requested_by,
                      project_id=run.project_id, target_type="generation_run", target_id=run.id,
                      details={"error_code": run.error_code})
                await db.commit()
                return
            run.status = "running"
            await db.commit()
        try:
            result = await traced_invoke(factory, run, profile,
                input_messages(run, parent_content=parent.content if parent else None),
                invoke=invoke_profile, guard=guard_generation)
            content = validate_output(redact(result.text))
            error = None
        except LLMInvocationError as exc:
            error, result, content = exc.category, None, None
        except ValueError:
            error, result, content = "invalid_response", None, None
        except Exception:
            error, result, content = "generation_failed", None, None
        async with factory() as db:
            try:
                await guard_generation(db, run)
            except ValueError as exc:
                from app.runtime_service import RuntimeDenied
                error = exc.code if isinstance(exc, RuntimeDenied) else "execution_unavailable"
            run = await db.scalar(select(GenerationRun).where(
                GenerationRun.id == run_id).with_for_update().execution_options(populate_existing=True))
            if run is None or run.status != "running":
                return
            deliverable = await db.scalar(select(Deliverable).where(
                Deliverable.id == run.deliverable_id).with_for_update())
            parent = await db.get(DeliverableVersion, run.parent_version_id) if run.parent_version_id else None
            expected_version = parent.version if parent else 0
            if (deliverable is None or deliverable.current_version != expected_version or
                (run.parent_version_id and (parent is None or parent.deliverable_id != run.deliverable_id))):
                error = "stale_parent" if run.parent_version_id else "invalid_state"
            try:
                await finish_generation(db, run, error)
            except ValueError as exc:
                from app.runtime_service import RuntimeDenied
                error = exc.code if isinstance(exc, RuntimeDenied) else "execution_unavailable"
                await finish_generation(db, run, error)
            if error:
                run.status, run.error_code = "failed", error
            else:
                version = DeliverableVersion(deliverable_id=deliverable.id, version=expected_version + 1,
                                             title=deliverable.title, content=content,
                                             status="Brouillon IA", generation_run_id=run.id,
                                             parent_version_id=run.parent_version_id)
                db.add(version)
                deliverable.current_version = expected_version + 1
                run.status = "completed"
                run.input_tokens, run.output_tokens = result.input_tokens, result.output_tokens
            run.completed_at = utcnow()
            await apply_usage(db, run)
            audit(db, "generation.complete", "failed" if error else "success",
                  actor_id=run.requested_by, project_id=run.project_id,
                  target_type="generation_run", target_id=run.id,
                  details={"deliverable_id": run.deliverable_id,
                           "profile_version_id": run.llm_profile_version_id,
                           "parent_version_id": run.parent_version_id,
                           "error_code": error, "version": expected_version + 1 if not error else None})
            await db.commit()
    finally:
        await factory.kw["bind"].dispose()


@celery_app.task(name="mimic.document.generate", soft_time_limit=100, time_limit=110)
def generate_document(run_id: str) -> None:
    asyncio.run(_generate_document(run_id))


async def _compute_diff(run_id: str) -> None:
    factory = database_factory()
    try:
        async with factory() as db:
            run = await db.scalar(select(DiffRun).where(DiffRun.id == run_id).with_for_update())
            if run is None or run.status != "queued":
                return
            run.status = "running"
            await db.commit()
        async with factory() as db:
            run = await db.scalar(select(DiffRun).where(DiffRun.id == run_id).with_for_update())
            if run is None or run.status != "running":
                return
            left = await db.get(DeliverableVersion, run.left_version_id)
            right = await db.get(DeliverableVersion, run.right_version_id)
            deliverable = await db.get(Deliverable, left.deliverable_id) if left else None
            try:
                if (left is None or right is None or deliverable is None or
                    deliverable.project_id != run.project_id):
                    raise ValueError("Versions invalides.")
                result = content_diff(left, right)
                run.status, run.result = "completed", result
                error = None
            except ValueError:
                run.status, run.error_code = "failed", "invalid_versions"
                error = "invalid_versions"
            except Exception:
                run.status, run.error_code = "failed", "diff_failed"
                error = "diff_failed"
            run.completed_at = utcnow()
            audit(db, "version.diff.complete", "failed" if error else "success",
                  actor_id=run.requested_by, project_id=run.project_id,
                  target_type="diff_run", target_id=run.id,
                  details={"error_code": error, "left_version_id": run.left_version_id,
                           "right_version_id": run.right_version_id})
            await db.commit()
    finally:
        await factory.kw["bind"].dispose()


@celery_app.task(name="mimic.version.diff")
def compute_diff(run_id: str) -> None:
    asyncio.run(_compute_diff(run_id))


@celery_app.task(name="mimic.git.execute", soft_time_limit=110, time_limit=120)
def git_execute(operation_id: str) -> None:
    from app.git_worker import run_operation
    asyncio.run(run_operation(operation_id))
