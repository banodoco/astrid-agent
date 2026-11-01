"""Native provider acceptance and history behavior; no Runtime transport substitute."""
from __future__ import annotations

import copy

import pytest

from astrid.sdk.timeline._command_state import State
from astrid.sdk.timeline.commit import _digest, _manifest, bundle_from_document
from astrid.sdk.timeline.native_occurrence_delete import capture_occurrence_delete
from tests.editing_conformance.test_recipe_occurrence_delete import delete_document
from tests.sdk.test_timeline_commit_provider import CommitRejected, DetachedRealm, inverse
from tests.sdk.test_timeline_commit_provider import runtime_abi as runtime_abi


def capture(realm, owner):
    return capture_occurrence_delete(bundle_from_document(realm.documents[realm.head['revision_id']]), owner)


@pytest.mark.parametrize('count,index', [(3, 0), (3, 1), (3, 2), (1, 0)])
def test_authenticated_delete_reload_exact_inverse_redo(count, index):
    document = delete_document(count)
    realm = DetachedRealm(document)
    before = copy.deepcopy(realm.head)
    deleted = realm.call([capture(realm, 'occurrence:occ-' + str(index))])
    removed = deleted['data']['canonical_document']
    assert len(removed['parent_revision']['payload']['occurrences']) == count - 1
    assert realm.documents[before['revision_id']] == document
    undone = realm.call([inverse([deleted['receipt']['receipt_id']])])
    assert undone['data']['canonical_document'] == document
    redone = realm.call([inverse([undone['receipt']['receipt_id']])])
    assert redone['data']['canonical_document'] == removed
    # A subsequent inverse traverses both recreation and removal reservation history.
    restored = realm.call([inverse([redone['receipt']['receipt_id']])])
    assert restored['data']['canonical_document'] == document


def test_two_independent_group_deletes_rebase_and_restore_without_parent_membership_guard():
    document = delete_document()
    realm = DetachedRealm(document)
    base = copy.deepcopy(realm.head)
    first = capture(realm, 'occurrence:occ-0')
    second = capture(realm, 'occurrence:occ-2')
    a = realm.call([first])
    b = realm.call([second], base=base)
    assert [row['occurrence_id'] for row in b['data']['canonical_document']['parent_revision']['payload']['occurrences']] == ['occ-1']
    undone = realm.call([inverse([a['receipt']['receipt_id']])])
    assert [row['occurrence_id'] for row in undone['data']['canonical_document']['parent_revision']['payload']['occurrences']] == ['occ-0', 'occ-1']
    restored = realm.call([inverse([b['receipt']['receipt_id']])])
    result = restored['data']['canonical_document']
    assert result['parent_revision']['payload'] == document['parent_revision']['payload']
    assert result['parent_revision']['content_digest'] == document['parent_revision']['content_digest']
    for key in ('shot_revisions', 'internal_timeline_revisions', 'dependency_manifest'):
        assert result[key] == document[key]


def test_same_id_recreation_is_reserved_but_authenticated_undo_reuses_id(monkeypatch):
    from astrid.sdk.timeline import commands

    document = delete_document(2)
    bundle = bundle_from_document(document)
    value = copy.deepcopy(State(bundle).entity('occurrence:occ-0'))
    original = commands.operations

    def create(ctx, command):
        value['shot_id'] = ctx.state.occurrence('occurrence:occ-1')['shot_id']
        ctx.create_entity('occurrence:occ-0', kind='occurrence', owner='parent', value=value)

    monkeypatch.setattr(commands, 'operations', lambda: {
        **original(), 'test.reuse-occurrence': commands.Operation({'type': 'object'}, create, {})})
    realm = DetachedRealm(document)
    deleted = realm.call([capture(realm, 'occurrence:occ-0')])
    before, accepted = copy.deepcopy(realm.head), copy.deepcopy(realm.accepted)
    with pytest.raises(CommitRejected) as error:
        realm.call([{'schema_version': 1, 'op': 'test.reuse-occurrence', 'version': '1',
                     'select': 'parent', 'args': {}}])
    assert error.value.failure['details']['reason'] == 'identity_collision'
    assert realm.head == before and realm.accepted == accepted
    restored = realm.call([inverse([deleted['receipt']['receipt_id']])])
    assert restored['data']['canonical_document'] == document


