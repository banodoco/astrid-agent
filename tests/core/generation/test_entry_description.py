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
    assert fields['seed']['type'] == 'integer' and 'default' not in fields['seed']
    assert fields['count']['default'] == 1
    assert fields['prompt']['required'] and 'default' not in fields['prompt']
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
    assert field['name'] == 'texture_amount' and field['type'] == 'number'
    assert field['default'] == 0.6 and field['max'] == 1
    assert description['inputs'][1]['name'] == 'instruction'
    assert description['inputs'][1]['type'] == 'string'
    assert entries.resolve_entry_inputs('custom', {'grain': 0.8}, registry=registry)[1] == {'texture_amount': 0.8}
    with pytest.raises(ValueError, match='duplicate/conflicting'):
        entries.resolve_entry_inputs('custom', {'grain': 0.8, 'texture_amount': 0.8}, registry=registry)
    with pytest.raises(ValueError, match='violates max'):
        entries.resolve_entry_inputs('custom', {'texture_amount': 2}, registry=registry)


def test_h3_managed_ports_describe_and_resolve_without_execution(monkeypatch):
    def forbidden(*args, **kwargs):
        pytest.fail('execution/probe operation during entry discovery')
    monkeypatch.setattr(entries, 'target_schema', forbidden)
    monkeypatch.setattr(entries, '_ready_entries_for', forbidden)
    monkeypatch.setattr(entries, '_load_entry_workflow', forbidden)
    from astrid.sdk.generation import GenerationFacade
    monkeypatch.setattr(GenerationFacade, '_invoke', forbidden)
    description = entries.describe_entry('h3')
    assert description['mode'] == 'transform' and description['action'] == 'h3_av.transform'
    assert [(row['name'], row['type'], row['required']) for row in description['inputs']] == [
        ('request', 'file', True), ('asset_map', 'file', True)]
    digest = 'sha256:' + 'a' * 64
    values = {'request': {'digest': digest}, 'asset_map': digest}
    assert entries.resolve_entry_inputs('h3', values)[1] == values
    with pytest.raises(ValueError, match='managed Runtime object'):
        entries.resolve_entry_inputs('h3', {**values, 'request': '/tmp/request.json'})
    with pytest.raises(ValueError, match='unsupported entry inputs'):
        entries.resolve_entry_inputs('h3', {**values, 'prompt': 'p'})


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
    description = entries.describe_entry('legacy', registry=registry)
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
