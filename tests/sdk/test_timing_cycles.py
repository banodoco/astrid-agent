"""The deliberate COMPILER cycle exception; all bundles are disposable fixtures."""
from __future__ import annotations

import copy
import json
from pathlib import Path

import pytest

from astrid.sdk import timeline_intent as intent
from astrid.sdk.timeline.compiler import TimingCycleError, TimingInputs, compile_timing
from astrid.sdk.timeline_checkout import Checkout, TimelineEditError

TINY = Path(__file__).resolve().parents[1] / 'fixtures/timeline_editing/tiny.json'


def tiny_bundle():
    return json.loads(TINY.read_text())


def fixture_clip(bundle, clip_id):
    return next(clip for shot in bundle['shots'].values()
                for clip in shot['internal_timeline']['clips'] if clip['id'] == clip_id)


def tiny():
    return Checkout(tiny_bundle())


def recorded_cycle():
    # Exact supported declaration repro in F-COMPILER-002/engineering.py.
    bundle = tiny_bundle()
    intent.set_on(fixture_clip(bundle, 'a-plate'), 'c2')
    intent.set_on(fixture_clip(bundle, 'b-plate'), 'c1')
    return Checkout(bundle)


def test_recorded_cycle_has_a_complete_stable_directed_path():
    tl = recorded_cycle()
    expected = ('c1.plate.start', 'c2.start', 'c2.plate.start', 'c1.start', 'c1.plate.start')
    for _ in range(2):
        with pytest.raises(TimingCycleError) as error:
            compile_timing(tl.bundle)
        assert isinstance(error.value, TimelineEditError)
        assert error.value.code == 'CYCLE'
        assert error.value.path == expected
        assert str(error.value) == 'CYCLE: ' + ' -> '.join(expected)
        # Input storage order does not choose the reported path.
        for shot in tl.bundle['shots'].values():
            shot['internal_timeline']['clips'].reverse()


@pytest.mark.parametrize('text', ['c1', 'c001 +2f', 'beat 1 after c1'])
def test_self_cycle_uses_parsed_cut_aliases_and_nested_moments(text):
    tl = tiny()
    intent.set_on(tl.cut_picture('c1').data, text)
    with pytest.raises(TimingCycleError) as error:
        compile_timing(tl.bundle)
    assert error.value.path == ('c1.plate.start', 'c1.start', 'c1.plate.start')


def test_distinct_longer_cycle_reports_every_link():
    tl = tiny()
    shot = tl.bundle['shots']['A']['internal_timeline']
    third = copy.deepcopy(tl.cut_picture('c1').data)
    third.update(id='third', at=3.0, hold=1.0)
    intent.set_cut(third, 'c3')
    shot['clips'].append(third)
    for own, ref in [('c1', 'c2'), ('c2', 'c3'), ('c3', 'c1')]:
        intent.set_on(tl.cut_picture(own).data, ref)
    with pytest.raises(TimingCycleError) as error:
        compile_timing(tl.bundle)
    assert error.value.path == ('c1.plate.start', 'c2.start', 'c2.plate.start',
                                'c3.start', 'c3.plate.start', 'c1.start', 'c1.plate.start')


def test_cut_end_dependency_is_the_next_cut_start():
    tl = tiny()
    intent.set_on(tl.cut_picture('c1').data, 'end of c1')
    intent.set_on(tl.cut_picture('c2').data, 'c1')
    with pytest.raises(TimingCycleError) as error:
        compile_timing(tl.bundle)
    assert error.value.path == ('c1.end', 'c2.start', 'c2.plate.start',
                                'c1.start', 'c1.plate.start', 'c1.end')


def test_word_and_cut_dependencies_can_form_a_cycle():
    tl = tiny()
    intent.set_on(tl.cut_picture('c1').data, '"viral"')
    intent.set_on(tl.clip('a-vo').data, 'c1')
    with pytest.raises(TimingCycleError) as error:
        compile_timing(tl.bundle)
    assert set(error.value.path) == {'c1.plate.start', 'c1.start', 'a-vo.start'}


