"""Native receipt readback retains executed bytes without reopening compilation."""
import hashlib
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from astrid.core.generation.backends.vibecomfy_workflow import VibeComfyWorkflowBackend
from tests.core.generation.test_producer_result_recovery import compiled, envelope
from tests.packs.vibecomfy.test_local_server_run import comfy  # noqa: F401


@pytest.mark.parametrize('route', ['embedded', 'attached'])
def test_native_success_projects_exact_settings_and_preserves_refs(monkeypatch, tmp_path, route):
    from astrid.packs.vibecomfy import production_engine
    from vibecomfy.testing.canonical import canonical_bytes
    root = tmp_path / 'out'
    path = envelope(root, status='succeeded')
    payload = json.loads(path.read_bytes())
    record = compiled()
    payload['inputs']['effective_settings'] = {
        'digest': 'sha256:' + record.api_digest, 'sha256': record.api_digest,
        'bytes': len(canonical_bytes(record.to_dict()['api_projection'])), 'filename': 'executed.json'}
    payload['evidence']['producer']['runtime'] = {
        'approved_projection': record.to_dict(), 'api_digest': record.api_digest}
    path.write_text(json.dumps(payload))
    original = json.loads(path.read_bytes())
    result = SimpleNamespace(managed_generation_result_path=path, managed_generation_result=None)
    monkeypatch.setattr(production_engine, 'run_workflow_result_path', lambda *args, **kwargs: result)
    kwargs = {'profile_id': 'local_server', 'comfy_url': 'http://127.0.0.1:8188'} if route == 'attached' else {}
    returned = VibeComfyWorkflowBackend().workflow(tmp_path / 'absent.py', root,
        task_identity='task-1', attempt_identity='attempt-1', **kwargs)
    after = json.loads(path.read_bytes())
    assert returned is result
    assert (root / 'effective-settings.json').read_bytes() == canonical_bytes(record.to_dict()['api_projection'])
    assert after['inputs']['source'] == original['inputs']['source']
    assert after['inputs']['effective_settings'] == original['inputs']['effective_settings']
    assert after['evidence'] == original['evidence']
    assert after['outputs'] == original['outputs']
    assert after['outcomes'] == original['outcomes']


@pytest.mark.parametrize('cancelled', [False, True])
@pytest.mark.parametrize('partial', ['valid', 'corrupt', 'remote'])
def test_native_failure_writes_known_envelope_with_honest_partial_evidence(
        monkeypatch, tmp_path, cancelled, partial):
    from astrid.packs.vibecomfy import production_engine
    from vibecomfy.testing.canonical import canonical_bytes
    from vibecomfy.workflow import VibeWorkflow, WorkflowSource
    from vibecomfy.workflow_bundle import ApprovedProjectionRecord, emit_bundle
    monkeypatch.chdir(tmp_path)
    workflow = VibeWorkflow('receipt-test', WorkflowSource('receipt-test'))
    sink = workflow.add_node('SaveImage', _id='1', uid='save', filename_prefix='result')
    workflow = workflow.finalize({}, output_node=sink, output_type='SaveImage', name='image',
        artifact_kind='image', mime_type='image/png', filename_prefix='result')
    source = tmp_path / 'source.json'
    source.write_bytes(b' original submitted source\r\n')
    bundle = emit_bundle(workflow, tmp_path / 'workflow.py', {'operation': 'authored'})
    base = compiled().to_dict()
    base['revision_id'] = bundle.revision_id
    record = ApprovedProjectionRecord.from_dict(base)
    out = tmp_path / 'out'
    error = SystemExit(143) if cancelled else RuntimeError('native failure')
    original_source = {'digest': 'sha256:' + 'a' * 64, 'bytes': 19, 'filename': 'submitted.json'}
    producer = {key: {'existing': key} for key in ('engine', 'entry', 'models', 'versions', 'provider', 'build')}
    transport = {'target': {'owner': 'human'}, 'execution_binding': {'attempt_id': 'attempt-1'},
                 'failure': {'native': 'failure'}, 'cancel': {'requested': True, 'observed_stop': False}}

    def fail(*args, **kwargs):
        run = out / '.vibecomfy-runtime' / 'out' / 'runs' / 'native-123'
        run.mkdir(parents=True)
        output = run / 'result.png'
        output.write_bytes(b'verified partial bytes')
        artifact = {'path': str(output) if partial != 'remote' else None,
                    'reported_path': 'result.png', 'bytes': output.stat().st_size,
                    'sha256': hashlib.sha256(output.read_bytes()).hexdigest()}
        if partial == 'corrupt':
            output.write_bytes(b'changed after native hash')
        receipt = run / 'attempt.json'
        receipt.write_text(json.dumps({
            'runtime_evidence': {'approved_projection': record.to_dict(), 'api_digest': record.api_digest,
                'adapter': {'kind': 'external', 'endpoint': 'http://127.0.0.1:8188'},
                'terminal': {'phase': 'cancelled' if cancelled else 'verification'}},
            'artifacts': [artifact], 'inputs': {'source': original_source},
            'evidence': {'producer': producer, 'transport': transport}}))
        error.receipt_path = str(receipt)
        raise error

    monkeypatch.setattr(production_engine, 'run_workflow_result_path', fail)
    with pytest.raises(type(error)) as caught:
        VibeComfyWorkflowBackend().workflow(bundle.python_path, out,
            task_identity='task-1', attempt_identity='attempt-1')
    assert caught.value is error
    path = caught.value.managed_generation_result_path
    assert path == out / 'managed-generation-result.json'
    payload = json.loads(path.read_bytes())
    assert payload['producer_run_id'] == 'native-123'
    assert payload['task_id'] == 'task-1' and payload['attempt_id'] == 'attempt-1'
    assert payload['outcomes']['execution']['status'] == ('cancelled' if cancelled else 'failed')
    assert payload['outcomes']['publication']['status'] == 'not_started'
    assert payload['inputs']['source'] == original_source
    assert payload['evidence']['transport'] == transport
    assert all(payload['evidence']['producer'][key] == value for key, value in producer.items())
    settings = payload['inputs']['effective_settings']
    assert Path(settings['path']).read_bytes() == canonical_bytes(record.to_dict()['api_projection'])
    assert settings['sha256'] == record.api_digest
    assert len(payload['outputs']) == (1 if partial == 'valid' else 0)
    if payload['outputs']:
        output = payload['outputs'][0]
        assert output['producer_output_id'] == 'vibecomfy:image:0'
        assert (out / output['path']).read_bytes() == b'verified partial bytes'
    else:
        assert payload['warnings']


