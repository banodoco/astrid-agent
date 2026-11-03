"""Placement reuses clip.move writes with owner and complete binding guards."""
from __future__ import annotations

import copy
import json
from pathlib import Path

import pytest

from astrid.sdk.timeline._clip_commands import capture_clip_add
from astrid.sdk.timeline._command_state import ABSENT, CommandError, State
from astrid.sdk.timeline.capture import capture_move, capture_track_create
from astrid.sdk.timeline.commands import execute, validate_command
from astrid.sdk.timeline.commit import _digest, bundle_from_document, document_from_bundle
from astrid.sdk.timeline.native_placement_commands import capture_placement
from astrid.sdk.timeline.rebase import replay
from tests.editing_conformance.fixture_loader import load_fixture
from tests.editing_conformance.test_recipe_m1_commands import inverse, later
from tests.sdk.test_timeline_commit_provider_document import document_fixture

FIXTURE = Path(__file__).parent / 'fixtures' / 'recipe_m2_placement.json'


def placement_document():
    document = document_fixture()
    for index, row in enumerate(document['parent_revision']['payload']['occurrences']):
        row.update(source_offset=0, speed=1)
        row['placement']['start_ms'] = 10000 + index * 10000
    document['parent_revision']['content_digest'] = _digest(document['parent_revision']['payload'])
    return document


def capture(bundle, owner='parent', clip_id='clip-a', on=4.004):
    return capture_placement(bundle, owner, clip_id, on,
                             ownership='parent' if owner == 'parent' else 'occurrence')


def test_parent_and_occurrence_golden_capture_replay_reload_inverse_redo():
    for case in load_fixture(FIXTURE)['cases']:
        original = bundle_from_document(case['input'])
        before = copy.deepcopy(original)
        command = capture_placement(original, **case['request'])
        assert command == case['command']
        ordinary = execute(original, [capture_move(original, command['select'], command['args']['on'])])
        validate_command(command)
        assert json.loads(json.dumps(command, allow_nan=False)) == command
        plan = execute(original, [command])
        assert plan.bundle == ordinary.bundle
        assert plan.records[0]['writes'] == ordinary.records[0]['writes']
        assert plan.records == case['records']
        assert document_from_bundle(plan.bundle) == case['output']
        assert replay(plan, original).records == plan.records
        assert original == before
        assert inverse(plan.bundle, plan.records).bundle == original
        reloaded = bundle_from_document(case['output'])
        undone = inverse(reloaded, plan.records)
        assert document_from_bundle(undone.bundle, source_bundles=[original]) == case['input']
        # Reload retains the compiler's occurrence materialization and revision
        # identities without a historical closure. Authored values still restore.
        for owner in ('parent', 'occurrence:occ-a', 'occurrence:occ-b'):
            assert State(undone.bundle).config(owner) == State(original).config(owner)
            assert State(undone.bundle).collection(owner, 'clip') == State(original).collection(owner, 'clip')
            assert State(undone.bundle).entity(owner + '/registry') == State(original).entity(owner + '/registry')
            if owner != 'parent':
                expected = copy.deepcopy(State(original).occurrence(owner))
                actual = copy.deepcopy(State(undone.bundle).occurrence(owner))
                expected.pop('shot_id')
                actual.pop('shot_id')
                assert actual == expected
        redone = inverse(undone.bundle, undone.records)
        assert document_from_bundle(redone.bundle) == case['output']
        assert command['created'] == []


