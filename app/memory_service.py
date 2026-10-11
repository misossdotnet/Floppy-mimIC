"""Mémoire Human-in-the-Loop et récupération bornée de références projet."""
import re

from sqlalchemy import and_, or_, select

from app.agent_models import AgentTaskRun, AgentTask
from app.business_models import ContextItemVersion, Deliverable, DeliverableVersion, SourceDocument
from app.identity_models import utcnow
from app.identity_service import audit
from app.memory_models import ProjectMemory, ProjectMemoryGrant, ProjectMemoryVersion

VALIDATION_PERMISSIONS = {'convention':'memory.validate.technical','architecture_decision':'memory.validate.technical',
                          'business_constraint':'memory.validate.business','result':'memory.validate.result'}
CONTENT_FIELDS = ('title','content','source_note','source_refs','confidence','change_note')
SECRET_PATTERNS = (
    r'-----BEGIN (?:[A-Z ]+ )?PRIVATE KEY-----', r'\b(?:sk-[A-Za-z0-9_-]{20,}|AKIA[A-Z0-9]{16})\b',
    r'\bBearer\s+[A-Za-z0-9_.-]{16,}',
    r'(?i)\b(?:api[_-]?key|password|mot[ _-]de[ _-]passe|secret|token)\s*[:=]\s*[\"\']?[A-Za-z0-9_+/.-]{8,}',
)


def check_secrets(values):
    text='\n'.join(str(values.get(k,'')) for k in ('title','content','source_note','change_note'))
    if any(re.search(pattern,text) for pattern in SECRET_PATTERNS):
        raise ValueError('Contenu refusé : signature de secret reconnaissable. Utilisez une référence de configuration.')


async def check_sources(db, memory, values):
    check_secrets(values)
    refs=values.get('source_refs',[])
    if memory.scope=='global' and refs:
        raise ValueError('Une mémoire globale ne peut référencer des données privées de projet.')
    keys=[(r['type'],str(r['id'])) for r in refs]
    if len(keys)!=len(set(keys)): raise ValueError('Sources dupliquées.')
    for kind,rid in keys:
        if kind=='source_document': query=select(SourceDocument.id).where(SourceDocument.id==rid,SourceDocument.project_id==memory.project_id)
        elif kind=='context_item_version':
            from app.business_models import ContextItem
            query=select(ContextItemVersion.id).join(ContextItem,ContextItem.id==ContextItemVersion.item_id).where(
                ContextItemVersion.id==rid,ContextItem.project_id==memory.project_id)
        elif kind=='deliverable_version': query=select(DeliverableVersion.id).join(Deliverable,Deliverable.id==DeliverableVersion.deliverable_id).where(
            DeliverableVersion.id==rid,Deliverable.project_id==memory.project_id)
        elif kind=='agent_task_run': query=select(AgentTaskRun.id).join(AgentTask,AgentTask.id==AgentTaskRun.task_id).where(
            AgentTaskRun.id==rid,AgentTask.project_id==memory.project_id)
        else: raise ValueError('Type de source inconnu.')
        if not await db.scalar(query): raise ValueError('Source absente ou hors projet.')
    values['source_refs']=[{'type':kind,'id':rid} for kind,rid in keys]


async def head_version(db,memory):
    return await db.scalar(select(ProjectMemoryVersion).where(ProjectMemoryVersion.memory_id==memory.id,
        ProjectMemoryVersion.version_number==memory.current_version_number))


async def lock_memory(db,memory_id,project_id=None,scope='project'):
    memory=await db.scalar(select(ProjectMemory).where(ProjectMemory.id==memory_id,ProjectMemory.scope==scope,
        ProjectMemory.project_id==project_id).with_for_update().execution_options(populate_existing=True))
    if memory is None: raise ValueError('Mémoire introuvable.')
    return memory


def version_view(version):
    return {key:getattr(version,key) for key in ('id','memory_id','version_number','parent_version_id',*CONTENT_FIELDS,
        'status','action','actor_id','session_id','created_at')}


