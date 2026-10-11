"""Projection de lecture projet : aucun claim, receipt, accord ou effet Git."""
from collections import Counter, defaultdict
from datetime import timedelta
from decimal import Decimal

from sqlalchemy import func, select

from app.agent_autonomy import approval_required
from app.agent_coordinator import blockers
from app.agent_models import AgentActionApproval, AgentPlanVersion, AgentSession, AgentTask, FlowRun
from app.agent_sprint_service import next_action, reason_label
from app.business_models import AnalysisRun, Deliverable, DeliverableVersion, GenerationRun, SourceDocument
from app.conversation_models import AgentMessage, AgentMessageDelivery
from app.coffy_metrics import summarize as summarize_coffy
from app.coffy_models import (ScheduledLLMFeedback, ScheduledLLMOccurrence,
                              ScheduledLLMResultVersion, ScheduledLLMRun,
                              ScheduledLLMSubject, ScheduledLLMTaskVersion,
                              ScheduledLLMVersionSubject)
from app.git_models import CodeChange, CodeReview, GitOperation, GitWorktree
from app.git_adapter import minimize
from app.identity_models import utcnow
from app.incident_models import TechnicalIncident
from app.llm_trace import redact
from app.llm_models import LLMProfileVersion
from app.runtime_models import RuntimeQuota, RuntimeUsage
from app.runtime_service import totals

KINDS = {'': 'Toutes les activités', 'documents': 'Documents et traitements',
         'sessions': 'Sessions actives ou expirées', 'tasks': 'Tâches bloquées',
         'messages': 'Messages non acquittés', 'worktrees': 'Worktrees',
         'reviews': 'Revues de code courantes', 'quotas': 'Quotas cumulatifs',
         'alerts': 'Alertes opérationnelles', 'budgets':'Budgets périodiques',
         'budget_alerts':'Alertes budgétaires', 'coffy': 'Tâches LLM récurrentes'}
STATES = ('En attente', 'En cours', 'Décision humaine', 'Terminé', 'Échec')
PAGE_SIZE = 20


def safe_label(value):
    return minimize(redact(str(value)))[:240]


def card(kind, identity, title, state, href, created_at, *, owner='—', details=(), document_status=None):
    return dict(kind=kind, id=identity, title=safe_label(title), state=state, href=href,
                created_at=created_at, action_owner=owner, details=[safe_label(d) for d in details],
                document_status=document_status)


def task_blockers(task, tasks, plan, run_status, sessions, approvals, active_runtime_tasks, now):
    """Même porte que le coordinateur, sans appeler ses méthodes de mutation."""
    if task.status in {'completed', 'cancelled'} or any(t.parent_task_id == task.id for t in tasks):
        return []
    if task.status in {'claimed', 'running'}:
        reasons = []
        if plan.status != 'approved' or run_status != 'running': reasons.append('run_inactive')
        if not task.lease_expires_at or task.lease_expires_at <= now: reasons.append('lease_expired')
        session = sessions.get(task.claimed_session_id)
        if (not session or session.status != 'active' or session.expires_at <= now
                or getattr(session, 'max_expires_at', session.expires_at) <= now): reasons.append('session_expired')
        return reasons
    reasons = blockers(task, tasks, plan, run_status)
    if task.id in active_runtime_tasks: reasons.append('runtime_active')
    if approval_required(task, tasks, plan):
        approval = approvals.get((task.id, task.attempt_count + 1))
        if not approval or approval.status != 'approved':
            reasons.append('approval_pending' if approval else 'approval_required')
    return reasons


