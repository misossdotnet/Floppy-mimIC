"""Frontière Git Smart HTTP : PAT Gitea, origine autorisée et traces minimisées."""
import base64

import pytest

from app import git_adapter as adapter


def test_pat_git_basic_uniquement_dans_environnement_origine(monkeypatch,tmp_path):
    secret='credential-fictif-recette-20261009'
    remote='https://forge.example.org/Maurice/recette.git'
    monkeypatch.setenv('MIMIC_GIT_SECRET_FORGE',secret)
    calls=[]
    def process(argv,**kwargs):
        calls.append((argv,kwargs['env']))
        return 0,b'',1
    monkeypatch.setattr(adapter,'process',process)
    entry={'auth_env':'MIMIC_GIT_SECRET_FORGE','remote':remote,'_path':tmp_path}
    adapter.git(tmp_path,'ls-remote','--heads',remote,entry=entry)
    argv,env=calls[-1]
    assert secret not in str(argv) and not any(secret in str(v) for k,v in env.items() if k!='GIT_CONFIG_VALUE_0')
    assert env['GIT_CONFIG_KEY_0']=='http.'+remote+'.extraHeader'
    scheme,encoded=env['GIT_CONFIG_VALUE_0'].removeprefix('Authorization: ').split()
    assert scheme=='Basic' and base64.b64decode(encoded).decode()=='x-access-token:'+secret
    assert secret not in adapter.minimize(secret)
    assert encoded not in adapter.minimize('diagnostic '+encoded)
    assert encoded not in adapter.minimize(env['GIT_CONFIG_VALUE_0'])
    adapter.git(tmp_path,'rev-parse','HEAD',entry=entry)
    assert 'GIT_CONFIG_VALUE_0' not in calls[-1][1]


@pytest.mark.parametrize('credential',[None,'valeur\ninterdite','valeur\x7finterdite'])
def test_credential_absent_ou_controle_refuse_avant_git(monkeypatch,tmp_path,credential):
    if credential is None:
        monkeypatch.delenv('MIMIC_GIT_SECRET_FORGE',raising=False)
    else:
        monkeypatch.setenv('MIMIC_GIT_SECRET_FORGE',credential)
    monkeypatch.setattr(adapter,'process',lambda *args,**kwargs:pytest.fail('Aucun processus autorisé'))
    with pytest.raises(adapter.GitError) as error:
        adapter.git(tmp_path,'ls-remote','https://forge.example.org/Maurice/recette.git',
            entry={'auth_env':'MIMIC_GIT_SECRET_FORGE','remote':'https://forge.example.org/Maurice/recette.git'})
    assert error.value.code==('git_credential_missing' if credential is None else 'git_credential_invalid')
