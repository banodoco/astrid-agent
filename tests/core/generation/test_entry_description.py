from __future__ import annotations

import json

import pytest

from astrid.core.generation import entries
from astrid.core.model_catalog.registry import ModelRegistry
from astrid.core.model_catalog.schema import ModelEntry, ModeSpec
from vibecomfy.workflow import VibeNode, VibeWorkflow, WorkflowSource


@pytest.fixture
def catalog(monkeypatch):
    monkeypatch.setattr(entries, 'user_entries', lambda: [])
    monkeypatch.setattr(entries, 'action_entries', lambda: [])
    monkeypatch.setattr(entries, '_ready_entries_for', lambda *args: pytest.fail('unrelated workflow build'))
    return ModelRegistry([ModelEntry(id='example', modality='image', modes={
        't2i': ModeSpec(supports=('prompt', 'seed', 'count', 'size'), requires=('prompt',)),
        'edit': ModeSpec(supports=('prompt', 'image_ref', 'steps'), requires=('prompt', 'image_ref')),
    })])


def test_catalog_selected_mode_fields_and_typed_defaults(catalog):
    description = entries.describe_entry('example', registry=catalog)
    assert description['mode'] is None and description['inputs'] == []
    assert set(description['mode_descriptions']) == {'t2i', 'edit'}
    fields = {row['name']: row for row in entries.describe_entry('example', 't2i', registry=catalog)['inputs']}
    assert fields['seed'] == {'name': 'seed', 'type': 'integer', 'default': None}
    assert fields['count']['default'] == 1
    assert fields['prompt'] == {'name': 'prompt', 'type': 'string', 'default': None}
    edit = entries.describe_entry('example', 'edit', registry=catalog)
    assert {row['name'] for row in edit['inputs']} == {'prompt', 'image_ref', 'steps'}
    assert json.loads(json.dumps(edit)) == edit


@pytest.mark.parametrize('inputs,match', [
    ({'prompt': 'p', 'seed': True}, 'integer'),
    ({'prompt': 'p', 'seed': '1'}, 'integer'),
    ({'prompt': 'p', 'seed': None}, 'null'),
    ({'prompt': 'p', 'unknown': 1}, 'unsupported entry inputs'),
    ({'prompt': ''}, 'missing required'),
    ({'prompt': '   '}, 'missing required'),
    ({}, 'missing required'),
    ({'prompt': 'p', 'width': 512}, 'together'),
    ({'prompt': 'p', 'width': 512, 'height': 512, 'size': '1x1'}, 'conflicting'),
])
def test_catalog_rejects_invalid_values(catalog, inputs, match):
    with pytest.raises(ValueError, match=match):
        entries.resolve_entry_inputs('example', inputs, registry=catalog)


def test_inference_and_dimension_convenience_use_selected_schema(catalog):
    description, values = entries.resolve_entry_inputs('example', {'prompt': 'p', 'width': 640, 'height': 480}, registry=catalog)
    assert description['mode'] == 't2i' and values == {'prompt': 'p', 'size': '640x480'}
    description, values = entries.resolve_entry_inputs('example', {'prompt': 'p', 'image_ref': 'ref.png', 'steps': 3}, registry=catalog)
    assert description['mode'] == 'edit' and values['steps'] == 3
    with pytest.raises(ValueError, match='unsupported entry inputs'):
        entries.resolve_entry_inputs('example', {'prompt': 'p', 'seed': 4}, mode='edit', registry=catalog)


def test_workflow_nonstandard_input_binding_alias_default_and_range(monkeypatch):
    workflow = VibeWorkflow('custom', WorkflowSource('custom'))
    workflow.nodes['1'] = VibeNode('1', 'CustomNode', inputs={'texture_amount': 0.4})
    workflow.register_input('texture', '1', 'texture_amount', 0.4, type='FLOAT', default=0.4,
                            aliases=('grain',), range={'min': 0, 'max': 1})
    workflow.nodes['2'] = VibeNode('2', 'CustomNode', inputs={'instruction': 'declared prompt'})
    workflow.register_input('instruction', '2', 'instruction', 'declared prompt', default='declared prompt')
    entry = {'id': 'custom', 'kind': 'image', 'modes': ['t2i'], 'binds': {'texture_amount': 'texture'},
             'defaults': {'texture_amount': 0.6}, 'bundle': '/unused.py'}
    entry['binds']['instruction'] = 'instruction'
    monkeypatch.setattr(entries, 'user_entries', lambda: [entry])
    monkeypatch.setattr(entries, 'action_entries', lambda: [])
    monkeypatch.setattr(entries, '_load_entry_workflow', lambda selected: workflow)
    registry = ModelRegistry([])
    description = entries.describe_entry('custom', registry=registry)
    field = description['inputs'][0]
    assert field == {'name': 'texture', 'type': 'number', 'default': 0.6}
    native_field = entries.describe_entry('custom', registry=registry, _native=True)['inputs'][0]
    assert native_field['name'] == 'texture' and native_field['type'] == 'number'
    assert 'texture_amount' in native_field['aliases']
    assert native_field['default'] == 0.6 and native_field['max'] == 1
    assert description['inputs'][1]['name'] == 'instruction'
    assert description['inputs'][1]['type'] == 'string'
    assert entries.resolve_entry_inputs('custom', {'grain': 0.8}, registry=registry)[1] == {'texture': 0.8}
    with pytest.raises(ValueError, match='duplicate/conflicting'):
        entries.resolve_entry_inputs('custom', {'grain': 0.8, 'texture_amount': 0.8}, registry=registry)
    with pytest.raises(ValueError, match='violates max'):
        entries.resolve_entry_inputs('custom', {'texture_amount': 2}, registry=registry)


