"""occurrence.trim: standard NLE trim of one occurrence window, content anchored in timeline time."""
from __future__ import annotations

import copy

import pytest

from astrid.sdk.timeline._clip_scalar_commands import capture_clip_set
from astrid.sdk.timeline._command_state import CommandError, State
from astrid.sdk.timeline.capture import occurrence_start
from astrid.sdk.timeline.commands import execute, validate_command
from astrid.sdk.timeline.native_occurrence_trim import capture_occurrence_trim
from astrid.sdk.timeline.native_placement_commands import capture_placement
from astrid.sdk.timeline.rebase import replay
from astrid.sdk.timeline.validation import validate_final_candidate
from tests.editing_conformance.test_recipe_m1_commands import bundle, inverse
from tests.editing_conformance.test_recipe_occurrence_create import empty_bundle

FIRST, SECOND = 'occurrence:occ-1', 'occurrence:occ-2'


def populated():
    """occ-1 [10 s, 16 s) and occ-2 [20 s, 30 s) share lane `picture`; each shot holds 12 s of content
    (a 2 s picture clip at 0 and a 12 s voice clip)."""
    value = bundle()
    value['placements'][0]['duration_ms'] = 6000
    for row in value['placements']:
        for clip in value['shots'][row['shot_id']]['internal_timeline']['clips']:
            clip['hold'] = 2 if clip['id'] == 'picture-1' else 12
    return value


def row(value, ref=FIRST):
    return State(value).occurrence(ref)


def trim(value, ref=FIRST, **changes):
    return execute(value, [capture_occurrence_trim(value, ref.removeprefix('occurrence:'), **changes)])


def refused(value, kind, ref=FIRST, **changes):
    with pytest.raises(CommandError) as raised:
        trim(value, ref, **changes)
    assert raised.value.kind == kind, raised.value


def test_empty_document_refuses_the_missing_occurrence_by_name():
    with pytest.raises(CommandError, match='no such occurrence') as raised:
        trim(empty_bundle(), 'occurrence:ghost', edge='end', to_ms=1000)
    assert raised.value.kind == 'identity_conflict'


def test_end_trim_changes_only_the_duration():
    original = populated()
    plan = trim(original, edge='end', to_ms=14000)
    after, before = row(plan.bundle), row(original)
    assert after['duration_ms'] == 4000
    assert after['placement'] == before['placement'] and after['source_offset'] == 0
    assert row(plan.bundle, SECOND) == row(original, SECOND)
    assert [w['path'] for w in plan.records[0]['writes']] == [['duration_ms']]


def test_start_trim_later_moves_start_and_offset_together_and_keeps_the_end():
    plan = trim(populated(), edge='start', to_ms=12000)
    after = row(plan.bundle)
    assert (after['placement']['start_ms'], after['source_offset'], after['duration_ms']) == (12000, 2000, 4000)
    assert occurrence_start(after) * 1000 + after['duration_ms'] == 16000


def test_start_trim_earlier_reveals_content_down_to_offset_zero():
    trimmed = trim(populated(), edge='start', to_ms=12000).bundle
    after = row(trim(trimmed, edge='start', to_ms=11000).bundle)
    assert (after['placement']['start_ms'], after['source_offset'], after['duration_ms']) == (11000, 1000, 5000)
    refused(trimmed, 'occurrence_boundary', edge='start', to_ms=9000)  # would need offset -1 s


@pytest.mark.parametrize(('changes', 'kind'), [
    ({'edge': 'end', 'to_ms': 10050}, 'unsupported_timing'),       # 50 ms
    ({'edge': 'end', 'to_ms': 21000}, 'occurrence_boundary'),      # past 12 s of content and into occ-2
    ({'edge': 'start', 'to_ms': 16000}, 'unsupported_timing'),     # zero length
])
def test_boundaries_are_refused_with_their_kind(changes, kind):
    refused(populated(), kind, **changes)