def test_beat_source_can_form_a_cycle_with_a_cut():
    tl = tiny()
    bed = tl.clip('a-music')
    intent.set_beats(bed.data, {'beats': [0.5, 1.0, 1.5]})
    intent.set_on(bed.data, 'c1')
    intent.set_on(tl.cut_picture('c1').data, 'beat 1 after "it"')
    with pytest.raises(TimingCycleError) as error:
        compile_timing(tl.bundle)
    assert error.value.path == ('a-music.end', 'a-music.start', 'c1.start',
                                'c1.plate.start', 'a-music.end')


def test_valid_forward_reference_and_end_reference_remain_accepted():
    tl = tiny()
    tl.clip('c1.rocket').on('c2 -1s')
    tl.clip('c1.rocket').until('end of c2')
    tl.resolve()
    assert tl.clip('c1.rocket').start == 3.0
    assert tl.clip('c1.rocket').end == 8.0


def test_picture_can_follow_a_later_cut_without_forming_a_cycle():
    tl = tiny()
    tl.cut_picture('c1').on('c2 -1s')
    tl.resolve()
    assert tl.time('c2 -1s') == 3.0
    # The legacy picture pass keeps a chapter's first picture at its in-point.
    assert tl.cut_picture('c1').start == 0.0
    assert tl.cut_picture('c2').start == 4.0


@pytest.mark.parametrize('terminal', ['end', 'end of c2', 'c2.end'])
def test_terminal_end_uses_compatibility_basis_without_self_feedback(terminal):
    tl = tiny()
    intent.set_until(tl.cut_picture('c2').data, terminal)
    before = copy.deepcopy(tl.bundle)
    assert compile_timing(tl.bundle).bundle == before


@pytest.mark.parametrize('land', ['"now"', {'word': 's2:1', 'text': 'now'}])
def test_sequence_terminal_span_and_fit_are_accepted(land):
    bundle = tiny_bundle()
    picture = fixture_clip(bundle, 'b-plate')
    intent.set_sequence(picture, 'ending', 0)
    intent.set_until(picture, 'end')
    second = copy.deepcopy(picture)
    second.update(id='last-step', at=2.0, hold=2.0)
    picture['hold'] = 2.0
    intent.set_sequence(second, 'ending', 1)
    intent.set_until(second, None)
    bundle['shots']['B']['internal_timeline']['clips'].append(second)
    intent.set_sequence_fit(picture, {'land': land, 'cycle': ['Q'], 'race': [2]})
    tl = Checkout(bundle)
    tl.resolve()
    steps = [c for c in tl.clips() if intent.sequence(c.data)]
    assert steps[0].start == 4.0
    assert steps[-1].end == 8.0
    assert all(round(a.end * tl.fps) == round(b.start * tl.fps) for a, b in zip(steps, steps[1:]))


def test_generated_sequence_names_stay_strings_and_fits_stay_shot_scoped():
    bundle = tiny_bundle()
    for clip_id, land, asset, race in [('a-plate', '"viral"', 'P', 12),
                                       ('b-plate', '"now"', 'Q', 24)]:
        picture = fixture_clip(bundle, clip_id)
        intent.set_sequence(picture, 'ending', 0)
        intent.set_sequence_fit(picture, {'land': land, 'cycle': [asset], 'race': [race]})

    tl = Checkout(bundle)
    tl.resolve()
    for shot, start, end, landing, asset, count in [('A', 0.0, 4.0, 1.3, 'P', 5),
                                                   ('B', 4.0, 8.0, 6.4, 'Q', 4)]:
        steps = [c for c in tl.clips(shot=shot) if intent.sequence(c.data)]
        assert len(steps) == count
        assert [intent.sequence(c.data) for c in steps] == [('ending', i) for i in range(count)]
        assert all(isinstance(intent.sequence(c.data)[0], str) for c in steps)
        assert {c.asset for c in steps} == {asset}
        assert steps[0].start == start and steps[-1].end == end
        assert steps[-1].start == landing
        assert all(round(a.end * tl.fps) == round(b.start * tl.fps) for a, b in zip(steps, steps[1:]))

    # Both fresh compilation and a serialized reload keep the generated identities.
    before = copy.deepcopy(tl.bundle)
    again = Checkout(bundle)
    again.resolve()
    assert again.bundle == before
    reloaded = Checkout(json.loads(json.dumps(before)))
    reloaded.resolve()
    assert reloaded.bundle == before
    tl.resolve()
    assert tl.bundle == before


