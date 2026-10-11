"""Cycle d'incident transactionnel, sans commande d'exécution implicite."""
import hashlib
import json
from datetime import timedelta
from types import SimpleNamespace

from sqlalchemy import select, func

from app.agent_models import AgentTask, AgentTeam, AgentTeamSkill, SkillProfile
from app.identity_models import Project, ProjectMember, User, utcnow
from app.identity_service import audit
from app.incident_models import IncidentEvent, IncidentNotification, TechnicalIncident
from app.llm_trace import redact
from app.memory_service import check_secrets, check_sources


class IncidentMissing(ValueError):
    pass


def fingerprint(values):
    return hashlib.sha256(json.dumps(values, sort_keys=True, ensure_ascii=False).encode()).hexdigest()


def safe_text(*values, sensitive_token=None):
    check_secrets({'content': '\n'.join(values)})
    if any(redact(value) != value or (sensitive_token and sensitive_token in value) for value in values):
        raise ValueError('Contenu refusé : secret reconnaissable.')


async def responsible(db, project_id, team_id, profile_id):
    found = await db.scalar(select(SkillProfile.id).join(AgentTeamSkill, AgentTeamSkill.skill_profile_id == SkillProfile.id)
        .join(AgentTeam, AgentTeam.id == AgentTeamSkill.team_id).where(AgentTeam.id == team_id,
        AgentTeam.project_id == project_id, AgentTeam.is_active.is_(True), SkillProfile.id == profile_id,
        SkillProfile.is_active.is_(True)))
    if not found:
        raise ValueError('Compétence active de la même équipe requise.')


def snapshot(row):
    return {k: getattr(row, k) for k in ('title', 'description', 'content_status', 'priority', 'status', 'responsible_profile_id')}


async def append_event(db, row, action, actor_id, values, *, session_id=None):
    event = IncidentEvent(incident_id=row.id, project_id=row.project_id, revision=row.revision,
        action=action, actor_id=actor_id, session_id=session_id, fingerprint=fingerprint(values),
        reason=values.get('reason', ''), source_refs=values.get('source_refs', []), snapshot=snapshot(row))
    db.add(event)
    await db.flush()
    # Destinataires figés : profil responsable et CP actifs du projet, aucun envoi externe.
    users = list(await db.scalars(select(ProjectMember.user_id).join(User, User.id == ProjectMember.user_id)
        .where(ProjectMember.project_id == row.project_id, ProjectMember.role_code == 'project_manager',
               ProjectMember.is_active.is_(True), User.is_active.is_(True))))
    expires = utcnow() + timedelta(hours=24)
    for user_id in users:
        db.add(IncidentNotification(incident_id=row.id, project_id=row.project_id, event_id=event.id,
            recipient_user_id=user_id, priority=row.priority, expires_at=expires))
    db.add(IncidentNotification(incident_id=row.id, project_id=row.project_id, event_id=event.id,
        recipient_profile_id=row.responsible_profile_id, priority=row.priority, expires_at=expires))
    audit(db, 'incident.' + action, 'success', actor_id=actor_id, project_id=row.project_id,
        target_type='technical_incident', target_id=row.id, details={'revision': row.revision,
        'event_id': event.id, 'status': row.status, 'priority': row.priority,
        'responsible_profile_id': row.responsible_profile_id, 'session_id': session_id})
    await db.flush()


