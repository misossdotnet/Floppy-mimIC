"""Porte humaine minimale du flux agentique mono-nœud."""

from uuid import uuid4

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.agent_models import (AgentPlanVersion, AgentTask, AgentTeam, AgentTeamSkill,
                              FlowDefinition, FlowRun, FlowVersion, SkillProfile)
from app.agent_progress import hierarchy_depths
from app.agent_coordinator import leaf_dependencies
from app.identity_models import utcnow
from app.identity_service import audit


async def create_flow(db: AsyncSession, *, project_id: str, team_id: str,
                      name: str, actor_id: str) -> FlowVersion:
    team = await db.get(AgentTeam, team_id)
    if team is None or team.project_id != project_id or not team.is_active:
        raise ValueError("Équipe inactive ou hors projet.")
    definition = FlowDefinition(project_id=project_id, team_id=team_id,
                                name=name, created_by=actor_id)
    db.add(definition)
    await db.flush()
    version = FlowVersion(flow_definition_id=definition.id, version_number=1,
                          graph={"nodes": [{"id": "agent_team", "type": "agent_team", "team_id": team_id}],
                                 "edges": []}, status="draft")
    db.add(version)
    await db.flush()
    audit(db, "flow.edit", "success", actor_id=actor_id, project_id=project_id,
          target_type="flow_version", target_id=version.id)
    return version


async def transition_flow(db: AsyncSession, *, project_id: str, version_id: str,
                          actor_id: str, target: str) -> FlowVersion:
    version = await db.scalar(select(FlowVersion).where(FlowVersion.id == version_id).with_for_update())
    definition = await db.get(FlowDefinition, version.flow_definition_id) if version else None
    if definition is None or definition.project_id != project_id:
        raise ValueError("Version de flux introuvable dans le projet.")
    expected = "draft" if target == "review" else "review"
    if version.status != expected or target not in {"review", "validated"}:
        raise ValueError("Transition de flux invalide.")
    if version.graph != {"nodes": [{"id": "agent_team", "type": "agent_team",
                                    "team_id": definition.team_id}], "edges": []}:
        raise ValueError("Graphe agentique non conforme.")
    team = await db.get(AgentTeam, definition.team_id)
    if team is None or not team.is_active or team.project_id != project_id:
        raise ValueError("Équipe inactive ou hors projet.")
    version.status = target
    if target == "validated":
        version.validated_by = actor_id
        version.validated_at = utcnow()
    audit(db, "flow.validate" if target == "validated" else "flow.edit", "success",
          actor_id=actor_id, project_id=project_id, target_type="flow_version",
          target_id=version.id, details={"status": target})
    return version


async def launch_flow(db: AsyncSession, *, project_id: str, version_id: str,
                      actor_id: str) -> FlowRun:
    version = await db.get(FlowVersion, version_id)
    definition = await db.get(FlowDefinition, version.flow_definition_id) if version else None
    if (version is None or definition is None or definition.project_id != project_id
            or version.status != "validated" or not version.validated_by or not version.validated_at):
        raise ValueError("Version de flux non validée.")
    run = FlowRun(project_id=project_id, team_id=definition.team_id,
                  flow_version_id=version.id, launched_by=actor_id, status="waiting_human")
    db.add(run)
    await db.flush()
    audit(db, "flow.execute", "success", actor_id=actor_id, project_id=project_id,
          target_type="flow_run", target_id=run.id)
    return run


