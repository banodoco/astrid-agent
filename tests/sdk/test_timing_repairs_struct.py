"""REPAIRS-STRUCT scenarios on disposable tiny inputs, not historical film proof."""
from __future__ import annotations

import copy
import json
from pathlib import Path

import pytest

from astrid.sdk import timeline_intent as intent
from astrid.sdk.timeline.compiler import TimingCycleError, compile_timing
from astrid.sdk.timeline_checkout import Checkout, TimelineEditError

TINY = Path(__file__).resolve().parents[1] / 'fixtures/timeline_editing/tiny.json'


def tiny():
    tl = Checkout(json.loads(TINY.read_text()))
    tl.clip('a-music').data['to'] = 8.0
    intent.set_beats(tl.clip('a-music').data, {'beats': [n / 2 for n in range(16)]})
    return tl


def test_music_film_feedback_is_refused_with_the_complete_path():
    tl = tiny()
    # A placed beat sizes the final picture, whose end sizes that beat's bed.
    intent.set_until(tl.clip('a-music').data, 'end of c2')
    intent.set_until(tl.cut_picture('c2').data, 'beat 1 after "now"')
    before = copy.deepcopy(tl.__dict__)
    with pytest.raises(TimingCycleError) as error:
        tl.resolve()
    assert error.value.path == ('a-music.end', 'c2.end', 'film.pictureEnd',
                                'c2.plate.end', 'a-music.end')
    assert str(error.value) == 'CYCLE: ' + ' -> '.join(error.value.path)
    assert tl.__dict__ == before


def test_keep_end_film_command_cannot_follow_its_own_placed_beat():
    tl = tiny()
    before = copy.deepcopy(tl.__dict__)
    with pytest.raises(TimingCycleError) as error:
        tl.film_end('beat 2 after "now"')
    assert 'film.pictureEnd' in error.value.path
    assert any('.segments' in node for node in error.value.path)
    assert tl.__dict__ == before


def test_terminal_start_following_its_own_end_is_a_self_cycle():
    tl = tiny()
    intent.set_on(tl.cut_picture('c2').data, 'end')
    before = copy.deepcopy(tl.bundle)
    with pytest.raises(TimingCycleError) as error:
        compile_timing(tl.bundle)
    assert 'c2.plate.start' in error.value.path
    assert 'c2.end' in error.value.path
    assert tl.bundle == before


@pytest.mark.parametrize('field', ['on', 'until', 'formula', 'timing_formula', 'land', 'beat'])
def test_existing_ambiguous_reference_refuses_without_picking_the_nearest(field):
    tl = tiny()
    # First resolve a unique declaration. A later raw/imported narration edit
    # introduces ambiguity; cached seconds must not become an occurrence choice.
    clip = tl.clip('c1.rocket')
    text = 'beat 1 after "viral"' if field == 'beat' else '"viral"'
    if field in ('on', 'beat'):
        intent.set_on(clip.data, text)
    elif field == 'until':
        intent.set_until(clip.data, text)
    elif field in ('formula', 'timing_formula'):
        intent.set_formula(clip.data, 'params.delay' if field == 'formula' else 'hold',
                           {'moment': text, 'as': 'timeline_seconds'})
    else:
        clip = tl.cut_picture('c1')
        intent.set_sequence(clip.data, 'opener', 0)
        intent.set_sequence_fit(clip.data, {'land': text})
    tl.resolve()
    intent.set_line(tl.clip('b-vo').data, 's2', [[0.1, 0.4, 'viral'], [0.5, 0.8, 'now']])
    before = copy.deepcopy(tl.__dict__)
    with pytest.raises(TimelineEditError, match='AMBIGUOUS_REFERENCE') as error:
        tl.resolve()
    message = str(error.value)
    assert clip.address in message and text in message
    assert '"viral" in s1' in message and '"viral" in s2' in message
    assert 'in <line>' in message and '#n' in message
    assert tl.__dict__ == before


def test_scoped_occurrence_remains_explicit():
    tl = tiny()
    intent.set_line(tl.clip('a-vo').data, 's1', [[.1, .3, 'and'], [.4, .6, 'and']])
    tl.clip('c1.rocket').on('"and" in s1 #2')
    tl.resolve()
    assert intent.on(tl.clip('c1.rocket').data) == '"and" in s1 #2'


def test_line_scope_still_requires_an_occurrence_when_the_line_repeats_the_word():
    tl = tiny()
    intent.set_line(tl.clip('a-vo').data, 's1', [[.1, .3, 'and'], [.4, .6, 'and']])
    intent.set_on(tl.clip('c1.rocket').data, '"and" in s1')
    with pytest.raises(TimelineEditError, match='"and" in s1 #1; "and" in s1 #2'):
        compile_timing(tl.bundle)


def test_ambiguity_refusal_rolls_back_an_earlier_timing_command():
    tl = tiny()
    before = copy.deepcopy(tl.__dict__)
    with pytest.raises(TimelineEditError, match='AMBIGUOUS_REFERENCE'):
        with tl.edit('timing then ambiguity', check=False, quiet=True):
            tl.insert_time(2, .5)
            intent.set_line(tl.clip('b-vo').data, 's2', [[.1, .4, 'viral'], [.5, .8, 'now']])
            tl.resolve()
    assert tl.__dict__ == before


def test_a_fixed_bed_in_plays_on_mode_can_size_the_final_picture():
    tl = tiny()
    tl.music = 'plays_on'
    before = copy.deepcopy(tl.clip('a-music').data)
    tl.film_end('beat 2 after "now"')
    assert tl.duration == 7.0
    assert tl.clip('a-music').data == before
    assert tl.music == 'plays_on'


def test_plays_on_override_preserves_an_existing_numeric_ripple_basis():
    tl = tiny()
    tl.clip('a-music').data['to'] = 12.0
    intent.set_beats(tl.clip('a-music').data, {'beats': [n / 2 for n in range(24)]})
    tl.insert_time(2, 1)
    from astrid.sdk.timeline.music_beds import collection
    before = copy.deepcopy(collection(tl))
    tl.film_end('beat 2 after "now"', music='plays_on')
    assert tl.duration == 8.0
    assert collection(tl) == before
    assert tl.music == 'keep_end'  # the per-call override remains temporary
    tl.resolve()
    assert collection(tl) == before


@pytest.mark.parametrize('canonical_fit', [False, True])
def test_terminal_sequence_end_import_keeps_its_span_and_authored_members(canonical_fit):
    tl = tiny()
    head = tl.cut_picture('c2')
    head.data['hold'] = 2.0
    intent.set_sequence(head.data, 'ending', 0)
    tail = copy.deepcopy(head.data)
    tail.update(id='authored-tail', at=2.0, params={'other_hand': True})
    intent.set_sequence(tail, 'ending', 1)
    tl.bundle['shots']['B']['internal_timeline']['clips'].append(tail)
    if canonical_fit:
        intent.set_sequence_fit(head.data, {'land': 'end'})
    else:
        intent.set_until(head.data, 'end')
    before = copy.deepcopy(tl.bundle)
    first = compile_timing(tl.bundle).bundle
    assert first == before
    assert compile_timing(first).bundle == first
    assert tl.sequence('ending').steps[-1].end == 8.0