async def memory_view(db,memory):
    return {'id':memory.id,'project_id':memory.project_id,'scope':memory.scope,'kind':memory.kind,
        'current_version_number':memory.current_version_number,'reference_version_id':memory.reference_version_id,
        'created_by':memory.created_by,'created_at':memory.created_at,'current':version_view(await head_version(db,memory))}


async def append_version(db,memory,values,status,action,*,actor_id=None,session_id=None):
    parent=await head_version(db,memory) if memory.current_version_number else None
    memory.current_version_number+=1
    version=ProjectMemoryVersion(memory_id=memory.id,version_number=memory.current_version_number,
        parent_version_id=parent.id if parent else None,status=status,action=action,actor_id=actor_id,session_id=session_id,
        **{key:values[key] for key in CONTENT_FIELDS})
    db.add(version); await db.flush()
    if status=='Validé': memory.reference_version_id=version.id
    elif status=='Obsolète': memory.reference_version_id=None
    audit(db,f'memory.{action}','success',actor_id=actor_id,project_id=memory.project_id,
        target_type='project_memory',target_id=memory.id,details={'version_id':version.id,'version':version.version_number,
        'parent_version_id':version.parent_version_id,'status':status,'scope':memory.scope,'kind':memory.kind,'session_id':session_id})
    await db.flush()
    return version


async def create_memory(db,project_id,kind,values,owner_id,*,session_id=None):
    if project_id is None and kind!='convention':
        raise ValueError('La portée globale est réservée aux conventions autorisées.')
    memory=ProjectMemory(project_id=project_id,scope='project' if project_id else 'global',kind=kind,created_by=owner_id)
    await check_sources(db,memory,values)
    db.add(memory); await db.flush()
    await append_version(db,memory,values,'Brouillon IA' if session_id else 'À revoir','propose',
                         actor_id=None if session_id else owner_id,session_id=session_id)
    return memory


async def revise_memory(db,memory_id,project_id,expected_version,values,*,actor_id=None,session_id=None,scope='project'):
    memory=await lock_memory(db,memory_id,project_id,scope)
    if memory.current_version_number!=expected_version: raise ValueError('Version périmée : rechargez la mémoire.')
    await check_sources(db,memory,values)
    await append_version(db,memory,values,'Brouillon IA' if session_id else 'À revoir','revise',actor_id=actor_id,session_id=session_id)
    return memory


async def decide_memory(db,memory_id,project_id,expected_version,action,actor_id,reason='',scope='project'):
    memory=await lock_memory(db,memory_id,project_id,scope)
    head=await head_version(db,memory)
    status={'submit':'À revoir','approve':'Validé','reject':'Rejeté','retire':'Obsolète'}[action]
    if (head.action==action and head.change_note==reason.strip()
            and expected_version in {head.version_number,head.version_number-1}): return memory
    if memory.current_version_number!=expected_version: raise ValueError('Version périmée : rechargez la mémoire.')
    if action=='submit' and head.status!='Brouillon IA': raise ValueError('Soumission réservée au brouillon IA.')
    if action in {'approve','reject'} and head.status!='À revoir': raise ValueError('Version en revue requise.')
    if action=='retire' and memory.reference_version_id is None: raise ValueError('Aucune référence active à retirer.')
    if action in {'reject','retire'} and not reason.strip(): raise ValueError('Motif requis pour rejet ou retrait.')
    values={k:getattr(head,k) for k in CONTENT_FIELDS}; values['change_note']=reason.strip()
    check_secrets(values)
    await append_version(db,memory,values,status,action,actor_id=actor_id)
    return memory


def grant_view(grant,memory,version):
    usable=grant.revoked_at is None and memory.reference_version_id==version.id and version.status=='Validé'
    return {'id':grant.id,'project_id':grant.project_id,'memory_id':memory.id,'version_id':version.id,
        'version_number':version.version_number,'title':version.title,'kind':memory.kind,'usable':usable,
        'created_by':grant.created_by,'created_at':grant.created_at,'revoked_by':grant.revoked_by,'revoked_at':grant.revoked_at}


