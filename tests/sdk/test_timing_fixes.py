"""TIMING-FIXES regressions on the admitted tiny fixture, not historical certification."""
from __future__ import annotations

import copy
import json
from pathlib import Path

import pytest

from astrid.sdk import timeline_intent as intent
from astrid.sdk.timeline_checkout import Checkout, TimelineEditError
from astrid.sdk.timeline.model import Cut

TINY = Path(__file__).resolve().parents[1] / 'fixtures/timeline_editing/tiny.json'


def tiny():
    tl = Checkout(json.loads(TINY.read_text()))
    intent.set_on(tl.clip('a-rocket').data, None)
    return tl


def sequence(*, racing=False):
    tl = tiny()
    head = tl.clip('a-plate')
    head.data['hold'] = 2.0
    intent.set_sequence(head.data, 'seq', 0)
    second = copy.deepcopy(head.data)
    second.update(id='authored-tail', at=2.0, hold=2.0)
    intent.set_sequence(second, 'seq', 1)
    tl.bundle['shots']['A']['internal_timeline']['clips'].append(second)
    if racing:
        intent.set_sequence_fit(head.data, {'land': '"viral"', 'lead': [12], 'race': [6, 4, 2],
                                            'cycle': ['P', 'Q'], 'then': 'R'})
    return tl


def take(tl, text):
    words = [[i * .2 + .1, i * .2 + .2, word] for i, word in enumerate(text.split())]
    tl.voice('s1').replace('Q', words=words, ripple=False)


def test_sdk_fit_uses_the_compiler_and_orphan_checks_single_land_store():
    tl = sequence()
    tl.sequence('seq').fit(until='"viral"')
    head = tl.sequence('seq').steps[0]
    assert intent.sequence_fit(head.data)['land'] == '"viral"'
    assert intent.until(head.data) is None
    assert tl.sequence('seq').steps[-1].end == pytest.approx(1.3, abs=1 / tl.fps)
    first = copy.deepcopy(tl.bundle)
    tl.sequence('seq').fit(until='"viral"')
    assert tl.bundle == first
    intent.set_sequence_fit(head.data, {**intent.sequence_fit(head.data), 'land': '"missing"'})
    assert any('fit until "missing"' in note for note in tl.orphans())


def test_sdk_fit_updates_existing_racing_land_instead_of_a_second_store():
    tl = sequence(racing=True)
    tl.resolve()
    tl.sequence('seq').fit(until='after "viral"')
    fit = intent.sequence_fit(tl.sequence('seq').steps[0].data)
    assert fit['land'] == 'after "viral"' and fit['race'] == [6, 4, 2]
    assert intent.until(tl.sequence('seq').steps[0].data) is None
    assert tl.sequence('seq').steps[-1].start == pytest.approx(1.7, abs=1 / tl.fps)


@pytest.mark.parametrize('position', [0, 2])
@pytest.mark.parametrize('field', ['on', 'until', 'moment', 'word', 'land', 'legacy_land'])
def test_unmatched_word_keeps_its_last_value_even_when_same_ordinal_still_speaks(field, position):
    tl = sequence() if field in ('land', 'legacy_land') else tiny()
    take(tl, 'and share and build')
    clip = tl.clip('a-rocket')
    ref = f'"and" in s1 #{1 if position == 0 else 2}'
    # Delete either occurrence; even a still-valid ordinal must not inherit its reference.
    if field == 'on':
        clip.on(ref)
    elif field == 'until':
        clip._set_start(0.0)
        clip.until(ref)
    elif field in ('moment', 'word'):
        expr = {'moment': ref} if field == 'moment' else {'word': f's1:{position}', 'text': 'and'}
        intent.set_formula(clip.data, 'params.delay', {**expr, 'as': 'timeline_seconds'})
    else:
        head = tl.sequence('seq').steps[0]
        land = ref if field == 'land' else {'word': f's1:{position}', 'text': 'and'}
        intent.set_sequence_fit(head.data, {'land': land, 'lead': [2], 'race': [2], 'cycle': ['P'], 'then': 'R'})
    tl.resolve()
    before_clip = (clip.start, clip.end, copy.deepcopy(clip.params))
    before_steps = [(c.id, c.start, c.end) for c in tl.sequence('seq').steps] if field in ('land', 'legacy_land') else None
    take(tl, 'different share and build' if position == 0 else 'and share different build')
    tl.resolve()
    assert tl.orphans() and any('ORPHAN' in n for n in tl.orphans())
    if before_steps is None:
        assert (clip.start, clip.end, clip.params) == before_clip
    else:
        assert [(c.id, c.start, c.end) for c in tl.sequence('seq').steps] == before_steps
    # A later take saying it again cannot resurrect the deleted word's binding.
    take(tl, 'and share and build')
    tl.resolve()
    assert tl.orphans()


