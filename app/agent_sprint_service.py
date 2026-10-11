"""Planification versionnée et projection de sprint sans transition de tâche."""
from collections import Counter, defaultdict
from datetime import timedelta

from sqlalchemy import select

from app.agent_autonomy import active_approval, approval_required
from app.agent_coordinator import blockers, plan_tasks
from app.agent_models import (AgentPlanVersion, AgentSession, AgentSprint, AgentSprintRevision,
                              AgentSprintTask, AgentTaskRun, FlowRun, SkillProfile)
from app.agent_progress import task_view
from app.identity_models import utcnow
from app.identity_service import audit


async def sprint_items(db, sprint_id):
    return list((await db.scalars(select(AgentSprintTask).where(
        AgentSprintTask.sprint_id == sprint_id).order_by(AgentSprintTask.task_id))).all())


def sprint_view(sprint):
    return {key: getattr(sprint, key) for key in ("id", "project_id", "plan_version_id", "name", "goal",
        "starts_at", "ends_at", "status", "revision", "created_by", "created_at", "activated_at", "closed_at")}


async def record_revision(db, sprint, actor_id, action):
    snapshot = {k: v.isoformat() if hasattr(v, "isoformat") else v for k,v in sprint_view(sprint).items()}
    items = await sprint_items(db, sprint.id)
    snapshot["items"] = [{"task_id": i.task_id, "target_due_at": i.target_due_at.isoformat() if i.target_due_at else None,
                          "added_at": i.added_at.isoformat()} for i in items]
    db.add(AgentSprintRevision(sprint_id=sprint.id, revision=sprint.revision, snapshot=snapshot,
                              action=action, actor_id=actor_id))
    audit(db, f"agent.sprint.{action}", "success", actor_id=actor_id, project_id=sprint.project_id,
        target_type="agent_sprint", target_id=sprint.id,
        details={"revision": sprint.revision, "plan_id": sprint.plan_version_id, "task_count": len(items),
                 "status": sprint.status})
    await db.flush()


async def lock_sprint(db, project_id, sprint_id):
    plan_id = await db.scalar(select(AgentSprint.plan_version_id).where(
        AgentSprint.id == sprint_id, AgentSprint.project_id == project_id))
    plan = await db.scalar(select(AgentPlanVersion).where(AgentPlanVersion.id == plan_id,
        AgentPlanVersion.project_id == project_id).with_for_update(key_share=True).execution_options(populate_existing=True))
    if not plan:
        raise ValueError("Sprint introuvable.")
    sprint = await db.scalar(select(AgentSprint).where(AgentSprint.id == sprint_id)
        .with_for_update().execution_options(populate_existing=True))
    return sprint, plan


async def validate_selection(db, plan, starts_at, ends_at, items, *, sprint_id=None, retained=frozenset()):
    if not timedelta(0) < ends_at - starts_at <= timedelta(days=90) or ends_at <= utcnow():
        raise ValueError("Période invalide : durée positive, au plus 90 jours, fin future.")
    ids = [str(i['task_id']) for i in items]
    if len(ids) > 20 or len(ids) != len(set(ids)):
        raise ValueError("Sélection limitée à 20 feuilles distinctes.")
    tasks = await plan_tasks(db, plan.id)
    by_id = {t.id: t for t in tasks}
    parents = {t.parent_task_id for t in tasks if t.parent_task_id}
    for item, tid in zip(items, ids):
        if tid not in by_id or tid in parents:
            raise ValueError("Chaque sélection doit être une feuille du plan.")
        if by_id[tid].status in {'completed','cancelled'} and tid not in retained:
            raise ValueError("Une nouvelle sélection ne peut inclure une tâche terminale.")
        due = item.get('target_due_at')
        if due and not starts_at <= due <= ends_at:
            raise ValueError("La date cible doit appartenir à la période du sprint.")
    others = (await db.scalars(select(AgentSprint).join(AgentSprintTask,
        AgentSprintTask.sprint_id == AgentSprint.id).where(AgentSprintTask.task_id.in_(ids),
        AgentSprint.id != sprint_id if sprint_id else True))).unique().all()
    for other in others:
        end = min(other.ends_at, other.closed_at) if other.closed_at else other.ends_at
        if end > other.starts_at and starts_at < end and ends_at > other.starts_at:
            raise ValueError("Une feuille est déjà planifiée dans une période qui se chevauche.")


async def save_items(db, sprint, items):
    existing = {i.task_id: i for i in await sprint_items(db, sprint.id)}
    selected = {str(i['task_id']) for i in items}
    for tid, row in existing.items():
        if tid not in selected:
            await db.delete(row)
    for item in items:
        tid = str(item['task_id'])
        row = existing.get(tid)
        if row is None:
            row = AgentSprintTask(sprint_id=sprint.id, task_id=tid,
                plan_version_id=sprint.plan_version_id, project_id=sprint.project_id)
            db.add(row)
        row.target_due_at = item.get('target_due_at')
    await db.flush()


