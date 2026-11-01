"""F04 input, envelope and no-write boundaries against the pinned Vibe API."""
from __future__ import annotations

import io
import json
import subprocess
from contextlib import nullcontext, redirect_stdout
from pathlib import Path
from types import SimpleNamespace

import pytest

from astrid.core.generation import cli, entries, preflight, targets
from astrid.core.model_catalog.registry import ModelRegistry
from astrid.sdk.generation import GenerationFacade, GenerationInputError
from astrid.sdk.results import InvocationResult
from vibecomfy.schema.types import InputSpec, NodeSchema
from vibecomfy.workflow import VibeEdge, VibeNode, VibeWorkflow, WorkflowSource


@pytest.fixture
def authored(monkeypatch, tmp_path):
    workflow = VibeWorkflow('f04', WorkflowSource('f04'))
    workflow.nodes['12'] = VibeNode('12', 'Texture', inputs={
        'prompt': 'default prompt', 'texture': 0.2, 'strength': 0.4,
        'connected': None, '_internal': 4})
    workflow.nodes['1'] = VibeNode('1', 'SaveImage')
    workflow.edges.append(VibeEdge('1', 'IMAGE', '12', 'connected'))
    workflow.register_input('prompt', '12', 'prompt', 'default prompt', type='STRING')
    workflow.register_input('texture', '12', 'texture', 0.2, type='FLOAT', range={'min': 0, 'max': 1})
    schema = NodeSchema('Texture', None, {
        'prompt': InputSpec(type='STRING'), 'texture': InputSpec(type='FLOAT'),
        'strength': InputSpec(type='FLOAT', default=0.4, min=0, max=1),
        'connected': InputSpec(type='FLOAT'), '_internal': InputSpec(type='INT')}, [])
    provider = SimpleNamespace(get_schema=lambda name: schema if name == 'Texture' else None)
    monkeypatch.setattr(entries, '_schema_provider', lambda: provider)
    entry = {'id': 'f04', 'kind': 'image', 'modes': ['t2i'], 'bundle': str(tmp_path / 'workflow.py'),
             'binds': {'prompt': 'prompt', 'texture': 'texture'}, 'defaults': {'texture': 0.2}, 'needs': []}
    monkeypatch.setattr(entries, 'user_entries', lambda: [entry])
    monkeypatch.setattr(entries, 'action_entries', lambda: [])
    monkeypatch.setattr(entries, '_load_entry_workflow', lambda row: workflow.copy())
    monkeypatch.setattr(ModelRegistry, 'load_default', lambda **kwargs: ModelRegistry([]))
    import astrid.sdk as sdk
    monkeypatch.setattr(sdk, '_load_model_registry', lambda **kwargs: ModelRegistry([]))
    monkeypatch.setattr('astrid.packs.vibecomfy.production_engine.load_workflow_path',
        lambda *args, **kwargs: SimpleNamespace(resolved=SimpleNamespace(workflow=workflow.copy())))
    return workflow, entry


def test_public_inventory_is_writable_native_rows(authored):
    result = entries.describe_entry('f04')
    rows = {row['name']: row for row in result['inputs']}
    assert result['engine'] == 'comfyui'
    assert set(rows) == {'prompt', 'texture', 'node:12.strength'}
    assert rows['node:12.strength'] == {'name': 'node:12.strength', 'type': 'number', 'default': 0.4}
    assert all(set(row) == {'name', 'type', 'default'} for row in rows.values())
    assert result['inputs'] == result['mode_descriptions']['t2i']['inputs']


def test_generation_help_and_mcp_publish_the_phase_one_commands(monkeypatch, capsys):
    from astrid import console, mcp_server
    monkeypatch.setattr(cli, '_open_client', lambda: pytest.fail('help opened Runtime'))
    assert console.main(['generate', '--help']) == 0
    help_text = capsys.readouterr().out
    assert all(name in help_text for name in ('list', 'targets', 'describe', 'workflow', 'release'))
    tools = mcp_server._generation_tools()
    assert {'generate_list', 'generate_targets', 'generate_describe', 'generate_workflow', 'generate_release'} <= tools.keys()


