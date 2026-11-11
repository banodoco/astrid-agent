"""A dormant blank image loader must not become an active media input."""
from types import SimpleNamespace

import pytest

from astrid.packs.vibecomfy import local_server_engine


def test_blank_image_loader_is_not_registered_as_public_input(tmp_path, monkeypatch):
    from vibecomfy.workflow import VibeWorkflow, WorkflowSource

    workflow = VibeWorkflow('blank', WorkflowSource('blank'))
    workflow.add_node('LoadImage', _id='1', uid='placeholder', image='')
    bundle = SimpleNamespace(workflow=workflow)
    monkeypatch.setattr(local_server_engine, '_canonical_bundle_value', lambda value: bundle)
    result = local_server_engine.local_server_bundle(bundle, tmp_path)
    assert result is bundle
    assert 'image' not in workflow.inputs


@pytest.mark.parametrize('second_output', [True, False], ids=['second-output-connected', 'dormant'])
def test_blank_loader_checks_every_target_output_root(tmp_path, monkeypatch, second_output):
    api = {
        '1': {'class_type': 'LoadImage', 'inputs': {'image': ''}},
        '2': {'class_type': 'SaveImage' if second_output else 'UnusedNode', 'inputs': {'images': ['1', 0]}},
        '3': {'class_type': 'SaveImage', 'inputs': {'images': ['4', 0]}},
        '4': {'class_type': 'EmptyImage', 'inputs': {}},
    }
    info = {
        'LoadImage': {'input': {'required': {'image': [['available.png']]}}, 'output': ['IMAGE'], 'output_node': False},
        'SaveImage': {'input': {'required': {'images': ['IMAGE']}}, 'output': [], 'output_node': True},
        'UnusedNode': {'input': {'required': {'images': ['IMAGE']}}, 'output': [], 'output_node': False},
        'EmptyImage': {'input': {'required': {}}, 'output': ['IMAGE'], 'output_node': False},
    }
    workflow = SimpleNamespace(outputs=[SimpleNamespace(node_id='3')], nodes={},
        runtime_nodes=lambda: {key: SimpleNamespace(class_type=node['class_type']) for key, node in api.items()},
        compile=lambda *args, **kwargs: api)
    bundle = SimpleNamespace(workflow=workflow)
    client = SimpleNamespace(object_info=lambda: info)
    monkeypatch.setattr(local_server_engine, '_uploaded_media_targets', lambda *args: set())
    from vibecomfy.schema import validate
    monkeypatch.setattr(validate, 'validate_api_against_schema', lambda *args: [])
    monkeypatch.setattr(validate, 'validate_api_link_shapes', lambda *args: [])
    if second_output:
        with pytest.raises(local_server_engine.ProductionEngineError, match='connected LoadImage'):
            local_server_engine._live_preflight(bundle, {}, 'http://127.0.0.1:8188', tmp_path, client=client)
    else:
        local_server_engine._live_preflight(bundle, {}, 'http://127.0.0.1:8188', tmp_path, client=client)
