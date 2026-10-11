"""Git CLI fermé et runner Docker : aucune commande/URL/chemin libre du client."""
import hashlib
import base64
import json
import os
from pathlib import Path, PurePosixPath
import re
import signal
import stat
import subprocess
import tempfile
import time
from urllib.parse import urlsplit

from app.llm_trace import redact

MAX_OUTPUT = 1024 * 1024
MAX_FILES = 200
MAX_BLOB = 256 * 1024
MAX_TREE = 8 * 1024 * 1024


class GitError(ValueError):
    def __init__(self, code):
        self.code = code
        super().__init__(code)


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False).encode()).hexdigest()


def minimize(value):
    text = redact(value)
    for key, secret in os.environ.items():
        if re.search(r'(SECRET|TOKEN|PASSWORD|ACCESS_KEY|AUTH_KEY)', key) and len(secret) >= 8:
            text = text.replace(secret, '[SECRET MASQUÉ]')
            text = text.replace(base64.b64encode(('x-access-token:'+secret).encode()).decode(), '[SECRET MASQUÉ]')
    text = re.sub(r'(?i)\bAuthorization:\s*Basic\s+[A-Za-z0-9+/=]+', '[SECRET MASQUÉ]', text)
    text = re.sub(r'[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]|\x1b\[[0-9;]*[A-Za-z]', '', text)
    return text


def safe_text(value):
    if minimize(value) != value:
        raise GitError('secret_or_control_character')


def minimize_log(value,entry):
    value=minimize(value)
    paths=[str(entry['_path']),str(entry['_root']),os.environ.get('HOME',''),os.environ.get('DOCKER_CONFIG','')]
    paths.extend(v['argv'][0] for v in entry.get('deployments',{}).values())
    for path in sorted(set(paths),key=len,reverse=True):
        if len(path)>1: value=value.replace(path,'[CHEMIN HÔTE MASQUÉ]')
    return value


def logical_path(value):
    path = PurePosixPath(value)
    if (not value or len(value) > 500 or path.is_absolute() or '\\' in value or
        any(part in ('', '.', '..') for part in value.split('/')) or
        any(ord(c) < 32 or ord(c) == 127 for c in value) or ':' in value or
        '.git' in path.parts):
        raise GitError('unsafe_path')
    safe_text(value)
    return path


def confined(root, relative, *, exists=True):
    path = root / logical_path(relative)
    current = root
    for part in path.relative_to(root).parts:
        current = current / part
        if current.is_symlink(): raise GitError('symlink_forbidden')
    if exists and not path.exists(): raise GitError('path_missing')
    if not path.resolve().is_relative_to(root): raise GitError('unsafe_path')
    return path


def secure_bytes(root,relative):
    logical_path(relative)
    current=os.open(root,os.O_RDONLY|os.O_DIRECTORY|os.O_NOFOLLOW)
    try:
        parts=relative.split('/')
        for part in parts[:-1]:
            child=os.open(part,os.O_RDONLY|os.O_DIRECTORY|os.O_NOFOLLOW,dir_fd=current)
            os.close(current);current=child
        fd=os.open(parts[-1],os.O_RDONLY|os.O_NOFOLLOW|os.O_NONBLOCK,dir_fd=current)
        try:
            info=os.fstat(fd)
            if not stat.S_ISREG(info.st_mode) or info.st_size>MAX_BLOB: raise GitError('unsafe_file')
            with os.fdopen(fd,'rb',closefd=False) as stream: blob=stream.read(MAX_BLOB+1)
            if len(blob)>MAX_BLOB: raise GitError('workspace_limit')
            return blob
        finally: os.close(fd)
    except OSError: raise GitError('workspace_changed_or_unsafe') from None
    finally: os.close(current)


def root_path(env):
    value = os.environ.get(env, '')
    if not value or not Path(value).is_absolute(): raise GitError('git_configuration_missing')
    path = Path(value)
    if path.resolve() != path or not path.is_dir(): raise GitError('unsafe_root')
    return path


def sha(value):
    if not re.fullmatch(r'[0-9a-f]{40}|[0-9a-f]{64}', value): raise GitError('full_commit_required')
    return value


