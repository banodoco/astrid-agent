"""Bounded default dispatch, canonical snapshot custody and CPU refusals."""
import copy
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

import pytest

from astrid.core.contracts.errors import AstridError
from astrid.core.generation import entries, preflight
from astrid.core.model_catalog.registry import ModelRegistry
from astrid.sdk.generation import GenerationFacade, _snapshot_generation_workflow
from tests.core.generation.backends.test_comfy_model_resolution import install_target, target_info
from tests.core.generation.backends.test_comfy_target import (  # noqa: F401
    engine_configuration, network, observation_configuration,
)


@pytest.mark.parametrize('failure', ['missing_model', 'pin'])
def test_failed_qwen_default_surfaces_own_error_without_z_image(network, monkeypatch, failure):
    info = target_info()
    if failure == 'pin':
        from vibecomfy.porting.object_info import get_class
        from vibecomfy.registry.ready import workflow_from_ready

        registry = ModelRegistry.load_default()
        preferred = registry.get('qwen-image-2.1-turbo')
        spec = preferred.modes['t2i'].backends['local']
        # The shared target fixture advertises Z-Image. The pin scenario must
        # advertise Qwen's required nodes before it can reach byte validation.
        for node in workflow_from_ready(spec.template).runtime_nodes().values():
            if node.class_type in info:
                continue
            entry = get_class(node.class_type)
            assert entry is not None
            info[node.class_type] = {
                'input': copy.deepcopy(entry['inputs']),
                'input_order': copy.deepcopy(entry['input_order']),
                'output': [item['type'] for item in entry['outputs']],
                'output_name': [item['name'] for item in entry['outputs']],
                'output_is_list': [item['is_list'] for item in entry['outputs']],
            }
        for role in spec.model_roles.values():
            info[role.node_class]['input']['required'][role.input][0].extend(role.names)
        # Supply an actual bad catalog pin. A complete compatible target and
        # the untampered producer template are otherwise a valid dry run.
        bad_mode = replace(preferred.modes['t2i'], backends={
            **preferred.modes['t2i'].backends,
            'local': replace(spec, template_hash='sha256:' + '0' * 64),
        })
        bad_preferred = replace(preferred, modes={**preferred.modes, 't2i': bad_mode})
        registry = ModelRegistry([
            bad_preferred if row.id == preferred.id else row
            for row in registry.list_all(include_closed=True)
        ])
        monkeypatch.setattr(ModelRegistry, 'load_default', classmethod(lambda cls, **kwargs: registry))
    client, calls = install_target(network, info)
    with pytest.raises(AstridError) as caught:
        GenerationFacade().image(placement=client.target.url, prompt='boat', dry_run=True)
    assert caught.value.code == ('input_rejected' if failure == 'pin' else 'missing_model')
    if failure == 'pin':
        assert 'failed its sha256 pin' in str(caught.value)
    else:
        assert 'qwen_image_2.1_turbo_int8_convrot.safetensors' in str(caught.value)
    assert caught.value.to_envelope()['entry']['id'] == 'qwen-image-2.1-turbo'
    # Z-Image would make a second inventory request and successfully substitute.
    assert calls == [('GET', '/system_stats'), ('GET', '/object_info')]


@pytest.mark.parametrize('mode,preferred', [
    ('tts', 'audio/qwen3_tts_custom_voice'),
    ('music', 'audio/minimax_music_3'),
    ('sfx', 'audio/hunyuan_video_foley'),
])
def test_audio_preferred_row_owns_its_error(network, monkeypatch, mode, preferred):
    client, _ = install_target(network, target_info())
    row = next(row for row in entries.ready_entries() if row['id'] == preferred)
    monkeypatch.setattr(entries, 'ready_entries', lambda: [dict(row, id='aaa-alternative'), row])
    monkeypatch.setattr(entries, 'user_entries', lambda: [])
    attempted = []

    def prepare(path, **kwargs):
        attempted.append(Path(path).stem)
        raise AstridError('preferred workflow dependency is unavailable', code='missing_node_pack')

    monkeypatch.setattr(entries, 'prepare', prepare)
    with pytest.raises(AstridError, match='preferred workflow dependency') as caught:
        GenerationFacade()._generate_on_target(registry=ModelRegistry([]), model=None,
            mode=mode, modality='audio', placement=client.target.url, execution=None,
            inputs={}, dry_run=True)
    assert caught.value.code == 'missing_node_pack'
    assert attempted == [Path(row['bundle']).stem]


