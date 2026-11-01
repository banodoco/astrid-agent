"""Reviewed doc.patch lists merge disjoint edits and retain their own guards."""
from __future__ import annotations

import copy
import json

import pytest

from astrid_runtime_client import ApiError
from astrid.sdk import timeline
from astrid.sdk.timeline._command_state import ABSENT, present
from astrid.sdk.timeline.sync import CommitConflict
from tests.editing_conformance.test_doc_patch_v2 import change, patch, submit
from tests.editing_conformance.test_doc_patch_v2 import runtime_abi as runtime_abi  # noqa: F401
from tests.sdk.test_timeline_commit_provider import CommitRejected, DetachedRealm
from tests.sdk.test_timeline_sync import connected  # noqa: F401


def snapshot(realm):
    return copy.deepcopy((realm.head, realm.documents, realm.entries, realm.accepted))


def move_occurrence(document, identity, after_id):
    rows = document['parent_revision']['payload']['occurrences']
    row = next(row for row in rows if row['occurrence_id'] == identity)
    return change(['occurrences'], present(row), op='list.move', id=identity,
                  after_id=after_id, order_before=[item['occurrence_id'] for item in rows])


def insert_track(identity, after_id='picture'):
    return patch(change(['config', 'tracks'], ABSENT,
                        {'id': identity, 'kind': 'audio'}, op='list.insert', id=identity,
                        after_id=after_id, order_before=['picture']),
                 created=['parent/track:' + identity])


def test_sdk_reviewed_list_declares_empty_reads_for_fields_merge(connected):
    client, generated, path = connected
    editor = timeline.open('p', 't', client=client, outbox_path=path)
    command = patch(change(['clips', 'id:a', 'at'], present(0), 2))
    generated.refusal = ApiError(409, 'conflict', 'stop before provider execution', details={
        'status': 'conflict', 'reason': 'value_conflict', 'current': generated.head,
    })

    with pytest.raises(CommitConflict):
        editor.commit([command], label='Reviewed list')

    request = json.loads(generated.calls[-1][0])
    assert request['merge'] == 'fields'
    assert request['reads'] == []


def test_disjoint_reviewed_doc_patch_list_merges():
    realm = DetachedRealm()
    base = copy.deepcopy(realm.head)
    submit(realm, patch(change(['unknown', 'opaque'], present(['parent-1', None]),
                          ['reviewed', None])), reads=[], merge='fields')

    result = submit(realm, patch(change(['name'], present('Renamed Film'), 'Reviewed Film')),
                    base=base, reads=[], merge='fields')

    assert result['data']['status'] in {'merged', 'rebased'}
    payload = result['data']['canonical_document']['parent_revision']['payload']
    assert payload['name'] == 'Reviewed Film'
    assert payload['unknown']['opaque'] == ['reviewed', None]


def test_reviewed_list_before_value_guard_conflicts_after_peer_change():
    realm = DetachedRealm()
    base = copy.deepcopy(realm.head)
    submit(realm, patch(change(['name'], present('Renamed Film'), 'Peer Film')),
           reads=[], merge='fields')
    before = snapshot(realm)

    with pytest.raises(CommitRejected) as error:
        submit(realm, patch(change(['name'], present('Renamed Film'), 'Reviewed Film')),
               base=base, reads=[], merge='fields')

    assert error.value.failure['details']['reason'] in {'previous_value_mismatch', 'value_conflict'}
    assert snapshot(realm) == before


def test_reviewed_list_same_field_aba_still_conflicts():
    realm = DetachedRealm()
    base = copy.deepcopy(realm.head)
    submit(realm, patch(change(['name'], present('Renamed Film'), 'Peer Film')),
           reads=[], merge='fields')
    submit(realm, patch(change(['name'], present('Peer Film'), 'Renamed Film')),
           reads=[], merge='fields')
    before = snapshot(realm)

    with pytest.raises(CommitRejected) as error:
        submit(realm, patch(change(['name'], present('Renamed Film'), 'Reviewed Film')),
               base=base, reads=[], merge='fields')

    assert error.value.failure['details']['reason'] == 'value_conflict'
    assert snapshot(realm) == before


@pytest.mark.parametrize('guard', ['order', 'membership'])
def test_reviewed_list_order_and_membership_guards_still_conflict(guard):
    realm = DetachedRealm()
    base = copy.deepcopy(realm.head)
    document = realm.documents[base['revision_id']]
    if guard == 'order':
        peer = patch(move_occurrence(document, 'occ-b', None))
        reviewed = patch(move_occurrence(document, 'occ-b', None))
    else:
        peer = insert_track('audio')
        reviewed = insert_track('music')
    submit(realm, peer, reads=[], merge='fields')
    before = snapshot(realm)

    with pytest.raises(CommitRejected) as error:
        submit(realm, reviewed, base=base, reads=[], merge='fields')

    assert error.value.failure['details']['reason'] == 'selection_changed'
    assert snapshot(realm) == before


def test_reviewed_list_idless_array_guard_still_conflicts():
    realm = DetachedRealm()
    base = copy.deepcopy(realm.head)
    submit(realm, patch(change(['unknown', 'opaque'], present(['parent-1', None]),
                          ['peer', None])), reads=[], merge='fields')
    before = snapshot(realm)

    with pytest.raises(CommitRejected) as error:
        submit(realm, patch(change(['unknown', 'opaque'], present(['parent-1', None]),
                                 ['reviewed', None])), base=base, reads=[], merge='fields')

    assert error.value.failure['details']['reason'] in {'previous_value_mismatch', 'value_conflict'}
    assert snapshot(realm) == before
