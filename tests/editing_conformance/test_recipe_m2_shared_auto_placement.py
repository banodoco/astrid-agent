"""Shared-shot auto-placement: append, lane inheritance, start-frame shift over existing birth ops."""
from __future__ import annotations

import copy
import json
from pathlib import Path

import pytest

from astrid.sdk.timeline._command_state import CommandError, State
from astrid.sdk.timeline.commands import execute as _execute
from astrid.sdk.timeline.commands import validate_command
from astrid.sdk.timeline.commit import bundle_from_document, document_from_bundle
from astrid.sdk.timeline.native_auto_placement import auto_place_shared, unplace_shared
from astrid.sdk.timeline.native_shot_pool_commands import POOL, capture_shot_pool
from astrid.sdk.timeline.rebase import replay
from tests.editing_conformance.test_recipe_m1_commands import inverse as _inverse
from tests.editing_conformance.test_recipe_m2_auto_placement import GenuineTimeline
from tests.editing_conformance.test_recipe_occurrence_media import inputs
from tests.sdk.test_timeline_commit_provider import (
    inverse as inverse_call,
)

execute = _execute
inverse = _inverse

OWNER = 'occurrence:occ-a'
CHILD = OWNER + '/clip:placed'
FIXTURE = Path(__file__).parent / 'fixtures' / 'recipe_m2_shared_auto_placement.json'
# occ-a starts at 8 s at 30000/1001 fps (global frame 240). clip-a covers frames 270..330 and
# clip-b 390..450 on 'picture'; 'overlay' carries clip-c 270..510 (wider than the picture lane).
CLIP_A_START, CLIP_B_START, PICTURE_END, SHOT_END = 270, 390, 450, 510
CASES = {'image-append': ('image', None, None), 'video-append': ('video', None, None),
         'image-shift': ('image', CLIP_B_START, None), 'video-overlay-anchor': ('video', None, 'clip-c')}


@pytest.fixture
def genuine_runtime(request, tmp_path, monkeypatch):
    parameter = getattr(request.node, 'callspec', None)
    values = parameter.params if parameter is not None else {}
    name = values.get('name')
    kind = values.get('kind') or CASES[name][0]
    context = GenuineTimeline.create(tmp_path, monkeypatch, kind)
    try:
        yield context
    finally:
        context.close()


def build(kind, *, frame=None, anchor=None, wide=True, context=None, **kwargs):
    if context is None:
        document, _, new, verified = inputs(kind)
    else:
        context.prepare_fixture(wide=wide, shared=True)
        document, new, verified = context.document, context.new, context.verified
    bundle = bundle_from_document(document)
    state = State(bundle)
    if wide:
        state.occurrence(OWNER)['duration_ms'] = 20000
    internal = state.config(OWNER)
    if not any(track['id'] == 'overlay' for track in internal['tracks']):
        internal['tracks'].append({'id': 'overlay', 'kind': 'visual'})
    if not any(row['id'] == 'clip-c' for row in internal['clips']):
        internal['clips'].append({'id': 'clip-c', 'track': 'overlay', 'asset': 'picture', 'clipType': 'media',
                                  'at': 1, 'hold': 8})
    result = auto_place_shared(bundle, OWNER, 'placed', new, ownership='occurrence',
                               verified_media=verified, frame=frame, anchor=anchor, **kwargs)
    return document, bundle, result, verified


def case(name):
    kind, frame, anchor = CASES[name]
    return build(kind, frame=frame, anchor=anchor)


@pytest.mark.parametrize('name', CASES)
def test_portable_goldens(name):
    _, bundle, result, verified = case(name)
    plan = execute(bundle, result['commands'], verified_media=verified)
    golden = json.loads(FIXTURE.read_text())[name]
    assert golden == {'commands': result['commands'], 'capture': result['capture'],
                      'child': State(plan.bundle).entity(CHILD)}