def branch(value, *, task_branch=False):
    if (not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9/_-]{0,179}', value) or '..' in value or
        '//' in value or value.endswith('/') or (task_branch and not value.startswith('codex/'))):
        raise GitError('invalid_branch')
    return value


def catalog(key=None):
    try:
        raw = json.loads(os.environ.get('MIMIC_GIT_CATALOG', '{}'))
        if not isinstance(raw, dict) or len(raw) > 100: raise ValueError()
        if key is None: return sorted(k for k in raw if re.fullmatch(r'[a-z0-9][a-z0-9_-]{0,79}', k))
        entry = raw[key]
        if not isinstance(entry, dict) or not set(entry) <= {'path', 'remote', 'auth_env', 'tests', 'deployments', 'test_paths'}:
            raise ValueError()
        repos, trees = root_path('MIMIC_GIT_ROOT'), root_path('MIMIC_WORKTREE_ROOT')
        if repos.is_relative_to(trees) or trees.is_relative_to(repos): raise GitError('roots_must_be_separate')
        path = confined(repos, entry['path'])
        if not path.is_dir(): raise GitError('repository_missing')
        gitdir = path / '.git'
        if gitdir.exists() and (gitdir.is_symlink() or not gitdir.is_dir()): raise GitError('linked_repository_forbidden')
        for env in (entry.get('auth_env'),):
            if env and not re.fullmatch(r'MIMIC_GIT_SECRET_[A-Z0-9_]{1,80}', env): raise ValueError()
        remote = entry.get('remote')
        if remote:
            u = urlsplit(remote)
            if u.scheme in ('https','http'):
                if (u.username or u.password or u.query or u.fragment or not u.hostname or
                    (u.scheme == 'http' and os.environ.get('MIMIC_GIT_ALLOW_HTTP') != 'true')):
                    raise GitError('remote_forbidden')
            elif u.scheme or not Path(remote).is_absolute(): raise GitError('remote_forbidden')
            else:
                if not Path(remote).is_relative_to(repos): raise GitError('remote_forbidden')
                confined(repos, str(Path(remote).relative_to(repos)))
        safe_text(json.dumps(entry))
        for command in entry.get('tests', {}).values():
            if set(command) != {'image', 'argv'} or not re.fullmatch(r'(?:[a-zA-Z0-9/_.:-]+@)?sha256:[0-9a-f]{64}', command['image']): raise ValueError()
            validate_argv(command['argv'])
        for deploy in entry.get('deployments', {}).values():
            if not set(deploy) <= {'argv','env_refs'}: raise ValueError()
            validate_argv(deploy['argv'])
            program = Path(deploy['argv'][0])
            if not program.is_absolute() or program.resolve() != program or not program.is_file() or program.is_relative_to(repos) or program.is_relative_to(trees):
                raise GitError('deployment_adapter_forbidden')
            for name, ref in deploy.get('env_refs', {}).items():
                if not re.fullmatch(r'[A-Z][A-Z0-9_]{0,60}',name) or not re.fullmatch(r'MIMIC_GIT_SECRET_[A-Z0-9_]{1,80}',ref): raise ValueError()
        test_paths = entry.get('test_paths', ['tests'])
        if not 1 <= len(test_paths) <= 20: raise ValueError()
        for prefix in test_paths: logical_path(prefix)
        adapter_hashes={k:hashlib.sha256(Path(v['argv'][0]).read_bytes()).hexdigest() for k,v in entry.get('deployments',{}).items()}
        return {**entry, '_path': path, '_root': trees, '_fingerprint': digest({'entry':entry,'root':str(repos),'trees':str(trees),'adapters':adapter_hashes})}
    except (KeyError, TypeError, ValueError) as exc:
        if isinstance(exc, GitError): raise
        raise GitError('invalid_git_catalog') from None


def validate_argv(argv):
    if not isinstance(argv,list) or not 1 <= len(argv) <= 30 or any(not isinstance(v,str) or not v or len(v)>500 or '\x00' in v for v in argv):
        raise GitError('invalid_command_catalog')


