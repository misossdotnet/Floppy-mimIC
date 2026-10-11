"""API V1 : identité, ACL, projets et configuration LLM."""

import hmac
import hashlib
import os
import secrets
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import Depends, FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import JSONResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.identity_models import LoginSession, Project, ProjectMember, Role, User, utcnow
from app.agent_api import (router as agent_router, team_router as agent_team_router,
                           catalog_router as agent_catalog_router, task_router as agent_task_router)
from app.incident_api import router as incident_router, agent_router as agent_incident_router
from app.git_web import router as git_web_router
from app.git_api import router as git_router, agent_router as agent_git_router
from app.agent_space_web import router as agent_space_web_router
from app.conversation_api import router as agent_message_router, human_router as agent_conversation_router
from app.agent_flow_api import router as agent_flow_router
from app.agent_sprint_api import router as agent_sprint_router
from app.agent_sprint_web import router as agent_sprint_web_router
from app.coffy_api import router as coffy_router, subject_router as coffy_subject_router
from app.coffy_web import router as coffy_web_router
from app.memory_api import router as memory_router, agent_router as agent_memory_router, global_router as global_memory_router
from app.llm_trace import cost_label
from app.llm_trace_api import router as llm_trace_router
from app.runtime_api import router as runtime_router, agent_router as agent_runtime_router
from app.runtime_web import router as runtime_web_router
from app.pilotage_api import router as pilotage_router
from app.business_models import (AnalysisRun, ContextComment, ContextItem, ContextItemVersion,
                                 ContextRelation, Deliverable, DeliverableComment, DeliverableVersion, DiffRun,
                                 ExportArtifact, GenerationRun, PublicationRecord, SourceDocument)
from app.context_service import (CONTEXT_TYPES, RELATION_TYPES, TECHNICAL_TYPES, add_comment,
                                 add_relation, create_item, decide_item, obsolete_item,
                                 remove_relation, resolve_comment, revise_item)
from app.ingestion_service import InvalidSource, validate_upload, validate_text
from app.generation_service import (DOCUMENT_STYLES, DOCUMENT_TYPES,
                                    retry_generation, revise_deliverable, start_generation,
                                    start_regeneration)
from app.object_storage import MAX_OBJECT_BYTES, ObjectStorage, StorageUnavailable, new_object_key
from app.review_service import (EXPORT_FORMATS, add_comment as add_deliverable_comment,
                                expected_role, publish, request_export, resolve_comment as resolve_deliverable_comment,
                                transition, validator_permission)
from app.versioning_service import request_diff, restore_version
from app.identity_service import (
    ROLE_LABELS, activate_user, assign_member, audit, authenticate, create_invitation,
    csrf_for, database_factory, normalize_email, project_permission,
    session_user,
)
from app.llm_models import LLMConnectionCheck, LLMProfileVersion
from app.llm_service import (
    PROVIDERS, SECRET_PATTERN, LLMProfileInput, allowed_base_urls, check_connection,
    choose_default, create_profile, credential_available, current_profile,
    current_profiles, default_profile, parse_form, profile_ready, revise_profile, settings as llm_settings,
)


@asynccontextmanager
async def lifespan(application: FastAPI):
    auth_key = os.environ.get("MIMIC_AUTH_KEY", "")
    if len(auth_key) < 32:
        raise RuntimeError("MIMIC_AUTH_KEY doit contenir au moins 32 caractères aléatoires")
    application.state.sessions = database_factory()
    application.state.auth_key = auth_key
    yield
    await application.state.sessions.kw["bind"].dispose()


app = FastAPI(title="Floppy-MimIC · API V1", lifespan=lifespan)
app.include_router(agent_router)
app.include_router(agent_team_router)
app.include_router(agent_catalog_router)
app.include_router(agent_task_router)
app.include_router(git_web_router)
app.include_router(git_router)
app.include_router(agent_git_router)
app.include_router(incident_router)
app.include_router(agent_incident_router)
app.include_router(agent_space_web_router)
app.include_router(agent_message_router)
app.include_router(agent_conversation_router)
app.include_router(agent_flow_router)
app.include_router(agent_sprint_router)
app.include_router(agent_sprint_web_router)
app.include_router(coffy_router)
app.include_router(coffy_subject_router)
app.include_router(coffy_web_router)
app.include_router(memory_router)
app.include_router(agent_memory_router)
app.include_router(global_memory_router)
app.include_router(llm_trace_router)
from app.budget_api import router as budget_router
app.include_router(budget_router)
app.include_router(runtime_router)
app.include_router(agent_runtime_router)
app.include_router(runtime_web_router)
app.include_router(pilotage_router)
APP_DIR = Path(__file__).resolve().parent
app.mount("/static", StaticFiles(directory=APP_DIR / "static"), name="static")
templates = Jinja2Templates(directory=APP_DIR / "templates")


@app.exception_handler(HTTPException)
async def html_error(request: Request, exc: HTTPException):
    if request.url.path.startswith("/api/"):
        return no_store(JSONResponse({"detail": exc.detail}, status_code=exc.status_code))
    if exc.status_code == 303 and exc.headers and exc.headers.get("Location"):
        return RedirectResponse(exc.headers["Location"], status_code=303)
    token = request.cookies.get(cookie_name("session"))
    user = None
    if token and hasattr(request.app.state, "sessions"):
        async with request.app.state.sessions() as db:
            principal_row = await session_user(db, token)
            if principal_row:
                user = principal_row[0]
    title = {403: "Accès refusé", 404: "Page introuvable", 409: "Action impossible",
             422: "Données invalides"}.get(exc.status_code, "Erreur")
    message = str(exc.detail) if exc.status_code in {409, 422} else {
        403: "Vous n'avez pas l'autorisation nécessaire ou le formulaire a expiré.",
        404: "Cette ressource n'est pas accessible.",
    }.get(exc.status_code, "La demande ne peut pas être traitée.")
    response = templates.TemplateResponse(request, "pages/error.html", {
        "request": request, "current_user": user, "identity_mode": True,
        "permissions": {"platform_admin": user.is_platform_admin} if user else {},
        "csrf_token": csrf_for(token, "session") if user and token else "",
        "title": title, "message": message,
    }, status_code=exc.status_code)
    return no_store(response)


def secure_cookie() -> bool:
    return os.environ.get("MIMIC_COOKIE_SECURE", "true").lower() != "false"


def cookie_name(kind: str) -> str:
    return ("__Host-" if secure_cookie() else "") + f"mimic-{kind}"


def set_cookie(response, kind: str, value: str, max_age: int) -> None:
    response.set_cookie(cookie_name(kind), value, max_age=max_age, secure=secure_cookie(),
                        httponly=True, samesite="lax", path="/")


def no_store(response):
    response.headers["Cache-Control"] = "no-store"
    response.headers["Referrer-Policy"] = "no-referrer"
    response.headers["X-Content-Type-Options"] = "nosniff"
    return response


async def db_session(request: Request):
    async with request.app.state.sessions() as db:
        yield db


async def principal(request: Request, db: AsyncSession = Depends(db_session)) -> tuple[User, LoginSession, str]:
    token = request.cookies.get(cookie_name("session"))
    result = await session_user(db, token)
    if result is None:
        raise HTTPException(status_code=303, headers={"Location": "/login"})
    user, session = result
    return user, session, token


def require_csrf(request: Request, supplied: str, *, session_token: str | None = None) -> None:
    secret = session_token or request.cookies.get(cookie_name("anon"))
    purpose = "session" if session_token else "anonymous"
    if not secret or not hmac.compare_digest(csrf_for(secret, purpose), supplied):
        raise HTTPException(status_code=403, detail="Formulaire expiré ou invalide.")


def context(request: Request, user: User, token: str, **data):
    return {"request": request, "current_user": user, "identity_mode": True,
            "csrf_token": csrf_for(token, "session"), "permissions": {
                "platform_admin": user.is_platform_admin,
                "project_create": True,
                "member_manage": user.is_platform_admin,
                "llm_configure": user.is_platform_admin,
            }, **data}


def page(request: Request, name: str, user: User, token: str, **data):
    return no_store(templates.TemplateResponse(request, f"pages/{name}.html",
                                               context(request, user, token, **data)))


def anonymous_page(request: Request, name: str, *, error_message: str | None = None,
                   success_message: str | None = None):
    nonce = request.cookies.get(cookie_name("anon")) or secrets.token_urlsafe(32)
    response = templates.TemplateResponse(request, f"pages/{name}.html", {
        "request": request, "current_user": None, "identity_mode": True, "permissions": {},
        "csrf_token": csrf_for(nonce, "anonymous"),
        "error_message": error_message, "success_message": success_message,
    })
    set_cookie(response, "anon", nonce, 900)
    return no_store(response)


async def project_for_read(db: AsyncSession, user: User, project_id: str) -> Project:
    project = await db.get(Project, project_id)
    if project is None:
        raise HTTPException(status_code=404, detail="Projet introuvable.")
    if not await project_permission(db, user, project_id, "project.read"):
        audit(db, "project.read", "denied", actor_id=user.id, project_id=project.id,
              target_type="project", target_id=project.id)
        await db.commit()
        raise HTTPException(status_code=404, detail="Projet introuvable.")
    return project


async def lock_project(db: AsyncSession, project_id: str) -> Project:
    """Sérialise les changements d'ACL et relit le responsable courant."""
    project = await db.scalar(select(Project).where(Project.id == project_id).with_for_update()
                              .execution_options(populate_existing=True))
    if project is None:
        raise HTTPException(status_code=404)
    return project


def project_data(project: Project, owner_name: str = "—") -> dict:
    return {"id": project.id, "name": project.name, "description": project.description,
            "owner_id": project.owner_id,
            "status": {"draft": "Brouillon", "scoping": "En cadrage", "in_review": "En validation",
                       "validated": "Validé", "archived": "Archivé"}.get(project.status, project.status),
            "owner_name": owner_name, "updated_at": project.updated_at.strftime("%d/%m/%Y")}


@app.get("/", include_in_schema=False)
async def home():
    return RedirectResponse("/projects", status_code=303)


@app.get("/login")
async def login_form(request: Request):
    return anonymous_page(request, "login")


@app.post("/login")
async def login(request: Request, email: str = Form(...), password: str = Form(...),
                csrf_token: str = Form(...), db: AsyncSession = Depends(db_session)):
    require_csrf(request, csrf_token)
    try:
        normalized = normalize_email(email)
    except ValueError:
        normalized = email.strip().lower()[:320]
    throttle_key = hmac.new(request.app.state.auth_key.encode(), normalized.encode(), "sha256").hexdigest()
    result = await authenticate(db, email=normalized, password=password, throttle_key=throttle_key)
    await db.commit()
    if result is None:
        return anonymous_page(request, "login", error_message="Identifiants invalides ou accès temporairement indisponible.")
    _, token = result
    response = RedirectResponse("/projects", status_code=303)
    set_cookie(response, "session", token, 12 * 3600)
    response.delete_cookie(cookie_name("anon"), path="/")
    return no_store(response)


