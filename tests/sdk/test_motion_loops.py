"""Synthetic source-level loop producer checks; no film or render proof."""
import copy

import pytest

from astrid.sdk import timeline_intent as intent
from astrid.sdk.timeline._common import TimelineEditError
from astrid.sdk.timeline.compiler import compile_timing
from astrid.sdk.timeline_checkout import Checkout


def example():
    clips = [
        {'id': 'plate', 'clipType': 'am-snap-plate', 'track': 'picture', 'at': 0, 'hold': 4},
        {'id': 'sprite', 'clipType': 'am-sprite', 'track': 'sprite', 'at': .5, 'hold': 1,
         'params': {'frames': {'count': 4, 'fps': 12}}},
        {'id': 'voice', 'clipType': 'media', 'track': 'vo', 'at': 0, 'hold': 3,
         'app': {'astrid': {'segment': 'n21', 'words': [[.25, .4, 'ready'], [.5, .8, 'Astrid']]}}},
        {'id': 'music-main', 'clipType': 'media', 'track': 'music', 'at': 0,
         'from': 2, 'to': 10, 'speed': 2,
         'app': {'astrid': {'beats': {'beats': [2, 3, 4.4, 6, 8]}}}},
        {'id': 'other-bed', 'clipType': 'media', 'track': 'music', 'at': 0, 'hold': 4,
         'app': {'astrid': {'beats': {'beats': [.1, .2, .4, .7, 1.1]}}}},
    ]
    return Checkout({'parent': {'config': {'theme_overrides': {'visual': {'canvas': {'fps': 30}}}}, 'clips': []},
                     'placements': [{'shot_id': 'S', 'occurrence_id': 'oS',
                                     'placement': {'start_ms': 2000}, 'duration_ms': 4000}],
                     'shots': {'S': {'internal_timeline': {
                         'tracks': [{'id': name, 'kind': kind} for name, kind in
                                    [('picture', 'visual'), ('sprite', 'visual'), ('vo', 'audio'), ('music', 'audio')]],
                         'clips': clips}}}})


def spec(period):
    return {'phase': {'ref': 'beat 1 after "ready" in n21'}, 'period': period, 'beforePhase': 'continue'}


def resolved(tl):
    return Checkout(compile_timing(tl.bundle).bundle).clip('sprite').data['params']['loop']


def test_fixed_period_uses_global_moment_without_touching_onset_or_key_origin():
    tl = example()
    sprite = tl.clip('sprite')
    sprite.data['keyframeOrigin'] = .25
    # Fixed period permits a word reference and does not author a clip onset.
    authored = {'phase': {'ref': '"Astrid" in n21'}, 'period': {'frames': 12}}
    intent.set_loop(sprite.data, authored)
    authored['period']['frames'] = 999  # authoring stores owned input
    before = copy.deepcopy(tl.bundle)
    assert resolved(tl) == {'phaseFrame': 75, 'periodFrames': 12}
    assert tl.bundle == before
    assert intent.on(sprite.data) is None
    assert sprite.data['keyframeOrigin'] == .25


def test_moving_clip_start_does_not_move_global_loop_phase():
    tl = example()
    sprite = tl.clip('sprite')
    intent.set_loop(sprite.data, spec({'beats': 1, 'bed': 'music-main'}))
    first = resolved(tl)
    sprite.data['at'] = 1.0
    assert resolved(tl) == first
    assert first == {'phaseFrame': 75, 'beatFrames': [60, 75, 96, 120, 150], 'periodBeats': 1}


def test_actual_placed_intervals_use_bed_trim_speed_and_ignore_other_music():
    tl = example()
    intent.set_loop(tl.clip('sprite').data, spec({'beats': 1, 'bed': 'music-main'}))
    loop = resolved(tl)
    assert [b-a for a, b in zip(loop['beatFrames'], loop['beatFrames'][1:])] == [15, 21, 24, 30]
    assert 66 not in loop['beatFrames']