def test_extent_and_the_previous_occurrence_are_protected():
    value = populated()
    refused(value, 'occurrence_boundary', SECOND, edge='end', to_ms=33000)   # 13 s of 12 s, nothing after it
    refused(value, 'occurrence_boundary', SECOND, edge='start', to_ms=15000)  # reveals before offset 0 / into occ-1


def test_the_schema_refuses_bad_args():
    for args in ({'edge': 'middle', 'to_ms': 1000}, {'edge': 'end', 'to_ms': -1}, {'edge': 'end'},
                 {'edge': 'end', 'to_ms': 1000, 'unknown': 1}, {'edge': 'end', 'to_ms': 1.5}):
        with pytest.raises(CommandError):
            validate_command({'schema_version': 1, 'op': 'occurrence.trim', 'version': '1', 'select': FIRST,
                              'args': {'occurrence_id': 'occ-1', **args}, 'intent': {'ownership': 'occurrence'}})


def test_an_existing_overrun_is_not_refused_when_the_trim_does_not_push_it_further():
    value = populated()
    value['placements'][0]['duration_ms'] = 13000  # past the 12 s of content
    value['placements'][1]['placement']['start_ms'] = 30000
    assert row(trim(value, edge='end', to_ms=22500).bundle)['duration_ms'] == 12500
    refused(value, 'occurrence_boundary', edge='end', to_ms=23500)


def test_start_and_at_ms_stay_consistent_when_the_row_carries_both():
    value = populated()
    value['placements'][0]['at_ms'] = 10000
    after = row(trim(value, edge='start', to_ms=12000).bundle)
    assert after['at_ms'] == after['placement']['start_ms'] == 12000
    assert occurrence_start(after) == 12.0  # raises when the two codecs disagree


def test_replay_and_inverse_restore_the_original_row():
    original = populated()
    plan = trim(original, edge='start', to_ms=12000)
    assert replay(plan, original).records == plan.records
    assert row(inverse(plan.bundle, plan.records).bundle) == row(original)


def test_a_later_edit_of_a_start_trimmed_shot_is_accepted():
    original = populated()
    trimmed = trim(original, edge='start', to_ms=12000).bundle
    validate_final_candidate(copy.deepcopy(trimmed), baseline=original)
    # content stays anchored in timeline time: picture-1 (shot time 0-2 s) is hidden; moving it
    # to 14 s lands at shot time 4 s
    moved = execute(trimmed, [capture_placement(trimmed, FIRST, 'picture-1', 14, ownership='occurrence')]).bundle
    assert State(moved).entity(FIRST + '/clip:picture-1')['at'] == 4
    validate_final_candidate(copy.deepcopy(moved), baseline=trimmed)
    with pytest.raises(CommandError) as raised:   # before the window starts is still refused
        execute(trimmed, [capture_placement(trimmed, FIRST, 'picture-1', 11, ownership='occurrence')])
    assert raised.value.kind == 'occurrence_boundary'
    # clip.set on a CHILD clip of the trimmed shot: hold 2 s at shot time 4 s fits the window [2, 6]
    child = FIRST + '/clip:picture-1'
    after = execute(moved, [capture_clip_set(moved, child, values={'hold': 1.5})]).bundle
    assert State(after).entity(child)['hold'] == 1.5
    validate_final_candidate(copy.deepcopy(after), baseline=moved)
    unrelated = copy.deepcopy(trimmed)
    unrelated['parent']['config']['tracks'].append({'id': 'extra', 'kind': 'visual'})
    validate_final_candidate(unrelated, baseline=trimmed)


def test_an_end_trim_hides_content_without_failing_the_final_validation():
    original = populated()
    shorter = trim(original, edge='end', to_ms=14000).bundle
    validate_final_candidate(copy.deepcopy(shorter), baseline=original)
    from astrid.core.timeline.authoring_bundle import AuthoringBundleError
    with pytest.raises(AuthoringBundleError, match='half-open bounds'):  # no baseline: judged in full
        validate_final_candidate(copy.deepcopy(shorter))
