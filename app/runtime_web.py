"""Listes et formulaires accessibles pour la gouvernance des runtimes."""
from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import RedirectResponse
from pydantic import ValidationError
from sqlalchemy import or_, select
from app.agent_api import db_session
from app.agent_sprint_web import web_actor, display_date
from app.agent_space_web import projects_for
from app.agent_catalog import CLIENT_CATALOG
from app.agent_models import AgentSession, AgentTask, AgentTeam, AgentTeamSkill, SkillProfile
from app.identity_service import audit, project_permission
from app.llm_models import LLMProfileVersion
from app import runtime_api as api, runtime_service as service
from app import budget_api, budget_service
from app.runtime_models import RuntimeBinding, RuntimeQuota, RuntimeSelection, RuntimeUsage, RuntimeVersion

router=APIRouter(prefix='/projects/{project_id}/agents/runtimes',tags=['runtime-pages'])
MESSAGES={
 'budget_still_blocking':'Le budget courant suspend encore ce plan. Révisez les limites ou attendez la période suivante avant une reprise humaine.',
 'suspension_unavailable':'Cette suspension n’appartient pas au projet.',
 'alert_unavailable':'Cette alerte n’appartient pas au projet.',
 'revision_stale':'La configuration a changé. Rechargez la page et vérifiez la version avant de réessayer.',
 'profile_unavailable':'Ce profil LLM doit être activé et sa version vérifiée par l’administrateur.',
 'runtime_unavailable':'Cette version de runtime n’appartient pas au projet.',
 'runtime_disabled':'Ce runtime est désactivé.',
 'scope_unavailable':'Ce périmètre ne fait pas partie du projet.',
 'sensitive_input':'Retirez les secrets et caractères de contrôle de la saisie.',
 'usage_not_active':'Cet appel est déjà clôturé ou inaccessible.',
 'stop_confirmation_required':'Confirmez l’arrêt effectif du worker ou client avant de libérer ce créneau.',
 'client_unavailable':'Choisissez un client du catalogue.',
}


async def data(db,project,scope,scope_id,offset):
    current=(await db.scalars(select(RuntimeVersion).where(RuntimeVersion.project_id==project.id,
        RuntimeVersion.is_current.is_(True)).order_by(RuntimeVersion.name))).all()
    versions=(await db.scalars(select(RuntimeVersion).where(RuntimeVersion.project_id==project.id)
        .order_by(RuntimeVersion.created_at.desc()).limit(50))).all()
    bindings=(await db.scalars(select(RuntimeBinding).where(RuntimeBinding.project_id==project.id)
        .order_by(RuntimeBinding.created_at.desc()).limit(100))).all()
    quotas=(await db.scalars(select(RuntimeQuota).where(RuntimeQuota.project_id==project.id)
        .order_by(RuntimeQuota.created_at.desc()).limit(100))).all()
    profiles=(await db.scalars(select(LLMProfileVersion).where(or_(LLMProfileVersion.is_current.is_(True),
        LLMProfileVersion.id.in_([r.profile_version_id for r in versions if r.profile_version_id])))
        .order_by(LLMProfileVersion.name).limit(200))).all()
    agents=(await db.execute(select(AgentTeamSkill.id,AgentTeam.name,SkillProfile.label,SkillProfile.version_number).join(AgentTeam,
        AgentTeam.id==AgentTeamSkill.team_id).join(SkillProfile,SkillProfile.id==AgentTeamSkill.skill_profile_id)
        .where(AgentTeam.project_id==project.id).limit(200))).all()
    options=[('project',project.id,project.name)]
    options += [('agent',r.id,f'{r.name} · {r.label} v{r.version_number}') for r in agents]
    for model,kind,label in ((AgentTask,'task','title'),(AgentSession,'session','client_code')):
        rows=(await db.scalars(select(model).where(model.project_id==project.id).order_by(model.created_at.desc()).limit(200))).all()
        options += [(kind,r.id,f'{getattr(r,label)} · {r.id}') for r in rows]
    # Une paire fournisseur/modèle garde la même identité entre versions.
    options += list({('model',service.model_key(p),f'{p.provider} / {p.model_name}') for p in profiles})
    calls=(await db.scalars(service.scope_query(project.id,scope,scope_id).order_by(RuntimeUsage.started_at.desc(),
        RuntimeUsage.id).offset(offset).limit(21))).all()
    selections=(await db.scalars(select(RuntimeSelection).where(RuntimeSelection.project_id==project.id)
        .order_by(RuntimeSelection.created_at.desc()).limit(20))).all()
    budget_data = await budget_service.snapshot(db, project.id)
    return dict(budget_data=budget_data,current=current,versions=versions,bindings=bindings,quotas=quotas,profiles=profiles,options=options,
                calls=calls[:20],selections=selections,totals=await service.totals(db,project.id,scope,scope_id),
                next_offset=offset+20 if len(calls)>20 else None)