def test_real_attached_settings_match_exact_http_prompt(comfy, tmp_path):
    from vibecomfy.testing.canonical import canonical_bytes
    out = tmp_path / 'attempt' / 'out'
    original = comfy.workflow.read_bytes()
    result = VibeComfyWorkflowBackend().workflow(comfy.workflow, out,
        task_identity='task-1', attempt_identity='attempt-1', profile_id='local_server',
        comfy_url=comfy.url, workflow_input_bindings={'seed': 123})
    payload = json.loads(result.managed_generation_result_path.read_bytes())
    ref = payload['inputs']['effective_settings']
    assert Path(ref['path']).read_bytes() == canonical_bytes(comfy.prompts[0])
    assert ref['sha256'] == hashlib.sha256(canonical_bytes(comfy.prompts[0])).hexdigest()
    assert payload['inputs']['source']['sha256'] == hashlib.sha256(original).hexdigest()
    assert Path(payload['inputs']['source']['path']).read_bytes() == original
    assert all(key in payload['evidence']['producer'] for key in ('engine', 'entry', 'models', 'versions', 'provider', 'build'))
    assert all(key in payload['evidence']['transport'] for key in ('target', 'execution_binding', 'failure', 'cancel'))


def test_real_attached_failure_retains_verified_downloads(comfy, tmp_path, monkeypatch):
    import importlib
    run = importlib.import_module('vibecomfy.runtime.run')
    from vibecomfy.testing.canonical import canonical_bytes
    def reject(*args, **kwargs):
        raise RuntimeError('verification failed after download')
    monkeypatch.setattr(run, '_verify_declared_media', reject)
    out = tmp_path / 'attempt' / 'out'
    with pytest.raises(RuntimeError, match='verification failed') as caught:
        VibeComfyWorkflowBackend().workflow(comfy.workflow, out,
            task_identity='task-1', attempt_identity='attempt-1', profile_id='local_server', comfy_url=comfy.url)
    payload = json.loads(caught.value.managed_generation_result_path.read_bytes())
    assert len(payload['outputs']) == 1
    ref = payload['inputs']['effective_settings']
    assert Path(ref['path']).read_bytes() == canonical_bytes(comfy.prompts[0])
    assert payload['outcomes']['execution']['status'] == 'failed'
    assert payload['outcomes']['verification']['status'] == 'failed'
    assert payload['evidence']['transport']['cancel'] is None
    assert (out / payload['outputs'][0]['path']).read_bytes() == (comfy.output / 'astrid_1.png').read_bytes()