def test_arbitrary_bindings_use_native_set_input_and_override_defaults(authored, monkeypatch):
    _, entry = authored
    calls = []
    original = VibeWorkflow.set_input
    def set_input(self, name, value):
        calls.append((name, value))
        return original(self, name, value)
    monkeypatch.setattr(VibeWorkflow, 'set_input', set_input)
    _, values = entries.resolve_entry_inputs('f04', {'texture': 0.7, 'node:12.strength': 0.8})
    workflow, plan, assets = entries.prepare(Path(entry['bundle']), target_client=None, mode='t2i',
        binds=entry['binds'], defaults=entry['defaults'], values=values)
    assert workflow.nodes['12'].inputs['strength'] == 0.8
    assert workflow.nodes['12'].inputs['texture'] == 0.7
    assert ('node:12.strength', 0.8) in calls
    assert not plan['unbound'] and not assets


@pytest.mark.parametrize('values', [
    {'node:12.connected': 0.2}, {'node:12._internal': 2}, {'node:99.strength': 0.2},
    {'node:12.strength': '0.2'}, {'node:12.strength': True}, {'node:12.strength': None},
    {'node:12.strength': 1.1}, {'texture': 0.2, 'node:12.texture': 0.2},
])
def test_unwritable_invalid_and_duplicate_bindings_block_before_target(authored, monkeypatch, values):
    monkeypatch.setattr(GenerationFacade, '_generate_on_target', lambda *a, **k: pytest.fail('admission'))
    with pytest.raises(GenerationInputError) as raised:
        GenerationFacade().image(entry='f04', target='auto', **values)
    payload = raised.value.to_envelope()
    assert payload['status'] == 'blocked' and payload['task_id'] is None
    assert payload['issues'][0]['code'] == 'input_rejected'
    assert payload['issues'][0]['details']['constraint']
    assert payload['issues'][0]['details']['field']


def test_assignment_values_and_first_equals():
    assert entries.merge_input_values(assignments=[
        'literal=a=b', 'number=0.25', 'bool=true', 'nil=null', 'array=[1,2]',
        'object={"a":1}', 'text="123"']) == {
            'literal': 'a=b', 'number': 0.25, 'bool': True, 'nil': None,
            'array': [1, 2], 'object': {'a': 1}, 'text': '123'}
    with pytest.raises(ValueError, match='duplicate'):
        entries.merge_input_values({'texture': 0.4}, assignments=['texture=0.5'])


def test_cli_sdk_and_mcp_accept_the_same_non_common_bindings(authored, monkeypatch, capsys):
    from astrid import mcp_server
    captured = []
    monkeypatch.setattr(GenerationFacade, '_generate_on_target',
        lambda self, **kwargs: captured.append(kwargs) or {'ok': True})
    monkeypatch.setattr(cli, '_open_client', lambda: nullcontext('runtime'))
    assert cli.main(['image', '--entry', 'f04', '--prompt', 'paint',
        '--input', 'texture=0.7', '--input', 'node:12.strength=0.8', '--json', '--no-wait']) == 0
    assert captured[-1]['inputs'] == {'prompt': 'paint', 'texture': 0.7, 'node:12.strength': 0.8}
    assert captured[-1]['wait'] is False
    capsys.readouterr()
    GenerationFacade().image(entry='f04', target='auto', inputs={'texture': 0.7, 'node:12.strength': 0.8})
    assert captured[-1]['inputs'] == {'texture': 0.7, 'node:12.strength': 0.8}
    def run(argv, cwd):
        start = argv.index('generate')
        output = io.StringIO()
        with redirect_stdout(output):
            code = cli.main(argv[start + 1:])
        return subprocess.CompletedProcess(argv, code, output.getvalue(), '')
    monkeypatch.setattr(mcp_server, '_run', run)
    tool = mcp_server._generation_tools()['generate_image']
    result = mcp_server.call_tool(tool, {'entry': 'f04', 'inputs': {'texture': 0.7, 'node:12.strength': 0.8}})
    assert not result['isError']
    assert captured[-1]['inputs'] == {'texture': 0.7, 'node:12.strength': 0.8}
    assert captured[-1]['wait'] is False


