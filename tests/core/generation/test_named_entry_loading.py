"""Named selection and pin validation must not build unrelated ready workflows."""
from __future__ import annotations

import pytest

from astrid.core.contracts.errors import AstridError
from astrid.core.generation import defaults, entries, settings
from astrid.core.model_catalog.registry import ModelRegistry
from astrid.sdk.generation import GenerationFacade

READY = 'image/z_image_turbo'


def _golden_error(case, error):
    """Optional paired-proof artifacts retain the complete typed refusal and hint."""
    import json
    import os
    from pathlib import Path
    destination = os.environ.get('ASTRID_A3_GOLDEN_DIR')
    if destination:
        value = {'type': f'{type(error).__module__}.{type(error).__qualname__}',
                 'message': str(error), 'code': getattr(error, 'code', None)}
        if hasattr(error, 'to_envelope'):
            value['envelope'] = error.to_envelope()
        Path(destination).mkdir(parents=True, exist_ok=True)
        Path(destination, case + '.json').write_text(json.dumps(value, sort_keys=True, indent=2) + '\n')


@pytest.fixture
def counted(monkeypatch):
    from vibecomfy.registry import ready
    entries.ready_entries.cache_clear()
    calls = []
    original = ready.workflow_from_ready

    def load(identifier, **kwargs):
        calls.append(identifier)
        return original(identifier, **kwargs)

    monkeypatch.setattr(ready, 'workflow_from_ready', load)
    yield calls
    entries.ready_entries.cache_clear()


def _snapshot(entry=READY):
    return settings.Snapshot(settings.validate_document({
        'defaults': {'image.t2i': {'pack': 'vibecomfy', 'entry': entry}}}), 1, 'runtime')


def test_attached_named_selection_loads_one_ready_workflow(counted, monkeypatch):
    from astrid.core.generation import availability

    class Selected(Exception):
        pass

    selected = []

    def require(row, mode):
        selected.append(row)
        raise Selected

    monkeypatch.setattr(settings, 'read', lambda *a, **kw: _snapshot())
    monkeypatch.setattr(availability, 'require_entry_available', require)
    with pytest.raises(Selected):
        GenerationFacade()._generate_on_target(registry=ModelRegistry.load_default(),
            model=READY, mode='t2i', modality='image', placement='http://127.0.0.1:8188',
            execution=None, inputs={'prompt': 'a boat'}, dry_run=True)
    print(f'named workflow loads: {len(counted)}; ids: {counted}')
    assert selected[0]['id'] == READY
    assert counted == [READY]


def test_automatic_selection_still_loads_all_ready_workflows(counted, monkeypatch):
    from vibecomfy import resources

    from astrid.core.generation import availability

    class Selected(Exception):
        pass

    def require(*args):
        raise Selected

    monkeypatch.setattr(availability, 'require_model_available', require)
    with pytest.raises(Selected):
        GenerationFacade()._generate_on_target(registry=ModelRegistry.load_default(),
            model=None, mode='t2i', modality='image', placement='http://127.0.0.1:8188',
            execution=None, inputs={'prompt': 'a boat'}, dry_run=True)
    expected = [row['id'] for row in resources.template_index()['templates']]
    assert counted == expected


def test_unrelated_broken_workflow_does_not_refuse_saved_pin(monkeypatch, counted):
    from vibecomfy.errors import ShippedResourceMissingError
    from vibecomfy.registry import ready

    original = ready.workflow_from_ready

    def load(identifier, **kwargs):
        if identifier != READY:
            raise ShippedResourceMissingError('unrelated workflow missing', '/missing/unrelated.py')
        return original(identifier, **kwargs)

    monkeypatch.setattr(ready, 'workflow_from_ready', load)
    assert defaults.pinned_entry('image', 't2i', _snapshot()) == READY
    assert defaults.ordered_ids('image', 't2i', _snapshot())[0] == READY
    assert counted == [READY, READY]


def test_pin_validation_loads_only_its_ready_entry(counted):
    assert defaults.pinned_entry('image', 't2i', _snapshot()) == READY
    assert counted == [READY]


def test_unknown_pin_keeps_full_inventory_hint(monkeypatch):
    pin = {'pack': 'vibecomfy', 'entry': 'not-a-real-entry'}
    with pytest.raises(AstridError) as old:
        defaults.check_pin('image.t2i', pin, defaults.known_entries())
    with pytest.raises(AstridError) as new:
        defaults.pinned_entry('image', 't2i', _snapshot(pin['entry']))
    assert type(new.value) is type(old.value)
    assert str(new.value) == str(old.value)
    assert new.value.code == old.value.code
    assert new.value.to_envelope() == old.value.to_envelope()
    _golden_error('unknown-pin', new.value)


@pytest.mark.parametrize('failure', ['missing', 'metadata', 'unknown'])
def test_own_broken_or_unknown_pin_keeps_refusal(monkeypatch, counted, failure):
    from vibecomfy.errors import ShippedResourceMissingError
    from vibecomfy.registry import ready

    if failure == 'metadata':
        from vibecomfy import resources
        monkeypatch.setattr(resources, 'coverage_manifest', lambda: {'workflows': []})
    original = ready.workflow_from_ready
    identifier = 'not-a-real-entry' if failure == 'unknown' else READY

    def load(name, **kwargs):
        if name == READY:
            if failure == 'missing':
                raise ShippedResourceMissingError('selected workflow missing', '/missing/selected.py')
            raise RuntimeError('selected workflow metadata unavailable')
        return original(name, **kwargs)

    monkeypatch.setattr(ready, 'workflow_from_ready', load)
    snapshot = _snapshot(identifier)
    pin = snapshot.content['defaults']['image.t2i']
    with pytest.raises(AstridError) as old:
        defaults.check_pin('image.t2i', pin, defaults.known_entries())
    entries.ready_entries.cache_clear()
    with pytest.raises(AstridError) as new:
        defaults.pinned_entry('image', 't2i', snapshot)
    assert type(new.value) is type(old.value)
    assert str(new.value) == str(old.value)
    assert new.value.to_envelope() == old.value.to_envelope()
    _golden_error('own-pin-' + failure, new.value)


