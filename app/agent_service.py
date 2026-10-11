"""Contrat d'enregistrement et d'authentification du relais MCP V1."""

import secrets
from datetime import timedelta, timezone

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.agent_catalog import CLIENT_CAPABILITIES, CLIENT_CATALOG, SKILL_CATALOG, SKILL_CAPABILITIES
from app.agent_models import AgentSession, AgentTeam, AgentTeamSkill, SkillProfile
from app.identity_models import Project, User, utcnow
from app.identity_service import audit, digest, project_permission


CLIENT_CODES = frozenset(CLIENT_CATALOG)
SKILL_CODES = frozenset(SKILL_CATALOG)
HEARTBEAT_SECONDS = 120
MAX_SESSION_HOURS = 12


async def seed_skill_profiles(db: AsyncSession) -> None:
    """Catalogue V1 utilisé au bootstrap des bases de test après TRUNCATE."""
    for code, label in SKILL_CATALOG.items():
        profile = await db.scalar(select(SkillProfile).where(
            SkillProfile.code == code, SkillProfile.version_number == 1))
        if profile is None:
            db.add(SkillProfile(code=code, version_number=1, label=label,
                allowed_capabilities=sorted(SKILL_CAPABILITIES[code]), is_active=True))
    await db.flush()


def validate_registration(client_code: str, skill_code: str, capabilities: list[str],
                          allowed_capabilities: list[str]) -> list[str]:
    if client_code not in CLIENT_CODES or skill_code not in SKILL_CODES:
        raise ValueError("Client ou compétence non catalogué.")
    if (not capabilities or len(capabilities) != len(set(capabilities))
            or set(capabilities) - CLIENT_CAPABILITIES[client_code]
            or set(capabilities) - set(allowed_capabilities)):
        raise ValueError("Capacités inconnues ou dupliquées.")
    if "session.heartbeat" not in capabilities:
        raise ValueError("La capacité de heartbeat est obligatoire.")
    return sorted(capabilities)


async def create_team(db: AsyncSession, *, actor: User, project_id: str,
                      name: str, skill_codes: list[str]) -> AgentTeam:
    clean_name = name.strip()
    if not 1 <= len(clean_name) <= 120 or not skill_codes or len(skill_codes) != len(set(skill_codes)):
        raise ValueError("Nom ou compétences invalides.")
    if set(skill_codes) - SKILL_CODES:
        raise ValueError("Compétence inconnue.")
    existing = await db.scalar(select(AgentTeam).where(
        AgentTeam.project_id == project_id, AgentTeam.name == clean_name))
    if existing is not None:
        raise ValueError("Une équipe porte déjà ce nom dans le projet.")
    profiles = (await db.scalars(select(SkillProfile).where(
        SkillProfile.code.in_(skill_codes), SkillProfile.is_active.is_(True))
        .order_by(SkillProfile.version_number.desc()))).all()
    by_code = {}
    for profile in profiles:
        by_code.setdefault(profile.code, profile)
    if len(by_code) != len(skill_codes):
        raise ValueError("Profil de compétence indisponible.")
    team = AgentTeam(project_id=project_id, name=clean_name, created_by=actor.id)
    db.add(team)
    await db.flush()
    for code in skill_codes:
        db.add(AgentTeamSkill(team_id=team.id, skill_profile_id=by_code[code].id))
    audit(db, "agent.team.create", "success", actor_id=actor.id,
          project_id=project_id, target_type="agent_team", target_id=team.id,
          details={"skills": sorted(skill_codes)})
    await db.flush()
    return team


