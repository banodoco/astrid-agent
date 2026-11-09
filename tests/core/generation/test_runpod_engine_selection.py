"""RunPod engine selection and pending target admission."""
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
REFUSALS = {'runpod_engine_unavailable', 'missing_dependency',
            'engine_not_found', 'COMFY_TARGET_NO_MODEL'}
TARGETS = ('auto', 'managed', 'runpod', 'attached')


def _slot_kwargs(modality, mode, image):
    """Minimal public inputs per default slot; a new slot must be listed here."""
    return {('image', 't2i'): dict(prompt='a boat'),
            ('image', 'edit'): dict(mode='edit', prompt='a boat', image_ref=str(image)),
            ('audio', 'tts'): dict(mode='tts', text='hello'),
            ('audio', 'music'): dict(mode='music', prompt='a song'),
            ('audio', 'sfx'): dict(mode='sfx'),
            ('video', 'i2v'): dict(prompt='a boat', image_ref=str(image))}[(modality, mode)]


# Default slots come from the facade's own mapping, plus the video default.
from tests.helpers.generation_defaults import default_entries
SLOT_DEFAULTS = default_entries()
SLOTS = sorted({*SLOT_DEFAULTS, ('video', 'i2v')})


@pytest.fixture
def image(tmp_path):
    path = tmp_path / 'start.png'
    path.write_bytes(b'\x89PNG\r\n\x1a\n')
    return path


@pytest.fixture
def attached_url():
    """A human-attached ComfyUI on loopback, provisioned like the stand-in pod: the default workflows'
    real node schemas and the model files each engine's install receipt lists (no weights on CPU)."""
    from tests.integrations.runpod_slot_pod import PodComfy, pin_object_info, receipt_files
    from tests.integrations.test_runpod_slot_path_cpu import SLOTS as CPU_SLOTS, SlotPod, _attached_comfy, _slot_classes
    classes = set().union(*(_slot_classes(slot) for slot in CPU_SLOTS))
    pod = SlotPod.__new__(SlotPod)  # receipts only: no provider HTTP
    pod.receipts, pod.schema = {}, pin_object_info()
    files = []
    for engine in dict.fromkeys(("comfyui", *entries.runpod_engines())):
        files += receipt_files(pod._receipt_for(engine))
    with _attached_comfy(PodComfy(classes, lambda: files, extra=pin_object_info())) as url:
        yield url


def _target_value(target, request):
    """The request's target: 'attached' is the loopback ComfyUI's address, others pass through."""
    return request.getfixturevalue('attached_url') if target == 'attached' else target


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


# An attached ComfyUI resolves sfx and tts only where their model weights are installed; the CPU
# harness keeps none, so those two attached cells need a GPU host to resolve (readiness refuses with
# missing_model / input_rejected on CPU, as it should).
_NEEDS_WEIGHTS = {(('audio', 'sfx'), 'attached'), (('audio', 'tts'), 'attached')}
_CELLS = [pytest.param(slot, target, id=f"{'/'.join(slot)}-{target}",
                       marks=pytest.mark.gpu if (slot, target) in _NEEDS_WEIGHTS else ())
          for target in TARGETS for slot in SLOTS]