def test_cycle_refusal_keeps_inputs_state_and_commands_immutable():
    tl = recorded_cycle()
    state = {'notes': ['keep'], 'report': ['unchanged'], '_music': 'plays_on'}
    commands = [('reflow', (), {'gaps': {'a-vo': 0.5}})]
    before = copy.deepcopy((tl.bundle, state, commands))
    with pytest.raises(TimingCycleError):
        compile_timing(TimingInputs(tl.bundle, state), commands)
    assert (tl.bundle, state, commands) == before


def test_checkout_cycle_refusal_does_not_adopt_any_state():
    tl = recorded_cycle()
    tl._journal.append(('existing', 'snapshot'))
    tl.notes.append('keep')
    tl._mcache = {'sentinel': ['keep']}
    before = copy.deepcopy(tl.__dict__)
    bundle, history, cache = tl.bundle, tl._journal, tl._mcache
    with pytest.raises(TimingCycleError):
        tl.resolve()
    assert tl.__dict__ == before
    assert tl.bundle is bundle and tl._journal is history and tl._mcache is cache
    assert tl._edit_depth == 0 and tl._open_edit is None


def test_real_checkout_transaction_rolls_back_new_cycle():
    tl = tiny()
    before = copy.deepcopy(tl.__dict__)
    with pytest.raises(TimingCycleError):
        with tl.edit('cyclic declaration', check=False, quiet=True):
            tl.cut_picture('c1').on('c2')
            tl.cut_picture('c2').on('c1')
            tl.resolve()
    assert tl.__dict__ == before


def test_cycle_refusal_restores_state_from_before_an_earlier_timing_command():
    tl = tiny()
    tl.report.append('keep this report')
    before = copy.deepcopy(tl.__dict__)
    report, bundle = tl.report, tl.bundle
    with pytest.raises(TimingCycleError):
        with tl.edit('timing then cycle', check=False, quiet=True):
            tl.insert_time(1.0, 0.5)
            tl.cut_picture('c1').on('c2')
            tl.cut_picture('c2').on('c1')
            tl.resolve()
    assert tl.__dict__ == before
    assert tl.report is report and tl.bundle is bundle


def test_cycle_introduced_by_compiled_command_is_refused_before_adoption():
    tl = tiny()
    before = copy.deepcopy(tl.__dict__)
    with pytest.raises(TimingCycleError):
        tl._place_on(tl.cut_picture('c1'), 'c1')
    assert tl.__dict__ == before


def test_an_evaluator_error_cannot_adopt_a_new_cyclic_partial_result(monkeypatch):
    def broken_retime(workspace):
        intent.set_on(workspace.cut_picture('c1').data, 'c2')
        intent.set_on(workspace.cut_picture('c2').data, 'c1')
        raise RuntimeError('an unrelated evaluator failure after writing declarations')

    tl = tiny()
    before = copy.deepcopy(tl.__dict__)
    monkeypatch.setattr(Checkout, 'retime', broken_retime)
    with pytest.raises(TimingCycleError):
        tl.resolve()
    assert tl.__dict__ == before


def test_parameter_moment_does_not_introduce_a_timing_dependency():
    bundle = tiny_bundle()
    intent.set_formula(fixture_clip(bundle, 'a-plate'), 'params.opacity',
                       {'moment': 'c1', 'as': 'timeline_seconds'})
    tl = Checkout(bundle)
    tl.resolve()
    assert tl.cut_picture('c1').params['opacity'] == 0.0


def test_formula_that_writes_timing_is_checked():
    tl = tiny()
    intent.set_formula(tl.cut_picture('c1').data, 'at', {'moment': 'c1', 'as': 'timeline_seconds'})
    with pytest.raises(TimingCycleError):
        compile_timing(tl.bundle)