async def coffy_projection(db, project_id, now, filters):
    """Lecture des métadonnées CoFFY, limitée aux runs du projet et à leur cohorte."""
    period = filters['period']
    start = now - timedelta(days=int(period[:-1])) if period != 'all' else None
    query = (select(ScheduledLLMRun.id, ScheduledLLMRun.task_id, ScheduledLLMRun.status,
                    ScheduledLLMRun.error_code, ScheduledLLMRun.created_at,
                    ScheduledLLMRun.started_at, ScheduledLLMRun.completed_at,
                    ScheduledLLMTaskVersion.title, ScheduledLLMTaskVersion.result_domain,
                    LLMProfileVersion.provider, LLMProfileVersion.model_name,
                    ScheduledLLMResultVersion.status.label('result_status'))
             .join(ScheduledLLMTaskVersion,
                   ScheduledLLMTaskVersion.id == ScheduledLLMRun.task_version_id)
             .join(LLMProfileVersion,
                   LLMProfileVersion.id == ScheduledLLMRun.llm_profile_version_id)
             .outerjoin(ScheduledLLMResultVersion,
                 (ScheduledLLMResultVersion.run_id == ScheduledLLMRun.id) &
                 (ScheduledLLMResultVersion.version == ScheduledLLMRun.result_current_version))
             .where(ScheduledLLMRun.project_id == project_id,
                    ScheduledLLMRun.created_at <= now))
    if start is not None:
        query = query.where(ScheduledLLMRun.created_at >= start)
    if filters['status']:
        query = query.where(ScheduledLLMRun.status == filters['status'])
    if filters['provider']:
        query = query.where(LLMProfileVersion.provider == filters['provider'])
    if filters['profile']:
        query = query.where(ScheduledLLMRun.llm_profile_version_id == filters['profile'])
    if filters['subject']:
        query = query.where(select(ScheduledLLMVersionSubject.version_id).where(
            ScheduledLLMVersionSubject.version_id == ScheduledLLMRun.task_version_id,
            ScheduledLLMVersionSubject.subject_id == filters['subject']).exists())
    rows = [dict(row._mapping) for row in (await db.execute(query)).all()]
    for row in rows:
        row['provider'] = safe_label(row['provider'])
        row['model_name'] = safe_label(row['model_name'])
        if row['error_code']:
            row['error_code'] = safe_label(row['error_code'])
    cohort = query.with_only_columns(ScheduledLLMRun.id, maintain_column_froms=True).subquery()
    feedback = (await db.execute(select(ScheduledLLMResultVersion.run_id,
        ScheduledLLMResultVersion.version, ScheduledLLMFeedback.id,
        ScheduledLLMFeedback.author_id, ScheduledLLMFeedback.score,
        ScheduledLLMFeedback.created_at).join(ScheduledLLMResultVersion,
            ScheduledLLMResultVersion.id == ScheduledLLMFeedback.result_version_id)
        .join(cohort, cohort.c.id == ScheduledLLMResultVersion.run_id))).all() if rows else []
    metrics = summarize_coffy(rows, [dict(item._mapping) for item in feedback])
    skipped = []
    if not filters['status']:
        skipped_query = (select(ScheduledLLMOccurrence.coalesced_count,
                                ScheduledLLMOccurrence.count_truncated)
            .join(ScheduledLLMTaskVersion,
                  ScheduledLLMTaskVersion.id == ScheduledLLMOccurrence.task_version_id)
            .join(LLMProfileVersion,
                  LLMProfileVersion.id == ScheduledLLMTaskVersion.llm_profile_version_id)
            .where(ScheduledLLMOccurrence.project_id == project_id,
                   ScheduledLLMOccurrence.status == 'skipped',
                   ScheduledLLMOccurrence.created_at <= now))
        if start is not None:
            skipped_query = skipped_query.where(ScheduledLLMOccurrence.created_at >= start)
        if filters['provider']:
            skipped_query = skipped_query.where(LLMProfileVersion.provider == filters['provider'])
        if filters['profile']:
            skipped_query = skipped_query.where(
                ScheduledLLMTaskVersion.llm_profile_version_id == filters['profile'])
        if filters['subject']:
            skipped_query = skipped_query.where(select(ScheduledLLMVersionSubject.version_id).where(
                ScheduledLLMVersionSubject.version_id == ScheduledLLMOccurrence.task_version_id,
                ScheduledLLMVersionSubject.subject_id == filters['subject']).exists())
        skipped = (await db.execute(skipped_query)).all()
    metrics['skipped'] = {'occurrences': len(skipped),
                          'coalesced_due': sum(row.coalesced_count for row in skipped),
                          'count_truncated': any(row.count_truncated for row in skipped)}
    profiles = (await db.execute(select(LLMProfileVersion.id, LLMProfileVersion.provider,
        LLMProfileVersion.model_name, LLMProfileVersion.version).join(ScheduledLLMRun,
            ScheduledLLMRun.llm_profile_version_id == LLMProfileVersion.id)
        .where(ScheduledLLMRun.project_id == project_id).distinct()
        .order_by(LLMProfileVersion.provider, LLMProfileVersion.model_name,
                  LLMProfileVersion.id))).all()
    subjects = (await db.execute(select(ScheduledLLMSubject.id, ScheduledLLMSubject.title)
        .where(ScheduledLLMSubject.project_id == project_id)
        .order_by(ScheduledLLMSubject.title, ScheduledLLMSubject.id))).all()
    activities = []
    role_label = {'business': 'Product Owner', 'technical': 'Architecte Applicatif',
                  'pilotage': 'Chef de Projet'}
    for row in rows:
        if row['status'] in {'failed', 'timeout'}:
            state, owner = 'Échec', 'Chef de Projet : examiner'
        elif row['status'] == 'cancelled':
            state, owner = 'Terminé', '—'
        elif row['status'] in {'pending', 'queued'}:
            state, owner = 'En attente', 'Ordonnanceur ou worker'
        elif row['status'] in {'running', 'retrying'}:
            state, owner = 'En cours', 'Worker LLM'
        elif row['result_status'] in {'Brouillon IA', 'À revoir'}:
            state = 'Décision humaine'
            owner = ('Membre habilité : soumettre' if row['result_status'] == 'Brouillon IA'
                     else role_label[row['result_domain']] + ' : décider')
        else:
            state, owner = 'Terminé', '—'
        details = [f"Run {row['status']}", f"{row['provider']} · {row['model_name']}"]
        if row['error_code']:
            details.append('Erreur : ' + row['error_code'])
        if row['started_at'] and row['completed_at'] and row['completed_at'] >= row['started_at']:
            details.append(f"Durée : {(row['completed_at'] - row['started_at']).total_seconds():.1f} s")
        activities.append(card('coffy', row['id'], row['title'], state,
            f'/projects/{project_id}/scheduled-tasks/{row["task_id"]}/runs/{row["id"]}',
            row['created_at'], owner=owner, details=details,
            document_status=row['result_status']))
    return {**metrics, 'period': period, 'period_start': start, 'period_end': now,
        'filters': filters,
        'profiles': [{'id': row.id, 'label': safe_label(f'{row.provider} · {row.model_name} · v{row.version}')}
                     for row in profiles],
        'subjects': [{'id': row.id, 'title': safe_label(row.title)} for row in subjects]}, activities


