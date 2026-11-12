from __future__ import annotations

import hashlib
import json

import pytest

from astrid.core.execution.generic_host import GenericPackHost
from tests.test_generic_host import FakeRuntime, _write_manifest


@pytest.mark.parametrize('failed,manifest,logs,upload_failure', [
    (True, False, True, False), (True, True, True, False),
    (True, False, True, True), (True, False, False, False),
    (False, True, True, False),
])
def test_logs_are_terminal_outputs(tmp_path, failed, manifest, logs, upload_failure):
    path = _write_manifest(tmp_path / 'pack')
    definition = json.loads(path.read_text())
    definition['metadata']['output_result_manifest'] = True
    script = """from pathlib import Path
import json, hashlib
out = Path('{out}')
if LOGS:
    (out/'engine.log').write_bytes(b'engine bytes')
    (out/'run.json').write_bytes(b'{\"run\":1}')
    (out/'logs.json').write_text(json.dumps({'schema':'run-logs.v1','logs':[{'name':'engine','role':'engine','path':'engine.log'},{'name':'run','role':'run','path':'run.json'}],'children':[]}))
if MANIFEST:
    p=out/'answer.txt'; p.write_text('ok')
    (out/'manifest.json').write_text(json.dumps({'schema_version':1,'kind':'fixture','inputs':{},'outputs':[{'name':'answer','path':'answer.txt','role':'result','content_hash':'sha256:'+hashlib.sha256(p.read_bytes()).hexdigest(),'bytes':2}],'created':'t','warnings':[]}))
if FAILED:
    raise SystemExit(1)
""".replace('LOGS', repr(logs)).replace('MANIFEST', repr(manifest)).replace('FAILED', repr(failed))
    definition['command']['argv'] = ['{python_exec}', '-c', script]
    path.write_text(json.dumps(definition))
    runtime = FakeRuntime()
    original_upload = runtime.upload_object
    def upload(path, **kwargs):
        if upload_failure and path.parent.name == 'logs':
            raise OSError('log upload failed')
        return original_upload(path, **kwargs)
    runtime.upload_object = upload
    host = GenericPackHost(pack_roots=[tmp_path], client=runtime, attempt_root=tmp_path / 'attempt')
    host.discover()
    task = {'task': {'id':'task-1', 'capability':'test.echo', 'project_id':'demo',
                     'attempt_id':'attempt-1', 'fence':1, 'spec':{'spec':{'inputs':{}}}}}
    runtime.tasks['task-1'] = task
    if failed:
        with pytest.raises(Exception):
            host.run_task(task, lease_token='lease-1')
        assert len(runtime.failures) == 1
        result = runtime.failures[0][3]
    else:
        host.run_task(task, lease_token='lease-1')
        result = runtime.settlements[0][2]
    rows = result['outputs']
    logs_out = [row for row in rows if row.get('role') == 'log']
    if logs and not upload_failure:
        assert {row['variant_key'] for row in logs_out} == {'engine', 'run'}
        assert all(row['output_port'] == 'logs' for row in logs_out)
        assert {row['digest'].removeprefix('sha256:') for row in logs_out} == {
            hashlib.sha256(b'engine bytes').hexdigest(), hashlib.sha256(b'{"run":1}').hexdigest()}
    else:
        assert not logs_out
    assert any(row['name'] == 'answer' for row in rows) == manifest
    if upload_failure:
        receipt = json.loads(next((tmp_path / 'attempt').rglob('result.json')).read_text())
        assert not receipt.get('evidence_pending')
        assert 'logs unavailable: OSError' in receipt['warnings']