@pytest.mark.parametrize('name', CASES)
@pytest.mark.integration
def test_exact_capture_replay_inverse_and_redo(name, genuine_runtime):
    kind, frame, anchor = CASES[name]
    _, bundle, result, verified = build(kind, frame=frame, anchor=anchor, context=genuine_runtime)
    before = copy.deepcopy(bundle)
    [command] = result['commands']
    validate_command(command)
    assert json.loads(json.dumps(result, allow_nan=False)) == result
    plan = execute(bundle, [command], verified_media=verified)
    assert bundle == before
    assert plan.commands == [command]
    assert replay(plan, bundle, verified_media=verified).records == plan.records
    undone = inverse(plan.bundle, plan.records)
    assert undone.bundle == before
    redone = inverse(undone.bundle, genuine_runtime.accepted_boundaries(
        undone.records, plan.bundle, undone.bundle, plan.commands))
    assert redone.bundle == plan.bundle


@pytest.mark.parametrize('kind', ['image', 'video'])
def test_append_goes_after_the_greatest_end_over_every_lane_with_the_four_second_hold(kind):
    _, bundle, result, verified = build(kind)
    capture = result['capture']
    assert (capture['resolution'], capture['start_frame'], capture['seconds']) == ('append', SHOT_END, 4)
    child = State(execute(bundle, result['commands'], verified_media=verified).bundle).entity(CHILD)
    assert child['at'] == pytest.approx(SHOT_END * 1001 / 30000 - 8)
    assert (child['clipType'], child['hold'], child['track']) == (kind, 4, 'picture')
    if kind == 'video':
        assert (child['from'], child['to'], child['speed']) == (0, 4, 1)


def test_append_to_an_empty_shot_starts_at_the_occurrence_start():
    _, bundle, _, verified = build('image')
    State(bundle).config(OWNER)['clips'].clear()
    result = auto_place_shared(bundle, OWNER, 'placed', verified[1]['selection'], ownership='occurrence',
                               verified_media=verified)
    assert result['capture']['start_frame'] == 240


def test_append_past_the_occurrence_window_is_refused_by_the_existing_boundary():
    # The window is fixed at migration; growing it needs an extent writer (see the design note).
    with pytest.raises(CommandError) as error:
        build('image', wide=False)
    assert error.value.kind == 'occurrence_boundary'


@pytest.mark.parametrize('kind', ['image', 'video'])
def test_lane_is_the_anchor_lane_when_the_anchor_is_in_the_shot_else_the_occurrence_lane(kind):
    _, bundle, result, verified = build(kind, anchor='clip-c')
    assert result['capture']['lane'] == 'overlay'
    assert State(execute(bundle, result['commands'], verified_media=verified).bundle).entity(CHILD)['track'] == 'overlay'
    for anchor in ('gone', None, 'clip-a'):
        other = build(kind, anchor=anchor)[2]['capture']
        assert (other['lane'], other['anchor']['state']) == (
            'picture', 'absent' if anchor in ('gone', None) else 'present')


def test_the_anchor_never_moves_the_start():
    assert build('image', anchor='clip-a')[2]['capture']['start_frame'] == SHOT_END


@pytest.mark.parametrize('kind', ['image', 'video'])
def test_an_occupied_start_frame_shifts_by_one_while_interval_overlap_is_allowed(kind):
    _, bundle, result, verified = build(kind, frame=CLIP_B_START)
    capture = result['capture']
    assert (capture['resolution'], capture['requested_frame'], capture['skipped'], capture['start_frame']) == (
        'explicit', CLIP_B_START, [CLIP_B_START], CLIP_B_START + 1)
    # A start inside clip-b's interval but not equal to any start is kept (legacy allows overlap).
    assert build(kind, frame=CLIP_B_START + 5)[2]['capture']['start_frame'] == CLIP_B_START + 5


def test_the_shift_chains_over_consecutive_taken_starts_and_clamps_negative_frames():
    _, bundle, first, verified = build('image', frame=CLIP_B_START)
    taken = execute(bundle, first['commands'], verified_media=verified).bundle
    again = auto_place_shared(taken, OWNER, 'second', first['capture']['media'], ownership='occurrence',
                              verified_media=verified, frame=CLIP_B_START)
    assert (again['capture']['skipped'], again['capture']['start_frame']) == (
        [CLIP_B_START, CLIP_B_START + 1], CLIP_B_START + 2)
    # A negative frame clamps to 0, which is before the occurrence start, so the boundary refuses.
    with pytest.raises(CommandError) as error:
        auto_place_shared(bundle, OWNER, 'early', first['capture']['media'], ownership='occurrence',
                          verified_media=verified, frame=-7)
    assert error.value.kind == 'occurrence_boundary'


