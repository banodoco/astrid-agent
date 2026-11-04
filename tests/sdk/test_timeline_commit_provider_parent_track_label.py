"""Parent-track label acceptance through the native commit provider."""
from __future__ import annotations

import copy
import json

import pytest

from astrid.sdk.timeline._command_state import State
from astrid.sdk.timeline._track_commands import capture_track_set
from astrid.sdk.timeline.commit import bundle_from_document
from tests.sdk.test_timeline_commit_provider import (
    CommitRejected,
    DetachedRealm,
    inverse,
    runtime_abi,  # noqa: F401 - import registers the shared Runtime ABI fixture
)


def current(realm):
    return bundle_from_document(realm.documents[realm.head['revision_id']])


def test_move_then_parent_label_accepted_reload_inverse_preserves_newer_disjoint_move():
    realm = DetachedRealm()
    moved = realm.call([realm.move()])
    label = capture_track_set(current(realm), 'parent/track:picture', values={'label': 'Picture'})
    accepted = realm.call([label])
    reloaded = bundle_from_document(json.loads(json.dumps(accepted['data']['canonical_document'])))
    assert State(reloaded).entity('parent/track:picture')['label'] == 'Picture'
    assert State(reloaded).entity('parent/clip:clip-a')['at'] == State(
        bundle_from_document(moved['data']['canonical_document'])).entity('parent/clip:clip-a')['at']

    newer_move = realm.call([realm.move('parent/clip:clip-b', 8.008)])
    undone = realm.call([inverse([accepted['receipt']['receipt_id']])])
    document = undone['data']['canonical_document']
    restored = bundle_from_document(json.loads(json.dumps(document)))
    assert State(restored).entity('parent/clip:clip-a')['at'] == 3.003
    assert State(restored).entity('parent/clip:clip-b')['at'] == 8.008
    assert 'label' not in State(restored).entity('parent/track:picture')
    assert newer_move['data']['canonical_document']['parent_revision']['payload']['config']['clips'][1]['at'] == 8.008

    redone = realm.call([inverse([undone['receipt']['receipt_id']])])
    assert State(bundle_from_document(redone['data']['canonical_document'])).entity(
        'parent/track:picture')['label'] == 'Picture'


def test_parent_label_clear_and_empty_value_are_distinct_after_accepted_reload():
    realm = DetachedRealm()
    ref = 'parent/track:picture'
    empty = realm.call([capture_track_set(current(realm), ref, values={'label': ''})])
    assert State(current(realm)).entity(ref)['label'] == ''
    cleared = realm.call([capture_track_set(current(realm), ref, clear=['label'])])
    reloaded = bundle_from_document(json.loads(json.dumps(cleared['data']['canonical_document'])))
    assert 'label' not in State(reloaded).entity(ref)
    assert realm.call([inverse([cleared['receipt']['receipt_id']])])['data']['canonical_document'] == (
        empty['data']['canonical_document'])


def test_parent_label_original_claim_conflict_refuses_without_accepting():
    realm = DetachedRealm()
    original = capture_track_set(current(realm), 'parent/track:picture', values={'label': 'Original'})
    realm.call([capture_track_set(current(realm), 'parent/track:picture', values={'label': 'Other'})])
    before, count = copy.deepcopy(realm.head), len(realm.entries)
    with pytest.raises(CommitRejected) as error:
        realm.call([original])
    assert error.value.failure['details']['reason'] == 'dependency_changed'
    assert realm.head == before and len(realm.entries) == count


@pytest.mark.parametrize('aba', [False, True])
def test_parent_label_unchanged_publication_and_aba_refuse_guarded_undo(aba):
    realm = DetachedRealm()
    first = realm.call([capture_track_set(current(realm), 'parent/track:picture', values={'label': 'same'})])
    if aba:
        realm.call([capture_track_set(current(realm), 'parent/track:picture', values={'label': 'away'})])
    last = realm.call([capture_track_set(current(realm), 'parent/track:picture', values={'label': 'same'})])
    assert last['receipt']['receipt_id'] != first['receipt']['receipt_id']
    record = realm.entries[-1]['records']['commands'][0]
    if not aba:
        assert any(write['before'] == write['after'] for write in record['writes'])
    before = copy.deepcopy(realm.head)
    with pytest.raises(CommitRejected) as error:
        realm.call([inverse([first['receipt']['receipt_id']])])
    assert error.value.failure['details']['reason'] == 'intervening_write'
    assert realm.head == before
