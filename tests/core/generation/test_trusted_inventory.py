"""Explicit trust accepts exact model declarations and preserves graph validation."""
from copy import deepcopy
from types import SimpleNamespace

import pytest
from vibecomfy.workflow import VibeEdge, VibeNode, VibeOutput, VibeWorkflow, WorkflowSource

from astrid.core.generation import entries
from astrid.core.generation.target_inventory import trust_declared_loader_choices
from astrid.packs.vibecomfy.local_server_engine import _live_preflight


def _fixture():
    workflow = VibeWorkflow('trust-test', WorkflowSource('trust-test'))
    workflow.nodes['1'] = VibeNode('1', 'CheckpointLoaderSimple', inputs={'ckpt_name': 'declared.safetensors'})
    workflow.nodes['2'] = VibeNode('2', 'TestOutput', inputs={'count': 1, 'style': 'normal'})
    workflow.edges.append(VibeEdge('1', '0', '2', 'model'))
    workflow.outputs.append(VibeOutput('2', 'TestOutput'))
    info = {
        'CheckpointLoaderSimple': {'input': {'required': {'ckpt_name': [['other.safetensors']]}},
                                  'output': ['MODEL'], 'output_name': ['MODEL']},
        'TestOutput': {'input': {'required': {'model': ['MODEL'], 'count': ['INT'], 'style': [['normal']]}},
                       'output': [], 'output_node': True},
    }
    client = SimpleNamespace(object_info=lambda: info, http_origin='http://127.0.0.1:8188',
                             target=SimpleNamespace(kind='attached', facts={}))
    entry = {'id': 'test', 'kind': 'image', 'needs': [{'class_type': 'CheckpointLoaderSimple',
             'field': 'ckpt_name', 'value': 'declared.safetensors', 'role': 'checkpoint'}]}
    return workflow, info, client, entry


def test_trust_overlays_only_exact_declared_loader_choices_and_survives_refresh():
    workflow, info, client, entry = _fixture()
    original = deepcopy(info)
    provider = entries.target_schema(entry, workflow, client, align='trust')
    assert provider.target_model_reference('CheckpointLoaderSimple', 'ckpt_name', 'declared.safetensors')
    assert not provider.target_model_reference('CheckpointLoaderSimple', 'ckpt_name', 'unrelated.safetensors')
    provider.refresh()
    assert provider.target_model_reference('CheckpointLoaderSimple', 'ckpt_name', 'declared.safetensors')
    assert info == original
    assert provider.get_schema('TestOutput').inputs['style'].choices == ['normal']


@pytest.mark.parametrize('mode', ['check', 'align'])
def test_non_trust_still_refuses_missing_loader(mode):
    workflow, _, client, entry = _fixture()
    with pytest.raises(Exception, match='missing checkpoint'):
        entries.target_schema(entry, workflow, client, align=mode)


@pytest.mark.parametrize('fault', ['missing_node', 'type', 'link', 'widget'])
def test_trust_live_preflight_keeps_non_model_validation(fault, tmp_path):
    workflow, info, client, _ = _fixture()
    if fault == 'missing_node':
        info.pop('CheckpointLoaderSimple')
    elif fault == 'type':
        workflow.nodes['2'].inputs['count'] = 'wrong'
    elif fault == 'link':
        workflow.edges[0] = VibeEdge('missing', '0', '2', 'model')
    else:
        workflow.nodes['2'].inputs['style'] = 'unknown'
    with pytest.raises(Exception):
        _live_preflight(SimpleNamespace(workflow=workflow), None, client.http_origin, tmp_path,
                        client=client, target_custody=[])


def test_live_trust_accepts_exact_loader_and_keeps_live_facts(tmp_path):
    workflow, info, client, _ = _fixture()
    original = deepcopy(info)
    provider = _live_preflight(SimpleNamespace(workflow=workflow), None, client.http_origin, tmp_path,
                               client=client, target_custody=[])
    assert provider.target_model_reference('CheckpointLoaderSimple', 'ckpt_name', 'declared.safetensors')
    assert info == original
    assert trust_declared_loader_choices({}, workflow) == {}


def test_live_check_still_refuses_missing_loader(tmp_path):
    workflow, _, client, _ = _fixture()
    with pytest.raises(Exception, match='missing models'):
        _live_preflight(SimpleNamespace(workflow=workflow), None, client.http_origin, tmp_path, client=client)


def test_only_explicit_trust_skips_managed_local_byte_verification(monkeypatch):
    workflow, info, client, entry = _fixture()
    info['CheckpointLoaderSimple']['input']['required']['ckpt_name'][0].append('declared.safetensors')
    client.target = SimpleNamespace(kind='managed', facts={'model_root': {'test': 'binding'}})
    from astrid.core.generation import model_root
    def reject_binding(*args, **kwargs):
        raise ValueError('bytes not verified')
    monkeypatch.setattr(model_root, 'validate_model_root_binding', reject_binding)
    entries.target_schema(entry, workflow, client, align='trust')
    for mode in ('align', 'check'):
        with pytest.raises(ValueError, match='bytes not verified'):
            entries.target_schema(entry, workflow, client, align=mode)


def test_catalog_trust_resolves_only_a_single_declared_missing_name():
    from dataclasses import replace
    from astrid.core.model_catalog.registry import ModelRegistry
    from astrid.core.generation.backends.comfy_target import _capture_model_resolution, ComfyModelMissing
    from astrid.core.generation.backends.vibecomfy import VibeComfyBackend
    from tests.core.generation.backends.test_qwen_21_turbo import object_info
    from vibecomfy.registry.ready import workflow_from_ready
    from vibecomfy.workflow_bundle import load_bundle
    registry = ModelRegistry.load_default()
    entry = registry.get('qwen-image-2.1-turbo')
    workflow = workflow_from_ready(entry.modes['t2i'].backends['local'].template)
    info = object_info(workflow)
    info['UNETLoader']['input']['required']['unet_name'] = [[]]
    client = SimpleNamespace(object_info=lambda: info, http_origin='http://127.0.0.1:8188',
                             target=SimpleNamespace(url='http://127.0.0.1:8188', kind='attached'))
    backend = VibeComfyBackend(target_client=client)
    with pytest.raises(ComfyModelMissing):
        backend.prepare_catalog_workflow(entry, 't2i', {'prompt': 'test', 'seed': 42}, resolve_target=True)
    prepared, plan, schema = backend.prepare_catalog_workflow(
        entry, 't2i', {'prompt': 'test', 'seed': 42}, resolve_target=True, align='trust')
    assert plan['model_bytes'] == 'unverified_declaration'
    assert load_bundle(prepared).compile(schema_provider=schema).api_projection
    assert info['UNETLoader']['input']['required']['unet_name'] == [[]]
    spec = entry.modes['t2i'].backends['local']
    role, declaration = next((role, declaration) for role, declaration in spec.model_roles.items()
                             if declaration.node_class == 'UNETLoader')
    ambiguous = replace(spec, model_roles={**spec.model_roles,
        role: replace(declaration, names=(*declaration.names, 'another.safetensors'))})
    mode = replace(entry.modes['t2i'], backends={'local': ambiguous})
    ambiguous_entry = replace(entry, modes={'t2i': mode})
    with pytest.raises(ComfyModelMissing):
        _capture_model_resolution(client, ModelRegistry([ambiguous_entry]), entry.id, 't2i', align='trust')