@app.post("/logout")
async def logout(request: Request, csrf_token: str = Form(...),
                 actor: tuple[User, LoginSession, str] = Depends(principal),
                 db: AsyncSession = Depends(db_session)):
    user, session, token = actor
    require_csrf(request, csrf_token, session_token=token)
    session.revoked_at = utcnow()
    audit(db, "auth.logout", "success", actor_id=user.id, target_type="user", target_id=user.id)
    await db.commit()
    response = RedirectResponse("/login", status_code=303)
    response.delete_cookie(cookie_name("session"), path="/")
    return no_store(response)


@app.get("/activate")
async def activation_form(request: Request):
    return anonymous_page(request, "activate")


@app.post("/activate")
async def activate(request: Request, email: str = Form(...), code: str = Form(...),
                   password: str = Form(...), csrf_token: str = Form(...),
                   db: AsyncSession = Depends(db_session)):
    require_csrf(request, csrf_token)
    valid = await activate_user(db, email=email, code=code, password=password)
    await db.commit()
    if not valid:
        return anonymous_page(request, "activate", error_message="Activation impossible. Vérifiez le code et le mot de passe (12 caractères minimum).")
    return anonymous_page(request, "login", success_message="Compte activé. Vous pouvez vous connecter.")


@app.get("/projects")
async def projects(request: Request, q: str = "", actor: tuple[User, LoginSession, str] = Depends(principal),
                   db: AsyncSession = Depends(db_session)):
    user, _, token = actor
    if user.is_platform_admin:
        rows = (await db.scalars(select(Project).order_by(Project.updated_at.desc()))).all()
        items = [{**project_data(p), "role": "Administrateur Plateforme"} for p in rows]
    else:
        rows = (await db.execute(select(Project, Role.label).join(
            ProjectMember, ProjectMember.project_id == Project.id,
        ).join(Role, Role.code == ProjectMember.role_code).where(
            ProjectMember.user_id == user.id, ProjectMember.is_active.is_(True),
        ).order_by(Project.updated_at.desc()))).all()
        items = [{**project_data(p), "role": label} for p, label in rows]
    query = q.strip().casefold()
    if query:
        items = [item for item in items if query in item["name"].casefold()]
    return page(request, "projects", user, token, projects=items, filters={"q": q}, project_statuses=[])


@app.get("/projects/new")
async def new_project(request: Request, actor: tuple[User, LoginSession, str] = Depends(principal)):
    user, _, token = actor
    return page(request, "project_form", user, token, form={})


@app.post("/projects")
async def create_project(request: Request, name: str = Form(...), description: str = Form(""),
                         csrf_token: str = Form(...), actor: tuple[User, LoginSession, str] = Depends(principal),
                         db: AsyncSession = Depends(db_session)):
    user, _, token = actor
    require_csrf(request, csrf_token, session_token=token)
    name, description = name.strip(), description.strip()
    if not 1 <= len(name) <= 120 or len(description) > 3000:
        return page(request, "project_form", user, token, form={"name": name, "description": description},
                    error_message="Nom ou description invalide.")
    project = Project(name=name, description=description, owner_id=user.id)
    db.add(project)
    await db.flush()
    await assign_member(db, actor=user, project=project, user=user, role_code="project_manager")
    audit(db, "project.create", "success", actor_id=user.id, project_id=project.id,
          target_type="project", target_id=project.id)
    await db.commit()
    return RedirectResponse(f"/projects/{project.id}", status_code=303)


@app.get("/projects/{project_id}")
async def project_home(request: Request, project_id: str,
                       actor: tuple[User, LoginSession, str] = Depends(principal),
                       db: AsyncSession = Depends(db_session)):
    user, _, token = actor
    project = await project_for_read(db, user, project_id)
    owner = await db.get(User, project.owner_id)
    source_count = await db.scalar(select(func.count()).select_from(SourceDocument).where(
        SourceDocument.project_id == project_id))
    pending_count = await db.scalar(select(func.count()).select_from(ContextItem).where(
        ContextItem.project_id == project_id, ContextItem.status.in_(["Brouillon IA", "À revoir"])))
    failed_count = await db.scalar(select(func.count()).select_from(SourceDocument).where(
        SourceDocument.project_id == project_id, SourceDocument.status == "error"))
    return page(request, "project", user, token, project=project_data(project, owner.display_name if owner else "—"),
                attention_items=[], metrics={"sources": source_count, "pending_reviews": pending_count,
                                             "failures": failed_count})


async def require_project_permission(db: AsyncSession, user: User, project: Project,
                                     permission: str) -> None:
    if not await project_permission(db, user, project.id, permission):
        audit(db, permission, "denied", actor_id=user.id, project_id=project.id)
        await db.commit()
        raise HTTPException(status_code=403)


def source_view(source: SourceDocument) -> dict:
    labels = {"uploaded": "En attente", "extracting": "Extraction", "extracted": "Extrait",
              "analyzing": "Analyse IA", "analyzed": "Analysé", "error": "Échec"}
    errors = {"storage_unavailable": "Le stockage documentaire est indisponible. Relancez plus tard.",
              "dispatch_unavailable": "Le worker est indisponible. Relancez le traitement.",
              "extraction_failed": "L'extraction a échoué. Vérifiez le document puis relancez.",
              "invalid_response": "La réponse IA est invalide. Relancez l'analyse.",
              "configuration_unavailable": "Le profil LLM n'est plus disponible. Contactez l'administrateur."}
    return {"id": source.id, "name": source.name, "kind": "Besoin" if source.kind == "need" else "Document",
            "processing_status": labels.get(source.status, source.status), "raw_status": source.status,
            "created_at": source.created_at.strftime("%d/%m/%Y %H:%M"), "mime_type": source.mime_type,
            "size_label": f"{source.size_bytes:,} octets", "hash_short": source.sha256[:16],
            "attempt": source.attempt, "error_message": errors.get(source.error_code,
                "Le traitement a échoué. Consultez la tentative et relancez." if source.error_code else None),
            "analysis_status": "Terminée" if source.status == "analyzed" else None}


@app.get("/projects/{project_id}/sources")
async def sources(request: Request, project_id: str,
                  actor: tuple[User, LoginSession, str] = Depends(principal),
                  db: AsyncSession = Depends(db_session)):
    user, _, token = actor
    project = await project_for_read(db, user, project_id)
    rows = (await db.scalars(select(SourceDocument).where(SourceDocument.project_id == project_id)
                             .order_by(SourceDocument.created_at.desc()))).all()
    return page(request, "sources", user, token, project=project_data(project),
                sources=[source_view(row) for row in rows], max_upload_size="25 Mio",
                permissions={"source_upload": await project_permission(db, user, project_id, "source.upload"),
                             "platform_admin": user.is_platform_admin})


@app.post("/projects/{project_id}/sources")
async def upload_source(request: Request, project_id: str, file: UploadFile = File(...),
                        csrf_token: str = Form(...), actor: tuple[User, LoginSession, str] = Depends(principal),
                        db: AsyncSession = Depends(db_session)):
    user, _, token = actor
    require_csrf(request, csrf_token, session_token=token)
    project = await project_for_read(db, user, project_id)
    await require_project_permission(db, user, project, "source.upload")
    if project.status == "archived":
        raise HTTPException(status_code=409, detail="Projet archivé.")
    content = await file.read(MAX_OBJECT_BYTES + 1)
    try:
        name, mime = validate_upload(file.filename or "", content)
    except InvalidSource as exc:
        audit(db, "source.upload", "denied", actor_id=user.id, project_id=project_id,
              details={"reason": "invalid_source"})
        await db.commit()
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    key = new_object_key(project_id, "sources")
    try:
        storage = ObjectStorage.from_env()
        digest = await run_in_threadpool(storage.put_bytes, key, content, mime)
    except (StorageUnavailable, ValueError):
        raise HTTPException(status_code=503, detail="Stockage documentaire indisponible.") from None
    try:
        source = SourceDocument(project_id=project_id, created_by=user.id, name=name,
                                kind="file", mime_type=mime, object_key=key,
                                sha256=digest, size_bytes=len(content), status="uploaded")
        db.add(source)
        await db.flush()
        audit(db, "source.upload", "success", actor_id=user.id, project_id=project_id,
              target_type="source_document", target_id=source.id,
              details={"mime_type": mime, "size_bytes": len(content), "sha256": digest})
        await db.commit()
    except Exception:
        await db.rollback()
        try:
            await run_in_threadpool(storage.delete, key)
        except StorageUnavailable:
            pass
        raise
    try:
        from app.worker import extract_source
        extract_source.delay(source.id)
    except Exception:
        source.status, source.error_code = "error", "dispatch_unavailable"
        audit(db, "source.extract.dispatch", "failed", actor_id=user.id, project_id=project_id,
              target_type="source_document", target_id=source.id)
        await db.commit()
    return RedirectResponse(f"/projects/{project_id}/sources/{source.id}", status_code=303)


@app.post("/projects/{project_id}/needs")
async def create_need(request: Request, project_id: str, text: str = Form(...),
                      csrf_token: str = Form(...), actor: tuple[User, LoginSession, str] = Depends(principal),
                      db: AsyncSession = Depends(db_session)):
    user, _, token = actor
    require_csrf(request, csrf_token, session_token=token)
    project = await project_for_read(db, user, project_id)
    await require_project_permission(db, user, project, "source.upload")
    if project.status == "archived":
        raise HTTPException(status_code=409, detail="Projet archivé.")
    try:
        content = validate_text(text)
    except InvalidSource as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    if len(content) > 20_000:
        raise HTTPException(status_code=422, detail="Le besoin est limité à 20 000 caractères.")
    source = SourceDocument(project_id=project_id, created_by=user.id,
                            name=content.splitlines()[0][:100], kind="need", mime_type="text/plain",
                            sha256=hashlib.sha256(content.encode()).hexdigest(), size_bytes=len(content.encode()),
                            direct_text=content, extracted_text=content, status="extracted", attempt=1)
    db.add(source)
    await db.flush()
    audit(db, "source.need.create", "success", actor_id=user.id, project_id=project_id,
          target_type="source_document", target_id=source.id, details={"size_bytes": source.size_bytes})
    await db.commit()
    return RedirectResponse(f"/projects/{project_id}/sources/{source.id}", status_code=303)


async def source_for_read(db: AsyncSession, project_id: str, source_id: str) -> SourceDocument:
    row = await db.scalar(select(SourceDocument).where(SourceDocument.id == source_id,
                                                          SourceDocument.project_id == project_id))
    if row is None:
        raise HTTPException(status_code=404)
    return row


@app.get("/projects/{project_id}/sources/{source_id}")
async def source_detail(request: Request, project_id: str, source_id: str,
                        actor: tuple[User, LoginSession, str] = Depends(principal),
                        db: AsyncSession = Depends(db_session)):
    user, _, token = actor
    project = await project_for_read(db, user, project_id)
    source = await source_for_read(db, project_id, source_id)
    runs = (await db.scalars(select(AnalysisRun).where(AnalysisRun.source_id == source.id)
                             .order_by(AnalysisRun.created_at.desc()))).all()
    return page(request, "source_detail", user, token, project=project_data(project),
                source=source_view(source), analysis_runs=runs,
                can_analyze=await project_permission(db, user, project_id, "generation.create"),
                can_retry=await project_permission(db, user, project_id, "source.upload"))


