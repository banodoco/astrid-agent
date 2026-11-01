"""Trim acceptance/replay through the native provider; Runtime ABI is stubbed."""
from __future__ import annotations

import copy
import json

import pytest

from astrid.sdk.timeline._scalar_commands import capture_clip_set_params, capture_track_set
from astrid.sdk.timeline._trim_commands import capture_clip_trim
from astrid.sdk.timeline.commit import _digest, bundle_from_document
from tests.editing_conformance.test_recipe_m2_clip_trim import REF, clip, media_document
from tests.sdk.test_timeline_commit_provider import CommitRejected, DetachedRealm, inverse
from tests.sdk.test_timeline_commit_provider import runtime_abi as runtime_abi


def current(realm):
    return bundle_from_document(realm.documents[realm.head['revision_id']])


def trim(realm, **args):
    return capture_clip_trim(current(realm), REF, **args)


def test_trim_committed_reload_exact_inverse_redo_keeps_registry_and_child_pins():
    original = media_document()
    realm = DetachedRealm(original)
    command = trim(realm, in_s=1, out_s=5)
    accepted = realm.call([command])
    assert accepted['data']['commands'] == [command]
    document = accepted['data']['canonical_document']
    reloaded = bundle_from_document(json.loads(json.dumps(document)))
    assert (clip(reloaded)['from'], clip(reloaded)['to'], clip(reloaded)['hold']) == (1, 5, 4)
    assert clip(reloaded)['at'] == 1 and clip(reloaded)['speed'] == 2
    for key in ('shot_revisions', 'internal_timeline_revisions', 'dependency_manifest'):
        assert document[key] == original[key]
    for key in ('registry', 'occurrences'):
        assert document['parent_revision']['payload'][key] == original['parent_revision']['payload'][key]
    assert all(item[0] not in ('transcript', 'config') for item in realm.reads)
    undo = realm.call([inverse([accepted['receipt']['receipt_id']])])
    assert undo['data']['canonical_document'] == original
    redo = realm.call([inverse([undo['receipt']['receipt_id']])])
    assert redo['data']['canonical_document'] == document


def test_move_then_trim_is_an_ordinary_native_writer_and_undo_keeps_the_move():
    realm = DetachedRealm(media_document())
    moved = realm.call([realm.move()])
    moved_document = moved['data']['canonical_document']
    accepted = realm.call([trim(realm, in_s=1, out_s=5)])
    row = clip(current(realm))
    assert row['at'] == 3.003
    assert row['app']['astrid']['fixed_start'] is True
    assert 'for' not in row['app']['astrid']
    assert row['app']['astrid']['opaque'] == [None, False]
    undone = realm.call([inverse([accepted['receipt']['receipt_id']])])
    assert undone['data']['canonical_document'] == moved_document


def test_stale_trim_rebases_over_disjoint_params_with_original_claims_and_guarded_inverse():
    realm = DetachedRealm(media_document(params={}))
    base = copy.deepcopy(realm.head)
    command = trim(realm, in_s=1, out_s=5)
    other = capture_clip_set_params(current(realm), REF, values={'opaque': [None, False, 0]})
    before_trim = realm.call([other])['data']['canonical_document']
    accepted = realm.call([command], base=base)
    assert accepted['data']['status'] == 'rebased'
    assert accepted['data']['commands'] == [command]
    assert clip(current(realm))['params'] == {'opaque': [None, False, 0]}
    later = realm.call([capture_track_set(current(realm), 'parent/track:picture', values={'label': 'other'})])
    undone = realm.call([inverse([accepted['receipt']['receipt_id']])])
    expected = copy.deepcopy(before_trim)
    # Compare payloads because a disjoint label gives the parent a new identity.
    expected['parent_revision']['payload']['config']['tracks'][0]['label'] = 'other'
    assert undone['data']['canonical_document']['parent_revision']['payload'] == expected['parent_revision']['payload']
    assert undone['data']['canonical_document']['dependency_manifest'] == later['data']['canonical_document']['dependency_manifest']


@pytest.mark.parametrize('aba', [False, True])
def test_same_value_and_aba_trim_publications_block_original_inverse(aba):
    realm = DetachedRealm(media_document())
    first = realm.call([trim(realm, in_s=1, out_s=5)])
    revision = realm.head['revision_id']
    if aba:
        realm.call([trim(realm, in_s=2, out_s=4)])
    again = realm.call([trim(realm, in_s=1, out_s=5)])
    assert realm.head['revision_id'] == revision
    assert first['receipt']['receipt_id'] != again['receipt']['receipt_id']
    record = realm.entries[-1]['records']['commands'][0]
    if not aba:
        assert all(write['before'] == write['after'] for write in record['writes'])
    before = copy.deepcopy((realm.head, realm.entries, realm.documents))
    with pytest.raises(CommitRejected) as error:
        realm.call([inverse([first['receipt']['receipt_id']])])
    assert error.value.failure['details']['reason'] == 'intervening_write'
    assert (realm.head, realm.entries, realm.documents) == before


def test_original_trim_dependency_change_and_forged_capture_are_atomic():
    realm = DetachedRealm(media_document())
    original = trim(realm, in_s=1, out_s=5)
    realm.call([trim(realm, in_s=2, out_s=4)])
    before = copy.deepcopy((realm.head, realm.entries, realm.documents))
    with pytest.raises(CommitRejected):
        realm.call([original])
    assert (realm.head, realm.entries, realm.documents) == before
    forged = trim(realm, in_s=0, out_s=10)
    forged['reads'] = []
    with pytest.raises(CommitRejected) as error:
        realm.call([forged])
    assert error.value.failure['details']['reason'] == 'capture_mismatch'
    assert (realm.head, realm.entries, realm.documents) == before


def test_missing_duration_refuses_without_fetch_or_partial_acceptance():
    document = media_document()
    payload = document['parent_revision']['payload']
    del payload['registry']['assets']['picture']['duration']
    document['parent_revision']['content_digest'] = _digest(payload)
    realm = DetachedRealm(document)
    before = copy.deepcopy((realm.head, realm.entries, realm.documents))
    command = {'schema_version': 1, 'op': 'clip.trim', 'version': '1', 'select': REF,
               'args': {'in_s': 1, 'out_s': 5}}
    with pytest.raises(CommitRejected, match='registry source duration'):
        realm.call([command])
    assert (realm.head, realm.entries, realm.documents) == before
    assert realm.prepared == realm.accepted == []


@pytest.mark.parametrize('changes', [{'args': {'in_s': -1}}, {'args': {'out_s': 11}},
    {'args': {'in_s': 3, 'out_s': 3}}, {'args': {'in_s': 0, 'out_s': .0000001}},
    {'args': {'out_s': 5, 'clear': ['speed']}}, {'select': 'occurrence:occ-a/clip:clip-a'},
    {'intent': {'ripple': 'all'}}])
def test_late_bad_trim_cannot_partially_publish_prior_label(changes):
    realm = DetachedRealm(media_document())
    label = capture_track_set(current(realm), 'parent/track:picture', values={'label': 'unaccepted'})
    invalid = {'schema_version': 1, 'op': 'clip.trim', 'version': '1', 'select': REF,
               'args': {'in_s': 1, 'out_s': 5}, **changes}
    before = copy.deepcopy((realm.head, realm.entries, realm.documents))
    with pytest.raises(CommitRejected):
        realm.call([label, invalid])
    assert (realm.head, realm.entries, realm.documents) == before
    assert realm.prepared == realm.accepted == []