@pytest.mark.parametrize('kind', ['image', 'video'])
def test_the_exact_completed_variant_is_bound_and_the_other_occurrence_is_untouched(kind):
    _, bundle, result, verified = build(kind)
    plan = execute(bundle, result['commands'], verified_media=verified)
    binding = State(plan.bundle).entity(OWNER + '/registry')['assets']['reserved-new']
    assert (binding['generationId'], binding['variantId']) == ('generation-new', 'variant-new')
    assert plan.bundle['parent'] == bundle['parent']
    assert State(plan.bundle).config('occurrence:occ-b') == State(bundle).config('occurrence:occ-b')


@pytest.mark.parametrize('kind', ['image', 'video'])
def test_an_unowned_shared_legacy_or_display_owner_is_refused_without_touching_input(kind):
    _, bundle, _, verified = build(kind)
    pin = verified[1]['selection']
    shared = copy.deepcopy(bundle)
    shared['placements'][1]['shot_id'] = shared['placements'][0]['shot_id']
    legacy = copy.deepcopy(bundle)
    legacy['parent']['config']['pinnedShotGroups'] = [{'shotId': 'g', 'clipIds': [], 'poolGenerationIds': []}]
    for head, owner, ownership, refusal in [
            (shared, OWNER, 'occurrence', 'unsupported_ownership'),
            (bundle, 'parent', 'occurrence', 'unsupported_ownership'),
            (bundle, OWNER + '::display', 'occurrence', 'unsupported_ownership'),
            (bundle, OWNER, 'shared', 'unsupported_ownership'),
            (legacy, OWNER, 'occurrence', 'migration_needed')]:
        before = copy.deepcopy(head)
        with pytest.raises(CommandError) as error:
            auto_place_shared(head, owner, 'placed', pin, ownership=ownership, verified_media=verified)
        assert error.value.kind == refusal
        assert head == before


def test_unverified_unsupported_or_too_short_media_is_refused():
    _, bundle, _, verified = build('image')
    pin = verified[1]['selection']
    with pytest.raises(CommandError):
        auto_place_shared(bundle, OWNER, 'placed', pin, ownership='occurrence', verified_media=[])
    audio = copy.deepcopy(verified)
    audio[1]['media_type'] = 'audio/fixture'
    with pytest.raises(CommandError) as error:
        auto_place_shared(bundle, OWNER, 'placed', pin, ownership='occurrence', verified_media=audio)
    assert error.value.kind == 'unsupported_media'
    # The legacy hold ignored media length; a native video row authors a 0..4 window, so it refuses.
    _, vbundle, result, vverified = build('video')
    short = copy.deepcopy(vverified)
    short[1]['duration_seconds'] = 3
    with pytest.raises(CommandError):
        execute(vbundle, result['commands'], verified_media=short)


def replays_conflict(kind, change, **request):
    _, bundle, result, verified = build(kind, **request)
    plan = execute(bundle, result['commands'], verified_media=verified)
    head = copy.deepcopy(bundle)
    change(State(head))
    with pytest.raises(CommandError) as error:
        replay(plan, head, verified_media=verified)
    assert error.value.kind == 'dependency_changed'
    return head, verified


@pytest.mark.parametrize('kind', ['image', 'video'])
def test_replay_conflicts_when_the_anchor_lane_changed_and_a_rebuild_inherits_the_new_lane(kind):
    # Was: the birth stayed on the old lane (overlay) while a fresh capture inherits picture.
    *_, head, verified = replays_conflict(
        kind, lambda state: state.entity(OWNER + '/clip:clip-c').update(track='picture'), anchor='clip-c')
    rebuilt = auto_place_shared(head, OWNER, 'placed', verified[1]['selection'], ownership='occurrence',
                                verified_media=verified, anchor='clip-c')
    assert rebuilt['capture']['lane'] == 'picture'