def process(argv, *, cwd=None, env=None, timeout=30, limit=MAX_OUTPUT):
    """Lecture incrémentale bornée ; tue le groupe, jamais d'erreur subprocess brute."""
    import selectors
    result = bytearray()
    started = time.monotonic()
    try:
        p = subprocess.Popen(argv, cwd=cwd, env=env, stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, start_new_session=True)
    except OSError: raise GitError('tool_unavailable') from None
    selector = selectors.DefaultSelector()
    selector.register(p.stdout, selectors.EVENT_READ)
    try:
        while selector.get_map():
            if time.monotonic() - started > timeout: raise GitError('tool_timeout')
            for k, _ in selector.select(0.1):
                chunk = os.read(k.fd, 8192)
                if not chunk: selector.unregister(k.fileobj)
                result.extend(chunk)
                if len(result) > limit: raise GitError('output_limit')
        code = p.wait(timeout=max(0.1, timeout - (time.monotonic()-started)))
        return code, bytes(result), int((time.monotonic()-started)*1000)
    except (GitError, subprocess.TimeoutExpired):
        try: os.killpg(p.pid, signal.SIGKILL)
        except ProcessLookupError: pass
        p.wait()
        raise GitError('output_limit' if len(result)>limit else 'tool_timeout') from None
    finally:
        selector.close(); p.stdout.close()


def git(path, *args, entry=None, ok=True):
    with tempfile.TemporaryDirectory(prefix='mimic-git-home-') as home:
        env = {'PATH':os.defpath, 'HOME':home, 'LANG':'C.UTF-8', 'LC_ALL':'C',
            'GIT_CONFIG_NOSYSTEM':'1','GIT_CONFIG_GLOBAL':os.devnull,
            'GIT_TERMINAL_PROMPT':'0','GIT_OPTIONAL_LOCKS':'0','GIT_NO_REPLACE_OBJECTS':'1'}
        if entry and entry.get('auth_env') and args[0] in ('push','ls-remote'):
            token = os.environ.get(entry['auth_env'])
            if not token: raise GitError('git_credential_missing')
            if any(ord(c)<32 or ord(c)==127 for c in token): raise GitError('git_credential_invalid')
            # Git Smart HTTP Gitea vérifie le PAT comme mot de passe Basic, pas comme header API « token ».
            header='Authorization: Basic '+base64.b64encode(('x-access-token:'+token).encode()).decode()
            env.update(GIT_CONFIG_COUNT='1', GIT_CONFIG_KEY_0='http.'+entry['remote']+'.extraHeader',
                       GIT_CONFIG_VALUE_0=header)
        argv = ['git','--no-pager','--literal-pathspecs','-c','core.hooksPath='+os.devnull,
            '-c','core.fsmonitor=false','-c','core.attributesFile='+os.devnull,'-c','color.ui=false',
            '-c','credential.helper=','-c','protocol.ext.allow=never','-c','protocol.file.allow=always',
            '-c','http.followRedirects=false','-c','submodule.recurse=false','-C',str(path),*args]
        if entry and path != entry['_path'] and entry.get('_gitdir'):
            argv[argv.index('-C'):argv.index('-C')+2]=['--git-dir='+str(entry['_gitdir']),'--work-tree='+str(path)]
        code,out,_ = process(argv,env=env)
        if ok and code: raise GitError('git_command_failed')
        return out if ok else (code,out)


def check_repo(entry):
    path = entry['_path']
    if git(path,'rev-parse','--is-bare-repository').strip() == b'true':
        common = path
    else:
        if git(path,'rev-parse','--show-toplevel').strip().decode() != str(path): raise GitError('repository_identity_changed')
        common = path / '.git'
    if common.is_symlink() or common.resolve() != common: raise GitError('unsafe_git_directory')
    # Un fichier attributes du dépôt peut invoquer un filtre lors du checkout.
    out = git(path,'config','--local','--no-includes','--name-only','--list')
    harmless=r'^(core\.(repositoryformatversion|filemode|bare|logallrefupdates|ignorecase|precomposeunicode)|remote\.[A-Za-z0-9_-]+\.(url|fetch|pushurl)|branch\..+\.(remote|merge)|user\.(name|email)|init\.defaultbranch)$'
    forbidden = [line for line in out.decode(errors='replace').splitlines() if not re.fullmatch(harmless,line)]
    if forbidden: raise GitError('unsafe_git_config')
    if (common/'objects'/'info'/'alternates').exists() or (common/'info'/'grafts').exists(): raise GitError('external_git_objects')
    return common