def test_cli_sdk_mcp_share_rejection_envelope(authored, monkeypatch, capsys):
    from astrid import mcp_server
    monkeypatch.setattr(cli, '_open_client', lambda: pytest.fail('invalid input opened Runtime'))
    assert cli.main(['image', '--entry', 'f04', '--input', 'node:12.strength="bad"', '--json']) == 2
    cli_error = json.loads(capsys.readouterr().out)
    with pytest.raises(GenerationInputError) as raised:
        GenerationFacade().image(entry='f04', target='auto', inputs={'node:12.strength': 'bad'})
    sdk_error = raised.value.to_envelope()
    assert cli_error == sdk_error
    assert cli_error['issues'][0]['code'] == 'input_rejected'
    done = subprocess.CompletedProcess([], 2, json.dumps(cli_error), '')
    result = mcp_server._cli_result(done)
    assert result['isError'] and result['structuredContent'] == cli_error


def test_readiness_missing_drift_unknown_and_details():
    assert preflight.readiness_envelope()['status'] == 'ready'
    assert preflight.compare_versions({'comfyui': None}, {'comfyui': '1'}) == []
    assert preflight.compare_versions({'comfyui': '1'}, {}) == []
    warnings = preflight.compare_versions({'comfyui': '1'}, {'comfyui': '2'})
    assert preflight.readiness_envelope(issues=warnings)['status'] == 'ready_with_warnings'
    error = preflight.generation_error({'code': 'COMFY_TARGET_UNAVAILABLE', 'message': 'down'})
    assert error.to_envelope()['issues'][0]['code'] == 'target_unreachable'
    assert error.to_envelope()['issues'][0]['details']['engine_code'] == 'COMFY_TARGET_UNAVAILABLE'
    blocked = preflight.readiness_envelope(issues=[*warnings, *error.readiness['issues']])
    assert blocked['status'] == 'blocked' and blocked['fixes'] == []


def test_read_only_commands_do_not_write_config_sources_or_locks(authored, monkeypatch, tmp_path, capsys):
    monkeypatch.setattr(Path, 'home', lambda: tmp_path)
    config = tmp_path / '.vibecomfy' / 'config.toml'
    config.parent.mkdir()
    config.write_text('[comfyui]\nurl="http://127.0.0.1:18888"\n[library]\nroot="unchanged"\n')
    before = {p.relative_to(tmp_path): p.read_bytes() for p in tmp_path.rglob('*') if p.is_file()}
    monkeypatch.setattr(targets, 'save_target', lambda *a, **k: pytest.fail('save'))
    monkeypatch.setattr(targets, 'update_config', lambda *a, **k: pytest.fail('write'))
    monkeypatch.setattr(cli, '_open_client', lambda: pytest.fail('Runtime'))
    assert cli.main(['describe', '--entry', 'f04', '--json']) == 0
    assert cli.main(['targets', '--json']) == 0
    capsys.readouterr()
    assert {p.relative_to(tmp_path): p.read_bytes() for p in tmp_path.rglob('*') if p.is_file()} == before
    assert not config.with_name('config.toml.lock').exists()


