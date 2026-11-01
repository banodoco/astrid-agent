"""Whole canonical group removal through the native recipe and history inverse."""
from __future__ import annotations

import copy
from pathlib import Path

import pytest
from jsonschema import Draft202012Validator

from astrid.sdk.timeline._collections import AcceptedRecords
from astrid.sdk.timeline._command_state import ABSENT, CommandError, State
from astrid.sdk.timeline.commands import _SCHEMA, execute
from astrid.sdk.timeline.commit import _digest, bundle_from_document, document_from_bundle
from astrid.sdk.timeline.native_occurrence_delete import capture_occurrence_delete
from astrid.sdk.timeline.rebase import replay
from tests.editing_conformance.fixture_loader import load_fixture
from tests.editing_conformance.test_recipe_m1_commands import inverse, later
from tests.sdk.test_timeline_commit_provider_document import document_fixture

FIXTURE = Path(__file__).parent / 'fixtures/recipe_m2_occurrence_delete.json'


def delete_document(count=3):
    document = document_fixture(shared=False)
    parent = document['parent_revision']['payload']
    first = parent['occurrences'][0]
    first.update(source_offset=0, speed=1, metadata=None)
    first['provenance'] = {'group_migration': {
        'legacy_group': {'shotId': 'chapter-shot', 'trackId': 'picture', 'clipIds': ['clip-a', 'clip-b'],
                         'mode': 'images', 'name': None, 'opaque': [None, False, 0]},
        'members': [{'legacy_clip_id': identity, 'clip_id': identity} for identity in ('clip-a', 'clip-b')]}}
    parent['occurrences'] = [dict(copy.deepcopy(first), occurrence_id='occ-' + str(i)) for i in range(count)]
    document['parent_revision']['content_digest'] = _digest(parent)
    return document


def accepted(records, before, after):
    entry = {'records': {'commands': records}}
    if records[0]['command']['op'] == 'timeline.inverse':
        entry['records']['undo_of'] = records[0]['command']['args']['publications']
    return AcceptedRecords([entry], records, lambda _: (copy.deepcopy(before), copy.deepcopy(after)))


def reload(bundle, sources=()):
    return bundle_from_document(document_from_bundle(bundle, source_bundles=sources))


def portable_golden():
    original = bundle_from_document(delete_document())
    command = capture_occurrence_delete(original, 'occurrence:occ-1')
    plan = execute(original, [command])
    return {'bundle': original, 'select': command['select'], 'command': command,
            'after_bundle': plan.bundle, 'records': plan.records,
            'after_document': document_from_bundle(plan.bundle)}


@pytest.mark.parametrize('count,index', [(3, 0), (3, 1), (3, 2), (1, 0)])
def test_delete_reload_exact_guarded_inverse_redo_order_and_metadata(count, index):
    document = delete_document(count)
    original = bundle_from_document(document)
    before = copy.deepcopy(original)
    target = 'occurrence:occ-' + str(index)
    command = capture_occurrence_delete(original, target)
    plan = execute(original, [command])
    assert original == before
    assert plan.bundle['shots'] == before['shots']
    assert plan.bundle['parent'] == before['parent']
    assert command['resolved'] == [target] and command['created'] == []
    assert plan.records[0]['writes'] == [{'ref': target, 'path': [],
                                         'before': State(original).get(target, []), 'after': ABSENT}]
    assert all(s['owner'] == target for s in plan.records[0]['selections'])
    removed_document = document_from_bundle(plan.bundle)
    removed = bundle_from_document(removed_document)
    assert len(removed['placements']) == count - 1
    assert removed_document['dependency_manifest']['media'] == document['dependency_manifest']['media']
    if count == 1:
        assert removed_document['shot_revisions'] == removed_document['internal_timeline_revisions'] == []
    records = accepted(plan.records, original, removed)
    undone = inverse(removed, records)
    assert document_from_bundle(undone.bundle, source_bundles=(original, removed)) == document
    restored = reload(undone.bundle, (original, removed))
    redone = inverse(restored, accepted(undone.records, removed, restored))
    assert document_from_bundle(redone.bundle, source_bundles=(original, removed)) == removed_document
    assert replay(plan, original).records == plan.records


def test_other_occurrence_child_edit_and_metadata_do_not_conflict():
    original = bundle_from_document(delete_document())
    command = capture_occurrence_delete(original, 'occurrence:occ-1')
    plan = execute(original, [command])
    head = copy.deepcopy(original)
    State(head).entity('occurrence:occ-0/clip:clip-a')['hold'] = 3
    State(head).entity('occurrence:occ-2')['metadata'] = {'newer': None}
    intervening = [later('occurrence:occ-0/clip:clip-a', ['hold'], 3),
                   later('occurrence:occ-2', ['metadata'], {'newer': None})]
    rebased = replay(plan, head, intervening_records=intervening)
    removed = reload(rebased.bundle)
    undone = inverse(removed, accepted(rebased.records, head, removed), intervening)
    assert document_from_bundle(undone.bundle, source_bundles=(head, removed)) == document_from_bundle(head)


