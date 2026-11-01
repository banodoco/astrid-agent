"""A10 CPU contracts. All provider, Runtime, publication and host owners are fakes."""
from contextlib import nullcontext, redirect_stdout
from copy import deepcopy
import hashlib
import io
import json
from pathlib import Path
import socket
import subprocess
from types import SimpleNamespace

import pytest

from astrid.core.generation import cli, entries, preflight, targets
from astrid.core.model_catalog.registry import ModelRegistry
from astrid.packs.runpod.prepared_task import PreparedWorkerSelection
from astrid.sdk import invocation
from astrid.sdk.generation import GenerationFacade
from astrid.sdk.results import InvocationResult


PAID = dict(consent=('paid',), max_usd=2.0, deadline='2099-01-01T00:00:00Z')


@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    monkeypatch.setattr(socket.socket, 'connect', lambda *a, **k: pytest.fail('network used'))
    monkeypatch.setattr(socket, 'create_connection', lambda *a, **k: pytest.fail('network used'))


class FakeOwner:
    """R03 preparation and F07 retained owner; R04 release is admission only."""
    def __init__(self, root, validation_target):
        self.events = []
        self.validation = validation_target
        self.retained = {}
        claim = root / 'claim.json'
        claim.write_text(json.dumps({'state': 'claimed', 'pod_id': 'pod-a',
            'provider_account_ref': 'account-a', 'network_volume_id': 'volume-a'}))
        self.selection = PreparedWorkerSelection(claim, root / 'profile.json', root / 'staging.json',
            root / 'journal', {'kind': 'runpod', 'pod_id': 'pod-a',
                'provider_account_ref': 'account-a', 'storage': {'network_volume_id': 'volume-a'}}, {}, {})
        self.checkout = lambda **kwargs: pytest.fail('GPU checkout used')
        self.activated = set()
        self.retired = set()

    def validation_target(self):
        self.events.append('validate-target')
        return self.validation

    def monitor(self, task):
        key = (task['task_id'], task['run_id'])
        if task['state'] in {'succeeded', 'failed', 'cancelled'}:
            if key not in self.retired:
                self.retired.add(key)
                self.events.append(('retire', key))
        elif key not in self.activated:
            self.activated.add(key)
            self.events.append(('activate', key, deepcopy(task.get('execution_binding'))))

    def prepare(self, **kwargs):
        self.events.append(('prepare', kwargs))
        assert kwargs['consent'] == ('paid',)
        assert kwargs['max_usd'] == 2.0 and kwargs['deadline'] == PAID['deadline']
        return preflight.PreparedRunPodGeneration(self.selection, '/fake/selection.json', self.checkout, self.monitor)

    def validate_h3(self, inputs):
        self.events.append(('validate-h3', deepcopy(inputs)))
        if inputs['request'] == 'invalid.json':
            raise ValueError('invalid H3 request')
        return preflight.readiness_envelope(entry={'id': 'h3'}, target={'placement': 'runpod'})

    def prepare_h3(self, **kwargs):
        self.events.append(('prepare-h3', kwargs))
        return preflight.PreparedRunPodGeneration(self.selection, '/fake/selection.json', self.checkout, self.monitor)

    def retain(self, task, monitor):
        self.events.append(('retain', task['task_id']))
        self.retained[task['task_id']] = monitor

    def describe(self):
        self.events.append('describe')
        return {'engine': 'comfyui', 'placement': 'runpod', 'pod_id': 'pod-a', 'state': 'cleanup_pending'}

    def release(self, *, wait):
        self.events.append(('release', wait))
        # R04 fake: an admitted release never asserts provider absence.
        return {'task_id': 'release-task-a', 'idempotency_key': 'runpod-release:operation-a',
                'state': 'cleanup_pending'}