@pytest.mark.parametrize('kind', ['image', 'video'])
def test_replay_conflicts_when_the_shot_extent_grew_and_a_rebuild_appends_after_it(kind):
    # Was: append landed at the stale extent (510 vs a fresh 540).
    *_, head, verified = replays_conflict(
        kind, lambda state: state.entity(OWNER + '/clip:clip-c').update(hold=9))
    rebuilt = auto_place_shared(head, OWNER, 'placed', verified[1]['selection'], ownership='occurrence',
                                verified_media=verified)
    assert rebuilt['capture']['start_frame'] == SHOT_END + 30


@pytest.mark.parametrize('kind', ['image', 'video'])
def test_replay_conflicts_when_a_clip_moved_onto_the_shifted_start_and_a_rebuild_shifts_again(kind):
    # Was: 390 shifted to 391 at capture, another clip moved to 391, replay still created at 391.
    def occupy(state):
        state.entity(OWNER + '/clip:clip-a')['at'] = (CLIP_B_START + 1) * 1001 / 30000 - 8

    *_, head, verified = replays_conflict(kind, occupy, frame=CLIP_B_START)
    rebuilt = auto_place_shared(head, OWNER, 'placed', verified[1]['selection'], ownership='occurrence',
                                verified_media=verified, frame=CLIP_B_START)
    assert (rebuilt['capture']['skipped'], rebuilt['capture']['start_frame']) == (
        [CLIP_B_START, CLIP_B_START + 1], CLIP_B_START + 2)


@pytest.mark.parametrize('kind', ['image', 'video'])
def test_an_exact_retry_of_the_same_request_returns_the_same_result(kind):
    _, bundle, result, verified = build(kind, anchor='clip-c')
    plan = execute(bundle, result['commands'], verified_media=verified)
    assert replay(plan, bundle, verified_media=verified).records == plan.records
    again = auto_place_shared(bundle, OWNER, 'placed', verified[1]['selection'], ownership='occurrence',
                              verified_media=verified, anchor='clip-c')
    assert again == result


@pytest.mark.parametrize('kind', ['image', 'video'])
def test_a_birth_cannot_appear_through_both_occurrences_after_a_retarget(kind):
    # Was: add-media (video, and an image on an inherited lane) had no replay-time owner check.
    for anchor in (None, 'clip-c'):
        _, bundle, result, verified = build(kind, anchor=anchor)
        plan = execute(bundle, result['commands'], verified_media=verified)
        head = copy.deepcopy(bundle)
        head['placements'][1]['shot_id'] = head['placements'][0]['shot_id']
        with pytest.raises(CommandError) as error:
            replay(plan, head, verified_media=verified)
        # The recorded placement.owners selection catches the retarget before the handler re-runs.
        assert error.value.kind == 'selection_changed'


@pytest.mark.parametrize('kind', ['image', 'video'])
def test_replay_refuses_when_a_shot_clip_was_deleted_meanwhile_and_a_rebuild_appends_after_the_rest(kind):
    _, bundle, result, verified = build(kind, anchor='clip-c')
    plan = execute(bundle, result['commands'], verified_media=verified)
    head = copy.deepcopy(bundle)
    rows = State(head).config(OWNER)['clips']
    rows[:] = [row for row in rows if row['id'] != 'clip-c']
    with pytest.raises(CommandError):
        replay(plan, head, verified_media=verified)
    rebuilt = auto_place_shared(head, OWNER, 'placed', verified[1]['selection'], ownership='occurrence',
                                verified_media=verified, anchor='clip-c')
    assert (rebuilt['capture']['anchor']['state'], rebuilt['capture']['lane'], rebuilt['capture']['start_frame']) == (
        'absent', 'picture', PICTURE_END)


def test_unplace_removes_the_image_expression_keeps_the_binding_and_is_guarded():
    _, bundle, result, verified = build('image')
    placed = execute(bundle, result['commands'], verified_media=verified).bundle
    plan = execute(placed, unplace_shared(placed, OWNER, 'placed', ownership='occurrence')['commands'])
    assert 'placed' not in {row['id'] for row in State(plan.bundle).collection(OWNER, 'clip')}
    assert 'reserved-new' in State(plan.bundle).entity(OWNER + '/registry')['assets']
    assert inverse(plan.bundle, plan.records).bundle == placed