@pytest.mark.parametrize('api', ['generation', 'defaults'])
def test_full_listing_still_surfaces_broken_workflow(monkeypatch, counted, api):
    from vibecomfy.errors import ShippedResourceMissingError
    from vibecomfy.registry import ready

    from astrid.core.generation import listing

    def fail(*args, **kwargs):
        raise ShippedResourceMissingError('listing workflow missing', '/missing/listing.py')

    monkeypatch.setattr(ready, 'workflow_from_ready', fail)
    with pytest.raises(ShippedResourceMissingError, match='listing workflow missing') as caught:
        (listing.generation_rows if api == 'generation' else defaults.listing)()
    _golden_error('listing-' + api, caught.value)


@pytest.mark.parametrize('identifier, message', [
    ('not-a-real-entry', "unknown generation entry 'not-a-real-entry'"),
    ('a3-unready', "entry 'a3-unready' kind or mode requires review"),
])
def test_named_refusal_keeps_typed_message(monkeypatch, identifier, message):
    from astrid.sdk.generation import GenerationInputError
    if identifier == 'a3-unready':
        monkeypatch.setattr(entries, 'user_entries', lambda: [
            {'id': identifier, 'kind': None, 'modes': [], 'bundle': '/unused'}])
    with pytest.raises(GenerationInputError) as caught:
        GenerationFacade().image(entry=identifier, placement='http://127.0.0.1:8188', dry_run=True)
    assert str(caught.value) == message
    assert caught.value.code == 'input_rejected'
    _golden_error('named-' + identifier, caught.value)


def test_direct_projection_is_identical_to_full_projection(counted):
    direct = entries.find_entry(READY)
    assert counted == [READY]
    full = next(row for row in entries.ready_entries() if row['id'] == READY)
    assert direct == full


def test_h3_named_dry_run_plan(tmp_path, monkeypatch, capsys, counted):
    """CLI --entry uses the existing continuation request and a local ComfyUI stub."""
    import json
    import os
    import re
    from pathlib import Path

    from astrid.core.generation import cli
    from tests.core.generation.test_h3_local_job import REQUEST, _video
    from tests.integrations.runpod_slot_pod import PodComfy, pin_object_info, receipt_files
    from tests.integrations.test_runpod_slot_path_cpu import (
        SlotPod,
        _attached_comfy,
        _h3_built_classes,
    )

    source = _video(tmp_path / 'source.mp4', 3, frames=57)
    request = json.loads(REQUEST.read_text())
    assets = {request['media'][0]['asset']['id']: str(source), 'source': str(source)}
    inputs = tmp_path / 'inputs.json'
    inputs.write_text(json.dumps({'request': str(REQUEST), 'asset_map': assets}))
    schema = pin_object_info()
    classes = set(schema) | _h3_built_classes(REQUEST, assets, tmp_path)
    pod = SlotPod.__new__(SlotPod)
    pod.receipts, pod.schema, pod.volume = {}, schema, None
    files = receipt_files(pod._receipt_for('comfyui'))
    comfy = PodComfy(classes, lambda: files, extra=schema)
    hooks = entries.entry_hooks(entries.find_entry('h3'))
    namespace = hooks['build'].__globals__
    original = namespace['_load_graph']
    graphs = []

    def load_graph(path):
        graphs.append(str(path))
        return original(path)

    monkeypatch.setitem(namespace, '_load_graph', load_graph)
    snapshot = settings.Snapshot(settings.validate_document({
        'defaults': {'video.v2v': {'pack': 'h3_av', 'entry': 'h3'}}}), 1, 'runtime')
    monkeypatch.setattr(settings, 'read', lambda *a, **kw: snapshot)
    counted.clear()  # Only count the invocation, after the target fixture is provisioned.
    entries.ready_entries.cache_clear()
    with _attached_comfy(comfy) as url:
        assert cli.main(['video', '--entry', 'h3', '--placement', url,
                         '--inputs-json', str(inputs), '--dry-run', '--json']) == 0
        result = json.loads(capsys.readouterr().out)
    assert result['status'] == 'ready'
    assert result['entry']['id'] == 'h3'
    assert not comfy.prompts and not comfy.uploads
    destination = os.environ.get('ASTRID_A3_GOLDEN_DIR')
    if destination:
        # Only volatile addresses, checkout paths and scratch job identities differ.
        text = json.dumps(result, sort_keys=True, indent=2)
        text = text.replace(url, '<attached>').replace(str(tmp_path), '<inputs>')
        text = text.replace(str(REQUEST), '<request.v2.json>')
        text = re.sub(r'/[^" ]*/astrid-job-[^/" ]+', '<job>', text)
        text = re.sub(r'("verified_at": )"[^"]+"', r'\1"<observation-time>"', text)
        Path(destination).mkdir(parents=True, exist_ok=True)
        Path(destination, 'h3-plan.json').write_text(text + '\n')
    print(f'H3 CLI workflow loads: {len(counted) + len(graphs)} '
          f'(ready metadata={len(counted)}, selected graphs={len(graphs)})')
    assert len(graphs) == 1
    assert counted == []
