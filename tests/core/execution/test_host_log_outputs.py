from __future__ import annotations

import hashlib
import json

import pytest

from astrid.core.execution.generic_host import GenericPackHost
from astrid.core._shared import run_logs
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
    index = out/'logs.json'
    doc = json.loads(index.read_text()) if index.exists() else {'schema':'run-logs.v1','logs':[],'children':[]}
    doc['logs'] += [{'name':'engine','role':'engine','path':'engine.log'},{'name':'run','role':'run','path':'run.json'}]
    index.write_text(json.dumps(doc))
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
    # The host always declares the job's own stdout and stderr; pack logs add to them.
    host_streams = {'job.stdout', 'job.stderr'}
    if not upload_failure:
        expected = host_streams | ({'engine', 'run'} if logs else set())
        assert {row['variant_key'] for row in logs_out} == expected
        assert all(row['output_port'] == 'logs' for row in logs_out)
        pack_digests = {row['digest'].removeprefix('sha256:') for row in logs_out
                        if row['variant_key'] not in host_streams}
        assert pack_digests == ({hashlib.sha256(b'engine bytes').hexdigest(),
                                 hashlib.sha256(b'{"run":1}').hexdigest()} if logs else set())
    else:
        assert not logs_out
    assert any(row['name'] == 'answer' for row in rows) == manifest
    if upload_failure:
        receipt = json.loads(next((tmp_path / 'attempt').rglob('result.json')).read_text())
        assert not receipt.get('evidence_pending')
        assert 'logs unavailable: OSError' in receipt['warnings']


def test_duplicate_log_source_is_a_warning_and_keeps_first_output(tmp_path):
    output = tmp_path / 'outputs'
    output.mkdir()
    (output / 'logs').mkdir()
    (output / 'logs' / 'index.json').write_text(json.dumps({
        'schema': 'run-logs.collected.v1',
        'logs': [{'name': 'engine', 'role': 'engine', 'file': 'engine.log', 'missing': False,
                  'source': '/remote/job/engine.log'}],
    }))
    host_logs = output / '.host-logs'
    host_logs.mkdir()
    (output / 'host-engine.log').write_text('host engine')
    run_logs.declare(host_logs, run_logs.LogDecl(
        name='engine', role='engine', path=str((output / 'host-engine.log').resolve())))

    warnings = []
    outputs = GenericPackHost(pack_roots=[])._log_outputs(output, warnings=warnings)

    assert [item['variant_key'] for item in outputs] == ['engine']
    assert len(warnings) == 1
    assert '/remote/job/engine.log' in warnings[0]
    assert str((output / 'host-engine.log').resolve()) in warnings[0]