@pytest.mark.parametrize('slot,target', _CELLS)
def test_default_slot_resolves_to_requested_target_or_refuses(slot, target, image, monkeypatch, request):
    modality, mode = slot
    if target != 'runpod':
        monkeypatch.setattr(entries, 'runpod_engine', lambda *a, **k: pytest.fail('RunPod engine chosen'))
        monkeypatch.setattr(generation, '_runpod_placement_policy', lambda *a, **k: pytest.fail('RunPod policy'))
    try:
        plan = _call(modality, mode, image, placement=_target_value(target, request), dry_run=True,
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
    plan = dict(_call(modality, mode, image, placement='runpod', dry_run=True, **PAID))
    client = NoPodClient()
    _call(modality, mode, image, placement='runpod', client=client, project='demo', wait=False, **LIVE)
    assert [capability for capability, _ in client.calls] == ['vibecomfy.run']
    inputs = client.calls[0][1]['inputs']
    policy = json.loads(inputs['placement_policy'])
    assert 'comfy_url' not in inputs  # never a local execution path
    assert (policy['kind'], policy['engine']) == (plan['target']['placement'], plan['target']['engine'])
    assert inputs['generation_intent']['metadata']['model'] == plan['entry']['id']


@pytest.mark.parametrize('slot,engine', [(('audio', 'tts'), 'comfyui'), (('audio', 'music'), 'comfyui'),
                                         (('image', 't2i'), 'comfyui'), (('image', 'edit'), 'comfyui')],
                         ids=lambda value: '/'.join(value) if isinstance(value, tuple) else value)
def test_audio_runpod_uses_h3_engine_and_image_keeps_comfyui(slot, engine, image, preferences):
    modality, mode = slot
    plan = dict(_call(modality, mode, image, placement='runpod', dry_run=True, **PAID))
    assert plan['target']['engine'] == engine
    client = NoPodClient()
    _call(modality, mode, image, placement='runpod', client=client, project='demo', wait=False, **LIVE)
    assert json.loads(client.calls[0][1]['inputs']['placement_policy'])['engine'] == engine


def test_runpod_request_never_selects_a_local_target(image, preferences, monkeypatch):
    from astrid.core.generation import preflight
    monkeypatch.setattr(preflight, 'select_generation_target', lambda *a, **k: pytest.fail('local target selected'))
    client = NoPodClient()
    _call('audio', 'music', image, placement='runpod', client=client, project='demo', wait=False, **LIVE)
    assert len(client.calls) == 1


@pytest.mark.parametrize('target', ['auto', 'managed'])
def test_local_request_never_reaches_runpod(target, image, monkeypatch):
    monkeypatch.setattr(entries, 'runpod_engine', lambda *a, **k: pytest.fail('RunPod engine chosen'))
    monkeypatch.setattr(generation, '_runpod_placement_policy', lambda *a, **k: pytest.fail('RunPod policy'))
    client = NoPodClient()
    with pytest.raises(AstridError) as raised:  # no local engine in the test environment
        _call('audio', 'music', image, placement=target, client=client, project='demo', wait=False)
    assert raised.value.code in REFUSALS
    assert client.calls == []


def test_uncovered_entry_reaches_submission(image, preferences):
    client = NoPodClient()
    _call('video', 'i2v', image, placement='runpod', client=client, project='demo', wait=False, **LIVE)
    assert [capability for capability, _ in client.calls] == ['vibecomfy.run']


@pytest.mark.parametrize('pinned', [False, True])
def test_selected_h3_does_not_fall_back_when_required_inputs_missing(image, preferences, monkeypatch, pinned):
    from astrid.core.generation import defaults
    if pinned:
        monkeypatch.setattr(defaults, 'pinned_entry', lambda *args: 'h3')
        monkeypatch.setattr(defaults, 'ordered_ids', lambda *args: ('h3',))
    client = NoPodClient()
    with pytest.raises(Exception, match="unsupported entry inputs|needs the 'request' input"):
        GenerationFacade().video(prompt='a boat', image_ref=str(image), placement='runpod',
                                 client=client, project='demo', wait=False,
                                 **({} if pinned else {'entry': 'h3'}), **LIVE)
    assert client.calls == []


def test_uncovered_entry_selects_recipe():
    entry = entries.find_entry('video/ltx2_3_i2v')
    assert entry is not None
    assert entries.runpod_engine(entry) == 'comfyui'


def test_engine_choice_uses_recipe_order_and_requires_a_recipe(monkeypatch):
    entry = {'id': 'uncovered'}
    monkeypatch.setattr(entries, 'runpod_engines', lambda: ('comfyui', 'other'))
    assert entries.runpod_engine(entry) == 'comfyui'
    monkeypatch.setattr(entries, 'runpod_engines', lambda: ())
    with pytest.raises(AstridError) as raised:
        entries.runpod_engine(entry)
    assert raised.value.code == 'runpod_engine_unavailable'


def test_pending_target_does_not_check_controller_lock():
    entry = {'needs': [{'class_type': 'UNETLoader', 'field': 'unet_name',
                        'value': 'diffusion_models/no_such_model.safetensors'}]}
    assert entries.target_schema(entry, None, None, engine='comfyui') is None


def test_h3_resolves_to_its_engine_through_the_same_helper(preferences, monkeypatch):
    """H3 pins its Ref2V LoRA in its own graph; the comfyui lock supplies the rest."""
    h3 = entries.find_entry('h3')
    assert entries.runpod_engine(h3) == 'comfyui'
    chosen = []
    real = entries.runpod_engine
    monkeypatch.setattr(entries, 'runpod_engine', lambda entry: chosen.append(entry['id']) or real(entry))
    client = NoPodClient()
    GenerationFacade().video(entry='h3', placement='runpod', client=client,
                             inputs={'request': 'r.json', 'asset_map': 'a.json'}, **LIVE)
    assert chosen == ['h3']
    assert [capability for capability, _ in client.calls] == ['h3_av.transform']
    assert client.calls[0][1]['inputs']['placement_policy']['engine'] == 'comfyui'