@pytest.mark.parametrize('count', [3, 4])
def test_repeated_and_phrases_follow_the_matched_phrase_not_its_ordinal(count):
    tl = tiny()
    old = 'And share And the LLMs And build' + (' And finish' if count == 4 else '')
    take(tl, old)
    clip = tl.clip('a-rocket').on('"And the LLMs" in s1')
    intent.set_formula(clip.data, 'params.delay', {'moment': 'after "And the LLMs" in s1', 'as': 'timeline_seconds'})
    tl.resolve()
    take(tl, 'And new ' + old)
    tl.resolve()
    assert clip.start == pytest.approx(tl.word('And the LLMs').start, abs=1 / tl.fps)
    assert clip.params['delay'] == pytest.approx(tl.word('LLMs').end)
    assert not tl.orphans()


def test_matched_legacy_word_formula_and_fit_land_are_rewritten_and_reported():
    tl = sequence()
    take(tl, 'and share it')
    clip = tl.clip('a-rocket')
    intent.set_formula(clip.data, 'params.delay', {'word': 's1:0', 'text': 'and', 'as': 'timeline_seconds'})
    head = tl.sequence('seq').steps[0]
    intent.set_sequence_fit(head.data, {'land': '"and" in s1 #1', 'lead': [1], 'race': [2], 'cycle': ['P'], 'then': 'R'})
    tl.resolve()
    take(tl, 'front and back and share it')
    tl.resolve()
    assert intent.formulas(clip.data)['params.delay']['word'] == 's1:3'
    assert clip.params['delay'] == pytest.approx(tl.word('and', n=2).start)
    assert intent.sequence_fit(tl.sequence('seq').steps[0].data)['land'] == '"and" in s1 #2'
    assert any('#1' in n and '#2' in n for n in tl.notes)


def test_authored_step_identity_and_another_hands_edit_survive_narration_refit():
    tl = sequence()
    seq = tl.sequence('seq')
    seq.fit(until='"viral"')
    tail = tl.clip('authored-tail')
    tail.params.update(x=987, foreign={'hand': 'B'})
    tail.data['app']['foreign'] = {'keep': True}
    take(tl, 'it slowly went viral')
    tl.resolve()
    assert [c.id for c in seq.steps] == ['a-plate', 'authored-tail']
    assert tail.params == {'x': 987, 'foreign': {'hand': 'B'}}
    assert tail.data['app']['foreign'] == {'keep': True}
    assert seq.steps[-1].end == pytest.approx(tl.word('viral').start, abs=1 / tl.fps)


def test_racing_terminal_member_keeps_id_params_and_foreign_metadata_on_refit():
    tl = sequence(racing=True)
    tl.resolve()
    tail = tl.sequence('seq').steps[-1]
    tail_id = tail.id
    tail.params['x'] = 987
    tail.data['app']['foreign'] = {'hand': 'B'}
    take(tl, 'it slowly went viral')
    tl.resolve()
    current_tail = tl.sequence('seq').steps[-1]
    assert current_tail.id == tail_id
    assert current_tail.params['x'] == 987
    assert current_tail.data['app']['foreign'] == {'hand': 'B'}
    assert tl.clip(tail_id).data is tail.data


def test_missing_referenced_step_is_a_conflict_instead_of_a_prefix_match():
    tl = sequence()
    cut = Cut(tl, {'index': 1, 'start': 0, 'end': 4, 'shot_id': 'A',
                   'sequence': {'clip_ids': ['a-plate', 'authored-tail']}})
    tail = tl.clip('authored-tail')
    replacement = copy.deepcopy(tail.data)
    replacement['id'] = 'authored-tail-other'
    tail.remove()
    tl.bundle['shots']['A']['internal_timeline']['clips'].append(replacement)
    with pytest.raises(TimelineEditError, match='MISSING_SEQUENCE_STEP.*authored-tail'):
        _ = cut.steps


