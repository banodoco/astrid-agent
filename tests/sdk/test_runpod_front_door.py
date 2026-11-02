"""Retained-host SDK/CLI contracts, plus intact legacy owner census checks."""
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
from tests.integrations.test_runpod_host_endpoint_r5 import (
    host_route, retained_host, tuple_sources, retained, small_lease, lease_selection_case, sources,
)


def _host_policy(task):
    spec = task['spec'].get('spec', task['spec'])
    return json.loads(spec['inputs']['target_policy'])


def _host_cli_args(r):
    return [r.kind, '--entry', 'retained-' + r.kind, '--target', 'runpod', '--project', r.project,
        '--consent', 'paid', '--max-usd', str(r.paid['max_usd']), '--deadline', r.paid['deadline'],
        '--offer-digest', r.paid['offer_digest'], '--preparation-seconds', '60',
        '--execution-seconds', '60', '--idle-seconds', '1800', '--no-wait']


PAID = dict(consent=('paid',), max_usd=2.0, deadline='2099-01-01T00:00:00Z')


@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    connect = socket.socket.connect
    def local_only(sock, address):
        if isinstance(address, tuple):
            assert address[0] in {'127.0.0.1', '::1', 'localhost'}, 'external network used'
        else:
            assert sock.family == socket.AF_UNIX, 'external network used'
        return connect(sock, address)
    monkeypatch.setattr(socket.socket, 'connect', local_only)


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
def test_host_endpoint_route_admits_local_task_with_only_policy(route, raw, monkeypatch):
    from astrid.packs.runpod import target_selection
    preferences = {'provider_account_ref': 'account-a', 'gpu_types': ['gpu-a'],
                   'image': 'worker@sha256:' + 'a' * 64, 'idle_timeout_seconds': 1800}
    monkeypatch.setattr(target_selection, 'read_runpod_preferences', lambda: target_selection.RunPodPreferences.from_mapping(preferences))
    calls = []

    def invoke(capability, **kwargs):
        calls.append((capability, kwargs))
        return InvocationResult(capability, 'executor', 'command', True,
                                kernel_task_id='local-task', raw_result={'state': 'queued'})

    route.client.invoke_result = invoke
    paid = {**PAID, 'offer_digest': 'sha256:' + 'b' * 64, 'preparation_seconds': 60,
            'execution_seconds': 60, 'idle_seconds': 1800}
    result = generate_route(route, raw=raw, **paid)
    assert result.kernel_task_id == 'local-task' and len(calls) == 1
    capability, kwargs = calls[0]
    assert capability == 'vibecomfy.run' and kwargs.get('execution_request') is None
    inputs = kwargs['inputs']
    assert 'comfy_url' not in inputs
    policy = json.loads(inputs['target_policy'])
    assert policy['kind'] == 'runpod' and policy['offer_digest'] == paid['offer_digest']
    assert policy['preferences'] == target_selection.RunPodPreferences.from_mapping(preferences).as_mapping()
    assert route.owner.events == []


def test_status_and_release_are_runtime_tasks(route, monkeypatch):
    from astrid.packs.runpod import target_selection
    preferences = {'provider_account_ref': 'account-a', 'gpu_types': ['gpu-a'],
                   'image': 'worker@sha256:' + 'a' * 64}
    monkeypatch.setattr(target_selection, 'read_runpod_preferences', lambda: target_selection.RunPodPreferences.from_mapping(preferences))
    calls = []

    def invoke(capability, **kwargs):
        calls.append((capability, kwargs))
        return InvocationResult(capability, 'executor', 'command', True,
            kernel_task_id='task-' + capability, raw_result={'result': {'payload': {'target_operation': {'state': 'absent'}}}})

    route.client.invoke_result = invoke
    assert GenerationFacade().describe(target='runpod', client=route.client) == {'state': 'absent'}
    release = GenerationFacade().release(target='runpod', engine='comfyui', client=route.client, wait=False)
    assert release.kernel_task_id == 'task-runpod.release'
    assert [capability for capability, _ in calls] == ['runpod.status', 'runpod.release']
    assert calls[0][1]['inputs']['include_quote'] is True and calls[1][1]['wait'] is False
    assert route.owner.events == []