def tree_path(entry, worktree, *, exists=True):
    rel = f'{worktree.project_id}/{worktree.id}'
    return confined(entry['_root'],rel,exists=exists)


def check_tree(entry, worktree):
    common = check_repo(entry)
    path = tree_path(entry,worktree)
    marker = path / '.git'
    if not marker.is_file() or marker.is_symlink(): raise GitError('worktree_identity_changed')
    actual = marker.read_text().strip()
    target = Path(actual.removeprefix('gitdir: '))
    if not actual.startswith('gitdir: ') or not target.is_absolute() or target != common/'worktrees'/worktree.id or target.resolve()!=target:
        raise GitError('worktree_identity_changed')
    entry['_gitdir']=target
    if git(path,'rev-parse','--show-toplevel',entry=entry).strip().decode() != str(path): raise GitError('worktree_identity_changed')
    if git(path,'symbolic-ref','--short','HEAD',entry=entry).strip().decode() != worktree.branch: raise GitError('worktree_branch_changed')
    return path


def workspace_manifest(path):
    # Tous les fichiers, même ignorés, sont contrôlés avant lecture/exécution.
    manifest = {}; total=0; seen=0; tree_unsafe=False
    for current, dirs, files in os.walk(path,followlinks=False):
        for name in dirs+files:
            candidate=Path(current)/name; relative=candidate.relative_to(path).as_posix()
            if relative == '.git': continue
            logical_path(relative)
            if candidate.is_symlink(): raise GitError('symlink_forbidden')
            if name=='.git' or (candidate.is_dir() and (candidate/'.git').exists()): raise GitError('nested_repository_forbidden')
            if candidate.is_file():
                seen+=1; size=candidate.stat().st_size; total+=size
                if seen>2000 or size>MAX_BLOB or total>MAX_TREE: raise GitError('workspace_limit')
                blob=secure_bytes(path,relative)
                manifest[relative]=hashlib.sha256(blob).hexdigest()
                try: plain=blob.decode('utf-8')
                except UnicodeDecodeError: plain=''; tree_unsafe=True
                tree_unsafe |= minimize(plain)!=plain or bool(re.search(r'(^|/)(\.env(?:\..*)?|credentials[^/]*|id_rsa|id_ed25519)$|\.(pem|key|p12|pfx)$',relative,re.I))
            elif not candidate.is_dir(): raise GitError('special_file_forbidden')
    return manifest,tree_unsafe