@app.get("/projects/{project_id}/sources/{source_id}/download")
async def download_source(project_id: str, source_id: str,
                          actor: tuple[User, LoginSession, str] = Depends(principal),
                          db: AsyncSession = Depends(db_session)):
    user, _, _ = actor
    await project_for_read(db, user, project_id)
    source = await source_for_read(db, project_id, source_id)
    if source.kind != "file" or not source.object_key:
        raise HTTPException(status_code=404)
    try:
        content = await run_in_threadpool(ObjectStorage.from_env().get_bytes, source.object_key)
    except (StorageUnavailable, ValueError):
        raise HTTPException(status_code=503, detail="Stockage documentaire indisponible.") from None
    if hashlib.sha256(content).hexdigest() != source.sha256:
        raise HTTPException(status_code=503, detail="Intégrité du document indisponible.")
    audit(db, "source.download", "success", actor_id=user.id, project_id=project_id,
          target_type="source_document", target_id=source.id)
    await db.commit()
    return no_store(Response(content, media_type=source.mime_type,
                             headers={"Content-Disposition": 'attachment; filename="source"'}))


@app.post("/projects/{project_id}/sources/{source_id}/retry")
async def retry_extraction(request: Request, project_id: str, source_id: str,
                           csrf_token: str = Form(...), actor: tuple[User, LoginSession, str] = Depends(principal),
                           db: AsyncSession = Depends(db_session)):
    user, _, token = actor
    require_csrf(request, csrf_token, session_token=token)
    project = await project_for_read(db, user, project_id)
    await require_project_permission(db, user, project, "source.upload")
    if project.status == "archived":
        raise HTTPException(status_code=409, detail="Projet archivé.")
    source = await db.scalar(select(SourceDocument).where(SourceDocument.id == source_id,
                                SourceDocument.project_id == project_id).with_for_update())
    if source is None:
        raise HTTPException(status_code=404)
    if source.status != "error":
        raise HTTPException(status_code=409, detail="La source n'est pas en échec.")
    source.status, source.error_code = "uploaded", None
    audit(db, "source.extract.retry", "success", actor_id=user.id, project_id=project_id,
          target_type="source_document", target_id=source.id)
    await db.commit()
    try:
        from app.worker import extract_source
        extract_source.delay(source.id)
    except Exception:
        source.status, source.error_code = "error", "dispatch_unavailable"
        await db.commit()
    return RedirectResponse(f"/projects/{project_id}/sources/{source_id}", status_code=303)


@app.post("/projects/{project_id}/sources/{source_id}/analyze")
async def start_analysis(request: Request, project_id: str, source_id: str,
                         csrf_token: str = Form(...), actor: tuple[User, LoginSession, str] = Depends(principal),
                         db: AsyncSession = Depends(db_session)):
    user, _, token = actor
    require_csrf(request, csrf_token, session_token=token)
    project = await project_for_read(db, user, project_id)
    await require_project_permission(db, user, project, "generation.create")
    if project.status == "archived":
        raise HTTPException(status_code=409, detail="Projet archivé.")
    source = await db.scalar(select(SourceDocument).where(SourceDocument.id == source_id,
                                SourceDocument.project_id == project_id).with_for_update())
    if source is None:
        raise HTTPException(status_code=404)
    if source.status not in {"extracted", "analyzed"}:
        raise HTTPException(status_code=409, detail="La source doit être extraite avant l'analyse.")
    profile = await default_profile(db)
    if profile is None:
        raise HTTPException(status_code=409, detail="Configurez et vérifiez un profil LLM par défaut.")
    from app.llm_trace import analysis_inputs
    run = AnalysisRun(project_id=project_id, source_id=source.id, requested_by=user.id,
                      llm_profile_version_id=profile.id, status="queued", **analysis_inputs(source, profile))
    source.status, source.error_code = "analyzing", None
    db.add(run)
    await db.flush()
    audit(db, "source.analyze.request", "success", actor_id=user.id, project_id=project_id,
          target_type="analysis_run", target_id=run.id,
          details={"source_id": source.id, "profile_version_id": profile.id})
    await db.commit()
    try:
        from app.worker import analyze_source
        analyze_source.delay(run.id)
    except Exception:
        run.status, run.error_code, run.completed_at = "failed", "dispatch_unavailable", utcnow()
        source.status, source.error_code = "extracted", "dispatch_unavailable"
        audit(db, "source.analyze.dispatch", "failed", actor_id=user.id, project_id=project_id,
              target_type="analysis_run", target_id=run.id)
        await db.commit()
    return RedirectResponse(f"/projects/{project_id}/sources/{source_id}", status_code=303)


@app.get("/projects/{project_id}/context")
async def context_list(request: Request, project_id: str, type: str = "", status: str = "", item_id: str = "",
                       actor: tuple[User, LoginSession, str] = Depends(principal),
                       db: AsyncSession = Depends(db_session)):
    user, _, token = actor
    project = await project_for_read(db, user, project_id)
    query = select(ContextItem).where(ContextItem.project_id == project_id)
    if type in CONTEXT_TYPES:
        query = query.where(ContextItem.type == type)
    if status in {"Brouillon IA", "À revoir", "Validé", "Rejeté", "Obsolète"}:
        query = query.where(ContextItem.status == status)
    items = (await db.scalars(query.order_by(ContextItem.created_at.desc()))).all()
    selected = await db.scalar(select(ContextItem).where(ContextItem.project_id == project_id,
                                                         ContextItem.id == item_id)) if item_id else None
    source_names = {s.id: s.name for s in (await db.scalars(select(SourceDocument).where(
        SourceDocument.project_id == project_id))).all()}
    def view(item):
        return {"id": item.id, "title": item.title, "content": item.content,
                "type": CONTEXT_TYPES[item.type], "status": item.status,
                "version": item.current_version, "source_name": source_names.get(item.source_id),
                "source_id": item.source_id, "origin": item.origin, "type_code": item.type}
    can_edit = project.status != "archived" and await project_permission(db, user, project_id, "context.edit")
    can_validate = bool(project.status != "archived" and selected and await project_permission(db, user, project_id,
        "context.validate.technical" if selected.type in TECHNICAL_TYPES else "context.validate.business"))
    versions = (await db.scalars(select(ContextItemVersion).where(
        ContextItemVersion.item_id == selected.id).order_by(ContextItemVersion.version.desc()))).all() if selected else []
    author_ids = {v.actor_id for v in versions if v.actor_id}
    relation_rows = (await db.scalars(select(ContextRelation).where(
        ContextRelation.project_id == project_id,
        ((ContextRelation.source_id == selected.id) | (ContextRelation.target_id == selected.id)),
        ContextRelation.is_active.is_(True)).order_by(ContextRelation.created_at))).all() if selected else []
    related_ids = {r.source_id for r in relation_rows} | {r.target_id for r in relation_rows}
    related = {i.id: i for i in (await db.scalars(select(ContextItem).where(
        ContextItem.project_id == project_id, ContextItem.id.in_(related_ids)))).all()} if related_ids else {}
    comments = (await db.scalars(select(ContextComment).join(ContextItemVersion,
        ContextComment.item_version_id == ContextItemVersion.id).where(
        ContextItemVersion.item_id == selected.id).order_by(ContextComment.created_at))).all() if selected else []
    author_ids |= {c.author_id for c in comments} | {c.resolved_by for c in comments if c.resolved_by}
    authors = {u.id: u.display_name for u in (await db.scalars(select(User).where(User.id.in_(author_ids)))).all()} if author_ids else {}
    version_numbers = {v.id: v.version for v in versions}
    return page(request, "context", user, token, project=project_data(project),
                context_items=[view(item) for item in items], selected_item=view(selected) if selected else None,
                context_types=[{"code": code, "label": label} for code, label in CONTEXT_TYPES.items()],
                relation_types=[{"code": code, "label": label} for code, label in RELATION_TYPES.items()],
                relation_targets=[view(item) for item in (await db.scalars(select(ContextItem).where(
                    ContextItem.project_id == project_id, ContextItem.id != selected.id,
                    ContextItem.status != "Obsolète").order_by(ContextItem.title))).all()] if selected else [],
                item_versions=[{"version": v.version, "title": v.title, "content": v.content,
                                "status": v.status, "actor": authors.get(v.actor_id, "IA"),
                                "created_at": v.created_at.strftime("%d/%m/%Y %H:%M")} for v in versions],
                item_relations=[{"id": r.id, "type": RELATION_TYPES.get(r.type, r.type),
                                 "direction": "sortante" if r.source_id == selected.id else "entrante",
                                 "other": view(related[r.target_id if r.source_id == selected.id else r.source_id])}
                                for r in relation_rows],
                item_comments=[{"id": c.id, "body": c.body, "status": c.status,
                                "version": version_numbers.get(c.item_version_id),
                                "author": authors.get(c.author_id, "Utilisateur"),
                                "created_at": c.created_at.strftime("%d/%m/%Y %H:%M")}
                               for c in comments],
                filters={"type": type, "status": status}, can_validate=can_validate,
                can_comment=project.status != "archived" and await project_permission(db, user, project_id, "context.comment"),
                permissions={"context_edit": can_edit, "platform_admin": user.is_platform_admin})


async def context_item_for_update(db: AsyncSession, project_id: str, item_id: str) -> ContextItem:
    item = await db.scalar(select(ContextItem).where(ContextItem.project_id == project_id,
                                                   ContextItem.id == item_id).with_for_update())
    if item is None:
        raise HTTPException(status_code=404)
    return item


@app.post("/projects/{project_id}/context")
async def create_context(request: Request, project_id: str, type: str = Form(...),
                         title: str = Form(...), content: str = Form(...), csrf_token: str = Form(...),
                         actor: tuple[User, LoginSession, str] = Depends(principal),
                         db: AsyncSession = Depends(db_session)):
    user, _, token = actor
    require_csrf(request, csrf_token, session_token=token)
    project = await project_for_read(db, user, project_id)
    await require_project_permission(db, user, project, "context.edit")
    if project.status == "archived":
        raise HTTPException(status_code=409, detail="Projet archivé.")
    try:
        item = await create_item(db, project_id=project_id, source_id=None, analysis_run_id=None,
                                 actor_id=user.id, type=type, title=title, content=content, origin="manual")
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    await db.commit()
    return RedirectResponse(f"/projects/{project_id}/context?item_id={item.id}", status_code=303)