@pytest.mark.parametrize('raw', [False, True])
def test_waiting_route_activates_once_and_reconstructs_publication_fake(host_route, raw):
    """Publication now follows the retained host, with real Runtime delivery."""
    from astrid.core.generation.backends.base import GenerationResult
    from astrid.core.generation.delivery import deliver_generation
    r = host_route
    r.start()
    result = r.generate(r.kind, raw=raw, wait=True)
    assert isinstance(result, GenerationResult) and result.task_id
    delivered = deliver_generation(result, client=r.client, project=r.project, out=r.tmp / 'delivered')
    assert delivered['image_paths']
    expected = (r.output / ('proof.png' if r.kind == 'image' else 'proof.mp4')).read_bytes()
    assert Path(delivered['image_paths'][0]).read_bytes() == expected
    binding = r.c.read()['lease']['endpoint_binding']
    assert binding['source_sha256'] == 'sha256:' + hashlib.sha256(
        r.paths[r.kind].with_name('source.json').read_bytes()).hexdigest()
    task = r.show(result.task_id)
    payload = task['result'].get('payload', task['result'])
    assert payload['target_binding'] == binding
    assert r.c.creates == len(r.state.prompts) == 1
    assert sum(e[0] == 'enable' for e in r.state.events) == 1
    assert r.bindings.provider.lease_owner is r.host._target_bindings.provider.lease_owner
    assert not getattr(r.client, '_generation_task_owners', {})
    assert r.release().ok and r.c.deletes == ['pod-1']
    assert r.c.read()['cleanup']['absence']['exact_get_status'] == 404



@pytest.mark.parametrize('raw', [False, True])
def test_prepared_image_video_and_raw_admit_once_and_follow_same_binding(host_route, raw):
    r = host_route
    receipt = r.tmp / 'receipt.json'
    result = r.generate(r.kind, raw=raw, recovery_path=receipt)
    task_id = result.kernel_task_id
    before = r.show(task_id)
    assert before['state'] == 'queued' and r.c.creates == 0
    assert not before.get('execution_request')
    assert not getattr(r.client, '_generation_prepared_owners', {})
    r.start()
    assert r.follow(task_id).ok
    binding = deepcopy(r.c.read()['lease']['endpoint_binding'])
    resumed = (r.generate(r.kind, raw=raw, recovery_path=receipt, resume=True)
               if raw else r.follow(task_id))
    assert resumed.kernel_task_id == task_id
    assert r.c.read()['lease']['endpoint_binding'] == binding
    assert r.c.creates == len(r.state.prompts) == 1
    assert r.release().ok



def test_dry_run_writes_nothing_and_does_not_claim(route, monkeypatch):
    monkeypatch.setattr(Path, 'home', lambda: route.tmp)
    before = {p: p.read_bytes() for p in route.tmp.rglob('*') if p.is_file()}
    result = generate_route(route, dry_run=True, **PAID)
    assert result['status'] == 'ready' and result['target']['placement'] == 'runpod'
    assert route.owner.events == [] and not route.calls
    assert {p: p.read_bytes() for p in route.tmp.rglob('*') if p.is_file()} == before



@pytest.mark.parametrize('changes', [dict(consent=()), dict(max_usd=None), dict(deadline=None),
    dict(deadline='2020-01-01T00:00:00Z'), dict(max_usd=float('nan')), dict(engine='wan2gp')])
def test_missing_fresh_paid_authority_denies_before_claim(route, changes):
    with pytest.raises((ValueError, RuntimeError)) as raised:
        generate_route(route, **{**PAID, **changes})
    assert raised.value.to_envelope()['status'] == 'blocked'
    assert route.owner.events == [] and not route.calls


def test_invalid_entry_values_and_raw_source_block_before_claim(route):
    GenerationFacade().targets(target='runpod', save=True, inputs={
        'provider_account_ref': 'account-a', 'gpu_types': ['gpu-a'],
        'image': 'worker@sha256:' + 'a' * 64})
    disclosed = {**PAID, 'offer_digest': 'sha256:' + 'b' * 64,
                 'preparation_seconds': 60, 'execution_seconds': 60, 'idle_seconds': 1800}
    with pytest.raises((ValueError, RuntimeError), match='unknown'):
        generate_route(route, inputs={'unknown': 3}, **disclosed)
    route.source.write_text('not a canonical workflow')
    with pytest.raises((ValueError, RuntimeError)):
        generate_route(route, raw=True, **disclosed)
    assert not any(isinstance(e, tuple) and e[0] == 'prepare' for e in route.owner.events)
    assert route.calls == []


