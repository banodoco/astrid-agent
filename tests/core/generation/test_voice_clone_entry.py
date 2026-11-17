"""The voice_clone audio entry: discovery, input validation, compile dry run, and its pinned model rows."""
from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path

import pytest

from astrid.core.generation import cli, engine_recipe, entries

REPO = Path(__file__).resolve().parents[3]
LOCK = REPO / 'astrid/packs/runpod/models.lock.json'
RECIPE = REPO / 'astrid/packs/runpod/recipe.linux-cu130.json'
ENGINE_MANIFEST = REPO / 'astrid/packs/vibecomfy/extensions/generation.engine/comfyui.yaml'
PINS = REPO / 'astrid/packs/runpod/pins.json'
BASE_REPO = 'Qwen/Qwen3-TTS-12Hz-1.7B-Base'
BASE_REVISION = 'fd4b254389122332181a7c3db7f27e918eec64e3'
BASE_DIR = 'TTS/Qwen3-TTS/Qwen3-TTS-12Hz-1.7B-Base'
HEX64 = re.compile(r'[0-9a-f]{64}')

VALID = {
    'reference_audio': '/nonexistent/reference.wav',
    'reference_text': 'This is the reference.',
    'target_text': 'Hello there, this is the cloned voice.',
}


def _lock_rows():
    return [m for m in json.loads(LOCK.read_text(encoding='utf-8'))['models'] if m['repository'] == BASE_REPO]


def _entry():
    row = entries.find_entry('voice_clone')
    assert row is not None, 'voice_clone entry is not discovered'
    return row


# Entry discovery

def test_voice_clone_is_a_contributed_audio_entry_with_the_tts_mode():
    row = _entry()
    assert row['kind'] == 'audio'
    assert row['modes'] == ['tts']
    assert row['contribution'] == 'generation.voice_clone'
    assert 'tts' in cli._KIND_SPEC['audio']['audio_modes']
    assert any(item['id'] == 'voice_clone' for item in entries.contributed_entries())


def test_describe_lists_the_declared_public_inputs_for_tts():
    description = entries.describe_entry('voice_clone', mode='tts')
    assert description['kind'] == 'audio' and description['mode'] == 'tts'
    assert [item['name'] for item in description['inputs']] == [
        'reference_audio', 'reference_text', 'target_text', 'language', 'seed']
    by_name = {item['name']: item for item in description['inputs']}
    assert by_name['language']['default'] == 'English'
    assert by_name['seed']['default'] == 125


def test_describe_refuses_a_mode_the_entry_does_not_declare():
    with pytest.raises(ValueError, match='does not support mode'):
        entries.describe_entry('voice_clone', mode='music')


# Input validation

def test_missing_reference_text_is_refused_with_its_name():
    values = {key: value for key, value in VALID.items() if key != 'reference_text'}
    with pytest.raises(entries.MissingRequiredEntryInputsError, match='reference_text'):
        entries.resolve_entry_inputs('voice_clone', values, mode='tts', kind='audio')


def test_missing_target_text_is_refused_with_its_name():
    values = {key: value for key, value in VALID.items() if key != 'target_text'}
    with pytest.raises(entries.MissingRequiredEntryInputsError, match='target_text'):
        entries.resolve_entry_inputs('voice_clone', values, mode='tts', kind='audio')


def test_language_outside_the_declared_choices_is_refused():
    with pytest.raises(ValueError, match="language' must be one of"):
        entries.resolve_entry_inputs('voice_clone', {**VALID, 'language': 'Klingon'}, mode='tts', kind='audio')


def test_seed_must_be_an_integer():
    with pytest.raises(ValueError, match="'seed' requires an integer"):
        entries.resolve_entry_inputs('voice_clone', {**VALID, 'seed': 'x'}, mode='tts', kind='audio')


def test_model_size_is_not_a_caller_input():
    # The 1.7B Base checkpoint is the only one pinned; a caller cannot pick an unpinned variant.
    with pytest.raises(ValueError, match='unsupported entry inputs: model_size'):
        entries.resolve_entry_inputs('voice_clone', {**VALID, 'model_size': '0.6B'}, mode='tts', kind='audio')


def test_valid_inputs_resolve_unchanged():
    _, values = entries.resolve_entry_inputs('voice_clone', VALID, mode='tts', kind='audio')
    assert values == VALID


# Compile dry run (no target, no run): the bundle compiles to the bound graph

def test_bundle_compiles_to_the_bound_clone_graph_without_a_target():
    row = _entry()
    workflow, plan, assets = entries.prepare(entries.bundle_path(row), target_client=None, mode='tts',
        binds=row.get('binds'), defaults=row.get('defaults'),
        values={**VALID, 'seed': 7, 'language': 'English'})
    assert plan['unbound'] == []
    assert set(assets) == {'reference_audio'}
    assert sorted((node_id, node.class_type) for node_id, node in workflow.nodes.items()) == [
        ('1', 'LoadAudio'), ('2', 'AILab_Qwen3TTSVoiceClone'), ('3', 'SaveAudioMP3')]
    clone = workflow.nodes['2'].inputs
    assert clone['model_size'] == '1.7B'
    assert clone['reference_text'] == VALID['reference_text']
    assert clone['target_text'] == VALID['target_text']
    assert clone['seed'] == 7
    assert clone['language'] == 'English'
    assert [(output.node_id, output.output_type) for output in workflow.outputs] == [('3', 'SaveAudioMP3')]