async def create_incident(db, project_id, values, actor_id, *, session=None, sensitive_token=None):
    # Sérialise aussi les clés humaines et les mutations avec l'archivage du projet.
    project = await db.scalar(select(Project).where(Project.id == project_id).with_for_update()
                              .execution_options(populate_existing=True))
    if not project or project.status == 'archived':
        raise ValueError('Projet absent ou archivé.')
    replay_lock = int(fingerprint({'actor': actor_id, 'key': values['request_key']})[:15], 16)
    await db.execute(select(func.pg_advisory_xact_lock(replay_lock)))
    key = fingerprint({**values, 'session_id': session.id if session else None})
    previous = await db.scalar(select(TechnicalIncident).where(TechnicalIncident.created_by == actor_id,
        TechnicalIncident.request_key == values['request_key']))
    if previous:
        if previous.project_id != project_id or previous.request_fingerprint != key:
            raise ValueError('Clé de signalement déjà utilisée avec une autre charge.')
        return previous, False
    await responsible(db, project_id, values['team_id'], values['responsible_profile_id'])
    if session and values['team_id'] != session.team_id:
        raise ValueError('Équipe incompatible avec la session.')
    if values.get('task_id'):
        task = await db.scalar(select(AgentTask).where(AgentTask.id == values['task_id'],
            AgentTask.project_id == project_id, AgentTask.team_id == values['team_id']))
        if not task or (session and task.skill_profile_id != session.skill_profile_id):
            raise IncidentMissing('Tâche absente ou incompatible.')
    safe_text(values['title'], values['description'], sensitive_token=sensitive_token)
    if session and session.expires_at <= utcnow():
        raise ValueError('Session expirée.')
    row = TechnicalIncident(project_id=project_id, created_by=actor_id, request_fingerprint=key,
        created_by_session_id=session.id if session else None,
        content_status='Brouillon IA' if session else 'À revoir', **values)
    db.add(row)
    await db.flush()
    await append_event(db, row, 'report', actor_id, values, session_id=session.id if session else None)
    return row, True


async def change_incident(db, project_id, incident_id, action, values, actor_id):
    row = await db.scalar(select(TechnicalIncident).where(TechnicalIncident.id == incident_id,
        TechnicalIncident.project_id == project_id).with_for_update().execution_options(populate_existing=True))
    if not row:
        raise IncidentMissing('Incident introuvable.')
    latest = await db.scalar(select(IncidentEvent).where(IncidentEvent.incident_id == row.id,
                                                       IncidentEvent.revision == row.revision))
    if (latest.action == action and latest.actor_id == actor_id and latest.fingerprint == fingerprint(values)
            and values['expected_revision'] == row.revision - 1):
        return row
    if row.revision != values['expected_revision']:
        raise ValueError('Révision périmée : rechargez l’incident.')
    if row.status == 'resolved':
        raise ValueError('Incident déjà résolu.')
    reason = values.get('reason', '')
    safe_text(reason, values.get('title', ''), values.get('description', ''))
    if action != 'acknowledge' and not reason:
        raise ValueError('Justification requise.')
    if action == 'acknowledge':
        if row.status != 'open': raise ValueError('Incident déjà pris en charge.')
        row.status = 'acknowledged'
    elif action in ('assign', 'escalate'):
        profile_id = values['responsible_profile_id']
        await responsible(db, project_id, row.team_id, profile_id)
        if profile_id == row.responsible_profile_id: raise ValueError('Choisissez une autre compétence.')
        row.responsible_profile_id = profile_id
        if action == 'escalate': row.status = 'escalated'
    elif action == 'priority':
        if row.priority == values['priority']: raise ValueError('Priorité inchangée.')
        row.priority = values['priority']
    elif action == 'edit':
        row.title, row.description, row.content_status = values['title'], values['description'], 'À revoir'
    elif action == 'resolve':
        await check_sources(db, SimpleNamespace(project_id=project_id, scope='project'),
                            {'source_refs': values.get('source_refs', [])})
        row.status, row.resolved_at = 'resolved', utcnow()
    else:
        raise ValueError('Action inconnue.')
    row.revision += 1
    await append_event(db, row, action, actor_id, values)
    return row


async def incident_view(db, row, *, with_history=False):
    profile = await db.get(SkillProfile, row.responsible_profile_id)
    result = {k: getattr(row, k) for k in ('id', 'project_id', 'team_id', 'task_id', 'responsible_profile_id',
        'title', 'description', 'priority', 'status', 'revision', 'content_status', 'created_by',
        'created_by_session_id', 'created_at', 'resolved_at')}
    result['usage'] = 'untrusted_incident_data'
    result['responsible_name'] = f'{profile.label} v{profile.version_number}'
    if with_history:
        events = (await db.scalars(select(IncidentEvent).where(IncidentEvent.incident_id == row.id)
            .order_by(IncidentEvent.revision))).all()
        result['events'] = [{k: getattr(e, k) for k in ('id', 'revision', 'action', 'actor_id', 'session_id',
            'reason', 'source_refs', 'snapshot', 'created_at')} for e in events]
    return result