@pytest.fixture(params=['image', 'video'])
def route(request, tmp_path, monkeypatch):
    """Compile a real tiny bundle against an offline object_info snapshot."""
    from vibecomfy.schema.provider import TargetSchemaProvider
    from vibecomfy.workflow import VibeWorkflow, WorkflowSource
    from vibecomfy.workflow_bundle import emit_bundle
    import astrid.sdk as sdk

    kind = request.param
    sink = 'SaveImage' if kind == 'image' else 'VHS_VideoCombine'
    workflow = VibeWorkflow('a10-' + kind, WorkflowSource('a10-' + kind))
    workflow.add_node(sink, _id='1', uid='sink', filename_prefix='a10')
    workflow.register_input('filename_prefix', '1', 'filename_prefix', 'a10', type='STRING')
    info = {sink: {'input': {'required': {'filename_prefix': ['STRING', {'default': 'a10'}]}},
                   'output': [], 'output_name': [], 'output_is_list': [], 'output_node': True}}
    schema = TargetSchemaProvider.from_object_info(info, server_url='http://127.0.0.1:1', model_availability='target')
    monkeypatch.setattr(entries, '_schema_provider', lambda: schema)
    source = tmp_path / 'workflow.py'
    (tmp_path / 'source.json').write_text(json.dumps({'nodes': [
        {'id': 1, 'type': sink, 'inputs': [], 'widgets_values': ['a10']}], 'links': []}))
    emit_bundle(workflow, source, {'operation': 'authored'})
    mode = 't2i' if kind == 'image' else 't2v'
    entry = {'id': workflow.id, 'kind': kind, 'modes': [mode], 'bundle': str(source),
             'binds': {}, 'defaults': {}, 'needs': []}
    monkeypatch.setattr(entries, 'user_entries', lambda: [entry])
    monkeypatch.setattr(entries, 'ready_entries', lambda: [])
    monkeypatch.setattr(entries, 'action_entries', lambda: [])
    monkeypatch.setattr(sdk, '_load_model_registry', lambda **kwargs: ModelRegistry([]))
    monkeypatch.setattr(preflight, '_module_available', lambda name: True)
    monkeypatch.setattr(preflight, '_comfyui_runtime_available', lambda: pytest.fail('local managed runtime'))
    target = SimpleNamespace(object_info=lambda: info, http_origin='http://127.0.0.1:1',
        target=SimpleNamespace(url='https://pod-a.invalid/never-attach'),
        generation_target=targets.TargetRecord(owner='astrid'))
    owner = FakeOwner(tmp_path, target)
    calls = []
    tasks = {}

    def import_file(*, project, path):
        digest = 'sha256:' + hashlib.sha256(path.read_bytes()).hexdigest()
        return SimpleNamespace(ok=True, data={'object_id': digest, 'filename': path.name})

    client = SimpleNamespace(runpod_generation_owner=owner, media=SimpleNamespace(import_file=import_file),
                             tasks=SimpleNamespace(show=lambda task_id: tasks[task_id]))

    def invoke_result(capability_id, **kwargs):
        calls.append((capability_id, kwargs))
        task = {'task_id': 'task-a', 'run_id': 'run-a', 'capability_id': capability_id,
                'state': 'queued', 'execution_request': deepcopy(kwargs['execution_request']),
                'execution_binding': {'target': deepcopy(owner.selection.target), 'activation_id': 'activation-a'}}
        tasks['task-a'] = task
        tick = invocation._prepared_owner_for(client, kwargs['recovery_path'], capability_id)
        # F05 Runtime admission fake; F06 publication stays outside this unit.
        return invocation._observe_task_invocation(client, capability_id=capability_id,
            capability_type='executor', native_kind='command', task_id='task-a', run_id='run-a',
            initial_task=task, initial_ok=True, wait=kwargs['wait'], owner_tick=tick,
            timeout_seconds=1, poll_seconds=0.001, discover_managed_outputs=False)

    client.invoke_result = invoke_result
    return SimpleNamespace(kind=kind, mode=mode, entry=entry, source=source, owner=owner,
                           client=client, calls=calls, tasks=tasks, tmp=tmp_path)


def generate_route(route, *, raw=False, wait=False, **kwargs):
    if raw:
        return GenerationFacade().workflow(workflow=route.source, target='runpod', inputs={},
            client=route.client, project='demo', wait=wait, **kwargs)
    return getattr(GenerationFacade(), route.kind)(entry=route.entry['id'], target='runpod',
        client=route.client, project='demo', wait=wait, **kwargs)


