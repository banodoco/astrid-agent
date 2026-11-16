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


def test_strict_timeline_validation_preserves_marker_and_rejects_future():
    from astrid.core.timeline.validators.timeline import validate_timeline

    config = {'output': {'resolution': '1280x720', 'fps': 30, 'file': 'out.mp4'},
              'tracks': [{'id': 'v', 'kind': 'visual', 'label': 'V',
                          'app': {'clipPlacementVersion': 1, 'scaleAppliesToPositionedClips': True}}],
              'clips': []}
    original = copy.deepcopy(config)
    validate_timeline(config)
    assert config == original
    config['tracks'][0]['app']['clipPlacementVersion'] = 2
    with pytest.raises(ValueError):
        validate_timeline(config)


def test_repeated_child_mixed_markers_survive_track_projection():
    from astrid.core.timeline.shot_composition_projection import _build_track_group
    from astrid.sdk.timeline._appearance import normalize_revision_closure

    parent = {'payload': {'config': {'output': {'resolution': '1280x720'}, 'tracks': [], 'clips': []}, 'clips': []}}
    child = {'revision_id': 'r', 'payload': {'output': {'resolution': '1920x1080'},
        'tracks': [{'id': 'v', 'kind': 'visual', 'label': 'V'},
                   {'id': 'w', 'kind': 'visual', 'label': 'W',
                    'app': {'clipPlacementVersion': 1, 'scaleAppliesToPositionedClips': True}}],
        'clips': [{'id': 'a', 'track': 'v', 'clipType': 'media', 'x': 0},
                  {'id': 'b', 'track': 'w', 'clipType': 'media', 'x': 0}]}}
    _, children, findings = normalize_revision_closure(parent, [child])
    source = children[0]['payload']
    assert source['clips'][0]['fit'] == 'cover' and 'fit' not in source['clips'][1]
    assert any(item.get('clipId') == 'a' for item in findings)
    projected = {'tracks': [{'id': 'anchor', 'kind': 'visual'}]}
    for occurrence in ('first', 'second'):
        _build_track_group(projected, source, {'occurrence_id': occurrence, 'track': 'anchor'}, [])
    projected_tracks = [t for t in projected['tracks'] if t['id'] != 'anchor']
    assert {t['id'] for t in projected_tracks} == {'first~v', 'first~w', 'second~v', 'second~w'}
    assert all(t['app']['clipPlacementVersion'] == 1 for t in projected_tracks)


def test_nested_shot_refusal_names_clip_and_property():
    from astrid.core.timeline.shot_composition_projection import _clip_type, ShotCompositionProjectionError
    with pytest.raises(ShotCompositionProjectionError, match="clip 'nested'.*property clipType"):
        _clip_type({'id': 'nested', 'clipType': 'shot'})


