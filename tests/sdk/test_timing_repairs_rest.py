"""Intentional trim/speed and bed-scope repairs on disposable inputs."""
import copy
import json
from pathlib import Path

import pytest

from astrid.sdk import timeline_intent as intent
from astrid.sdk.timeline._common import TimelineEditError
from astrid.sdk.timeline.compiler import compile_timing
from astrid.sdk.timeline_checkout import Checkout

TINY = Path(__file__).resolve().parents[1] / 'fixtures/timeline_editing/tiny.json'


def tiny():
    return Checkout(json.loads(TINY.read_text()))


def media(tl, *, audio=False, hold=False):
    data = dict(id='trimmed', clipType='media', track='vo' if audio else 'sprite',
                asset='Q', at=.5, **{'from': 2., 'to': 10., 'speed': 2.},
                app={'foreign': {'keep': [1, True]}})
    if hold:
        data['hold'] = 8.
    tl._internal('A')['clips'].append(data)
    return tl.clip('trimmed')


@pytest.mark.parametrize('audio', [False, True])
@pytest.mark.parametrize('hold', [False, True])
def test_trimmed_until_follows_a_moving_word_with_speed(audio, hold):
    tl = tiny()
    clip = media(tl, audio=audio, hold=hold)
    data = clip.data
    clip.until('"viral"')
    tl.voice('s1').replace('Q', words=[[.1, .3, 'It'], [1.8, 2., 'viral']])
    assert clip.data is data
    assert clip.end == pytest.approx(2.3)
    assert clip.data['from'] == 2.
    assert clip.data['hold' if hold else 'to'] == pytest.approx(3.6 if hold else 5.6)
    assert clip.data['app']['foreign'] == {'keep': [1, True]}
    once = copy.deepcopy(tl.bundle)
    tl.resolve()
    assert tl.bundle == once


@pytest.mark.parametrize('speed', [.5, 2.])
def test_trim_stores_source_hold_once_and_slip_preserves_span(speed):
    tl = tiny()
    tl._internal('A')['registry']['assets']['Q']['duration'] = 12.
    clip = media(tl)
    clip.data['speed'] = speed
    clip.trim(2., 8.)
    assert clip.data['hold'] == 6.
    assert clip.duration == pytest.approx(6. / speed)
    clip.slip(1.)
    assert (clip.data['from'], clip.data['to']) == (3., 9.)
    assert clip.duration == pytest.approx(6. / speed)


def beds(tl, specs):
    tl.clip('a-music').remove()
    for ident, at, end, grid in specs:
        data = dict(id=ident, clipType='media', track='music', asset='Q', at=at,
                    **{'from': 0., 'to': end-at})
        intent.set_beats(data, {'beats': grid, 'downbeats': grid})
        tl._internal('A')['clips'].append(data)


def test_beats_do_not_jump_to_the_next_bed_or_across_a_gap():
    tl = tiny()
    beds(tl, [('first', 0., 1.5, [.5, 1.]), ('later', 2., 4., [.5, 1.])])
    with pytest.raises(TimelineEditError, match='no beat'):
        tl._moment_time('beat 1 after "viral"')  # base 1.3: first bed has no later beat
    with pytest.raises(TimelineEditError, match='no beat'):
        tl._moment_time('beat 1 before c2')  # c2 is outside both beds


@pytest.mark.parametrize('reverse', [False, True])
@pytest.mark.parametrize('kind', ['beat', 'downbeat'])
def test_overlapping_beds_use_latest_start_then_stable_id(reverse, kind):
    tl = tiny()
    specs = [('old', 0., 4., [1.4, 1.5, 2.]),
             ('a-owner', 1., 4., [.6, 1.2]), ('z-other', 1., 4., [.45, .5])]
    beds(tl, list(reversed(specs)) if reverse else specs)
    assert tl.time(f'{kind} 1 after "viral"') == pytest.approx(1.6)
    assert tl.time(f'{kind} 2 after "viral"') == pytest.approx(2.2)
    with pytest.raises(TimelineEditError, match=f'no {kind}'):
        tl._moment_time(f'{kind} 1 before "viral"')


