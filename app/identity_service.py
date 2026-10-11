"""Services d'authentification, de sessions et d'ACL projet."""

import hashlib
import hmac
import os
import secrets
from datetime import timedelta

from argon2 import PasswordHasher
from argon2.exceptions import VerifyMismatchError, VerificationError
from email_validator import EmailNotValidError, validate_email
from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.identity_models import (
    AuditEvent, Invitation, LoginSession, LoginThrottle, Permission,
    Project, ProjectMember, Role, RolePermission, User, utcnow,
)


ROLE_LABELS = {
    "product_owner": "Product Owner",
    "project_manager": "Chef de Projet",
    "architect": "Architecte Applicatif",
    "reader": "Lecteur",
}
COMMON = {"project.read", "pilotage.read", "agent.read", "version.compare", "memory.read", "scheduled_task.read"}
MEMBER = COMMON | {"source.upload", "generation.create", "deliverable.comment", "context.comment",
                   "agent.session.connect", "agent.task.claim", "agent.message.send", "memory.propose", "incident.report", "code.collect", "code.test", "code.review",
                   "scheduled_task.result.edit", "scheduled_task.feedback"}
ROLE_PERMISSIONS = {
    "product_owner": MEMBER | {"context.edit", "context.validate.business", "deliverable.validate.business", "flow.run.decide", "memory.validate.business", "scheduled_task.review"},
    "project_manager": MEMBER | {"budget.configure", "quota.manage", "project.manage", "planning.validate", "deliverable.publish", "flow.execute", "agent.plan.approve", "agent.run.manage", "memory.validate.result", "incident.manage", "worktree.create", "worktree.delete", "git.merge", "git.push", "git.deploy", "scheduled_task.manage", "scheduled_task.subject.manage", "scheduled_task.run", "scheduled_task.review"},
    "architect": MEMBER | {"runtime.configure", "architecture.validate", "context.edit", "context.validate.technical", "flow.edit", "flow.validate", "flow.run.decide", "agent.team.configure", "memory.validate.technical", "repository.manage", "code.approve", "scheduled_task.review"},
    "reader": COMMON,
}
PLATFORM_PERMISSIONS = {"platform.admin", "llm.configure", "user.manage", "agent.profile.configure", "memory.global.manage"}
password_hasher = PasswordHasher()
_dummy_hash = password_hasher.hash("mot-de-passe-factice-pour-la-verification")


def database_factory() -> async_sessionmaker[AsyncSession]:
    url = os.environ.get("MIMIC_DATABASE_URL")
    if not url or not url.startswith("postgresql+asyncpg://"):
        raise RuntimeError("MIMIC_DATABASE_URL doit cibler PostgreSQL via asyncpg")
    engine = create_async_engine(url, pool_pre_ping=True)
    return async_sessionmaker(engine, expire_on_commit=False)


def normalize_email(value: str) -> str:
    try:
        return validate_email(value.strip(), check_deliverability=False).normalized.lower()
    except EmailNotValidError as exc:
        raise ValueError("Adresse e-mail invalide.") from exc


def validate_password(value: str) -> None:
    if not 12 <= len(value) <= 128:
        raise ValueError("Le mot de passe doit contenir entre 12 et 128 caractères.")