@pytest.mark.parametrize('raw', [False, True])
def test_waiting_route_activates_once_and_reconstructs_publication_fake(route, raw):
    from astrid.core.generation import GENERATION_RESULT_KEY
    from astrid.core.generation.backends.base import GenerationResult
    monitor = route.owner.monitor
    def settle(task):
        monitor(task)
        if task['state'] == 'queued':
            route.tasks['task-a'].update(state='succeeded', result={
                'outputs': [], 'payload': {GENERATION_RESULT_KEY: GenerationResult(run_dir=route.tmp).to_dict()}})
    route.owner.monitor = settle
    result = generate_route(route, raw=raw, wait=True, **PAID)
    assert isinstance(result, GenerationResult) and result.task_id == 'task-a'
    assert len(route.calls) == len(route.owner.activated) == len(route.owner.retired) == 1


@pytest.mark.parametrize('raw', [False, True])
def test_prepared_image_video_and_raw_admit_once_and_follow_same_binding(route, raw):
    result = generate_route(route, raw=raw, **PAID)
    assert result.ok and result.kernel_task_id == 'task-a'
    assert len(route.calls) == 1
    capability, args = route.calls[0]
    assert capability == 'vibecomfy.run' and args['wait'] is False
    assert 'comfy_url' not in args['inputs']
    assert args['execution_request'] == route.owner.selection.execution_request(None)
    assert args['execution_request']['target']['pod_id'] == 'pod-a'
    assert route.owner.events[0] == 'validate-target'
    tick = route.client._generation_task_owners['task-a']
    assert tick.endpoint_checkout is route.owner.checkout
    assert tick is route.owner.retained['task-a']
    before = deepcopy(route.tasks['task-a']['execution_binding'])
    route.tasks['task-a']['state'] = 'succeeded'
    route.tasks['task-a']['result'] = {'outputs': []}
    followed = invocation.observe_task_invocation(route.client, capability_id=capability,
        capability_type='executor', native_kind='command', task_id='task-a', run_id='run-a',
        wait=True, poll_seconds=0.001)
    assert followed.ok and followed.kernel_task_id == result.kernel_task_id
    assert len(route.calls) == 1
    assert route.tasks['task-a']['execution_binding'] == before
    assert len([event for event in route.owner.events if isinstance(event, tuple) and event[0] == 'prepare']) == 1
    assert len(route.owner.activated) == len(route.owner.retired) == 1


def test_dry_run_writes_nothing_and_does_not_claim(route, monkeypatch):
    monkeypatch.setattr(Path, 'home', lambda: route.tmp)
    before = {p: p.read_bytes() for p in route.tmp.rglob('*') if p.is_file()}
    result = generate_route(route, dry_run=True, **PAID)
    assert result['status'] == 'ready' and result['target']['placement'] == 'runpod'
    assert route.owner.events == ['validate-target'] and not route.calls
    assert {p: p.read_bytes() for p in route.tmp.rglob('*') if p.is_file()} == before


@pytest.mark.parametrize('changes', [dict(consent=()), dict(max_usd=None), dict(deadline=None),
    dict(deadline='2020-01-01T00:00:00Z'), dict(max_usd=float('nan')), dict(engine='wan2gp')])
def test_missing_fresh_paid_authority_denies_before_claim(route, changes):
    with pytest.raises((ValueError, RuntimeError)) as raised:
        generate_route(route, **{**PAID, **changes})
    assert raised.value.to_envelope()['status'] == 'blocked'
    assert route.owner.events == [] and not route.calls


def test_invalid_entry_values_and_raw_source_block_before_claim(route):
    with pytest.raises((ValueError, RuntimeError)):
        generate_route(route, inputs={'unknown': 3}, **PAID)
    route.source.write_text('not a canonical workflow')
    with pytest.raises((ValueError, RuntimeError)):
        generate_route(route, raw=True, **PAID)
    assert not any(isinstance(e, tuple) and e[0] == 'prepare' for e in route.owner.events)
    assert route.calls == []


