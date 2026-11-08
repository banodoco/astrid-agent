"""M-02: one RunPod engine choice, derived from the selected entry's models.

Each default slot asked for a target either resolves to that target (and, on
RunPod, to the engine whose pinned lock covers the entry) or is an explicit,
coded refusal before any pod or task exists.
"""
from __future__ import annotations

import hashlib
import json
from types import SimpleNamespace

import pytest

import astrid.sdk.generation as generation
from astrid.core.contracts.errors import AstridError
from astrid.core.generation import entries
from astrid.sdk.generation import GenerationFacade
from astrid.sdk.results import InvocationResult

PAID = dict(consent=('paid',), max_usd=2.0, deadline='2099-01-01T00:00:00Z')
LIVE = dict(PAID, offer_digest='sha256:' + 'b' * 64, preparation_seconds=60,
            execution_seconds=60, idle_seconds=1800)
# Explicit refusals a target route may answer with instead of resolving.
REFUSALS = {'runpod_engine_unavailable', 'foley_runpod_unsupported', 'missing_dependency',
            'engine_not_found', 'COMFY_TARGET_NO_MODEL', 'video_entry_required'}
TARGETS = ('auto', 'managed', 'runpod')


def _slot_kwargs(modality, mode, image):
    """Minimal public inputs per default slot; a new slot must be listed here."""
    return {('image', 't2i'): dict(prompt='a boat'),
            ('image', 'edit'): dict(mode='edit', prompt='a boat', image_ref=str(image)),
            ('audio', 'tts'): dict(mode='tts', text='hello'),
            ('audio', 'music'): dict(mode='music', prompt='a song'),
            ('audio', 'sfx'): dict(mode='sfx'),
            ('video', 'i2v'): dict(prompt='a boat', image_ref=str(image))}[(modality, mode)]


# Default slots come from the facade's own mapping, plus the video default.
SLOTS = sorted({*generation._DEFAULT_ENTRIES, ('video', 'i2v')})


@pytest.fixture
def image(tmp_path):
    path = tmp_path / 'start.png'
    path.write_bytes(b'\x89PNG\r\n\x1a\n')
    return path


@pytest.fixture
def preferences(monkeypatch):
    from astrid.packs.runpod import target_selection
    saved = target_selection.RunPodPreferences.from_mapping({
        'provider_account_ref': 'account-a', 'gpu_types': ['gpu-a'],
        'image': 'worker@sha256:' + 'a' * 64, 'idle_timeout_seconds': 1800})
    monkeypatch.setattr(target_selection, 'read_runpod_preferences', lambda: saved)


class NoPodClient:
    """Admission fake: records tasks; touching the RunPod owner fails the test."""

    def __init__(self):
        self.calls = []

    @property
    def runpod_generation_owner(self):
        pytest.fail('RunPod owner (pod creation) reached from the facade')

    @property
    def media(self):
        def import_file(*, project, path):
            digest = 'sha256:' + hashlib.sha256(path.read_bytes()).hexdigest()
            return SimpleNamespace(ok=True, data={'object_id': digest, 'filename': path.name})
        return SimpleNamespace(import_file=import_file)

    def invoke_result(self, capability, **kwargs):
        self.calls.append((capability, kwargs))
        return InvocationResult(capability, 'executor', 'command', True,
                                kernel_task_id='task-a', raw_result={'state': 'queued'})


def _expected_engine(entry_id, mode):
    entry = entries.find_entry(entry_id)
    if entry is None:  # a catalog id: its local template owns the models
        from astrid.core.model_catalog.registry import ModelRegistry
        template = ModelRegistry.load_default().get(entry_id).modes[mode].backends['local'].template
        entry = entries.find_entry(template)
    return entries.runpod_engine(entry)


def _call(modality, mode, image, **kwargs):
    return getattr(GenerationFacade(), modality)(**_slot_kwargs(modality, mode, image), **kwargs)


@pytest.mark.parametrize('target', TARGETS)
@pytest.mark.parametrize('slot', SLOTS, ids=lambda slot: '/'.join(slot))
def test_default_slot_resolves_to_requested_target_or_refuses(slot, target, image, monkeypatch):
    modality, mode = slot
    if target != 'runpod':
        monkeypatch.setattr(entries, 'runpod_engine', lambda *a, **k: pytest.fail('RunPod engine chosen'))
        monkeypatch.setattr(generation, '_runpod_target_policy', lambda *a, **k: pytest.fail('RunPod policy'))
    try:
        plan = _call(modality, mode, image, target=target, dry_run=True,
                     **(PAID if target == 'runpod' else {}))
    except AstridError as exc:
        assert exc.code in REFUSALS, (slot, target, exc.code, str(exc))
        return
    resolved = dict(plan)['target']
    if target == 'runpod':
        assert resolved['placement'] == 'runpod'
        assert resolved['engine'] == _expected_engine(dict(plan)['entry']['id'], mode)
    else:
        assert resolved.get('placement') != 'runpod'


@pytest.mark.parametrize('slot', [s for s in SLOTS if s[1] in {'t2i', 'edit', 'tts', 'music'}],
                         ids=lambda slot: '/'.join(slot))
