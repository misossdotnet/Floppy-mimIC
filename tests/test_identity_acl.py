import asyncio
import re

from sqlalchemy import func, select

from app.identity_models import AuditEvent, Project, ProjectMember, User
from app.identity_cli import bootstrap_admin

from .conftest import ADMIN_PASSWORD, create_user, with_db


def csrf(html: str) -> str:
    match = re.search(r'name="csrf_token" value="([0-9a-f]+)"', html)
    assert match, "Jeton CSRF absent"
    return match.group(1)


def login(client, email: str, password: str = ADMIN_PASSWORD):
    token = csrf(client.get("/login").text)
    response = client.post("/login", data={"email": email, "password": password,
                                           "csrf_token": token})
    assert response.status_code == 303
    return csrf(client.get("/projects/new").text)


def test_bootstrap_administrateur_initial(pg_client):
    asyncio.run(bootstrap_admin("premier-admin@example.org", "Premier admin", ADMIN_PASSWORD))
    assert login(pg_client, "premier-admin@example.org")
    assert pg_client.get("/admin/llm").status_code == 200
    try:
        asyncio.run(bootstrap_admin("second-admin@example.org", "Second admin", ADMIN_PASSWORD))
    except RuntimeError as exc:
        assert "Bootstrap refusé" in str(exc)
    else:
        raise AssertionError("Le second bootstrap doit être refusé")


def test_invitation_activation_acl_et_audit(pg_client):
    _, admin_email = create_user(admin=True)
    admin_csrf = login(pg_client, admin_email)
    response = pg_client.post("/admin/users", data={
        "email": "invitee@example.org", "display_name": "Invitée", "csrf_token": admin_csrf,
    })
    assert response.status_code == 200
    invitation = re.search(r'class="app-invitation-code">([^<]+)<', response.text)
    assert invitation
    code = invitation.group(1)

    project = pg_client.post("/projects", data={"name": "Projet ACL", "description": "",
                                                 "csrf_token": admin_csrf})
    assert project.status_code == 303
    project_id = project.headers["location"].split("/")[-1]

    async def invited_user(db):
        return await db.scalar(select(User).where(User.email == "invitee@example.org"))

    invited = asyncio.run(with_db(invited_user))
    assert invited and not invited.is_active
    assert pg_client.post(f"/projects/{project_id}/members", data={
        "email": invited.email, "role": "reader", "csrf_token": admin_csrf,
    }).status_code == 303
    admin_session = pg_client.cookies.get("mimic-session")
    pg_client.cookies.clear()
    activation_csrf = csrf(pg_client.get("/activate").text)
    activated = pg_client.post("/activate", data={"email": invited.email, "code": code,
        "password": ADMIN_PASSWORD, "csrf_token": activation_csrf})
    assert activated.status_code == 200
    repeated = pg_client.post("/activate", data={"email": invited.email, "code": code,
        "password": ADMIN_PASSWORD, "csrf_token": csrf(pg_client.get("/activate").text)})
    assert "Activation impossible" in repeated.text
    member_csrf = login(pg_client, invited.email)
    member_session = pg_client.cookies.get("mimic-session")
    assert pg_client.get(f"/projects/{project_id}").status_code == 200
    assert pg_client.get("/admin/llm").status_code == 403
    assert pg_client.post(f"/projects/{project_id}/members", data={
        "email": admin_email, "role": "architect", "csrf_token": member_csrf,
    }).status_code == 403

    pg_client.cookies.set("mimic-session", admin_session)
    other = pg_client.post("/projects", data={"name": "Projet privé", "description": "",
                                                "csrf_token": admin_csrf})
    assert other.status_code == 303
    pg_client.cookies.set("mimic-session", member_session)
    assert pg_client.get(other.headers["location"]).status_code == 404
    pg_client.cookies.set("mimic-session", admin_session)
    deactivated = pg_client.post(f"/admin/users/{invited.id}/deactivate",
                                 data={"csrf_token": admin_csrf})
    assert deactivated.status_code == 303
    pg_client.cookies.set("mimic-session", member_session)
    assert pg_client.get("/projects").headers["location"] == "/login"

    async def state(db):
        audit_rows = (await db.scalars(select(AuditEvent))).all()
        membership = await db.scalar(select(ProjectMember).where(
            ProjectMember.project_id == project_id, ProjectMember.user_id == invited.id))
        projects = await db.scalar(select(func.count()).select_from(Project))
        return audit_rows, membership.is_active, projects

    audit_rows, active, project_count = asyncio.run(with_db(state))
    events = [row.action for row in audit_rows]
    assert not active and project_count == 2
    assert all(code not in str(row.details) for row in audit_rows)
    assert {"user.invite", "user.activate", "project.member.assign",
            "user.deactivate", "platform.access"}.issubset(set(events))
