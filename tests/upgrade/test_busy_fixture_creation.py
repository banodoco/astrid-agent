"""Creation and reconciliation through real historical helper subprocesses."""
import io
import json
from pathlib import Path
import shlex
import sqlite3
import subprocess
import sys
import tarfile
from types import SimpleNamespace

import pytest

from scripts import journey_version_jumps as jumps

REVISIONS = ["11443b0b5b34bd5b36725504fb083146192a759d",
             "6d53a19070faf63fc15ae1f81417722edc2c8a5f"]


@pytest.fixture(scope="module", params=REVISIONS)
def runtime_source(request, tmp_path_factory):
    from tests.helpers.runtime import runtime_checkout
    root = tmp_path_factory.mktemp("runtime")
    archive = subprocess.check_output(["git", "-C", str(runtime_checkout()), "archive", request.param])
    with tarfile.open(fileobj=io.BytesIO(archive)) as stream:
        stream.extractall(root, filter="data")
    return root


def setup_realm(source, realm):
    code = r'''
import json,sys
from pathlib import Path
from runtime_protocol.store import RealmStore
from runtime_protocol.service import RuntimeService
r=Path(sys.argv[1]); RealmStore.initialize(r).close(); s=RuntimeService(r)
s.create_project({'slug':'preserve','name':'Preserve'},idempotency_key='preserve')
s.ingest_object(b'durable content',media_type='text/plain',original_name='keep',idempotency_key='keep')
epoch=s.store._current_runtime_epoch()
s.store.upsert_executor('unrelated',['user.work'],runtime_epoch=epoch)
t=s.store.create_task('user.work',{'note':'must survive'})['task']
s.store._claim_task(t['id'],'unrelated','unrelated-token',runtime_epoch=epoch)
# Even an overdue unrelated lease must not be reaped by the fixture claim.
s.store.conn.execute("UPDATE tasks SET lease_expires_at='2000-01-01T00:00:00+00:00' WHERE id=?", (t['id'],))
s.close()
u=r/'staging'/'uploads'/('b'*32+'.upload'); u.parent.mkdir(parents=True,exist_ok=True); u.write_bytes(b'unrelated staged bytes')
print(json.dumps({'other':t['id'],'epoch':epoch,'upload':str(u)}))
'''
    result = subprocess.run([sys.executable, '-I', '-c',
        'import sys; sys.path.insert(0,sys.argv.pop(1));\n' + code, str(source), str(realm)],
        capture_output=True, text=True, timeout=30)
    assert result.returncode == 0, result.stderr
    return json.loads(result.stdout)


def wrapper(path, source, fault):
    bootstrap = 'import sys,runpy; sys.path.insert(0,' + repr(str(source)) + '); sys.argv=sys.argv[2:]\n'
    if fault == 'cleanup_failure':
        bootstrap += 'if sys.argv[1]=="release": raise RuntimeError("injected reconciliation failure")\n'
    if fault in {'create', 'claim', 'heartbeat'}:
        method = {'create': 'create_task', 'claim': '_claim_task', 'heartbeat': 'heartbeat_task'}[fault]
        bootstrap += ('from runtime_protocol.store import RealmStore\n'
                      'original=RealmStore.' + method + '\n'
                      'def fail(*args,**kwargs):\n original(*args,**kwargs)\n raise OSError("injected ' + fault + ' failure")\n'
                      'if sys.argv[1]=="busy": RealmStore.' + method + '=fail\n')
    bootstrap += 'runpy.run_path(sys.argv[0],run_name="__main__")\n'
    # run_path propagates SystemExit(0), so catch it to simulate transport loss.
    bootstrap = bootstrap.replace('runpy.run_path(sys.argv[0],run_name="__main__")\n',
        'try: runpy.run_path(sys.argv[0],run_name="__main__")\nexcept SystemExit as exc:\n if exc.code: raise\n')
    if fault in {'output', 'cleanup_failure'}:
        bootstrap += 'if sys.argv[1]=="busy": print("invalid trailer")\n'
    if fault == 'nonzero':
        bootstrap += 'if sys.argv[1]=="busy": raise SystemExit(7)\n'
    if fault == 'timeout':
        bootstrap += 'if sys.argv[1]=="busy":\n import time; sys.stdout.flush(); time.sleep(30)\n'
    path.write_text('#!/bin/sh\nexec ' + shlex.quote(sys.executable) + ' -I -c ' + shlex.quote(bootstrap) + ' "$@"\n')
    path.chmod(0o700)


