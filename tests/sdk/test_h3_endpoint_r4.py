"""Connected CPU contracts: real H3 compiler/schema, fake paid owner/Runtime.

The inert transport never starts a listener, reads a bearer, or contacts a pod.
The existing production EngineEndpointTransport identity checks still run.
"""
from copy import deepcopy
from dataclasses import replace
import hashlib
import json
from pathlib import Path
import socket
from types import SimpleNamespace

import pytest

from astrid.core.generation import preflight
from astrid.core.generation.backends.comfy_target import ComfyTarget, ComfyTargetClient
from astrid.core.generation.engine_contracts import (
    EngineEndpoint, EndpointAuth, HealthEvidence, LifecycleOwner, RunPodAttestation, SecretRef,
)
from astrid.core.generation.engine_transport import EngineEndpointTransport
from astrid.packs.h3_av.actions.transform import run
from astrid.sdk import invocation
from astrid.sdk.generation import GenerationFacade
from tests.sdk.test_runpod_front_door import FakeOwner, PAID

ROOT = Path(__file__).resolve().parents[2]


@pytest.fixture(autouse=True)
def no_external_io(monkeypatch):
    monkeypatch.setattr(socket.socket, 'connect', lambda *a, **k: pytest.fail('network used'))
    monkeypatch.setattr(socket, 'create_connection', lambda *a, **k: pytest.fail('network used'))


@pytest.fixture(scope='module')
def captured_schema():
    import vibecomfy
    from tests.helpers.h3_cpu_runtime import _object_info_from_checkout
    schema = _object_info_from_checkout(Path(vibecomfy.__file__).resolve().parents[1])
    # Synthetic ready endpoint: advertise the pinned H3 filenames. This is
    # deliberately no assertion about installed weights or their bytes.
    for kind, field, names in (
        ('UNETLoader', 'unet_name', ['minimax_h3_ref2va_pruned_int8_convrot.safetensors']),
        ('VAELoader', 'vae_name', ['minimax_h3_video_vae_int8_convrot.safetensors', 'minimax_h3_audio_vae_fp32.safetensors']),
        ('LoraLoaderModelOnly', 'lora_name', ['minimax_h3_fl2v_turbo_8step_v1.0_comfyui_bf16.safetensors']),
        ('CLIPLoader', 'clip_name', ['qwen3vl_32b_minimax_h3_int8_convrot.safetensors']),
    ):
        schema[kind]['input']['required'][field][0].extend(names)
    return schema


class InertTransport(EngineEndpointTransport):
    @property
    def server_url(self):
        return getattr(self, 'origin', 'http://127.0.0.1:18188')

    @property
    def retained_server_url(self):
        return self.server_url

    def object_info(self, **kwargs):
        return deepcopy(self.schema)


@pytest.fixture
def endpoint(tmp_path, captured_schema):
    health = HealthEvidence('healthy', '2026-11-01T00:00:00Z', 'a' * 64, True)
    attestation = RunPodAttestation('runpod-engine.v1', 'account-a', 'pod123',
        str(tmp_path / 'claim-snapshot'), 'b' * 64, '/engine/install.json', 'c' * 64, health, 'a' * 64)
    record = EngineEndpoint('astrid.engine-endpoint.v1', 'vibecomfy-h3', 'vibecomfy-h3',
        'linux-x86_64-cu130', 'runpod', 'https://pod123-8000.proxy.runpod.net/engines/vibecomfy-h3',
        EndpointAuth('bearer-file', SecretRef('file', str(tmp_path / 'unopened.token'))),
        'ready', health, LifecycleOwner('astrid-runpod', 'operation-a', str(tmp_path / 'claim'),
                                      None, 'pod123', False), attestation)
    transport = InertTransport(record, pod_id='pod123', agent_port=8000)
    transport.schema = deepcopy(captured_schema)
    return ComfyTargetClient(ComfyTarget(record.url, 'pod'), endpoint_transport=transport)


