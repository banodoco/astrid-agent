"""A dormant blank image loader must not become an active media input."""
from types import SimpleNamespace

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
