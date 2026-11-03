"""Occurrence history replays exact rows at each sequential effect boundary."""
from __future__ import annotations

import copy

import pytest

from astrid.sdk.timeline._command_state import CommandError, present
from astrid.sdk.timeline._commit_patch import _managed_boundaries
from astrid.sdk.timeline._doc_patch import Closure
from astrid.sdk.timeline.commit import bundle_from_document
from tests.editing_conformance.test_doc_patch_occurrence_reorder import move, snapshot
from tests.editing_conformance.test_doc_patch_v2 import change, patch, submit
from tests.sdk.test_timeline_commit_provider import CommitRejected, DetachedRealm
from tests.sdk.test_timeline_commit_provider import runtime_abi as runtime_abi
from tests.sdk.test_timeline_commit_provider_document import document_fixture


@pytest.mark.parametrize('ordered', [False, True])
def test_occurrence_membership_read_ignores_move_but_order_read_conflicts(ordered):
    document = document_fixture()
    realm = DetachedRealm(document)
    base = copy.deepcopy(realm.head)
    submit(realm, patch(move(document)), reads=[])
    read = {'kind': 'collection', 'ref': 'parent', 'path': ['occurrences'],
            'members': ['occ-a', 'occ-b'], 'order': ['occ-a', 'occ-b'] if ordered else None}
    command = patch(change(['name'], present('Renamed Film'), 'Ordered Film'))
    before = snapshot(realm)
    if ordered:
        with pytest.raises(CommitRejected) as error:
            submit(realm, command, reads=[read], base=base)
        assert error.value.failure['details']['reason'] == 'selection_changed'
        assert snapshot(realm) == before
    else:
        result = submit(realm, command, reads=[read], base=base)
        assert result['data']['status'] == 'rebased'
        assert result['data']['canonical_document']['parent_revision']['payload']['occurrences'] == (
            list(reversed(document['parent_revision']['payload']['occurrences'])))


def test_history_replays_move_timing_and_move_at_their_sequential_boundaries():
    document = document_fixture()
    realm = DetachedRealm(document)
    base = copy.deepcopy(realm.head)
    staged = copy.deepcopy(document)
    rows = staged['parent_revision']['payload']['occurrences']
    rows.reverse()
    rows[0]['placement']['start_ms'] = 0
    submit(realm, patch(move(document), change(
        ['occurrences', 'occurrence_id:occ-b', 'placement', 'start_ms'], present(10000), 0),
        move(staged, 'occ-a')), reads=[])
    result = submit(realm, patch(change(['name'], present('Renamed Film'), 'Ordered Film')),
                    reads=[], base=base)
    assert result['data']['status'] == 'rebased'
    expected = document['parent_revision']['payload']['occurrences']
    expected[1]['placement']['start_ms'] = 0
    assert result['data']['canonical_document']['parent_revision']['payload']['occurrences'] == expected


@pytest.mark.parametrize('aba', [False, True])
def test_native_row_write_after_history_move_still_guards_stale_reorder(aba):
    realm = DetachedRealm()
    document = realm.documents[realm.head['revision_id']]
    submit(realm, patch(move(document)), reads=[])
    base = copy.deepcopy(realm.head)
    document = copy.deepcopy(realm.documents[base['revision_id']])
    path = ['occurrences', 'occurrence_id:occ-b', 'unknown', 'literal']
    submit(realm, patch(change(path, present('chapter-shot'), 'peer')), reads=[])
    write = realm.entries[-1]['records']['commands'][0]['writes'][0]
    assert write['ref'] == 'occurrence:occ-b' and write['path'] == ['unknown', 'literal']
    if aba:
        submit(realm, patch(change(path, present('peer'), 'chapter-shot')), reads=[])
    before = snapshot(realm)
    with pytest.raises(CommitRejected) as error:
        submit(realm, patch(move(document, after='occ-a')), reads=[], base=base)
    assert error.value.failure['details']['reason'] == 'value_conflict'
    assert snapshot(realm) == before


@pytest.mark.parametrize('damage', ['owner', 'pin', 'opaque', 'order', 'membership', 'anchor', 'output'])
def test_history_move_requires_exact_physical_row_order_and_output(damage):
    document = document_fixture()
    realm = DetachedRealm(document)
    submit(realm, patch(move(document)), reads=[])
    entry = copy.deepcopy(realm.entries[-1])
    effect = entry['records']['steps'][0]['patch'][0]
    if damage in ('owner', 'pin'):
        effect['before']['value'][{'owner': 'shot_id', 'pin': 'shot_revision_id'}[damage]] = 'foreign'
    elif damage == 'opaque':
        effect['before']['value']['unknown']['literal'] = 'forged'
    elif damage == 'order':
        effect['order_before'].reverse()
    elif damage == 'membership':
        effect['order_before'].pop()
    else:
        effect['after_id'] = 'foreign' if damage == 'anchor' else 'occ-a'
    before = Closure(bundle_from_document(document))
    after = Closure(bundle_from_document(realm.documents[realm.head['revision_id']]))
    original = copy.deepcopy((before.document, after.document))
    with pytest.raises(CommandError) as error:
        list(_managed_boundaries(entry, before, after))
    assert error.value.kind == 'history_unavailable'
    assert (before.document, after.document) == original