@app.post("/projects/{project_id}/context/{item_id}/versions")
async def update_context(request: Request, project_id: str, item_id: str,
                         title: str = Form(...), content: str = Form(...), expected_version: int = Form(...),
                         csrf_token: str = Form(...), actor: tuple[User, LoginSession, str] = Depends(principal),
                         db: AsyncSession = Depends(db_session)):
    user, _, token = actor
    require_csrf(request, csrf_token, session_token=token)
    project = await project_for_read(db, user, project_id)
    await require_project_permission(db, user, project, "context.edit")
    if project.status == "archived":
        raise HTTPException(status_code=409, detail="Projet archivé.")
    item = await context_item_for_update(db, project_id, item_id)
    try:
        await revise_item(db, item, actor=user, title=title, content=content,
                          expected_version=expected_version)
    except ValueError as exc:
        raise HTTPException(status_code=409 if "rechargez" in str(exc) else 422,
                            detail=str(exc)) from exc
    await db.commit()
    return RedirectResponse(f"/projects/{project_id}/context?item_id={item_id}", status_code=303)


@app.post("/projects/{project_id}/context/{item_id}/decisions")
async def decide_context(request: Request, project_id: str, item_id: str,
                         decision: str = Form(...), expected_version: int = Form(...),
                         reason: str = Form(""),
                         csrf_token: str = Form(...), actor: tuple[User, LoginSession, str] = Depends(principal),
                         db: AsyncSession = Depends(db_session)):
    user, _, token = actor
    require_csrf(request, csrf_token, session_token=token)
    project = await project_for_read(db, user, project_id)
    item = await context_item_for_update(db, project_id, item_id)
    permission = "context.edit" if decision == "review" else (
        "context.validate.technical" if item.type in TECHNICAL_TYPES else "context.validate.business")
    await require_project_permission(db, user, project, permission)
    if project.status == "archived":
        raise HTTPException(status_code=409, detail="Projet archivé.")
    try:
        await decide_item(db, item, actor=user, decision=decision,
                          expected_version=expected_version, reason=reason)
    except ValueError as exc:
        raise HTTPException(status_code=422 if "motif" in str(exc) else 409, detail=str(exc)) from exc
    await db.commit()
    return RedirectResponse(f"/projects/{project_id}/context?item_id={item_id}", status_code=303)


@app.post("/projects/{project_id}/context/{item_id}/obsolete")
async def obsolete_context(request: Request, project_id: str, item_id: str,
                           expected_version: int = Form(...), csrf_token: str = Form(...),
                           actor: tuple[User, LoginSession, str] = Depends(principal),
                           db: AsyncSession = Depends(db_session)):
    user, _, token = actor
    require_csrf(request, csrf_token, session_token=token)
    project = await project_for_read(db, user, project_id)
    await require_project_permission(db, user, project, "context.edit")
    if project.status == "archived":
        raise HTTPException(status_code=409, detail="Projet archivé.")
    item = await context_item_for_update(db, project_id, item_id)
    try:
        await obsolete_item(db, item, actor=user, expected_version=expected_version)
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    await db.commit()
    return RedirectResponse(f"/projects/{project_id}/context?item_id={item_id}", status_code=303)


@app.post("/projects/{project_id}/context/{item_id}/relations")
async def create_context_relation(request: Request, project_id: str, item_id: str,
                                  target_id: str = Form(...), type: str = Form(...),
                                  expected_version: int = Form(...), csrf_token: str = Form(...),
                                  actor: tuple[User, LoginSession, str] = Depends(principal),
                                  db: AsyncSession = Depends(db_session)):
    user, _, token = actor
    require_csrf(request, csrf_token, session_token=token)
    project = await project_for_read(db, user, project_id)
    await require_project_permission(db, user, project, "context.edit")
    if project.status == "archived":
        raise HTTPException(status_code=409, detail="Projet archivé.")
    if item_id == target_id:
        raise HTTPException(status_code=422, detail="Un élément ne peut pas se relier à lui-même.")
    rows = (await db.scalars(select(ContextItem).where(ContextItem.project_id == project_id,
        ContextItem.id.in_([item_id, target_id])).order_by(ContextItem.id).with_for_update())).all()
    by_id = {row.id: row for row in rows}
    if item_id not in by_id or target_id not in by_id:
        raise HTTPException(status_code=404)
    try:
        await add_relation(db, by_id[item_id], by_id[target_id], actor=user,
                           type=type, expected_version=expected_version)
    except ValueError as exc:
        raise HTTPException(status_code=409 if "rechargez" in str(exc) or "existe" in str(exc)
                            else 422, detail=str(exc)) from exc
    await db.commit()
    return RedirectResponse(f"/projects/{project_id}/context?item_id={item_id}", status_code=303)


@app.post("/projects/{project_id}/context/{item_id}/relations/{relation_id}/remove")
async def delete_context_relation(request: Request, project_id: str, item_id: str, relation_id: str,
                                  csrf_token: str = Form(...),
                                  actor: tuple[User, LoginSession, str] = Depends(principal),
                                  db: AsyncSession = Depends(db_session)):
    user, _, token = actor
    require_csrf(request, csrf_token, session_token=token)
    project = await project_for_read(db, user, project_id)
    await require_project_permission(db, user, project, "context.edit")
    if project.status == "archived":
        raise HTTPException(status_code=409, detail="Projet archivé.")
    relation = await db.scalar(select(ContextRelation).where(ContextRelation.id == relation_id,
        ContextRelation.project_id == project_id,
        ((ContextRelation.source_id == item_id) | (ContextRelation.target_id == item_id))).with_for_update())
    if relation is None:
        raise HTTPException(status_code=404)
    try:
        remove_relation(db, relation, actor=user)
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    await db.commit()
    return RedirectResponse(f"/projects/{project_id}/context?item_id={item_id}", status_code=303)


@app.post("/projects/{project_id}/context/{item_id}/comments")
async def create_context_comment(request: Request, project_id: str, item_id: str,
                                 body: str = Form(...), version: int = Form(...),
                                 csrf_token: str = Form(...),
                                 actor: tuple[User, LoginSession, str] = Depends(principal),
                                 db: AsyncSession = Depends(db_session)):
    user, _, token = actor
    require_csrf(request, csrf_token, session_token=token)
    project = await project_for_read(db, user, project_id)
    await require_project_permission(db, user, project, "context.comment")
    if project.status == "archived":
        raise HTTPException(status_code=409, detail="Projet archivé.")
    item = await context_item_for_update(db, project_id, item_id)
    if item.current_version != version:
        raise HTTPException(status_code=409, detail="Cet élément a changé ; rechargez la page.")
    snapshot = await db.scalar(select(ContextItemVersion).where(
        ContextItemVersion.item_id == item_id, ContextItemVersion.version == version))
    try:
        await add_comment(db, snapshot, actor=user, project_id=project_id, body=body)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    await db.commit()
    return RedirectResponse(f"/projects/{project_id}/context?item_id={item_id}", status_code=303)


@app.post("/projects/{project_id}/context/{item_id}/comments/{comment_id}/resolve")
async def resolve_context_comment(request: Request, project_id: str, item_id: str, comment_id: str,
                                  csrf_token: str = Form(...),
                                  actor: tuple[User, LoginSession, str] = Depends(principal),
                                  db: AsyncSession = Depends(db_session)):
    user, _, token = actor
    require_csrf(request, csrf_token, session_token=token)
    project = await project_for_read(db, user, project_id)
    await require_project_permission(db, user, project, "context.comment")
    if project.status == "archived":
        raise HTTPException(status_code=409, detail="Projet archivé.")
    comment = await db.scalar(select(ContextComment).join(ContextItemVersion,
        ContextComment.item_version_id == ContextItemVersion.id).join(ContextItem,
        ContextItemVersion.item_id == ContextItem.id).where(ContextComment.id == comment_id,
        ContextItem.id == item_id, ContextItem.project_id == project_id).with_for_update(of=ContextComment))
    if comment is None:
        raise HTTPException(status_code=404)
    try:
        resolve_comment(db, comment, actor=user, project_id=project_id, item_id=item_id)
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    await db.commit()
    return RedirectResponse(f"/projects/{project_id}/context?item_id={item_id}", status_code=303)


@app.get("/projects/{project_id}/generations")
async def generations(request: Request, project_id: str,
                      actor: tuple[User, LoginSession, str] = Depends(principal),
                      db: AsyncSession = Depends(db_session)):
    user, _, token = actor
    project = await project_for_read(db, user, project_id)
    items = (await db.scalars(select(ContextItem).where(
        ContextItem.project_id == project_id,
        ContextItem.status.notin_(["Rejeté", "Obsolète"]))
        .order_by(ContextItem.type, ContextItem.title))).all()
    runs = (await db.scalars(select(GenerationRun).where(
        GenerationRun.project_id == project_id).order_by(GenerationRun.created_at.desc())
        .limit(100))).all()
    deliverable_ids = {run.deliverable_id for run in runs}
    deliverables = {row.id: row for row in (await db.scalars(select(Deliverable).where(
        Deliverable.project_id == project_id, Deliverable.id.in_(deliverable_ids)))).all()} if deliverable_ids else {}
    versions = {}
    if deliverable_ids:
        for row in (await db.scalars(select(DeliverableVersion).where(
            DeliverableVersion.deliverable_id.in_(deliverable_ids))
            .order_by(DeliverableVersion.version))).all():
            versions[row.deliverable_id] = row
    profile = await default_profile(db)
    can_generate = project.status != "archived" and await project_permission(
        db, user, project_id, "generation.create")
    return page(request, "generations", user, token, project=project_data(project),
                deliverable_types=[{"code": code, "label": label} for code, label in DOCUMENT_TYPES.items()],
                document_styles=[{"code": code, "label": label} for code, label in DOCUMENT_STYLES.items()],
                context_items=[{"id": item.id, "title": item.title, "type": CONTEXT_TYPES[item.type],
                                "status": item.status, "version": item.current_version} for item in items],
                context_summary=f"{len(items)} élément(s) disponibles ; choisissez les sources du livrable.",
                unvalidated_context_count=sum(item.status != "Validé" for item in items),
                profile_available=profile is not None,
                generation_runs=[{"id": run.id, "title": deliverables[run.deliverable_id].title,
                                  "attempt": run.attempt_number, "status": run.status,
                                  "error_code": run.error_code,
                                  "is_regeneration": bool(run.parent_version_id),
                                  "reason": run.reason,
                                  "tokens_label": f"{run.input_tokens} entrée / {run.output_tokens} sortie" if run.input_tokens is not None and run.output_tokens is not None else "Non disponible",
                                  "cost_label": cost_label(run),
                                  "version_id": versions[run.deliverable_id].id if run.status == "completed" and run.deliverable_id in versions else None,
                                  "version_label": f"v{versions[run.deliverable_id].version}" if run.status == "completed" and run.deliverable_id in versions else None}
                                 for run in runs],
                permissions={"platform_admin": user.is_platform_admin,
                             "generation_create": can_generate})


async def dispatch_generation(db: AsyncSession, run: GenerationRun) -> None:
    try:
        from app.worker import generate_document
        generate_document.delay(run.id)
    except Exception:
        run.status, run.error_code, run.completed_at = "failed", "dispatch_unavailable", utcnow()
        audit(db, "generation.dispatch", "failed", actor_id=run.requested_by,
              project_id=run.project_id, target_type="generation_run", target_id=run.id,
              details={"error_code": "dispatch_unavailable"})
        await db.commit()


