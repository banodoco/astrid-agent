"""The one window-aware overrun rule (occurrence_window.window_overrun_ms), and child edits after a start trim.

Checks are ON everywhere: nothing here skips a validator. A child past the window is cropped; only a window
that runs past the shot's content is refused."""
from __future__ import annotations

import copy

import pytest

from astrid.core.timeline.authoring_bundle import AuthoringBundleError
from astrid.core.timeline.occurrence_window import cropped_ms, visible_span_ms, window_overrun_ms
from astrid.core.timeline.shot_composition_projection import _project_clip
from astrid.sdk.timeline._clip_scalar_commands import capture_clip_set
from astrid.sdk.timeline._command_state import CommandError, State
from astrid.sdk.timeline._trim_commands import capture_clip_trim
from astrid.sdk.timeline.capture import capture_move
from astrid.sdk.timeline.commands import execute
from astrid.sdk.timeline.native_placement_commands import capture_placement
from astrid.sdk.timeline.validation import validate_final_candidate
from astrid.sdk.timeline_checkout import Checkout
from tests.editing_conformance.test_recipe_occurrence_trim import FIRST, SECOND, populated, row, trim

CHILD = FIRST + '/clip:picture-1'


def stored_mapping():
    value = populated()
    value['placements'][0]['source_offset'] = {'start': 0, 'end': 0}
    return value


def start_trimmed(value=None):
    """occ-1 window [12 s, 16 s) showing shot time [2, 6]; shot-local zero sits at 10 s."""
    return trim(value or populated(), edge='start', to_ms=12000).bundle


def absolute(bundle, clip_id, ref=FIRST):
    """Absolute (timeline) start of an UNCROPPED child through the shared projection helper."""
    occurrence = row(bundle, ref)
    start = occurrence['placement']['start_ms'] / 1000
    shot = bundle['shots'][occurrence['shot_id']]['internal_timeline']['clips']
    raw = copy.deepcopy(next(c for c in shot if c['id'] == clip_id))
    out = _project_clip(raw, occurrence=occurrence, revision={'shot_id': 's', 'revision_id': 'r',
                        'internal_timeline_revision': {'revision_id': 'i'}}, occurrence_start=start,
                        occurrence_end=start + occurrence['duration_ms'] / 1000, occurrence_speed=1.0,
                        track_definitions=[{'id': 'picture', 'kind': 'visual'}], path='t')
    return None if out is None else out['at']


def window_origin_time(bundle, clip_id):
    """start - offset + tau, the definition itself, from stored fields."""
    occurrence = row(bundle)
    offset = occurrence['source_offset']
    offset = offset['start'] if isinstance(offset, dict) else offset
    clips = bundle['shots'][occurrence['shot_id']]['internal_timeline']['clips']
    tau = next(c for c in clips if c['id'] == clip_id)['at']
    return occurrence['placement']['start_ms'] / 1000 - offset / 1000 + tau


# ---- Task 1: one rule -------------------------------------------------------------------------

def test_end_trim_over_longer_content_commits_with_checks_on():
    original = populated()
    shorter = trim(original, edge='end', to_ms=14000).bundle   # 4 s window over 12 s of content
    validate_final_candidate(copy.deepcopy(shorter), baseline=original)
    validate_final_candidate(copy.deepcopy(shorter))             # and judged in full, no baseline


def test_start_trim_commits_with_checks_on():
    original = populated()
    trimmed = start_trimmed(original)
    validate_final_candidate(copy.deepcopy(trimmed), baseline=original)
    validate_final_candidate(copy.deepcopy(trimmed))


def test_a_real_overrun_is_still_refused_with_its_kind():
    trimmed = start_trimmed()
    with pytest.raises(CommandError) as raised:      # the handler: 13 s window over 12 s of content
        trim(populated(), SECOND, edge='end', to_ms=33000)
    assert raised.value.kind == 'occurrence_boundary'
    grown = copy.deepcopy(trimmed)
    grown['placements'][0]['duration_ms'] = 11000    # window [2 s, 13 s] of 12 s content
    with pytest.raises(AuthoringBundleError, match='half-open bounds'):
        validate_final_candidate(copy.deepcopy(grown))
    with pytest.raises(AuthoringBundleError, match='half-open bounds'):
        validate_final_candidate(copy.deepcopy(grown), baseline=trimmed)
    untrimmed = populated()
    longer = copy.deepcopy(untrimmed)
    longer['placements'][0]['duration_ms'] = 13000   # an end extension past the 12 s of content
    with pytest.raises(AuthoringBundleError, match='half-open bounds'):
        validate_final_candidate(longer, baseline=untrimmed)


def test_an_existing_overrun_the_change_does_not_touch_still_passes():
    base = start_trimmed()
    base['placements'][0]['duration_ms'] = 11000     # already overran (no baseline would refuse it)
    after = copy.deepcopy(base)
    after['parent']['config']['tracks'].append({'id': 'extra', 'kind': 'visual'})
    validate_final_candidate(after, baseline=base)