@pytest.mark.integration
@pytest.mark.parametrize('fault', ['none', 'create', 'claim', 'heartbeat', 'output', 'nonzero', 'timeout'])
def test_creation_and_reconciliation_preserve_existing_data(runtime_source, tmp_path, monkeypatch, fault):
    run = tmp_path / 'run'
    root, data, evidence = run/'program', run/'data', run/'evidence'
    version = root/'versions'/'old'
    version.mkdir(parents=True)
    (data/'runtime').mkdir(parents=True)
    (run/'tst-b1-owner.json').write_text('{}')
    realm = data/'realm'
    before = setup_realm(runtime_source, realm)
    (version/'version.json').write_text('{}')
    (data/'runtime'/'discovery.json').write_text(json.dumps({'realm_root':str(realm)}))
    launcher = tmp_path/'python-wrapper'
    wrapper(launcher, runtime_source, fault)
    with sqlite3.connect(realm/'realm.sqlite3') as db:
        # Snapshot the unrelated task and all durable user content tables.
        user_before = {table: db.execute('SELECT * FROM '+table+' ORDER BY rowid').fetchall()
                       for table in ('projects', 'objects')}
        other_before = db.execute('SELECT * FROM tasks WHERE id=?',(before['other'],)).fetchone()
    cas_before = {str(p.relative_to(realm)): p.read_bytes() for p in (realm/'cas').rglob('*') if p.is_file()}
    monkeypatch.setattr(jumps.journey, 'bootstrap', lambda *_: pytest.fail('update must not run'))
    if fault == 'timeout':
        actual_run = jumps.journey.run
        def short_run(*args, **kwargs):
            kwargs['timeout'] = 2
            return actual_run(*args, **kwargs)
        monkeypatch.setattr(jumps.journey, 'run', short_run)
    result = {'checks':{}}
    expected = {'none': FileNotFoundError, 'create': RuntimeError, 'claim': RuntimeError,
                'heartbeat': RuntimeError, 'output': json.JSONDecodeError, 'nonzero': RuntimeError,
                'timeout': subprocess.TimeoutExpired}[fault]
    with pytest.raises(expected):
        jumps.busy_refusal(SimpleNamespace(app_checkout=tmp_path,shell='sh'),root,data,evidence,
                           tmp_path/'manifest',{'root':str(version),'python':str(launcher),'env':{}},
                           123,'birth',None,'birth',result)
    with sqlite3.connect(realm/'realm.sqlite3') as db:
        assert db.execute('SELECT runtime_epoch FROM runtime_lifecycle').fetchone()[0] == before['epoch']
        assert db.execute('SELECT * FROM tasks WHERE id=?',(before['other'],)).fetchone() == other_before
        assert {table: db.execute('SELECT * FROM '+table+' ORDER BY rowid').fetchall()
                for table in user_before} == user_before
        statuses = db.execute("SELECT status FROM tasks WHERE capability='tst.c1.busy'").fetchall()
    assert statuses == ([('failed',)] if fault in {'none','output','nonzero','timeout'} else [])
    assert Path(before['upload']).read_bytes() == b'unrelated staged bytes'
    assert {str(p.relative_to(realm)):p.read_bytes() for p in (realm/'cas').rglob('*') if p.is_file()} == cas_before
    assert (evidence/'busy-release'/'release.json').exists()
    assert 'busy_cleanup_error' not in result and not result['checks']