def test_cli_paid_flags_match_sdk_and_retain_admission(host_route, monkeypatch, capsys):
    r = host_route
    monkeypatch.setattr(cli, '_open_client', lambda: nullcontext(r.client))
    assert cli.main(_host_cli_args(r) + ['--json']) == 0
    result = json.loads(capsys.readouterr().out)
    task = r.show(result['kernel_task_id'])
    policy = _host_policy(task)
    assert policy == {**r.selection, **r.paid, 'consent': ['paid']}
    assert task['state'] == 'queued' and r.c.creates == 0
    assert not task.get('execution_binding') and not getattr(r.client, '_generation_task_owners', {})



def test_cli_no_wait_text_returns_task_without_claiming_completion(host_route, monkeypatch, capsys):
    r = host_route
    monkeypatch.setattr(cli, '_open_client', lambda: nullcontext(r.client))
    assert cli.main(_host_cli_args(r)) == 0
    text = capsys.readouterr().out
    assert text.startswith('task: ') and text.endswith('\n') and len(text.splitlines()) == 1
    assert r.show(text.removeprefix('task: ').strip())['state'] == 'queued'
    assert r.c.creates == 0



def test_target_describe_and_release_use_typed_owner_without_claim(host_route, monkeypatch, capsys):
    r = host_route
    r.start()
    assert r.generate(r.kind, wait=True).task_id
    owner = r.bindings.provider.lease_owner
    monkeypatch.setattr(cli, '_open_client', lambda: nullcontext(r.client))
    assert cli.main(['describe', '--target', 'runpod', '--json']) == 0
    status = json.loads(capsys.readouterr().out)
    assert status['pod_id'] == 'pod-1' and status['lease']['state'] == 'idle'
    assert cli.main(['release', '--engine', 'comfyui', '--target', 'runpod', '--no-wait', '--json']) == 0
    result = json.loads(capsys.readouterr().out)
    task_id = result['kernel_task_id']
    assert r.follow(task_id).ok
    assert r.show(task_id)['capability_id'] == 'runpod.release'
    assert r.bindings.provider.lease_owner is owner
    assert r.c.read()['state'] == 'cleanup_complete' and r.c.deletes == ['pod-1']



def test_release_denial_keeps_the_runtime_task_and_blocked_envelope(host_route, monkeypatch, capsys):
    r = host_route
    r.start()
    assert r.generate(r.kind, wait=True).task_id
    r.c.absent_get = False  # Provider DELETE succeeds, independent GET absence is unproven.
    monkeypatch.setattr(cli, '_open_client', lambda: nullcontext(r.client))
    assert cli.main(['release', '--engine', 'comfyui', '--target', 'runpod', '--json']) == 2
    result = json.loads(capsys.readouterr().out)
    assert result['status'] == 'blocked' and result['task_id']
    task = r.show(result['task_id'])
    assert task['state'] == 'failed' and task['capability_id'] == 'runpod.release'
    assert result['issues'] and r.c.read()['state'] == 'cleanup_pending'
    assert r.c.creates == 1
    r.c.absent_get = True
    assert r.release().ok and r.c.read()['state'] == 'cleanup_complete'



def test_absent_host_interface_is_a_blocked_sdk_and_cli_envelope(host_route, monkeypatch, capsys):
    r = host_route
    r.bindings.close()
    r.host._target_bindings = r.host._target_bindings_kind = None
    r.host.pack_roots = tuple(p for p in r.host.pack_roots if p.name != 'runpod')
    r.start()
    admitted = r.generate(r.kind)
    result = r.follow(admitted.kernel_task_id)
    assert not result.ok and result.kernel_task_id
    monkeypatch.setattr(cli, '_open_client', lambda: nullcontext(r.client))
    args = _host_cli_args(r)
    args.remove('--no-wait')
    assert cli.main(args + ['--json']) == 2
    blocked = json.loads(capsys.readouterr().out)
    assert blocked['status'] == 'blocked' and blocked['task_id']
    assert blocked['issues'][0]['code'] == preflight.generation_error(result.error).to_envelope()['issues'][0]['code']
    assert r.c.creates == 0 and not r.c.owner.path.exists() and not r.state.prompts