def test_bundle_declares_the_pinned_base_models_and_node_pack():
    row = _entry()
    workflow = entries._load_entry_workflow(row)
    assert workflow.metadata['custom_node_packs']['ComfyUI-QwenTTS']['classes_used'] == ['AILab_Qwen3TTSVoiceClone']
    models = workflow.requirements.models
    assert len(models) == len(_lock_rows()) == 11
    lock_by_destination = {m['destination']: m for m in _lock_rows()}
    for model in models:
        destination = f"{model['subdir']}/{model['name']}"
        pinned = lock_by_destination[destination]
        assert model['sha256'] == pinned['sha256']
        assert model['size_bytes'] == pinned['bytes']
        assert model['url'] == f"https://huggingface.co/{BASE_REPO}/resolve/{BASE_REVISION}/{pinned['path']}"


# Pins: lock rows, node pack, and the recipe digests that name them

def test_base_lock_rows_are_pinned_by_revision_size_and_sha256():
    rows = _lock_rows()
    assert {row['revision'] for row in rows} == {BASE_REVISION}
    for row in rows:
        assert row['url'] is None
        assert row['destination'] == f"{BASE_DIR}/{row['path']}"
        assert not row['path'].startswith(BASE_DIR)
        assert isinstance(row['bytes'], int) and row['bytes'] > 0
        assert HEX64.fullmatch(row['sha256']), row['name']
    main = next(row for row in rows if row['path'] == 'model.safetensors')
    assert (main['bytes'], main['sha256']) == (
        3857413744, '38fc7fc51c5e776e840414b6fd443962e9411b9654888fd7913e4da643cb857c')


def test_node_pack_pin_matches_the_recipe_pin():
    pins = json.loads(PINS.read_text(encoding='utf-8'))['sources']['comfyui-qwentts']
    assert pins['revision'] == _entry_pack_commit()


def _entry_pack_commit():
    return entries._load_entry_workflow(_entry()).metadata['custom_node_packs']['ComfyUI-QwenTTS']['commit']


def test_recipe_and_engine_manifest_digests_match_the_packaged_files():
    lock_digest = hashlib.sha256(LOCK.read_bytes()).hexdigest()
    recipe_digest = hashlib.sha256(RECIPE.read_bytes()).hexdigest()
    recipe = json.loads(RECIPE.read_text(encoding='utf-8'))
    assert recipe['models_lock']['sha256'] == lock_digest
    assert f'sha256: "{recipe_digest}"' in ENGINE_MANIFEST.read_text(encoding='utf-8')


def test_engine_recipe_loads_the_base_rows_into_the_linux_lock():
    lock = engine_recipe.load_recipe_model_lock('comfyui', 'linux-x86_64-cu130')
    destinations = {model.destination for model in lock.models}
    assert all(f"{BASE_DIR}/{row['path']}" in destinations for row in _lock_rows())


# Audio input given as a project media id (materialized through the Runtime, digest-checked)

def test_media_id_reference_is_materialized_and_digest_checked(tmp_path, monkeypatch):
    import contextlib
    from types import SimpleNamespace
    from astrid.core.generation import cli as generation_cli
    from astrid.sdk import media_handles

    raw = b'RIFF-fake-audio-bytes'
    digest = 'sha256:' + hashlib.sha256(raw).hexdigest()

    class FakeClient:
        media = SimpleNamespace(read_bytes=lambda d: raw if d == digest else b'')

    @contextlib.contextmanager
    def fake_open_client():
        yield FakeClient()

    monkeypatch.setattr(generation_cli, '_open_client', fake_open_client)
    monkeypatch.setattr(media_handles, 'resolve_media_handle',
                        lambda client, project, ref: ({'digest': digest, 'filename': 'speaker.wav'}, None))
    parsed = SimpleNamespace(project='proj')
    with contextlib.ExitStack() as stack:
        path, got = generation_cli._media_ref_file(parsed, 'media-1', stack)
        assert got == digest
        assert Path(path).name == 'speaker.wav' and Path(path).read_bytes() == raw
    assert not Path(path).exists()  # the temporary file lives only for the ExitStack


def test_media_id_reference_without_project_is_refused(monkeypatch):
    import contextlib
    from types import SimpleNamespace
    from astrid.core.generation import cli as generation_cli

    with contextlib.ExitStack() as stack, pytest.raises(ValueError, match='needs --project'):
        generation_cli._media_ref_file(SimpleNamespace(project=None), 'media-1', stack)


def test_media_bytes_that_do_not_match_their_digest_are_refused(monkeypatch):
    import contextlib
    from types import SimpleNamespace
    from astrid.core.generation import cli as generation_cli
    from astrid.sdk import media_handles

    digest = 'sha256:' + hashlib.sha256(b'expected').hexdigest()

    class FakeClient:
        media = SimpleNamespace(read_bytes=lambda d: b'tampered')

    @contextlib.contextmanager
    def fake_open_client():
        yield FakeClient()

    monkeypatch.setattr(generation_cli, '_open_client', fake_open_client)
    monkeypatch.setattr(media_handles, 'resolve_media_handle',
                        lambda client, project, ref: ({'digest': digest}, None))
    with contextlib.ExitStack() as stack, pytest.raises(ValueError, match='do not match their digest'):
        generation_cli._media_ref_file(SimpleNamespace(project='proj'), 'media-1', stack)