def test_bound_down_human_target_never_switches_or_writes(monkeypatch, tmp_path):
    from astrid.core.generation.backends.comfy_target import ComfyTargetClient
    monkeypatch.setattr(Path, 'home', lambda: tmp_path)
    config = tmp_path / '.vibecomfy' / 'config.toml'
    config.parent.mkdir()
    config.write_text('[comfyui]\nurl="http://127.0.0.1:18888"\n')
    before = config.read_bytes()
    calls = []
    def request(self, path, **kwargs):
        calls.append((self.target.url, path))
        raise OSError('offline')
    monkeypatch.setattr(ComfyTargetClient, 'request_json', request)
    monkeypatch.setattr(preflight, '_comfyui_runtime_available', lambda: pytest.fail('managed fallback'))
    with pytest.raises(preflight.GenerationReadinessError) as raised:
        preflight.select_generation_target('auto')
    payload = raised.value.to_envelope()
    assert payload['issues'][0]['code'] == 'target_unreachable'
    assert payload['target']['owner'] == 'human'
    assert payload['target']['endpoint'] == 'http://127.0.0.1:18888'
    assert calls == [('http://127.0.0.1:18888', '/system_stats')]
    assert config.read_bytes() == before and not config.with_name('config.toml.lock').exists()


def test_no_wait_returns_the_exact_admission_receipt(monkeypatch):
    receipt = InvocationResult('generation.generate_image', 'executor', 'command', True,
                               kernel_task_id='task-1', kernel_run_id='run-1')
    calls = []
    def invoke(self, *args, **kwargs):
        calls.append(kwargs)
        return receipt
    monkeypatch.setattr(GenerationFacade, '_invoke', invoke)
    result = GenerationFacade().image(model='z-image', execution='cloud', wait=False, prompt='paint', project='demo')
    assert result is receipt and len(calls) == 1
    assert calls[0]['wait'] is False


def test_terminal_exec_error_preserves_task_and_shared_envelope():
    from astrid.sdk.results import _reconstruct_generation_result
    from astrid.sdk.exceptions import CapabilityRuntimeError
    result = InvocationResult('vibecomfy.run', 'executor', 'command', False,
        error={'code': 'COMFY_TARGET_UNAVAILABLE', 'message': 'target went away', 'sdk_category': 'runtime'},
        kernel_task_id='task-1', kernel_attempt_id='attempt-1')
    with pytest.raises(CapabilityRuntimeError) as raised:
        _reconstruct_generation_result(result)
    payload = raised.value.to_envelope()
    assert payload['task_id'] == 'task-1'
    assert payload['issues'][0]['code'] == 'target_unreachable'
    assert preflight.generation_error(raised.value).to_envelope() == payload
    assert preflight.generation_error(payload).to_envelope() == payload


def test_replacement_without_the_atomic_vibe_api_blocks_before_write(monkeypatch, capsys):
    import vibecomfy.entries as vibe_entries
    def import_entry(source, entry_id, directory=None):
        pytest.fail('unsupported replacement wrote an entry')
    monkeypatch.setattr(vibe_entries, 'import_as_entry', import_entry)
    monkeypatch.setattr(cli, '_open_client', lambda: pytest.fail('unsupported replacement opened Runtime'))
    assert cli.main(['workflow', 'unused.py', '--as-entry', 'mine', '--replace', '--json']) == 2
    payload = json.loads(capsys.readouterr().out)
    assert payload['issues'][0]['code'] == 'missing_dependency'
    assert payload['issues'][0]['details']['field'] == 'replace'


def test_raw_target_workflow_never_discards_a_prepared_execution_binding(monkeypatch):
    monkeypatch.setattr(entries, 'resolve_workflow_inputs', lambda *a, **k: pytest.fail('source loading'))
    with pytest.raises(GenerationInputError, match='prepared binding'):
        GenerationFacade().workflow(workflow='unused.py', target='auto',
            execution_request={'target': {'kind': 'runpod', 'pod_id': 'exact-pod'}})


def test_legacy_sdk_validation_also_exposes_the_shared_envelope(authored):
    from astrid.sdk.exceptions import CapabilityValidationError
    with pytest.raises(CapabilityValidationError) as raised:
        GenerationFacade().image(model='absent', execution='local')
    payload = raised.value.to_envelope()
    assert payload['status'] == 'blocked' and payload['task_id'] is None
    assert payload['issues'][0]['code'] == 'input_rejected'


