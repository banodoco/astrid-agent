"""Frame cache binds registry resolution and shared renderer source bytes."""
from pathlib import Path
from types import SimpleNamespace

from astrid.core.rendering import project_elements
from astrid.packs.rendering.actions.timeline_visualize import composed_frame as capture
from astrid.packs.rendering.rendering.renderers.remotion import run


def test_registry_aliases_and_shared_sources_bind_cache_without_staging_names(tmp_path, monkeypatch):
    manifest = tmp_path / 'remotion/node_modules/@banodoco/timeline-composition/package.json'
    manifest.parent.mkdir(parents=True)
    manifest.write_text('{}')
    monkeypatch.setenv('ASTRID_REMOTION_PROJECT_DIR', str(tmp_path / 'remotion'))
    rendering = tmp_path / 'rendering'
    shared = rendering / 'shared'
    ui = rendering / 'ui/threejs-live-editor'
    shared.mkdir(parents=True)
    ui.mkdir(parents=True)
    contract = shared / 'element_contracts.ts'
    surface = ui / 'SceneSurface.tsx'
    contract.write_text('contract one')
    surface.write_text('surface one')
    registry = object()
    state = {'root': tmp_path / 'collection-a'}

    def roots(*, registry, include_aliases):
        assert include_aliases
        return [
            {'root': str(rendering), 'scope': 'pack-rendering-elements',
             'kind_roots': {'effects': str(rendering / 'effects')}},
            {'root': str(state['root']), 'scope': 'pack-collection-elements',
             'kind_roots': {'effects': str(state['root'] / 'effects')}},
            {'root': str(registry), 'scope': 'pack-pinned-elements',
             'kind_roots': {'effects': str(registry / 'elements/effects')}},
        ]

    monkeypatch.setattr(run, 'gen_effect_registry', SimpleNamespace(
        _element_registry=lambda: registry, render_pack_roots=roots))
    monkeypatch.setattr(project_elements, 'selections', lambda config: [object()])
    monkeypatch.setattr(project_elements, 'stage_project_elements',
                        lambda config, root, registry: (root / 'pack',
                            [{'root': str(root / 'pack'), 'scope': 'pack-pinned-elements'}]))
    first = capture._render_registry_cache_inputs({'clips': []})
    assert first == capture._render_registry_cache_inputs({'clips': []})
    assert 'astrid-cache-registry-' not in str(first)
    state['root'] = tmp_path / 'collection-b'
    moved = capture._render_registry_cache_inputs({'clips': []})
    assert moved[0] != first[0] and moved[1] == first[1]
    contract.write_text('contract two')
    changed_contract = capture._render_registry_cache_inputs({'clips': []})
    assert changed_contract[1] != moved[1]
    surface.write_text('surface two')
    assert capture._render_registry_cache_inputs({'clips': []})[1] != changed_contract[1]


def test_renderer_environment_includes_aliases_and_shared_digest(tmp_path, monkeypatch):
    monkeypatch.setattr(capture, '_checkout_root', lambda: tmp_path)
    monkeypatch.setattr(capture, '_digest_paths', lambda paths: 'same')
    monkeypatch.setattr(capture, '_pack_root_digests', lambda checkout, config: [])
    selected = {'aliases': [{'scope': 'first'}], 'shared': 'one'}
    monkeypatch.setattr(capture, '_render_registry_cache_inputs',
                        lambda config: (selected['aliases'], selected['shared']))
    first = capture.renderer_environment_identity({})
    selected['aliases'] = [{'scope': 'second'}]
    assert capture.renderer_environment_identity({}) != first
    selected['aliases'] = [{'scope': 'first'}]
    selected['shared'] = 'two'
    assert capture.renderer_environment_identity({}) != first
