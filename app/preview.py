"""Aperçu local en lecture seule des templates Jinja2.

Ce module ne constitue pas l'API métier de Floppy-MimIC. Les routes de
présentation utilisent uniquement des données locales de démonstration.
"""

from pathlib import Path

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates


APP_DIR = Path(__file__).resolve().parent
app = FastAPI(title="Floppy-MimIC · aperçu des templates")
app.mount("/static", StaticFiles(directory=APP_DIR / "static"), name="static")
templates = Jinja2Templates(directory=APP_DIR / "templates")

PROJECT = {
    "id": "preview",
    "name": "Projet de présentation",
    "description": "Aperçu des parcours IHM de la V1, sans données métier persistées.",
    "status": "Brouillon",
    "owner_name": "Équipe projet",
    "updated_at": "—",
}
SOURCE = {
    "id": "source-preview",
    "name": "Source de présentation",
    "kind": "Document",
    "processing_status": "completed",
    "created_at": "—",
    "mime_type": "application/pdf",
    "size_label": "—",
    "hash_short": "—",
    "analysis_status": "completed",
    "analysis_summary": "Résultat illustratif, sans appel LLM.",
}
VERSION = {
    "id": "version-preview",
    "title": "Livrable de présentation",
    "version_label": "v1",
    "status": "À revoir",
    "content": "Contenu de présentation. Une sortie IA réelle serait créée en Brouillon IA et soumise à une décision humaine.",
    "created_at": "—",
    "source_label": "Source de présentation",
    "model_label": "Aucun modèle appelé",
}
RUN = {"id": "run-preview", "status": "paused"}
PLAN = {
    "id": "plan-preview",
    "version": 1,
    "status": "review",
    "summary": "Plan de présentation en attente d'une approbation humaine.",
    "max_tasks": 4,
    "max_parallel": 2,
    "max_tokens": 100000,
    "parent_version": None,
}
TASK = {
    "id": "task-preview",
    "key": "AG-01",
    "title": "Analyser le besoin",
    "profile_name": "Analyste métier",
    "plan_version": 1,
    "status": "blocked",
    "description": "Tâche de présentation. Le plan attend une approbation humaine.",
    "dependencies_label": "Approbation du plan",
    "blocked_reason": "Plan non approuvé",
}


def render(request: Request, page: str, **data):
    context = {
        "preview_mode": True,
        "current_user": {"display_name": "Aperçu IHM"},
        "permissions": {},
        "csrf_token": "",
        "filters": {},
        "metrics": {},
        **data,
    }
    return templates.TemplateResponse(request, f"pages/{page}.html", context)


@app.get("/", include_in_schema=False)
def home():
    return RedirectResponse("/projects")


@app.get("/projects")
def projects(request: Request):
    return render(
        request,
        "projects",
        projects=[{**PROJECT, "role": "Chef de Projet"}],
        project_statuses=["Brouillon", "En cadrage", "Archivé"],
    )


@app.get("/projects/new")
def project_form(request: Request):
    return render(request, "project_form", form={})


@app.get("/admin")
def admin(request: Request):
    return render(request, "admin", llm_config={"provider": "Passerelle de présentation", "model": "Aucun modèle appelé"}, technical_events=[])