@pytest.fixture
def h3_inputs(tmp_path):
    document = {
        'version': 1, 'operation': 'generate', 'output': {'duration': 4},
        'content': {'prompt': 'A quiet scene with gentle motion and matching sound.'},
        'changes': {'video': [], 'audio': []},
        'references': [{'asset': 'look', 'purpose': 'appearance'}], 'overrides': {'seed': 42, 'steps': 8},
    }
    from PIL import Image
    source = tmp_path / 'look.png'
    Image.new('RGB', (32, 32), (30, 40, 50)).save(source)
    request = tmp_path / 'request.json'
    request.write_text(json.dumps(document))
    assets = tmp_path / 'assets.json'
    assets.write_text(json.dumps({'look': str(source)}))
    return {'request': str(request), 'asset_map': str(assets)}


@pytest.fixture
def compilation(tmp_path, h3_inputs):
    from astrid.packs.h3_av.shared.input_bundle import build_input_bundle
    request = run.normalize_request(run.load_request_document(h3_inputs['request']))
    bundle = build_input_bundle(request, json.loads(Path(h3_inputs['asset_map']).read_text()), tmp_path / 'inputs.zip')
    sealed = run._compile_plan(request, bundle, tmp_path / 'compiled')
    compiled = json.loads((tmp_path / 'compiled/compile/compilation.json').read_text())
    return compiled, sealed


def test_real_h3_graph_uses_target_choices_without_local_model_root(endpoint, compilation, tmp_path, monkeypatch):
    import astrid.core.generation.model_root as model_root
    monkeypatch.setattr(model_root, 'model_root_binding_from_environment',
                        lambda **k: pytest.fail('local model root demanded'))
    compiled, _ = compilation
    first = preflight.require_h3_graph_readiness(compiled, endpoint, tmp_path)
    second = preflight.require_h3_graph_readiness(compiled, endpoint, tmp_path)
    assert first == second
    assert first['members'] == {name: row['sha256'] for name, row in compiled['workflow'].items()}
    assert all(first[key].startswith('sha256:') for key in ('api_sha256', 'model_lock_sha256', 'object_info_sha256'))
    changed = deepcopy(compiled)
    changed['workflow_inputs']['prompt'] = 'A different exact graph.'
    assert preflight.require_h3_graph_readiness(changed, endpoint, tmp_path)['api_sha256'] != first['api_sha256']


@pytest.mark.parametrize('gap,message', [
    ('empty', 'missing nodes'), ('node', 'missing nodes'), ('model', 'missing models'),
    ('field', 'unsupported_required'), ('malformed', 'invalid live schema'),
])
def test_exact_graph_gaps_refuse_before_paid_preparation(endpoint, h3_inputs, gap, message):
    schema = endpoint.endpoint_transport.schema
    if gap == 'empty':
        schema.clear()
    elif gap == 'node':
        schema.pop('MiniMaxH3ReferenceToVideo')
    elif gap == 'model':
        schema['UNETLoader']['input']['required']['unet_name'][0] = ['other.safetensors']
    elif gap == 'field':
        schema['MiniMaxH3ReferenceToVideo']['input']['required']['unsupported_required'] = ['INT']
    else:
        schema['MiniMaxH3ReferenceToVideo'] = {'input': 'not a schema'}
    with pytest.raises(RuntimeError, match=message):
        run.preflight_endpoint(h3_inputs, endpoint)


def test_member_tampering_fails_before_schema_observation(endpoint, compilation, tmp_path, monkeypatch):
    compiled, _ = compilation
    Path(compiled['workflow']['source.json']['path']).write_text('{}')
    monkeypatch.setattr(endpoint, 'object_info', lambda: pytest.fail('schema before hash gate'))
    with pytest.raises(ValueError, match='bytes changed'):
        preflight.require_h3_graph_readiness(compiled, endpoint, tmp_path)