def test_original_app_whole_group_visible_result_after_canonical_migration_boundary():
    document = delete_document(1)
    parent = document['parent_revision']['payload']
    internal = document['internal_timeline_revisions'][0]['payload']
    tracks = [{'id': 'V1', 'kind': 'visual', 'label': 'V1'}]
    clips = [{'id': 'clip-' + str(i + 1), 'at': 5 * i, 'track': 'V1', 'clipType': 'hold',
              'asset': 'asset-' + str(i + 1), 'hold': 5} for i in range(2)]
    parent['clips'] = parent['config']['clips'] = []
    parent['config']['tracks'] = copy.deepcopy(tracks)
    internal['tracks'], internal['clips'] = tracks, clips
    assets = {'asset-' + str(i): copy.deepcopy(parent['registry']['assets']['picture']) for i in (1, 2)}
    parent['registry'] = internal['registry'] = {'assets': assets}
    parent['config']['output']['fps'] = internal['output']['fps'] = 30
    parent['occurrences'][0]['placement']['track'] = parent['occurrences'][0]['track'] = 'V1'
    parent['occurrences'][0]['provenance']['group_migration'] = {
        'legacy_group': {'shotId': 'shot-1', 'trackId': 'V1', 'clipIds': ['clip-1', 'clip-2'], 'mode': 'images'},
        'members': [{'legacy_clip_id': identity, 'clip_id': identity} for identity in ('clip-1', 'clip-2')]}

    internal.pop('source_window')
    for image in [document['parent_revision'], *document['shot_revisions'], *document['internal_timeline_revisions']]:
        image['content_digest'] = _digest(image['payload'])
    document['dependency_manifest'] = _manifest(document['shot_revisions'], document['internal_timeline_revisions'],
                                                 document['dependency_manifest']['media'], [])
    realm = DetachedRealm(document)
    deleted = realm.call([capture(realm, 'occurrence:occ-0')])
    accepted = deleted['data']['canonical_document']
    payload = accepted['parent_revision']['payload']
    assert payload['occurrences'] == []
    assert payload['clips'] == payload['config']['clips'] == []
    assert payload['config']['tracks'] == tracks
    assert payload['registry'] == parent['registry']
    assert realm.call([inverse([deleted['receipt']['receipt_id']])])['data']['canonical_document'] == document


@pytest.mark.parametrize('aba', [False, True])
def test_provider_equal_state_and_aba_child_publications_accept_stale_delete(monkeypatch, aba):
    from astrid.sdk.timeline import commands

    original = commands.operations

    def write(ctx, command):
        ref = command['select']
        for value in command['args']['values']:
            ctx.write(ref, ['hold'], value)

    monkeypatch.setattr(commands, 'operations', lambda: {
        **original(), 'test.child-hold': commands.Operation({'type': 'object'}, write, {}, True)})
    realm = DetachedRealm(delete_document())
    base = copy.deepcopy(realm.head)
    command = capture(realm, 'occurrence:occ-1')
    realm.call([{'schema_version': 1, 'op': 'test.child-hold', 'version': '1',
                 'select': 'occurrence:occ-1/clip:clip-a', 'args': {'values': [3, 2] if aba else [2]}}])
    before, accepted = copy.deepcopy(realm.head), copy.deepcopy(realm.accepted)
    result = realm.call([command], base=base)
    expected_realm = DetachedRealm(delete_document())
    expected = expected_realm.call([capture(expected_realm, 'occurrence:occ-1')])
    assert result['data']['status'] == 'rebased'
    assert result['data']['rebased_onto'] == before
    assert result['data']['commands'] == [command]
    assert result['data']['canonical_document'] == expected['data']['canonical_document']
    assert [row['occurrence_id'] for row in result['data']['canonical_document'][
        'parent_revision']['payload']['occurrences']] == ['occ-0', 'occ-2']
    assert result is realm.accepted[-1]
    assert realm.accepted[:-1] == accepted
    assert result['receipt']['result'] == result['data']
    assert realm.head == {'revision_id': result['data']['accepted_revision_id'],
                          'publication_id': result['receipt']['receipt_id']}
    assert realm.entries[-1]['receipt'] == result['receipt']
    assert realm.entries[-1]['records'] == realm.prepared[-1]['records']
    assert [record['command'] for record in realm.entries[-1]['records']['commands']] == result['data']['commands']


def test_grouped_two_deletes_are_one_history_inverse_with_exact_original_order():
    document = delete_document()
    realm = DetachedRealm(document)
    deleted = realm.call([capture(realm, 'occurrence:occ-0'), capture(realm, 'occurrence:occ-2')])
    removed = deleted['data']['canonical_document']
    undone = realm.call([inverse([deleted['receipt']['receipt_id']])])
    assert undone['data']['canonical_document'] == document
    redone = realm.call([inverse([undone['receipt']['receipt_id']])])
    assert redone['data']['canonical_document'] == removed


def test_last_consumer_inverse_preserves_existing_nonliteral_source_map_baseline():
    from tests.sdk.test_timeline_commit_provider_document import document_fixture

    document = document_fixture(shared=False)
    realm = DetachedRealm(document)
    deleted = realm.call([capture(realm, 'occurrence:occ-a')])
    removed = deleted['data']['canonical_document']
    undone = realm.call([inverse([deleted['receipt']['receipt_id']])])
    assert undone['data']['canonical_document'] == document
    redone = realm.call([inverse([undone['receipt']['receipt_id']])])
    assert redone['data']['canonical_document'] == removed


def test_independent_shared_occurrences_can_both_delete_across_last_consumer_materialization():
    realm = DetachedRealm(delete_document(2))
    base = copy.deepcopy(realm.head)
    first = capture(realm, 'occurrence:occ-0')
    second = capture(realm, 'occurrence:occ-1')
    a = realm.call([first])
    b = realm.call([second], base=base)
    assert b['data']['canonical_document']['parent_revision']['payload']['occurrences'] == []
    realm.call([inverse([b['receipt']['receipt_id']])])
    restored = realm.call([inverse([a['receipt']['receipt_id']])])
    assert restored['data']['canonical_document'] == delete_document(2)