def test_workflow_admission_failure_keeps_the_shared_error_and_task_id():
    receipt = InvocationResult('vibecomfy.run', 'executor', 'command', False,
        error={'code': 'COMFY_TARGET_UNAVAILABLE', 'message': 'offline', 'sdk_category': 'runtime'},
        kernel_task_id='task-1')
    client = SimpleNamespace(invoke_result=lambda *a, **k: receipt)
    result = GenerationFacade().workflow(workflow={'digest': 'sha256:' + 'a' * 64},
                                        project='demo', client=client, wait=False)
    assert not result.ok
    assert result.error['task_id'] == 'task-1'
    assert result.error['issues'][0]['code'] == 'target_unreachable'
    from astrid.sdk.results import _reconstruct_generation_result
    from astrid.sdk.exceptions import CapabilityRuntimeError
    with pytest.raises(CapabilityRuntimeError, match='offline'):
        _reconstruct_generation_result(result)


def test_release_routes_to_actual_owner_and_fails_closed_without_one():
    calls = []
    owner = SimpleNamespace(release_generation=lambda **kwargs: calls.append(kwargs) or 'admission')
    assert GenerationFacade().release(engine='local', target='auto', client=owner) == 'admission'
    assert calls == [{'engine': 'comfyui', 'target': 'auto', 'wait': False}]
    with pytest.raises(preflight.GenerationReadinessError) as raised:
        GenerationFacade().release(engine='comfyui', target='auto')
    assert raised.value.to_envelope()['issues'][0]['code'] == 'missing_dependency'


def test_catalog_uses_the_same_arbitrary_binding_path(authored, monkeypatch):
    from astrid.core.model_catalog.schema import BackendSpec, ModelEntry, ModeSpec
    from astrid.sdk.generation import _prepare_catalog_target_workflow
    workflow, _entry = authored
    candidate = ModelEntry(id='catalog-f04', modality='image', modes={
        't2i': ModeSpec(backends={'local': BackendSpec(template='image/f04')})})
    monkeypatch.setattr(preflight, '_module_available', lambda name: True)
    monkeypatch.setattr(preflight, '_comfyui_runtime_available', lambda: True)
    backend = SimpleNamespace(prepare_catalog_workflow=lambda *a, **k: (workflow.copy(), None, None))
    bound, plan, schema, assets = _prepare_catalog_target_workflow(
        registry=ModelRegistry([candidate]), candidate=candidate, mode='t2i', modality='image',
        inputs={'node:12.strength': 0.8}, target_client=None, backend=backend)
    assert bound.nodes['12'].inputs['strength'] == 0.8
    assert plan['bindings']['node:12.strength'] == 0.8 and not assets
    with pytest.raises(ValueError, match='no binding'):
        _prepare_catalog_target_workflow(registry=ModelRegistry([candidate]), candidate=candidate,
            mode='t2i', modality='image', inputs={'unsupported': 1}, target_client=None, backend=backend)


def test_missing_target_nodes_and_models_are_all_reported(authored):
    workflow, entry = authored
    client = SimpleNamespace(object_info=lambda: {}, generation_target=targets.TargetRecord(
        endpoint='http://127.0.0.1:18888'))
    with pytest.raises(preflight.GenerationReadinessError) as raised:
        entries.target_schema(entry, workflow, client)
    payload = raised.value.to_envelope()
    assert payload['status'] == 'blocked'
    assert {issue['details']['node'] for issue in payload['issues']} == {'Texture', 'SaveImage'}
    assert all(issue['code'] == 'missing_dependency' for issue in payload['issues'])
    assert all(issue['details']['bytes'] is None for issue in payload['issues'])
    assert payload['fixes'][0]['scope'] == 'read_only'


