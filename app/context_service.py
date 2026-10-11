"""Propositions de contexte et décisions humaines versionnées."""

import json

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.business_models import ContextComment, ContextItem, ContextItemVersion, ContextRelation
from app.identity_models import User, utcnow
from app.identity_service import audit

CONTEXT_TYPES = {
    "actor": "Acteur", "requirement": "Exigence", "constraint": "Contrainte",
    "business_rule": "Règle métier", "entity": "Entité", "role": "Rôle",
    "permission": "Permission", "component": "Composant", "flow": "Flux",
    "risk": "Risque", "assumption": "Hypothèse",
}
TECHNICAL_TYPES = {"entity", "role", "permission", "component", "flow"}
RELATION_TYPES = {
    "depends_on": "Dépend de", "covers": "Couvre", "conflicts_with": "Contredit",
    "refines": "Précise", "relates_to": "Lié à",
}


def parse_proposals(raw: str) -> list[dict]:
    try:
        data = json.loads(raw)
    except (ValueError, TypeError) as exc:
        raise ValueError("Réponse IA non structurée.") from exc
    items = data.get("items") if isinstance(data, dict) else None
    if not isinstance(items, list) or not 1 <= len(items) <= 20:
        raise ValueError("Liste de propositions IA invalide.")
    cleaned = []
    for item in items:
        if not isinstance(item, dict) or item.get("type") not in CONTEXT_TYPES:
            raise ValueError("Type de proposition IA invalide.")
        title, content = item.get("title"), item.get("content")
        if not isinstance(title, str) or not isinstance(content, str):
            raise ValueError("Proposition IA incomplète.")
        title, content = title.strip(), content.strip()
        if not 1 <= len(title) <= 180 or not 1 <= len(content) <= 4000:
            raise ValueError("Proposition IA trop longue ou vide.")
        cleaned.append({"type": item["type"], "title": title, "content": content})
    return cleaned


async def create_item(db: AsyncSession, *, project_id: str, source_id: str | None,
                      analysis_run_id: str | None, actor_id: str | None, type: str,
                      title: str, content: str, origin: str) -> ContextItem:
    if type not in CONTEXT_TYPES or origin not in {"ai", "manual"}:
        raise ValueError("Type ou origine invalide.")
    title, content = title.strip(), content.strip()
    if not 1 <= len(title) <= 180 or not 1 <= len(content) <= 4000:
        raise ValueError("Titre ou contenu invalide.")
    status = "Brouillon IA" if origin == "ai" else "À revoir"
    item = ContextItem(project_id=project_id, source_id=source_id,
                       analysis_run_id=analysis_run_id, created_by=actor_id,
                       type=type, title=title, content=content, origin=origin,
                       status=status, current_version=1)
    db.add(item)
    await db.flush()
    db.add(ContextItemVersion(item_id=item.id, version=1, title=title,
                              content=content, status=status, actor_id=actor_id))
    audit(db, "context.create", "success", actor_id=actor_id, project_id=project_id,
          target_type="context_item", target_id=item.id,
          details={"type": type, "origin": origin, "source_id": source_id})
    return item


async def revise_item(db: AsyncSession, item: ContextItem, *, actor: User,
                      title: str, content: str, expected_version: int) -> None:
    if item.current_version != expected_version:
        raise ValueError("Cet élément a changé ; rechargez la page.")
    if item.status == "Obsolète":
        raise ValueError("Un élément obsolète ne peut pas être modifié.")
    title, content = title.strip(), content.strip()
    if not 1 <= len(title) <= 180 or not 1 <= len(content) <= 4000:
        raise ValueError("Titre ou contenu invalide.")
    old_status = item.status
    item.current_version += 1
    item.title, item.content, item.status = title, content, "À revoir"
    db.add(ContextItemVersion(item_id=item.id, version=item.current_version,
                              title=title, content=content, status=item.status,
                              actor_id=actor.id))
    audit(db, "context.revise", "success", actor_id=actor.id, project_id=item.project_id,
          target_type="context_item", target_id=item.id,
          details={"version": item.current_version, "from": old_status, "to": item.status})