def test_cli_paid_flags_match_sdk_and_retain_admission(route, monkeypatch, capsys):
    monkeypatch.setattr(cli, '_open_client', lambda: nullcontext(route.client))
    assert cli.main([route.kind, '--entry', route.entry['id'], '--target', 'runpod', '--project', 'demo',
        '--consent', 'paid', '--max-usd', '2', '--deadline', PAID['deadline'], '--no-wait', '--json']) == 0
    result = json.loads(capsys.readouterr().out)
    assert result['kernel_task_id'] == 'task-a' and len(route.calls) == 1
    preparation = next(e[1] for e in route.owner.events if isinstance(e, tuple) and e[0] == 'prepare')
    assert {key: preparation[key] for key in PAID} == PAID


def test_cli_no_wait_text_returns_task_without_claiming_completion(route, monkeypatch, capsys):
    monkeypatch.setattr(cli, '_open_client', lambda: nullcontext(route.client))
    assert cli.main([route.kind, '--entry', route.entry['id'], '--target', 'runpod', '--project', 'demo',
        '--consent', 'paid', '--max-usd', '2', '--deadline', PAID['deadline'], '--no-wait']) == 0
    assert capsys.readouterr().out == 'task: task-a\n'


def test_target_describe_and_release_use_typed_owner_without_claim(route, monkeypatch, capsys):
    monkeypatch.setattr(cli, '_open_client', lambda: nullcontext(route.client))
    assert cli.main(['describe', '--target', 'runpod', '--json']) == 0
    assert json.loads(capsys.readouterr().out)['pod_id'] == 'pod-a'
    assert cli.main(['release', '--engine', 'comfyui', '--target', 'runpod', '--no-wait', '--json']) == 0
    release = json.loads(capsys.readouterr().out)
    assert release['idempotency_key'] == 'runpod-release:operation-a'
    assert release['state'] == 'cleanup_pending'
    assert route.owner.events == ['describe', ('release', False)]


def test_release_denial_keeps_the_runtime_task_and_blocked_envelope(route, monkeypatch, capsys):
    route.owner.release = lambda **kwargs: InvocationResult('runpod.release', 'executor', 'command', False,
        error={'code': 'cleanup_pending', 'message': 'drain is uncertain'}, kernel_task_id='release-task-a')
    monkeypatch.setattr(cli, '_open_client', lambda: nullcontext(route.client))
    assert cli.main(['release', '--engine', 'comfyui', '--target', 'runpod', '--json']) == 2
    result = json.loads(capsys.readouterr().out)
    assert result['status'] == 'blocked' and result['task_id'] == 'release-task-a'
    assert result['issues'][0]['code'] == 'cleanup_pending'


def test_absent_host_interface_is_a_blocked_sdk_and_cli_envelope(route, monkeypatch, capsys):
    del route.client.runpod_generation_owner
    with pytest.raises(preflight.GenerationReadinessError) as raised:
        generate_route(route, **PAID)
    monkeypatch.setattr(cli, '_open_client', lambda: nullcontext(route.client))
    assert cli.main([route.kind, '--entry', route.entry['id'], '--target', 'runpod', '--project', 'demo',
        '--consent', 'paid', '--max-usd', '2', '--deadline', PAID['deadline'], '--no-wait', '--json']) == 2
    assert json.loads(capsys.readouterr().out) == raised.value.to_envelope()
    assert route.calls == []


def test_foreign_binding_on_follow_never_reactivates(route):
    generate_route(route, **PAID)
    route.tasks['task-a']['execution_binding']['activation_id'] = 'foreign'
    with pytest.raises(RuntimeError, match='binding changed'):
        invocation.observe_task_invocation(route.client, capability_id='vibecomfy.run',
            capability_type='executor', native_kind='command', task_id='task-a', run_id='run-a',
            wait=True, poll_seconds=0.001)
    assert len(route.calls) == len(route.owner.activated) == 1


