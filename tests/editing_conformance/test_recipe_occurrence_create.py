"""Empty shot birth through the public recipe, then ordinary child authoring."""
from __future__ import annotations

import copy

import pytest

from astrid.sdk.timeline._command_state import ABSENT, CommandError, State
from astrid.sdk.timeline.capture import capture_track_create
from astrid.sdk.timeline.commands import execute
from astrid.sdk.timeline.commit import (
    _digest,
    bundle_from_document,
    document_from_bundle,
)
from astrid.sdk.timeline.native_occurrence_create import capture_occurrence_create
from astrid.sdk.timeline.native_occurrence_media_commands import (
    capture_occurrence_add_media,
)
from astrid.sdk.timeline.rebase import replay
from tests.editing_conformance.test_recipe_m1_commands import inverse, later
from tests.editing_conformance.test_recipe_occurrence_duplicate import accepted

OWNER = 'occurrence:first-occurrence'


def empty_bundle(*, fps=None):
    payload = {'config': {'clips': [], 'tracks': []}, 'registry': {},
               'clips': [], 'occurrences': []}
    if fps is not None:
        payload['config']['output'] = {'fps': fps, 'width': 1920, 'unknown': [None, False]}
    parent = {'revision_id': 'bootstrap', 'payload': payload, 'content_digest': _digest(payload)}
    empty = bundle_from_document({
        'schema_version': 1, 'project_id': 'project', 'timeline_id': 'film',
        'parent_revision': parent, 'shot_revisions': [], 'internal_timeline_revisions': [],
        'dependency_manifest': {'shots': [], 'internal_timelines': [], 'media': [], 'documents': []}})
    return execute(empty, [capture_track_create(empty, 'parent', {
        'id': 'picture', 'kind': 'visual', 'role': 'output', 'opaque': None})]).bundle


def birth(bundle, **changes):
    args = {'name': 'First chapter', 'track': 'picture', 'on': 0, 'duration': 10,
            'occurrence_id': 'first-occurrence', 'shot_id': 'first-shot', **changes}
    return capture_occurrence_create(bundle, **args)


@pytest.mark.parametrize('fps', [None, {'numerator': 30000, 'denominator': 1001}])
def test_birth_exact_roots_capture_replay_reload_inverse_redo(fps):
    original = empty_bundle(fps=fps)
    before = copy.deepcopy(original)
    command = birth(original, on=1)
    plan = execute(original, [command])
    assert original == before
    assert plan.records[0]['created'] == [
        {'ref': 'shot:first-shot', 'kind': 'shot', 'owner': None},
        {'ref': OWNER, 'kind': 'occurrence', 'owner': 'parent'}]
    assert command['created'] == ['shot:first-shot', OWNER]
    assert all(write['before'] == ABSENT for write in plan.records[0]['writes'])
    assert [s['key'] for s in command['selections']] == ['collection.occurrences', 'collection.tracks']
    assert plan.bundle['source_mapping'] == original['source_mapping']
    shot = State(plan.bundle).entity('shot:first-shot')
    assert shot == {'payload': {'name': 'First chapter', 'items': []}, 'internal_timeline': {
        'output': copy.deepcopy(original['parent']['config'].get('output', {})),
        'tracks': [], 'clips': [], 'registry': {'assets': {}}}}
    occurrence = State(plan.bundle).occurrence(OWNER)
    assert occurrence['placement']['start_ms'] == pytest.approx(1001 if fps else 1000)
    assert occurrence['duration_ms'] == 10000
    assert replay(plan, original).records == plan.records
    document = document_from_bundle(plan.bundle)
    reloaded = bundle_from_document(document)
    assert document_from_bundle(reloaded) == document
    undone = inverse(reloaded, accepted(plan.records, original, reloaded))
    assert document_from_bundle(undone.bundle, source_bundles=(original, reloaded)) == document_from_bundle(original)
    redone = inverse(undone.bundle, accepted(undone.records, reloaded, undone.bundle))
    assert document_from_bundle(redone.bundle, source_bundles=(original, reloaded)) == document


