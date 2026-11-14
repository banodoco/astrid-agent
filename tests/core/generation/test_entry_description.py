from __future__ import annotations

import json

import pytest

from astrid.core.generation import entries
from astrid.core.model_catalog.registry import ModelRegistry
from astrid.core.model_catalog.schema import ModelEntry, ModeSpec
from vibecomfy.workflow import VibeNode, VibeWorkflow, WorkflowSource


def test_shipped_foley_description_preserves_explicit_sfx_video_and_license(tmp_path):
    identifier = 'audio/hunyuan_video_foley'
    video = tmp_path / 'clip.mp4'
    video.write_bytes(b'local synthetic video fixture')
    description, values = entries.resolve_entry_inputs(identifier,
        {'prompt': 'gravel footsteps', 'video_ref': str(video)}, mode='sfx', kind='audio')
    assert description['kind'] == 'audio' and description['modes'] == ['sfx']
    assert description['mode'] == 'sfx'
    assert 'license_notice' not in description
    assert {row['name']: row['type'] for row in description['inputs']}['video_ref'] == 'file'
    assert values == {'prompt': 'gravel footsteps', 'video_ref': str(video)}
    native = entries.describe_entry(identifier, mode='sfx', _native=True)
    assert all(next(row for row in native['inputs'] if row['name'] == name)['required']
               for name in ('video_ref', 'prompt'))


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


@pytest.mark.parametrize('entry_id', ['lava-sr', 'deepfilternet3'])
def test_audio_enhancement_catalog_inputs_use_typed_action_ports(entry_id):
    spec = ModelRegistry.load_default().get(entry_id).modes['sfx']
    description = {'inputs': entries._catalog_inputs('audio', spec, 'sfx')}
    fields = {row['name']: row for row in description['inputs']}
    assert set(fields) == set(spec.supports)
    assert all(row['type'] == 'string' for row in fields.values())
    assert fields['audio_ref']['required']
    values = {'audio_ref': 'reference.wav'}
    if entry_id == 'lava-sr':
        values.update(audio_format='wav', bitrate='192k')
    assert entries.validate_inputs(description, values) == values
    with pytest.raises(entries.MissingRequiredEntryInputsError, match='audio_ref'):
        entries.validate_inputs(description, {})
    for name in fields:
        with pytest.raises(ValueError, match='requires a string'):
            entries.validate_inputs(description, {**values, name: 7})


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


def test_h3_declared_inputs_describe_and_resolve_without_execution(monkeypatch):
    """h3 declares both request-document and plain-input ports without executing its bundle."""
    monkeypatch.setattr(entries, 'target_schema', lambda *a, **k: pytest.fail('probe during entry discovery'))
    monkeypatch.setattr(entries, '_load_entry_workflow', lambda *a, **k: pytest.fail('bundle loaded for declared inputs'))
    description = entries.describe_entry('h3', _native=True)
    assert description['source'] == 'workflow' and 'action' not in description
    assert description['mode'] is None and description['modes'] == ['i2v', 'v2v', 'video-edit']
    description = entries.describe_entry('h3', mode='v2v', _native=True)
    ports = {row['name']: row for row in description['inputs']}
    # h3 declares the kind's plain inputs (5fe9ed7a0): either input form is optional per port.
    assert [(n, p['type'], p['required']) for n, p in ports.items()] == [
        ('request', 'file', False), ('asset_map', 'file_map', False),
        ('prompt', 'string', False), ('image_ref', 'file', False),
        ('video_ref', 'file', False), ('frames', 'integer', False),
        ('seed', 'integer', False), ('steps', 'integer', False)]
    values = {'request': '/tmp/request.json', 'asset_map': {'source': '/tmp/source.mp4'}}
    assert entries.resolve_entry_inputs('h3', values, mode='v2v')[1] == values
    plain = {'prompt': 'p', 'video_ref': '/tmp/source.mp4'}
    assert entries.resolve_entry_inputs('h3', plain, mode='v2v')[1] == plain
    with pytest.raises(ValueError, match='nonempty map'):
        entries.resolve_entry_inputs('h3', {**values, 'asset_map': '/tmp/assets.json'}, mode='v2v')
    with pytest.raises(ValueError, match='unsupported entry inputs'):
        entries.resolve_entry_inputs('h3', {**values, 'undeclared': 'p'}, mode='v2v')