def test_foreign_binding_on_follow_never_reactivates(host_route):
    r = host_route
    r.start()
    result = r.generate(r.kind, wait=True)
    saved = r.c.read()
    foreign = deepcopy(saved)
    foreign['lease']['endpoint_binding']['owner_epoch'] = 'foreign'
    r.c.owner.path.write_text(json.dumps(foreign))
    assert r.follow(result.task_id).ok  # Following a settled task never owns a provider transition.
    denied = r.release()
    assert not denied.ok and r.c.deletes == []
    assert r.c.creates == len(r.state.prompts) == 1
    assert r.c.read()['operation_id'] == saved['operation_id']
    r.c.owner.path.write_text(json.dumps(saved))
    assert r.release().ok



def test_no_wait_retains_exact_custody_when_activation_is_uncertain(host_route):
    r = host_route
    r.c.lost_start = r.c.cancel_unknown = True
    admitted = r.generate(r.kind)
    assert admitted.ok and r.c.creates == 0
    r.start()
    assert not r.follow(admitted.kernel_task_id).ok
    saved = r.c.read()
    job = saved['lease']['preparation']['job']
    assert saved['state'] == 'cleanup_pending' and job['state'] == 'starting'
    assert r.c.pod.cancelled and set(r.c.pod.cancelled) == {job['id']}
    assert not r.release().ok and not r.c.deletes
    retry = r.generate(r.kind)
    assert not r.follow(retry.kernel_task_id).ok
    assert r.c.creates == 1 and len(r.c.pod.job_log) == 1 and not r.state.prompts
    assert r.c.read()['operation_id'] == saved['operation_id']
    r.c.cancel_unknown = False
    assert r.release().ok and r.c.deletes == ['pod-1']



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


def test_h3_no_wait_uses_prepared_child_and_preserves_parent_default(retained_host):
    r = retained_host
    from tests.sdk.test_h3_endpoint_r4 import h3_inputs as input_fixture
    h3_inputs = input_fixture.__wrapped__(r.tmp)
    result = GenerationFacade().video(entry='h3', target='runpod', client=r.client,
        project=r.project, wait=False, **h3_inputs, **r.paid)
    assert result.ok and result.kernel_task_id
    task = r.show(result.kernel_task_id)
    spec = task['spec'].get('spec', task['spec'])
    plan = spec['inputs']['plan']
    assert plan['controls']['target_policy'] == {**r.selection, **r.paid, 'engine': 'vibecomfy-h3', 'consent': ['paid']}
    assert all(stage.get('target', {'kind': 'default'}) == {'kind': 'default'} for stage in plan['stages'])
    assert 'prepared_worker' not in spec['inputs'] and not getattr(r.client, '_generation_prepared_owners', {})
    assert r.c.creates == 0



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

def test_cli_paid_flags_admit_local_host_policy(route, monkeypatch, capsys):
    from astrid.packs.runpod import target_selection
    preferences = {'provider_account_ref': 'account-a', 'gpu_types': ['gpu-a'],
                   'image': 'worker@sha256:' + 'a' * 64}
    monkeypatch.setattr(target_selection, 'read_runpod_preferences',
                        lambda: target_selection.RunPodPreferences.from_mapping(preferences))
    monkeypatch.setattr(cli, '_open_client', lambda: nullcontext(route.client))
    calls = []
    def invoke(capability, **kwargs):
        calls.append((capability, kwargs))
        return InvocationResult(capability, 'executor', 'command', True,
                                kernel_task_id='local-task', raw_result={'state': 'queued'})
    route.client.invoke_result = invoke
    assert cli.main([route.kind, '--entry', route.entry['id'], '--target', 'runpod', '--project', 'demo',
        '--consent', 'paid', '--max-usd', '2', '--deadline', PAID['deadline'],
        '--offer-digest', 'sha256:' + 'b' * 64, '--preparation-seconds', '60',
        '--execution-seconds', '60', '--idle-seconds', '1800', '--no-wait', '--json']) == 0
    assert json.loads(capsys.readouterr().out)['kernel_task_id'] == 'local-task'
    capability, kwargs = calls[0]
    assert capability == 'vibecomfy.run' and kwargs.get('execution_request') is None
    policy = json.loads(kwargs['inputs']['target_policy'])
    assert policy['offer_digest'] == 'sha256:' + 'b' * 64 and policy['consent'] == ['paid']
    assert policy['preparation_seconds'] == 60 and policy['execution_seconds'] == 60
    assert policy['idle_seconds'] == 1800 and route.owner.events == []
