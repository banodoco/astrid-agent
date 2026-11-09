"""H3 parent freezes host target policy without preparing a remote worker."""
import json
from pathlib import Path
import sys
from types import SimpleNamespace

import pytest
from astrid.core.execution.executor.actions import action_executor_definition
from astrid.core.execution.executor.registry import ExecutorRegistry
from astrid.core.pack.loader import load_pack_manifest
from astrid.packs.h3_av.actions.transform import run
from astrid.packs.runpod import target_selection
from astrid.sdk.generation import GenerationFacade
from tests.sdk.test_h3_endpoint_r4 import captured_schema, endpoint, h3_inputs, setup_sdk
from tests.sdk.test_runpod_front_door import PAID

ROOT = Path(__file__).resolve().parents[2]


def test_h3_freezes_policy_into_default_local_stages(monkeypatch, tmp_path, endpoint, h3_inputs):
    owner, client, calls = setup_sdk(monkeypatch, tmp_path, endpoint)
    preferences = {'provider_account_ref': 'account-a', 'gpu_types': ['gpu-a'],
                   'image': 'worker@sha256:' + 'a' * 64}
    monkeypatch.setattr(target_selection, 'read_runpod_preferences',
        lambda: target_selection.RunPodPreferences.from_mapping(preferences))
    definitions = []
    for name in ('h3_av', 'vibecomfy'):
        pack = load_pack_manifest(ROOT / 'astrid/packs' / name / 'pack.yaml')
        definitions.extend(action_executor_definition(pack, key, action) for key, action in pack.actions.items())
    registry = ExecutorRegistry(definitions)

    def invoke(capability, **kwargs):
        calls.append((capability, kwargs))
        return run.project_parent(kwargs['inputs'], project=kwargs['project'], client=client, registry=registry)

    client.invoke_result = invoke
    projected, policy = GenerationFacade().video(entry='h3', target='runpod', client=client,
        project='project-a', wait=False, **h3_inputs, **PAID,
        offer_digest='sha256:' + 'b' * 64, preparation_seconds=60,
        execution_seconds=60, idle_seconds=1800)
    target = projected['plan']['controls']['target_policy']
    assert target['engine'] == 'vibecomfy-h3' and target['kind'] == 'runpod'
    assert target == calls[0][1]['inputs']['target_policy']
    assert policy['targets'] == [{'kind': 'default'}]
    assert all(stage['target'] == {'kind': 'default'} for stage in policy['stages'])
    assert owner.events == []


def test_h3_substitute_refuses_before_import_or_provider(monkeypatch, tmp_path, endpoint, h3_inputs):
    owner, client, _ = setup_sdk(monkeypatch, tmp_path, endpoint)
    monkeypatch.setenv('ASTRID_H3_T9_MODEL_SUBSTITUTE_PROFILE', 'test-only')
    monkeypatch.setattr(client.media, 'import_bytes', lambda **kwargs: pytest.fail('import before refusal'))
    with pytest.raises(Exception, match='cannot use the T9 substitute'):
        run.project_parent({**h3_inputs, 'target_policy': {'kind': 'runpod'}},
                           project='project-a', client=client, registry=None)
    assert owner.events == []


def test_staged_child_cannot_replace_parent_target_policy():
    from astrid.core.execution._child_bridge import ChildBridgeError, HostChildBridge

    class Admitted(Exception):
        pass

    expected = {'kind': 'runpod', 'offer_digest': 'sha256:' + 'a' * 64}
    bridge = object.__new__(HostChildBridge)
    bridge.policy = {'targets': [], 'capabilities': []}
    bridge.task = {'spec': {'inputs': {'plan': {'controls': {'target_policy': expected}}}}}
    bridge._child_execution_request = lambda value: None
    record = SimpleNamespace(id='vibecomfy.run', capability_digest='digest',
                             definition=SimpleNamespace(metadata={'host_target_policy': True, 'action_invocation': True}))
    def admit(*args):
        raise Admitted()
    bridge.host = SimpleNamespace(capabilities={record.id: record}, admit=admit)
    request = {'child': {'capability_id': record.id, 'capability_digest': 'digest'},
               'inputs': {'target_policy': json.dumps({**expected, 'offer_digest': 'sha256:' + 'b' * 64})},
               'input_descriptors': [], 'stage': 'run'}
    with pytest.raises(ChildBridgeError, match='differs from the admitted parent'):
        bridge._submit(request)
    request['inputs']['target_policy'] = json.dumps(expected)
    with pytest.raises(Admitted):
        bridge._submit(request)


def test_strict_startup_accepts_only_the_admitted_target_module(tmp_path):
    from astrid.core.execution.generic_host import HostError, _network_startup_argv

    module = 'astrid.packs.vibecomfy.actions.run.run'
    command = [sys.executable, '-X', 'utf8', '-m', module, 'run']
    with pytest.raises(HostError, match='strict network startup'):
        _network_startup_argv(command, tmp_path, {}, broker_required=False)
    with pytest.raises(HostError, match='strict network startup'):
        _network_startup_argv(command, tmp_path, {}, broker_required=False, admitted_module='foreign.module')
    with pytest.raises(HostError, match='strict network startup'):
        _network_startup_argv([sys.executable, '-c', 'pass'], tmp_path, {},
                              broker_required=False, admitted_module=module)
    wrapped = _network_startup_argv(command, tmp_path, {}, broker_required=False, admitted_module=module)
    assert wrapped[:6] == [sys.executable, '-X', 'utf8', '-I', '-S', '-c']
    config = json.loads(wrapped[7])
    assert config['module'] == module and config['captured'] is True and wrapped[8:] == ['run']


def test_final_publication_intent_stays_in_runtime_not_command(tmp_path):
    from astrid.core.contracts.binding import assert_provided_inputs_bound, expand_command

    pack = load_pack_manifest(ROOT / 'astrid/packs/h3_av/pack.yaml')
    definition = action_executor_definition(pack, 'publication_finalizer', pack.actions['publication_finalizer'])
    values = {'python_exec': sys.executable, 'candidate': str(tmp_path / 'candidate'),
              'verification': str(tmp_path / 'verification'), 'expected_request_digest': 'a' * 64,
              'generation_intent': {'version': 1, 'modality': 'video'}, 'out': str(tmp_path)}
    binding = expand_command(definition.command, definition.inputs, values, definition.metadata)
    assert_provided_inputs_bound(binding, definition.inputs, values, definition.metadata)
    assert 'generation_intent' in binding.host_consumed_inputs
    assert '--generation-intent' not in binding.argv