@pytest.mark.parametrize('template', ['audio/qwen3_tts_custom_voice', 'audio/minimax_music_3'])
def test_ready_snapshot_retains_declared_source_and_loads_in_production(template, tmp_path):
    from vibecomfy.registry.ready import repo_ready_template_root, resolve_ready_template
    from vibecomfy.security.provenance import Provenance
    from vibecomfy.workflow_bundle import load_bundle
    from astrid.packs.vibecomfy.production_engine import load_workflow_path

    original = resolve_ready_template(template).path
    before = load_bundle(original, trust=Provenance.USER_CONFIRMED)
    metadata = before.workflow.metadata
    declared = repo_ready_template_root().parent / (
        metadata.get('source_workflow_path') or metadata.get('source_path') or metadata['source_workflow'])
    members = {p: p.read_bytes() for p in (original, original.with_suffix('.vibe.json'), declared)}
    with _snapshot_generation_workflow(original) as frozen:
        assert frozen.parent != original.parent
        assert frozen.read_bytes() == members[original]
        assert frozen.with_name('source.json').read_bytes() == members[declared]
        loaded = load_workflow_path(frozen, tmp_path / 'production')
        loaded.resolved.require_canonical_authority('snapshot regression')
        assert loaded.workflow_identity == before.workflow_identity
        assert loaded.workflow_revision == before.revision_id
        assert loaded.resolved.semantic_digest == before.semantic_digest
        assert loaded.resolved.ui_digest == before.ui_digest
        assert loaded.resolved.workflow.inputs.keys() == before.workflow.inputs.keys()
        assert set(members) == {original, original.with_suffix('.vibe.json'), declared}
    assert not frozen.exists()
    assert all(path.read_bytes() == data for path, data in members.items())


@pytest.mark.parametrize('mode', ['t2i', 'edit', 't2v', 'tts', 'music', 'sfx'])
def test_no_gpu_envelope_names_target_and_explicit_voice_option(mode, monkeypatch):
    monkeypatch.setattr(preflight, '_registered_managed_engine', lambda: None)
    entry = SimpleNamespace(id='selected', modes={mode: SimpleNamespace(
        backends={'local': SimpleNamespace(template='declared/template')})})
    with pytest.raises(preflight.GenerationReadinessError) as caught:
        preflight.require_generation_target_readiness(entry, mode, None)
    envelope = caught.value.to_envelope()
    assert envelope['status'] == 'blocked' and envelope['task_id'] is None
    assert envelope['issues'][0]['code'] == 'engine_not_found'
    message = envelope['issues'][0]['message']
    assert 'needs a local GPU or --placement runpod' in message
    assert ('Edge TTS' in message) == (mode == 'tts')
    if mode == 'tts':
        assert 'generation.generate_speech with provider=edge-tts' in message
    assert envelope['fixes']


@pytest.mark.parametrize('mode', ['tts', 'music'])
def test_installed_engine_refusal_retains_default_context(mode, monkeypatch):
    from astrid.core.generation import engine_local_endpoint
    from astrid.core.generation.engine_contracts import EngineError

    class MissingEngine(EngineError):
        code = 'engine_not_installed'

    def broken(*args, **kwargs):
        raise MissingEngine('missing managed engine')

    monkeypatch.setattr(engine_local_endpoint, 'recorded_engine_root', broken)
    with pytest.raises(preflight.GenerationReadinessError) as caught:
        preflight.select_generation_target('managed', entry_id='preferred', mode=mode)
    envelope = caught.value.to_envelope()
    assert envelope['entry'] == {'id': 'preferred', 'mode': mode, 'engine': 'comfyui'}
    assert envelope['issues'][0]['code'] == 'engine_not_found'
    message = envelope['issues'][0]['message']
    assert 'needs a local GPU or --placement runpod' in message
    assert ('Edge TTS' in message) == (mode == 'tts')