def inspect_tree(entry, worktree):
    path = check_tree(entry,worktree)
    def read_git(*args,**kw): return git(path,*args,entry=entry,**kw)
    head = sha(read_git('rev-parse','HEAD').strip().decode())
    sha(worktree.base_commit)
    if read_git('merge-base','--is-ancestor',worktree.base_commit,head,ok=False)[0]: raise GitError('base_not_ancestor')
    manifest,tree_unsafe=workspace_manifest(path)
    modes=read_git('ls-files','--stage','-z').split(b'\0')
    if any(row.startswith((b'120000 ',b'160000 ')) for row in modes): raise GitError('symlink_or_submodule_forbidden')
    dirty=bool(read_git('status','--porcelain','--untracked-files=all'))
    changed=read_git('diff','--name-only','-z',worktree.base_commit,'--').split(b'\0')
    untracked=read_git('ls-files','--others','--exclude-standard','-z').split(b'\0')
    paths=sorted(set(v.decode('utf-8','strict') for v in changed+untracked if v))
    if len(paths)>MAX_FILES: raise GitError('changed_files_limit')
    files=[]; unsafe=tree_unsafe
    for relative in paths:
        logical_path(relative)
        candidate=confined(path,relative,exists=False)
        sensitive=bool(re.search(r'(^|/)(\.env(?:\..*)?|credentials[^/]*|id_rsa|id_ed25519)$|\.(pem|key|p12|pfx)$',relative,re.I))
        if candidate.exists(): content=secure_bytes(path,relative)
        else: content=b''
        binary=b'\0' in content
        try: text=content.decode('utf-8')
        except UnicodeDecodeError: text='';binary=True
        raw=read_git('diff','--no-ext-diff','--no-textconv','--no-renames','--unified=3',worktree.base_commit,'--',relative).decode('utf-8',errors='replace')
        if relative in [v.decode() for v in untracked if v]:
            import difflib
            raw=''.join(difflib.unified_diff([],text.splitlines(keepends=True),fromfile='/dev/null',tofile=relative))
        masked=sensitive or minimize(raw)!=raw or minimize(text)!=text
        unsafe |= masked or binary
        safe='[CONTENU MASQUÉ]' if masked else '[FICHIER BINAIRE]' if binary else raw
        hunks=[]
        if not (masked or binary):
            chunks=re.split(r'(?m)(?=^@@ )',safe)
            for chunk in chunks[1:]:
                hunks.append({'id':digest({'file':relative,'diff':chunk}),'header':chunk.splitlines()[0],'diff':chunk})
        files.append({'path':relative,'diff':safe,'hunks':hunks,'masked':bool(masked),'binary':binary})
    commits=read_git('log','--format=%H%x00%B%x00','--max-count=101',worktree.base_commit+'..'+head).split(b'\0')
    log=[]
    for i in range(0,len(commits)-1,2):
        commit=commits[i].strip().decode()
        if commit:
            message=commits[i+1].decode(errors='replace')
            safe=minimize(message);unsafe |= safe!=message
            log.append({'id':sha(commit),'subject':safe.splitlines()[0][:180] if safe.splitlines() else ''})
    if len(log)>100: raise GitError('commits_limit')
    scanned=0
    for commit in log:
        changed_blobs=read_git('diff-tree','--no-commit-id','--name-only','-r','-m','-z',commit['id']).split(b'\0')
        for filename in changed_blobs:
            if not filename: continue
            name=filename.decode('utf-8');logical_path(name)
            code,size=read_git('cat-file','-s',commit['id']+':'+name,ok=False)
            if code: continue
            scanned+=int(size)
            if int(size)>MAX_BLOB or scanned>MAX_TREE: raise GitError('history_limit')
            blob=read_git('show',commit['id']+':'+name)
            try: content=blob.decode('utf-8')
            except UnicodeDecodeError: content='';unsafe=True
            unsafe |= minimize(content)!=content or bool(re.search(r'(^|/)(\.env(?:\..*)?|credentials[^/]*|id_rsa|id_ed25519)$|\.(pem|key|p12|pfx)$',name,re.I))
    final_manifest,_=workspace_manifest(path)
    if (manifest!=final_manifest or read_git('rev-parse','HEAD').strip().decode()!=head or
        bool(read_git('status','--porcelain','--untracked-files=all'))!=dirty):
        raise GitError('workspace_changed_during_collection')
    result={'head':head,'fingerprint':digest({'head':head,'files':manifest}), 'dirty':dirty,
        'files':files,'commits':log,'unsafe':unsafe}
    if len(json.dumps(result).encode())>MAX_OUTPUT: raise GitError('snapshot_limit')
    return result


def create_tree(entry, worktree):
    check_repo(entry); sha(worktree.base_commit); branch(worktree.branch,task_branch=True)
    path=tree_path(entry,worktree,exists=False)
    git(entry['_path'],'cat-file','-e',worktree.base_commit+'^{commit}')
    # Contrôle des modes avant checkout : un symlink ne doit jamais être créé.
    modes=git(entry['_path'],'ls-tree','-r','-l',worktree.base_commit)
    sizes=[int(row.split()[3]) for row in modes.splitlines() if row.split()[3]!=b'-']
    if len(sizes)>2000 or any(n>MAX_BLOB for n in sizes) or sum(sizes)>MAX_TREE: raise GitError('workspace_limit')
    if any(row.startswith((b'120000 ',b'160000 ')) for row in modes.splitlines()): raise GitError('symlink_or_submodule_forbidden')
    if path.exists(): raise GitError('worktree_already_exists')
    path.parent.mkdir(exist_ok=True)
    git(entry['_path'],'worktree','add','-b',worktree.branch,str(path),worktree.base_commit)
    return {'head':worktree.base_commit}