def setup_sdk(monkeypatch, tmp_path, endpoint, *, project_parent=False):
    import astrid.sdk as sdk
    from astrid.core.generation import entries
    from astrid.core.model_catalog.registry import ModelRegistry
    entry = {'id': 'h3', 'action': 'h3_av.transform', 'kind': 'video', 'modes': ['transform'],
        'inputs': [{'name': name, 'type': 'file', 'required': True, 'file_custody': 'producer_local'}
                   for name in ('request', 'asset_map')]}
    monkeypatch.setattr(entries, 'describe_entry', lambda *a, **k: {**entry, 'source': 'action', 'mode': 'transform'})
    monkeypatch.setattr(sdk, '_load_model_registry', lambda **k: ModelRegistry([]))

    class Owner(FakeOwner):
        h3_endpoint_route = True

        def prepare_h3(self, **kwargs):
            prepared = super().prepare_h3(**kwargs)
            return replace(prepared, h3_target=endpoint, h3_warm_lease_seconds=1800)

    owner = Owner(tmp_path, endpoint)
    claim = json.loads(owner.selection.claim_handle_path.read_text())
    claim['pod_id'] = 'pod123'
    owner.selection.claim_handle_path.write_text(json.dumps(claim))
    owner.selection = replace(owner.selection, target={**owner.selection.target, 'pod_id': 'pod123'})
    calls = []
    client = SimpleNamespace(runpod_generation_owner=owner)

    def invoke(capability, **kwargs):
        calls.append((capability, kwargs))
        if project_parent:
            from astrid.core.execution.executor.actions import action_executor_definition
            from astrid.core.execution.executor.registry import ExecutorRegistry
            from astrid.core.pack.loader import load_pack_manifest
            definitions = []
            for pack_name in ('h3_av', 'vibecomfy'):
                pack = load_pack_manifest(ROOT / 'astrid/packs' / pack_name / 'pack.yaml')
                definitions.extend(action_executor_definition(pack, key, action) for key, action in pack.actions.items())
            registry = ExecutorRegistry(definitions)
            retained = invocation._prepared_owner_for(client, kwargs['recovery_path'], capability)
            retained.verify_h3_endpoint()
            return run.project_parent(kwargs['inputs'], project=kwargs['project'], client=client,
                                      registry=registry, h3_endpoint=retained.h3_endpoint)
        return {'task_id': 'parent-a'}

    client.invoke_result = invoke
    client.projects = SimpleNamespace(show=lambda _: SimpleNamespace(ok=True, data={'project_id': 'project-a'}))
    def import_bytes(**kwargs):
        return SimpleNamespace(ok=True, data={'object_id': 'sha256:' + hashlib.sha256(kwargs['data']).hexdigest()})
    client.media = SimpleNamespace(import_bytes=import_bytes)
    return owner, client, calls


def test_sdk_preflight_and_parent_freeze_one_endpoint_with_all_local_stages(
        monkeypatch, tmp_path, endpoint, h3_inputs):
    owner, client, calls = setup_sdk(monkeypatch, tmp_path, endpoint, project_parent=True)
    projected, policy = GenerationFacade().video(entry='h3', target='runpod', client=client,
        project='project-a', wait=False, **h3_inputs, **PAID)
    assert len(calls) == 1
    assert 'prepared_worker' not in calls[0][1]['inputs']
    assert 'execution_request' not in calls[0][1]
    assert [event[0] if isinstance(event, tuple) else event for event in owner.events] == [
        'validate-h3', 'validate-target', 'prepare-h3']
    handoff = owner.events[-1][1]['inputs']['_h3_endpoint']
    assert projected['plan']['controls']['h3_endpoint'] == handoff
    assert handoff['warm_lease_seconds'] == 1800
    assert handoff['target'] == preflight.freeze_h3_endpoint(endpoint)
    assert projected['plan']['compilation_digest'] == handoff['preflight']['compilation_digest']
    assert policy['targets'] == [{'kind': 'default'}]
    assert all(stage['target'] == {'kind': 'default'} for stage in policy['stages'])
    assert len(client._generation_prepared_owners) == 1