async def render(request,db,actor,project,*,scope='project',scope_id=None,offset=0,edit=None,form=None,error=None,status=200):
    from app.main import page
    scope_id=scope_id or project.id
    try:
        await service.validate_scope(db,project.id,scope,scope_id)
    except service.RuntimeDenied:
        raise HTTPException(status_code=404) from None
    details=await data(db,project,scope,scope_id,offset)
    if edit:
        row=next((r for r in details['current'] if r.runtime_id==edit),None)
        if not row: raise HTTPException(status_code=404)
        if form is None:
            form={**service.view(row),'action':'runtime','expected_version':row.version}
    permissions={code:project.status!='archived' and await project_permission(db,actor[0],project.id,code)
                 for code in ('runtime.configure','quota.manage','budget.configure','agent.run.manage')}
    response=page(request,'agent_runtimes',actor[0],actor[2],project=project,view='runtimes',projects=await projects_for(db,actor[0]),
        scope=scope,scope_id=scope_id,form=form or {},permissions=permissions,clients=CLIENT_CATALOG,error_message=error,
        display_date=display_date,**details)
    response.status_code=status
    response.headers['Cache-Control']='no-store'
    return response


@router.get('')
async def home(request:Request,project_id:str,scope:api.Scope='project',scope_id:str|None=None,edit:str|None=None,
               filter_scope:str|None=None,offset:int=Query(default=0,ge=0,le=10000),db=Depends(db_session),actor=Depends(web_actor)):
    project=await api.authorize(db,actor,project_id)
    if filter_scope:
        parts=filter_scope.split(':')
        if len(parts)!=2 or parts[0] not in ('project','agent','task','session','model'):
            raise HTTPException(status_code=404)
        scope,scope_id=parts
    return await render(request,db,actor,project,scope=scope,scope_id=scope_id,edit=edit,offset=offset)


@router.post('')
async def save(request:Request,project_id:str,db=Depends(db_session),actor=Depends(web_actor)):
    raw=dict(await request.form())
    action=raw.get('action')
    if action not in ('runtime','binding','quota','reconcile','budget','acknowledge','resume','evaluate'):
        raise HTTPException(status_code=400)
    permission='runtime.configure' if action in ('runtime','binding') else 'quota.manage'
    if action in ('budget','acknowledge','evaluate'): permission='budget.configure'
    if action=='resume': permission='agent.run.manage'
    project=await api.authorize(db,actor,project_id,raw.get('csrf_token'),permission)
    values={k:v for k,v in raw.items() if k not in ('action','csrf_token','usage_id','scope_option')}
    try:
        if action=='evaluate':
            await budget_service.evaluate(db,project_id)
        elif action in ('acknowledge','resume'):
            payload=api.Input(reason=raw.get('reason',''))
            callback=budget_service.acknowledge if action=='acknowledge' else budget_service.resume
            await callback(db,project_id,raw.get('identity'),actor[0],payload.reason)
        elif action=='budget':
            values['scope'],values['scope_id'],version=raw.get('scope_option','').split(':')
            values['expected_version']=int(version)
            values['warning_percent']=int(values.get('warning_percent',80))
            values['currency']=values.get('currency') or None
            values['enabled']=raw.get('enabled')=='on'
            payload=budget_api.BudgetInput.model_validate(values)
            await budget_service.revise(db,project_id,actor[0],payload)
        elif action=='runtime':
            values['expected_version']=int(values.get('expected_version',0))
            values['timeout_seconds']=int(values.get('timeout_seconds',80))
            values['enabled']=raw.get('enabled')=='on'
            values['include_memory']=raw.get('include_memory')=='on'
            for key in ('runtime_id','profile_version_id','client_code'):
                values[key]=values.get(key) or None
            payload=api.RuntimeInput.model_validate(values)
            await service.revise_runtime(db,project_id,actor[0],payload)
        elif action in ('binding','quota'):
            values['scope'],values['scope_id'],version=raw.get('scope_option','').split(':')
            values['expected_version']=int(version)
            if action=='binding':
                values['runtime_version_id']=values.get('runtime_version_id') or None
                payload=api.BindingInput.model_validate(values)
            else:
                for key in ('max_tokens','max_seconds','max_concurrency','max_cost','currency'):
                    values[key]=values.get(key) or None
                for key in ('max_tokens','max_concurrency'):
                    if values[key] is not None: values[key]=int(values[key])
                payload=api.QuotaInput.model_validate(values)
            await service.revise_policy(db,project_id,actor[0],payload,quota=action=='quota')
        else:
            payload=api.ReconcileInput(reason=raw.get('reason',''),stopped_confirmed=raw.get('stopped_confirmed')=='on')
            await service.reconcile(db,project_id,raw.get('usage_id'),actor[0],payload.reason,payload.stopped_confirmed)
        await db.commit()
    except (ValueError,ValidationError) as exc:
        await db.rollback()
        await db.refresh(project)
        await db.refresh(actor[0])
        message=MESSAGES.get(exc.code,'Action refusée.') if isinstance(exc,service.RuntimeDenied) else 'Vérifiez les champs : type, bornes, profil ou client, et devise du coût.'
        audit(db,'runtime.form','denied',project_id=project_id,actor_id=actor[0].id,
              details={'action':action,'error_code':exc.code if isinstance(exc,service.RuntimeDenied) else 'invalid_input'})
        await db.commit()
        preserved=service.redact(raw)
        if action=='budget':
            preserved={**preserved,'enabled':raw.get('enabled')=='on'}
        elif action=='runtime':
            preserved={**preserved,'enabled':raw.get('enabled')=='on','include_memory':raw.get('include_memory')=='on'}
        return await render(request,db,actor,project,form=preserved,error=message,status=409)
    return RedirectResponse(f'/projects/{project_id}/agents/runtimes',status_code=303)
