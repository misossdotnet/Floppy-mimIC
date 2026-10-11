"""Messages bornés et receipts ; aucune exécution implicite depuis le contenu."""
import hashlib
import json
from datetime import timedelta
from types import SimpleNamespace

from sqlalchemy import or_, select

from app.agent_models import AgentPlanVersion, AgentTask, AgentTaskClaim, AgentTaskRun, AgentTeamSkill, SkillProfile
from app.agent_routing import require_approved_execution
from app.conversation_models import AgentMessage, AgentMessageDelivery, CoordinationChannel
from app.identity_models import utcnow
from app.identity_service import audit
from app.llm_trace import redact
from app.memory_service import check_sources


class MessageMissing(ValueError):
    """Objet absent ou non accessible au profil courant."""


def event(db, session, action, message, **details):
    audit(db, action, "success", project_id=session.project_id,
          target_type="agent_message", target_id=message.id,
          details={"session_id": session.id, **details})


async def send_message(db, session, values, *, sensitive_token=None):
    # Le verrou de session pris à l'authentification sérialise les clés de rejeu.
    fingerprint = hashlib.sha256(json.dumps(values, sort_keys=True, ensure_ascii=False).encode()).hexdigest()
    previous = await db.scalar(select(AgentMessage).where(
        AgentMessage.sender_session_id == session.id, AgentMessage.request_key == values["request_key"]))
    if previous:
        if previous.request_fingerprint != fingerprint:
            raise ValueError("Clé d'envoi déjà utilisée avec une autre charge.")
        return previous, False

    task = await db.scalar(select(AgentTask).where(AgentTask.id == values["task_id"],
        AgentTask.project_id == session.project_id, AgentTask.team_id == session.team_id))
    if task is None:
        raise ValueError("Tâche source non autorisée.")
    plan = await db.scalar(select(AgentPlanVersion).where(AgentPlanVersion.id == task.plan_version_id)
        .with_for_update(key_share=True).execution_options(populate_existing=True))
    await require_approved_execution(db, plan, session.project_id, session.team_id)
    # Le coordinateur prend aussi le verrou de plan avant toute clôture de claim.
    await db.refresh(task)
    now = utcnow()
    attempt = await db.scalar(select(AgentTaskRun).join(AgentTaskClaim, AgentTaskClaim.id == AgentTaskRun.claim_id)
        .where(AgentTaskRun.task_id == task.id, AgentTaskClaim.session_id == session.id)
        .order_by(AgentTaskRun.attempt_number.desc()).limit(1))
    claim = await db.get(AgentTaskClaim, attempt.claim_id) if attempt else None
    active = (attempt and attempt.status == "running" and task.status == "claimed"
        and task.claimed_session_id == session.id and claim.released_at is None
        and claim.lease_expires_at > now and task.lease_expires_at and task.lease_expires_at > now)
    completed = (attempt and attempt.status == "completed" and task.status == "completed"
                 and claim.release_reason == "completed")
    if task.skill_profile_id != session.skill_profile_id or not (active or completed):
        raise ValueError("Tentative source propre à la session, active ou achevée, requise.")

    expires_at = values.get("expires_at")
    if expires_at:
        from datetime import datetime
        expires_at = datetime.fromisoformat(expires_at)
        if expires_at.tzinfo is None or not now < expires_at <= now + timedelta(days=7):
            raise ValueError("Expiration future avec fuseau, limitée à sept jours, requise.")
    else:
        expires_at = now + timedelta(hours=24)
    source_values = {"title": values["summary"], "content": values["content"],
                     "source_refs": values["artifact_refs"]}
    if sensitive_token and any(sensitive_token in source_values[key] for key in ("title", "content")):
        raise ValueError("Contenu refusé : jeton de session.")
    if any(redact(source_values[key]) != source_values[key] for key in ("title", "content")):
        raise ValueError("Contenu refusé : secret reconnaissable.")
    await check_sources(db, SimpleNamespace(project_id=session.project_id, scope="project"), source_values)
    profiles = set(await db.scalars(select(SkillProfile.id).join(AgentTeamSkill,
        AgentTeamSkill.skill_profile_id == SkillProfile.id).where(
        AgentTeamSkill.team_id == session.team_id, SkillProfile.is_active.is_(True))))
    channel_id = values.get("channel_id")
    if channel_id:
        channel = await db.scalar(select(CoordinationChannel).where(CoordinationChannel.id == channel_id,
            CoordinationChannel.project_id == session.project_id, CoordinationChannel.team_id == session.team_id)
            .with_for_update())
        if (channel is None or channel.plan_version_id != plan.id
            or session.skill_profile_id not in channel.participant_profile_ids):
            raise ValueError("Canal hors portée de la tâche et du profil.")
        if values.get("recipient_profile_id") or values.get("attachment") or values.get("channel_kind"):
            raise ValueError("Les participants et le rattachement d'un canal existant sont figés.")
        participants = channel.participant_profile_ids
    else:
        kind = values.get("channel_kind") or "direct"
        recipient = values.get("recipient_profile_id")
        if kind == "direct":
            if recipient not in profiles or recipient == session.skill_profile_id:
                raise ValueError("Profil destinataire actif et distinct de la même équipe requis.")
            participants = sorted((session.skill_profile_id, recipient))
        else:
            if recipient:
                raise ValueError("Un broadcast ou fil n'accepte pas de destinataire individuel.")
            participants = sorted(profiles)
        attachment = values.get("attachment")
        if kind != "thread" and attachment:
            raise ValueError("Seul un fil accepte un rattachement d'artefact.")
        if attachment and attachment['type'] == 'technical_incident':
            from app.incident_models import TechnicalIncident
            incident = await db.scalar(select(TechnicalIncident).where(TechnicalIncident.id == attachment['id'],
                TechnicalIncident.project_id == session.project_id, TechnicalIncident.team_id == session.team_id))
            if not incident:
                raise MessageMissing('Incident absent ou hors équipe.')
        elif attachment:
            attachment_values = {"source_refs": [attachment]}
            await check_sources(db, SimpleNamespace(project_id=session.project_id, scope="project"), attachment_values)
        channel = CoordinationChannel(project_id=session.project_id, plan_version_id=plan.id,
            team_id=session.team_id, task_id=task.id, kind=kind,
            participant_profile_ids=participants, attachment=attachment,
            created_by_session_id=session.id)
    recipients = sorted(set(participants) - {session.skill_profile_id})
    if not recipients or not set(participants) <= profiles:
        raise ValueError("Participants actifs de l'équipe requis.")
    reply = values.get("reply_to_id")
    if reply and (not channel_id or not await db.scalar(select(AgentMessage.id).where(
            AgentMessage.id == reply, AgentMessage.channel_id == channel_id))):
        raise ValueError("Réponse hors du canal.")
    target_id = values.get("target_task_id")
    if values["kind"] == "handoff":
        target = await db.scalar(select(AgentTask).where(AgentTask.id == target_id,
            AgentTask.plan_version_id == plan.id, AgentTask.team_id == session.team_id,
            AgentTask.project_id == session.project_id))
        if (channel.kind != "direct" or not target or target.skill_profile_id != recipients[0]
                or target.status in ("cancelled", "completed")):
            raise ValueError("Handoff direct vers une tâche du plan et une compétence compatible requis.")
    elif target_id:
        raise ValueError("La tâche cible est réservée aux handoffs.")
    # Une attente de verrou ou de référence ne prolonge jamais le bail/session.
    send_time = utcnow()
    if (session.expires_at <= send_time or session.max_expires_at <= send_time
            or (active and (claim.lease_expires_at <= send_time or task.lease_expires_at <= send_time))
            or expires_at <= send_time):
        raise ValueError("Session, bail ou message expiré avant transmission.")
    if not channel_id:
        db.add(channel)
        await db.flush()
    message = AgentMessage(project_id=session.project_id, plan_version_id=plan.id, team_id=session.team_id,
        channel_id=channel.id, sender_session_id=session.id, sender_profile_id=session.skill_profile_id,
        task_run_id=attempt.id, request_key=values["request_key"], request_fingerprint=fingerprint,
        reply_to_id=reply, kind=values["kind"], target_task_id=target_id, summary=values["summary"],
        content=values["content"], artifact_refs=source_values["source_refs"], priority=values["priority"],
        created_at=now, expires_at=expires_at)
    db.add(message)
    await db.flush()
    for profile_id in recipients:
        db.add(AgentMessageDelivery(project_id=session.project_id, message_id=message.id,
            recipient_profile_id=profile_id, status="pending"))
    event(db, session, "agent.message.send", message, channel_id=channel.id, kind=message.kind,
          priority=message.priority, task_run_id=attempt.id, recipient_count=len(recipients))
    await db.flush()
    return message, True