def docker_env():
    return {k:v for k,v in os.environ.items() if k in ('PATH','HOME','DOCKER_HOST','DOCKER_CONTEXT','DOCKER_CONFIG')}

def run_test(entry,worktree,key,operation_id):
    command=entry.get('tests',{}).get(key)
    if not command: raise GitError('test_command_unknown')
    path=check_tree(entry,worktree)
    name='mimic-test-'+operation_id
    argv=['docker','run','--pull=never','--rm','--name',name,'--network=none','--read-only',
        '--user','65534:65534','--cap-drop=ALL','--security-opt','no-new-privileges',
        '--pids-limit','64','--memory','512m','--cpus','1','--tmpfs','/tmp:rw,noexec,nosuid,size=67108864',
        '--mount',f'type=bind,src={path},dst=/workspace,readonly','--workdir','/workspace',
        '--env','HOME=/tmp','--env','PYTHONDONTWRITEBYTECODE=1',command['image'],*command['argv']]
    try:
        code,out,elapsed=process(argv,env=docker_env(),timeout=40,limit=64000)
        status='passed' if code==0 else 'error' if code in (125,126,127,137) else 'failed'
        return {'status':status,'exit_code':code,'duration_ms':elapsed,'log':minimize_log(out.decode(errors='replace'),entry)[:8000]}
    finally:
        # En cas de timeout, la CLI tuée ne doit pas laisser le conteneur continuer.
        try: process(['docker','rm','-f',name],env=docker_env(),timeout=5,limit=4000)
        except GitError: pass


def target_commit(entry,target):
    branch(target)
    code,out=git(entry['_path'],'rev-parse','--verify','refs/heads/'+target,ok=False)
    if code: raise GitError('target_branch_missing')
    return sha(out.strip().decode())


def remote_commit(entry,target):
    branch(target)
    remote=entry.get('remote')
    if not remote: raise GitError('remote_missing')
    out=git(entry['_path'],'ls-remote','--heads',remote,'refs/heads/'+target,entry=entry).decode().splitlines()
    return sha(out[0].split()[0]) if out else None


def apply_sensitive(entry,worktree,kind,params):
    path=check_tree(entry,worktree); head=sha(params['head'])
    if kind=='delete':
        git(entry['_path'],'worktree','remove',str(path))
        return {'removed':True,'branch_retained':worktree.branch}
    if kind=='merge':
        target=params['target']; old=params['target_head']
        records=git(entry['_path'],'worktree','list','--porcelain').decode().splitlines()
        if 'branch refs/heads/'+target in records: raise GitError('target_branch_checked_out')
        if git(entry['_path'],'merge-base','--is-ancestor',old,head,ok=False)[0]: raise GitError('fast_forward_required')
        git(entry['_path'],'update-ref','refs/heads/'+target,head,old)
        return {'head':head,'target':target}
    if kind=='push':
        target=params['target']; current=remote_commit(entry,target)
        if current!=params['target_head']: raise GitError('remote_changed')
        if current and git(entry['_path'],'merge-base','--is-ancestor',current,head,ok=False)[0]: raise GitError('fast_forward_required')
        # Lease CAS exacte ; l'ancestralité vérifiée ci-dessus interdit tout push divergent.
        lease='--force-with-lease=refs/heads/'+target+':'+(current or '')
        git(entry['_path'],'push','--porcelain',lease,entry['remote'],head+':refs/heads/'+target,entry=entry)
        return {'head':head,'target':target}
    deployment=entry.get('deployments',{}).get(params['target'])
    if not deployment: raise GitError('deployment_unknown')
    env={'PATH':os.defpath,'MIMIC_COMMIT':head,'MIMIC_WORKTREE':str(path),'MIMIC_OPERATION':params['operation_id']}
    for name,ref in deployment.get('env_refs',{}).items():
        value=os.environ.get(ref)
        if not value: raise GitError('deployment_secret_missing')
        env[name]=value
    code,out,elapsed=process(deployment['argv'],cwd=path,env=env,timeout=40,limit=64000)
    if code: raise GitError('deployment_failed')
    return {'head':head,'target':params['target'],'duration_ms':elapsed,'log':minimize_log(out.decode(errors='replace'),entry)[:8000]}