@app.get("/projects/{project_id}")
@app.get("/projects/{project_id}/{tail:path}")
def project_page(request: Request, project_id: str, tail: str = ""):
    if project_id != PROJECT["id"]:
        raise HTTPException(status_code=404)

    common = {"project": PROJECT}
    if tail == "":
        return render(request, "project", **common, attention_items=[])
    if tail == "sources":
        return render(request, "sources", **common, sources=[SOURCE], max_upload_size="10 Mo")
    if tail.startswith("sources/"):
        return render(request, "source_detail", **common, source=SOURCE)
    if tail == "context":
        item = {
            "id": "context-preview",
            "title": "Exigence de présentation",
            "type": "Exigence",
            "source_name": SOURCE["name"],
            "status": "Brouillon IA",
            "version": 1,
            "content": "Proposition illustrative à corriger et valider humainement.",
        }
        return render(request, "context", **common, context_items=[item], selected_item=item, context_types=["Exigence", "Contrainte", "Risque"])
    if tail == "workflows":
        flow = {
            "id": "flow-preview",
            "name": "Du besoin à la revue",
            "version_id": "flow-version-preview",
            "version_label": "v1",
            "latest_version_label": "v1",
            "status": "validated",
            "description": "Flux de présentation avec porte humaine et équipe IA.",
            "run_id": RUN["id"],
            "nodes": [
                {"label": "Analyser", "kind_label": "Équipe IA", "owner_role": "Chef de Projet", "inputs_label": "Besoin"},
                {"label": "Décider", "kind_label": "Validation humaine", "owner_role": "Product Owner", "inputs_label": "Proposition"},
            ],
        }
        return render(request, "workflows", **common, workflows=[flow], selected_flow=flow)
    if tail.startswith("workflows/") and tail.endswith("/edit"):
        flow = {
            "id": "flow-preview",
            "version_id": "flow-version-preview",
            "version_label": "v1",
            "status": "validated",
            "description": "Version de présentation en lecture seule.",
            "nodes": [
                {"label": "Analyser", "kind_label": "Équipe IA", "owner_role": "Chef de Projet", "inputs_label": "Besoin"},
                {"label": "Décider", "kind_label": "Validation humaine", "owner_role": "Product Owner", "inputs_label": "Proposition"},
            ],
        }
        catalog = [
            {"label": "Besoin", "description": "Entrée du flux"},
            {"label": "Équipe IA", "description": "Plan agentique approuvé avant exécution"},
            {"label": "Validation humaine", "description": "Porte de décision obligatoire"},
            {"label": "Publication", "description": "Version validée uniquement"},
        ]
        return render(request, "workflow_editor", **common, flow=flow, node_catalog=catalog, validation_errors=[])
    if tail == "pilotage":
        return render(
            request,
            "pilotage",
            **common,
            metrics={"running": 0, "human_decisions": 1, "failures": 0, "cost_label": "Non disponible", "cost_partial": True},
            activities=[{
                "title": "Approbation du plan agentique",
                "kind": "Décision humaine",
                "reference": "RUN-preview",
                "state": "Décision humaine",
                "document_status": None,
                "action_owner": "Chef de Projet",
                "href": "/projects/preview/agents",
            }],
        )
    if tail == "agents":
        return render(
            request,
            "agents",
            **common,
            run=RUN,
            plan=PLAN,
            profiles=[{"name": "Analyste métier", "version": 1, "role": "Analyse"}],
            next_actions=[{"owner": "Chef de Projet", "description": "Examiner le plan avant toute exécution"}],
            tasks=[TASK],
        )
    if tail.startswith("agents/runs/") and "/tasks/" in tail:
        return render(request, "agent_task", **common, run=RUN, task=TASK)
    if tail.startswith("agents/runs/") and tail.endswith("/edit"):
        editable_plan = {**PLAN, "tasks": [{"key": "AG-01", "title": "Analyser le besoin", "owner_profile_id": "analyst-preview", "dependency_keys": []}]}
        return render(request, "agent_plan_edit", **common, run=RUN, plan=editable_plan, profiles=[{"id": "analyst-preview", "name": "Analyste métier"}])
    if tail.startswith("agents/runs/") and tail.endswith("/diff"):
        return render(
            request,
            "agent_plan_diff",
            **common,
            run=RUN,
            previous_plan={"version": 0, "status": "superseded", "summary": "Plan initial de présentation."},
            current_plan=PLAN,
            changes=[{"kind": "Ajout", "description": "Analyse du besoin"}],
        )
    if tail == "generations":
        return render(
            request,
            "generations",
            **common,
            deliverable_types=[{"code": "use_cases", "label": "Cas d'usage"}, {"code": "architecture", "label": "Architecture"}],
            context_summary="Une proposition de contexte attend une validation humaine.",
            unvalidated_context_count=1,
            generation_runs=[],
        )
    if tail == "reviews":
        return render(request, "reviews", **common, review_items=[{"version_id": VERSION["id"], "title": VERSION["title"], "version_label": "v1", "status": "À revoir", "expected_role": "Product Owner"}])
    if tail.startswith("reviews/"):
        return render(request, "review_detail", **common, version=VERSION, comments=[])
    if tail == "versions":
        return render(request, "versions", **common, versions=[VERSION])
    if tail == "publications":
        return render(request, "publications", **common, publishable_versions=[], exports=[])
    if tail == "audit":
        return render(request, "audit", **common, events=[{"action": "Plan soumis à revue", "created_at": "—", "actor_label": "Système de présentation", "target_label": "Plan v1", "summary": "Aucune exécution réelle."}], audit_actions=["Plan soumis à revue"])
    if tail == "members":
        return render(request, "members", **common, members=[{"display_name": "Utilisateur de présentation", "role": "Chef de Projet", "status": "Actif"}], assignable_roles=[])
    raise HTTPException(status_code=404)