async def register_session(db: AsyncSession, *, actor: User, project_id: str,
                           team_id: str, client_code: str, skill_code: str,
                           capabilities: list[str]) -> tuple[AgentSession, str]:
    if client_code not in CLIENT_CODES or skill_code not in SKILL_CODES:
        raise ValueError("Client ou compétence non catalogué.")
    team = await db.scalar(select(AgentTeam).where(
        AgentTeam.id == team_id, AgentTeam.project_id == project_id,
        AgentTeam.is_active.is_(True)))
    if team is None:
        raise ValueError("Équipe du projet indisponible.")
    profile = await db.scalar(select(SkillProfile).join(
        AgentTeamSkill, AgentTeamSkill.skill_profile_id == SkillProfile.id).where(
        AgentTeamSkill.team_id == team_id, SkillProfile.code == skill_code,
        SkillProfile.is_active.is_(True)).order_by(SkillProfile.version_number.desc()))
    if profile is None:
        raise ValueError("Compétence non autorisée dans cette équipe.")
    capabilities = validate_registration(client_code, skill_code, capabilities,
                                         profile.allowed_capabilities)
    now = utcnow()
    token = secrets.token_urlsafe(48)
    session = AgentSession(project_id=project_id, team_id=team.id,
                           skill_profile_id=profile.id, created_by=actor.id,
                           client_code=client_code, skill_code=skill_code,
                           capabilities=capabilities, token_hash=digest(token),
                           last_heartbeat_at=now,
                           expires_at=now + timedelta(seconds=HEARTBEAT_SECONDS),
                           max_expires_at=now + timedelta(hours=MAX_SESSION_HOURS))
    db.add(session)
    await db.flush()
    audit(db, "agent.session.connect", "success", actor_id=actor.id,
          project_id=project_id, target_type="agent_session", target_id=session.id,
          details={"client": client_code, "skill_profile_id": profile.id,
                   "team_id": team.id, "capabilities": capabilities})
    return session, token


async def authenticated_session(db: AsyncSession, *, token: str | None,
                                session_id: str, project_id: str | None = None,
                                capability: str | None = None) -> AgentSession | None:
    if not token or len(token) > 256:
        return None
    session = await db.scalar(select(AgentSession).where(
        AgentSession.id == session_id, AgentSession.token_hash == digest(token),
    ).with_for_update())
    if session is None or (project_id is not None and session.project_id != project_id):
        return None
    now = utcnow()
    if session.status == "active" and (session.expires_at <= now or session.max_expires_at <= now):
        session.status = "expired"
        from app.agent_routing import release_session_claims
        await release_session_claims(db, session, "session_expired")
        audit(db, "agent.session.expire", "success", project_id=session.project_id,
              target_type="agent_session", target_id=session.id)
        await db.flush()
    if session.status == "active":
        creator = await db.get(User, session.created_by)
        project = await db.get(Project, session.project_id)
        team = await db.get(AgentTeam, session.team_id) if session.team_id else None
        profile = await db.get(SkillProfile, session.skill_profile_id) if session.skill_profile_id else None
        team_skill = await db.scalar(select(AgentTeamSkill.id).where(
            AgentTeamSkill.team_id == session.team_id,
            AgentTeamSkill.skill_profile_id == session.skill_profile_id)) if team and profile else None
        if (creator is None or not creator.is_active or project is None or project.status == "archived"
                or team is None or not team.is_active or team.project_id != session.project_id
                or profile is None or not profile.is_active or profile.code != session.skill_code
                or team_skill is None
                or not await project_permission(db, creator, session.project_id, "agent.session.connect")):
            await revoke_session(db, session)
    if session.status != "active" or (capability and capability not in session.capabilities):
        return None
    return session


async def expire_project_sessions(db: AsyncSession, project_id: str) -> None:
    now = utcnow()
    sessions = (await db.scalars(select(AgentSession).where(
        AgentSession.project_id == project_id, AgentSession.status == "active",
        AgentSession.expires_at <= now).with_for_update())).all()
    for session in sessions:
        session.status = "expired"
        from app.agent_routing import release_session_claims
        await release_session_claims(db, session, "session_expired")
        audit(db, "agent.session.expire", "success", project_id=project_id,
              target_type="agent_session", target_id=session.id)
    if sessions:
        await db.flush()


async def heartbeat(db: AsyncSession, session: AgentSession) -> None:
    now = utcnow()
    session.last_heartbeat_at = now
    session.expires_at = min(now + timedelta(seconds=HEARTBEAT_SECONDS),
                             session.max_expires_at.astimezone(timezone.utc))
    audit(db, "agent.session.heartbeat", "success", project_id=session.project_id,
          target_type="agent_session", target_id=session.id)
    from app.agent_routing import extend_session_claims
    await extend_session_claims(db, session)
    await db.flush()


async def revoke_session(db: AsyncSession, session: AgentSession, *, actor_id: str | None = None) -> None:
    if session.status != "revoked":
        session.status = "revoked"
        session.revoked_at = utcnow()
        from app.agent_routing import release_session_claims
        await release_session_claims(db, session, "session_revoked")
        audit(db, "agent.session.revoke", "success", actor_id=actor_id,
              project_id=session.project_id, target_type="agent_session", target_id=session.id)
        await db.flush()