@app.post("/projects/{project_id}/generations")
async def create_generation(request: Request, project_id: str,
                            deliverable_type: str = Form(...), style: str = Form(...),
                            context_ids: list[str] = Form(...), csrf_token: str = Form(...),
                            actor: tuple[User, LoginSession, str] = Depends(principal),
                            db: AsyncSession = Depends(db_session)):
    user, _, token = actor
    require_csrf(request, csrf_token, session_token=token)
    project = await project_for_read(db, user, project_id)
    await require_project_permission(db, user, project, "generation.create")
    if project.status == "archived":
        raise HTTPException(status_code=409, detail="Projet archivé.")
    profile = await default_profile(db)
    if profile is None:
        raise HTTPException(status_code=409, detail="Configurez et vérifiez un profil LLM par défaut.")
    try:
        run = await start_generation(db, project_id=project_id, actor=user,
                                     kind=deliverable_type, style=style,
                                     item_ids=context_ids, profile=profile)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    await db.commit()
    await dispatch_generation(db, run)
    return RedirectResponse(f"/projects/{project_id}/generations", status_code=303)


@app.post("/projects/{project_id}/generations/{run_id}/retry")
async def retry_document(request: Request, project_id: str, run_id: str,
                         csrf_token: str = Form(...),
                         actor: tuple[User, LoginSession, str] = Depends(principal),
                         db: AsyncSession = Depends(db_session)):
    user, _, token = actor
    require_csrf(request, csrf_token, session_token=token)
    project = await project_for_read(db, user, project_id)
    await require_project_permission(db, user, project, "generation.create")
    if project.status == "archived":
        raise HTTPException(status_code=409, detail="Projet archivé.")
    old = await db.scalar(select(GenerationRun).where(
        GenerationRun.project_id == project_id, GenerationRun.id == run_id).with_for_update())
    if old is None:
        raise HTTPException(status_code=404)
    profile = await db.get(LLMProfileVersion, old.llm_profile_version_id)
    if profile is None or not profile_ready(profile):
        raise HTTPException(status_code=409, detail="Le profil LLM de cette tentative est indisponible.")
    try:
        run = await retry_generation(db, old, actor=user)
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    await db.commit()
    await dispatch_generation(db, run)
    return RedirectResponse(f"/projects/{project_id}/generations", status_code=303)


@app.get("/projects/{project_id}/reviews")
async def reviews(request: Request, project_id: str,
                  actor: tuple[User, LoginSession, str] = Depends(principal),
                  db: AsyncSession = Depends(db_session)):
    user, _, token = actor
    project = await project_for_read(db, user, project_id)
    rows = (await db.execute(select(DeliverableVersion, Deliverable).join(
        Deliverable, DeliverableVersion.deliverable_id == Deliverable.id).where(
        Deliverable.project_id == project_id,
        Deliverable.current_version == DeliverableVersion.version,
        DeliverableVersion.status.in_(["Brouillon IA", "À revoir"]))
        .order_by(DeliverableVersion.created_at.desc()))).all()
    return page(request, "reviews", user, token, project=project_data(project),
                review_items=[{"version_id": v.id, "title": v.title,
                               "version_label": f"v{v.version}", "status": v.status,
                               "expected_role": expected_role(d.type)} for v, d in rows])


async def version_provenance(db: AsyncSession, version: DeliverableVersion) -> dict:
    run = await db.get(GenerationRun, version.generation_run_id) if version.generation_run_id else None
    profile = await db.get(LLMProfileVersion, run.llm_profile_version_id) if run else None
    return {"prompt_version": run.prompt_version if run else "—",
            "model": f"{profile.provider} · {profile.model_name} (v{profile.version})" if profile else "—",
            "style": DOCUMENT_STYLES.get(run.parameters.get("style"), "—") if run else "—",
            "context": ", ".join(f"{item['title']} v{item['version']} ({item['status']})"
                                 for item in run.context_snapshot) if run else "—",
            "reason": run.reason if run and run.parent_version_id else None,
            "prompt_adjustment": run.prompt_adjustment if run and run.parent_version_id else None,
            "run_id": run.id if run else None}


@app.get("/projects/{project_id}/versions")
async def versions(request: Request, project_id: str, diff_id: str | None = None,
                   actor: tuple[User, LoginSession, str] = Depends(principal),
                   db: AsyncSession = Depends(db_session)):
    user, _, token = actor
    project = await project_for_read(db, user, project_id)
    await require_project_permission(db, user, project, "version.compare")
    rows = (await db.execute(select(DeliverableVersion, Deliverable, User).join(
        Deliverable, DeliverableVersion.deliverable_id == Deliverable.id).outerjoin(
        User, DeliverableVersion.actor_id == User.id).where(
        Deliverable.project_id == project_id).order_by(
        Deliverable.title, DeliverableVersion.version.desc()))).all()
    published = {p.version_id: p for p in (await db.scalars(select(PublicationRecord).join(
        Deliverable, PublicationRecord.deliverable_id == Deliverable.id).where(
        Deliverable.project_id == project_id))).all()}
    can_restore = project.status != "archived" and await project_permission(
        db, user, project_id, "generation.create")
    version_numbers = {v.id: v.version for v, _, _ in rows}
    items = [{"id": v.id, "title": v.title, "version_label": f"v{v.version}",
              "version_number": v.version,
              "status": "Obsolète" if v.id in published and not published[v.id].active else v.status,
              "created_at": v.created_at.strftime("%d/%m/%Y %H:%M"),
              "parent_label": f"v{version_numbers[v.parent_version_id]}" if v.parent_version_id in version_numbers else "—",
              "actor_name": author.display_name if author else "IA",
              "current_version": d.current_version,
              "can_restore": can_restore and v.version < d.current_version}
             for v, d, author in rows]
    diff_view = None
    if diff_id:
        diff_run = await db.scalar(select(DiffRun).where(
            DiffRun.id == diff_id, DiffRun.project_id == project_id))
        if diff_run is None:
            raise HTTPException(status_code=404)
        left, left_deliverable = await review_target(db, project_id, diff_run.left_version_id)
        right, right_deliverable = await review_target(db, project_id, diff_run.right_version_id)
        if left_deliverable.id != right_deliverable.id:
            raise HTTPException(status_code=404)
        diff_view = {"id": diff_run.id, "status": diff_run.status,
                     "error_code": diff_run.error_code, "result": diff_run.result,
                     "left_label": f"{left.title} v{left.version}",
                     "right_label": f"{right.title} v{right.version}",
                     "left_meta": await version_provenance(db, left),
                     "right_meta": await version_provenance(db, right)}
    return page(request, "versions", user, token, project=project_data(project),
                versions=items, diff_run=diff_view,
                permissions={"platform_admin": user.is_platform_admin,
                             "version_restore": can_restore})


@app.post("/projects/{project_id}/versions/diffs")
async def create_diff(request: Request, project_id: str, left: str = Form(...),
                      right: str = Form(...), csrf_token: str = Form(...),
                      actor: tuple[User, LoginSession, str] = Depends(principal),
                      db: AsyncSession = Depends(db_session)):
    user, _, token = actor
    require_csrf(request, csrf_token, session_token=token)
    project = await project_for_read(db, user, project_id)
    await require_project_permission(db, user, project, "version.compare")
    left_version, left_deliverable = await review_target(db, project_id, left)
    right_version, right_deliverable = await review_target(db, project_id, right)
    if left_deliverable.id != right_deliverable.id or left == right:
        raise HTTPException(status_code=422, detail="Choisissez deux versions distinctes du même livrable.")
    try:
        diff_run = await request_diff(db, project_id=project_id, actor=user,
                                      left=left_version, right=right_version)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    await db.commit()
    try:
        from app.worker import compute_diff
        compute_diff.delay(diff_run.id)
    except Exception:
        diff_run.status, diff_run.error_code, diff_run.completed_at = "failed", "dispatch_unavailable", utcnow()
        audit(db, "version.diff.dispatch", "failed", actor_id=user.id,
              project_id=project_id, target_type="diff_run", target_id=diff_run.id,
              details={"error_code": "dispatch_unavailable"})
        await db.commit()
    return RedirectResponse(f"/projects/{project_id}/versions?diff_id={diff_run.id}", status_code=303)


@app.post("/projects/{project_id}/versions/{version_id}/restore")
async def restore_document(request: Request, project_id: str, version_id: str,
                           expected_version: int = Form(...), confirm: str = Form(...),
                           csrf_token: str = Form(...),
                           actor: tuple[User, LoginSession, str] = Depends(principal),
                           db: AsyncSession = Depends(db_session)):
    user, _, token = actor
    require_csrf(request, csrf_token, session_token=token)
    project = await project_for_read(db, user, project_id)
    await require_project_permission(db, user, project, "generation.create")
    if project.status == "archived":
        raise HTTPException(status_code=409, detail="Projet archivé.")
    if confirm != "yes":
        raise HTTPException(status_code=422, detail="Confirmez la restauration.")
    source, deliverable = await review_target(db, project_id, version_id, lock=True)
    try:
        restored = await restore_version(db, deliverable, source, actor=user,
                                         expected_version=expected_version)
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    await db.commit()
    return RedirectResponse(f"/projects/{project_id}/reviews/{restored.id}", status_code=303)


@app.post("/projects/{project_id}/versions/{version_id}/regenerations")
async def regenerate_document(request: Request, project_id: str, version_id: str,
                              reason: str = Form(...), prompt_adjustment: str = Form(""),
                              context_ids: list[str] = Form(...), expected_version: int = Form(...),
                              csrf_token: str = Form(...),
                              actor: tuple[User, LoginSession, str] = Depends(principal),
                              db: AsyncSession = Depends(db_session)):
    user, _, token = actor
    require_csrf(request, csrf_token, session_token=token)
    project = await project_for_read(db, user, project_id)
    await require_project_permission(db, user, project, "generation.create")
    if project.status == "archived":
        raise HTTPException(status_code=409, detail="Projet archivé.")
    parent, deliverable = await review_target(db, project_id, version_id, lock=True)
    if parent.version != expected_version:
        raise HTTPException(status_code=409, detail="Ce livrable a changé ; rechargez la page.")
    profile = await default_profile(db)
    if profile is None:
        raise HTTPException(status_code=409, detail="Configurez et vérifiez un profil LLM par défaut.")
    try:
        run = await start_regeneration(db, deliverable, parent, actor=user,
                                       project_id=project_id, reason=reason,
                                       prompt_adjustment=prompt_adjustment,
                                       item_ids=context_ids, profile=profile)
    except ValueError as exc:
        raise HTTPException(status_code=409 if "rechargez" in str(exc) or "déjà en cours" in str(exc)
                            else 422, detail=str(exc)) from exc
    await db.commit()
    await dispatch_generation(db, run)
    return RedirectResponse(f"/projects/{project_id}/generations", status_code=303)