def digest(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def csrf_for(secret: str, purpose: str) -> str:
    return hmac.new(secret.encode("utf-8"), purpose.encode("utf-8"), hashlib.sha256).hexdigest()


def audit(db: AsyncSession, action: str, outcome: str, *, actor_id: str | None = None,
          project_id: str | None = None, target_type: str | None = None,
          target_id: str | None = None, details: dict | None = None) -> None:
    db.add(AuditEvent(action=action, outcome=outcome, actor_id=actor_id,
                      project_id=project_id, target_type=target_type, target_id=target_id,
                      details=details or {}))


async def seed_roles(db: AsyncSession) -> None:
    """Catalogue V1 fixe. Une exécution répétée est sans effet sur les liaisons existantes."""
    for code, label in ROLE_LABELS.items():
        if await db.get(Role, code) is None:
            db.add(Role(code=code, label=label))
    all_permissions = set().union(*ROLE_PERMISSIONS.values(), PLATFORM_PERMISSIONS, {"project.create"})
    for code in all_permissions:
        if await db.get(Permission, code) is None:
            db.add(Permission(code=code))
    await db.flush()
    for role_code, permissions in ROLE_PERMISSIONS.items():
        for code in permissions:
            key = {"role_code": role_code, "permission_code": code}
            if await db.get(RolePermission, (role_code, code)) is None:
                db.add(RolePermission(**key))
    await db.flush()


async def create_invitation(db: AsyncSession, *, actor: User, email: str,
                            display_name: str) -> tuple[User, str]:
    email = normalize_email(email)
    name = display_name.strip()
    if not 1 <= len(name) <= 120:
        raise ValueError("Le nom doit contenir entre 1 et 120 caractères.")
    user = await db.scalar(select(User).where(User.email == email).with_for_update())
    if user is not None and user.is_active:
        raise ValueError("Ce compte est déjà actif. Ajoutez-le directement au projet.")
    if user is None:
        user = User(email=email, display_name=name)
        db.add(user)
        await db.flush()
    else:
        user.display_name = name
    now = utcnow()
    old = await db.scalars(select(Invitation).where(Invitation.user_id == user.id, Invitation.used_at.is_(None)))
    for invitation in old:
        invitation.used_at = now
    code = secrets.token_urlsafe(32)
    db.add(Invitation(user_id=user.id, token_hash=digest(code), invited_by=actor.id,
                      expires_at=now + timedelta(hours=48)))
    audit(db, "user.invite", "success", actor_id=actor.id, target_type="user", target_id=user.id)
    await db.flush()
    return user, code


async def activate_user(db: AsyncSession, *, email: str, code: str, password: str) -> bool:
    try:
        email = normalize_email(email)
        validate_password(password)
    except ValueError:
        return False
    if len(code) > 256:
        return False
    user = await db.scalar(select(User).where(User.email == email).with_for_update())
    if user is None or user.is_active:
        return False
    invitation = await db.scalar(select(Invitation).where(
        Invitation.user_id == user.id, Invitation.token_hash == digest(code),
        Invitation.used_at.is_(None), Invitation.expires_at > utcnow(),
    ).with_for_update())
    if invitation is None:
        audit(db, "user.activate", "denied", actor_id=user.id, target_type="user", target_id=user.id)
        return False
    user.password_hash = password_hasher.hash(password)
    user.is_active = True
    invitation.used_at = utcnow()
    audit(db, "user.activate", "success", actor_id=user.id, target_type="user", target_id=user.id)
    await db.flush()
    return True


async def authenticate(db: AsyncSession, *, email: str, password: str, throttle_key: str) -> tuple[User, str] | None:
    """Limite par identifiant : 5 échecs/15 min, blocage de 15 min."""
    now = utcnow()
    await db.execute(pg_insert(LoginThrottle).values(
        identifier_hash=throttle_key, failures=0, window_started_at=now,
    ).on_conflict_do_nothing(index_elements=[LoginThrottle.identifier_hash]))
    throttle = await db.scalar(select(LoginThrottle).where(
        LoginThrottle.identifier_hash == throttle_key).with_for_update())
    if throttle.blocked_until and throttle.blocked_until > now:
        audit(db, "auth.login", "blocked")
        return None
    if throttle.window_started_at < now - timedelta(minutes=15):
        throttle.failures = 0
        throttle.window_started_at = now
        throttle.blocked_until = None
    user = await db.scalar(select(User).where(User.email == email))
    stored_hash = user.password_hash if user and user.is_active and user.password_hash else _dummy_hash
    try:
        valid = password_hasher.verify(stored_hash, password)
    except (VerifyMismatchError, VerificationError):
        valid = False
    if not valid or user is None or not user.is_active or not user.password_hash:
        throttle.failures += 1
        if throttle.failures >= 5:
            throttle.blocked_until = now + timedelta(minutes=15)
        audit(db, "auth.login", "denied", actor_id=user.id if user else None)
        return None
    throttle.failures = 0
    throttle.blocked_until = None
    if password_hasher.check_needs_rehash(user.password_hash):
        user.password_hash = password_hasher.hash(password)
    user.last_login_at = now
    token = secrets.token_urlsafe(32)
    db.add(LoginSession(user_id=user.id, token_hash=digest(token),
                        expires_at=now + timedelta(hours=12)))
    audit(db, "auth.login", "success", actor_id=user.id, target_type="user", target_id=user.id)
    await db.flush()
    return user, token


async def session_user(db: AsyncSession, token: str | None) -> tuple[User, LoginSession] | None:
    if not token:
        return None
    row = await db.execute(select(User, LoginSession).join(LoginSession, LoginSession.user_id == User.id).where(
        LoginSession.token_hash == digest(token), LoginSession.revoked_at.is_(None),
        LoginSession.expires_at > utcnow(), User.is_active.is_(True),
    ))
    return row.one_or_none()


async def project_permission(db: AsyncSession, user: User, project_id: str, permission: str) -> bool:
    if user.is_platform_admin:
        return True
    allowed = await db.scalar(select(RolePermission.permission_code).join(
        ProjectMember, ProjectMember.role_code == RolePermission.role_code,
    ).where(ProjectMember.project_id == project_id, ProjectMember.user_id == user.id,
            ProjectMember.is_active.is_(True), RolePermission.permission_code == permission))
    return allowed is not None


async def assign_member(db: AsyncSession, *, actor: User, project: Project, user: User,
                        role_code: str) -> None:
    if role_code not in ROLE_LABELS:
        raise ValueError("Rôle inconnu.")
    if user.id == project.owner_id and role_code != "project_manager":
        raise ValueError("Le responsable du projet conserve le rôle Chef de Projet.")
    member = await db.scalar(select(ProjectMember).where(
        ProjectMember.project_id == project.id, ProjectMember.user_id == user.id,
    ).with_for_update())
    old_role = member.role_code if member and member.is_active else None
    if member is None:
        member = ProjectMember(project_id=project.id, user_id=user.id, role_code=role_code)
        db.add(member)
    else:
        member.role_code = role_code
        member.is_active = True
    audit(db, "project.member.assign", "success", actor_id=actor.id,
          project_id=project.id, target_type="user", target_id=user.id,
          details={"old_role": old_role, "new_role": role_code})
    await db.flush()