@pytest.mark.parametrize('kind', ['image', 'video'])
@pytest.mark.integration
def test_provider_accepts_shared_placement_with_exact_undo_and_redo(kind, genuine_runtime):
    _, bundle, result, verified = build(kind, context=genuine_runtime)
    realm = genuine_runtime
    created = realm.call(result['commands'], 'shared-placement')
    undo = realm.call([inverse_call([created['receipt']['receipt_id']])], 'shared-placement-undo')
    assert undo['data']['canonical_document'] != created['data']['canonical_document']
    redo = realm.call([inverse_call([undo['receipt']['receipt_id']])], 'shared-placement-redo')
    assert redo['data']['canonical_document'] == created['data']['canonical_document']
    assert document_from_bundle(bundle_from_document(created['data']['canonical_document'])) == (
        created['data']['canonical_document'])


def test_a_null_frame_records_pool_membership_without_a_clip():
    _, bundle, _, verified = build('image')
    result = auto_place_shared(bundle, OWNER, 'placed', verified[1]['selection'], ownership='occurrence',
                               verified_media=verified, pooled=True)
    plan = execute(bundle, result['commands'], verified_media=verified)
    assert State(plan.bundle).collection(OWNER, 'clip') == State(bundle).collection(OWNER, 'clip')
    assert result['capture']['resolution'] == 'pooled'


def test_keep_as_pooled_removes_the_clip_and_keeps_membership():
    _, bundle, result, verified = build('image')
    placed = execute(bundle, result['commands'], verified_media=verified).bundle
    unplaced = unplace_shared(placed, OWNER, 'placed', ownership='occurrence', keep_as_pooled=True,
                              media=verified[1]['selection'], verified_media=verified)
    plan = execute(placed, unplaced['commands'], verified_media=verified)
    assert 'placed' not in {row['id'] for row in State(plan.bundle).collection(OWNER, 'clip')}
    assert State(plan.bundle).config(OWNER)['app']['astrid']['pool'] == ['reserved-new']


def bound_case(kind, pool_state, **mode):
    _, bundle, _, verified = build(kind)
    pin = verified[1]['selection']
    State(bundle).config(OWNER)['registry']['assets'][pin['asset']] = {
        'media_id': pin['object_id'], 'type': kind,
        'generationId': pin['generation_id'], 'variantId': pin['variant_id']}
    if pool_state == 'empty':
        State(bundle).config(OWNER).setdefault('app', {}).setdefault('astrid', {})['pool'] = []
    result = auto_place_shared(bundle, OWNER, 'placed', pin, ownership='occurrence',
                               verified_media=verified, **mode)
    return bundle, result, verified


@pytest.mark.parametrize('kind', ['image', 'video'])
@pytest.mark.parametrize('mode', [{}, {'frame': CLIP_B_START}, {'anchor': 'clip-c'}])
@pytest.mark.parametrize('pool_state', ['absent', 'empty'])
def test_original_birth_refuses_a_native_pool_add_even_when_the_exact_alias_was_already_bound(
        kind, mode, pool_state):
    bundle, result, verified = bound_case(kind, pool_state, **mode)
    original = copy.deepcopy(result)
    plan = execute(bundle, result['commands'], verified_media=verified)
    assert len(plan.commands) == 1
    assert replay(plan, bundle, verified_media=verified).records == plan.records
    assert inverse(plan.bundle, plan.records).bundle == bundle
    pin = verified[1]['selection']
    added = execute(bundle, [capture_shot_pool(bundle, OWNER, 'add', pin, ownership='occurrence',
                                              verified_media=verified)], verified_media=verified)
    # Isolate pool membership: adding this already-bound alias creates no registry binding.
    assert [write['path'] for write in added.records[0]['writes']][-1] == POOL
    assert not added.records[0]['created']
    head = copy.deepcopy(added.bundle)
    with pytest.raises(CommandError) as error:
        replay(plan, head, intervening_records=added.records, verified_media=verified)
    assert error.value.kind == 'dependency_changed'
    assert head == added.bundle
    assert result == original
    assert State(head).get(OWNER + '/source', POOL)['value'] == [pin['asset']]
    assert CHILD not in {OWNER + '/clip:' + clip['id'] for clip in State(head).collection(OWNER, 'clip')}
    # This is a separate new capture, never a replacement for the original request.
    fresh = auto_place_shared(head, OWNER, 'placed', pin, ownership='occurrence',
                              verified_media=verified, **mode)
    assert fresh['capture']['promoted'] is True
    assert [command['op'] for command in fresh['commands']][1:] == ['shot.pool']
    assert fresh['commands'][1]['args']['action'] == 'remove'