async def incident_list(db, project_id, *, status=None, priority=None, profile_id=None, limit=50, offset=0):
    query = select(TechnicalIncident).where(TechnicalIncident.project_id == project_id)
    if status: query = query.where(TechnicalIncident.status == status)
    if priority: query = query.where(TechnicalIncident.priority == priority)
    if profile_id: query = query.where(TechnicalIncident.responsible_profile_id == profile_id)
    rows = (await db.scalars(query.order_by(TechnicalIncident.priority, TechnicalIncident.created_at.desc(),
        TechnicalIncident.id).offset(offset).limit(limit + 1))).all()
    return {'incidents': [await incident_view(db, row) for row in rows[:limit]],
            'next_offset': offset + limit if len(rows) > limit else None}


def notification_view(row):
    return {**{k: getattr(row, k) for k in ('id', 'incident_id', 'project_id', 'event_id', 'priority',
        'recipient_user_id', 'recipient_profile_id', 'created_at', 'expires_at', 'acknowledged_at',
        'acknowledged_by', 'acknowledged_by_session_id')},
        'status': 'expired' if row.status == 'pending' and row.expires_at <= utcnow() else row.status}


def notification_scope(project_id, user_id=None, session=None):
    return (IncidentNotification.project_id == project_id,
            IncidentNotification.recipient_profile_id == session.skill_profile_id if session else
            IncidentNotification.recipient_user_id == user_id)


def expire_notification(db, row, *, actor_id=None, session_id=None):
    if row.status == 'pending' and row.expires_at <= utcnow():
        row.status = 'expired'
        audit(db, 'incident.notification.expire', 'success', actor_id=actor_id, project_id=row.project_id,
            target_type='incident_notification', target_id=row.id, details={'incident_id': row.incident_id,
            'session_id': session_id})


async def notifications(db, project_id, *, user_id=None, session=None, poll=False, limit=50, offset=0):
    scope = notification_scope(project_id, user_id, session)
    if session:
        scope = (*scope, IncidentNotification.incident_id.in_(select(TechnicalIncident.id).where(TechnicalIncident.team_id == session.team_id)))
    if poll:
        expired = (await db.scalars(select(IncidentNotification).where(*scope,
            IncidentNotification.status == 'pending', IncidentNotification.expires_at <= utcnow())
            .order_by(IncidentNotification.id).limit(100).with_for_update())).all()
        for row in expired: expire_notification(db, row, actor_id=user_id, session_id=session.id if session else None)
        await db.flush()
    query = select(IncidentNotification).where(*scope)
    if session:
        query = query.join(TechnicalIncident, TechnicalIncident.id == IncidentNotification.incident_id).where(
            TechnicalIncident.team_id == session.team_id)
    if poll: query = query.where(IncidentNotification.status == 'pending', IncidentNotification.expires_at > utcnow())
    rows = (await db.scalars(query.order_by(IncidentNotification.priority, IncidentNotification.created_at,
        IncidentNotification.id).offset(offset).limit(limit + 1))).all()
    return {'notifications': [notification_view(row) for row in rows[:limit]],
            'next_offset': offset + limit if len(rows) > limit else None}


async def ack_notification(db, project_id, notification_id, *, user_id=None, session=None):
    query = select(IncidentNotification).where(*notification_scope(project_id, user_id, session),
        IncidentNotification.id == notification_id)
    if session:
        query = query.join(TechnicalIncident, TechnicalIncident.id == IncidentNotification.incident_id).where(
            TechnicalIncident.team_id == session.team_id)
    row = await db.scalar(query.with_for_update(of=IncidentNotification).execution_options(populate_existing=True))
    if not row: raise IncidentMissing('Notification introuvable.')
    expire_notification(db, row, actor_id=user_id, session_id=session.id if session else None)
    if row.status == 'pending':
        row.status, row.acknowledged_at = 'acknowledged', utcnow()
        row.acknowledged_by, row.acknowledged_by_session_id = user_id, session.id if session else None
        audit(db, 'incident.notification.ack', 'success', actor_id=user_id, project_id=project_id,
            target_type='incident_notification', target_id=row.id, details={'incident_id': row.incident_id,
            'session_id': session.id if session else None})
    await db.flush()
    return notification_view(row)