@pytest.mark.parametrize('stale', [False, True])
def test_native_cancel_without_outputs_keeps_stop_unknown_and_refuses_stale_receipt(monkeypatch, tmp_path, stale):
    from astrid.packs.vibecomfy import production_engine
    out = tmp_path / 'out'
    receipt = out / '.vibecomfy-runtime' / 'out' / 'runs' / 'native-123' / 'attempt.json'
    def write_receipt():
        receipt.parent.mkdir(parents=True, exist_ok=True)
        receipt.write_text(json.dumps({'runtime_evidence': {'approved_projection': compiled().to_dict(),
                                                           'terminal': {'phase': 'cancelled'}}}))
    if stale:
        write_receipt()
    error = SystemExit(143)
    def fail(*args, **kwargs):
        if not stale:
            write_receipt()
        error.receipt_path = str(receipt)
        raise error
    monkeypatch.setattr(production_engine, 'run_workflow_result_path', fail)
    with pytest.raises(SystemExit) as caught:
        VibeComfyWorkflowBackend().workflow(tmp_path / 'absent.py', out,
            task_identity='task-1', attempt_identity='attempt-1')
    if stale:
        assert not hasattr(caught.value, 'managed_generation_result_path')
        assert not (out / 'managed-generation-result.json').exists()
    else:
        payload = json.loads(caught.value.managed_generation_result_path.read_bytes())
        assert payload['outputs'] == []
        assert payload['outcomes']['execution']['status'] == 'cancelled'
        assert payload['evidence']['transport']['cancel']['requested'] is True
        assert payload['evidence']['transport']['cancel']['observed_stop'] is None


def test_checkout_failure_uses_task_spool_and_verified_host_output_root(monkeypatch, tmp_path):
    import vibecomfy.schema as schema
    from vibecomfy.workflow import VibeWorkflow, WorkflowSource
    from vibecomfy.workflow_bundle import ApprovedProjectionRecord, WorkflowBundle, emit_bundle
    from astrid.core.generation.backends import vibecomfy as backend
    from tests.core.generation.backends.test_checkout_server import _hc03_profile, _patch_remote_open
    monkeypatch.chdir(tmp_path)
    workflow = VibeWorkflow('checkout-evidence', WorkflowSource('checkout-evidence'))
    sink = workflow.add_node('SaveImage', _id='1', uid='save', filename_prefix='result')
    workflow = workflow.finalize({}, output_node=sink, output_type='SaveImage', name='image',
        artifact_kind='image', mime_type='image/png', filename_prefix='result')
    (tmp_path / 'source.json').write_bytes(b'{}\r\n')
    bundle = emit_bundle(workflow, tmp_path / 'workflow.py', {'operation': 'authored'})
    record = ApprovedProjectionRecord.from_dict({**compiled().to_dict(), 'revision_id': bundle.revision_id})
    profile = _hc03_profile(tmp_path)
    monkeypatch.setattr(backend, '_verify_owned_vibe_session', lambda *args: None)
    _patch_remote_open(monkeypatch, output=b'artifact')
    monkeypatch.setattr(schema, 'get_target_schema_provider', lambda **kwargs: SimpleNamespace(refresh=lambda: {}))
    monkeypatch.setattr(WorkflowBundle, 'compile', lambda *args, **kwargs: record)
    adapter = backend.CheckoutServerAdapter.from_host_session(hc03_profile=profile,
        model_id='z-image', template_id='image/z_image', invocation_identity='task-1')
    out = tmp_path / 'outputs' / 'task-1'
    error = RuntimeError('checkout producer failed after writing partial')

    def fail(pair, *, config):
        assert pair[0] is record and pair[1] is bundle
        assert config.runtime_root == out / '.vibecomfy-runtime'
        run = config.runtime_root / 'out' / 'runs' / 'checkout-native-123'
        run.mkdir(parents=True)
        partial = Path(config.extra['output_directory']) / 'result.png'
        partial.write_bytes(b'verified checkout partial')
        receipt = run / 'attempt.json'
        receipt.write_text(json.dumps({'runtime_evidence': {
            'approved_projection': record.to_dict(), 'api_digest': record.api_digest,
            'terminal': {'phase': 'verification'}}, 'artifacts': [
                {'path': str(partial), 'reported_path': 'result.png', 'bytes': partial.stat().st_size,
                 'sha256': hashlib.sha256(partial.read_bytes()).hexdigest()}]}))
        error.receipt_path = str(receipt)
        raise error
    monkeypatch.setattr(adapter, '_run_workflow', fail)
    with pytest.raises(RuntimeError) as caught:
        adapter.run_compiled_workflow(bundle, out, task_identity='task-1', attempt_identity='attempt-1')
    payload = json.loads(caught.value.managed_generation_result_path.read_bytes())
    assert payload['producer_run_id'] == 'checkout-native-123'
    assert payload['outcomes']['verification']['status'] == 'failed'
    assert len(payload['outputs']) == 1
    assert (out / payload['outputs'][0]['path']).read_bytes() == b'verified checkout partial'
    assert payload['inputs']['effective_settings']['sha256'] == record.api_digest