@app.get("/projects/{project_id}/reviews/{version_id}")
async def review_detail(request: Request, project_id: str, version_id: str,
                        actor: tuple[User, LoginSession, str] = Depends(principal),
                        db: AsyncSession = Depends(db_session)):
    user, _, token = actor
    project = await project_for_read(db, user, project_id)
    row = await db.execute(select(DeliverableVersion, Deliverable).join(
        Deliverable, DeliverableVersion.deliverable_id == Deliverable.id).where(
        Deliverable.project_id == project_id, DeliverableVersion.id == version_id))
    pair = row.first()
    if pair is None:
        raise HTTPException(status_code=404)
    version, deliverable = pair
    run = await db.get(GenerationRun, version.generation_run_id) if version.generation_run_id else None
    profile = await db.get(LLMProfileVersion, run.llm_profile_version_id) if run else None
    current = deliverable.current_version == version.version
    writable = project.status != "archived"
    can_edit = (writable and current and await project_permission(db, user, project_id, "generation.create"))
    can_comment = writable and await project_permission(db, user, project_id, "deliverable.comment")
    can_validate = (writable and current and version.status == "À revoir"
                    and await project_permission(db, user, project_id,
                                                 validator_permission(deliverable.type)))
    eligible_context = (await db.scalars(select(ContextItem).where(
        ContextItem.project_id == project_id,
        ContextItem.status.notin_(["Rejeté", "Obsolète"]))
        .order_by(ContextItem.type, ContextItem.title))).all() if can_edit and run else []
    regen_profile = await default_profile(db) if can_edit and run else None
    selected_context = {item["id"] for item in run.context_snapshot} if run else set()
    comments = (await db.execute(select(DeliverableComment, User).join(
        User, User.id == DeliverableComment.author_id).where(
        DeliverableComment.version_id == version.id)
        .order_by(DeliverableComment.created_at))).all()
    publication = await db.scalar(select(PublicationRecord).where(
        PublicationRecord.version_id == version.id)) if version.status == "Publié" else None
    effective_status = "Obsolète" if publication and not publication.active else version.status
    return page(request, "review_detail", user, token, project=project_data(project),
                version={"id": version.id, "title": version.title, "content": version.content,
                         "status": effective_status, "version_label": f"v{version.version}",
                         "version_number": version.version,
                         "parent_version_id": version.parent_version_id,
                         "created_at": version.created_at.strftime("%d/%m/%Y %H:%M"),
                         "source_label": f"{len(run.context_snapshot)} élément(s) de contexte" if run else "—",
                         "model_label": f"{profile.provider} · {profile.model_name} (v{profile.version})" if profile else "—",
                         "prompt_summary": run.prompt_version if run else "—",
                         "generation_run_id": run.id if run else None,
                         "cost_label": cost_label(run) if run else "Non disponible",
                         "parameters_summary": DOCUMENT_STYLES.get(run.parameters.get("style"), "—") if run else "—",
                         "regeneration_reason": run.reason if run else None,
                         "prompt_adjustment": run.prompt_adjustment if run else None,
                         "context_snapshot_label": ", ".join(
                             f"{item['title']} v{item['version']} ({item['status']})" for item in run.context_snapshot) if run else "—"},
                regeneration_context=[{"id": item.id, "title": item.title,
                                       "status": item.status, "version": item.current_version,
                                       "selected": item.id in selected_context}
                                      for item in eligible_context],
                regeneration_profile_available=regen_profile is not None,
                comments=[{"id": comment.id, "author_name": author.display_name,
                           "created_at": comment.created_at.strftime("%d/%m/%Y %H:%M"),
                           "text": comment.body, "status": comment.status} for comment, author in comments],
                permissions={"platform_admin": user.is_platform_admin,
                             "version_edit": can_edit,
                             "comment_create": can_comment,
                             "comment_resolve": can_comment,
                             "review_submit": can_edit and version.status == "Brouillon IA",
                             "review_validate": can_validate,
                             "review_reject": can_validate,
                             "regenerate": can_edit and run is not None}, versions_enabled=True)


async def review_target(db: AsyncSession, project_id: str, version_id: str,
                        *, lock: bool = False) -> tuple[DeliverableVersion, Deliverable]:
    version = await db.get(DeliverableVersion, version_id)
    if version is None:
        raise HTTPException(status_code=404)
    query = select(Deliverable).where(Deliverable.id == version.deliverable_id,
                                      Deliverable.project_id == project_id)
    if lock:
        query = query.with_for_update()
    deliverable = await db.scalar(query)
    if deliverable is None:
        raise HTTPException(status_code=404)
    return version, deliverable


@app.post("/projects/{project_id}/reviews/{version_id}/decision/{action}")
async def decide_review(request: Request, project_id: str, version_id: str, action: str,
                        csrf_token: str = Form(...), reason: str = Form(""),
                        actor: tuple[User, LoginSession, str] = Depends(principal),
                        db: AsyncSession = Depends(db_session)):
    if action not in {"submit", "validate", "reject"}:
        raise HTTPException(status_code=404)
    user, _, token = actor
    require_csrf(request, csrf_token, session_token=token)
    project = await project_for_read(db, user, project_id)
    version, deliverable = await review_target(db, project_id, version_id, lock=True)
    permission = "generation.create" if action == "submit" else validator_permission(deliverable.type)
    await require_project_permission(db, user, project, permission)
    if project.status == "archived":
        raise HTTPException(status_code=409, detail="Projet archivé.")
    try:
        decided = await transition(db, deliverable, version, actor=user, action=action, reason=reason)
    except ValueError as exc:
        audit(db, f"deliverable.{action}", "denied", actor_id=user.id,
              project_id=project_id, target_type="deliverable_version", target_id=version_id,
              details={"error_code": "invalid_transition"})
        await db.commit()
        raise HTTPException(status_code=409 if "rechargez" in str(exc) or "interdite" in str(exc)
                            else 422, detail=str(exc)) from exc
    await db.commit()
    return RedirectResponse(f"/projects/{project_id}/reviews/{decided.id}", status_code=303)


@app.post("/projects/{project_id}/reviews/{version_id}/comments")
async def comment_review(request: Request, project_id: str, version_id: str,
                         text: str = Form(...), csrf_token: str = Form(...),
                         actor: tuple[User, LoginSession, str] = Depends(principal),
                         db: AsyncSession = Depends(db_session)):
    user, _, token = actor
    require_csrf(request, csrf_token, session_token=token)
    project = await project_for_read(db, user, project_id)
    await require_project_permission(db, user, project, "deliverable.comment")
    if project.status == "archived":
        raise HTTPException(status_code=409, detail="Projet archivé.")
    version, _ = await review_target(db, project_id, version_id)
    try:
        await add_deliverable_comment(db, version, actor=user, project_id=project_id, body=text)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    await db.commit()
    return RedirectResponse(f"/projects/{project_id}/reviews/{version_id}", status_code=303)


@app.post("/projects/{project_id}/reviews/{version_id}/comments/{comment_id}/resolve")
async def resolve_review_comment(request: Request, project_id: str, version_id: str,
                                 comment_id: str, csrf_token: str = Form(...),
                                 actor: tuple[User, LoginSession, str] = Depends(principal),
                                 db: AsyncSession = Depends(db_session)):
    user, _, token = actor
    require_csrf(request, csrf_token, session_token=token)
    project = await project_for_read(db, user, project_id)
    await require_project_permission(db, user, project, "deliverable.comment")
    if project.status == "archived":
        raise HTTPException(status_code=409, detail="Projet archivé.")
    await review_target(db, project_id, version_id)
    comment = await db.scalar(select(DeliverableComment).where(
        DeliverableComment.id == comment_id,
        DeliverableComment.version_id == version_id).with_for_update())
    if comment is None:
        raise HTTPException(status_code=404)
    try:
        await resolve_deliverable_comment(db, comment, actor=user, project_id=project_id)
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    await db.commit()
    return RedirectResponse(f"/projects/{project_id}/reviews/{version_id}", status_code=303)


@app.get("/projects/{project_id}/publications")
async def publications(request: Request, project_id: str,
                       actor: tuple[User, LoginSession, str] = Depends(principal),
                       db: AsyncSession = Depends(db_session)):
    user, _, token = actor
    project = await project_for_read(db, user, project_id)
    valid_rows = (await db.execute(select(DeliverableVersion, Deliverable).join(
        Deliverable, DeliverableVersion.deliverable_id == Deliverable.id).where(
        Deliverable.project_id == project_id,
        Deliverable.current_version == DeliverableVersion.version,
        DeliverableVersion.status == "Validé").order_by(DeliverableVersion.created_at.desc()))).all()
    published_rows = (await db.execute(select(PublicationRecord, DeliverableVersion).join(
        DeliverableVersion, PublicationRecord.version_id == DeliverableVersion.id).join(
        Deliverable, PublicationRecord.deliverable_id == Deliverable.id).where(
        Deliverable.project_id == project_id, PublicationRecord.active.is_(True))
        .order_by(PublicationRecord.published_at.desc()))).all()
    export_rows = (await db.execute(select(ExportArtifact, PublicationRecord, DeliverableVersion).join(
        PublicationRecord, ExportArtifact.publication_id == PublicationRecord.id).join(
        DeliverableVersion, PublicationRecord.version_id == DeliverableVersion.id).join(
        Deliverable, PublicationRecord.deliverable_id == Deliverable.id).where(
        Deliverable.project_id == project_id, PublicationRecord.active.is_(True))
        .order_by(ExportArtifact.created_at.desc()))).all()
    can_publish = project.status != "archived" and await project_permission(
        db, user, project_id, "deliverable.publish")
    return page(request, "publications", user, token, project=project_data(project),
                publishable_versions=[{"id": v.id, "title": v.title,
                                       "version_label": f"v{v.version}", "status": v.status}
                                      for v, _ in valid_rows],
                published_versions=[{"id": pub.version_id, "title": v.title,
                                     "version_label": f"v{v.version}"} for pub, v in published_rows],
                exports=[{"id": artifact.id, "title": version.title,
                          "format": artifact.format, "status": artifact.status,
                          "created_at": artifact.created_at.strftime("%d/%m/%Y %H:%M")}
                         for artifact, _, version in export_rows],
                permissions={"platform_admin": user.is_platform_admin,
                             "deliverable_publish": can_publish})


@app.post("/projects/{project_id}/publications")
async def publish_review(request: Request, project_id: str, version_id: str = Form(...),
                         csrf_token: str = Form(...),
                         actor: tuple[User, LoginSession, str] = Depends(principal),
                         db: AsyncSession = Depends(db_session)):
    user, _, token = actor
    require_csrf(request, csrf_token, session_token=token)
    project = await project_for_read(db, user, project_id)
    await require_project_permission(db, user, project, "deliverable.publish")
    if project.status == "archived":
        raise HTTPException(status_code=409, detail="Projet archivé.")
    version, deliverable = await review_target(db, project_id, version_id, lock=True)
    try:
        await publish(db, deliverable, version, actor=user)
    except ValueError as exc:
        audit(db, "deliverable.publish", "denied", actor_id=user.id,
              project_id=project_id, target_type="deliverable_version", target_id=version_id,
              details={"error_code": "invalid_transition"})
        await db.commit()
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    await db.commit()
    return RedirectResponse(f"/projects/{project_id}/publications", status_code=303)


