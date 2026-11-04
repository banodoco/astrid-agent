"""Exercise fixture release through the actual historical helper subprocess."""
import io
from pathlib import Path
import subprocess
import sys
import tarfile

import pytest

from scripts import journey_workspace


@pytest.mark.integration
@pytest.mark.parametrize("revision", [
    "11443b0b5b34bd5b36725504fb083146192a759d",
    "6d53a19070faf63fc15ae1f81417722edc2c8a5f",
])
def test_release_fails_closed_and_preserves_unrelated_work(tmp_path, revision):
    from tests.helpers.runtime import runtime_checkout
    archive = subprocess.check_output(["git", "-C", str(runtime_checkout()), "archive", revision])
    source = tmp_path / "source"
    with tarfile.open(fileobj=io.BytesIO(archive)) as stream:
        stream.extractall(source, filter="data")
    program = r'''
import json, subprocess, sys
from pathlib import Path
sys.path.insert(0, sys.argv[1])
from runtime_protocol.store import RealmStore
from runtime_protocol.service import RuntimeService
import importlib.util
spec=importlib.util.spec_from_file_location('helper',sys.argv[2]); h=importlib.util.module_from_spec(spec); spec.loader.exec_module(h)
root=Path(sys.argv[3]); root.mkdir(); (root/'tst-b1-owner.json').write_text('{}')
realm=root/'realm'; RealmStore.initialize(realm).close()
identity=h.realm_identity(realm)
request=root/'request.json'
bootstrap='import runpy,sys; sys.path.insert(0,sys.argv.pop(1)); sys.argv=sys.argv[1:]; runpy.run_path(sys.argv[0],run_name="__main__")'
def call(action, payload):
    request.write_text(json.dumps(payload))
    return subprocess.run([sys.executable,'-I','-c',bootstrap,sys.argv[1],sys.argv[2],action,
                           '--run-root',str(root),'--realm',str(realm),'--request',str(request)],
                          capture_output=True,text=True,timeout=30)
operation={'id':'c'*64,'lease_token':'d'*64}
created=call('busy',{'realm_identity':identity,'operation':operation}); assert created.returncode == 0, created.stderr
assert call('busy',{'realm_identity':identity,'operation':operation}).returncode != 0
keys=json.loads(created.stdout)
s=RealmStore(realm)
epoch=s._current_runtime_epoch()
s.upsert_executor('other',['user.work'],runtime_epoch=epoch)
other=s.create_task('user.work',{'note':'unrelated'})['task']
s._claim_task(other['id'],'other','other-token',runtime_epoch=epoch)
s.close()
upload=realm/'staging'/'uploads'/('a'*32+'.upload'); upload.parent.mkdir(parents=True,exist_ok=True); upload.write_bytes(b'keep')
def status():
    s=RealmStore(realm)
    try: return (s.get_task(keys['task_id'])['task']['status'],s.get_task(other['id'])['task']['status'],s._current_runtime_epoch())
    finally: s.close()
assert status()==('running','running',epoch)
# Replacing the selected directory must never target the replacement realm.
original=root/'original-realm'; realm.rename(original)
replacement=RealmStore.initialize(realm); replacement_epoch=replacement._current_runtime_epoch(); replacement.close()
p=call('release',{'realm_identity':identity,'keys':keys})
assert p.returncode != 0
replacement=RealmStore(realm)
assert replacement._current_runtime_epoch()==replacement_epoch
replacement.close()
realm.rename(root/'replacement-realm'); original.rename(realm)
assert status()==('running','running',epoch)
for change in ({'task_id':other['id']},{'lease_token':'wrong'},{'fence':keys['fence']+1},{'attempt_id':'wrong'}):
    p=call('release',{'realm_identity':identity,'keys':{**keys,**change}})
    assert p.returncode != 0 and status()==('running','running',epoch), p.stderr
wrong={**identity,'realm_id':'different'}
p=call('release',{'realm_identity':wrong,'keys':keys})
assert p.returncode != 0 and status()==('running','running',epoch)
# A dependent task means the Runtime settlement could mutate unrelated work.
s=RealmStore(realm)
s.conn.execute('INSERT INTO task_dependencies(continuation_task_id, predecessor_task_id, ordinal) VALUES (?,?,0)', (other['id'],keys['task_id']))
s.close()
p=call('release',{'realm_identity':identity,'keys':keys})
assert p.returncode != 0 and status()==('running','running',epoch)
s=RealmStore(realm); s.conn.execute('DELETE FROM task_dependencies WHERE predecessor_task_id=?',(keys['task_id'],)); s.close()
# Another owner must cause a closed failure rather than bypassing admission.
s=RealmStore(realm)
p=call('busy',{'realm_identity':identity, 'operation':{'id':'a'*64,'lease_token':'b'*64}})
assert p.returncode != 0
p=call('release',{'realm_identity':identity,'keys':keys})
assert p.returncode != 0
s.close(); assert status()==('running','running',epoch)
p=call('release',{'realm_identity':identity,'operation':{**operation,'lease_token':'e'*64}})
assert p.returncode != 0 and status()==('running','running',epoch)
p=call('release',{'realm_identity':identity,'operation':operation})
assert p.returncode == 0, p.stderr
assert json.loads(p.stdout)=={'released':True}
assert status()==('failed','running',epoch) and upload.read_bytes()==b'keep'
'''
    result = subprocess.run([sys.executable, "-I", "-c", program, str(source),
                             str(Path(journey_workspace.__file__).resolve()), str(tmp_path / "run")],
                            capture_output=True, text=True, timeout=120)
    assert result.returncode == 0, result.stdout + result.stderr