def test_canvas_inverse_requires_authenticated_materialization_binding(monkeypatch):
    from types import SimpleNamespace
    from astrid.sdk.timeline.validation import _authenticated_initialization_fps_removals
    from tests.sdk.test_timeline_sync import initial_bundle
    from astrid.sdk.timeline import _step_undo

    baseline = initial_bundle()
    baseline['parent']['config']['output'] = {'resolution': '1280x720', 'fps': 24, 'file': 'output.mp4'}
    baseline['parent']['config']['theme_overrides'] = {'visual': {'canvas': {'width': 1280, 'height': 720, 'fps': 24}}}
    candidate = copy.deepcopy(baseline)
    candidate['parent']['config'].pop('output')
    output = baseline['parent']['config']['output']
    source = {'command': {'op': 'clip.set'}, 'writes': [{'ref': 'parent', 'path': ['config', 'output'],
        'before': {'state': 'absent'}, 'after': {'state': 'present', 'value': output}}],
        'dependencies': [{'key': 'placement.canvas.materialization', 'pin': {'ref': 'parent',
            'path': ['config', 'output'], 'after': output, 'authored_canvas': {'width': 1280, 'height': 720, 'fps': 24}}}]}
    inverse = {'command': {'op': 'timeline.inverse'}, 'writes': [{'ref': 'parent', 'path': ['config', 'output'],
        'before': {'state': 'present', 'value': output}, 'after': {'state': 'absent'}}]}
    entry = {'records': {'commands': [source]}}
    history = SimpleNamespace(entries=[entry])
    selected = [source]
    monkeypatch.setattr(_step_undo, 'entry_records', lambda *_: selected)
    assert _authenticated_initialization_fps_removals(candidate, baseline, [inverse], history) == {'parent.config.output.fps'}
    forged = copy.deepcopy(source); forged['dependencies'][0]['pin']['after'] = {**output, 'fps': 30}
    selected = [forged]; entry['records']['commands'] = [forged]
    assert not _authenticated_initialization_fps_removals(candidate, baseline, [inverse], history)
    missing = copy.deepcopy(source); missing['dependencies'] = []
    selected = [missing]; entry['records']['commands'] = [missing]
    assert not _authenticated_initialization_fps_removals(candidate, baseline, [inverse], history)
    selected = []; entry['records']['commands'] = [source]
    assert not _authenticated_initialization_fps_removals(candidate, baseline, [inverse], history)
    selected = [source]
    changed = copy.deepcopy(baseline); changed['parent']['config']['output']['fps'] = 25
    assert not _authenticated_initialization_fps_removals(candidate, changed, [inverse], history)
    changed = copy.deepcopy(baseline); changed['parent']['config']['theme_overrides']['visual']['canvas']['width'] = 640
    assert not _authenticated_initialization_fps_removals(candidate, changed, [inverse], history)


def test_clip_fit_type_mirrors_runtime_schema():
    from typing import get_args, get_type_hints
    from astrid.core.timeline.banodoco_schema import SharedTimelineClip, load_vendored_timeline_schema
    import scripts.gen_remotion_types as generator

    schema = load_vendored_timeline_schema()
    assert set(get_args(get_type_hints(SharedTimelineClip)['fit'])) == {'contain', 'cover'}
    assert set(schema['definitions']['TimelineClip']['properties']['fit']['enum']) == {'contain', 'cover'}
    assert "fit?: 'contain' | 'cover';" in generator.generate()


def test_missing_child_canvas_refusal_names_revision_and_clip():
    from astrid.sdk.timeline._appearance import normalize_revision_closure
    parent = {'payload': {'config': {'output': {'resolution': '1280x720', 'fps': 24},
                                    'tracks': [], 'clips': []}, 'clips': []}}
    child = {'revision_id': 'opening-rev', 'payload': {'tracks': [{'id': 'v', 'kind': 'visual', 'scale': 2}],
             'clips': [{'id': 'opening-image', 'track': 'v', 'clipType': 'image', 'x': 0}]}}
    with pytest.raises(ValueError, match='opening-rev.*opening-image.*output.resolution'):
        normalize_revision_closure(parent, [child])


def test_canvasless_child_proof_preserves_no_output_and_uses_parent():
    from astrid.sdk.timeline._appearance import normalize_revision_closure
    parent = {'payload': {'config': {'output': {'resolution': '1280x720', 'fps': 24}, 'tracks': [], 'clips': []}, 'clips': []}}
    child = {'revision_id': 'opening', 'payload': {'tracks': [{'id': 'v', 'kind': 'visual',
             'app': {'scaleAppliesToPositionedClips': True}}],
             'clips': [{'id': 'image', 'track': 'v', 'clipType': 'image'}]}}
    original = copy.deepcopy(child)
    _, children, findings = normalize_revision_closure(parent, [child])
    assert 'output' not in children[0]['payload']
    assert any(item.get('kind') == 'canvas-not-required' and item['owner'] == 'opening' for item in findings)
    assert child == original