async def create_sprint(db, project_id, plan_id, values, actor_id):
    plan = await db.scalar(select(AgentPlanVersion).where(AgentPlanVersion.id == plan_id,
        AgentPlanVersion.project_id == project_id).with_for_update(key_share=True))
    if not plan or plan.status != 'approved' or not plan.approved_by or not plan.approved_at:
        raise ValueError("Plan approuvé requis.")
    run = await db.get(FlowRun, plan.validated_flow_run_id)
    if not run or run.status not in {'running','paused'}:
        raise ValueError("Run actif ou suspendu requis pour planifier.")
    await validate_selection(db, plan, values['starts_at'], values['ends_at'], values['items'])
    sprint = AgentSprint(project_id=project_id, plan_version_id=plan.id, name=values['name'], goal=values['goal'],
        starts_at=values['starts_at'], ends_at=values['ends_at'], status='draft', revision=1, created_by=actor_id)
    db.add(sprint); await db.flush()
    await save_items(db, sprint, values['items'])
    await record_revision(db, sprint, actor_id, 'create')
    return sprint


async def revise_sprint(db, project_id, sprint_id, expected_revision, values, actor_id):
    sprint, plan = await lock_sprint(db, project_id, sprint_id)
    if sprint.status != 'draft' or sprint.revision != expected_revision:
        raise ValueError("Seul le brouillon à la révision attendue est modifiable.")
    retained = {i.task_id for i in await sprint_items(db,sprint.id)}
    await validate_selection(db, plan, values['starts_at'], values['ends_at'], values['items'],
                             sprint_id=sprint.id, retained=retained)
    for key in ('name','goal','starts_at','ends_at'):
        setattr(sprint, key, values[key])
    sprint.revision += 1
    await save_items(db, sprint, values['items'])
    await record_revision(db, sprint, actor_id, 'revise')
    return sprint


async def transition_sprint(db, project_id, sprint_id, action, expected_revision, actor_id):
    sprint, plan = await lock_sprint(db, project_id, sprint_id)
    target = {'start':'active','close':'closed','cancel':'cancelled'}[action]
    if sprint.status == target and sprint.revision in {expected_revision, expected_revision+1}:
        return sprint
    if sprint.revision != expected_revision:
        raise ValueError("Révision périmée : rechargez le sprint.")
    allowed = {'start':{'draft'},'close':{'active'},'cancel':{'draft','active'}}[action]
    if sprint.status not in allowed:
        raise ValueError("Transition de sprint interdite.")
    if action == 'start':
        run = await db.get(FlowRun, plan.validated_flow_run_id)
        if (not sprint.starts_at <= utcnow() < sprint.ends_at or not await sprint_items(db,sprint.id)
                or plan.status != 'approved' or not run or run.status not in {'running','paused'}):
            raise ValueError("Période en cours, feuilles sélectionnées et plan exécutable requis.")
        sprint.activated_at = utcnow()
    else:
        sprint.closed_at = utcnow()
    sprint.status, sprint.revision = target, sprint.revision+1
    await record_revision(db,sprint,actor_id,action)
    return sprint


def reason_label(reason):
    labels = {'run_inactive':"Run suspendu ou arrêté", 'attempt_limit':"Budget de tentatives épuisé",
        'retry_delay':"Délai avant relance", 'parallel_limit':"Tous les créneaux sont occupés",
        'approval_required':"Accord humain requis", 'approval_pending':"Décision humaine attendue",
        'status:failed':"Échec : relance humaine requise", 'lease_expired':"Réservation expirée",
        'session_expired':"Session déconnectée ou expirée"}
    return "Prérequis : " + reason.partition(':')[2] if reason.startswith('dependency:') else labels.get(reason,reason)


def next_action(reasons, column):
    if column in {'done','cancelled'}: return 'none', "Aucune action d'exécution"
    if any(r in {'approval_required','approval_pending','run_inactive','attempt_limit','status:failed'} for r in reasons):
        return 'human', "Chef de Projet : examiner le blocage dans le plan"
    if any(r in {'lease_expired','session_expired'} for r in reasons):
        return 'service', "Relais : récupérer la réservation lors de la prochaine demande"
    if reasons: return 'agent', "Agent : attendre les prérequis, le délai ou un créneau"
    return 'agent', "Agent : poursuivre la tâche" if column=='in_progress' else "Agent compatible : réserver la tâche"


