"""Imported media metadata survives occurrence media operations."""
import pytest

from astrid.sdk.timeline._command_state import State
from astrid.sdk.timeline.commands import execute
from astrid.sdk.timeline.commit import bundle_from_document
from astrid.sdk.timeline.native_occurrence_media_commands import capture_occurrence_swap_media
from tests.editing_conformance.test_recipe_occurrence_media import OWNER, TARGET, inputs
from tests.sdk.test_timeline_commit_provider import runtime_abi


@pytest.fixture(autouse=True)
def _runtime_abi(monkeypatch, request):
    if "realm" not in request.fixturenames:
        runtime_abi.__wrapped__(monkeypatch)


def test_imported_still_swap_records_resolution():
    document, old, new, verified = inputs()
    verified[-1].update(width=1920, height=1080)
    bundle = bundle_from_document(document)
    command = capture_occurrence_swap_media(bundle, TARGET, old, new, verified_media=verified)
    plan = execute(bundle, [command], verified_media=verified)
    assert State(plan.bundle).registry(OWNER)['assets'][new['asset']]['resolution'] == '1920x1080'


@pytest.mark.parametrize('kind', ['image', 'video'])
@pytest.mark.parametrize('operation', ['add', 'swap', 'same-alias', 'existing'])
def test_visual_metadata_for_media_bindings(kind, operation):
    from astrid.sdk.timeline.native_occurrence_media_commands import capture_occurrence_add_media

    document, old, new, verified = inputs(kind)
    verified[-1].update(width=640, height=360)
    bundle = bundle_from_document(document)
    assets = State(bundle).registry(OWNER)['assets']
    if operation == 'same-alias':
        new['asset'] = old['asset']
        verified[-1]['selection']['asset'] = old['asset']
        assets[old['asset']]['resolution'] = '100x100'
        if kind == 'video':
            for clip in State(bundle).config(OWNER)['clips']:
                if clip.get('asset') == old['asset'] and 'from' not in clip:
                    clip.update({'from': 0, 'to': clip['hold']})
    if operation == 'existing':
        assets[new['asset']] = {'media_id': new['object_id'], 'type': kind,
                                'generationId': new['generation_id'], 'variantId': new['variant_id']}
    if operation == 'add':
        row = {'id': 'new-child', 'clipType': kind, 'asset': new['asset'], 'track': 'picture',
               'at': 0, 'hold': 4 if kind == 'video' else 1,
               **({'from': 0, 'to': 4, 'speed': 1} if kind == 'video' else {})}
        command = capture_occurrence_add_media(bundle, OWNER, row, new, verified_media=verified)
    else:
        command = capture_occurrence_swap_media(bundle, TARGET, old, new, verified_media=verified)
    result = execute(bundle, [command], verified_media=verified)
    entry = State(result.bundle).registry(OWNER)['assets'][new['asset']]
    assert entry['resolution'] == '640x360'
    if kind == 'video':
        assert entry['duration'] == 6


@pytest.mark.parametrize('dimensions', [{}, {'width': 640}, {'width': 0, 'height': 360},
                                       {'width': True, 'height': 360}])
def test_same_alias_discards_stale_resolution_when_dimensions_unavailable(dimensions):
    document, old, new, verified = inputs()
    new['asset'] = old['asset']
    verified[-1]['selection']['asset'] = old['asset']
    verified[-1].update(dimensions)
    bundle = bundle_from_document(document)
    State(bundle).registry(OWNER)['assets'][old['asset']]['resolution'] = '100x100'
    command = capture_occurrence_swap_media(bundle, TARGET, old, new, verified_media=verified)
    result = execute(bundle, [command], verified_media=verified)
    assert 'resolution' not in State(result.bundle).registry(OWNER)['assets'][old['asset']]