def test_dry_run_target_and_engine_equal_live_policy(slot, image, preferences):
    modality, mode = slot
    plan = dict(_call(modality, mode, image, target='runpod', dry_run=True, **PAID))
    client = NoPodClient()
    _call(modality, mode, image, target='runpod', client=client, project='demo', wait=False, **LIVE)
    assert [capability for capability, _ in client.calls] == ['vibecomfy.run']
    inputs = client.calls[0][1]['inputs']
    policy = json.loads(inputs['target_policy'])
    assert 'comfy_url' not in inputs  # never a local execution path
    assert (policy['kind'], policy['engine']) == (plan['target']['placement'], plan['target']['engine'])
    assert inputs['generation_intent']['metadata']['model'] == plan['entry']['id']


@pytest.mark.parametrize('slot,engine', [(('audio', 'tts'), 'vibecomfy-h3'), (('audio', 'music'), 'vibecomfy-h3'),
                                         (('image', 't2i'), 'comfyui'), (('image', 'edit'), 'comfyui')],
                         ids=lambda value: '/'.join(value) if isinstance(value, tuple) else value)
def test_audio_runpod_uses_h3_engine_and_image_keeps_comfyui(slot, engine, image, preferences):
    modality, mode = slot
    plan = dict(_call(modality, mode, image, target='runpod', dry_run=True, **PAID))
    assert plan['target']['engine'] == engine
    client = NoPodClient()
    _call(modality, mode, image, target='runpod', client=client, project='demo', wait=False, **LIVE)
    assert json.loads(client.calls[0][1]['inputs']['target_policy'])['engine'] == engine


def test_runpod_request_never_selects_a_local_target(image, preferences, monkeypatch):
    from astrid.core.generation import preflight
    monkeypatch.setattr(preflight, 'select_generation_target', lambda *a, **k: pytest.fail('local target selected'))
    client = NoPodClient()
    _call('audio', 'music', image, target='runpod', client=client, project='demo', wait=False, **LIVE)
    assert len(client.calls) == 1


@pytest.mark.parametrize('target', ['auto', 'managed'])
def test_local_request_never_reaches_runpod(target, image, monkeypatch):
    monkeypatch.setattr(entries, 'runpod_engine', lambda *a, **k: pytest.fail('RunPod engine chosen'))
    monkeypatch.setattr(generation, '_runpod_target_policy', lambda *a, **k: pytest.fail('RunPod policy'))
    client = NoPodClient()
    with pytest.raises(AstridError) as raised:  # no local engine in the test environment
        _call('audio', 'music', image, target=target, client=client, project='demo', wait=False)
    assert raised.value.code in REFUSALS
    assert client.calls == []


def test_uncovered_entry_refuses_before_any_task(image, preferences):
    """ltx-2.3 models are in no RunPod lock: refuse, admit nothing, create no pod."""
    client = NoPodClient()
    with pytest.raises(AstridError) as raised:
        _call('video', 'i2v', image, entry='ltx-2.3', target='runpod', client=client, project='demo', wait=False, **LIVE)
    assert raised.value.code in REFUSALS and 'No RunPod engine lock covers' in str(raised.value)
    assert client.calls == []


def test_engine_helper_reads_the_pinned_locks():
    tts, music = entries.find_entry('audio/qwen3_tts_custom_voice'), entries.find_entry('audio/minimax_music_3')
    assert entries._missing_locked_models(tts, 'comfyui') and not entries._missing_locked_models(tts, 'vibecomfy-h3')
    assert entries.runpod_engine(music) == 'vibecomfy-h3'
    assert entries.runpod_engine(entries.find_entry('image/qwen_image_2_1_turbo')) == 'comfyui'
    with pytest.raises(AstridError) as raised:
        entries.runpod_engine(None)
    assert raised.value.code == 'runpod_engine_unavailable'


def test_dry_run_without_client_checks_the_engine_lock():
    music = entries.find_entry('audio/minimax_music_3')
    assert entries.target_schema(music, None, None, engine='vibecomfy-h3') is None
    with pytest.raises(AstridError) as raised:
        entries.target_schema(music, None, None, engine='comfyui')
    assert raised.value.code == 'missing_model'


def test_h3_resolves_to_its_engine_through_the_same_helper(preferences, monkeypatch):
    """H3 pins its Ref2V LoRA in its own graph; the vibecomfy-h3 lock supplies the rest."""
    h3 = entries.find_entry('h3')
    assert entries.runpod_engine(h3) == 'vibecomfy-h3'
    assert entries._lock_coverage(h3, 'vibecomfy-h3')[1] > entries._lock_coverage(h3, 'comfyui')[1]
    chosen = []
    real = entries.runpod_engine
    monkeypatch.setattr(entries, 'runpod_engine', lambda entry: chosen.append(entry['id']) or real(entry))
    client = NoPodClient()
    GenerationFacade().video(entry='h3', target='runpod', client=client,
                             inputs={'request': 'r.json', 'asset_map': 'a.json'}, **LIVE)
    assert chosen == ['h3']
    assert [capability for capability, _ in client.calls] == ['h3_av.transform']
    assert client.calls[0][1]['inputs']['target_policy']['engine'] == 'vibecomfy-h3'