def test_split_bed_descendants_supply_actual_edited_beat_intervals():
    tl = example()
    bed = tl.clip('music-main')
    bed.data['to'] = 4.4  # source at cut is excluded from first half-open piece
    tail = copy.deepcopy(bed.data)
    tail.update(id='music-main:b', at=1.2, **{'from': 5.6, 'to': 10})
    tail['app']['split_from'] = 'music-main'
    tl.bundle['shots']['S']['internal_timeline']['clips'].append(tail)
    intent.set_loop(tl.clip('sprite').data, spec({'beats': 1, 'bed': 'music-main'}))
    assert resolved(tl)['beatFrames'] == [60, 75, 102, 132]


def test_multi_beat_period_retains_exact_adjacent_beat_grid():
    tl = example()
    intent.set_loop(tl.clip('sprite').data, spec({'beats': 2, 'bed': 'music-main'}))
    loop = resolved(tl)
    assert loop['periodBeats'] == 2 and loop['phaseFrame'] == 75
    assert loop['beatFrames'] == [60, 75, 96, 120, 150]


def test_phase_identity_uses_existing_frame_quantization_for_decimal_source_math():
    tl = example()
    music = tl.clip('music-main').data
    music.update(speed=1, **{'from': 2.2})
    music['app']['astrid']['beats']['beats'] = [2.2, 2.3, 3.2, 4.4, 6]
    tl.clip('voice').data['app']['astrid']['words'][0] = [.1, .2, 'ready']
    sprite = tl.clip('sprite')
    sprite.data['at'] = .1
    intent.set_loop(sprite.data, {'phase': {'ref': '"ready" in n21'},
                                 'period': {'beats': 1, 'bed': 'music-main'}})
    assert resolved(tl)['phaseFrame'] == 63


def test_no_authored_loop_keeps_legacy_params_and_compilation_is_idempotent():
    tl = example()
    params = copy.deepcopy(tl.clip('sprite').data['params'])
    compiled = compile_timing(tl.bundle)
    assert Checkout(compiled.bundle).clip('sprite').data['params'] == params
    assert compile_timing(compiled.bundle).bundle == compiled.bundle
    intent.set_loop(tl.clip('sprite').data, spec({'frames': 12}))
    compiled = compile_timing(tl.bundle)
    assert compile_timing(compiled.bundle).bundle == compiled.bundle
    sprite = Checkout(compiled.bundle).clip('sprite')
    intent.set_loop(sprite.data, None)
    assert 'loop' not in sprite.data['params'] and intent.loop(sprite.data) is None


@pytest.mark.parametrize('period', [{'frames': 0}, {'frames': True}, {'frames': 1.5},
                                    {'beats': 0, 'bed': 'music-main'},
                                    {'beats': 1, 'bed': 'missing'}, {'beats': 1},
                                    {'frames': 12, 'beats': 1, 'bed': 'music-main'}])
def test_invalid_loop_period_blocks_compilation_without_mutating_input(period):
    tl = example()
    intent.set_loop(tl.clip('sprite').data, spec(period))
    before = copy.deepcopy(tl.bundle)
    with pytest.raises(TimelineEditError):
        compile_timing(tl.bundle)
    assert tl.bundle == before


def test_missing_adjacent_beat_coverage_is_rejected_without_tempo_extrapolation():
    tl = example()
    tl.clip('sprite').data['hold'] = 3.0
    intent.set_loop(tl.clip('sprite').data, spec({'beats': 1, 'bed': 'music-main'}))
    with pytest.raises(TimelineEditError, match='adjacent placed beats covering'):
        compile_timing(tl.bundle)


def test_beat_period_phase_must_be_a_placed_beat_of_the_requested_bed():
    tl = example()
    intent.set_loop(tl.clip('sprite').data, {'phase': {'ref': '"ready" in n21'},
                                         'period': {'beats': 1, 'bed': 'music-main'}})
    with pytest.raises(TimelineEditError, match='actual placed beat'):
        compile_timing(tl.bundle)


def test_loop_does_not_silently_accept_a_clip_with_no_loop_consumer():
    tl = example()
    intent.set_loop(tl.clip('plate').data, spec({'frames': 12}))
    with pytest.raises(TimelineEditError, match='am-sprite consumer'):
        compile_timing(tl.bundle)