@pytest.mark.parametrize('fault', ['missing-node', 'controls', 'changed-endpoint', 'cold', 'approval-type', 'multipass', 'placeholder', 't9', 't9-empty'])
def test_sdk_unsupported_cases_never_prepare_or_admit(monkeypatch, tmp_path, endpoint, h3_inputs, fault):
    owner, client, calls = setup_sdk(monkeypatch, tmp_path, endpoint)
    if fault == 'missing-node':
        endpoint.endpoint_transport.schema.clear()
    elif fault == 'controls':
        h3_inputs['prepared_worker'] = 'untrusted.json'
    elif fault == 'changed-endpoint':
        # Ready endpoint identity changes during schema observation.
        original = endpoint.object_info
        def substitute():
            result = original()
            endpoint.endpoint_transport._endpoint = replace(endpoint.endpoint_transport.endpoint,
                lifecycle_owner=replace(endpoint.endpoint_transport.endpoint.lifecycle_owner, owner_id='other'))
            return result
        monkeypatch.setattr(endpoint, 'object_info', substitute)
    elif fault == 'cold':
        owner.validation = ComfyTargetClient(ComfyTarget('http://127.0.0.1:18188', 'attached'))
    elif fault == 'approval-type':
        h3_inputs['editorial_approved'] = 'yes'
    elif fault == 'multipass':
        from tests.packs.h3_av.test_planner import _source_edit
        multiple = _source_edit(extra_video=({'during': [3, 4], 'area': {'full_frame': True}, 'action': 'generate'},))
        Path(h3_inputs['request']).write_text(json.dumps(multiple.value))
    elif fault in {'t9', 't9-empty'}:
        monkeypatch.setenv('ASTRID_H3_T9_MODEL_SUBSTITUTE_PROFILE', '' if fault == 't9-empty' else '/unopened-synthetic-profile.json')
    else:
        document = json.loads(Path(h3_inputs['request']).read_text())
        document['content']['prompt'] = '{{PROMPT}}'
        Path(h3_inputs['request']).write_text(json.dumps(document))
    with pytest.raises(Exception, match='T9 substitute' if fault.startswith('t9') else None):
        GenerationFacade().video(entry='h3', target='runpod', client=client, project='project-a',
                                 **h3_inputs, **PAID)
    assert not calls
    assert not any(isinstance(event, tuple) and event[0] == 'prepare-h3' for event in owner.events)


def test_recovery_reuses_retained_endpoint_and_refuses_substitution(monkeypatch, tmp_path, endpoint, h3_inputs):
    owner, client, calls = setup_sdk(monkeypatch, tmp_path, endpoint)
    path = tmp_path / 'parent-recovery.json'
    facade = GenerationFacade()
    kwargs = dict(entry='h3', target='runpod', client=client, project='project-a',
                  recovery_path=path, **h3_inputs, **PAID)
    facade.video(**kwargs)
    follow = {key: value for key, value in kwargs.items() if key not in PAID}
    assert facade.video(**follow, resume=True) == {'task_id': 'parent-a'}
    assert sum(isinstance(event, tuple) and event[0] == 'prepare-h3' for event in owner.events) == 1
    retained = invocation._prepared_owner_for(client, path, 'h3_av.transform')
    task = {'task_id': 'parent-a', 'run_id': 'run-a', 'capability_id': 'h3_av.transform', 'state': 'succeeded'}
    retained(task)
    with monkeypatch.context() as finished:
        finished.setattr(retained, 'verify_h3_endpoint', lambda: pytest.fail('reopened a terminal owner bridge'))
        retained(task)
    assert sum(isinstance(event, tuple) and event[0] == 'retire' for event in owner.events) == 1
    endpoint.endpoint_transport._endpoint = replace(endpoint.endpoint_transport.endpoint,
        lifecycle_owner=replace(endpoint.endpoint_transport.endpoint.lifecycle_owner, owner_id='substitute'))
    with pytest.raises(Exception, match='endpoint identity changed'):
        facade.video(**kwargs, resume=True)
    assert len(calls) == 2


@pytest.mark.parametrize('substitution', ['origin', 'attestation', 'owner', 'warm-lease'])
def test_prepared_endpoint_cannot_replace_readiness_target(monkeypatch, tmp_path, endpoint, h3_inputs, substitution):
    owner, client, calls = setup_sdk(monkeypatch, tmp_path, endpoint)
    original = owner.prepare_h3
    def prepare(**kwargs):
        prepared = original(**kwargs)
        transport = endpoint.endpoint_transport
        if substitution == 'origin':
            transport.origin = 'http://127.0.0.1:28188'
        elif substitution == 'attestation':
            transport._endpoint = replace(transport.endpoint,
                attestation=replace(transport.endpoint.attestation, install_receipt_sha256='d' * 64))
        elif substitution == 'owner':
            transport._endpoint = replace(transport.endpoint,
                lifecycle_owner=replace(transport.endpoint.lifecycle_owner, owner_id='foreign'))
        else:
            prepared = replace(prepared, h3_warm_lease_seconds=600)
        return prepared
    owner.prepare_h3 = prepare
    with pytest.raises(ValueError, match='1800 second warm lease' if substitution == 'warm-lease' else 'substituted the selected endpoint'):
        GenerationFacade().video(entry='h3', target='runpod', client=client, project='project-a',
                                 **h3_inputs, **PAID)
    assert not calls and not getattr(client, '_generation_prepared_owners', {})
    assert sum(isinstance(event, tuple) and event[0] == 'prepare-h3' for event in owner.events) == 1