@pytest.mark.parametrize('change', ['membership', 'order', 'source', 'shot-content', 'metadata', 'duplicate'])
def test_changed_target_membership_source_or_identity_refuses_atomically(change):
    original = bundle_from_document(delete_document())
    command = capture_occurrence_delete(original, 'occurrence:occ-1')
    head = copy.deepcopy(original)
    state = State(head)
    source = state.config('occurrence:occ-1')
    if change == 'membership':
        source['clips'].pop()
    elif change == 'order':
        source['clips'].reverse()
    elif change == 'source':
        state.entity('occurrence:occ-1')['shot_id'] = head['placements'][0]['shot_id']
    elif change == 'shot-content':
        head['shots'][state.occurrence('occurrence:occ-1')['shot_id']]['payload']['metadata'] = None
    elif change == 'metadata':
        state.entity('occurrence:occ-1')['metadata'] = {}
    else:
        head['placements'].append(copy.deepcopy(head['placements'][1]))
    before = copy.deepcopy(head)
    with pytest.raises(CommandError):
        execute(head, [command])
    assert head == before


@pytest.mark.parametrize('ref,path', [
    ('occurrence:occ-1', []), ('occurrence:occ-1', ['shot_id']),
    ('occurrence:occ-1/clip:clip-a', ['asset']),
    ('occurrence:occ-1/source', ['clips']),
    ('occurrence:occ-1/shot', ['payload']),
])
def test_equal_state_and_aba_publication_history_refuses(ref, path):
    original = bundle_from_document(delete_document())
    plan = execute(original, [capture_occurrence_delete(original, 'occurrence:occ-1')])
    value = State(original).get(ref, path)['value']
    intervening = [later(ref, path, value)]
    with pytest.raises(CommandError, match='intervening'):
        inverse(plan.bundle, accepted(plan.records, original, plan.bundle), intervening)


# PLAN's settled equal-state rule, confirmed by the orchestrator's 09:42 decision,
# applies to occurrence.delete: ordinary replay accepts an equal final state when
# the stable identity is unchanged. The inverse test above remains history-strict.
@pytest.mark.parametrize('ref,path', [
    ('occurrence:occ-1', ['shot_id']),
    ('occurrence:occ-1/clip:clip-a', ['asset']),
    ('occurrence:occ-1/shot', ['payload']),
])
def test_equal_state_and_aba_publication_history_accepts_replay(ref, path):
    original = bundle_from_document(delete_document())
    plan = execute(original, [capture_occurrence_delete(original, 'occurrence:occ-1')])
    value = State(original).get(ref, path)['value']
    rebased = replay(plan, original, intervening_records=[later(ref, path, value)])
    assert rebased.commands == plan.commands
    assert rebased.records == plan.records
    assert rebased.bundle == plan.bundle
    assert [row['occurrence_id'] for row in rebased.bundle['placements']] == ['occ-0', 'occ-2']
    assert State(rebased.bundle).entity('occurrence:occ-1', optional=True) is None


@pytest.mark.parametrize('select', ['parent', 'shot:chapter-shot', 'shot:picture', 'occ-1:clip-a',
                                    'occurrence:occ-1/clip:clip-a', 'occurrence:missing'])
def test_explicit_single_occurrence_identity_only(select):
    with pytest.raises(CommandError):
        capture_occurrence_delete(bundle_from_document(delete_document()), select)


def test_legacy_group_refuses_with_migration_needed_and_no_mutation():
    original = bundle_from_document(delete_document())
    original['parent']['config']['pinnedShotGroups'] = [{'shotId': 'chapter-shot', 'trackId': 'picture',
                                                       'clipIds': ['clip-a', 'clip-b']}]
    before = copy.deepcopy(original)
    with pytest.raises(CommandError) as error:
        capture_occurrence_delete(original, 'occurrence:occ-1')
    assert error.value.kind == 'migration_needed'
    assert original == before


def test_inverse_requires_authenticated_order_and_source_boundary():
    original = bundle_from_document(delete_document(1))
    plan = execute(original, [capture_occurrence_delete(original, 'occurrence:occ-0')])
    removed = reload(plan.bundle)
    with pytest.raises(CommandError) as error:
        inverse(removed, plan.records)
    assert error.value.kind == 'history_unavailable'


def test_closed_schema_and_portable_native_capture_golden():
    fixture = load_fixture(FIXTURE)
    assert portable_golden() == fixture
    validator = Draft202012Validator(_SCHEMA)
    validator.validate(fixture['command'])
    for field, value in [('args', {'delete_shared_content': True}), ('intent', {'ownership': 'shared'}),
                         ('version', '2')]:
        assert list(validator.iter_errors(dict(fixture['command'], **{field: value})))
    # The group artifact adds only new closed definitions/dispatch, preserving Bundle 2 definitions.
    assert _SCHEMA['$defs']['ClipDeleteArgs'] == {'type': 'object', 'properties': {}, 'additionalProperties': False}


@pytest.mark.parametrize('metadata', ['absent', None, {}, {'explicit': None, 'ordered': [False, 0, []]}])
def test_exact_opaque_and_tagged_metadata_survive_last_consumer_history_inverse(metadata):
    original = bundle_from_document(delete_document(1))
    row = State(original).entity('occurrence:occ-0')
    if metadata == 'absent':
        row.pop('metadata')
    else:
        row['metadata'] = copy.deepcopy(metadata)
    source = State(original).config('occurrence:occ-0')
    source['clips'][0]['app'] = None
    source['clips'][1].pop('app')
    source['opaque'] = {'literal': 'occurrence:occ-0', 'nullable': None}
    document = document_from_bundle(original)
    # Reopen to give the authored fixture its real immutable baseline.
    original = bundle_from_document(document)
    plan = execute(original, [capture_occurrence_delete(original, 'occurrence:occ-0')])
    removed = reload(plan.bundle)
    undone = inverse(removed, accepted(plan.records, original, removed))
    assert document_from_bundle(undone.bundle, source_bundles=(original, removed)) == document