def test_helper_reports_crop_and_visible_intersection():
    assert window_overrun_ms(2000, 4000, 12000) == 0       # window inside content
    assert window_overrun_ms(2000, 11000, 12000) == 1000   # the real overrun, in ms
    assert window_overrun_ms(0, 13000, 12000) == 0         # untrimmed tail is not judged ...
    assert window_overrun_ms(0, 13000, 12000, previous=(0, 6000)) == 1000   # ... until it grows
    assert visible_span_ms(1000, 5000, 2000, 4000) == (2000, 5000)   # child [1,5] in window [2,6]
    assert visible_span_ms(0, 2000, 2000, 4000) is None              # entirely before the window
    assert cropped_ms(1000, 9000, 2000, 4000) == (1000, 3000)        # hidden before / after


def test_a_child_extending_past_the_window_is_cropped_not_refused():
    trimmed = start_trimmed()
    clips = trimmed['shots'][row(trimmed)['shot_id']]['internal_timeline']['clips']
    picture = next(c for c in clips if c['id'] == 'picture-1')
    picture.update(at=1, hold=9)                      # shot time [1, 10]; window shows [2, 6]
    validate_final_candidate(copy.deepcopy(trimmed))
    occurrence = row(trimmed)
    start = occurrence['placement']['start_ms'] / 1000
    assert absolute(trimmed, 'picture-1') == start                      # cropped to the window start
    span = visible_span_ms(1000, 10000, occurrence['source_offset'], occurrence['duration_ms'])
    assert span == (2000, 6000)
    occurrence_end = start + occurrence['duration_ms'] / 1000
    projected = _project_clip(copy.deepcopy(picture), occurrence=occurrence, revision={
        'shot_id': 's', 'revision_id': 'r', 'internal_timeline_revision': {'revision_id': 'i'}},
        occurrence_start=start, occurrence_end=occurrence_end, occurrence_speed=1.0,
        track_definitions=[{'id': 'picture', 'kind': 'visual'}], path='t')
    assert projected['hold'] == pytest.approx(4.0)                      # the visible intersection


def test_check_signature_agrees_on_start_trimmed_and_flags_only_a_real_overrun():
    trimmed = start_trimmed()
    sid = row(trimmed)['shot_id']

    def mine():  # occ-2's own untrimmed content overhang is a separate finding (see the end-card test)
        return {key: sig for key, sig in Checkout(copy.deepcopy(trimmed))._length_findings().items() if key[0] == sid}

    assert mine() == {}
    clips = trimmed['shots'][sid]['internal_timeline']['clips']
    next(c for c in clips if c['id'] == 'picture-1').update(at=1, hold=9)    # cropped, not flagged
    assert mine() == {}
    trimmed['placements'][0]['duration_ms'] = 11000                          # window [2, 13] of 12 s
    assert [key for key in mine()] == [(sid, sid)]
    blocking, existing = Checkout(copy.deepcopy(trimmed))._length_problems(Checkout(copy.deepcopy(trimmed)))
    assert blocking == [] and len(existing) >= 1                             # the same overrun in the base


# ---- Task 2: child edits after a start trim ---------------------------------------------------

@pytest.mark.parametrize('make', [populated, stored_mapping], ids=['scalar-offset', 'mapping-offset'])
def test_child_clip_set_commits_and_keeps_its_absolute_time(make):
    trimmed = start_trimmed(make())
    moved = execute(trimmed, [capture_placement(trimmed, FIRST, 'picture-1', 14, ownership='occurrence')]).bundle
    before = absolute(moved, 'picture-1')
    after = execute(moved, [capture_clip_set(moved, CHILD, values={'hold': 1.5})]).bundle
    assert State(after).entity(CHILD)['hold'] == 1.5
    assert absolute(after, 'picture-1') == before == window_origin_time(after, 'picture-1') == 14.0
    validate_final_candidate(copy.deepcopy(after), baseline=moved)


@pytest.mark.parametrize('make', [populated, stored_mapping], ids=['scalar-offset', 'mapping-offset'])
def test_child_placement_move_and_clip_move_land_at_the_right_absolute_time(make):
    trimmed = start_trimmed(make())
    for builder in (lambda b: capture_placement(b, FIRST, 'picture-1', 14, ownership='occurrence'),
                    lambda b: capture_move(b, CHILD, 14)):
        moved = execute(trimmed, [builder(trimmed)]).bundle
        assert State(moved).entity(CHILD)['at'] == 4                       # 14 s - (12 s - 2 s)
        assert absolute(moved, 'picture-1') == window_origin_time(moved, 'picture-1') == 14.0
        validate_final_candidate(copy.deepcopy(moved), baseline=trimmed)
        # Before the window (but inside the shot) is cropped there, not refused; the shot's own zero is the fault.
        cropped = execute(trimmed, [capture_move(trimmed, CHILD, 11)]).bundle
        assert State(cropped).entity(CHILD)['at'] == 1
        validate_final_candidate(copy.deepcopy(cropped), baseline=trimmed)
        with pytest.raises(CommandError) as raised:
            execute(trimmed, [capture_move(trimmed, CHILD, 9)])
        assert raised.value.kind == 'occurrence_boundary'


