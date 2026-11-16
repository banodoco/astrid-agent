"""The guarded appearance seam delegates legacy migration to the package."""
import copy

import pytest

from astrid.sdk.timeline._appearance import normalize_owner, package_query
from astrid.sdk.timeline._recording import Recording
from tests.sdk.test_timeline_sync import initial_bundle


def test_normalize_owner_records_package_changes_and_dependencies():
    bundle = initial_bundle()
    bundle['parent']['config']['output']['resolution'] = '1280x720'
    for clip in bundle['parent']['clips']:
        clip['clipType'] = 'media'
    bundle['parent']['clips'][0]['x'] = 20
    bundle['parent']['clips'][1]['cropTop'] = .1
    original = copy.deepcopy(bundle)
    ctx = Recording(bundle, {}, 0)
    findings = normalize_owner(ctx, 'parent/clip:a')
    assert any(f['kind'] == 'explicit-fit' for f in findings)
    assert ctx.state.entity('parent/clip:a')['fit'] == 'cover'
    assert ctx.state.entity('parent/clip:b')['fit'] == 'cover'
    assert ctx.state.entity('parent/track:picture')['app']['clipPlacementVersion'] == 1
    assert {r['key'] for r in ctx.record['selections']} >= {'collection.clips', 'collection.tracks'}
    assert any(r['ref'] == 'parent' and r['path'] == ['config', 'output'] for r in ctx.record['reads'])
    assert original['parent']['clips'][0].get('fit') is None


def test_package_placement_provenance_and_missing_canvas():
    from astrid.sdk.timeline._appearance import canvas
    result = package_query({'operation': 'resolve', 'input': {
        'clip': {'id': 'a', 'fit': 'cover'}, 'track': {'id': 'v', 'fit': 'contain'},
        'canvas': {'width': 1280, 'height': 720}, 'intrinsic': {'width': 1920, 'height': 800}}})
    assert result['provenance']['fit'] == 'clip'
    assert result['local']['media']['x'] == -224
    with pytest.raises(ValueError, match='output.resolution'):
        canvas({}, 'clip a')


def test_revision_closure_normalizes_child_with_own_canvas_without_mutation():
    from astrid.sdk.timeline._appearance import normalize_revision_closure

    config = {'output': {'resolution': '1280x720'}, 'tracks': [{'id': 'v', 'kind': 'visual'}],
              'clips': [{'id': 'child', 'track': 'v', 'clipType': 'media', 'x': 0}]}
    parent = {'payload': {'config': {**copy.deepcopy(config), 'clips': []}, 'clips': []}}
    child = {'revision_id': 'r', 'payload': copy.deepcopy(config)}
    original = copy.deepcopy(child)
    _, children, findings = normalize_revision_closure(parent, [child])
    assert children[0]['payload']['clips'][0]['fit'] == 'cover'
    assert any(item['owner'] == 'r' for item in findings)
    assert child == original
    child['payload']['output']['resolution'] = '1920x1080'
    _, children, _ = normalize_revision_closure(parent, [child])
    assert children[0]['payload']['clips'][0]['x'] == 0
    assert children[0]['payload']['clips'][0]['fit'] == 'cover'


def test_legacy_expansion_refuses_lost_child_track_context():
    from astrid.core.timeline.expand_shots import expand_shot_clips, ShotExpansionError

    base = {'output': {'resolution': '1280x720'}, 'tracks': [{'id': 'v', 'kind': 'visual'}]}
    parent = {**copy.deepcopy(base), 'clips': [{'id': 'shot', 'clipType': 'shot', 'track': 'v',
              'at': 0, 'hold': 1, 'params': {'shot_id': 's', 'timeline_document_id': 'd'}}]}
    child = {**copy.deepcopy(base), 'clips': [{'id': 'a', 'clipType': 'media', 'track': 'v', 'at': 0, 'hold': 1}]}
    with pytest.raises(ShotExpansionError, match='shot.*a.*track'):
        expand_shot_clips(parent, {'assets': {}}, load_timeline=lambda _: (child, {'assets': {}}))


def test_child_canvas_normalization_then_parent_canvas_placement():
    from astrid.sdk.timeline._appearance import normalize_revision_closure

    parent = {'payload': {'config': {'output': {'resolution': '1280x720'}, 'tracks': [], 'clips': []}, 'clips': []}}
    child = {'revision_id': 'r', 'payload': {'output': {'resolution': '1920x1080'},
        'tracks': [{'id': 'v', 'kind': 'visual', 'scale': 2}],
        'clips': [{'id': 'positioned', 'track': 'v', 'clipType': 'media', 'x': 0},
                  {'id': 'plain', 'track': 'v', 'clipType': 'media'}]}}
    _, children, _ = normalize_revision_closure(parent, [child])
    track = children[0]['payload']['tracks'][0]
    positioned, plain = children[0]['payload']['clips']
    # Inverse scale bake is centered on the child canvas (960, 540).
    assert positioned['x'] == 480 and positioned['width'] == 960
    request = {'clip': plain, 'track': {**track, 'scale': 1}, 'canvas': {'width': 1280, 'height': 720},
               'intrinsic': {'width': 1920, 'height': 800}}
    contain = package_query({'operation': 'resolve', 'input': request})
    assert contain['local']['full'] == {'x': 0, 'y': 0, 'width': 1280, 'height': 720}
    assert contain['local']['media']['width'] == 1280
    assert contain['local']['media']['height'] == pytest.approx(533.3333333333334)
    request['clip'] = {**plain, 'fit': 'cover'}
    fill = package_query({'operation': 'resolve', 'input': request})
    assert fill['local']['media'] == {'x': -224, 'y': 0, 'width': 1728, 'height': 720}
