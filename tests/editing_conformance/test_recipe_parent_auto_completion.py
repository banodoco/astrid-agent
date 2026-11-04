"""Parent completion keeps a literal birth with authoritative original timing reads."""
from __future__ import annotations

import copy

import pytest

from astrid.sdk.timeline._command_state import CommandError, State
from astrid.sdk.timeline.commands import execute
from astrid.sdk.timeline.commit import bundle_from_document, document_from_bundle
from astrid.sdk.timeline.native_auto_placement import auto_place_completion
from astrid.sdk.timeline.rebase import replay
from tests.editing_conformance.test_recipe_m1_commands import accepted_boundaries, inverse
from tests.editing_conformance.test_recipe_m2_parent_media import CHILD, edit, parent_inputs


def basis(kind='image'):
    document, _, pin, verified = parent_inputs(kind)
    return bundle_from_document(document), pin, verified


def build(bundle, pin, verified, **policy):
    return auto_place_completion(bundle, 'parent', 'born', pin, ownership='parent',
                                 track='overlay', verified_media=verified, **policy)


@pytest.mark.parametrize('kind', ['image', 'video'])
@pytest.mark.parametrize('policy,frame,seconds', [
    ({'anchor': 'ov-a'}, 60, 5),
    ({'anchor': 'missing'}, 210, 5),
    ({'anchor': 'missing', 'fallback_at': 22.25, 'fallback_track': 'overlay'}, 667, 5),
    ({'anchor': 'ov-a', 'duration': 3}, 60, None),
])
def test_exact_birth_replay_reload_inverse_redo_and_defaults(kind, policy, frame, seconds):
    bundle, pin, verified = basis(kind)
    original = copy.deepcopy(bundle)
    result = build(bundle, pin, verified, **policy)
    assert result['capture']['start_frame'] == frame
    expected_seconds = (3 if kind == 'video' else 5) if seconds is None else seconds
    assert result['capture']['seconds'] == expected_seconds
    [command] = result['commands']
    assert command['op'] == 'clip.add-media' and set(command['args']) == {'row', 'media'}
    assert command['created'] == ['parent/asset:reserved-new', CHILD]
    assert command['args']['media'] == pin
    plan = execute(bundle, [command], verified_media=verified)
    assert replay(plan, bundle, verified_media=verified).records == plan.records
    loaded = bundle_from_document(document_from_bundle(plan.bundle))
    undone = inverse(loaded, plan.records)
    assert undone.bundle['parent'] == bundle['parent']
    assert undone.bundle['shots'] == bundle['shots']
    assert undone.bundle['placements'] == bundle['placements']
    redone = inverse(undone.bundle, accepted_boundaries(undone.records, loaded, undone.bundle))
    assert document_from_bundle(redone.bundle) == document_from_bundle(plan.bundle)
    if kind == 'video':
        row = State(plan.bundle).entity(CHILD)
        assert (row['from'], row['to'], row['hold'], row['speed']) == (0, row['hold'], row['hold'], 1)
    assert bundle == original


@pytest.mark.parametrize('kind', ['image', 'video'])
@pytest.mark.parametrize('mode', ['anchor', 'lane-end', 'saved'])
@pytest.mark.parametrize('field', ['params', 'volume', 'opaque'])
def test_unrelated_clip_fields_rebase_without_changing_capture(kind, mode, field):
    bundle, pin, verified = basis(kind)
    policy = {'anchor': 'ov-a'} if mode == 'anchor' else (
        {'anchor': 'missing', 'fallback_at': 22.25} if mode == 'saved' else {})
    result = build(bundle, pin, verified, **policy)
    plan = execute(bundle, result['commands'], verified_media=verified)
    head = copy.deepcopy(bundle)
    edit(head, 'parent/clip:ov-a', **{field: [None, False, {'unrelated': 1}]})
    assert build(head, pin, verified, **policy) == result
    rebased = replay(plan, head, verified_media=verified)
    assert [{key: value for key, value in record.items() if key != 'derived_writes'}
            for record in rebased.records] == [
                {key: value for key, value in record.items() if key != 'derived_writes'}
                for record in plan.records]
    assert State(rebased.bundle).entity('parent/clip:ov-a') == State(head).entity('parent/clip:ov-a')
    assert inverse(rebased.bundle, rebased.records).bundle == head


@pytest.mark.parametrize('kind', ['image', 'video'])
@pytest.mark.parametrize('mode', ['anchor', 'lane-end', 'saved'])
@pytest.mark.parametrize('change', ['at', 'hold', 'track', 'fps', 'track-kind', 'track-role',
                                   'track-hidden', 'track-delete', 'anchor-insert', 'order'])