@pytest.mark.parametrize('owner,on', [('parent', 4.004), ('occurrence:occ-a', 12.012)])
def test_preserves_raw_identity_source_media_order_opaque_and_sibling(owner, on):
    original = bundle_from_document(placement_document())
    ref = owner + '/clip:clip-a'
    target = State(original).entity(ref)
    target.pop('hold')
    target.update({'from': 2, 'to': 6, 'speed': 2})
    if owner == 'parent':
        original['parent']['config']['clips'] = copy.deepcopy(original['parent']['clips'])
    plan = execute(original, [capture(original, owner, on=on)])
    moved = State(plan.bundle).entity(ref)
    assert (moved['id'], moved['track'], moved['asset']) == ('clip-a', 'picture', 'picture')
    assert (moved['from'], moved['to'], moved['speed']) == (2, 6, 2)
    assert moved['app']['org.example'] == target['app']['org.example']
    assert moved['app']['astrid']['for'] == 2
    for scope in ('parent', 'occurrence:occ-a', 'occurrence:occ-b'):
        assert [row['id'] for row in State(plan.bundle).collection(scope, 'clip')] == [
            row['id'] for row in State(original).collection(scope, 'clip')]
        assert State(plan.bundle).entity(scope + '/registry') == State(original).entity(scope + '/registry')
        if scope != owner:
            assert State(plan.bundle).collection(scope, 'clip') == State(original).collection(scope, 'clip')
    assert plan.bundle['placements'] == original['placements']
    assert inverse(plan.bundle, plan.records).bundle == original
    record = plan.records[0]
    assert next(s for s in record['selections'] if s['key'] == 'move.target')['order'] == [ref]
    assert any(d['key'] == 'move.source' for d in record['dependencies'])
    assert all(w['ref'] == ref for w in record['writes'])


@pytest.mark.parametrize('app', [None, {}, {'astrid': None},
    {'on': None, 'until': 'end', 'until_film_end': True,
     'astrid': {'on': 'word:old:0', 'until': None, 'until_film_end': None}, 'opaque': [None]}])
def test_absence_null_and_relation_clears_roundtrip(app):
    original = bundle_from_document(placement_document())
    for rows in (original['parent']['clips'], original['parent']['config']['clips']):
        rows[0]['app'] = copy.deepcopy(app)
    plan = execute(original, [capture(original)])
    moved = State(plan.bundle).entity('parent/clip:clip-a')
    for container in (moved['app'], moved['app']['astrid']):
        assert all(key not in container for key in ('on', 'until', 'until_film_end'))
    writes = plan.records[0]['writes']
    assert any(w['after'] == ABSENT for w in writes)
    assert inverse(plan.bundle, plan.records).bundle == original


def test_disjoint_rebase_retains_original_capture_and_unrelated_values():
    original = bundle_from_document(placement_document())
    plan = execute(original, [capture(original, 'occurrence:occ-a', on=12.012)])
    head = copy.deepcopy(original)
    State(head).entity('occurrence:occ-a/clip:clip-a')['opaque'] = {'concurrent': [None]}
    State(head).entity('occurrence:occ-b/clip:clip-a')['at'] = 3
    head['parent']['name'] = 'Someone else renamed this'
    rebased = replay(plan, head)
    assert rebased.commands == plan.commands
    assert State(rebased.bundle).entity('occurrence:occ-a/clip:clip-a')['opaque'] == {'concurrent': [None]}
    assert inverse(rebased.bundle, rebased.records).bundle == head


@pytest.mark.parametrize('damage', ['target', 'track', 'media', 'window', 'map', 'blocker-order', 'absence'])
def test_relevant_conflicts_refuse_atomically(damage):
    original = bundle_from_document(placement_document())
    plan = execute(original, [capture(original, 'occurrence:occ-a', on=12.012)])
    head = copy.deepcopy(original)
    state = State(head)
    if damage == 'target':
        state.entity('occurrence:occ-a/clip:clip-a')['id'] = 'different'
    elif damage == 'track':
        state.entity('occurrence:occ-a/track:picture')['kind'] = 'audio'
    elif damage == 'media':
        state.entity('occurrence:occ-a/registry')['assets']['picture']['media_id'] = 'sha256:' + '2' * 64
    elif damage == 'window':
        state.entity('occurrence:occ-a/clip:clip-a')['hold'] = 3
    elif damage == 'map':
        state.occurrence('occurrence:occ-a')['speed'] = 2
    elif damage == 'blocker-order':
        head['placements'].reverse()
    else:
        state.entity('occurrence:occ-a/clip:clip-a')['at_ms'] = 1000
    before = copy.deepcopy(head)
    with pytest.raises(CommandError):
        replay(plan, head)
    assert head == before


