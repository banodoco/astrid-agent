"""Occurrence ordering preserves the verified rows, owners and physical pins."""
from __future__ import annotations

import copy

import pytest

from astrid.sdk.timeline._command_state import ABSENT, CommandError, present
from astrid.sdk.timeline._doc_patch import apply_change
from astrid.sdk.timeline.commands import execute
from astrid.sdk.timeline.commit import bundle_from_document, document_from_bundle
from tests.editing_conformance.test_doc_patch_v2 import change, patch, step, submit
from tests.sdk.test_timeline_commit_provider import CommitRejected, DetachedRealm
from tests.sdk.test_timeline_commit_provider import runtime_abi as runtime_abi
from tests.sdk.test_timeline_commit_provider_document import document_fixture


def move(document, identity='occ-b', after=None):
    rows = document['parent_revision']['payload']['occurrences']
    return change(['occurrences'], present(next(row for row in rows if row['occurrence_id'] == identity)),
                  op='list.move', id=identity, after_id=after,
                  order_before=[row['occurrence_id'] for row in rows])


def snapshot(realm):
    return copy.deepcopy((realm.head, realm.documents, realm.entries, realm.accepted, realm.prepared))


def test_existing_provider_accepts_reorder_with_exact_rows_pins_and_receipt():
    document = document_fixture()
    original = copy.deepcopy(document)
    realm = DetachedRealm(document)
    call = patch(move(document))
    controls = {'reads': [], 'label': 'Reorder occurrences', 'merge': 'fields',
                'steps': [step('reorder', 0, call)]}
    before = snapshot(realm)
    preview = submit(realm, call, dry_run=True, **controls)
    assert snapshot(realm) == before
    assert preview['receipt'] is None and preview['data']['would_status'] == 'accepted'
    result = submit(realm, call, **controls)
    assert result['data']['status'] == 'accepted'
    assert result['receipt']['result'] == result['data']
    landed = result['data']['canonical_document']
    assert preview['data']['canonical_document'] == landed
    assert landed['parent_revision']['payload']['occurrences'] == list(reversed(
        document['parent_revision']['payload']['occurrences']))
    for key in ('shot_revisions', 'internal_timeline_revisions', 'dependency_manifest'):
        assert landed[key] == document[key]
    record = realm.entries[-1]['records']['commands'][0]
    assert len(record['writes']) == 1 and not record['created']
    assert record['writes'][0]['ref'] == 'parent'
    assert record['writes'][0]['path'] == ['occurrences']
    binding = next(row for row in record['bindings'] if row['key'] == 'doc.patch:0')
    assert binding['change'] == call['args']['changes'][0]
    assert binding['physical_ref'] == 'parent' and binding['revision_id'] == 'parent-1'
    assert realm.entries[-1]['records']['steps'][0]['patch'] == call['args']['changes']
    assert document == original


def test_sequential_reorders_and_keyed_timing_use_each_change_boundary():
    document = document_fixture()
    first = move(document)
    staged = copy.deepcopy(document)
    staged['parent_revision']['payload']['occurrences'].reverse()
    second = move(staged, 'occ-a')
    timing = change(['occurrences', 'occurrence_id:occ-b', 'placement', 'start_ms'], present(10000), 0)
    call = patch(first, second, timing)
    original = bundle_from_document(document)
    plan = execute(original, [call], commit_request={'reads': []})
    rows = document_from_bundle(plan.bundle)['parent_revision']['payload']['occurrences']
    assert [row['occurrence_id'] for row in rows] == ['occ-a', 'occ-b']
    assert rows[1]['placement']['start_ms'] == 0
    assert len(plan.records[0]['writes']) == 3
    assert original == bundle_from_document(document)


@pytest.mark.parametrize('damage', ['previous', 'shot_id', 'shot_revision_id', 'revision_id',
                                   'unknown', 'order', 'membership', 'missing', 'anchor', 'self'])
def test_reorder_operands_refuse_without_writes(damage):
    document = document_fixture()
    item = move(document)
    if damage == 'previous':
        item['before'] = ABSENT.copy()
    elif damage in ('shot_id', 'shot_revision_id', 'revision_id'):
        item['before']['value'][damage] = 'forged'
    elif damage == 'unknown':
        item['before']['value']['unknown']['literal'] = 'forged'
    elif damage == 'order':
        item['order_before'].reverse()
    elif damage == 'membership':
        item['order_before'].pop()
    elif damage == 'missing':
        item['id'] = 'foreign'
    else:
        item['after_id'] = 'foreign' if damage == 'anchor' else item['id']
    realm = DetachedRealm(document)
    before = snapshot(realm)
    with pytest.raises(CommitRejected) as error:
        submit(realm, patch(item), reads=[])
    assert error.value.failure['details']['reason'] == (
        'invalid_edit_command' if damage in ('anchor', 'self') else 'previous_value_mismatch')
    assert snapshot(realm) == before