async def propose_plan(db: AsyncSession, *, project_id: str, run_id: str,
                       tasks: list[dict], actor_id: str, max_parallel_tasks: int = 4,
                       max_attempts: int = 3, autonomy_mode: str = "bounded") -> AgentPlanVersion:
    run = await db.scalar(select(FlowRun).where(FlowRun.id == run_id).with_for_update())
    if run is None or run.project_id != project_id or run.status != "waiting_human":
        raise ValueError("Exécution indisponible pour le plan.")
    if await db.scalar(select(AgentPlanVersion.id).where(
            AgentPlanVersion.validated_flow_run_id == run_id)):
        raise ValueError("Un plan existe déjà pour cette exécution.")
    depths = hierarchy_depths(tasks)
    leaf_dependencies(tasks)
    if not 1 <= max_parallel_tasks <= 8 or not 1 <= max_attempts <= 5:
        raise ValueError("Limites du plan invalides.")
    if autonomy_mode not in {"bounded", "sensitive", "per_action"} or any(
            item.get("action_class", "standard") not in {"standard", "sensitive"} for item in tasks):
        raise ValueError("Mode d'autonomie ou classe d'action invalide.")
    if autonomy_mode == "bounded" and any(item.get("action_class") == "sensitive" for item in tasks):
        raise ValueError("Une action sensible exige un mode avec approbation explicite.")
    profiles = (await db.scalars(select(SkillProfile).join(AgentTeamSkill,
        AgentTeamSkill.skill_profile_id == SkillProfile.id).where(
        AgentTeamSkill.team_id == run.team_id, SkillProfile.is_active.is_(True)))).all()
    by_code = {profile.code: profile for profile in profiles}
    if any(item["skill_code"] not in by_code for item in tasks):
        raise ValueError("Compétence absente de l'équipe.")
    plan = AgentPlanVersion(project_id=project_id, team_id=run.team_id,
        flow_run_id=run.id, validated_flow_run_id=run.id, status="proposed",
        max_parallel_tasks=max_parallel_tasks, max_attempts=max_attempts, autonomy_mode=autonomy_mode)
    db.add(plan)
    await db.flush()
    ids = {item["task_key"]: str(uuid4()) for item in tasks}
    # Insérer les parents avant leurs enfants pour la FK composite PostgreSQL.
    for item in sorted(tasks, key=lambda item: depths[item["task_key"]]):
        db.add(AgentTask(id=ids[item["task_key"]], project_id=project_id,
            plan_version_id=plan.id, team_id=run.team_id,
            skill_profile_id=by_code[item["skill_code"]].id,
            task_key=item["task_key"], title=item["title"], status="ready",
            action_class=item.get("action_class", "standard"),
            parent_task_id=ids.get(item.get("parent_task_key")), depth=depths[item["task_key"]],
            dependency_keys=item.get("dependency_keys", []),
            priority=item.get("priority", "P2"), due_at=item.get("due_at"), progress_percent=0))
        await db.flush()
    audit(db, "agent.plan.propose", "success", actor_id=actor_id, project_id=project_id,
          target_type="agent_plan_version", target_id=plan.id,
          details={"task_count": len(tasks), "autonomy_mode": autonomy_mode,
                   "sensitive_task_keys": [item["task_key"] for item in tasks if item.get("action_class") == "sensitive"]})
    return plan


async def transition_plan(db: AsyncSession, *, project_id: str, plan_id: str,
                          actor_id: str, target: str) -> AgentPlanVersion:
    plan = await db.scalar(select(AgentPlanVersion).where(
        AgentPlanVersion.id == plan_id, AgentPlanVersion.project_id == project_id).with_for_update(key_share=True))
    run = await db.scalar(select(FlowRun).where(
        FlowRun.id == plan.validated_flow_run_id).with_for_update()) if plan and plan.validated_flow_run_id else None
    if plan is None or run is None or run.status != "waiting_human" or run.team_id != plan.team_id:
        raise ValueError("Plan ou exécution indisponible.")
    expected = "proposed" if target == "review" else "review"
    if plan.status != expected or target not in {"review", "approved"}:
        raise ValueError("Transition de plan invalide.")
    plan.status = target
    if target == "approved":
        plan.approved_by = actor_id
        plan.approved_at = utcnow()
        run.status = "running"
    audit(db, "agent.plan.approve" if target == "approved" else "agent.plan.propose",
          "success", actor_id=actor_id, project_id=project_id,
          target_type="agent_plan_version", target_id=plan.id,
          details={"status": target})
    return plan
