from copy import deepcopy
from fractions import Fraction

import pytest

from astrid.packs.rendering.actions.timeline_visualize.readable_timing import project_readable_timing
from astrid.packs.timeline.cli import _human_motion_lines


def _key(at, width=100, opacity=1, x=0):
    return dict(at=at, x=x, y=0, width=width, height=100, opacity=opacity)


def _clip(identifier='a', at=0, hold=3, speed=1, kind='animated-media-transform', **authored):
    raw = dict(id=identifier, clipType=kind, track='v', at=at, hold=hold, speed=speed, **authored)
    return dict(clip_id=identifier, occurrence_id='occ', clip_type=kind,
                authored_fields=raw, render_timing=deepcopy(raw), track=dict(id='v', kind='visual'))


def _project(clips, *, selected=None, fps=30):
    context = dict(status='complete', fps=fps, tracks=[dict(id='v', kind='visual'), dict(id='audio', kind='audio')],
                   clips=[deepcopy(row['render_timing']) for row in clips])
    for row in context['clips']:
        row['target'] = dict(occurrence_id='occ', clip_id=row['id'])
    return project_readable_timing(clips if selected is None else selected, context)


def _seconds(wire):
    return Fraction(*wire)


def test_transform_clock_uses_rounded_mount_and_hold_speed_not_source_clock():
    clip = _clip(at=.05, hold=4, speed=2,
                 params=dict(keyframes=[_key(0), _key(1, 200), _key(3, 300)],
                             sourceSegments=[dict(at=0, sourceStart=7, speed=9), dict(at=1, sourceStart=0, speed=.25)]))
    clip['authored_fields'].update({'from': 77, 'to': 999})
    result = _project([clip])[0]
    assert result['timing_projection']['mounted'] == dict(start_frame=2, end_frame=62, start=[1, 15], end=[31, 15])
    width = [change for change in result['timed_changes'] if change['property'] == 'width']
    assert len(width) == 1
    assert width[0]['before'] == 100 and width[0]['after'] == 250
    assert width[0]['start'] == [1, 15] and width[0]['end'] == [31, 15]
    assert result['source_clock'] == 'source playback segments use a separate media clock'
    assert result['authored_fields']['params']['sourceSegments'] == clip['authored_fields']['params']['sourceSegments']


def test_property_specific_holds_and_reversals_do_not_collapse_to_equal_endpoints():
    clip = _clip(hold=5, params=dict(keyframes=[_key(0), _key(1, 200, .5), _key(2, 200, .25),
                                             _key(3, 100, .25), _key(4, 200, 1)]))
    result = _project([clip])[0]
    width = [row for row in result['timed_changes'] if row['property'] == 'width']
    assert [(r['before'], r['after'], r['hold']) for r in width] == [
        (100, 200, False), (200, 200, True), (200, 100, False), (100, 200, False), (200, 200, True)]
    opacity = [row for row in result['timed_changes'] if row['property'] == 'opacity']
    assert [(r['start'], r['end'], r['hold']) for r in opacity] == [
        ([0, 1], [2, 1], False), ([2, 1], [3, 1], True), ([3, 1], [4, 1], False), ([4, 1], [5, 1], True)]
    assert not any(row['property'] in {'x', 'y', 'height'} for row in result['timed_changes'])
    assert 'more change intervals' in '\n'.join(_human_motion_lines(result))
    assert 'width holds 200' in '\n'.join(_human_motion_lines(result, detail=True))


@pytest.mark.parametrize('effects', [{'fade_in': .05, 'fade_out': .25}, [{'fade_in': .05}, {'fade_out': .25}]])
def test_ordinary_visual_fades_exist_without_keyframes_and_round_like_pinned_helper(effects):
    clip = _clip(kind='media', at=.05, hold=1, effects=effects)
    result = _project([clip])[0]
    assert result['timing_unknowns'] == []
    assert [(c['kind'], c['start'], c['end']) for c in result['timed_changes']] == [
        ('fade_in', [1, 15], [2, 15]), ('fade_out', [4, 5], [16, 15])]
    assert all(c['property'] == 'opacity multiplier' for c in result['timed_changes'])


def test_visual_and_audio_fade_names_are_distinct_contracts():
    visual = _clip(kind='media', effects={'fadeIn': 1}, params={'fadeIn': 1})
    audio = _clip('sound', kind='audio', params={'fadeIn': .5, 'fadeOut': 1}, effects={'fade_in': 2})
    audio['track'] = {'id': 'audio', 'kind': 'audio'}
    audio['render_timing']['track'] = 'audio'
    projected = _project([visual, audio])
    assert projected[0]['timed_changes'] == []
    assert [(row['property'], row['start'], row['end']) for row in projected[1]['timed_changes']] == [
        ('gain multiplier', [0, 1], [1, 2]), ('gain multiplier', [2, 1], [3, 1])]