def test_clip_trim_cannot_address_a_child_clip_and_says_so():
    """clip.trim v1 accepts only `parent/clip:<id>`; an occurrence-owned child ref is refused plainly."""
    trimmed = start_trimmed()
    with pytest.raises(CommandError, match='does not match'):
        capture_clip_trim(trimmed, CHILD, in_s=0.5)


def test_the_last_shot_length_sync_follows_the_window_not_shot_local_zero():
    """A start-trimmed last shot keeps its window length: sync used to add source_offset to it."""
    from tests.editing_conformance.test_preexisting_violations_scoped import overrunning_head

    head = overrunning_head()
    head['placements'][0]['duration_ms'] = 10500                 # window [1.5 s, 12 s]: exactly the content
    tl = Checkout(copy.deepcopy(head))
    assert tl._sync_film_length() == []
    assert tl._row('film')['duration_ms'] == 10500


def test_check_does_not_shift_a_word_anchored_clip_in_a_start_trimmed_shot():
    """A word anchored clip stays put: word time is shot-local zero (start - source_offset) + the word's local time.
    words.py used the window start as zero, so check() moved the clip by exactly source_offset (1 s here)."""
    import json
    from pathlib import Path

    raw = json.loads((Path(__file__).parents[1] / 'fixtures' / 'timeline_editing' / 'tiny.json').read_text())
    trimmed = copy.deepcopy(raw)
    first = trimmed['placements'][0]  # what occurrence.trim start->1000 ms leaves behind
    first['placement']['start_ms'], first['duration_ms'], first['source_offset'] = 1000, 3000, 1000
    tl = Checkout(copy.deepcopy(trimmed))
    before = {c.address: (round(c.start, 4), round(c.end, 4)) for c in tl.clips()}
    tl.check()
    assert {c.address: (round(c.start, 4), round(c.end, 4)) for c in tl.clips()} == before


def _tiny(overrides):
    """tiny.json (shot A [0, 4 s), shot B [4, 8 s) last), with the given internal clip fields overridden."""
    import json
    from pathlib import Path

    raw = json.loads((Path(__file__).parents[1] / 'fixtures' / 'timeline_editing' / 'tiny.json').read_text())
    for (shot, clip_id), fields in overrides.items():
        next(c for c in raw['shots'][shot]['internal_timeline']['clips'] if c['id'] == clip_id).update(fields)
    return raw


def test_check_says_out_loud_that_a_trimmed_shot_crops_a_clip():
    raw = _tiny({('A', 'a-plate'): {'hold': 6.0}})                      # shot-local [0, 6]
    first = raw['placements'][0]                               # window [1 s, 4 s] of shot A after a start trim to 1 s
    first['placement']['start_ms'], first['duration_ms'], first['source_offset'] = 1000, 3000, 1000
    notes = Checkout(copy.deepcopy(raw))._crop_notes()
    assert [line for line in notes if 'plate' in line and 'runs 2.0 s past its shot and is cropped there' in line], notes
    report = Checkout(copy.deepcopy(raw)).check()
    lines = [line for line in report.problems if 'cropped there' in line]
    assert lines and not set(lines) & set(report.blocking), report.problems   # a note: the crop is visible, never blocking
    assert not any(line.startswith('length') for line in report.blocking)


def test_a_clip_past_the_last_shots_window_cannot_extend_the_film_in_one_path_and_be_cut_in_another():
    """Almost Ready's end card shape: the last (untrimmed) shot's picture runs past the shot's end.
    Authoring path: the film follows its last picture (sync), so the window grows to cover it: nothing is cut.
    A clip that is NOT a picture (a voice-over) cannot move the film's end, so the render cuts it at the stored end and the
    preview would show it: that disagreement blocks."""
    picture = _tiny({('B', 'b-plate'): {'hold': 9.0}})                   # ends at 4 + 9 = 13 s, window ends 8 s
    stale = Checkout(copy.deepcopy(picture))
    assert any('past its shot' in line for line in stale._length_problems()[0])   # unsynced: new, so it blocks
    synced = Checkout(copy.deepcopy(picture))
    assert synced._sync_film_length()                                              # the film now runs 13 s
    assert synced._row('B')['duration_ms'] == 9000
    assert synced._length_findings() == {}
    assert not [line for line in synced.check().problems if 'cropped' in line or 'past its shot' in line]

    voice = _tiny({('B', 'b-vo'): {'hold': 9.0}})                    # a voice-over 9 s long on a 4 s shot
    tl = Checkout(copy.deepcopy(voice))
    tl._sync_film_length()
    assert tl._row('B')['duration_ms'] == 4000                                     # not a picture: the film does not grow
    assert any('render cuts it off' in line for line in tl._length_problems()[0])         # a new one blocks
    assert any('render cuts it off' in line for line in tl.check().problems)              # and an old one is still said