async def projection(db, project_id, *, agent_read, coffy_read=False, coffy_filters=None,
                     kind='', state='', offset=0):
    now = utcnow()
    base = f'/projects/{project_id}'
    api = f'/api/projects/{project_id}'
    cards = []
    # Sélection explicite : contenu, prompt, manifeste et credential restent hors projection.
    sources = (await db.execute(select(SourceDocument.id, SourceDocument.name, SourceDocument.status,
        SourceDocument.created_at).where(SourceDocument.project_id == project_id))).all()
    for row in sources:
        status = {'uploaded': 'En attente', 'extracting': 'En cours', 'error': 'Échec', 'extracted':'Terminé'}.get(row.status, 'En attente')
        cards.append(card('documents', row.id, row.name, status, f'{base}/sources/{row.id}', row.created_at,
                          details=['Import documentaire', row.status]))
    generations = (await db.execute(select(GenerationRun.id, GenerationRun.status, GenerationRun.created_at, GenerationRun.error_code,
        Deliverable.title).join(Deliverable, (Deliverable.id == GenerationRun.deliverable_id) &
        (Deliverable.project_id == project_id)).where(GenerationRun.project_id == project_id))).all()
    for row in generations:
        status = {'queued': 'En attente', 'running': 'En cours', 'failed': 'Échec', 'completed':'Terminé'}.get(row.status, 'En attente')
        cards.append(card('documents', row.id, row.title, status, f'{base}/generations', row.created_at,
                          details=['Génération', row.status]))
    analyses = (await db.execute(select(AnalysisRun.id, AnalysisRun.source_id.label('source_document_id'), AnalysisRun.status,
        AnalysisRun.created_at, AnalysisRun.error_code).where(AnalysisRun.project_id == project_id))).all()
    for row in analyses:
        status = {'queued':'En attente', 'running':'En cours', 'failed':'Échec', 'completed':'Terminé'}.get(row.status, 'En attente')
        cards.append(card('documents', row.id, 'Analyse documentaire', status,
            f'{base}/sources/{row.source_document_id}', row.created_at, details=[row.status]))
    versions = (await db.execute(select(DeliverableVersion.id, DeliverableVersion.title, DeliverableVersion.status,
        DeliverableVersion.created_at).join(Deliverable, (Deliverable.id == DeliverableVersion.deliverable_id) &
        (Deliverable.current_version == DeliverableVersion.version)).where(Deliverable.project_id == project_id))).all()
    for row in versions:
        status = 'Décision humaine' if row.status in {'Brouillon IA', 'À revoir'} else 'Terminé'
        cards.append(card('documents', row.id, row.title, status, f'{base}/reviews/{row.id}', row.created_at,
                          owner='Rôle de validation du livrable', document_status=row.status))
    counters = dict(active_agents=None, active_sessions=None, expired_sessions=None, blocked_tasks=None,
                    unread_messages=None, unacknowledged_deliveries=None, worktrees=None, reviews=None,
                    quotas=None, alerts=None, budgets=None, budget_alerts=None, budget_suspensions=None)
    consumption = None
    budget_data = None
    coffy = None
    if coffy_read:
        coffy, coffy_cards = await coffy_projection(db, project_id, now, coffy_filters or
            {'period': '30d', 'status': '', 'provider': '', 'profile': None, 'subject': None})
        cards.extend(coffy_cards)
    if agent_read:
        sessions = (await db.execute(select(AgentSession.id, AgentSession.team_id, AgentSession.skill_profile_id,
            AgentSession.client_code, AgentSession.skill_code, AgentSession.status, AgentSession.expires_at, AgentSession.max_expires_at,
            AgentSession.created_at).where(AgentSession.project_id == project_id))).all()
        active = [s for s in sessions if s.status == 'active' and min(s.expires_at, s.max_expires_at) > now]
        expired = [s for s in sessions if s.status == 'expired' or (s.status == 'active' and min(s.expires_at, s.max_expires_at) <= now)]
        counters.update(active_agents=len({(s.team_id, s.skill_profile_id) for s in active}),
                        active_sessions=len(active), expired_sessions=len(expired))
        for row in active + expired:
            expired_now = row.status == 'expired' or min(row.expires_at, row.max_expires_at) <= now
            cards.append(card('sessions', row.id, f'{row.client_code} · {row.skill_code}',
                'En attente' if expired_now else 'En cours', f'{api}/agent-sessions', row.created_at,
                details=['Session expirée' if expired_now else 'Session active', row.skill_code]))
        tasks = list((await db.scalars(select(AgentTask).where(AgentTask.project_id == project_id))).all())
        plans = {p.id: p for p in (await db.scalars(select(AgentPlanVersion).where(AgentPlanVersion.project_id == project_id))).all()}
        runs = {r.id: r.status for r in (await db.scalars(select(FlowRun).where(FlowRun.project_id == project_id))).all()}
        by_plan = defaultdict(list)
        for task in tasks: by_plan[task.plan_version_id].append(task)
        approvals = {(a.task_id, a.attempt_number): a for a in (await db.scalars(select(AgentActionApproval)
            .join(AgentTask, AgentTask.id == AgentActionApproval.task_id).where(AgentTask.project_id == project_id,
                AgentActionApproval.status.in_(['pending', 'approved'])))).all()}
        usages = (await db.execute(select(RuntimeUsage.id, RuntimeUsage.status, RuntimeUsage.context,
            RuntimeUsage.started_at, RuntimeUsage.charged_seconds, RuntimeUsage.error_code)
            .where(RuntimeUsage.project_id == project_id))).all()
        active_runtime_tasks = {u.context.get('task') for u in usages if u.status == 'active'}
        sessions_by_id = {s.id: s for s in sessions}
        from app.budget_service import snapshot
        budget_data = await snapshot(db, project_id)
        budget_data.pop('history', None)
        budget_data['budgets'] = [{k:v for k,v in b.items() if k not in ('reason','created_by')}
                                  for b in budget_data['budgets']]
        suspended_plans = {s['plan_id'] for s in budget_data['suspensions'] if s['resumed_at'] is None}
        counters.update(budgets=sum(b['enabled'] for b in budget_data['budgets']),
            budget_alerts=budget_data['unacknowledged'], budget_suspensions=budget_data['suspended'])
        counters['blocked_tasks'] = 0
        for task in tasks:
            plan = plans[task.plan_version_id]
            reasons = task_blockers(task, by_plan[plan.id], plan, runs.get(plan.validated_flow_run_id, 'unavailable'),
                sessions_by_id, approvals, active_runtime_tasks, now)
            if (plan.id in suspended_plans and task.status not in ('completed','cancelled')
                    and task.id not in active_runtime_tasks
                    and not any(t.parent_task_id == task.id for t in by_plan[plan.id])):
                reasons.append('budget_suspended')
            if not reasons: continue
            counters['blocked_tasks'] += 1
            owner, action = next_action(reasons, 'blocked')
            if 'runtime_active' in reasons: owner, action = 'human', 'Chef de Projet : examiner l’appel runtime actif'
            if 'budget_suspended' in reasons: owner, action = 'human', 'Chef de Projet : examiner le budget et décider la reprise'
            labels = ['Suspension budgétaire' if r == 'budget_suspended' else reason_label(r) if r != 'runtime_active' else 'Appel runtime actif : réconciliation requise après arrêt' for r in reasons]
            status = 'Décision humaine' if owner == 'human' else 'En attente'
            href = f'{base}/agents/runtimes#budget-alerts' if 'budget_suspended' in reasons else f'{api}/agent-tasks?plan_id={plan.id}'
            cards.append(card('tasks', task.id, task.title, status, href,
                task.created_at, owner=action, details=labels))
        deliveries = (await db.execute(select(AgentMessageDelivery.id, AgentMessageDelivery.status,
            AgentMessageDelivery.recipient_profile_id, AgentMessage.id.label('message_id'), AgentMessage.channel_id,
            AgentMessage.created_at).join(AgentMessage, (AgentMessage.id == AgentMessageDelivery.message_id) &
            (AgentMessage.project_id == project_id)).where(AgentMessageDelivery.project_id == project_id,
                AgentMessageDelivery.status.in_(['pending', 'delivered']), AgentMessage.expires_at > now))).all()
        counters.update(unread_messages=len({r.message_id for r in deliveries}), unacknowledged_deliveries=len(deliveries))
        for row in deliveries:
            cards.append(card('messages', row.id, f'Message {row.message_id}', 'En attente',
                f'{base}/agents/coordination?selected={row.channel_id}', row.created_at,
                owner=f'Profil {row.recipient_profile_id}', document_status='Brouillon IA',
                details=['Non livré' if row.status == 'pending' else 'Livré, non acquitté']))
        trees = (await db.execute(select(GitWorktree.id, GitWorktree.branch, GitWorktree.status,
            GitWorktree.task_id, GitWorktree.created_at).where(GitWorktree.project_id == project_id,
                GitWorktree.status != 'removed'))).all()
        counters['worktrees'] = len(trees)
        for row in trees:
            cards.append(card('worktrees', row.id, row.branch, 'Échec' if row.status in {'failed', 'uncertain'} else 'En cours',
                f'{base}/agents/code?selected={row.id}', row.created_at, details=[row.status, f'Tâche {row.task_id}']))
        latest = select(CodeChange.worktree_id, func.max(CodeChange.revision).label('revision')).where(
            CodeChange.project_id == project_id).group_by(CodeChange.worktree_id).subquery()
        reviews = (await db.execute(select(CodeReview.id, CodeReview.status, CodeReview.created_at, CodeReview.session_id,
            CodeChange.worktree_id).join(CodeChange, (CodeChange.id == CodeReview.change_id) &
            (CodeChange.project_id == project_id)).join(latest, (latest.c.worktree_id == CodeChange.worktree_id) &
            (latest.c.revision == CodeChange.revision)).join(GitWorktree, (GitWorktree.id == CodeChange.worktree_id) &
            (GitWorktree.project_id == project_id)).where(CodeReview.project_id == project_id,
            CodeReview.status != 'reviewed', GitWorktree.status != 'removed'))).all()
        counters['reviews'] = len(reviews)
        for row in reviews:
            cards.append(card('reviews', row.id, f'Revue {row.id}', 'En cours' if row.session_id else 'Décision humaine',
                f'{base}/agents/code?selected={row.worktree_id}', row.created_at,
                owner='Session reviewer' if row.session_id else 'Reviewer humain désigné',
                details=[row.status, 'Avis IA consultatif' if row.session_id else 'Revue humaine']))
        quotas = (await db.scalars(select(RuntimeQuota).where(RuntimeQuota.project_id == project_id,
            RuntimeQuota.is_current.is_(True)))).all()
        quotas = [q for q in quotas if any(getattr(q, k) is not None for k in ('max_tokens','max_seconds','max_concurrency','max_cost'))]
        counters['quotas'] = len(quotas)
        for row in quotas:
            limits = [f'{label} : {getattr(row, field)}' for field, label in
                (('max_tokens','Tokens'),('max_seconds','Secondes'),('max_concurrency','Concurrence'),('max_cost','Coût estimé'))
                if getattr(row, field) is not None]
            if row.currency: limits.append(row.currency)
            cards.append(card('quotas', row.id, f'Quota {row.scope} · v{row.version}', 'En cours',
                f'{base}/agents/runtimes?scope={row.scope}&scope_id={row.scope_id}', row.created_at, details=limits))
        incidents = (await db.execute(select(TechnicalIncident.id, TechnicalIncident.priority, TechnicalIncident.status,
            TechnicalIncident.created_at, TechnicalIncident.content_status).where(TechnicalIncident.project_id == project_id,
                TechnicalIncident.status != 'resolved'))).all()
        for row in incidents:
            cards.append(card('alerts', row.id, f'Incident {row.priority} · {row.id}', 'En attente',
                f'{base}/agents/incidents?selected={row.id}', row.created_at, details=[row.status], document_status=row.content_status))
        uncertain = (await db.execute(select(GitOperation.id, GitOperation.worktree_id, GitOperation.kind,
            GitOperation.created_at).where(GitOperation.project_id == project_id, GitOperation.status == 'uncertain'))).all()
        for row in uncertain:
            cards.append(card('alerts', row.id, f'Opération Git incertaine · {row.kind}', 'Décision humaine',
                f'{base}/agents/code?selected={row.worktree_id}', row.created_at, owner='Chef de Projet : réconcilier'))
        for row in usages:
            overdue = row.status == 'active' and Decimal(str((now - row.started_at).total_seconds())) > row.charged_seconds
            if row.status != 'unknown' and not overdue and not (row.error_code or '').startswith(('quota_', 'pricing_', 'budget_')): continue
            cards.append(card('alerts', row.id, 'Appel runtime à examiner', 'Décision humaine',
                f'{base}/agents/runtimes', row.started_at, owner='Chef de Projet',
                details=['Durée réservée dépassée' if overdue else row.status, 'Consulter le registre des appels']))
        # Une admission refusée avant fournisseur ne crée pas forcément de runtime_usage.
        failures = [(r, f'{base}/generations') for r in generations]
        failures.extend((r, f'{base}/sources/{r.source_document_id}') for r in analyses)
        for row, href in failures:
            if not (row.error_code or '').startswith(('quota_', 'pricing_', 'budget_')): continue
            cards.append(card('alerts', row.id, 'Traitement bloqué par le quota, le budget ou le tarif', 'Décision humaine',
                href, row.created_at, owner='Chef de Projet : examiner les quotas', details=[row.error_code]))
        for b in budget_data['budgets']:
            if not b['enabled']: continue
            cards.append(card('budgets',b['id'],f"Budget {b['scope']} · v{b['version']}",
                'Décision humaine' if b['level'] in ('exhausted','unavailable') else 'En cours',
                f'{base}/agents/runtimes#budgets',b['created_at'],owner='Chef de Projet',
                details=[f"{b['period']} UTC · {b['metric']} · plafond {b['amount']} {b['currency'] or ''}",
                         f"Charge : {b['charged'] if b['charged'] is not None else 'Non disponible'}"]))
        for a in budget_data['alerts']:
            if a['acknowledged_at'] is not None: continue
            cards.append(card('budget_alerts',a['id'],f"Alerte budgétaire · {a['level']}",'Décision humaine',
                f'{base}/agents/runtimes#budget-alerts',a['created_at'],owner='Chef de Projet',
                details=[f"Budget {a['budget_id']} · période {a['period_start'].isoformat()}"]))
        counters['alerts'] = sum(c['kind'] == 'alerts' for c in cards)
        consumption = await totals(db, project_id, 'project', project_id)
    counts = Counter(c['state'] for c in cards)
    filtered = [c for c in cards if (not kind or c['kind'] == kind) and (not state or c['state'] == state)]
    filtered.sort(key=lambda c: (c['created_at'], c['kind'], c['id']), reverse=True)
    return dict(project_id=project_id, as_of=now, counts={s: counts[s] for s in STATES}, counters=counters, consumption=consumption,
        agent_read=agent_read, activities=filtered[offset:offset+PAGE_SIZE], total=len(filtered), offset=offset,
        next_offset=offset+PAGE_SIZE if len(filtered) > offset+PAGE_SIZE else None,
        budget_data=budget_data, coffy=coffy, coffy_read=coffy_read,
        availability={'budgets': 'available' if agent_read else 'forbidden',
            'budget_alerts': 'available' if agent_read else 'forbidden',
            'coffy': 'available' if coffy_read else 'forbidden'})