def test_relevant_queries_refuse_stale_replay_without_mutation(kind, mode, change):
    bundle, pin, verified = basis(kind)
    policy = {'anchor': 'ov-a'} if mode == 'anchor' else (
        {'anchor': 'missing', 'fallback_at': 22.25} if mode == 'saved' else {})
    result = build(bundle, pin, verified, **policy)
    plan = execute(bundle, result['commands'], verified_media=verified)
    head = copy.deepcopy(bundle)
    if change in ('at', 'hold', 'track'):
        edit(head, 'parent/clip:ov-a', **{change: {'at': 2, 'hold': 3, 'track': 'picture'}[change]})
    elif change == 'fps':
        State(head).config('parent')['output']['fps'] = 24
    elif change.startswith('track-'):
        track = State(head).entity('parent/track:overlay')
        if change == 'track-delete':
            State(head).config('parent')['tracks'].remove(track)
        else:
            field, value = {'track-kind': ('kind', 'audio'), 'track-role': ('role', 'input'),
                            'track-hidden': ('hidden', True)}[change]
            track[field] = value
    elif change == 'anchor-insert':
        for rows in (head['parent']['clips'], head['parent']['config']['clips']):
            rows.append({'id': 'missing', 'track': 'overlay', 'at': 8, 'hold': 1})
    else:
        head['parent']['clips'].reverse()
        head['parent']['config']['clips'].reverse()
    before = copy.deepcopy(head)
    if mode == 'saved' and change in ('at', 'hold', 'track'):
        # Saved literal time consumes neither lane extent nor anchor timing.
        rebased = replay(plan, head, verified_media=verified)
        assert State(rebased.bundle).entity(CHILD) == State(plan.bundle).entity(CHILD)
    else:
        with pytest.raises(CommandError):
            replay(plan, head, verified_media=verified)
    assert head == before


@pytest.mark.parametrize('kind', ['image', 'video'])
def test_saved_lane_and_time_win_and_deleted_saved_target_never_chooses_latest(kind):
    bundle, pin, verified = basis(kind)
    State(bundle).config('parent')['tracks'].append({'id': 'saved', 'kind': 'visual'})
    for policy, lane, frame in [
        ({'anchor': 'ov-a', 'preferred_track': 'saved', 'fallback_track': 'overlay', 'fallback_at': 22.25},
         'saved', 60),
        ({'anchor': 'missing', 'preferred_track': 'overlay', 'fallback_track': 'saved', 'fallback_at': 22.25},
         'saved', 667),
    ]:
        result = build(bundle, pin, verified, **policy)
        assert (result['capture']['lane'], result['capture']['start_frame']) == (lane, frame)
        head = copy.deepcopy(bundle)
        State(head).config('parent')['tracks'].remove(State(head).entity('parent/track:saved'))
        with pytest.raises(CommandError, match='saved placement track'):
            build(head, pin, verified, **policy)
        with pytest.raises(CommandError):
            execute(head, result['commands'], verified_media=verified)


def test_lane_end_guards_other_lane_membership_but_not_its_timing():
    bundle, pin, verified = basis()
    result = build(bundle, pin, verified)
    head = copy.deepcopy(bundle)
    edit(head, 'parent/clip:clip-a', at=100, opaque=None)
    assert build(head, pin, verified) == result
    execute(head, result['commands'], verified_media=verified)
    edit(head, 'parent/clip:clip-a', track='overlay')
    with pytest.raises(CommandError):
        execute(head, result['commands'], verified_media=verified)


@pytest.mark.parametrize('field', ['at', 'at_ms', 'track', 'hold', 'from', 'to', 'speed'])
def test_timing_default_presence_is_guarded(field):
    bundle, pin, verified = basis()
    result = build(bundle, pin, verified, anchor='ov-a')
    head = copy.deepcopy(bundle)
    row = State(head).entity('parent/clip:ov-a')
    changed = copy.deepcopy(row)
    if field in changed:
        changed.pop(field)
    else:
        changed[field] = None
    for rows in (head['parent']['clips'], head['parent']['config']['clips']):
        rows[rows.index(next(row for row in rows if row['id'] == 'ov-a'))] = copy.deepcopy(changed)
    with pytest.raises(CommandError) as error:
        execute(head, result['commands'], verified_media=verified)
    assert error.value.kind == 'dependency_changed'


def test_extra_read_snapshot_is_never_authority_and_required_birth_reads_cannot_be_removed():
    bundle, pin, verified = basis()
    [command] = build(bundle, pin, verified, anchor='ov-a')['commands']
    forged = copy.deepcopy(command)
    extra = next(read for read in forged['reads'] if read['ref'] == 'parent/clip:ov-a' and read['path'] == ['hold'])
    extra['state']['value'] = 3
    with pytest.raises(CommandError, match='original authored guard'):
        execute(bundle, [forged], verified_media=verified)
    forged = copy.deepcopy(command)
    forged['reads'] = [read for read in forged['reads'] if read['ref'] != 'parent/track:overlay']
    with pytest.raises(CommandError, match='authoritative capture'):
        execute(bundle, [forged], verified_media=verified)


@pytest.mark.parametrize('saved', ['', False, 'deleted'])
@pytest.mark.parametrize('present', [False, True])
def test_invalid_saved_lane_refuses_instead_of_choosing_the_default(saved, present):
    bundle, pin, verified = basis()
    policy = {'anchor': 'ov-a', 'preferred_track': saved} if present else (
        {'anchor': 'absent', 'fallback_track': saved, 'fallback_at': 22.25})
    with pytest.raises(CommandError, match='saved placement track'):
        build(bundle, pin, verified, **policy)
