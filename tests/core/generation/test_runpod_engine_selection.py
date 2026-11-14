"""RunPod engine selection and pending target admission."""
from __future__ import annotations

import hashlib
import json
from contextlib import contextmanager
from types import SimpleNamespace
from pathlib import Path

import pytest

import astrid.sdk.generation as generation
from astrid.core.contracts.errors import AstridError
from astrid.core.generation import entries
from astrid.sdk.generation import GenerationFacade
from astrid.sdk.results import InvocationResult

LIVE_PLACEMENT = {'kind': 'runpod', 'machine': 'pod-a'}
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
    from PIL import Image

    path = tmp_path / 'start.png'
    # H3 planning decodes the reference; use a real still at its output canvas size.
    Image.new('RGB', (1024, 576)).save(path, format='PNG')
    return path


@pytest.fixture
def attached_url():
    """A human-attached ComfyUI on loopback, provisioned like the stand-in pod: the default workflows'
    real node schemas and the model files each engine's install receipt lists (no weights on CPU)."""
    from tests.integrations.runpod_slot_pod import PodComfy, pin_object_info, receipt_files
    from tests.integrations.test_runpod_slot_path_cpu import SLOTS as CPU_SLOTS, SlotPod, _attached_comfy, _slot_classes
    classes = set().union(*(_slot_classes(slot) for slot in CPU_SLOTS))
    pod = SlotPod.__new__(SlotPod)  # receipts only: no provider HTTP
    pod.receipts, pod.schema, pod.volume = {}, pin_object_info(), None  # no network volume: the receipt's models root is the engine's own
    pod.volume = None
    files = []
    for engine in dict.fromkeys(("comfyui", *entries.runpod_engines())):
        files += receipt_files(pod._receipt_for(engine))
    with _attached_comfy(PodComfy(classes, lambda: files, extra=pin_object_info())) as url:
        yield url


def _target_value(target, request):
    """The request's target: 'attached' is the loopback ComfyUI's address, others pass through."""
    return request.getfixturevalue('attached_url') if target == 'attached' else target


@pytest.fixture
def live_machine(monkeypatch):
    from astrid.core.generation import placement
    monkeypatch.setattr(placement, '_runpod_leases', lambda kind: [
        {'machine': 'pod-a', 'id': 'pod-a', 'live': True}])


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


def test_managed_preview_and_live_freeze_declarations_with_installed_alternate(monkeypatch, tmp_path):
    from astrid.core.generation import preflight
    from astrid.core.generation.backends.comfy_target import _resolve_roles
    from astrid.core.generation.engine_local_endpoint import record_engine_root
    from astrid.core.model_catalog.registry import ModelRegistry

    # The target has only the second declared VAE choice. Managed selection
    # must freeze the first declaration for both calls and let the host verify
    # that exact selection after alignment, without consulting this inventory.
    alternate = SimpleNamespace(target=SimpleNamespace(url='http://127.0.0.1:8188'),
        object_info=lambda: {'VAELoader': {'input': {'required': {
            'vae_name': [[r'FLUX1\ae.safetensors']]}}}})
    declaration = ModelRegistry.load_default().get('z-image').modes['t2i'].backends['local'].model_roles['vae']
    resolved, failures = _resolve_roles(SimpleNamespace(model_roles={'vae': declaration}), alternate.object_info())
    assert resolved == {'vae': r'FLUX1\ae.safetensors'} and not failures
    selections = []
    # Keep real managed deferral: the installed client must never be bound
    # while preparing declarations. Admission only needs the registered root.
    record_engine_root('comfyui', str(tmp_path / 'engine'))
    monkeypatch.setattr(preflight, '_installed_managed_target',
                        lambda *args, **kwargs: selections.append(alternate) or alternate)
    graphs = []
    prepare = generation._prepare_catalog_target_workflow
    def capture(**kwargs):
        assert kwargs['target_client'] is None
        result = prepare(**kwargs)
        graphs.append(result[0].compile('api'))
        return result
    monkeypatch.setattr(generation, '_prepare_catalog_target_workflow', capture)
    facade = GenerationFacade()
    args = dict(entry='z-image', prompt='a boat', seed=7, placement='managed')
    dry = facade.image(**args, dry_run=True)
    client = NoPodClient()
    facade.image(**args, client=client, project='demo', wait=False)
    assert dry['status'] == 'ready'
    assert not selections
    assert len(graphs) == 2 and graphs[0] == graphs[1]
    vaes = [node['inputs']['vae_name'] for node in graphs[0].values()
            if node['class_type'] == 'VAELoader']
    assert vaes == ['ae.safetensors']
    assert 'comfy_url' not in client.calls[0][1]['inputs']


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
# Measured 90 s on a hosted run (CI 43348849041); the suite default is 120 s.
@pytest.mark.timeout(300)
def test_default_slot_resolves_to_requested_target_or_refuses(slot, target, image, monkeypatch, request):
    modality, mode = slot
    if target != 'runpod':
        monkeypatch.setattr(entries, 'runpod_engine', lambda *a, **k: pytest.fail('RunPod engine chosen'))
        monkeypatch.setattr(generation, '_runpod_placement_policy', lambda *a, **k: pytest.fail('RunPod policy'))
    try:
        plan = _call(modality, mode, image, placement=_target_value(target, request), dry_run=True)
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
# Measured 108 s on a hosted run (CI 43348849041); the suite default is 120 s.
@pytest.mark.timeout(300)
def test_dry_run_target_and_engine_equal_live_policy(slot, image, live_machine):
    modality, mode = slot
    plan = dict(_call(modality, mode, image, placement='runpod', dry_run=True))
    client = NoPodClient()
    _call(modality, mode, image, placement=LIVE_PLACEMENT, client=client, project='demo', wait=False)
    assert [capability for capability, _ in client.calls] == ['vibecomfy.run']
    inputs = client.calls[0][1]['inputs']
    policy = json.loads(inputs['placement_policy'])
    assert 'comfy_url' not in inputs  # never a local execution path
    assert (policy['kind'], policy['engine']) == (plan['target']['placement'], plan['target']['engine'])
    assert set(policy) <= {'kind', 'engine', 'machine'}
    assert inputs['generation_intent']['metadata']['model'] == plan['entry']['id']


