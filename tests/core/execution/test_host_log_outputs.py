from __future__ import annotations

import hashlib
import json

import pytest

from astrid.core.execution.generic_host import GenericPackHost, HostError, _task_storage_envelope
from astrid.core._shared import run_logs
from tests.test_generic_host import FakeRuntime, _write_manifest


@pytest.mark.parametrize('failed', [False, True])
def test_empty_publication_directory_keeps_terminal_job_logs(tmp_path, failed):
    path = _write_manifest(tmp_path / 'pack')
    definition = json.loads(path.read_text())
    definition['metadata']['output_result_manifest'] = True
    script = """from pathlib import Path
import hashlib, json, sys
out = Path('{out}')
print('publication stdout', flush=True)
print('publication stderr', file=sys.stderr, flush=True)
assert not any(out.iterdir()), 'publication directory must be empty'
staged = out.parent / 'publication'
staged.mkdir()
answer = staged / 'answer.txt'
answer.write_bytes(b'published')
(staged / 'manifest.json').write_text(json.dumps({'schema_version':1,'kind':'fixture','inputs':{},'outputs':[{'name':'answer','path':'answer.txt','role':'result','content_hash':'sha256:'+hashlib.sha256(answer.read_bytes()).hexdigest(),'bytes':9}],'created':'t','warnings':[]}))
out.rmdir()
staged.replace(out)
assert {p.name for p in out.iterdir()} == {'answer.txt', 'manifest.json'}
raise SystemExit(FAILED)
""".replace('FAILED', repr(int(failed)))
    definition['command']['argv'] = ['{python_exec}', '-c', script]
    path.write_text(json.dumps(definition))
    runtime = FakeRuntime()
    host = GenericPackHost(pack_roots=[tmp_path], client=runtime, attempt_root=tmp_path / 'attempt')
    host.discover()
    task = {'task': {'id':'task-1', 'capability':'test.echo', 'project_id':'demo',
                     'attempt_id':'attempt-1', 'fence':1, 'spec':{'spec':{'inputs':{}}}}}
    runtime.tasks['task-1'] = task
    if failed:
        with pytest.raises(HostError, match='exited 1'):
            host.run_task(task, lease_token='lease-1')
        result = runtime.failures[0][3]
    else:
        host.run_task(task, lease_token='lease-1')
        result = runtime.settlements[0][2]
    rows = result['outputs']
    assert any(row['name'] == 'answer' for row in rows)
    logs = {row['variant_key']: row for row in rows if row.get('role') == 'log'}
    assert set(logs) == {'job.stdout', 'job.stderr'}
    for name, payload in (('job.stdout', b'publication stdout\n'),
                          ('job.stderr', b'publication stderr\n')):
        assert logs[name]['output_port'] == 'logs'
        assert logs[name]['digest'] == hashlib.sha256(payload).hexdigest()
    output = next((tmp_path / 'attempt').rglob('outputs'))
    index = {row['name']: row for row in run_logs.read_index(output)}
    assert set(index) == set(logs)
    for name in logs:
        source = output.parent / '.host-logs' / name
        assert index[name]['source'] == str(source)
        assert source.read_bytes() == (output / 'logs' / index[name]['file']).read_bytes()


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
    host_logs = tmp_path / '.host-logs'
    host_logs.mkdir()
    (output / 'host-engine.log').write_text('host engine')
    run_logs.declare(host_logs, run_logs.LogDecl(
        name='engine', role='engine', path=str((output / 'host-engine.log').resolve())))

    warnings = []
    outputs = GenericPackHost(pack_roots=[])._log_outputs(output, attempt=tmp_path, warnings=warnings)

    assert [item['variant_key'] for item in outputs] == ['engine']
    assert len(warnings) == 1
    assert '/remote/job/engine.log' in warnings[0]
    assert str((output / 'host-engine.log').resolve()) in warnings[0]


def test_distinct_empty_and_identical_logs_remain_distinct_outputs(tmp_path):
    output = tmp_path / 'outputs'
    output.mkdir()
    for name, payload in (('empty-a', b''), ('empty-b', b''), ('same-a', b'same'), ('same-b', b'same')):
        path = output / f'{name}.log'
        path.write_bytes(payload)
        run_logs.declare(output, run_logs.LogDecl(name=name, role='engine', path=str(path.resolve())))

    outputs = GenericPackHost(pack_roots=[])._log_outputs(output, attempt=tmp_path)
    runtime = FakeRuntime()
    runtime.INLINE_SETTLEMENT_OUTPUTS = True
    published = GenericPackHost(pack_roots=[], client=runtime)._upload_outputs(
        outputs, project_id='demo', run_id='run-1', task_id='task-1',
        attempt_id='attempt-1', lease_id='lease-1', fence=1,
    )

    assert {row['name'] for row in published} == {'empty-a', 'empty-b', 'same-a', 'same-b'}
    assert len(published) == 4
    assert len({row['filename'] for row in published}) == 4


def test_collected_logs_are_outside_task_output_and_scratch_caps(tmp_path):
    attempt = tmp_path / 'attempt'
    output = attempt / 'outputs'
    logs = output / 'logs'
    logs.mkdir(parents=True)
    (output / 'answer.txt').write_bytes(b'ok')
    (output / 'engine.log').write_bytes(b'x' * 80)
    run_logs.declare(output, run_logs.LogDecl(
        name='engine', role='engine', path=str((output / 'engine.log').resolve()),
    ))
    (logs / 'huge.log').write_bytes(b'x' * 100)
    host_logs = attempt / '.host-logs'
    host_logs.mkdir()
    (host_logs / 'logs.json').write_bytes(b'x' * 40)
    (host_logs / 'job.stdout').write_bytes(b'x' * 30)
    (host_logs / 'job.stderr').write_bytes(b'x' * 30)

    receipt = _task_storage_envelope(
        {'storage_estimate': {'scratch_bytes': 0, 'output_bytes': 2}},
        attempt, [],
    )
    assert receipt['output_bytes'] == 2
    assert receipt['scratch_bytes'] == 0


def test_log_declaration_cannot_exclude_an_output_directory_from_caps(tmp_path):
    attempt = tmp_path / 'attempt'
    output = attempt / 'outputs'
    output.mkdir(parents=True)
    (output / 'answer.txt').write_bytes(b'too large')
    run_logs.declare(output, run_logs.LogDecl(
        name='directory', role='engine', path=str(output.resolve()),
    ))

    with pytest.raises(HostError, match='staged output bytes'):
        _task_storage_envelope(
            {'storage_estimate': {'scratch_bytes': 0, 'output_bytes': 2}},
            attempt, [],
        )
