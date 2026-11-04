"""The actual native provider accepts and reserves literal clip identities."""
from __future__ import annotations

import copy

import pytest

from astrid.sdk.timeline._clip_commands import capture_clip_add
from astrid.sdk.timeline.commit import bundle_from_document
from tests.editing_conformance.test_recipe_m2_clip_add import empty_document, literal_row
from tests.sdk.test_timeline_commit_provider import (
    CommitRejected,
    DetachedRealm,
    inverse,
    runtime_abi,  # noqa: F401 (shared autouse Runtime ABI fixture)
    track_create,
)


def test_first_clip_provider_closed_reload_reservations_inverse_redo():
    realm = DetachedRealm(empty_document())
    empty = copy.deepcopy(realm.documents[realm.head['revision_id']])
    realm.call([track_create('first-track')])
    before = copy.deepcopy(realm.documents[realm.head['revision_id']])
    captured = capture_clip_add(bundle_from_document(before), 'parent', literal_row())
    created = realm.call([captured])
    document = created['data']['canonical_document']
    assert document['parent_revision']['payload']['clips'] == [literal_row()]
    assert document['parent_revision']['payload']['config']['clips'] == [literal_row()]
    assert realm.documents[realm.head['revision_id']] == document
    for key in ('shot_revisions', 'internal_timeline_revisions', 'dependency_manifest'):
        assert document[key] == empty[key]
    occupied = copy.deepcopy(realm.head)
    raw = {key: captured[key] for key in ('schema_version', 'op', 'version', 'select', 'args')}
    with pytest.raises(CommitRejected) as error:
        realm.call([raw])
    assert error.value.failure['details']['reason'] == 'identity_collision'
    assert realm.head == occupied
    undone = realm.call([inverse([created['receipt']['receipt_id']])])
    assert undone['data']['canonical_document'] == before
    retired, history = copy.deepcopy(realm.head), copy.deepcopy(realm.entries)
    with pytest.raises(CommitRejected) as error:
        realm.call([raw])
    assert error.value.failure['details']['reason'] == 'identity_collision'
    assert realm.head == retired and realm.entries == history
    redone = realm.call([inverse([undone['receipt']['receipt_id']])])
    assert redone['data']['canonical_document'] == document
    assert redone['data']['commands'][0]['created'] == ['parent/clip:first-clip']
    assert realm.call([inverse([redone['receipt']['receipt_id']])])['data']['canonical_document'] == before


def test_empty_to_nonempty_one_explicit_command_batch_has_no_hidden_lane_or_id():
    realm = DetachedRealm(empty_document())
    add = {'schema_version': 1, 'op': 'clip.add', 'version': '1', 'select': 'parent',
           'args': {'row': literal_row()}}
    accepted = realm.call([track_create('first-track'), add])
    records = realm.entries[-1]['records']['commands']
    assert [r['created'] for r in records] == [
        [{'ref': 'parent/track:first-track', 'kind': 'track', 'owner': 'parent'}],
        [{'ref': 'parent/clip:first-clip', 'kind': 'clip', 'owner': 'parent'}]]
    payload = accepted['data']['canonical_document']['parent_revision']['payload']
    assert [t['id'] for t in payload['config']['tracks']] == ['first-track']
    assert payload['clips'] == [literal_row()]


@pytest.mark.parametrize('changes', [{'track': 'missing'}, {'asset': 'x'}, {'from': 0}, {'at': -1}, {'hold': 0}])
def test_invalid_first_clip_batch_cannot_partially_create_the_track(changes):
    realm = DetachedRealm(empty_document())
    before = copy.deepcopy((realm.head, realm.entries, realm.documents))
    add = {'schema_version': 1, 'op': 'clip.add', 'version': '1', 'select': 'parent',
           'args': {'row': literal_row(**changes)}}
    with pytest.raises(CommitRejected):
        realm.call([track_create('first-track'), add])
    assert (realm.head, realm.entries, realm.documents) == before
    assert realm.prepared == realm.accepted == []