@pytest.mark.parametrize('slot,engine', [(('audio', 'tts'), 'comfyui'), (('audio', 'music'), 'comfyui'),
                                         (('image', 't2i'), 'comfyui'), (('image', 'edit'), 'comfyui')],
                         ids=lambda value: '/'.join(value) if isinstance(value, tuple) else value)
# Measured 109 s on a hosted run (CI 43348849041); the suite default is 120 s.
@pytest.mark.timeout(300)
def test_runpod_engine_matches_slot_recipe(slot, engine, image, live_machine):
    modality, mode = slot
    plan = dict(_call(modality, mode, image, placement='runpod', dry_run=True))
    assert plan['target']['engine'] == engine
    client = NoPodClient()
    _call(modality, mode, image, placement=LIVE_PLACEMENT, client=client, project='demo', wait=False)
    policy = json.loads(client.calls[0][1]['inputs']['placement_policy'])
    assert policy == {'kind': 'runpod', 'engine': engine, 'machine': 'pod-a'}


@pytest.mark.parametrize('message', ['required values were not supplied', 'input contract incomplete'])
def test_automatic_selection_skips_only_typed_missing_entry_inputs(monkeypatch, message):
    from astrid.core.generation import defaults, settings, availability
    from astrid.core.generation import placement as placements

    candidates = [
        {'id': 'a-contributed', 'kind': 'video', 'modes': ['t2v'], 'bundle': 'a.py',
         'hooks_declared': True},
        {'id': 'b-ordinary', 'kind': 'video', 'modes': ['t2v'], 'bundle': 'b.py'},
    ]
    monkeypatch.setattr(settings, 'read', lambda *a, **k: {})
    monkeypatch.setattr(placements, 'resolve_placement', lambda *a, **k: SimpleNamespace(kind='runpod', machine=None))
    monkeypatch.setattr(placements, 'validate_placement', lambda value: value)
    monkeypatch.setattr(defaults, 'pinned_entry', lambda *a, **k: None)
    monkeypatch.setattr(defaults, 'ordered_ids', lambda *a, **k: ())
    monkeypatch.setattr(entries, 'user_entries', lambda: candidates)
    monkeypatch.setattr(entries, 'ready_entries', lambda: [])
    monkeypatch.setattr(availability, 'require_entry_available', lambda *a, **k: None)
    monkeypatch.setattr(entries, 'describe_entry', lambda *a, **k: {'inputs': [{'name': 'request'}]})
    monkeypatch.setattr(entries, 'validate_inputs', lambda *a, **k: (_ for _ in ()).throw(
        entries.MissingRequiredEntryInputsError(message)))
    monkeypatch.setattr(entries, 'runpod_engine', lambda *a, **k: 'comfyui')

    @contextmanager
    def snapshot(path):
        yield Path(path)

    class CandidateReached(Exception):
        pass

    monkeypatch.setattr(generation, '_snapshot_generation_workflow', snapshot)
    monkeypatch.setattr(entries, 'prepare', lambda frozen, **kw: (_ for _ in ()).throw(CandidateReached(str(frozen))))
    with pytest.raises(CandidateReached, match='b.py'):
        GenerationFacade()._generate_on_target_impl(
            _job_dirs=[], registry=SimpleNamespace(list_by_modality=lambda _: []), model=None,
            mode='t2v', modality='video', placement='runpod', execution=None,
            inputs={'prompt': 'boat'}, dry_run=True)


