"""Historique documentaire, diff lisible et restauration non destructive."""

import difflib

from sqlalchemy.ext.asyncio import AsyncSession

from app.business_models import Deliverable, DeliverableVersion, DiffRun
from app.generation_service import MAX_OUTPUT_CHARS
from app.identity_models import User
from app.identity_service import audit


MAX_DIFF_CHARS = 120_000


def content_diff(left: DeliverableVersion, right: DeliverableVersion) -> str:
    if left.deliverable_id != right.deliverable_id or left.id == right.id:
        raise ValueError("Choisissez deux versions distinctes du même livrable.")
    if len(left.content) > MAX_OUTPUT_CHARS or len(right.content) > MAX_OUTPUT_CHARS:
        raise ValueError("Une version dépasse la limite de comparaison.")
    lines = difflib.unified_diff([line + "\n" for line in left.content.splitlines()],
                                 [line + "\n" for line in right.content.splitlines()],
                                 fromfile=f"v{left.version}", tofile=f"v{right.version}", n=3)
    result = "".join(lines)
    if len(result) > MAX_DIFF_CHARS:
        marker = "\n… Diff tronqué à 120 000 caractères."
        return result[:MAX_DIFF_CHARS - len(marker)] + marker
    return result or "Aucune différence de contenu."


async def request_diff(db: AsyncSession, *, project_id: str, actor: User,
                       left: DeliverableVersion, right: DeliverableVersion) -> DiffRun:
    if left.deliverable_id != right.deliverable_id or left.id == right.id:
        raise ValueError("Choisissez deux versions distinctes du même livrable.")
    run = DiffRun(project_id=project_id, left_version_id=left.id,
                  right_version_id=right.id, requested_by=actor.id, status="queued")
    db.add(run)
    await db.flush()
    audit(db, "version.diff.request", "success", actor_id=actor.id,
          project_id=project_id, target_type="diff_run", target_id=run.id,
          details={"left_version_id": left.id, "right_version_id": right.id})
    return run


async def restore_version(db: AsyncSession, deliverable: Deliverable,
                          source: DeliverableVersion, *, actor: User,
                          expected_version: int) -> DeliverableVersion:
    if deliverable.current_version != expected_version:
        raise ValueError("Ce livrable a changé ; rechargez la page.")
    if source.deliverable_id != deliverable.id or source.version >= deliverable.current_version:
        raise ValueError("Sélectionnez une version historique de ce livrable.")
    deliverable.current_version += 1
    restored = DeliverableVersion(deliverable_id=deliverable.id,
                                  version=deliverable.current_version,
                                  title=source.title, content=source.content,
                                  status="À revoir", generation_run_id=source.generation_run_id,
                                  parent_version_id=source.id, actor_id=actor.id)
    db.add(restored)
    await db.flush()
    audit(db, "deliverable.restore", "success", actor_id=actor.id,
          project_id=deliverable.project_id, target_type="deliverable_version",
          target_id=restored.id, details={"source_version_id": source.id,
                                          "version": restored.version})
    return restored