async def grant_memory(db,memory_id,project_id,expected_version,actor_id):
    memory=await lock_memory(db,memory_id,scope='global')
    head=await head_version(db,memory)
    if head.version_number!=expected_version or head.status!='Validé' or memory.reference_version_id!=head.id:
        raise ValueError('Partage réservé à la version courante validée.')
    prior=await db.scalar(select(ProjectMemoryGrant).where(ProjectMemoryGrant.project_id==project_id,
        ProjectMemoryGrant.memory_id==memory_id,ProjectMemoryGrant.revoked_at.is_(None)).with_for_update())
    if prior and prior.version_id==head.id: return prior,memory,head
    if prior:
        prior.revoked_by,prior.revoked_at=actor_id,utcnow()
        await db.flush()
    grant=ProjectMemoryGrant(project_id=project_id,memory_id=memory_id,version_id=head.id,created_by=actor_id)
    db.add(grant); await db.flush()
    audit(db,'memory.share','success',actor_id=actor_id,project_id=project_id,target_type='project_memory_grant',target_id=grant.id,
          details={'memory_id':memory_id,'version_id':head.id,'replaced_grant_id':prior.id if prior else None})
    return grant,memory,head


async def revoke_grant(db,project_id,grant_id,actor_id):
    grant=await db.get(ProjectMemoryGrant,grant_id)
    if not grant or grant.project_id!=project_id: raise ValueError('Partage introuvable.')
    memory=await lock_memory(db,grant.memory_id,scope='global')
    grant=await db.scalar(select(ProjectMemoryGrant).where(ProjectMemoryGrant.id==grant_id).with_for_update()
        .execution_options(populate_existing=True))
    if grant.revoked_at is None:
        grant.revoked_at,grant.revoked_by=utcnow(),actor_id
        audit(db,'memory.unshare','success',actor_id=actor_id,project_id=project_id,target_type='project_memory_grant',target_id=grant.id,
              details={'memory_id':grant.memory_id,'version_id':grant.version_id})
        await db.flush()
    return grant,memory,await db.get(ProjectMemoryVersion,grant.version_id)


async def project_grants(db,project_id):
    return (await db.execute(select(ProjectMemoryGrant,ProjectMemory,ProjectMemoryVersion).join(ProjectMemory,
        ProjectMemory.id==ProjectMemoryGrant.memory_id).join(ProjectMemoryVersion,ProjectMemoryVersion.id==ProjectMemoryGrant.version_id)
        .where(ProjectMemoryGrant.project_id==project_id).order_by(ProjectMemoryGrant.created_at,ProjectMemoryGrant.id))).all()


async def memory_context(db,project_id,*,q=None,kind=None,limit=20,max_chars=12000):
    # Un seul SELECT donne les références et partages du même instant ; aucune décision ou mutation.
    grants=select(ProjectMemoryGrant.version_id).where(ProjectMemoryGrant.project_id==project_id,
                                                     ProjectMemoryGrant.revoked_at.is_(None))
    query=select(ProjectMemory,ProjectMemoryVersion).join(ProjectMemoryVersion,ProjectMemoryVersion.id==ProjectMemory.reference_version_id).where(
        ProjectMemoryVersion.status=='Validé',or_(and_(ProjectMemory.scope=='project',ProjectMemory.project_id==project_id),
        and_(ProjectMemory.scope=='global',ProjectMemoryVersion.id.in_(grants))))
    if kind: query=query.where(ProjectMemory.kind==kind)
    if q: query=query.where(or_(ProjectMemoryVersion.title.icontains(q,autoescape=True),ProjectMemoryVersion.content.icontains(q,autoescape=True)))
    rows=(await db.execute(query.order_by(ProjectMemory.kind,ProjectMemory.id).limit(101))).all()
    refs=[]; chars=0; truncated=len(rows)>100
    for memory,version in rows[:100]:
        size=len(version.title)+len(version.content)+len(version.source_note)
        if len(refs)>=limit or chars+size>max_chars:
            truncated=True; continue
        chars+=size
        view=version_view(version)
        # Les motifs libres restent dans l'historique humain, pas dans le contexte injecté.
        view.pop('change_note')
        refs.append({'memory_id':memory.id,'scope':memory.scope,'kind':memory.kind,**view})
    return {'project_id':project_id,'usage':'reference_data_only','references':refs,'characters':chars,
        'max_characters':max_chars,'truncated':truncated,'as_of':utcnow()}