async def sprint_board(db, project_id, sprint_id, *, column=None, overdue_only=False, blocked_only=False):
    # Verrou de plan pour un instantané cohérent avec les mutations du coordinateur.
    # La lecture n'effectue ni expiration, ni demande d'accord, ni audit de mutation.
    sprint, plan = await lock_sprint(db, project_id, sprint_id)
    now = utcnow()
    tasks = await plan_tasks(db, plan.id); by_id = {t.id:t for t in tasks}
    items = await sprint_items(db,sprint.id)
    run = await db.get(FlowRun,plan.validated_flow_run_id)
    skills = {s.id:s.code for s in (await db.scalars(select(SkillProfile))).all()}
    session_ids = {t.claimed_session_id for t in tasks if t.claimed_session_id}
    sessions = {s.id:s for s in (await db.scalars(select(AgentSession).where(AgentSession.id.in_(session_ids)))).all()}
    attempts = (await db.scalars(select(AgentTaskRun).where(AgentTaskRun.task_id.in_([i.task_id for i in items])))).all()
    histories = defaultdict(list)
    for attempt in attempts: histories[attempt.task_id].append(attempt)
    cutoff = max(sprint.starts_at,min(now, sprint.ends_at, sprint.closed_at or sprint.ends_at))
    cards, cycle_times, completed_count, missing_dates = [], [], 0, 0
    reason_counts = Counter()
    for item in items:
        task = by_id[item.task_id]
        reasons = []
        if task.status in {'completed','cancelled'}:
            state = 'done' if task.status=='completed' else 'cancelled'
        elif task.status in {'claimed','running'}:
            if not run or run.status != 'running': reasons.append('run_inactive')
            if not task.lease_expires_at or task.lease_expires_at <= now: reasons.append('lease_expired')
            session = sessions.get(task.claimed_session_id)
            if not session or session.status != 'active' or session.expires_at <= now: reasons.append('session_expired')
            state = 'blocked' if reasons else 'in_progress'
        else:
            reasons = blockers(task,tasks,plan,run.status if run else 'unavailable')
            if task.status == 'ready' and approval_required(task,tasks,plan):
                approval = await active_approval(db,task)
                if not approval or approval.status != 'approved':
                    reasons.append('approval_pending' if approval else 'approval_required')
            state = 'blocked' if reasons else 'todo'
        history = histories[task.id]
        completed = next((a for a in history if a.status=='completed' and a.ended_at),None)
        completed_at = completed.ended_at if completed else None
        if task.status=='completed' and not completed_at: missing_dates += 1
        if completed_at and max(sprint.starts_at,item.added_at) <= completed_at < cutoff:
            completed_count += 1
            if history:
                cycle_times.append(max(0,(completed_at-min(a.started_at for a in history)).total_seconds()))
        deadline = item.target_due_at or task.due_at or sprint.ends_at
        ancestors=[]; parent=by_id.get(task.parent_task_id)
        while parent:
            ancestors.insert(0,parent.title); parent=by_id.get(parent.parent_task_id)
        owner, action = next_action(reasons,state)
        card = {**task_view(task), 'column':state, 'skill_code':skills[task.skill_profile_id],
            'ancestors':ancestors, 'target_due_at':item.target_due_at, 'effective_due_at':deadline,
            'overdue':task.status not in {'completed','cancelled'} and deadline < now,
            'completed_at':completed_at, 'completed_late':completed_at > deadline if completed_at else None,
            'blocked_by':reasons, 'blocker_labels':[reason_label(r) for r in reasons],
            'action_owner':owner, 'next_action':action}
        cards.append(card)
        # Une feuille compte une fois par cause, même avec plusieurs prérequis bloquants.
        reason_counts.update({r.split(':',1)[0] if r.startswith('dependency:') else r for r in reasons})
    counts = {c:sum(card['column']==c for card in cards) for c in ('todo','in_progress','blocked','done','cancelled')}
    elapsed_days = max(0,(cutoff-sprint.starts_at).total_seconds())/86400
    metrics = {'throughput_count':completed_count, 'throughput_per_day':round(completed_count/elapsed_days,3) if elapsed_days else None,
        'cycle_time_average_seconds':round(sum(cycle_times)/len(cycle_times),3) if cycle_times else None,
        'cycle_time_sample_count':len(cycle_times), 'completed_without_timestamp_count':missing_dates,
        'blocked_count':counts['blocked'], 'blocked_by_reason':dict(reason_counts),
        'overdue_count':sum(c['overdue'] for c in cards), 'window_start':sprint.starts_at,'window_end':cutoff,
        'historical_blocked_duration_seconds':None}
    cards = [c for c in cards if (not column or c['column']==column) and (not overdue_only or c['overdue'])
             and (not blocked_only or c['column']=='blocked')]
    cards.sort(key=lambda c:(c['priority'],c['effective_due_at'],c['task_key']))
    return {'sprint':sprint_view(sprint),'as_of':now,'period_ended':now>=sprint.ends_at,
            'counts':counts,'metrics':metrics,'cards':cards}