def test_no_wait_retains_exact_custody_when_activation_is_uncertain(route):
    def fail(task):
        raise RuntimeError('activation response lost')
    route.owner.monitor = fail
    with pytest.raises(RuntimeError, match='activation response lost'):
        generate_route(route, **PAID)
    assert len(route.calls) == 1
    assert route.owner.retained['task-a'] is route.client._generation_task_owners['task-a']
    assert not route.owner.retired


def test_same_host_h3_follow_retains_one_monitor_while_run_child_appears(tmp_path):
    owner = FakeOwner(tmp_path, None)
    client = SimpleNamespace()
    child = {'present': False, 'ensured': False, 'ensure_count': 0}
    def h3_monitor(parent):
        # F07 fake has the same polling responsibility as PreparedH3OwnerMonitor:
        # the run child may appear after the no-wait parent receipt is returned.
        if child['present'] and not child['ensured']:
            child['ensured'] = True
            child['ensure_count'] += 1
    tick = invocation.retain_prepared_owner(client, recovery_path=tmp_path / 'h3-receipt.json',
        capability_id='h3_av.transform', monitor=h3_monitor, owner=owner)
    parent = {'task_id': 'parent-a', 'run_id': 'run-a', 'capability_id': 'h3_av.transform', 'state': 'running'}
    client.tasks = SimpleNamespace(show=lambda task_id: parent)
    result = invocation._observe_task_invocation(client, capability_id='h3_av.transform',
        capability_type='executor', native_kind='command', task_id='parent-a', run_id='run-a',
        initial_task=parent, initial_ok=True, wait=False, owner_tick=tick, discover_managed_outputs=False)
    assert result.ok and child['ensure_count'] == 0
    parent['execution_binding'] = {'effective_target': {'kind': 'default'}}
    child['present'] = True
    owner.retained['parent-a'](parent)
    owner.retained['parent-a'](parent)
    parent.update(state='succeeded', result={'outputs': []})
    followed = invocation.observe_task_invocation(client, capability_id='h3_av.transform',
        capability_type='executor', native_kind='command', task_id='parent-a', run_id='run-a', wait=True)
    assert followed.ok and child['ensure_count'] == 1
    assert client._generation_task_owners['parent-a'] is tick


def test_queued_task_waits_for_its_first_exact_binding_without_readmission(route):
    request = route.owner.selection.execution_request(None)
    tick = invocation.retain_prepared_owner(route.client, recovery_path=route.tmp / 'queued.json',
        capability_id='vibecomfy.run', monitor=route.owner.monitor, owner=route.owner,
        execution_request=request, endpoint_checkout=route.owner.checkout)
    task = {'task_id': 'queued-a', 'run_id': 'run-a', 'capability_id': 'vibecomfy.run',
            'execution_request': request, 'state': 'queued', 'execution_binding': None}
    admitted = invocation._observe_task_invocation(route.client, capability_id='vibecomfy.run',
        capability_type='executor', native_kind='command', task_id='queued-a', run_id='run-a',
        initial_task=task, initial_ok=True, wait=False, owner_tick=tick, discover_managed_outputs=False)
    assert admitted.ok and not route.owner.activated
    task['execution_binding'] = {'effective_target': deepcopy(request['target']), 'activation_id': 'activation-a'}
    route.owner.retained['queued-a'](task)
    route.owner.retained['queued-a'](task)
    assert len(route.owner.activated) == 1 and route.calls == []


def test_foreign_first_binding_is_rejected_before_activation(route):
    request = route.owner.selection.execution_request(None)
    tick = invocation.retain_prepared_owner(route.client, recovery_path=route.tmp / 'foreign.json',
        capability_id='vibecomfy.run', monitor=route.owner.monitor, owner=route.owner, execution_request=request)
    with pytest.raises(RuntimeError, match='exact RunPod target'):
        tick({'task_id': 'foreign-a', 'run_id': 'run-a', 'capability_id': 'vibecomfy.run', 'state': 'queued',
              'execution_request': request, 'execution_binding': {'target': {'kind': 'runpod', 'pod_id': 'other'}}})
    assert not route.owner.activated