@app.post("/projects/{project_id}/exports")
async def create_export(request: Request, project_id: str, version_id: str = Form(...),
                        format: str = Form(...), csrf_token: str = Form(...),
                        actor: tuple[User, LoginSession, str] = Depends(principal),
                        db: AsyncSession = Depends(db_session)):
    user, _, token = actor
    require_csrf(request, csrf_token, session_token=token)
    project = await project_for_read(db, user, project_id)
    await require_project_permission(db, user, project, "deliverable.publish")
    if project.status == "archived":
        raise HTTPException(status_code=409, detail="Projet archivé.")
    version, _ = await review_target(db, project_id, version_id)
    publication = await db.scalar(select(PublicationRecord).where(
        PublicationRecord.version_id == version.id, PublicationRecord.active.is_(True)))
    if publication is None or version.status != "Publié":
        raise HTTPException(status_code=409, detail="Seule la référence publiée active peut être exportée.")
    try:
        artifact = await request_export(db, publication, actor=user,
                                        project_id=project_id, format=format)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    await db.commit()
    try:
        from app.worker import build_export
        build_export.delay(artifact.id)
    except Exception:
        artifact.status, artifact.error_code, artifact.completed_at = "failed", "dispatch_unavailable", utcnow()
        audit(db, "export.dispatch", "failed", actor_id=user.id, project_id=project_id,
              target_type="export_artifact", target_id=artifact.id,
              details={"error_code": "dispatch_unavailable"})
        await db.commit()
    return RedirectResponse(f"/projects/{project_id}/publications", status_code=303)


@app.get("/projects/{project_id}/exports/{artifact_id}/download")
async def download_export(request: Request, project_id: str, artifact_id: str,
                          actor: tuple[User, LoginSession, str] = Depends(principal),
                          db: AsyncSession = Depends(db_session)):
    user, _, _ = actor
    await project_for_read(db, user, project_id)
    row = (await db.execute(select(ExportArtifact, PublicationRecord).join(
        PublicationRecord, ExportArtifact.publication_id == PublicationRecord.id).join(
        Deliverable, PublicationRecord.deliverable_id == Deliverable.id).where(
        Deliverable.project_id == project_id, ExportArtifact.id == artifact_id))).first()
    if row is None:
        raise HTTPException(status_code=404)
    artifact, publication = row
    if artifact.status != "ready" or not publication.active or not artifact.object_key:
        raise HTTPException(status_code=409, detail="Cet export n'est pas disponible.")
    try:
        payload = await run_in_threadpool(ObjectStorage.from_env().get_bytes, artifact.object_key)
    except (StorageUnavailable, ValueError):
        raise HTTPException(status_code=503, detail="Stockage documentaire indisponible.") from None
    if hashlib.sha256(payload).hexdigest() != artifact.sha256:
        audit(db, "export.download", "failed", actor_id=user.id, project_id=project_id,
              target_type="export_artifact", target_id=artifact.id,
              details={"error_code": "integrity_mismatch"})
        await db.commit()
        raise HTTPException(status_code=409, detail="Intégrité de l'export invalide.")
    audit(db, "export.download", "success", actor_id=user.id, project_id=project_id,
          target_type="export_artifact", target_id=artifact.id)
    await db.commit()
    mime, extension = EXPORT_FORMATS[artifact.format]
    return no_store(Response(payload, media_type=mime,
                             headers={"Content-Disposition": f'attachment; filename="livrable-{artifact.id}.{extension}"'}))


@app.post("/projects/{project_id}/versions/{version_id}/revisions")
async def revise_document(request: Request, project_id: str, version_id: str,
                          content: str = Form(...), expected_version: int = Form(...),
                          csrf_token: str = Form(...),
                          actor: tuple[User, LoginSession, str] = Depends(principal),
                          db: AsyncSession = Depends(db_session)):
    user, _, token = actor
    require_csrf(request, csrf_token, session_token=token)
    project = await project_for_read(db, user, project_id)
    await require_project_permission(db, user, project, "generation.create")
    if project.status == "archived":
        raise HTTPException(status_code=409, detail="Projet archivé.")
    version = await db.get(DeliverableVersion, version_id)
    if version is None:
        raise HTTPException(status_code=404)
    deliverable = await db.scalar(select(Deliverable).where(
        Deliverable.id == version.deliverable_id, Deliverable.project_id == project_id)
        .with_for_update())
    if deliverable is None:
        raise HTTPException(status_code=404)
    try:
        revised = await revise_deliverable(db, deliverable, version, actor=user,
                                           content=content, expected_version=expected_version)
    except ValueError as exc:
        raise HTTPException(status_code=409 if "rechargez" in str(exc) else 422,
                            detail=str(exc)) from exc
    await db.commit()
    return RedirectResponse(f"/projects/{project_id}/reviews/{revised.id}", status_code=303)


@app.get("/projects/{project_id}/members")
async def members(request: Request, project_id: str,
                  actor: tuple[User, LoginSession, str] = Depends(principal),
                  db: AsyncSession = Depends(db_session)):
    user, _, token = actor
    project = await project_for_read(db, user, project_id)
    rows = (await db.execute(select(User, ProjectMember, Role.label).join(
        ProjectMember, ProjectMember.user_id == User.id,
    ).join(Role, Role.code == ProjectMember.role_code).where(
        ProjectMember.project_id == project_id, ProjectMember.is_active.is_(True),
    ).order_by(User.display_name))).all()
    items = [{"id": member.user_id, "display_name": target.display_name,
              "email": target.email, "role": role_label,
              "status": "Actif" if target.is_active else "Invité",
              "last_activity": target.last_login_at.strftime("%d/%m/%Y") if target.last_login_at else "—"}
             for target, member, role_label in rows]
    return page(request, "members", user, token, project=project_data(project), members=items,
                assignable_roles=[{"code": c, "label": l} for c, l in ROLE_LABELS.items()])


@app.post("/projects/{project_id}/members")
async def add_member(request: Request, project_id: str, email: str = Form(...), role: str = Form(...),
                     csrf_token: str = Form(...), actor: tuple[User, LoginSession, str] = Depends(principal),
                     db: AsyncSession = Depends(db_session)):
    user, _, token = actor
    require_csrf(request, csrf_token, session_token=token)
    project = await project_for_read(db, user, project_id)
    if not user.is_platform_admin:
        audit(db, "project.member.assign", "denied", actor_id=user.id, project_id=project.id)
        await db.commit()
        raise HTTPException(status_code=403)
    project = await lock_project(db, project_id)
    try:
        target_email = normalize_email(email)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    target = await db.scalar(select(User).where(User.email == target_email))
    if target is None:
        raise HTTPException(status_code=422, detail="Invitez d'abord cet utilisateur dans l'administration.")
    try:
        await assign_member(db, actor=user, project=project, user=target, role_code=role)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    await db.commit()
    return RedirectResponse(f"/projects/{project.id}/members", status_code=303)


@app.post("/projects/{project_id}/members/{target_id}/revoke")
async def revoke_member(request: Request, project_id: str, target_id: str, csrf_token: str = Form(...),
                        actor: tuple[User, LoginSession, str] = Depends(principal),
                        db: AsyncSession = Depends(db_session)):
    user, _, token = actor
    require_csrf(request, csrf_token, session_token=token)
    project = await project_for_read(db, user, project_id)
    if not user.is_platform_admin:
        audit(db, "project.member.revoke", "denied", actor_id=user.id, project_id=project.id)
        await db.commit()
        raise HTTPException(status_code=403)
    project = await lock_project(db, project_id)
    member = await db.scalar(select(ProjectMember).where(ProjectMember.project_id == project_id,
        ProjectMember.user_id == target_id, ProjectMember.is_active.is_(True)).with_for_update())
    if member is None:
        raise HTTPException(status_code=404)
    if member.user_id == project.owner_id:
        raise HTTPException(status_code=409, detail="Le responsable du projet doit rester membre.")
    member.is_active = False
    audit(db, "project.member.revoke", "success", actor_id=user.id, project_id=project.id,
          target_type="user", target_id=target_id, details={"old_role": member.role_code})
    await db.commit()
    return RedirectResponse(f"/projects/{project.id}/members", status_code=303)


@app.post("/projects/{project_id}/owner")
async def transfer_owner(request: Request, project_id: str, target_id: str = Form(...),
                         csrf_token: str = Form(...), actor: tuple[User, LoginSession, str] = Depends(principal),
                         db: AsyncSession = Depends(db_session)):
    user, _, token = actor
    require_csrf(request, csrf_token, session_token=token)
    project = await project_for_read(db, user, project_id)
    if not user.is_platform_admin:
        audit(db, "project.owner.transfer", "denied", actor_id=user.id, project_id=project.id)
        await db.commit()
        raise HTTPException(status_code=403)
    project = await lock_project(db, project_id)
    target = await db.get(User, target_id)
    member = await db.scalar(select(ProjectMember).where(
        ProjectMember.project_id == project.id, ProjectMember.user_id == target_id,
        ProjectMember.is_active.is_(True)).with_for_update())
    if target is None or not target.is_active or member is None:
        raise HTTPException(status_code=422, detail="Le nouveau responsable doit être un membre actif du projet.")
    if target.id == project.owner_id:
        raise HTTPException(status_code=409, detail="Cet utilisateur est déjà responsable.")
    previous_owner_id = project.owner_id
    previous_role = member.role_code
    project.owner_id = target.id
    member.role_code = "project_manager"
    audit(db, "project.owner.transfer", "success", actor_id=user.id, project_id=project.id,
          target_type="user", target_id=target.id,
          details={"previous_owner_id": previous_owner_id, "previous_role": previous_role,
                   "new_role": "project_manager"})
    await db.commit()
    return RedirectResponse(f"/projects/{project.id}/members", status_code=303)


async def require_admin(db: AsyncSession, user: User) -> None:
    if not user.is_platform_admin:
        audit(db, "platform.access", "denied", actor_id=user.id)
        await db.commit()
        raise HTTPException(status_code=403)


@app.get("/admin")
async def admin_home(actor: tuple[User, LoginSession, str] = Depends(principal),
                     db: AsyncSession = Depends(db_session)):
    await require_admin(db, actor[0])
    return RedirectResponse("/admin/users", status_code=303)


@app.get("/admin/users")
async def admin_users(request: Request, actor: tuple[User, LoginSession, str] = Depends(principal),
                      db: AsyncSession = Depends(db_session)):
    user, _, token = actor
    await require_admin(db, user)
    users = (await db.scalars(select(User).order_by(User.display_name))).all()
    return page(request, "admin_users", user, token, users=users)