def test_dry_run_is_the_readiness_envelope_without_source_or_config_writes(monkeypatch, tmp_path):
    from vibecomfy.schema.provider import TargetSchemaProvider
    from vibecomfy.workflow_bundle import emit_bundle
    workflow = VibeWorkflow('f04-dry', WorkflowSource('f04-dry'))
    workflow.add_node('EmptyImage', _id='12', uid='empty', width=64, height=64, batch_size=1, color=0)
    workflow.add_node('SaveImage', _id='1', uid='save', filename_prefix='f04')
    workflow.nodes['12'].native_output_names = ['IMAGE']
    workflow.nodes['12'].native_output_types = ['IMAGE']
    workflow.nodes['12'].native_output_slots = [0]
    workflow.nodes['1'].native_input_names = ['images']
    workflow.nodes['1'].native_input_types = ['IMAGE']
    workflow.edges.append(VibeEdge('12', '0', '1', 'images'))
    workflow.register_input('color', '12', 'color', 0, type='INT')
    info = {
        'EmptyImage': {'input': {'required': {
            'width': ['INT', {'default': 64, 'min': 1}], 'height': ['INT', {'default': 64, 'min': 1}],
            'batch_size': ['INT', {'default': 1, 'min': 1}], 'color': ['INT', {'default': 0}]}},
            'output': ['IMAGE'], 'output_name': ['IMAGE'], 'output_is_list': [False]},
        'SaveImage': {'input': {'required': {'images': ['IMAGE'], 'filename_prefix': ['STRING', {'default': 'f04'}]}},
            'output': [], 'output_name': [], 'output_is_list': []},
    }
    url = 'http://127.0.0.1:18888'
    schema = TargetSchemaProvider.from_object_info(info, server_url=url, model_availability='target')
    monkeypatch.setattr(entries, '_schema_provider', lambda: schema)
    source = tmp_path / 'workflow.py'
    (tmp_path / 'source.json').write_text(json.dumps({'nodes': [
        {'id': 12, 'type': 'EmptyImage', 'inputs': [], 'widgets_values': [64, 64, 1, 0]},
        {'id': 1, 'type': 'SaveImage', 'inputs': [{'name': 'images', 'type': 'IMAGE', 'link': 1}],
         'widgets_values': ['f04']}], 'links': [[1, 12, 0, 1, 0, 'IMAGE']]}))
    emit_bundle(workflow, source, {'operation': 'authored'})
    entry = {'id': 'f04-dry', 'kind': 'image', 'modes': ['t2i'], 'bundle': str(source),
             'binds': {'color': 'color'}, 'defaults': {'color': 7}, 'needs': []}
    monkeypatch.setattr(entries, 'user_entries', lambda: [entry])
    monkeypatch.setattr(entries, 'action_entries', lambda: [])
    description = entries.describe_entry('f04-dry', registry=ModelRegistry([]))
    client = SimpleNamespace(target=SimpleNamespace(url=url), http_origin=url,
        generation_target=targets.TargetRecord(endpoint=url), object_info=lambda: info)
    monkeypatch.setattr(preflight, 'select_generation_target', lambda *a, **k: client)
    monkeypatch.setattr(preflight, '_module_available', lambda name: True)
    monkeypatch.setattr(preflight, '_comfyui_runtime_available', lambda: pytest.fail('managed fallback'))
    monkeypatch.setattr(GenerationFacade, 'workflow', lambda *a, **k: pytest.fail('admission'))
    monkeypatch.setattr(targets, 'update_config', lambda *a, **k: pytest.fail('config write'))
    before = {p.relative_to(tmp_path): p.read_bytes() for p in tmp_path.rglob('*') if p.is_file()}
    result = GenerationFacade()._generate_on_target(registry=ModelRegistry([]), model=None,
        mode='t2i', modality='image', target=url, execution=None, inputs={'color': 12},
        dry_run=True, workflow_entry=entry)
    assert set(result) == {'entry', 'target', 'status', 'issues', 'fixes'}
    assert result['status'] == 'ready' and result['target']['owner'] == 'human'
    assert result['entry']['effective_values']['color'] == 12
    assert result['entry']['accepted_inputs'] == ['color']
    assert result['entry']['rejected_inputs'] == []
    assert all(set(row) == {'name', 'type', 'default'} for row in result['entry']['inputs'])
    assert result['entry']['inputs'] == description['inputs']
    assert {p.relative_to(tmp_path): p.read_bytes() for p in tmp_path.rglob('*') if p.is_file()} == before