@pytest.mark.parametrize('kind', ['image', 'video'])
@pytest.mark.parametrize('mode', [{}, {'frame': CLIP_B_START}, {'anchor': 'clip-c'}, {'pooled': True}])
@pytest.mark.parametrize('eligibility', ['absent', 'null', 'empty'])
def test_original_shared_request_refuses_a_changed_legacy_group_eligibility(kind, mode, eligibility):
    _, bundle, _, verified = build(kind)
    config = State(bundle).config('parent')
    if eligibility != 'absent':
        config['pinnedShotGroups'] = None if eligibility == 'null' else []
    result = auto_place_shared(bundle, OWNER, 'placed', verified[1]['selection'], ownership='occurrence',
                               verified_media=verified, **mode)
    original = copy.deepcopy(result)
    plan = execute(bundle, result['commands'], verified_media=verified)
    assert replay(plan, bundle, verified_media=verified).records == plan.records
    assert inverse(plan.bundle, plan.records).bundle == bundle
    # No native writer introduces legacy groups in this registry; use an explicit head fixture.
    head = copy.deepcopy(bundle)
    State(head).config('parent')['pinnedShotGroups'] = [
        {'shotId': 'legacy', 'clipIds': [], 'poolGenerationIds': []}]
    before = copy.deepcopy(head)
    with pytest.raises(CommandError) as error:
        replay(plan, head, verified_media=verified)
    assert error.value.kind == 'dependency_changed'
    with pytest.raises(CommandError) as error:
        auto_place_shared(head, OWNER, 'placed', verified[1]['selection'], ownership='occurrence',
                          verified_media=verified, **mode)
    assert error.value.kind == 'migration_needed'
    assert head == before
    assert result == original


@pytest.mark.parametrize('kind', ['image', 'video'])
@pytest.mark.parametrize('pool_state', ['absent', 'empty'])
def test_birth_reads_come_from_the_handler_and_other_owner_pool_edits_rebase(kind, pool_state):
    bundle, result, verified = bound_case(kind, pool_state)
    original = json.loads(json.dumps(result))
    command = result['commands'][0]
    plain = {key: command[key] for key in ('schema_version', 'op', 'version', 'select', 'args', 'intent')}
    plan = execute(bundle, [plain], verified_media=verified)
    assert plan.commands == result['commands']
    reads = plan.records[0]['reads']
    for ref, path in [(OWNER + '/source', POOL), ('parent', ['config', 'pinnedShotGroups'])]:
        assert {'ref': ref, 'path': path, 'state': State(bundle).get(ref, path)} in reads
    other = 'occurrence:occ-b'
    command = capture_shot_pool(bundle, other, 'add', verified[1]['selection'], ownership='occurrence',
                                verified_media=verified)
    added = execute(bundle, [command], verified_media=verified)
    added.bundle['parent']['name'] = 'Unrelated rename'
    State(added.bundle).config(OWNER).setdefault('app', {})['unrelated'] = True
    rebased = replay(plan, added.bundle, intervening_records=added.records, verified_media=verified)
    assert rebased.commands == result['commands']
    assert rebased.bundle['parent'] == added.bundle['parent']
    assert State(rebased.bundle).config(other) == State(added.bundle).config(other)
    assert inverse(rebased.bundle, rebased.records).bundle == added.bundle
    assert result == original