def test_h3_producer_local_ports_describe_and_resolve_without_execution(monkeypatch, capsys):
    # Reuse the real declarations during the port-validation matrix; repeated
    # pack discovery is unrelated to validating each managed-file boundary.
    declared_actions = entries.action_entries()
    registry = ModelRegistry.load_default()
    monkeypatch.setattr(entries, 'action_entries', lambda: declared_actions)
    monkeypatch.setattr(ModelRegistry, 'load_default', classmethod(lambda cls, **kwargs: registry))
    def forbidden(*args, **kwargs):
        pytest.fail('execution/probe operation during entry discovery')
    monkeypatch.setattr(entries, 'target_schema', forbidden)
    monkeypatch.setattr(entries, '_ready_entries_for', forbidden)
    monkeypatch.setattr(entries, '_load_entry_workflow', forbidden)
    from astrid.sdk.generation import GenerationFacade
    from astrid.sdk.client import AstridClient
    monkeypatch.setattr(GenerationFacade, '_invoke', forbidden)
    monkeypatch.setattr(AstridClient, 'open_from_launcher', forbidden)
    description = entries.describe_entry('h3', _native=True)
    assert description['mode'] == 'transform' and description['action'] == 'h3_av.transform'
    ports = {row['name']: row for row in description['inputs']}
    local = {'request', 'asset_map'}
    assert {name for name, port in ports.items() if port.get('file_custody') == 'producer_local'} == local
    assert all(ports[name]['type'] == 'file' for name in local)
    managed = {'execution_request', 'worker_qualification', 'prepared_worker', 'cleanup_receipt'}
    assert all(ports[name]['type'] == 'file' and 'file_custody' not in ports[name] for name in managed)
    assert ports['request']['required'] and not ports['asset_map']['required']
    from astrid import console
    assert console.main(['generate', 'describe', '--entry', 'h3', '--json']) == 0
    assert json.loads(capsys.readouterr().out) == entries.public_description(description)
    values = {'request': '/tmp/request.json', 'asset_map': '/tmp/assets.json'}
    assert entries.resolve_entry_inputs('h3', values)[1] == values
    with pytest.raises(ValueError, match='requires a string'):
        entries.resolve_entry_inputs('h3', {**values, 'request': {'digest': 'sha256:' + 'a' * 64}})
    with pytest.raises(ValueError, match='unsupported entry inputs'):
        entries.resolve_entry_inputs('h3', {**values, 'prompt': 'p'})
    digest = 'sha256:' + 'a' * 64
    for name in managed:
        assert entries.resolve_entry_inputs('h3', {**values, name: {'digest': digest}})[1][name] == {'digest': digest}
        with pytest.raises(ValueError, match='managed Runtime object'):
            entries.resolve_entry_inputs('h3', {**values, name: '/tmp/local.json'})


def test_other_action_file_ports_still_require_managed_descriptors(monkeypatch):
    action = {'id': 'other', 'kind': 'video', 'modes': ['transform'], 'action': 'example.run',
              'inputs': [{'name': 'file', 'type': 'file', 'required': True}]}
    monkeypatch.setattr(entries, 'user_entries', lambda: [])
    monkeypatch.setattr(entries, 'action_entries', lambda: [action])
    monkeypatch.setattr(entries, '_ready_ids', lambda: set())
    registry = ModelRegistry([])
    digest = 'sha256:' + 'a' * 64
    for value in (digest, {'digest': digest}):
        assert entries.resolve_entry_inputs('other', {'file': value}, registry=registry)[1] == {'file': value}
    with pytest.raises(ValueError, match='managed Runtime object'):
        entries.resolve_entry_inputs('other', {'file': '/tmp/local.json'}, registry=registry)
    for value in ('sha256:malformed', {'digest': 'sha256:malformed'}, {'digest': digest, 'object_id': 'sha256:' + 'b' * 64}):
        with pytest.raises(ValueError, match='managed Runtime object'):
            entries.resolve_entry_inputs('other', {'file': value}, registry=registry)


