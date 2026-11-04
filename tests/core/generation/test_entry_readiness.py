from pathlib import Path
from types import SimpleNamespace

from astrid.core.generation import entries, listing
from astrid.core.generation.backends.comfy_target import ComfyTarget
from astrid.core.model_catalog.registry import ModelRegistry
from vibecomfy.workflow import VibeNode, VibeWorkflow, WorkflowSource


def test_ready_projection_exposes_inputs_requirements_and_absolute_bundle():
    entry = entries.find_entry('image/z_image_turbo')
    assert Path(entry['bundle']).is_absolute() and Path(entry['bundle']).is_file()
    assert {'prompt', 'width', 'height', 'seed'} <= entry['binds'].keys()
    assert entry['inputs'] and entry['requirements']['models'] == entry['needs']
    assert {'UNETLoader', 'CLIPLoader', 'VAELoader'} <= {need['class_type'] for need in entry['needs']}
    assert entry['registry_id'] == 'z-image'


def test_user_entry_readiness_depends_on_each_target_inventory(monkeypatch, tmp_path):
    workflow = VibeWorkflow('entry', WorkflowSource('entry'))
    workflow.nodes['1'] = VibeNode('1', 'UNETLoader', inputs={'unet_name': 'chosen.safetensors'})
    workflow.nodes['2'] = VibeNode('2', 'SaveImage')
    entry = {'id': 'mine', 'kind': 'image', 'modes': ['t2i'], 'bundle': str(tmp_path / 'workflow.py'),
             'needs': [{'role': 'diffusion_model', 'class_type': 'UNETLoader', 'field': 'unet_name',
                        'value': 'chosen.safetensors'}], 'binds': {}, 'defaults': {}}
    monkeypatch.setattr(entries, 'user_entries', lambda: [entry])
    monkeypatch.setattr('astrid.packs.vibecomfy.production_engine.load_workflow_path',
                        lambda *args, **kwargs: SimpleNamespace(resolved=SimpleNamespace(workflow=workflow)))
    row = dict(id='mine', source='user-entry', template=entry['bundle'], mode='t2i', reason='')
    target = ComfyTarget('http://127.0.0.1:8188', 'attached')
    schema = {'UNETLoader': {'input': {'required': {'unet_name': [['chosen.safetensors']]}}},
              'SaveImage': {'input': {'required': {}}}}
    ready = listing._local_readiness(row, ModelRegistry([]), target, schema)
    assert ready['ready'] and ready['models_present'] == ['chosen.safetensors']
    schema['UNETLoader']['input']['required']['unet_name'] = [['other.safetensors']]
    missing = listing._local_readiness(row, ModelRegistry([]), target, schema)
    assert not missing['ready'] and 'chosen.safetensors' in missing['reason']


def test_empty_modes_and_directory_bundle_are_reported(monkeypatch, tmp_path):
    entry = {'id': 'review', 'kind': 'audio', 'modes': [], 'bundle': str(tmp_path),
             'needs': [{'ambiguity': 'choose an audio mode'}], 'binds': {}, 'defaults': {}}
    monkeypatch.setattr(entries, 'user_entries', lambda: [entry])
    monkeypatch.setattr(entries, 'ready_entries', lambda: [])
    monkeypatch.setattr(listing, 'discover', lambda: SimpleNamespace(actions=[]))
    monkeypatch.setattr(listing, 'availability_declaration', lambda: {})
    row = next(row for row in listing.catalog_rows() if row['id'] == 'review')
    assert row['mode'] == '' and 'review' in row['reason']
    assert entries.bundle_path(entry) == tmp_path / 'workflow.py'


def test_malformed_target_inventory_is_not_ready(monkeypatch, tmp_path):
    workflow = VibeWorkflow('entry', WorkflowSource('entry'))
    workflow.nodes['1'] = VibeNode('1', 'UNETLoader', inputs={'unet_name': 'model.safetensors'})
    entry = {'id': 'mine', 'needs': [{'role': 'diffusion', 'class_type': 'UNETLoader',
             'field': 'unet_name', 'value': 'model.safetensors'}]}
    target = ComfyTarget('http://127.0.0.1:8188', 'attached')
    client = listing._InventoryClient(target, {'UNETLoader': {'input': []}})
    import pytest
    with pytest.raises(ValueError, match='target is missing'):
        entries.target_schema(entry, workflow, client)