@pytest.mark.parametrize('count', [3, 4])
@pytest.mark.parametrize('occurrence', [1, 2, 3])
def test_each_repeated_and_word_follows_its_surrounding_phrase(count, occurrence):
    tl = tiny()
    text = 'And share And the LLMs And build' + (' And finish' if count == 4 else '')
    take(tl, text)
    clip = tl.clip('a-rocket').on(f'"And" in s1 #{occurrence}')
    take(tl, 'And new ' + text)
    tl.resolve()
    assert intent.on(clip.data) == f'"And" in s1 #{occurrence + 1}'
    assert clip.start == pytest.approx(tl.word('And', n=occurrence + 1).start, abs=1 / tl.fps)
    assert any(f'#{occurrence} →' in note and f'#{occurrence + 1}' in note for note in tl.notes)


def test_generated_step_layout_is_deterministic_and_recompile_is_idempotent():
    from astrid.sdk.timeline.compiler import compile_timing

    declared = sequence(racing=True).bundle
    first = compile_timing(declared).bundle
    assert compile_timing(declared).bundle == first
    assert compile_timing(first).bundle == first


def test_missing_clip_handle_cannot_select_the_rest_of_its_sequence():
    tl = sequence()
    tail = tl.clip('authored-tail')
    tail.remove()
    with pytest.raises(TimelineEditError, match='MISSING_SEQUENCE_STEP.*authored-tail'):
        tl.sequence(tail)


@pytest.mark.parametrize('formula', [False, True])
def test_accepting_an_orphan_keeps_the_cache_without_resurrecting_its_spelling(formula):
    tl = tiny()
    take(tl, 'and share and build')
    clip = tl.clip('a-rocket')
    if formula:
        intent.set_formula(clip.data, 'params.delay', {'word': 's1:0', 'text': 'and', 'as': 'timeline_seconds'})
    else:
        clip.on('"and" in s1 #1')
    tl.resolve()
    take(tl, 'different share and build')
    tl.resolve()
    before = (clip.start, copy.deepcopy(clip.params))
    clip.keep()
    tl.resolve()
    assert not tl.orphans()
    assert (clip.start, clip.params) == before
    assert 'and' not in (intent.on(clip.data) or '')
    assert not intent.formulas(clip.data)


def test_fit_refuses_a_selected_step_that_disappeared_after_lookup():
    tl = sequence()
    tail = tl.clip('authored-tail')
    selected = tl.sequence(tail)
    tail.remove()
    with pytest.raises(TimelineEditError, match='MISSING_SEQUENCE_STEP.*authored-tail'):
        selected.fit(until='"viral"')


def test_orphaned_timing_formula_has_no_live_dependency_on_a_different_word():
    from astrid.sdk.timeline.compiler import compile_timing

    tl = tiny()
    # An orphan is a frozen cache, even if its old spelling now resolves to the
    # narration clip itself. It must not become a new graph edge/self-cycle.
    voice = tl.clip('a-vo')
    intent.set_formula(voice.data, 'at', {'moment': '"it"', 'as': 'timeline_seconds',
                                        'orphan': 'ORPHAN: removed spoken word'})
    value = voice.data['at']
    result = compile_timing(tl.bundle)
    output = next(c for c in result.bundle['shots']['A']['internal_timeline']['clips'] if c['id'] == voice.id)
    assert output['at'] == value


@pytest.mark.parametrize('formula', [False, True])
def test_word_orphan_survives_save_reload_and_a_take_that_says_the_word_again(tmp_path, formula):
    tl = tiny()
    take(tl, 'and share and build')
    clip = tl.clip('a-rocket')
    if formula:
        intent.set_formula(clip.data, 'params.delay', {'word': 's1:0', 'text': 'and', 'as': 'timeline_seconds'})
    else:
        clip.on('"and" in s1 #1')
    tl.resolve()
    take(tl, 'different share and build')
    tl.resolve()
    before = (clip.start, clip.end, copy.deepcopy(clip.params))
    path = tmp_path / 'orphan.checkout.json'
    tl.save(path, quiet=True)
    tl = Checkout.load(path)
    take(tl, 'and share and build')
    tl.resolve()
    clip = tl.clip('a-rocket')
    assert any('ORPHAN' in note for note in tl.orphans())
    assert (clip.start, clip.end, clip.params) == before
