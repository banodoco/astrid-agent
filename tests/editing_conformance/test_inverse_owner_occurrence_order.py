"""Nested occurrence order and owner-bound immutable image continuations."""
from __future__ import annotations

import copy

import pytest

from astrid.sdk.timeline._command_state import ABSENT, CommandError, present
from astrid.sdk.timeline._commit_patch import inverse_address_resolver
from astrid.sdk.timeline._scalar_commands import capture_clip_set_params
from astrid.sdk.timeline.commit import bundle_from_document
from tests.editing_conformance.test_doc_patch_v2 import change, patch, step, submit
from tests.editing_conformance.test_provider_step_undo_v2 import undo
from tests.editing_conformance.test_recipe_occurrence_delete import delete_document
from tests.sdk.test_timeline_commit_provider import CommitRejected, DetachedRealm
from tests.sdk.test_timeline_commit_provider import runtime_abi as runtime_abi
from tests.sdk.test_timeline_occurrence_delete_provider import capture


def current(realm):
    return bundle_from_document(realm.documents[realm.head['revision_id']])


def edit(realm, command, *, dry_run=False):
    return submit(realm, command, label='Owner and order', merge='fields', reads=[],
                  steps=[step('edit', 0, command)], dry_run=dry_run,
                  source={'via': 'python'}, rationale='Guarded inverse', group_id='owner-order')


def snapshot(realm):
    return copy.deepcopy((realm.head, realm.entries, realm.documents, realm.accepted, realm.prepared))


@pytest.mark.parametrize('index', [0, 1])
@pytest.mark.parametrize('selected', [False, True])
def test_nested_delete_recreation_keeps_exact_stage_order(index, selected):
    original = delete_document(3)
    realm = DetachedRealm(original)
    deleted = edit(realm, capture(realm, 'occurrence:occ-' + str(index)))
    removed = deleted['data']['canonical_document']
    for expected in (original, removed, original, removed):
        target = realm.entries[-1]
        command = undo(target['publication_id'], *(['edit'] if selected else []))
        history = copy.deepcopy(realm.entries)
        before = snapshot(realm)
        preview = edit(realm, command, dry_run=True)
        assert preview['data']['would_status'] == 'accepted' and preview['receipt'] is None
        assert snapshot(realm) == before
        result = edit(realm, command)
        assert result['data']['canonical_document'] == expected
        assert result['receipt']['result'] == result['data']
        assert realm.entries[:-1] == history
        records = realm.entries[-1]['records']
        record = records['commands'][0]
        stages = [binding['boundary'] for binding in record['bindings'] if binding['key'] == 'inverse.stage']
        assert stages[0]['effect_range'][0] == 0
        assert stages[-1]['effect_range'][1] == len(records['steps'][0]['patch'])
        assert stages[-1]['native_write_range'][1] == len(record['writes'])


def branched(first='occ-a', second='occ-b'):
    realm = DetachedRealm()
    original = copy.deepcopy(realm.documents[realm.head['revision_id']])
    command = capture_clip_set_params(current(realm), 'occurrence:' + first + '/clip:clip-a', values={'x': 1})
    landed = edit(realm, command)
    command = capture_clip_set_params(current(realm), 'occurrence:' + second + '/clip:clip-a', values={'y': 2})
    sibling = edit(realm, command)
    return realm, original, landed, sibling


@pytest.mark.parametrize('first,second', [('occ-a', 'occ-b'), ('occ-b', 'occ-a')])
@pytest.mark.parametrize('selected', [False, True])
def test_owner_bound_inverse_preserves_sibling_images_and_exact_redo(first, second, selected):
    realm, original, landed, sibling = branched(first, second)
    original_row = next(row for row in original['parent_revision']['payload']['occurrences']
                        if row['occurrence_id'] == first)
    sibling_document = sibling['data']['canonical_document']
    sibling_row = next(row for row in sibling_document['parent_revision']['payload']['occurrences']
                       if row['occurrence_id'] == second)
    history = copy.deepcopy(realm.entries)
    command = undo(landed['receipt']['receipt_id'], *(['edit'] if selected else []))
    before = snapshot(realm)
    assert edit(realm, command, dry_run=True)['data']['would_status'] == 'accepted'
    assert snapshot(realm) == before
    undone = edit(realm, command)
    document = undone['data']['canonical_document']
    assert document['parent_revision']['payload']['occurrences'] == (
        [original_row, sibling_row] if first == 'occ-a' else [sibling_row, original_row])
    assert original['shot_revisions'][0] in document['shot_revisions']
    assert original['internal_timeline_revisions'][0] in document['internal_timeline_revisions']
    for key in ('shot_revisions', 'internal_timeline_revisions'):
        assert all(image in document[key] for image in sibling_document[key]
                   if image not in landed['data']['canonical_document'][key])
    assert realm.entries[:-1] == history
    redone = edit(realm, undo(undone['receipt']['receipt_id']))
    assert redone['data']['canonical_document'] == sibling_document


@pytest.mark.parametrize('damage', ['owner', 'owners', 'physical_ref', 'ref', 'successor',
                                  'revision_id', 'content_digest', 'edge_before', 'edge_after',
                                  'step_id', 'evaluated_head', 'command_ordinal', 'phase', 'missing'])