def test_child_audio_track_collision_uses_parent_visual_dispatch_without_gain_fade():
    clip = _clip(kind='audio', volume=.5, opacity=.6,
                 params={'fadeIn': 1}, effects={'fade_in': .5})
    clip['track'] = {'id': 'shared', 'kind': 'audio', 'muted': True, 'volume': .1}
    clip['track_ref'] = {'scope': 'internal_timeline', 'scope_id': 'child', 'track_id': 'shared'}
    clip['render_timing']['track'] = 'shared'
    parent = {'id': 'shared', 'kind': 'visual', 'opacity': .3, 'volume': .4, 'blendMode': 'screen'}
    context = {'status': 'complete', 'fps': 30, 'tracks': [parent], 'clips': [clip['render_timing']]}
    result = project_readable_timing([clip], context)[0]
    assert result['track'] == clip['track'] and result['track_ref'] == clip['track_ref']
    assert result['authored_fields']['params']['fadeIn'] == 1
    dispatch = result['compositor_dispatch']
    assert dispatch['track'] == parent and dispatch['fade_contract'] == 'visual_effects'
    assert dispatch['controls'] == {'base_gain': .2, 'track_opacity_multiplier': .3, 'clip_opacity_multiplier': .6}
    assert [(row['property'], row['start'], row['end']) for row in result['timed_changes']] == [
        ('opacity multiplier', [0, 1], [1, 2])]
    clip['authored_fields'].pop('effects')
    # params.fadeIn remains authored, but VisualClip does not consume it.
    assert project_readable_timing([clip], context)[0]['timed_changes'] == []


@pytest.mark.parametrize('identity', ['crossfade', 'wipe', 'slide-push', 'zoom-through'])
def test_r13_transition_ids_are_readable_without_rewriting_authored_data(identity):
    source = _clip('source', at=0, hold=2, kind='media')
    destination = _clip('destination', at=2, hold=2, kind='media',
                        transition={'type': identity, 'duration': .4})
    authored = deepcopy(destination['authored_fields']['transition'])
    first, second = _project([source, destination])
    assert 'transition_projection' not in first
    projection = second['transition_projection']
    assert projection['status'] == 'resolved'
    assert (projection['from_clip_id'], projection['to_clip_id']) == ('source', 'destination')
    assert (projection['start_frame'], projection['end_frame']) == (48, 60)
    assert second['timing_projection']['mounted']['start_frame'] == 48
    assert second['timing_projection']['mounted']['end_frame'] == 120
    assert second['authored_fields']['transition'] == destination['authored_fields']['transition'] == authored
    assert projection['transition']['type'] == ('cross-fade' if identity == 'crossfade' else identity)


def test_r13_unknown_transition_spelling_remains_unresolved():
    source = _clip('source', at=0, hold=2, kind='media', transition={'type': 'crossfadee', 'duration': .4})
    destination = _clip('destination', at=1.6, hold=2, kind='media')
    result = _project([source, destination])
    assert result[0]['transition_projection']['status'] == 'unresolved'
    assert "unknown transition id 'crossfadee'" in result[0]['transition_projection']['reason']


def test_r13_first_clip_still_applies_transition_on_its_own_frames():
    first = _clip('first', at=0, hold=2, kind='media', transition={'type': 'zoom-through', 'duration': .4})
    alone = _project([first])[0]
    projection = alone['transition_projection']
    assert projection['transition']['type'] == 'zoom-through'
    assert projection['status'] == 'resolved' and projection['from_clip_id'] is None
    assert (projection['start_frame'], projection['end_frame']) == (0, 12)
    assert alone['timing_projection']['mounted']['end_frame'] == 60


@pytest.mark.parametrize('muted, volume, expected_gain', [(False, .4, .2), (True, .4, 0), (False, -2, 0)])
def test_parent_audio_dispatch_overrides_child_visual_contract_and_inherits_gain(muted, volume, expected_gain):
    clip = _clip(kind='media', volume=.5, params={'fadeIn': .5}, effects={'fade_in': 2})
    parent = {'id': 'v', 'kind': 'audio', 'muted': muted, 'volume': volume, 'opacity': .1}
    result = project_readable_timing([clip], {'status': 'complete', 'fps': 30, 'tracks': [parent],
                                            'clips': [clip['render_timing']]})[0]
    dispatch = result['compositor_dispatch']
    assert dispatch['track'] == parent and dispatch['fade_contract'] == 'audio_params'
    assert dispatch['controls'] == {'base_gain': expected_gain}
    assert [(row['property'], row['start'], row['end']) for row in result['timed_changes']] == [
        ('gain multiplier', [0, 1], [1, 2])]
    assert result['track']['kind'] == 'visual'


