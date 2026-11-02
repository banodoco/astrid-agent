from __future__ import annotations

import hashlib
from pathlib import Path
from unittest.mock import Mock

import pytest

from astrid.core.model_catalog.registry import ModelRegistry
from astrid.sdk.generation import GenerationFacade
from vibecomfy.registry.ready import workflow_from_ready


def test_accepted_foley_catalog_and_template_are_present():
    model = ModelRegistry.load_default().get('hunyuan-foley-local')
    backend = model.modes['sfx'].backends['local']
    assert model.modality == 'audio'
    assert backend.template == 'audio/hunyuan_foley_v2a'
    workflow = workflow_from_ready(backend.template)
    assert backend.template_hash == 'sha256:' + hashlib.sha256(Path(workflow.source.path).read_bytes()).hexdigest()
    assert set(model.modes['sfx'].requires) == {'video_ref'}


@pytest.mark.parametrize('source', ['catalog', 'workflow'])
@pytest.mark.parametrize('explicit', [False, True])
def test_foley_requires_explicit_selection_before_qualification(monkeypatch, source, explicit):
    from astrid.core.generation import availability, entries, preflight
    import astrid.sdk.generation as generation

    model = ModelRegistry.load_default().get('hunyuan-foley-local')
    manifest = {'id': 'audio/hunyuan_foley_v2a', 'kind': 'audio', 'modes': ['sfx']}
    registry = ModelRegistry([model] if source == 'catalog' else [])
    identifier = model.id if source == 'catalog' else manifest['id']
    monkeypatch.setattr(entries, 'user_entries', lambda: [])
    monkeypatch.setattr(entries, 'ready_entries', lambda: [manifest] if source == 'workflow' else [])
    monkeypatch.setattr(entries, 'find_entry', lambda name: manifest if source == 'workflow' else None)
    qualify = Mock()
    monkeypatch.setattr(availability, 'require_model_available', qualify)
    monkeypatch.setattr(availability, 'require_entry_available', qualify)
    monkeypatch.setattr(generation, '_validate_catalog_target_request', qualify)
    class Probed(Exception):
        pass
    probe = Mock(side_effect=Probed)
    monkeypatch.setattr(preflight, 'select_generation_target', probe)
    with pytest.raises(Probed):
        GenerationFacade()._generate_on_target(registry=registry,
            model=identifier if explicit else None, mode='sfx', modality='audio',
            target='auto', execution=None, inputs={'video_ref': 'clip.mp4'}, dry_run=True)
    assert qualify.call_count == int(explicit)
    probe.assert_called_once()