def test_compiled_trimmed_end_preserves_inputs_and_release_notes_name_repairs():
    tl = tiny()
    clip = media(tl)
    intent.set_until(clip.data, '"viral"')
    before = copy.deepcopy(tl.bundle)
    result = compile_timing(before)
    assert before == tl.bundle
    output = next(c for c in result.bundle['shots']['A']['internal_timeline']['clips'] if c['id']=='trimmed')
    assert output['to'] == pytest.approx(3.6)
    notes = (Path(__file__).resolve().parents[2] / 'docs/setup/release-recipe.md').read_text()
    assert 'REPAIRS-REST' in notes
    assert 'trim' in notes and 'speed' in notes and 'overlap' in notes


def test_slip_without_source_out_uses_speed_and_leaves_hold_unchanged():
    tl = tiny()
    tl._internal('A')['registry']['assets']['Q']['duration'] = 12.
    clip = media(tl, hold=True)
    clip.data.pop('to')
    clip.slip(1.)
    assert (clip.data['from'], clip.data['to'], clip.data['hold']) == (3., 11., 8.)
    assert clip.duration == 4.


@pytest.mark.parametrize('audio', [False, True])
def test_trimmed_literal_for_is_timeline_seconds_at_nonunit_speed(audio):
    tl = tiny()
    clip = media(tl, audio=audio)
    intent.set_for(clip.data, 1.2)
    tl.resolve()
    assert clip.end == pytest.approx(1.7)
    assert clip.data['to'] == pytest.approx(4.4)


def test_trimmed_picture_runs_to_its_next_cut_end():
    tl = tiny()
    pic = tl.cut_picture('c1')
    pic.data.update(clipType='media', **{'from': 2., 'to': 3., 'speed': 2.})
    pic.data.pop('hold', None)
    tl.resolve()
    assert pic.end == pytest.approx(tl.cut_picture('c2').start)
    assert pic.data['to'] == pytest.approx(2. + pic.duration * 2.)


def test_beat_owner_uses_half_open_boundary_and_source_trim_speed():
    tl = tiny()
    boundary = tl.cut_picture('c2').start
    beds(tl, [('first', 0., boundary, [.5, 1.5]),
              ('incoming', boundary, boundary + 2., [3., 4.])])
    incoming = tl.clip('incoming')
    incoming.data.update(**{'from': 2., 'to': 6., 'speed': 2.})
    assert tl.time('beat 1 after c2') == pytest.approx(boundary + .5)
    with pytest.raises(TimelineEditError, match='no beat'):
        tl._moment_time('beat 1 before c2')
    with pytest.raises(TimelineEditError, match='no beat'):
        tl._moment_time('beat 3 after c2')


def test_beat_counts_stay_with_authored_bed_across_generated_pieces():
    tl = tiny()
    bed = tl.clip('a-music')
    bed.data['to'] = 8.
    intent.set_beats(bed.data, {'beats': [n/2 for n in range(17)]})
    tl.insert_time(2., 1.)
    # The held ending creates more than one output of the same authored bed.
    outputs = [c for c in tl.clips(audio=True) if c.track == 'music']
    assert len(outputs) > 1
    # Repeated beat 2.0 at the crossfade is one beat, so the fifth is 3.5.
    assert tl.time('beat 5 after "viral"') == pytest.approx(3.5)
    before = tl.time('beat 5 after "viral"')
    tl.resolve()
    assert tl.time('beat 5 after "viral"') == before


def test_failed_trim_rolls_back_earlier_corrected_end_with_live_identity():
    tl = tiny()
    tl._internal('A')['registry']['assets']['Q']['duration'] = 12.
    clip = media(tl)
    before = copy.deepcopy(tl.bundle)
    data = clip.data
    with pytest.raises(TimelineEditError, match='source range'):
        with tl.edit('end then invalid trim', check=False, quiet=True):
            clip.until('"viral"')
            clip.trim(2., 30.)
    assert tl.bundle == before
    assert tl.clip('trimmed').data is data