def test_parent_rechecks_input_bytes_after_paid_handoff(monkeypatch, tmp_path, endpoint, h3_inputs):
    owner, client, _ = setup_sdk(monkeypatch, tmp_path, endpoint, project_parent=True)
    original = owner.prepare_h3
    def prepare(**kwargs):
        prepared = original(**kwargs)
        from PIL import Image
        Image.new('RGB', (32, 32), (80, 90, 100)).save(tmp_path / 'look.png')
        return prepared
    owner.prepare_h3 = prepare
    client.media.import_bytes = lambda **k: pytest.fail('import before frozen input comparison')
    with pytest.raises(RuntimeError, match='inputs changed after pre-acquisition'):
        GenerationFacade().video(entry='h3', target='runpod', client=client, project='project-a',
                                 **h3_inputs, **PAID)


def test_recovery_receipt_binds_endpoint_snapshot_in_the_existing_request(monkeypatch, tmp_path, endpoint):
    """Exercise the real SDK receipt wrapper; only admission/follow are doubled."""
    from astrid.sdk.recovery import AdmissionReceipt
    from astrid.sdk.exceptions import CapabilityValidationError
    h3_endpoint = {'target': preflight.freeze_h3_endpoint(endpoint), 'preflight': {'compilation_digest': 'a' * 64}}
    client = SimpleNamespace(handshake=lambda *a: {'realm_id': 'realm-a'})
    recovery = tmp_path / 'recovery.json'
    invocation.retain_prepared_owner(client, recovery_path=recovery, capability_id='h3_av.transform',
        monitor=lambda task: None, h3_target=endpoint, h3_endpoint=h3_endpoint)
    observed = []
    enter = AdmissionReceipt.__enter__
    def freeze(receipt):
        result = enter(receipt)
        if not receipt.resume:
            observed.append(receipt.request_digest)
            receipt.freeze({'idempotency_key': 'same-admission', 'capability_id': 'h3_av.transform'},
                metadata={'capability_id': 'h3_av.transform', 'capability_type': 'executor', 'native_kind': 'action'})
        return result
    monkeypatch.setattr(AdmissionReceipt, '__enter__', freeze)
    # The second recursive call uses _recovery and reaches discovery. Stop
    # there, after the genuine wrapper has frozen and stored its request.
    class Admitted(RuntimeError):
        pass
    monkeypatch.setattr(invocation, '_sdk_module', lambda: (_ for _ in ()).throw(Admitted()))
    args = dict(kind='action', project='project-a', inputs={'request': 'request.json', 'asset_map': 'assets.json'},
                client=client, recovery_path=recovery, wait=True)
    with pytest.raises(Admitted):
        invocation.invoke('h3_av.transform', **args)
    monkeypatch.setattr(invocation, '_resume_receipt_invocation', lambda receipt, **k: receipt.data['admission'])
    assert invocation.invoke('h3_av.transform', **args, resume=True)['idempotency_key'] == 'same-admission'
    retained = invocation._prepared_owner_for(client, recovery, 'h3_av.transform')
    # Even a self-consistent replacement target cannot reuse the frozen receipt.
    endpoint.endpoint_transport.origin = 'http://127.0.0.1:28188'
    retained.h3_endpoint['target'] = preflight.freeze_h3_endpoint(endpoint)
    with pytest.raises(CapabilityValidationError, match='request/project identity mismatch'):
        invocation.invoke('h3_av.transform', **args, resume=True)
    assert len(observed) == 1