def test_effect_component_inherits_parent_opacity_but_does_not_claim_parent_audio_controls():
    clip = _clip(volume=.5, params={'keyframes': [_key(0), _key(1, 200)]})
    context = {'status': 'complete', 'fps': 30, 'tracks': [{'id': 'v', 'kind': 'visual', 'opacity': .3,
                                                        'muted': True, 'volume': .1}],
               'clips': [clip['render_timing']]}
    result = project_readable_timing([clip], context)[0]
    assert result['compositor_dispatch']['control_contract'] == 'visual_component'
    assert result['compositor_dispatch']['controls'] == {'track_opacity_multiplier': .3}
    context['tracks'][0]['kind'] = 'audio'
    result = project_readable_timing([clip], context)[0]
    assert result['timed_changes'] == []
    assert result['timing_unknowns'] == ['canonical transform is dispatched on an audio track; transform keyframes are not rendered']


@pytest.mark.parametrize('tracks', [
    [],
    [{'id': 'other', 'kind': 'visual'}],
    [{'id': 'v', 'kind': 'visual'}, {'id': 'v', 'kind': 'audio'}],
])
def test_unproven_compositor_dispatch_retains_authored_controls_without_effective_claim(tracks):
    clip = _clip(kind='media', volume=.5, params={'fadeIn': 1}, effects={'fade_in': 2})
    result = project_readable_timing([clip], {'status': 'complete', 'fps': 30, 'tracks': tracks,
                                            'clips': [clip['render_timing']]})[0]
    assert result['timed_changes'] == [] and 'timing_projection' not in result
    assert result['compositor_dispatch'] == {'status': 'unknown',
        'reason': 'compositor dispatch track unresolved or ambiguous; fade/control contract unavailable'}
    assert result['authored_fields'] == clip['authored_fields']


def test_accepted_transition_retimes_destination_and_preserves_off_page_identity_once():
    source = _clip('source', kind='media', at=1, hold=2, transition={'id': 'cross-fade', 'durationFrames': 15})
    destination = _clip('destination', at=2.5, hold=2, params={'keyframes': [_key(0), _key(1, 200)]})
    # The compositor ignores authored 2.5 and mounts destination at 1+2-.5.
    destination['render_timing']['at'] = 2.75
    projected = _project([source, destination])
    transition = projected[0]['transition_projection']
    assert transition['status'] == 'resolved'
    assert transition['start'] == [5, 2] and transition['end'] == [3, 1]
    assert transition['from_target']['clip_id'] == 'source' and transition['to_target']['clip_id'] == 'destination'
    assert projected[1]['timing_projection']['mounted']['start_frame'] == 75
    assert projected[1]['timed_changes'][0]['start'] == [5, 2]
    assert 'transition_projection' not in projected[1]
    only_source = _project([source, destination], selected=[source])[0]
    assert only_source['transition_projection']['to_target'] == dict(occurrence_id='occ', clip_id='destination')
    assert 'occ/source → occ/destination' in '\n'.join(_human_motion_lines(only_source))


@pytest.mark.parametrize('declaration, destination_at, reason', [
    ({'id': 'cross-fade', 'durationFrames': 999}, 2, 'duration non-positive or exceeds either clip'),
    ({'id': 'custom'}, 2, 'unknown transition id'),
    ({'id': 'fade'}, 4, 'does not overlap or abut'),
])
def test_rejected_transitions_retain_authored_declaration_without_invented_interval(declaration, destination_at, reason):
    a = _clip(kind='media', hold=2, transition=declaration)
    b = _clip('b', at=destination_at)
    result = _project([a, b])[0]
    assert result['transition_projection']['status'] == 'unresolved'
    assert reason in result['transition_projection']['reason']
    assert 'start' not in result['transition_projection']
    assert result['authored_fields']['transition'] == declaration


def test_custom_keyframe_shape_does_not_prove_canonical_element_contract():
    custom = _clip(kind='arbitrary-custom', params={'keyframes': [_key(0), _key(1, 200)]})
    property_keys = _clip(kind='media', params={'keyframes': [{'property': 'zoom', 'at': 0, 'value': 1}, {'property': 'zoom', 'at': 1, 'value': 2}]})
    results = _project([custom, property_keys])
    for result in results:
        assert result['timed_changes'] == []
        assert 'custom keyframe clock is unsupported' in result['timing_unknowns']
        assert result['timing_projection']['mounted']['start_frame'] == 0
        assert result['authored_fields']['params']['keyframes']


