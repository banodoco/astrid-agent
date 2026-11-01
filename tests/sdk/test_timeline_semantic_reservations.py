"""Semantic retirement through the unit port, not Runtime atomic admission."""
from __future__ import annotations

import copy

import pytest

from astrid.sdk.timeline import commands
from tests.editing_conformance.staging_recipes import install_staging_operations
from tests.editing_conformance.test_recorded_graph_roots import create_command, root_values
from tests.editing_conformance.test_recorded_graph_roots import root_ops as root_ops
from tests.sdk.test_timeline_commit_provider import (
    CommitRejected,
    DetachedPort,
    DetachedRealm,
    detached,
    inverse,
    track_create,
)
from tests.sdk.test_timeline_commit_provider import runtime_abi as runtime_abi

pytestmark = pytest.mark.usefixtures('root_ops')


@pytest.fixture(autouse=True)
def semantic_ops(monkeypatch):
    install_staging_operations(monkeypatch)

    def delete(ctx, command):
        # The bbb60b5c structural deletion primitives, extended to the fixture's
        # child owner. This test recipe makes no semantic dependency claim.
        ref = command['select']
        ctx.select('collection.clips', ref.rsplit('/', 1)[0])
        ctx.read(ref)
        ctx.clear(ref, [])

    def churn(ctx, command):
        for root in root_values():
            ctx.create_entity(**root)
        delete(ctx, deletion('occurrence:new-occ/clip:title'))
        ctx.create('occurrence:new-occ', 'clip', root_values()[0]['value']['internal_timeline']['clips'][0])

    registered = commands.operations()
    registered['fixture.clip-delete'] = commands.Operation({'type': 'object'}, delete, {})
    registered['fixture.root-churn'] = commands.Operation({'type': 'object'}, churn, {})
    monkeypatch.setattr(commands, 'operations', lambda: registered)


def deletion(ref):
    return {'schema_version': 1, 'op': 'fixture.clip-delete', 'version': '1',
            'select': ref, 'args': {}}


def creation(owner, identity):
    return {'schema_version': 1, 'op': 'fixture.clip-create', 'version': '1',
            'select': owner, 'args': {'row': {'id': identity, 'track': 'picture', 'at': 0, 'hold': 2}}}


def assert_collision(realm, calls):
    head, entries, documents = detached(realm.head), detached(realm.entries), detached(realm.documents)
    prepared = len(realm.prepared)
    with pytest.raises(CommitRejected) as error:
        realm.call(calls)
    assert error.value.failure['details']['reason'] == 'identity_collision'
    assert realm.head == head and realm.entries == entries and realm.documents == documents
    assert len(realm.prepared) == prepared


@pytest.mark.parametrize('intent', ['bootstrap', 'legacy_unknown'])
def test_seeded_clip_deletion_retires_identity_without_retroactive_created_facts(intent):
    realm = DetachedRealm()
    realm.entries[0]['intent'] = intent
    original = detached(realm.documents['parent-1'])
    deleted = realm.call([deletion('parent/clip:clip-a')])
    assert realm.entries[-1]['records']['commands'][0]['created'] == []
    assert_collision(realm, [creation('parent', 'clip-a')])
    undone = realm.call([inverse([deleted['receipt']['receipt_id']])])
    assert undone['data']['canonical_document'] == original
    redone = realm.call([inverse([undone['receipt']['receipt_id']])])
    assert redone['data']['canonical_document'] == deleted['data']['canonical_document']
    assert_collision(realm, [creation('parent', 'clip-a')])


def test_aggregate_children_are_retired_in_their_occurrence_scope():
    realm = DetachedRealm()
    introduced = realm.call([create_command()])
    assert introduced['data']['commands'][0]['created'] == ['shot:new-shot', 'occurrence:new-occ']
    deleted = realm.call([deletion('occurrence:new-occ/clip:title')])
    assert realm.entries[-1]['records']['commands'][0]['created'] == []
    assert_collision(realm, [creation('occurrence:new-occ', 'title')])
    undone = realm.call([inverse([deleted['receipt']['receipt_id']])])
    assert undone['data']['canonical_document'] == introduced['data']['canonical_document']
    redone = realm.call([inverse([undone['receipt']['receipt_id']])])
    assert redone['data']['canonical_document'] == deleted['data']['canonical_document']


@pytest.mark.parametrize('one_publication', [False, True])
def test_aggregate_removal_without_child_writes_still_retires_children(one_publication):
    realm = DetachedRealm()
    aggregate = root_values()[0]['value']
    aggregate['internal_timeline']['clips'] = []
    replace = {'schema_version': 1, 'op': 'fixture.properties', 'version': '1',
               'select': 'shot:new-shot', 'args': {'changes': [
                   {'path': [], 'state': {'state': 'present', 'value': aggregate}},
               ]}}
    if one_publication:
        realm.call([create_command(), replace])
    else:
        realm.call([create_command()])
        realm.call([replace])
    record = realm.entries[-1]['records']['commands'][-1]
    assert record['created'] == []
    assert [write['ref'] for write in record['writes']] == ['shot:new-shot']
    assert_collision(realm, [creation('occurrence:new-occ', 'title')])