@pytest.mark.parametrize('ref,path,value', [
    ('parent/clip:clip-a', ['at'], 1),
    ('parent/clip:clip-a', ['app', 'on'], None),
    ('parent/registry', ['assets', 'picture', 'media_id'], 'sha256:' + '1' * 64),
])
def test_same_final_value_allows_rebase_but_intervening_writes_block_undo(ref, path, value):
    original = bundle_from_document(placement_document())
    plan = execute(original, [capture(original)])
    intervening = [later(ref, path, value)]
    assert replay(plan, original, intervening_records=intervening).records == plan.records
    with pytest.raises(CommandError, match='intervening'):
        inverse(plan.bundle, plan.records, intervening)


@pytest.mark.parametrize('owner,ownership', [('parent', 'occurrence'), ('occurrence:occ-a', 'parent'),
    ('shot:chapter-shot', 'shared'), ('occurrence:occ-a', 'shared'), ('parent', 'shared'),
    (None, 'parent'), ('occurrence:bad/id', 'occurrence'), ('occurrence:bad::id', 'occurrence')])
def test_explicit_owner_required_and_shared_refused(owner, ownership):
    original = bundle_from_document(placement_document())
    with pytest.raises(CommandError) as error:
        capture_placement(original, owner, 'clip-a', 4, ownership=ownership)
    assert error.value.kind == 'unsupported_ownership'


@pytest.mark.parametrize('on', [None, True, -1, float('nan'), float('inf'), 'auto', {'frame': 120}])
def test_pool_auto_frame_objects_and_nonfinite_timing_refuse(on):
    original = bundle_from_document(placement_document())
    before = copy.deepcopy(original)
    with pytest.raises(CommandError):
        capture(original, on=on)
    assert original == before


@pytest.mark.parametrize('clip_id', ['', ' ', 'parent/clip:clip-a', 'clip-a::at', None, 'missing'])
def test_raw_existing_identity_required(clip_id):
    with pytest.raises(CommandError):
        capture(bundle_from_document(placement_document()), clip_id=clip_id)


def test_later_consumer_protects_created_track_when_undoing_placement_group():
    original = bundle_from_document(placement_document())
    track = capture_track_create(original, 'parent', {'id': 'new-track', 'kind': 'visual'})
    staged = execute(original, [track]).bundle
    row = {'id': 'new-clip', 'clipType': 'text', 'track': 'new-track', 'at': 0, 'hold': 1}
    add = capture_clip_add(staged, 'parent', row)
    staged = execute(staged, [add]).bundle
    placement = capture(staged, clip_id='new-clip')
    plan = execute(original, [track, add, placement])
    assert inverse(plan.bundle, plan.records).bundle == original
    head = copy.deepcopy(plan.bundle)
    head['parent']['config']['audio'] = [{'track_ref': 'parent/track:new-track', 'gain': 1}]
    before = copy.deepcopy(head)
    with pytest.raises(CommandError) as error:
        inverse(head, plan.records)
    assert error.value.kind == 'surviving_dependents'
    assert head == before


def test_shared_authored_root_refuses_at_capture_replay_and_inverse_even_on_another_lane():
    original = bundle_from_document(placement_document())
    plan = execute(original, [capture(original, 'occurrence:occ-a', on=12.012)])
    for baseline in (original, plan.bundle):
        head = copy.deepcopy(baseline)
        head['placements'][1]['shot_id'] = head['placements'][0]['shot_id']
        head['placements'][1]['placement']['track'] = 'unrelated-lane'
        head['placements'][1]['track'] = 'unrelated-lane'
        before = copy.deepcopy(head)
        with pytest.raises(CommandError):
            capture(head, 'occurrence:occ-a', on=12.012)
        with pytest.raises(CommandError):
            replay(plan, head)
        if baseline is plan.bundle:
            with pytest.raises(CommandError):
                inverse(head, plan.records)
        assert head == before