def test_missing_complete_context_never_guesses_an_incoming_transition_mount():
    clip = _clip(params={'keyframes': [_key(0), _key(1, 200)]})
    result = project_readable_timing([clip], {'status': 'unavailable', 'reason': 'complete render scheduling context exceeds 32768-byte bound'})[0]
    assert result['timed_changes'] == []
    assert 'timing_projection' not in result
    assert result['timing_unknowns'] == ['complete render scheduling context exceeds 32768-byte bound']
    assert result['clip_id'] == 'a' and result['occurrence_id'] == 'occ'


def test_stale_pinned_transform_revision_is_unknown():
    clip = _clip(params={'keyframes': [_key(0), _key(1, 200)]},
                 elementRef={'id': 'animated-media-transform', 'kind': 'effect', 'revision': 'old-revision'})
    result = _project([clip])[0]
    assert result['timed_changes'] == []
    assert result['timing_unknowns'] == ['animated-media-transform element revision is unverified or stale']


def test_large_transition_free_closure_still_summarizes_an_ordinary_fade():
    clip = _clip(kind='media', effects={'fade_in': 1})
    result = project_readable_timing([clip], {'status': 'unavailable', 'fps': 30, 'transition_free': True, 'tracks': [{'id': 'v', 'kind': 'visual'}],
                                            'reason': 'complete render scheduling context exceeds 32768-byte bound'})[0]
    assert result['timing_unknowns'] == []
    assert result['timed_changes'][0]['kind'] == 'fade_in'


def test_left_clipped_transform_restarts_local_clock_without_applying_source_trim():
    clip = _clip(at=10, hold=2, speed=2, params={'keyframes': [_key(0), _key(1, 200)]})
    clip['render_timing'].update({'from': 8, 'to': 10})
    clip['authored_fields']['at'] = -.5
    result = _project([clip])[0]
    change = result['timed_changes'][0]
    assert change['start'] == [10, 1] and change['end'] == [11, 1]
    assert change['before'] == 100 and change['after'] == 200


def test_opacity_only_transform_is_a_fade_without_invented_pan_or_zoom():
    result = _project([_clip(params={'keyframes': [_key(0), _key(1, opacity=.3)]})])[0]
    moving = [row for row in result['timed_changes'] if not row['hold']]
    assert len(moving) == 1 and moving[0]['property'] == 'opacity'
    assert moving[0]['before'] == 1 and moving[0]['after'] == pytest.approx(.3)


def test_last_clip_transition_retains_specific_unresolved_declaration():
    result = _project([_clip(kind='media', transition='fade')])[0]
    assert result['transition_projection']['status'] == 'unresolved'
    assert result['transition_projection']['reason'] == 'no following clip on compositor track'
    assert result['transition_projection']['transition'] == {'id': 'fade'}


def test_fractional_fps_without_existing_mounted_model_is_explicit_unknown():
    clip = _clip(params={'keyframes': [_key(0), _key(1, 200)]})
    result = _project([clip], fps=29.97)[0]
    assert result['timed_changes'] == []
    assert result['timing_unknowns'] == ['existing mounted transition model requires whole-number FPS']
    assert result['authored_fields']['params']['keyframes'][1]['width'] == 200


def test_omitted_timing_does_not_drop_clip_or_invent_transform():
    clip = _clip(params={'keyframes': [_key(0), _key(1, 200)]})
    clip['render_timing'] = None
    clip['render_timing_unknown'] = 'managed render timing fields exceed 4096-byte bound'
    result = project_readable_timing([clip])[0]
    assert result['timed_changes'] == []
    assert result['timing_unknowns'] == ['managed render timing fields exceed 4096-byte bound']
    assert result['clip_id'] == 'a'


def test_absent_actual_compositor_track_keeps_window_but_cannot_claim_mounted_motion():
    clip = _clip(params={'keyframes': [_key(0), _key(1, 200)]})
    result = project_readable_timing([clip], {'status': 'complete', 'fps': 30, 'tracks': [],
                                            'clips': [clip['render_timing']]})[0]
    assert result['timed_changes'] == []
    assert 'timing_projection' not in result
    assert result['timing_unknowns'] == ['compositor track unresolved; mounted clock unavailable']


def test_human_rows_and_frame_navigation_prefer_actual_mounted_intervals():
    from astrid.packs.timeline.cli import _human_clip_interval, _clip_bounds
    source = _clip('source', kind='media', at=1, hold=2, transition={'id': 'fade', 'durationFrames': 15})
    destination = _clip('destination', at=2.75, hold=2, params={'keyframes': [_key(0), _key(1, 200)]})
    destination.update(start=[11, 4], duration=[2, 1])
    mounted = _project([source, destination])[1]
    assert _human_clip_interval(mounted) == ('2.5', '4.5')
    assert _clip_bounds(mounted) == (Fraction(5, 2), Fraction(9, 2))
    # The original saved selection and authored placement are preserved.
    assert mounted['start'] == [11, 4]
    assert mounted['authored_fields']['at'] == 2.75