@pytest.mark.parametrize('tamper', [False, True], ids=['exact', 'changed-bindings'])
def test_staged_parent_hands_exact_graph_to_local_run(monkeypatch, tmp_path, endpoint, compilation, h3_inputs, tamper):
    compiled, sealed = compilation
    owner, client, _ = setup_sdk(monkeypatch, tmp_path, endpoint, project_parent=True)
    projected, policy = GenerationFacade().video(entry='h3', target='runpod', client=client,
        project='project-a', **h3_inputs, **PAID)
    plan = projected['plan']
    assert plan['compilation_digest'] == sealed['compilation_digest']
    assert policy['targets'] == [{'kind': 'default'}]
    parent = run.ManagedParentClient(plan, tmp_path)
    if tamper:
        compiled['workflow_inputs']['prompt'] = 'Changed after admission.'
    manifest = tmp_path / 'staged-compilation.json'
    manifest.write_text(json.dumps(compiled))
    files = {'compilation': manifest, 'managed_assets': Path(compiled['managed_assets']['path'])}
    files.update({port: Path(compiled['workflow'][name]['path'])
                  for port, name in (('python', 'workflow.py'), ('companion', 'workflow.vibe.json'), ('source', 'source.json'))})
    def materialize(_client, _result, port, *_args, **kwargs):
        path = files[port]
        return path, {'object_id': 'sha256:' + hashlib.sha256(path.read_bytes()).hexdigest()}
    calls = []
    monkeypatch.setattr(run, '_materialize_output', materialize)
    monkeypatch.setattr(run, '_invoke_stage', lambda _client, capability, **k: calls.append((capability, k)) or object())
    class ReadyForGpu(RuntimeError):
        pass
    def gpu(_client, **kwargs):
        assert _client is parent
        assert kwargs['placement'] == 'local' and kwargs['execution_request'] is None
        assert kwargs['inputs']['comfy_url'] == endpoint.http_origin
        assert json.loads(kwargs['inputs']['workflow_inputs']) == compiled['workflow_inputs']
        assert kwargs['inputs']['python']['object_id'] == 'sha256:' + compiled['workflow']['workflow.py']['sha256']
        calls.append(('vibecomfy.run', kwargs))
        raise ReadyForGpu('ready for GPU')
    monkeypatch.setattr(run, '_invoke_canonical_run', gpu)
    prepared = {'bundle_descriptor': {}, 'source_inputs': {}, 'preparation_path': tmp_path / 'preparation.json',
                'preparation_row': {'object_id': 'sha256:' + 'e' * 64},
                'preparation': {'request_digest': compiled['request_digest']}}
    args = SimpleNamespace(_managed_client=parent, project='project-a', resume=False)
    with pytest.raises(RuntimeError, match='graph differs' if tamper else 'ready for GPU'):
        run._execute_verified_pass(parent, args=args, root=tmp_path, request=None, raw_request={}, asset_map={},
            journal=None, idempotency_context={}, local_engine=None, prepared_selection=None,
            execution_request=None, placement='local', prepared_input=prepared)
    assert [capability for capability, _ in calls] == (
        ['h3_av.compile'] if tamper else ['h3_av.compile', 'vibecomfy.validate', 'vibecomfy.run'])
    if not tamper:
        # Structural/security validation stays offline; concrete target checks
        # remain in the run adapter immediately before prompt submission.
        assert calls[1][1]['inputs']['python_execution_consent'] == 'confirmed'
        assert 'workflow_inputs' not in calls[1][1]['inputs']


def test_parent_run_rejects_url_substitution_before_child_admission(monkeypatch, tmp_path, endpoint):
    plan = {'controls': {'h3_endpoint': {'target': preflight.freeze_h3_endpoint(endpoint)}},
            'stages': [{'name': 'run', 'inputs': []}]}
    parent = run.ManagedParentClient(plan, tmp_path)
    monkeypatch.setattr(invocation, 'invoke_result', lambda *a, **k: pytest.fail('substituted child admitted'))
    with pytest.raises(Exception, match='endpoint differs'):
        parent.invoke_result('vibecomfy.run', inputs={'comfy_url': 'http://127.0.0.1:28188'})


