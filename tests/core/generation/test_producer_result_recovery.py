"""Backend errors retain the existing partial envelope without changing it."""
import hashlib
import json
from types import SimpleNamespace

import pytest

from astrid.core.generation.backends.vibecomfy_workflow import VibeComfyWorkflowBackend
from astrid.core.generation.producer_result import bind_executed_settings, stage_executed_settings


def envelope(root, *, status='failed', task='task-1'):
    root.mkdir(parents=True, exist_ok=True)
    output = root / 'partial.png'
    output.write_bytes(b'verified partial bytes')
    payload = {
        'schema_version': 1, 'kind': 'managed-generation-result.v1',
        'task_id': task, 'attempt_id': 'attempt-1', 'producer_run_id': 'producer-1',
        'created': '2026-11-01T00:00:00Z', 'warnings': [],
        'inputs': {'source': {'digest': 'sha256:' + 'a' * 64, 'bytes': 17, 'filename': 'original.json'},
                   'effective_settings': {'digest': 'sha256:' + 'b' * 64}},
        'outputs': [{'producer_output_id': 'image:0', 'output_port': 'image', 'ordinal': 0,
                     'path': output.name, 'media_type': 'image/png', 'bytes': output.stat().st_size,
                     'sha256': hashlib.sha256(output.read_bytes()).hexdigest()}],
        'outcomes': {phase: {'status': value} for phase, value in (
            ('execution', status), ('retrieval', 'succeeded'),
            ('verification', 'succeeded'), ('publication', 'not_started'))},
        'evidence': {
            'producer': {key: {'existing': key} for key in ('engine', 'entry', 'models', 'versions', 'provider', 'build')},
            'transport': {'target': {'endpoint': 'http://127.0.0.1:8188'}, 'execution_binding': {'existing': True},
                          'failure': {'message': 'native failure'} if status == 'failed' else None,
                          'cancel': {'requested': True, 'observed_stop': None} if status == 'cancelled' else None}},
    }
    path = root / 'managed-generation-result.json'
    path.write_text(json.dumps(payload))
    return path


@pytest.mark.parametrize('route', ['embedded', 'attached', 'runpod'])
@pytest.mark.parametrize('cancelled', [False, True])
def test_backend_exposes_existing_verified_partial_result(monkeypatch, tmp_path, route, cancelled):
    import astrid.packs.vibecomfy.production_engine as producer
    status = 'cancelled' if cancelled else 'failed'
    root = tmp_path / 'out'
    path = envelope(root, status=status)
    original = path.read_bytes()
    error = SystemExit(143) if cancelled else RuntimeError('native failure')
    def fail(*args, **kwargs):
        raise error
    monkeypatch.setattr(producer, 'run_workflow_result_path', fail)
    backend = VibeComfyWorkflowBackend(endpoint_checkout=(lambda **kw: None) if route == 'runpod' else None)
    kwargs = {'task_identity': 'task-1', 'attempt_identity': 'attempt-1'}
    if route == 'attached':
        kwargs.update(profile_id='local_server', comfy_url='http://127.0.0.1:8188')
    elif route == 'runpod':
        kwargs.update(profile_id='checkout_server', hc03_profile={}, expected_execution_identity='exact-binding')
    with pytest.raises(type(error)) as caught:
        backend.workflow(tmp_path / 'workflow.py', root, **kwargs)
    assert caught.value is error
    assert caught.value.managed_generation_result_path == path
    assert path.read_bytes() == original  # All refs, evidence and partial bytes survive.


def test_wrapped_native_receipt_retains_nested_envelope(monkeypatch, tmp_path):
    import astrid.packs.vibecomfy.production_engine as producer
    path = envelope(tmp_path / 'native-run')
    native = RuntimeError('native')
    native.receipt_path = path.with_name('attempt.json')
    error = RuntimeError('wrapped')
    def fail(*args, **kwargs):
        raise error from native
    monkeypatch.setattr(producer, 'run_workflow_result_path', fail)
    with pytest.raises(RuntimeError) as caught:
        VibeComfyWorkflowBackend().workflow(tmp_path / 'workflow.py', tmp_path / 'out',
                                           task_identity='task-1', attempt_identity='attempt-1')
    assert caught.value.managed_generation_result_path == path