@pytest.mark.parametrize('owner', ['parent', 'occurrence:occ-a'])
@pytest.mark.parametrize('field', ['generationId', 'variantId', 'catalog_digest', 'object_id', 'opaque'])
def test_entire_existing_registry_selection_is_guarded_including_raw_path_aba(owner, field):
    original = bundle_from_document(placement_document())
    path = ['registry', 'assets', 'picture', field]
    entry = State(original).entity(owner + '/registry')['assets']['picture']
    entry[field] = 'original-pin'
    plan = execute(original, [capture(original, owner, on=4.004 if owner == 'parent' else 12.012)])
    head = copy.deepcopy(original)
    State(head).entity(owner + '/registry')['assets']['picture'][field] = 'changed-pin'
    with pytest.raises(CommandError):
        replay(plan, head)
    intervening = [later('parent' if owner == 'parent' else owner + '/source', path, 'original-pin')]
    assert replay(plan, original, intervening_records=intervening).records == plan.records
    with pytest.raises(CommandError, match='intervening'):
        inverse(plan.bundle, plan.records, intervening)


def test_equal_owner_membership_allows_rebase_but_aba_blocks_inverse():
    original = bundle_from_document(placement_document())
    plan = execute(original, [capture(original, 'occurrence:occ-a', on=12.012)])
    shot_id = original['placements'][0]['shot_id']
    for path in ([], ['shot_id']):
        value = {'occurrence_id': 'future', 'shot_id': shot_id} if not path else shot_id
        intervening = [later('occurrence:future', path, value)]
        assert replay(plan, original, intervening_records=intervening).records == plan.records
        with pytest.raises(CommandError, match='intervening'):
            inverse(plan.bundle, plan.records, intervening)
    unrelated = later('occurrence:future', [], {'occurrence_id': 'future', 'shot_id': 'other'})
    assert replay(plan, original, intervening_records=[unrelated]).bundle == plan.bundle


def test_schema_and_authoritative_recorder_refuse_forged_claims_and_intent():
    original = bundle_from_document(placement_document())
    command = capture(original, 'occurrence:occ-a', on=12.012)
    for field, replacement in [('intent', {'ownership': 'shared'}), ('version', '2'),
                               ('args', {'on': 12.012, 'timeline_frame': 360}),
                               ('created', ['occurrence:occ-a/clip:made-up']),
                               ('selections', []), ('reads', []), ('bindings', {})]:
        forged = copy.deepcopy(command)
        forged[field] = replacement
        with pytest.raises(CommandError):
            execute(original, [forged])


@pytest.mark.parametrize('damage', ['before-occurrence', 'cross-next-occurrence', 'retime', 'nested-clips',
                                   'last-occurrence-extent', 'missing-extent'])
def test_existing_move_limits_are_enforced_through_placement(damage):
    original = bundle_from_document(placement_document())
    on = 12.012
    owner = 'occurrence:occ-a'
    if damage == 'before-occurrence':
        on = 9
    elif damage == 'cross-next-occurrence':
        on = 19
    elif damage == 'retime':
        State(original).occurrence('occurrence:occ-a')['time_map'] = {'kind': 'piecewise'}
    elif damage == 'last-occurrence-extent':
        owner, on = 'occurrence:occ-b', 29
    elif damage == 'missing-extent':
        State(original).occurrence(owner).pop('duration_ms')
    else:
        State(original).entity('occurrence:occ-a/clip:clip-a')['children'] = []
    before = copy.deepcopy(original)
    with pytest.raises(CommandError):
        capture(original, owner, on=on)
    assert original == before