def test_run_adapter_rechecks_exact_h3_graph_before_any_prompt(monkeypatch, tmp_path, endpoint, compilation):
    from vibecomfy.security.provenance import Provenance
    from vibecomfy.workflow_bundle import load_bundle
    from astrid.packs.vibecomfy.local_server_engine import run_local_server
    compiled, _ = compilation
    preflight.require_h3_graph_readiness(compiled, endpoint, tmp_path)
    endpoint.endpoint_transport.schema.clear()
    monkeypatch.setattr(ComfyTargetClient, 'object_info', lambda _: endpoint.endpoint_transport.object_info())
    monkeypatch.setattr(ComfyTargetClient, 'submit_prompt', lambda *a: pytest.fail('unready prompt submitted'))
    bundle = load_bundle(Path(compiled['workflow']['workflow.py']['path']), trust=Provenance.USER_CONFIRMED)
    with pytest.raises(Exception, match='missing nodes'):
        run_local_server(bundle, tmp_path / 'run', comfy_url=endpoint.http_origin, comfy_output_dir='',
                         task_identity='parent-run', attempt_identity='attempt-a',
                         run_inputs=compiled['workflow_inputs'])


def test_run_adapter_approves_the_final_uploaded_h3_graph_before_prompt(monkeypatch, tmp_path, endpoint, compilation):
    import asyncio
    from astrid.core.generation.backends import vibecomfy_local_server as attached
    from astrid.packs.vibecomfy.actions.run.run import _run_and_settle
    import importlib
    runtime_run = importlib.import_module('vibecomfy.runtime.run')
    compiled, _ = compilation
    monkeypatch.setattr(ComfyTargetClient, 'object_info', lambda _: endpoint.endpoint_transport.object_info())
    uploaded = []
    def upload(url, path, **kwargs):
        assert url == endpoint.http_origin
        assert path.is_file()
        uploaded.append(path.name)
        return 'astrid/parent-run/' + path.name
    monkeypatch.setattr(attached, 'upload_media', upload)
    monkeypatch.setattr(attached, 'upload_image', upload)
    submitted = []
    class PromptBoundary(RuntimeError):
        pass
    def submit(client, payload):
        assert client.http_origin == endpoint.http_origin
        submitted.append(payload)
        raise PromptBoundary('GPU prompt boundary')
    monkeypatch.setattr(ComfyTargetClient, 'submit_prompt', submit)
    def run_sync(record, bundle, **kwargs):
        assert record.revision_id == bundle.revision_id
        api = record.to_dict()['api_projection']
        assert any(node['inputs'].get('unet_name') == compiled['workflow_inputs']['model'] for node in api.values())
        assert any(node['inputs'].get('image', '').startswith('astrid/parent-run/')
                   for node in api.values() if isinstance(node['inputs'].get('image', ''), str))
        assert any('attempt-a' in node['inputs'].get('filename_prefix', '') for node in api.values())
        asyncio.run(kwargs['server_client']._post_prompt(api))
    monkeypatch.setattr(runtime_run, 'run_sync', run_sync)
    with pytest.raises(PromptBoundary, match='GPU prompt boundary'):
        _run_and_settle(Path(compiled['workflow']['workflow.py']['path']), tmp_path / 'run',
            comfy_url=endpoint.http_origin, task_identity='parent-run', attempt_identity='attempt-a',
            placement='local', workflow_inputs=json.dumps(compiled['workflow_inputs']),
            managed_assets=compiled['managed_assets']['path'])
    assert len(submitted) == 1 and uploaded


def test_endpoint_freeze_never_starts_a_missing_bridge(endpoint):
    from astrid.core.generation.engine_contracts import TransportError
    real_transport = EngineEndpointTransport(endpoint.endpoint_transport.endpoint, pod_id='pod123', agent_port=8000)
    unstarted = ComfyTargetClient(ComfyTarget(real_transport.endpoint.url, 'pod'), endpoint_transport=real_transport)
    with pytest.raises(TransportError, match='bridge is not running'):
        preflight.freeze_h3_endpoint(unstarted)
    assert real_transport._server is None


def test_endpoint_freeze_does_not_consult_the_lazy_origin(endpoint, monkeypatch):
    monkeypatch.setattr(ComfyTargetClient, 'http_origin', property(
        lambda _: pytest.fail('freeze consulted a potentially restarting gateway getter')))
    assert preflight.freeze_h3_endpoint(endpoint)['comfy_url'] == 'http://127.0.0.1:18188'