def test_runpod_request_never_selects_a_local_target(image, live_machine, monkeypatch):
    from astrid.core.generation import preflight
    monkeypatch.setattr(preflight, 'select_generation_target', lambda *a, **k: pytest.fail('local target selected'))
    client = NoPodClient()
    _call('audio', 'music', image, placement=LIVE_PLACEMENT, client=client, project='demo', wait=False)
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


# Measured 112 s on a hosted run (CI 43348849041); the suite default is 120 s.
@pytest.mark.timeout(300)
def test_uncovered_entry_reaches_submission(image, live_machine):
    client = NoPodClient()
    _call('video', 'i2v', image, placement=LIVE_PLACEMENT, client=client, project='demo', wait=False)
    assert [capability for capability, _ in client.calls] == ['vibecomfy.run']


@pytest.mark.parametrize('pinned', [False, True])
def test_selected_h3_does_not_fall_back_when_required_inputs_missing(image, live_machine, monkeypatch, pinned):
    from astrid.core.generation import defaults
    if pinned:
        monkeypatch.setattr(defaults, 'pinned_entry', lambda *args: 'h3')
        monkeypatch.setattr(defaults, 'ordered_ids', lambda *args: ('h3',))
    client = NoPodClient()
    # h3 declares the kind's plain inputs (5fe9ed7a0): prompt alone still lacks a reference.
    with pytest.raises(Exception, match='requires a reference|prompt with exactly one of'):
        GenerationFacade().video(prompt='a boat', placement='runpod',
                                 client=client, project='demo', wait=False,
                                 **({} if pinned else {'entry': 'h3'}))
    assert client.calls == []


@pytest.mark.parametrize('pinned', [False, True])
def test_selected_h3_accepts_plain_image_inputs_without_falling_back(image, live_machine, monkeypatch, pinned):
    from astrid.core.generation import defaults
    if pinned:
        monkeypatch.setattr(defaults, 'pinned_entry', lambda *args: 'h3')
        monkeypatch.setattr(defaults, 'ordered_ids', lambda *args: ('h3',))
    chosen = []
    real = entries.runpod_engine
    monkeypatch.setattr(entries, 'runpod_engine', lambda entry: chosen.append(entry['id']) or real(entry))
    client = NoPodClient()
    GenerationFacade().video(prompt='a boat', image_ref=str(image), placement='runpod',
                             client=client, project='demo', wait=False,
                             **({} if pinned else {'entry': 'h3'}))
    assert chosen == ['h3']
    assert [capability for capability, _ in client.calls] == ['vibecomfy.run']


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


def test_h3_resolves_to_its_engine_through_the_same_helper(live_machine, monkeypatch, tmp_path):
    """H3 pins its Ref2V LoRA in its own graph; the comfyui lock supplies the rest."""
    from PIL import Image

    reference = tmp_path / 'look.png'
    Image.new('RGB', (16, 16), (32, 64, 128)).save(reference)
    request, assets = tmp_path / 'r.json', tmp_path / 'a.json'
    request.write_text(json.dumps({'version': 2, 'prompt': 'Animate <Picture 1>.', 'duration': 1,
        'media': [{'id': 'look', 'asset': 'look.png', 'role': 'reference', 'modality': 'image'}],
        'settings': {}}))
    assets.write_text(json.dumps({'look.png': str(reference)}))
    h3 = entries.find_entry('h3')
    assert entries.runpod_engine(h3) == 'comfyui'
    chosen = []
    real = entries.runpod_engine
    monkeypatch.setattr(entries, 'runpod_engine', lambda entry: chosen.append(entry['id']) or real(entry))
    client = NoPodClient()
    GenerationFacade().video(entry='h3', placement=LIVE_PLACEMENT, client=client, project='demo', wait=False,
                             inputs={'request': str(request), 'asset_map': str(assets)})
    assert chosen == ['h3']
    assert [capability for capability, _ in client.calls] == ['vibecomfy.run']
    assert json.loads(client.calls[0][1]['inputs']['placement_policy'])['engine'] == 'comfyui'