def effective_status(delivery, message):
    if delivery.status != "acknowledged" and message.expires_at <= utcnow():
        return "expired"
    return delivery.status


def channel_view(channel):
    return {key: getattr(channel, key) for key in ("id", "project_id", "plan_version_id", "team_id",
        "task_id", "kind", "participant_profile_ids", "attachment", "created_by_session_id", "created_at")}


async def message_view(db, message):
    deliveries = (await db.scalars(select(AgentMessageDelivery).where(
        AgentMessageDelivery.message_id == message.id).order_by(AgentMessageDelivery.recipient_profile_id))).all()
    receipts = [{"recipient_profile_id": row.recipient_profile_id, "status": effective_status(row, message),
        "delivered_at": row.delivered_at, "delivered_by_session_id": row.delivered_by_session_id,
        "acknowledged_at": row.acknowledged_at, "acknowledged_by_session_id": row.acknowledged_by_session_id}
        for row in deliveries]
    states = {receipt["status"] for receipt in receipts}
    aggregate = "acknowledged" if states == {"acknowledged"} else "expired" if "expired" in states else (
        "pending" if "pending" in states else "delivered")
    result = {key: getattr(message, key) for key in ("id", "project_id", "plan_version_id", "team_id",
        "channel_id", "sender_session_id", "sender_profile_id", "task_run_id", "reply_to_id", "kind",
        "target_task_id", "summary", "content", "artifact_refs", "priority", "created_at", "expires_at")}
    return {**result, "content_status": "Brouillon IA", "usage": "untrusted_coordination_data",
        "delivery_status": aggregate, "receipts": receipts,
        "handoff_status": ({"pending": "offered", "delivered": "received", "acknowledged": "accepted",
                             "expired": "expired"}[aggregate] if message.kind == "handoff" else None)}