def test_workflow_legacy_translation_is_shared_and_mode_specific(monkeypatch):
    workflow = VibeWorkflow('legacy', WorkflowSource('legacy'))
    workflow.nodes['1'] = VibeNode('1', 'CustomNode', inputs={'cfg': 1.0, 'image': 'reference.png'})
    workflow.register_input('guidance', '1', 'cfg', 1.0, type='FLOAT', default=1.0)
    workflow.register_input('image', '1', 'image', 'reference.png', type='STRING', default='reference.png')
    entry = {'id': 'legacy', 'kind': 'image', 'modes': ['t2i', 'edit'], 'bundle': '/unused.py',
             'binds': {'guidance': 'guidance', 'image': 'image'}, 'defaults': {}}
    monkeypatch.setattr(entries, 'user_entries', lambda: [entry])
    monkeypatch.setattr(entries, 'action_entries', lambda: [])
    loads = []
    monkeypatch.setattr(entries, '_load_entry_workflow', lambda selected: loads.append(selected) or workflow)
    registry = ModelRegistry([])
    description = entries.describe_entry('legacy', registry=registry, _native=True)
    assert len(loads) == 1
    t2i = {field['name']: field for field in description['mode_descriptions']['t2i']['inputs']}
    edit = {field['name']: field for field in description['mode_descriptions']['edit']['inputs']}
    assert 'guidance_scale' in t2i['guidance']['aliases']
    assert 'image_ref' not in t2i['image'].get('aliases', [])
    assert 'image_ref' in edit['image']['aliases']
    assert entries.resolve_entry_inputs('legacy', {'guidance_scale': 1.5}, mode='t2i', registry=registry)[1] == {'guidance': 1.5}
    assert entries.resolve_entry_inputs('legacy', {'image_ref': 'new.png'}, mode='edit', registry=registry)[1] == {'image': 'new.png'}
    with pytest.raises(ValueError, match='duplicate/conflicting'):
        entries.resolve_entry_inputs('legacy', {'guidance': 1.5, 'guidance_scale': 1.5}, mode='t2i', registry=registry)


def test_unknown_declared_type_fails_closed():
    description = {'inputs': [{'name': 'opaque', 'type': 'unknown', 'required': False}]}
    with pytest.raises(ValueError, match='no declared scalar type'):
        entries.validate_inputs(description, {'opaque': 'guess'})


def test_image_widget_is_a_file_only_for_load_image():
    workflow = VibeWorkflow('image', WorkflowSource('image'))
    workflow.nodes['1'] = VibeNode('1', 'LoadImage', inputs={'image': 'input.png'})
    workflow.nodes['2'] = VibeNode('2', 'CustomNode', inputs={'image': 'input.png'})
    workflow.register_input('load', '1', 'image', 'input.png', type='IMAGE')
    workflow.register_input('custom', '2', 'image', 'input.png', type='IMAGE')
    ports = entries._workflow_inputs({'binds': {}, 'defaults': {}}, 'edit', workflow=workflow)
    by_name = {port['name']: port for port in ports}
    assert by_name['load']['type'] == 'file'
    assert by_name['custom']['type'] == 'image'
    assert entries.validate_inputs({'inputs': ports}, {'load': 'reference.png'})['load'] == 'reference.png'
    with pytest.raises(ValueError, match='requires a string'):
        entries.validate_inputs({'inputs': ports}, {'load': 4})
    with pytest.raises(ValueError, match='no declared scalar type'):
        entries.validate_inputs({'inputs': ports}, {'custom': 'guess'})


def test_number_values_must_be_finite():
    description = {'inputs': [{'name': 'strength', 'type': 'number', 'required': False}]}
    for value in (True, float('inf'), float('nan'), '0.5'):
        with pytest.raises(ValueError, match='finite number'):
            entries.validate_inputs(description, {'strength': value})


def test_canonical_workflow_metadata_loader_is_offline_and_read_only(tmp_path, monkeypatch):
    from vibecomfy.schema import provider
    from vibecomfy.schema.on_demand import OnDemandInstallSchemaProvider
    def forbidden(*args, **kwargs):
        pytest.fail('live schema/provider/installation operation during metadata loading')
    monkeypatch.setattr(provider.RuntimeSchemaProvider, 'get_schema', forbidden)
    monkeypatch.setattr(OnDemandInstallSchemaProvider, 'get_schema', forbidden)
    source = tmp_path / 'custom.py'
    source.write_text('''from vibecomfy.workflow import VibeWorkflow, VibeNode, WorkflowSource
def build():
    wf = VibeWorkflow('metadata-only', WorkflowSource('metadata-only'))
    wf.nodes['1'] = VibeNode('1', 'CustomTextureNode', inputs={'grain': 0.5}, uid='custom-texture-node')
    wf.register_input('grain', '1', 'grain', 0.5, type='FLOAT', default=0.5)
    return wf
''')
    before = {path: path.read_bytes() for path in tmp_path.rglob('*') if path.is_file()}
    workflow = entries._load_entry_workflow({'bundle': str(source)})
    assert workflow.inputs['grain'].default == 0.5
    assert {path: path.read_bytes() for path in tmp_path.rglob('*') if path.is_file()} == before
