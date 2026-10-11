"""Transitions humaines et références documentaires immuables."""

import json

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.business_models import (Deliverable, DeliverableComment, DeliverableVersion,
                                 ExportArtifact, PublicationRecord)
from app.identity_models import User, utcnow
from app.identity_service import audit


BUSINESS_TYPES = {"scope", "use_cases", "user_stories", "functional_doc"}
TECHNICAL_TYPES = {"architecture", "data_model", "roles_permissions", "diagrams", "technical_doc"}
EXPORT_FORMATS = {"markdown": ("text/markdown; charset=utf-8", "md"),
                  "json": ("application/json; charset=utf-8", "json")}


def validator_permission(kind: str) -> str:
    if kind in BUSINESS_TYPES:
        return "deliverable.validate.business"
    if kind in TECHNICAL_TYPES:
        return "architecture.validate"
    raise ValueError("Type de livrable inconnu.")


def expected_role(kind: str) -> str:
    return "Product Owner" if kind in BUSINESS_TYPES else "Architecte Applicatif"


async def transition(db: AsyncSession, deliverable: Deliverable, current: DeliverableVersion,
                     *, actor: User, action: str, reason: str = "") -> DeliverableVersion:
    if current.version != deliverable.current_version:
        raise ValueError("Ce livrable a changé ; rechargez la page.")
    expected, target = {"submit": ("Brouillon IA", "À revoir"),
                        "validate": ("À revoir", "Validé"),
                        "reject": ("À revoir", "Rejeté")}[action]
    if current.status != expected:
        raise ValueError("Transition documentaire interdite.")
    reason = reason.strip()
    if action == "reject" and not 1 <= len(reason) <= 2000:
        raise ValueError("Le motif du rejet doit contenir de 1 à 2 000 caractères.")
    deliverable.current_version += 1
    version = DeliverableVersion(deliverable_id=deliverable.id, version=deliverable.current_version,
                                 title=current.title, content=current.content, status=target,
                                 generation_run_id=current.generation_run_id,
                                 parent_version_id=current.id, actor_id=actor.id)
    db.add(version)
    await db.flush()
    if action == "reject":
        comment = DeliverableComment(version_id=version.id, author_id=actor.id, body=reason)
        db.add(comment)
        await db.flush()
        audit(db, "deliverable.comment", "success", actor_id=actor.id,
              project_id=deliverable.project_id, target_type="deliverable_comment",
              target_id=comment.id, details={"version_id": version.id, "status": "open",
                                             "rejection_reason": True})
    audit(db, f"deliverable.{action}", "success", actor_id=actor.id,
          project_id=deliverable.project_id, target_type="deliverable_version", target_id=version.id,
          details={"parent_version_id": current.id, "version": version.version,
                   "reason_comment": action == "reject"})
    return version


async def add_comment(db: AsyncSession, version: DeliverableVersion, *, actor: User,
                      project_id: str, body: str) -> DeliverableComment:
    body = body.strip()
    if not 1 <= len(body) <= 5000:
        raise ValueError("Le commentaire doit contenir de 1 à 5 000 caractères.")
    comment = DeliverableComment(version_id=version.id, author_id=actor.id, body=body)
    db.add(comment)
    await db.flush()
    audit(db, "deliverable.comment", "success", actor_id=actor.id, project_id=project_id,
          target_type="deliverable_comment", target_id=comment.id,
          details={"version_id": version.id, "status": "open"})
    return comment


async def resolve_comment(db: AsyncSession, comment: DeliverableComment, *, actor: User,
                          project_id: str) -> None:
    if comment.status != "open":
        raise ValueError("Ce commentaire est déjà résolu.")
    comment.status, comment.resolved_by, comment.resolved_at = "resolved", actor.id, utcnow()
    audit(db, "deliverable.comment.resolve", "success", actor_id=actor.id, project_id=project_id,
          target_type="deliverable_comment", target_id=comment.id,
          details={"version_id": comment.version_id, "status": "resolved"})


async def publish(db: AsyncSession, deliverable: Deliverable, current: DeliverableVersion,
                  *, actor: User) -> PublicationRecord:
    if current.version != deliverable.current_version:
        raise ValueError("Ce livrable a changé ; rechargez la page.")
    if current.status != "Validé":
        raise ValueError("Seule une version validée peut être publiée.")
    previous = await db.scalar(select(PublicationRecord).where(
        PublicationRecord.deliverable_id == deliverable.id,
        PublicationRecord.active.is_(True)).with_for_update())
    if previous:
        previous.active, previous.replaced_at = False, utcnow()
        await db.flush()
    deliverable.current_version += 1
    version = DeliverableVersion(deliverable_id=deliverable.id, version=deliverable.current_version,
                                 title=current.title, content=current.content, status="Publié",
                                 generation_run_id=current.generation_run_id,
                                 parent_version_id=current.id, actor_id=actor.id)
    db.add(version)
    await db.flush()
    publication = PublicationRecord(deliverable_id=deliverable.id, version_id=version.id,
                                    active=True, published_by=actor.id)
    db.add(publication)
    await db.flush()
    audit(db, "deliverable.publish", "success", actor_id=actor.id,
          project_id=deliverable.project_id, target_type="publication_record", target_id=publication.id,
          details={"version_id": version.id, "validated_version_id": current.id,
                   "replaced_publication_id": previous.id if previous else None})
    return publication


async def request_export(db: AsyncSession, publication: PublicationRecord, *, actor: User,
                         project_id: str, format: str) -> ExportArtifact:
    if format not in EXPORT_FORMATS:
        raise ValueError("Format d'export inconnu.")
    if not publication.active:
        raise ValueError("La référence publiée est obsolète.")
    artifact = ExportArtifact(publication_id=publication.id, format=format, status="queued",
                              requested_by=actor.id)
    db.add(artifact)
    await db.flush()
    audit(db, "export.request", "success", actor_id=actor.id, project_id=project_id,
          target_type="export_artifact", target_id=artifact.id,
          details={"version_id": publication.version_id, "format": format})
    return artifact


def export_bytes(*, project_id: str, version: DeliverableVersion, format: str) -> bytes:
    if format == "markdown":
        return (f"# {version.title}\n\nProjet : {project_id}\nVersion : v{version.version}\n"
                f"Statut : Publié\n\n{version.content}\n").encode("utf-8")
    if format == "json":
        return json.dumps({"project_id": project_id, "version_id": version.id,
                           "version": version.version, "title": version.title,
                           "status": "Publié", "content": version.content},
                          ensure_ascii=False, indent=2).encode("utf-8")
    raise ValueError("Format d'export inconnu.")