async def history(db, project_id, *, session=None, channel_id=None, limit=50, offset=0):
    query = select(AgentMessage).where(AgentMessage.project_id == project_id)
    if session:
        own_delivery = select(AgentMessageDelivery.id).where(AgentMessageDelivery.message_id == AgentMessage.id,
            AgentMessageDelivery.recipient_profile_id == session.skill_profile_id).exists()
        query = query.where(AgentMessage.team_id == session.team_id,
            or_(AgentMessage.sender_profile_id == session.skill_profile_id, own_delivery))
    if channel_id:
        query = query.where(AgentMessage.channel_id == channel_id)
    messages = (await db.scalars(query.order_by(AgentMessage.created_at, AgentMessage.id)
                               .offset(offset).limit(limit + 1))).all()
    return {"messages": [await message_view(db, m) for m in messages[:limit]],
            "next_offset": offset + limit if len(messages) > limit else None}


def expire_delivery(db, session, message, delivery):
    if delivery.status not in ("acknowledged", "expired") and message.expires_at <= utcnow():
        delivery.status = "expired"
        event(db, session, "agent.message.expire", message, recipient_profile_id=delivery.recipient_profile_id)


async def poll_messages(db, session, limit):
    await receipt_scope_lock(db, session)
    scope = (AgentMessageDelivery.project_id == session.project_id,
             AgentMessageDelivery.recipient_profile_id == session.skill_profile_id,
             AgentMessage.team_id == session.team_id)
    # Traitement d'expiration borné, ordre stable pour les sessions d'un même profil.
    expired = (await db.execute(select(AgentMessageDelivery, AgentMessage).join(AgentMessage,
        AgentMessage.id == AgentMessageDelivery.message_id).where(*scope,
        AgentMessageDelivery.status.in_(("pending", "delivered")), AgentMessage.expires_at <= utcnow())
        .order_by(AgentMessageDelivery.id).limit(100).with_for_update(of=AgentMessageDelivery))).all()
    for delivery, message in expired:
        expire_delivery(db, session, message, delivery)
    await db.flush()
    rows = (await db.execute(select(AgentMessageDelivery, AgentMessage).join(AgentMessage,
        AgentMessage.id == AgentMessageDelivery.message_id).where(*scope,
        AgentMessageDelivery.status.in_(("pending", "delivered")), AgentMessage.expires_at > utcnow())
        .order_by(AgentMessage.priority, AgentMessage.created_at, AgentMessage.id).limit(limit)
        .with_for_update(of=AgentMessageDelivery))).all()
    received = []
    for delivery, message in rows:
        expire_delivery(db, session, message, delivery)
        if delivery.status == "expired":
            continue
        if delivery.status == "pending":
            delivery.status = "delivered"
            delivery.delivered_at = utcnow()
            delivery.delivered_by_session_id = session.id
            event(db, session, "agent.message.deliver", message, recipient_profile_id=session.skill_profile_id)
        received.append(message)
    await db.flush()
    return {"messages": [await message_view(db, m) for m in received]}


async def acknowledge(db, session, message_id):
    await receipt_scope_lock(db, session)
    row = (await db.execute(select(AgentMessageDelivery, AgentMessage).join(AgentMessage,
        AgentMessage.id == AgentMessageDelivery.message_id).where(AgentMessage.id == message_id,
        AgentMessageDelivery.project_id == session.project_id, AgentMessage.team_id == session.team_id,
        AgentMessageDelivery.recipient_profile_id == session.skill_profile_id)
        .with_for_update(of=AgentMessageDelivery))).first()
    if not row:
        raise MessageMissing("Message absent.")
    delivery, message = row
    expire_delivery(db, session, message, delivery)
    if delivery.status == "expired":
        # L'API conserve cette transition avant d'émettre le conflit.
        await db.flush()
        return message, "expired"
    if delivery.status == "pending":
        raise ValueError("Réception explicite requise avant l'accusé.")
    if delivery.status != "acknowledged":
        delivery.status = "acknowledged"
        delivery.acknowledged_at = utcnow()
        delivery.acknowledged_by_session_id = session.id
        event(db, session, "agent.message.ack", message, recipient_profile_id=session.skill_profile_id,
              kind=message.kind)
    await db.flush()
    return message, delivery.status


async def receipt_scope_lock(db, session):
    # Sérialiser les changements d'une boîte entre sessions du même profil,
    # y compris lorsqu'une échéance est franchie entre deux requêtes de polling.
    membership = await db.scalar(select(AgentTeamSkill.id).where(
        AgentTeamSkill.team_id == session.team_id, AgentTeamSkill.skill_profile_id == session.skill_profile_id)
        .with_for_update())
    if membership is None:
        raise MessageMissing("Profil absent de l'équipe.")