async def decide_item(db: AsyncSession, item: ContextItem, *, actor: User,
                      decision: str, expected_version: int, reason: str = "") -> None:
    if item.current_version != expected_version:
        raise ValueError("Cet élément a changé ; rechargez la page.")
    if decision == "review" and item.status == "Brouillon IA":
        target = "À revoir"
    elif decision in {"validate", "reject"} and item.status == "À revoir":
        target = "Validé" if decision == "validate" else "Rejeté"
    else:
        raise ValueError("Transition de statut impossible.")
    reason = reason.strip()
    if decision == "reject" and not 1 <= len(reason) <= 2000:
        raise ValueError("Un motif de rejet de 1 à 2000 caractères est requis.")
    previous = item.status
    item.current_version += 1
    item.status = target
    version = ContextItemVersion(item_id=item.id, version=item.current_version,
                                 title=item.title, content=item.content,
                                 status=target, actor_id=actor.id)
    db.add(version)
    if decision == "reject":
        await db.flush()
        await add_comment(db, version, actor=actor, project_id=item.project_id, body=reason)
    audit(db, f"context.{decision}", "success", actor_id=actor.id,
          project_id=item.project_id, target_type="context_item", target_id=item.id,
          details={"from": previous, "to": target, "version": item.current_version})


async def obsolete_item(db: AsyncSession, item: ContextItem, *, actor: User,
                        expected_version: int) -> None:
    if item.current_version != expected_version:
        raise ValueError("Cet élément a changé ; rechargez la page.")
    if item.status == "Obsolète":
        raise ValueError("Cet élément est déjà obsolète.")
    previous = item.status
    item.current_version += 1
    item.status = "Obsolète"
    db.add(ContextItemVersion(item_id=item.id, version=item.current_version,
                              title=item.title, content=item.content, status=item.status,
                              actor_id=actor.id))
    audit(db, "context.obsolete", "success", actor_id=actor.id,
          project_id=item.project_id, target_type="context_item", target_id=item.id,
          details={"from": previous, "to": item.status, "version": item.current_version})


async def add_relation(db: AsyncSession, source: ContextItem, target: ContextItem, *,
                       actor: User, type: str, expected_version: int) -> ContextRelation:
    if source.current_version != expected_version:
        raise ValueError("Cet élément a changé ; rechargez la page.")
    if source.project_id != target.project_id or source.id == target.id:
        raise ValueError("Les deux éléments doivent être distincts et dans le même projet.")
    if source.status == "Obsolète" or target.status == "Obsolète":
        raise ValueError("Un élément obsolète ne peut pas être relié.")
    if type not in RELATION_TYPES:
        raise ValueError("Type de relation inconnu.")
    existing = await db.scalar(select(ContextRelation).where(
        ContextRelation.source_id == source.id, ContextRelation.target_id == target.id,
        ContextRelation.type == type, ContextRelation.is_active.is_(True)))
    if existing:
        raise ValueError("Cette relation existe déjà.")
    relation = ContextRelation(project_id=source.project_id, source_id=source.id,
                               target_id=target.id, type=type, created_by=actor.id)
    db.add(relation)
    await db.flush()
    audit(db, "context.relation.create", "success", actor_id=actor.id,
          project_id=source.project_id, target_type="context_relation", target_id=relation.id,
          details={"source_id": source.id, "target_id": target.id, "type": type})
    return relation


def remove_relation(db: AsyncSession, relation: ContextRelation, *, actor: User) -> None:
    if not relation.is_active:
        raise ValueError("Cette relation a déjà été retirée.")
    relation.is_active = False
    relation.removed_by = actor.id
    relation.removed_at = utcnow()
    audit(db, "context.relation.remove", "success", actor_id=actor.id,
          project_id=relation.project_id, target_type="context_relation", target_id=relation.id,
          details={"source_id": relation.source_id, "target_id": relation.target_id,
                   "type": relation.type})


async def add_comment(db: AsyncSession, version: ContextItemVersion, *, actor: User,
                      project_id: str, body: str) -> ContextComment:
    body = body.strip()
    if not 1 <= len(body) <= 2000:
        raise ValueError("Le commentaire doit contenir de 1 à 2000 caractères.")
    comment = ContextComment(item_version_id=version.id, author_id=actor.id,
                             body=body, status="open")
    db.add(comment)
    await db.flush()
    audit(db, "context.comment.create", "success", actor_id=actor.id,
          project_id=project_id, target_type="context_comment", target_id=comment.id,
          details={"item_id": version.item_id, "version": version.version})
    return comment


def resolve_comment(db: AsyncSession, comment: ContextComment, *, actor: User,
                    project_id: str, item_id: str) -> None:
    if comment.status != "open":
        raise ValueError("Ce commentaire est déjà résolu.")
    comment.status = "resolved"
    comment.resolved_by = actor.id
    comment.resolved_at = utcnow()
    audit(db, "context.comment.resolve", "success", actor_id=actor.id,
          project_id=project_id, target_type="context_comment", target_id=comment.id,
          details={"item_id": item_id})