def test_native_create_label_params_group_reload_inverse_redo_preserves_disjoint_edit():
    from astrid.sdk.timeline._command_state import State
    from astrid.sdk.timeline._scalar_commands import capture_clip_set_params, capture_track_set
    from astrid.sdk.timeline.commands import execute

    realm = DetachedRealm(empty_document())
    realm.call([track_create('other')])
    original = copy.deepcopy(realm.documents[realm.head['revision_id']])
    local = bundle_from_document(original)
    calls = []
    for op, select, args in [
        ('track.create', 'parent', {'row': {'id': 'first-track', 'kind': 'visual',
                                          'opaque': [None, False]}}),
        ('clip.add', 'parent', {'row': literal_row()}),
        ('track.set', 'parent/track:first-track', {'values': {'label': ''}, 'clear': []}),
        ('clip.set-params', 'parent/clip:first-clip', {
            'values': {'null': None, 'opaque': {'ordered': [None, False, 0, {}, []]}},
            'clear': ['text']}),
    ]:
        plan = execute(local, [{'schema_version': 1, 'op': op, 'version': '1',
                                'select': select, 'args': args, 'group_id': 'native-group'}])
        calls.extend(plan.commands)
        local = plan.bundle
    accepted = realm.call(calls)
    document = accepted['data']['canonical_document']
    state = State(bundle_from_document(document))
    assert state.entity('parent/track:first-track')['label'] == ''
    assert state.entity('parent/clip:first-clip') == {
        **literal_row(), 'params': {'null': None, 'opaque': {'ordered': [None, False, 0, {}, []]}}}
    assert [row['id'] for row in state.collection('parent', 'track')] == ['other', 'first-track']
    assert accepted['data']['commands'] == calls
    assert [item['ref'] for record in realm.entries[-1]['records']['commands']
            for item in record['created']] == ['parent/track:first-track', 'parent/clip:first-clip']

    # Presence clears are a second undo unit, preserving literal null and "".
    clear_label = capture_track_set(state.bundle, 'parent/track:first-track', clear=['label'])
    local = execute(state.bundle, [clear_label]).bundle
    clear_null = capture_clip_set_params(local, 'parent/clip:first-clip', clear=['null'])
    cleared = realm.call([clear_label, clear_null])
    assert 'label' not in State(bundle_from_document(cleared['data']['canonical_document'])).entity(
        'parent/track:first-track')
    restored = realm.call([inverse([cleared['receipt']['receipt_id']])])
    assert restored['data']['canonical_document'] == document

    disjoint = capture_track_set(bundle_from_document(document), 'parent/track:other',
                                 values={'label': 'Another author'})
    realm.call([disjoint])
    # Both the clear and its inverse touched the group's fields, so undoing the
    # original group correctly refuses ABA even though the authored values match.
    with pytest.raises(CommitRejected) as error:
        realm.call([inverse([accepted['receipt']['receipt_id']])])
    assert error.value.failure['details']['reason'] == 'intervening_write'

    # A fresh history isolates the disjoint-author proof from those ABA writes.
    realm = DetachedRealm(original)
    accepted = realm.call(calls)
    realm.call([disjoint])
    undone = realm.call([inverse([accepted['receipt']['receipt_id']])])
    expected = execute(bundle_from_document(original), [disjoint]).bundle
    from astrid.sdk.timeline.commit import document_from_bundle
    assert undone['data']['canonical_document'] == document_from_bundle(expected)
    for reused in ([track_create('first-track')], [
        track_create('fresh-track'),
        {'schema_version': 1, 'op': 'clip.add', 'version': '1', 'select': 'parent',
         'args': {'row': literal_row(track='fresh-track')}},
    ]):
        before = copy.deepcopy((realm.head, realm.entries, realm.documents))
        with pytest.raises(CommitRejected) as error:
            realm.call(reused)
        assert error.value.failure['details']['reason'] == 'identity_collision'
        assert (realm.head, realm.entries, realm.documents) == before
    redone = realm.call([inverse([undone['receipt']['receipt_id']])])
    final = State(bundle_from_document(redone['data']['canonical_document']))
    assert final.entity('parent/track:other')['label'] == 'Another author'
    assert final.entity('parent/clip:first-clip') == state.entity('parent/clip:first-clip')
    assert final.collection('parent', 'track') == [
        {**original['parent_revision']['payload']['config']['tracks'][0], 'label': 'Another author'},
        state.entity('parent/track:first-track')]


def test_native_group_external_consumer_and_late_bad_stage_are_atomic():
    from astrid.sdk.timeline._scalar_commands import capture_clip_set_params
    from astrid.sdk.timeline.commands import execute

    realm = DetachedRealm(empty_document())
    track = track_create('first-track')
    add = {'schema_version': 1, 'op': 'clip.add', 'version': '1', 'select': 'parent',
           'args': {'row': literal_row()}}
    group = realm.call([track, add])
    realm.call([capture_clip_add(bundle_from_document(group['data']['canonical_document']),
                                 'parent', literal_row(id='external', at=3))])
    before = copy.deepcopy((realm.head, realm.entries, realm.documents))
    with pytest.raises(CommitRejected):
        realm.call([inverse([group['receipt']['receipt_id']])])
    assert (realm.head, realm.entries, realm.documents) == before

    fresh = DetachedRealm(empty_document())
    local = execute(bundle_from_document(empty_document()), [track, add]).bundle
    bad = capture_clip_set_params(local, 'parent/clip:first-clip', values={'text': 'valid'})
    bad['args']['clear'] = ['text']
    before = copy.deepcopy((fresh.head, fresh.entries, fresh.documents))
    with pytest.raises(CommitRejected):
        fresh.call([track, add, bad])
    assert (fresh.head, fresh.entries, fresh.documents) == before
    assert fresh.prepared == fresh.accepted == []