@pytest.mark.integration
def test_release_rolls_back_partial_settlement(tmp_path, runtime_source):
    source = runtime_source
    program = r"""
import importlib.util,json,sqlite3,subprocess,sys
from pathlib import Path
from runtime_protocol.store import RealmStore
from runtime_protocol.service import RuntimeService
spec=importlib.util.spec_from_file_location('helper',sys.argv[1]); h=importlib.util.module_from_spec(spec); spec.loader.exec_module(h)
root=Path(sys.argv[2]); (root/'tst-b1-owner.json').write_text('{}'); realm=root/'realm'
RealmStore.initialize(realm).close(); service=RuntimeService(realm); keys=h.busy(service); service.close()
request=root/'request.json'; request.write_text(json.dumps({'keys':keys,'realm_identity':h.realm_identity(realm)}))
def snapshot():
    with sqlite3.connect(realm/'realm.sqlite3') as db:
        return {name:db.execute('SELECT * FROM '+name+' ORDER BY rowid').fetchall() for name in ['tasks','runs','attempts','events','reservations','runtime_lifecycle']}
before=snapshot()
bootstrap='import sys,runpy; sys.path.insert(0,sys.argv.pop(1)); sys.argv=sys.argv[1:]\nfrom runtime_protocol.store import RealmStore\noriginal=RealmStore._append_event\ndef fail(self,*args,**kwargs):\n if args[2]=="task.failed": raise OSError("injected settlement failure")\n return original(self,*args,**kwargs)\nRealmStore._append_event=fail\nrunpy.run_path(sys.argv[0],run_name="__main__")'
p=subprocess.run([sys.executable,'-I','-c',bootstrap,sys.path[0],sys.argv[1],'release','--run-root',str(root),'--realm',str(realm),'--request',str(request)],capture_output=True,text=True,timeout=30)
assert p.returncode != 0 and 'injected settlement failure' in p.stderr,p.stderr
assert snapshot()==before
print(json.dumps({'rollback':'pass','fixture':'running','epoch':'preserved'}))
"""
    p=subprocess.run([sys.executable,'-I','-c','import sys; sys.path.insert(0,sys.argv.pop(1));\n'+program,str(source),str(jumps.journey.ROOT/'scripts/journey_workspace.py'),str(tmp_path)],capture_output=True,text=True,timeout=40)
    assert p.returncode==0,p.stdout+p.stderr
    print(p.stdout.strip())


@pytest.mark.integration
def test_creation_error_remains_primary_and_retry_reconciles(runtime_source, tmp_path, monkeypatch):
    run = tmp_path / 'run'
    root, data, evidence = run/'program', run/'data', run/'evidence'
    version = root/'versions'/'old'
    version.mkdir(parents=True)
    (data/'runtime').mkdir(parents=True)
    (run/'tst-b1-owner.json').write_text('{}')
    realm = data/'realm'
    before = setup_realm(runtime_source, realm)
    (data/'runtime'/'discovery.json').write_text(json.dumps({'realm_root':str(realm)}))
    launcher = tmp_path/'python-wrapper'
    wrapper(launcher, runtime_source, 'cleanup_failure')
    old = {'root':str(version),'python':str(launcher),'env':{}}
    result = {'checks':{}}
    with pytest.raises(json.JSONDecodeError) as caught:
        jumps.busy_refusal(SimpleNamespace(app_checkout=tmp_path,shell='sh'),root,data,evidence,
                           tmp_path/'manifest',old,123,'birth',None,'birth',result)
    assert isinstance(caught.value.__cause__, RuntimeError)
    assert 'release.json: exit' in result['busy_cleanup_error']
    request = json.loads((evidence/'busy'/'busy-request.json').read_text())
    with sqlite3.connect(realm/'realm.sqlite3') as db:
        assert db.execute("SELECT status FROM tasks WHERE capability='tst.c1.busy'").fetchall() == [('running',)]
    wrapper(launcher, runtime_source, 'none')
    assert jumps.workspace(old, data, evidence/'retry', 'release', request, realm=realm) == {'released': True}
    with sqlite3.connect(realm/'realm.sqlite3') as db:
        assert db.execute("SELECT status FROM tasks WHERE capability='tst.c1.busy'").fetchall() == [('failed',)]
        assert db.execute('SELECT status FROM tasks WHERE id=?',(before['other'],)).fetchone() == ('running',)
    assert Path(before['upload']).read_bytes() == b'unrelated staged bytes'