def test_owner_continuation_forgery_refuses_without_writes(damage):
    realm, _, landed, _ = branched()
    record = realm.entries[1]['records']['commands'][0]
    binding = next(item for item in record['bindings']
                   if item['key'] == 'commit.materialization' and item['physical_ref'].startswith('internal-'))
    if damage == 'missing':
        record['bindings'].remove(binding)
    elif damage in ('evaluated_head', 'command_ordinal', 'phase'):
        binding['boundary'][damage] = 'forged'
    else:
        binding[damage] = 'forged'
    before = snapshot(realm)
    def boundary(entry):
        return tuple(bundle_from_document(realm.documents[revision]) for revision in (
            entry['previous']['revision_id'], entry['revision_id']))

    with pytest.raises(CommandError) as proof_error:
        resolver = inverse_address_resolver(realm.entries[1:], current(realm), load_boundary=boundary)
        read = record['reads'][0]
        resolver(read, read['ref'], read['path'])
    assert proof_error.value.kind == 'history_unavailable'
    assert snapshot(realm) == before
    with pytest.raises(CommitRejected) as error:
        edit(realm, undo(landed['receipt']['receipt_id']))
    assert error.value.failure['details']['reason'] == 'history_unavailable'
    assert snapshot(realm) == before


def test_physical_site_without_owner_cannot_choose_a_sibling_successor():
    realm, _, _, _ = branched()
    before = snapshot(realm)
    def boundary(entry):
        return tuple(bundle_from_document(realm.documents[revision]) for revision in (
            entry['previous']['revision_id'], entry['revision_id']))

    resolver = inverse_address_resolver(realm.entries[1:], current(realm), load_boundary=boundary)
    with pytest.raises(CommandError) as error:
        resolver({}, 'internal-revision:internal-1', ['clips', 'id:clip-a', 'params'])
    assert error.value.kind == 'binding_changed'
    assert snapshot(realm) == before


def test_declared_shared_input_read_cannot_be_retargeted_to_one_owner():
    realm = DetachedRealm()
    command = capture_clip_set_params(current(realm), 'occurrence:occ-a/clip:clip-a', values={'x': 1})
    landed = submit(realm, command, label='Read shared input', merge='fields', steps=[step('edit', 0, command)],
                    reads=[{'kind': 'field', 'ref': 'internal-revision:internal-1',
                            'path': ['clips', 'id:clip-a', 'params'], 'state': ABSENT}])
    edit(realm, capture_clip_set_params(current(realm), 'occurrence:occ-b/clip:clip-a', values={'y': 2}))
    before = snapshot(realm)
    with pytest.raises(CommitRejected) as error:
        edit(realm, undo(landed['receipt']['receipt_id']))
    assert error.value.failure['details']['reason'] in ('binding_changed', 'intervening_write', 'inverse_conflict')
    assert snapshot(realm) == before


@pytest.mark.parametrize('damage', ['order', 'native-range', 'effect-range', 'source', 'step', 'ordinal'])
def test_nested_occurrence_partition_forgery_refuses_without_writes(damage):
    realm = DetachedRealm(delete_document(3))
    landed = edit(realm, capture(realm, 'occurrence:occ-0'))
    undone = edit(realm, undo(landed['receipt']['receipt_id']))
    records = realm.entries[-1]['records']
    record = records['commands'][0]
    stage = next(binding['boundary'] for binding in record['bindings'] if binding['key'] == 'inverse.stage')
    if damage == 'order':
        effect = next(item for item in records['steps'][0]['patch'] if item['path'] == ['occurrences'])
        effect['value'].reverse()
    elif damage == 'native-range':
        stage['native_write_range'][0] += 1
    elif damage == 'effect-range':
        stage['effect_range'][1] = 0
    elif damage == 'source':
        stage['publication_id'] = 'foreign'
    elif damage == 'step':
        stage['step_id'] = 'foreign'
    else:
        stage['command_ordinals'] = [1]
    before = snapshot(realm)
    with pytest.raises(CommitRejected) as error:
        edit(realm, undo(undone['receipt']['receipt_id']))
    assert error.value.failure['details']['reason'] == 'history_unavailable'
    assert snapshot(realm) == before


@pytest.mark.parametrize('key', ['reads', 'dependencies'])
def test_caller_cannot_supply_inverse_guard_facts(key):
    realm, _, landed, _ = branched()
    command = undo(landed['receipt']['receipt_id'])
    command['args'][key] = []
    before = snapshot(realm)
    with pytest.raises(CommitRejected) as error:
        edit(realm, command)
    assert error.value.failure['details']['reason'] == 'invalid_edit_command'
    assert snapshot(realm) == before


@pytest.mark.parametrize('later', ['equal', 'aba', 'consumer'])
def test_owner_specific_continuation_keeps_later_write_and_consumer_guards(later):
    realm, _, landed, _ = branched()
    ref = 'occurrence:occ-a/clip:clip-a'
    if later == 'consumer':
        document = realm.documents[realm.head['revision_id']]
        row = document['parent_revision']['payload']['occurrences'][0]
        shot = next(image for image in document['shot_revisions'] if image['revision_id'] == row['shot_revision_id'])
        edit_command = patch(change(['unknown', 'consumer'], ABSENT, True))
        submit(realm, edit_command, reads=[{'kind': 'field', 'ref': 'internal-revision:'
            + shot['internal_timeline_revision_id'], 'path': ['clips', 'id:clip-a', 'params'],
            'state': present({'x': 1})}], label='Consume output', merge='fields',
            steps=[step('consumer', 0, edit_command)])
    else:
        for value in ([2, 1] if later == 'aba' else [1]):
            edit(realm, capture_clip_set_params(current(realm), ref, values={'x': value}))
    before = snapshot(realm)
    with pytest.raises(CommitRejected) as error:
        edit(realm, undo(landed['receipt']['receipt_id']))
    assert error.value.failure['details']['reason'] in ('intervening_write', 'inverse_conflict')
    assert snapshot(realm) == before