def test_h3_no_wait_uses_prepared_child_and_preserves_parent_default(tmp_path, monkeypatch):
    entry = {'id': 'h3', 'action': 'h3_av.transform', 'kind': 'video', 'modes': ['transform'],
             'inputs': [{'name': 'request', 'type': 'file', 'required': True, 'file_custody': 'producer_local'},
                        {'name': 'asset_map', 'type': 'file', 'required': True, 'file_custody': 'producer_local'}]}
    monkeypatch.setattr(entries, 'find_entry', lambda name: entry if name == 'h3' else None)
    monkeypatch.setattr(entries, 'describe_entry', lambda *a, **k: {**entry, 'source': 'action', 'mode': 'transform'})
    import astrid.sdk as sdk
    monkeypatch.setattr(sdk, '_load_model_registry', lambda **k: ModelRegistry([]))
    owner = FakeOwner(tmp_path, None)
    calls = []
    client = SimpleNamespace(runpod_generation_owner=owner,
        invoke_result=lambda *args, **kwargs: calls.append((args, kwargs)) or {'task_id': 'h3-parent'})
    facade = GenerationFacade()
    assert facade.video(entry='h3', target='runpod', request='request.json', asset_map='assets.json',
        client=client, project='demo', wait=False, **PAID) == {'task_id': 'h3-parent'}
    assert len(calls) == 1 and calls[0][0] == ('h3_av.transform',)
    args = calls[0][1]
    assert args['wait'] is False and args['inputs']['prepared_worker'] == '/fake/selection.json'
    assert 'execution_request' not in args
    assert [e[0] for e in owner.events] == ['validate-h3', 'prepare-h3']
    assert len(client._generation_prepared_owners) == 1
    with pytest.raises(ValueError, match='invalid H3'):
        facade.video(entry='h3', target='runpod', request='invalid.json', asset_map='assets.json',
            client=client, project='demo', wait=False, **PAID)
    assert len(calls) == 1


def test_note_11_unqualified_wan_mcp_generate_and_generic_invoke_refuse_identically(monkeypatch):
    """Exercise the existing MCP CLI adapter; A05 remains its sole writer."""
    from astrid import mcp_server
    import astrid.sdk as sdk
    monkeypatch.setattr(cli, '_open_client', lambda: nullcontext(None))
    monkeypatch.setattr(invocation, '_kernel_invoke', lambda *a, **k: pytest.fail('unqualified admission'))
    monkeypatch.setattr(preflight, 'select_generation_target', lambda *a, **k: pytest.fail('unqualified target'))
    def run(argv, cwd):
        output = io.StringIO()
        with redirect_stdout(output):
            code = cli.main(argv[argv.index('generate') + 1:])
        return subprocess.CompletedProcess(argv, code, output.getvalue(), '')
    monkeypatch.setattr(mcp_server, '_run', run)
    result = mcp_server.call_tool(mcp_server._generation_tools()['generate_video'],
        {'model': 'wan-2.2', 'mode': 'i2v', 'prompt': 'boat', 'image_ref': 'start.png'})
    # Stub only discovery. The model matrix, qualification gate, SDK invoke
    # and MCP/CLI refusal projection remain production code.
    monkeypatch.setattr(sdk, '_load_registries', lambda **k: (None, None, None))
    monkeypatch.setattr(sdk, 'get_capability', lambda *a, **k: SimpleNamespace(
        id='generation.generate_video', capability_type='executor', native_kind='command',
        definition={}, inputs=(), outputs=()))
    with pytest.raises(Exception) as raised:
        sdk.invoke('generation.generate_video', kind='executor',
            inputs={'model': 'wan-2.2', 'mode': 'i2v', 'execution': 'local', 'prompt': 'boat', 'image_ref': 'start.png'},
            dry_run=True)
    generic = preflight.generation_error(raised.value).to_envelope()
    assert result['isError']
    assert result['structuredContent']['issues'] == generic['issues']
    assert generic['issues'][0]['code'] == 'generation_route_unqualified'