def test_failure_without_partial_outputs_keeps_existing_result(monkeypatch, tmp_path):
    import astrid.packs.vibecomfy.production_engine as producer
    root = tmp_path / 'out'
    path = envelope(root)
    payload = json.loads(path.read_text())
    payload['outputs'] = []
    for phase in ('retrieval', 'verification', 'publication'):
        payload['outcomes'][phase]['status'] = 'not_started'
    path.write_text(json.dumps(payload))
    error = RuntimeError('failed before outputs')
    def fail(*args, **kwargs):
        raise error
    monkeypatch.setattr(producer, 'run_workflow_result_path', fail)
    with pytest.raises(RuntimeError) as caught:
        VibeComfyWorkflowBackend().workflow(tmp_path / 'workflow.py', root,
                                           task_identity='task-1', attempt_identity='attempt-1')
    assert caught.value.managed_generation_result_path == path


@pytest.mark.parametrize('corrupt', [False, True])
def test_foreign_or_corrupt_partial_result_is_not_exposed(monkeypatch, tmp_path, corrupt):
    import astrid.packs.vibecomfy.production_engine as producer
    root = tmp_path / 'out'
    envelope(root, task='task-1' if corrupt else 'foreign-task')
    if corrupt:
        (root / 'partial.png').write_bytes(b'changed')
    error = RuntimeError('native')
    def fail(*args, **kwargs):
        raise error
    monkeypatch.setattr(producer, 'run_workflow_result_path', fail)
    with pytest.raises(RuntimeError) as caught:
        VibeComfyWorkflowBackend().workflow(tmp_path / 'workflow.py', root,
                                           task_identity='task-1', attempt_identity='attempt-1')
    assert caught.value is error
    assert not hasattr(caught.value, 'managed_generation_result_path')


def compiled():
    from vibecomfy.testing.canonical import canonical_digest
    from vibecomfy.workflow_bundle import ApprovedProjectionRecord
    api = {'1': {'class_type': 'ExecutedNode', 'inputs': {'text': 'bound prompt', 'seed': 42}}}
    return ApprovedProjectionRecord('revision-1', None, {'prompt': 'bound prompt'}, api,
                                    {'nodes': ['deliberately different UI graph']}, canonical_digest(api))


def test_staged_settings_are_exact_compiled_api_and_keep_existing_evidence(tmp_path):
    from vibecomfy.testing.canonical import canonical_bytes
    root = tmp_path / 'out'
    path = envelope(root)
    before = json.loads(path.read_text())
    before['inputs'].pop('effective_settings')
    path.write_text(json.dumps(before))
    record = compiled()
    ref = stage_executed_settings(record, root)
    assert ref['sha256'] == record.api_digest
    assert ref['bytes'] == len(canonical_bytes(record.to_dict()['api_projection']))
    assert (root / ref['filename']).read_bytes() == canonical_bytes(record.to_dict()['api_projection'])
    result = SimpleNamespace(managed_generation_result_path=path, managed_generation_result=None)
    bind_executed_settings(result, ref, task_id='task-1', attempt_id='attempt-1')
    after = json.loads(path.read_text())
    assert after['inputs']['effective_settings'] == ref
    assert after['inputs']['source'] == before['inputs']['source']
    assert after['evidence'] == before['evidence']
    assert after['outputs'] == before['outputs']
    assert after['outcomes'] == before['outcomes']
    assert result.managed_generation_result == after


def test_existing_project_settings_ref_is_preserved_and_unbound_ref_rejects(tmp_path):
    root = tmp_path / 'out'
    path = envelope(root)
    ref = stage_executed_settings(compiled(), root)
    payload = json.loads(path.read_text())
    payload['inputs']['effective_settings'] = {'object_id': 'sha256:' + ref['sha256'],
                                               'filename': 'executed.json'}
    path.write_text(json.dumps(payload))
    result = SimpleNamespace(managed_generation_result_path=path)
    bind_executed_settings(result, ref, task_id='task-1', attempt_id='attempt-1')
    bound = json.loads(path.read_text())['inputs']['effective_settings']
    assert bound['object_id'] == 'sha256:' + ref['sha256']
    assert bound['filename'] == 'executed.json' and 'path' not in bound
    payload['inputs']['effective_settings']['object_id'] = 'sha256:' + 'f' * 64
    path.write_text(json.dumps(payload))
    original = path.read_bytes()
    with pytest.raises(ValueError, match='differs from the executed API bytes'):
        bind_executed_settings(result, ref, task_id='task-1', attempt_id='attempt-1')
    assert path.read_bytes() == original