def test_birth_then_existing_track_and_media_commands_in_one_batch():
    from tests.editing_conformance.test_recipe_occurrence_media import inputs

    original = empty_bundle()
    first = birth(original)
    born = execute(original, [first]).bundle
    track = capture_track_create(born, OWNER, {'id': 'picture', 'kind': 'visual'})
    tracked = execute(born, [track]).bundle
    _, _, pin, verified = inputs('image')
    row = {'id': 'image', 'clipType': 'image', 'asset': pin['asset'],
           'track': 'picture', 'at': 0, 'hold': 2, 'opaque': [None, False]}
    add = capture_occurrence_add_media(tracked, OWNER, row, pin, verified_media=verified)
    plan = execute(original, [first, track, add], verified_media=verified)
    assert State(plan.bundle).entity(OWNER + '/clip:image') == row
    undone = inverse(plan.bundle, plan.records)
    assert undone.bundle == original
    assert inverse(undone.bundle, accepted(undone.records, plan.bundle, undone.bundle)).bundle == plan.bundle
    assert replay(plan, original, verified_media=verified).bundle == plan.bundle


@pytest.mark.parametrize('damage', ['track', 'tracks-order', 'occurrences', 'output', 'legacy', 'shot', 'occurrence'])
def test_original_guards_refuse_atomically(damage):
    original = empty_bundle()
    command = birth(original)
    head = copy.deepcopy(original)
    if damage == 'track':
        head['parent']['config']['tracks'][0]['opaque'] = {}
    elif damage == 'tracks-order':
        head['parent']['config']['tracks'].insert(0, {'id': 'other', 'kind': 'visual'})
    elif damage == 'output':
        head['parent']['config']['output'] = {'fps': 24}
    elif damage == 'legacy':
        head['parent']['config']['pinnedShotGroups'] = [{'id': 'legacy'}]
    else:
        created = execute(original, [birth(original, occurrence_id='other', shot_id='other')]).bundle
        head = created
        if damage == 'shot':
            command = birth(original, shot_id='other')
        if damage == 'occurrence':
            command = birth(original, occurrence_id='other')
    before = copy.deepcopy(head)
    with pytest.raises(CommandError):
        execute(head, [command])
    assert head == before


def test_unrelated_scalar_rebases_and_later_noop_or_aba_dependency_write_blocks_inverse():
    original = empty_bundle()
    plan = execute(original, [birth(original)])
    head = copy.deepcopy(original)
    head['parent']['unknown'] = [None, False]
    result = replay(plan, head, intervening_records=[later('parent', ['unknown'], [None, False])])
    assert result.bundle['parent']['unknown'] == [None, False]
    lane = original['parent']['config']['tracks'][0]
    for writes in ([later('parent/track:picture', [], lane)],
                   [later(OWNER, ['duration_ms'], 10000)]):
        with pytest.raises(CommandError, match='intervening'):
            inverse(plan.bundle, plan.records, writes)


def test_later_child_or_occurrence_dependency_prevents_birth_undo():
    original = empty_bundle()
    plan = execute(original, [birth(original)])
    tracked = execute(plan.bundle, [capture_track_create(plan.bundle, OWNER, {'id': 'child', 'kind': 'visual'})])
    with pytest.raises(CommandError):
        inverse(tracked.bundle, plan.records, tracked.records)
    dependent = copy.deepcopy(plan.bundle)
    dependent['placements'].append({**copy.deepcopy(dependent['placements'][0]), 'occurrence_id': 'dependent'})
    with pytest.raises(CommandError, match='surviving'):
        inverse(dependent, plan.records)


@pytest.mark.parametrize('change', [
    {'occurrence_id': ' '}, {'shot_id': 'shot/bad'}, {'shot_id': 'x::y'},
    {'duration': 0}, {'duration': True}, {'on': -1}, {'on': float('inf')}, {'track': 'missing'},
])
def test_closed_birth_inputs_refuse_before_mutation(change):
    original = empty_bundle()
    before = copy.deepcopy(original)
    with pytest.raises(CommandError):
        birth(original, **change)
    assert original == before


def test_birth_quantizer_overflow_is_permanent_and_does_not_mutate_capture():
    original = empty_bundle()
    before = copy.deepcopy(original)
    with pytest.raises(CommandError) as error:
        birth(original, on=1e308)
    assert error.value.kind == 'unsupported_timing'
    assert 'retry' not in str(error.value).lower()
    assert original == before


def test_append_birth_inverse_preserves_existing_order_and_opaque_content():
    original = empty_bundle()
    existing = execute(original, [birth(original, occurrence_id='existing', shot_id='existing')]).bundle
    State(existing).entity('occurrence:existing')['opaque'] = [None, False]
    plan = execute(existing, [birth(existing)])
    assert [row['occurrence_id'] for row in plan.bundle['placements']] == ['existing', 'first-occurrence']
    assert inverse(plan.bundle, plan.records).bundle == existing