@pytest.mark.parametrize('damage', ['insert', 'remove', 'set', 'retarget', 'owner', 'pin', 'root'])
def test_order_support_does_not_admit_managed_edge_writes(damage):
    document = document_fixture()
    rows = document['parent_revision']['payload']['occurrences']
    item = move(document)
    if damage == 'insert':
        item.update(op='list.insert', before=ABSENT.copy(), value=copy.deepcopy(rows[1]))
    elif damage == 'remove':
        item['op'] = 'list.remove'
        item.pop('after_id')
    elif damage == 'set':
        item = change(['occurrences'], present(rows), list(reversed(rows)))
    elif damage == 'root':
        parent = copy.deepcopy(document['parent_revision']['payload'])
        parent['occurrences'].reverse()
        item = change([], present(document['parent_revision']['payload']), parent)
    else:
        field = {'retarget': 'shot_revision_id', 'owner': 'shot_id', 'pin': 'revision_id'}[damage]
        row = copy.deepcopy(rows[1])
        row[field] = 'forged'
        item = change(['occurrences', 'occurrence_id:occ-b'], present(rows[1]), row)
    realm = DetachedRealm(document)
    before = snapshot(realm)
    with pytest.raises(CommitRejected) as error:
        submit(realm, patch(item), reads=[])
    assert error.value.failure['details']['reason'] == 'managed_closure_write'
    assert snapshot(realm) == before


def test_duplicate_identity_is_refused_before_installation():
    document = document_fixture()
    item = move(document)
    parent = copy.deepcopy(document['parent_revision']['payload'])
    parent['occurrences'][0]['occurrence_id'] = 'occ-b'
    original = copy.deepcopy(parent)
    with pytest.raises(CommandError) as error:
        apply_change(parent, item)
    assert error.value.kind == 'identity_conflict'
    assert parent == original


@pytest.mark.parametrize('aba', [False, True])
def test_same_row_peer_write_and_aba_refuse_without_writes(aba):
    document = document_fixture()
    realm = DetachedRealm(document)
    base = copy.deepcopy(realm.head)
    path = ['occurrences', 'occurrence_id:occ-b', 'unknown', 'literal']
    submit(realm, patch(change(path, present('chapter-shot'), 'peer')), reads=[])
    if aba:
        submit(realm, patch(change(path, present('peer'), 'chapter-shot')), reads=[])
    before = snapshot(realm)
    with pytest.raises(CommitRejected) as error:
        submit(realm, patch(move(document)), reads=[], base=base)
    assert error.value.failure['details']['reason'] == 'value_conflict'
    assert snapshot(realm) == before


@pytest.mark.parametrize('sibling', [False, True])
def test_reorder_merges_unread_peer_field_at_the_landing_head(sibling):
    document = document_fixture()
    realm = DetachedRealm(document)
    base = copy.deepcopy(realm.head)
    path = (['occurrences', 'occurrence_id:occ-a', 'unknown', 'literal'] if sibling
            else ['unknown', 'opaque'])
    previous = present('chapter-shot' if sibling else ['parent-1', None])
    submit(realm, patch(change(path, previous, 'peer')), reads=[])
    head = copy.deepcopy(realm.head)
    result = submit(realm, patch(move(document)), reads=[], base=base)
    assert result['data']['rebased_onto'] == head
    parent = result['data']['canonical_document']['parent_revision']['payload']
    assert [row['occurrence_id'] for row in parent['occurrences']] == ['occ-b', 'occ-a']
    assert (parent['occurrences'][1]['unknown']['literal'] if sibling else parent['unknown']['opaque']) == 'peer'


@pytest.mark.parametrize('damage', ['order', 'membership', 'row'])
def test_declared_reads_are_checked_at_the_post_move_step(damage):
    document = document_fixture()
    first = patch(move(document))
    second = patch(change(['name'], present('Renamed Film'), 'Ordered Film'))
    read = {'kind': 'collection', 'ref': 'parent', 'path': ['occurrences'],
            'members': ['occ-a', 'occ-b'], 'order': ['occ-b', 'occ-a']}
    realm = DetachedRealm(document)
    if damage == 'row':
        read = {'kind': 'field', 'ref': 'parent', 'path': ['occurrences', 'occurrence_id:occ-b'],
                'state': move(document)['before']}
    controls = {'reads': [], 'steps': [step('move', 0, first), step('name', 1, second, reads=[read])]}
    submit(realm, first, second, **controls)
    assert read in realm.entries[-1]['records']['steps'][1]['dependencies']
    if damage == 'order':
        read['order'].reverse()
    elif damage == 'membership':
        read.update(members=['occ-a'], order=['occ-a'])
    else:
        read['state']['value']['shot_revision_id'] = 'forged'
    controls['steps'][1]['reads'] = [read]
    realm = DetachedRealm(document)
    before = snapshot(realm)
    with pytest.raises(CommitRejected):
        submit(realm, first, second, **controls)
    assert snapshot(realm) == before


@pytest.mark.parametrize('aba', [False, True])
def test_intervening_order_and_order_aba_are_selection_conflicts(aba):
    document = document_fixture()
    realm = DetachedRealm(document)
    base = copy.deepcopy(realm.head)
    submit(realm, patch(move(document)), reads=[])
    if aba:
        head_document = realm.documents[realm.head['revision_id']]
        submit(realm, patch(move(head_document, 'occ-a')), reads=[])
    before = snapshot(realm)
    with pytest.raises(CommitRejected) as error:
        submit(realm, patch(move(document)), reads=[], base=base)
    assert snapshot(realm) == before
    assert error.value.failure['details']['reason'] == 'selection_changed'


def test_unread_parent_field_merges_an_intervening_occurrence_reorder():
    document = document_fixture()
    realm = DetachedRealm(document)
    base = copy.deepcopy(realm.head)
    submit(realm, patch(move(document)), reads=[])
    result = submit(realm, patch(change(['name'], present('Renamed Film'), 'Ordered Film')),
                    reads=[], base=base)
    parent = result['data']['canonical_document']['parent_revision']['payload']
    assert parent['name'] == 'Ordered Film'
    assert parent['occurrences'] == list(reversed(document['parent_revision']['payload']['occurrences']))