@pytest.mark.parametrize('same_record', [False, True])
def test_implicit_children_cannot_be_reallocated_within_one_batch(same_record):
    realm = DetachedRealm()
    calls = ([{'schema_version': 1, 'op': 'fixture.root-churn', 'version': '1',
               'select': 'parent', 'args': {}}] if same_record else
             [create_command(), deletion('occurrence:new-occ/clip:title'),
              creation('occurrence:new-occ', 'title')])
    assert_collision(realm, calls)
    assert realm.accepted == []


def test_children_introduced_and_deleted_in_one_publication_remain_retired():
    realm = DetachedRealm()
    realm.call([create_command(), deletion('occurrence:new-occ/clip:title')])
    assert_collision(realm, [creation('occurrence:new-occ', 'title')])


def test_seeded_delete_and_recreate_in_one_batch_is_atomic():
    realm = DetachedRealm()
    assert_collision(realm, [deletion('parent/clip:clip-a'), creation('parent', 'clip-a')])
    assert realm.accepted == []


def test_child_ids_remain_available_in_different_owner_and_kind_namespaces():
    realm = DetachedRealm()
    realm.call([create_command()])
    realm.call([deletion('occurrence:new-occ/clip:title')])
    realm.call([creation('parent', 'title')])
    realm.call([track_create('title', 'occurrence:new-occ')])
    roots = root_values()
    roots[0]['ref'] = 'shot:other-shot'
    roots[1]['ref'] = 'occurrence:other-occ'
    roots[1]['value'].update(occurrence_id='other-occ', shot_id='other-shot')
    result = realm.call([create_command(roots)])
    assert result['data']['commands'][0]['created'] == ['shot:other-shot', 'occurrence:other-occ']
    realm.call([creation('occurrence:occ-a', 'title')])


def test_opaque_identity_shaped_data_does_not_allocate_semantic_ids():
    roots = root_values()
    roots[0]['value']['internal_timeline']['unknown'] = {
        'clips': [{'id': 'opaque'}], 'literal': 'parent/clip:opaque',
        'owner': 'parent', 'ref': 'parent/track:opaque-track',
    }
    realm = DetachedRealm()
    realm.call([create_command(roots)])
    realm.call([creation('parent', 'opaque'), track_create('opaque-track')])
    realm.call([creation('occurrence:new-occ', 'opaque')])


@pytest.mark.parametrize('damage', ['input-missing', 'output-missing', 'digest', 'foreign',
                                   'records', 'write', 'ancestry'])
def test_missing_or_corrupt_relevant_history_fails_closed(damage):
    realm = DetachedRealm()
    introduced = realm.call([create_command()])
    revision = introduced['data']['canonical_document']['parent_revision']['revision_id']
    realm.call([deletion('occurrence:new-occ/clip:title')])
    if damage == 'input-missing':
        del realm.documents['parent-1']
    elif damage == 'output-missing':
        del realm.documents[revision]
    elif damage == 'digest':
        realm.documents[revision]['internal_timeline_revisions'][-1]['payload']['corrupt'] = True
    elif damage == 'foreign':
        realm.documents[revision]['project_id'] = 'foreign'
    elif damage == 'records':
        realm.entries[1]['records']['commands'][0]['created'] = []
    elif damage == 'write':
        realm.entries[2]['records']['commands'][0]['writes'][0]['before']['value']['id'] = 'wrong'
    else:
        realm.entries[1]['previous']['revision_id'] = 'wrong'
    head, count = detached(realm.head), len(realm.prepared)
    with pytest.raises(CommitRejected) as error:
        realm.call([creation('occurrence:new-occ', 'title')])
    assert error.value.failure['details']['reason'] == 'history_unavailable'
    assert realm.head == head and len(realm.prepared) == count


def test_unknown_legacy_prefix_needs_no_invented_allocation_history():
    realm = DetachedRealm()
    legacy = copy.deepcopy(realm.entries[0])
    legacy.update(publication_id='unknown-prefix', revision_id='unavailable-prefix', intent='legacy_unknown')
    realm.entries[0].update(previous={'publication_id': 'unknown-prefix', 'revision_id': 'unavailable-prefix'},
                            intent='legacy_unknown')
    realm.entries.insert(0, legacy)
    realm.call([deletion('parent/clip:clip-a')])
    assert_collision(realm, [creation('parent', 'clip-a')])
    realm.call([creation('parent', 'fresh')])
    assert ('revision', 'unavailable-prefix') not in realm.reads


def test_move_and_deletion_without_created_ids_do_not_scan_history(monkeypatch):
    realm = DetachedRealm()

    def forbidden(*args, **kwargs):
        raise AssertionError('ordinary operation without created IDs scanned history')

    monkeypatch.setattr(DetachedPort, 'history', forbidden)
    realm.call([realm.move()])
    realm.call([deletion('parent/clip:clip-a')])