@app.post("/admin/users")
async def invite_user(request: Request, email: str = Form(...), display_name: str = Form(...),
                      csrf_token: str = Form(...), actor: tuple[User, LoginSession, str] = Depends(principal),
                      db: AsyncSession = Depends(db_session)):
    user, _, token = actor
    require_csrf(request, csrf_token, session_token=token)
    await require_admin(db, user)
    try:
        invited, code = await create_invitation(db, actor=user, email=email, display_name=display_name)
    except ValueError as exc:
        users = (await db.scalars(select(User).order_by(User.display_name))).all()
        return page(request, "admin_users", user, token, users=users, error_message=str(exc))
    await db.commit()
    users = (await db.scalars(select(User).order_by(User.display_name))).all()
    return page(request, "admin_users", user, token, users=users,
                invitation={"email": invited.email, "code": code})


@app.post("/admin/users/{target_id}/deactivate")
async def deactivate_user(request: Request, target_id: str, csrf_token: str = Form(...),
                          actor: tuple[User, LoginSession, str] = Depends(principal),
                          db: AsyncSession = Depends(db_session)):
    user, _, token = actor
    require_csrf(request, csrf_token, session_token=token)
    await require_admin(db, user)
    if user.id == target_id:
        raise HTTPException(status_code=409, detail="Un administrateur ne peut pas se désactiver lui-même.")
    target = await db.get(User, target_id, with_for_update=True)
    if target is None:
        raise HTTPException(status_code=404)
    owned_project = await db.scalar(select(Project.id).where(Project.owner_id == target.id).limit(1))
    if owned_project:
        raise HTTPException(status_code=409, detail="Transférez d'abord les projets dont cet utilisateur est responsable.")
    target.is_active = False
    was_admin = target.is_platform_admin
    target.is_platform_admin = False
    sessions = await db.scalars(select(LoginSession).where(LoginSession.user_id == target.id,
                            LoginSession.revoked_at.is_(None)).with_for_update())
    for session in sessions:
        session.revoked_at = utcnow()
    memberships = await db.scalars(select(ProjectMember).where(
        ProjectMember.user_id == target.id, ProjectMember.is_active.is_(True)).with_for_update())
    for membership in memberships:
        membership.is_active = False
    audit(db, "user.deactivate", "success", actor_id=user.id, target_type="user", target_id=target.id,
          details={"was_platform_admin": was_admin})
    await db.commit()
    return RedirectResponse("/admin/users", status_code=303)


def llm_form_values(*, name: str, provider: str, model_name: str, base_url: str,
                    api_version: str, credential_env: str, temperature: str,
                    max_output_tokens: int, timeout_seconds: int, max_retries: int,
                    enabled: bool) -> dict:
    return {"name": name, "provider": provider, "model_name": model_name,
            "base_url": base_url, "api_version": api_version, "credential_env": credential_env,
            "temperature": temperature or None, "max_output_tokens": max_output_tokens,
            "timeout_seconds": timeout_seconds, "max_retries": max_retries,
            "enabled": enabled}


def llm_form_page(request: Request, user: User, token: str, form: dict, *,
                  profile_id: str | None = None, versions=None, error_message: str | None = None):
    safe_form = dict(form)
    reference = safe_form.get("credential_env")
    if reference and (not isinstance(reference, str) or not SECRET_PATTERN.fullmatch(reference)):
        safe_form["credential_env"] = ""
    return page(request, "llm_profile_form", user, token, form=safe_form, profile_id=profile_id,
                versions=versions or [], providers=PROVIDERS, allowed_urls=allowed_base_urls(),
                error_message=error_message)


async def llm_list_page(request: Request, db: AsyncSession, user: User, token: str,
                        *, error_message: str | None = None):
    profiles = await current_profiles(db)
    current_settings = await llm_settings(db)
    rows = []
    for profile in profiles:
        latest = await db.scalar(select(LLMConnectionCheck).where(
            LLMConnectionCheck.profile_version_id == profile.id,
        ).order_by(LLMConnectionCheck.created_at.desc()).limit(1))
        rows.append({"profile": profile, "ready": profile_ready(profile),
                     "credential_available": credential_available(profile),
                     "check_status": latest.outcome if latest else None,
                     "check_category": latest.error_category if latest else None,
                     "is_default": current_settings.default_version_id == profile.id})
    return page(request, "llm_settings", user, token, profiles=rows,
                allowed_urls=allowed_base_urls(), error_message=error_message)


@app.get("/admin/llm")
async def llm_list(request: Request, actor: tuple[User, LoginSession, str] = Depends(principal),
                   db: AsyncSession = Depends(db_session)):
    user, _, token = actor
    await require_admin(db, user)
    return await llm_list_page(request, db, user, token)


@app.get("/admin/llm/new")
async def llm_new(request: Request, actor: tuple[User, LoginSession, str] = Depends(principal),
                  db: AsyncSession = Depends(db_session)):
    user, _, token = actor
    await require_admin(db, user)
    return llm_form_page(request, user, token, {
        "name": "", "provider": "openai", "model_name": "", "base_url": "",
        "api_version": "", "credential_env": "", "temperature": "",
        "max_output_tokens": 2048, "timeout_seconds": 60, "max_retries": 1,
        "enabled": True,
    })


@app.post("/admin/llm")
async def llm_create(request: Request, name: str = Form(...), provider: str = Form(...),
                     model_name: str = Form(...), base_url: str = Form(""),
                     api_version: str = Form(""), credential_env: str = Form(""),
                     temperature: str = Form(""), max_output_tokens: int = Form(...),
                     timeout_seconds: int = Form(...), max_retries: int = Form(...),
                     enabled: bool = Form(False), csrf_token: str = Form(...),
                     actor: tuple[User, LoginSession, str] = Depends(principal),
                     db: AsyncSession = Depends(db_session)):
    user, _, token = actor
    require_csrf(request, csrf_token, session_token=token)
    await require_admin(db, user)
    values = llm_form_values(name=name, provider=provider, model_name=model_name,
        base_url=base_url, api_version=api_version, credential_env=credential_env,
        temperature=temperature, max_output_tokens=max_output_tokens,
        timeout_seconds=timeout_seconds, max_retries=max_retries, enabled=enabled)
    try:
        data = parse_form(values)
    except ValueError as exc:
        return llm_form_page(request, user, token, values, error_message=str(exc))
    await create_profile(db, user, data)
    await db.commit()
    return RedirectResponse("/admin/llm", status_code=303)


@app.get("/admin/llm/{profile_id}/edit")
async def llm_edit(request: Request, profile_id: str,
                   actor: tuple[User, LoginSession, str] = Depends(principal),
                   db: AsyncSession = Depends(db_session)):
    user, _, token = actor
    await require_admin(db, user)
    profile = await current_profile(db, profile_id)
    if profile is None:
        raise HTTPException(status_code=404)
    versions = (await db.scalars(select(LLMProfileVersion).where(
        LLMProfileVersion.profile_id == profile_id).order_by(LLMProfileVersion.version.desc()))).all()
    return llm_form_page(request, user, token, {
        "name": profile.name, "provider": profile.provider, "model_name": profile.model_name,
        "version": profile.version,
        "base_url": profile.base_url or "", "api_version": profile.api_version or "",
        "credential_env": profile.credential_env or "",
        "temperature": profile.temperature if profile.temperature is not None else "",
        "max_output_tokens": profile.max_output_tokens, "timeout_seconds": profile.timeout_seconds,
        "max_retries": profile.max_retries, "enabled": profile.enabled,
    }, profile_id=profile_id, versions=versions)


@app.post("/admin/llm/{profile_id}")
async def llm_update(request: Request, profile_id: str, name: str = Form(...), provider: str = Form(...),
                     model_name: str = Form(...), base_url: str = Form(""),
                     api_version: str = Form(""), credential_env: str = Form(""),
                     temperature: str = Form(""), max_output_tokens: int = Form(...),
                     timeout_seconds: int = Form(...), max_retries: int = Form(...),
                     enabled: bool = Form(False), version: int = Form(...),
                     csrf_token: str = Form(...),
                     actor: tuple[User, LoginSession, str] = Depends(principal),
                     db: AsyncSession = Depends(db_session)):
    user, _, token = actor
    require_csrf(request, csrf_token, session_token=token)
    await require_admin(db, user)
    values = llm_form_values(name=name, provider=provider, model_name=model_name,
        base_url=base_url, api_version=api_version, credential_env=credential_env,
        temperature=temperature, max_output_tokens=max_output_tokens,
        timeout_seconds=timeout_seconds, max_retries=max_retries, enabled=enabled)
    values["version"] = version
    try:
        data = parse_form(values)
    except ValueError as exc:
        return llm_form_page(request, user, token, values, profile_id=profile_id,
                             error_message=str(exc))
    try:
        revised = await revise_profile(db, user, profile_id, data, expected_version=version)
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    if revised is None:
        raise HTTPException(status_code=404)
    await db.commit()
    return RedirectResponse("/admin/llm", status_code=303)


@app.post("/admin/llm/{profile_id}/disable")
async def llm_disable(request: Request, profile_id: str, version: int = Form(...),
                      csrf_token: str = Form(...),
                      actor: tuple[User, LoginSession, str] = Depends(principal),
                      db: AsyncSession = Depends(db_session)):
    user, _, token = actor
    require_csrf(request, csrf_token, session_token=token)
    await require_admin(db, user)
    profile = await current_profile(db, profile_id, lock=True)
    if profile is None:
        raise HTTPException(status_code=404)
    data = LLMProfileInput.model_construct(
        name=profile.name, provider=profile.provider, model_name=profile.model_name,
        base_url=profile.base_url, api_version=profile.api_version,
        credential_env=profile.credential_env, temperature=profile.temperature,
        max_output_tokens=profile.max_output_tokens, timeout_seconds=profile.timeout_seconds,
        max_retries=profile.max_retries, enabled=False)
    try:
        await revise_profile(db, user, profile_id, data, expected_version=version)
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    await db.commit()
    return RedirectResponse("/admin/llm", status_code=303)


@app.post("/admin/llm/{profile_id}/check")
async def llm_check(request: Request, profile_id: str, csrf_token: str = Form(...),
                    actor: tuple[User, LoginSession, str] = Depends(principal),
                    db: AsyncSession = Depends(db_session)):
    user, _, token = actor
    require_csrf(request, csrf_token, session_token=token)
    await require_admin(db, user)
    profile = await current_profile(db, profile_id)
    if profile is None:
        raise HTTPException(status_code=404)
    await db.commit()
    await check_connection(db, user, profile)
    await db.commit()
    return RedirectResponse("/admin/llm", status_code=303)


@app.post("/admin/llm/{profile_id}/default")
async def llm_default(request: Request, profile_id: str, csrf_token: str = Form(...),
                      actor: tuple[User, LoginSession, str] = Depends(principal),
                      db: AsyncSession = Depends(db_session)):
    user, _, token = actor
    require_csrf(request, csrf_token, session_token=token)
    await require_admin(db, user)
    try:
        chosen = await choose_default(db, user, profile_id)
    except ValueError as exc:
        return await llm_list_page(request, db, user, token, error_message=str(exc))
    if chosen is None:
        raise HTTPException(status_code=404)
    await db.commit()
    return RedirectResponse("/admin/llm", status_code=303)
