"""Registry metadata comes from the exact verified media selection."""
import copy

import pytest

from astrid.sdk.timeline._command_state import State
from astrid.sdk.timeline.commands import execute
from astrid.sdk.timeline.commit import bundle_from_document
from astrid.sdk.timeline.native_occurrence_media_commands import _binding, capture_occurrence_add_media
from tests.editing_conformance.test_recipe_occurrence_media import OWNER, inputs
from tests.sdk.test_timeline_commit_provider import runtime_abi  # noqa: F401


@pytest.mark.parametrize('kind', ['image', 'video'])
def test_created_asset_uses_exact_verified_dimensions(kind, runtime_abi):
    document, old, new, verified = inputs(kind)
    verified[0].update(width=10, height=20)
    verified[1].update(width=1920, height=1080)
    bundle = bundle_from_document(document)
    row = {'id': 'reserved-child', 'clipType': kind, 'asset': new['asset'],
           'track': 'picture', 'at': .125, 'hold': 4 if kind == 'video' else 1.25,
           **({'from': 2, 'to': 6, 'speed': 2} if kind == 'video' else {})}
    command = capture_occurrence_add_media(bundle, OWNER, row, new, verified_media=verified)
    plan = execute(bundle, [command], verified_media=verified)
    entry = State(plan.bundle).registry(OWNER)['assets'][new['asset']]
    assert entry['resolution'] == '1920x1080'
    if kind == 'video':
        assert entry['duration'] == 6


def test_audio_duration_is_preserved_without_visual_resolution():
    _, _, pin, _ = inputs()
    entry = _binding(pin, 'audio', verified={'duration_seconds': 3.5, 'width': 20, 'height': 10})
    assert entry['duration'] == 3.5
    assert 'resolution' not in entry


@pytest.mark.parametrize('dimension', ['width', 'height'])
@pytest.mark.parametrize('value', [None, True, 0, -1, 1.5, '10'])
def test_invalid_verified_dimensions_do_not_create_resolution(dimension, value):
    _, _, pin, _ = inputs()
    verified = {'width': 10, 'height': 20, 'duration_seconds': 3.5}
    verified[dimension] = value
    for kind in ('image', 'video'):
        entry = _binding(pin, kind, verified=copy.deepcopy(verified))
        assert 'resolution' not in entry
        if kind == 'video':
            assert entry['duration'] == 3.5
