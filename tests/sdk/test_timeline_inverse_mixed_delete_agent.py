"""Focused regression for native clip-delete undo order recording."""
from __future__ import annotations

import copy

import pytest

from astrid.sdk.timeline import commands
from astrid.sdk.timeline._collections import order
from astrid.sdk.timeline._command_state import State, present
from astrid.sdk.timeline.capture import capture_clip_delete
from astrid.sdk.timeline.commit import _digest, create_runtime_commit_service, bundle_from_document
from tests.sdk.test_timeline_commit_provider import (
    CommitRejected,
    DetachedPort,
    DetachedRealm,
    inverse as inverse_command,
    runtime_abi as runtime_abi,
)
from tests.sdk.test_timeline_commit_provider_document import document_fixture


def _move_clip(bundle, identity, *, after_id):
    row = State(bundle).entity('parent/clip:' + identity)
    return {'schema_version': 1, 'op': 'doc.patch', 'version': '1', 'select': None,
            'args': {'changes': [{'op': 'list.move', 'ref': 'parent', 'path': ['clips'],
                                 'before': present(row), 'id': identity, 'after_id': after_id,
                                 'order_before': [ref.rsplit(':', 1)[1]
                                                  for ref in order(State(bundle), 'parent', 'clip')]}]}}


def _commit_with_order_steps(realm, calls):
    context = realm.context(calls)
    context['request']['steps'] = [
        {'id': f'mixed-delete-order-{index}', 'label': 'Mixed delete and order',
         'intent': '', 'tags': [], 'command_ordinals': [index],
         'patch': copy.deepcopy(call['args']['changes']) if call['op'] == 'doc.patch' else []}
        for index, call in enumerate(calls)]
    context['invocation']['body'] = copy.deepcopy(context['request'])
    context['request_hash'] = _digest(context['invocation'])
    return create_runtime_commit_service().commit(context, DetachedPort(realm, context))


@pytest.mark.parametrize('sequence', ['order-before-delete', 'delete-before-order'])
def test_mixed_native_clip_delete_and_order_change_restore_exact_prior_order(sequence):
    document = document_fixture(shared=False)
    parent = document['parent_revision']['payload']
    third = copy.deepcopy(parent['clips'][1])
    third.update(id='clip-c', at=9)
    parent['clips'].append(third)
    parent['config']['clips'] = copy.deepcopy(parent['clips'])
    parent['occurrences'] = []
    document['shot_revisions'] = []
    document['internal_timeline_revisions'] = []
    document['dependency_manifest']['shots'] = []
    document['dependency_manifest']['internal_timelines'] = []
    document['parent_revision']['content_digest'] = _digest(parent)
    realm = DetachedRealm(document)
    bundle = bundle_from_document(document)
    move = _move_clip(bundle, 'clip-c', after_id=None)
    if sequence == 'order-before-delete':
        after_move = commands.execute(bundle, [move])
        delete = capture_clip_delete(after_move.bundle, 'parent/clip:clip-b')
        calls = [move, delete]
    else:
        delete = capture_clip_delete(bundle, 'parent/clip:clip-b')
        after_delete = commands.execute(bundle, [delete])
        move = _move_clip(after_delete.bundle, 'clip-c', after_id=None)
        calls = [delete, move]

    original = copy.deepcopy(document)
    accepted = _commit_with_order_steps(realm, calls)
    source_records = copy.deepcopy(realm.entries[-1]['records']['commands'])
    undone = realm.call([inverse_command([accepted['receipt']['receipt_id']])])
    inverse_record = realm.entries[-1]['records']['commands'][0]

    assert undone['data']['canonical_document'] == original
    expected_writes = [{**write, 'before': write['after'], 'after': write['before']}
                       for record in reversed(source_records) for write in reversed(record['writes'])]
    assert inverse_record['writes'] == expected_writes
    mirror = inverse_record['derived_writes'][-1]
    assert mirror['ref'] == 'parent' and mirror['path'] == ['config', 'clips']
    expected_mirror_order = (['clip-c', 'clip-a', 'clip-b'] if sequence == 'order-before-delete'
                             else ['clip-a', 'clip-b', 'clip-c'])
    assert [row['id'] for row in mirror['after']['value']] == expected_mirror_order


def test_mixed_delete_inverse_refuses_a_later_authenticated_order_change():
    document = document_fixture(shared=False)
    parent = document['parent_revision']['payload']
    third = copy.deepcopy(parent['clips'][1])
    third.update(id='clip-c', at=9)
    parent['clips'].append(third)
    parent['config']['clips'] = copy.deepcopy(parent['clips'])
    parent['occurrences'] = []
    document['shot_revisions'] = []
    document['internal_timeline_revisions'] = []
    document['dependency_manifest']['shots'] = []
    document['dependency_manifest']['internal_timelines'] = []
    document['parent_revision']['content_digest'] = _digest(parent)
    realm = DetachedRealm(document)
    bundle = bundle_from_document(document)
    move = _move_clip(bundle, 'clip-c', after_id=None)
    after_move = commands.execute(bundle, [move])
    delete = capture_clip_delete(after_move.bundle, 'parent/clip:clip-b')
    accepted = _commit_with_order_steps(realm, [move, delete])

    later_bundle = bundle_from_document(realm.documents[realm.head['revision_id']])
    later_move = _move_clip(later_bundle, 'clip-a', after_id=None)
    _commit_with_order_steps(realm, [later_move])
    before = (copy.deepcopy(realm.head), copy.deepcopy(realm.entries), copy.deepcopy(realm.documents))
    with pytest.raises(CommitRejected) as error:
        realm.call([inverse_command([accepted['receipt']['receipt_id']])])

    assert error.value.failure['details']['reason'] == 'intervening_write'
    assert (realm.head, realm.entries, realm.documents) == before


def test_native_move_split_and_multiple_deletes_undo_exactly():
    from astrid.sdk.timeline._split_commands import capture_clip_split
    from astrid.sdk.timeline.capture import capture_move
    from tests.editing_conformance.test_doc_patch_v2 import submit
    from tests.editing_conformance.test_recipe_m2_clip_split import source_document

    original = source_document()
    realm = DetachedRealm(original)
    staged = bundle_from_document(original)
    calls = []
    for capture in (
        lambda bundle: capture_move(bundle, 'parent/clip:clip-b', 8),
        lambda bundle: capture_clip_split(bundle, 'parent/clip:clip-a', on=3, right_id='right'),
        lambda bundle: capture_clip_delete(bundle, 'parent/clip:clip-a'),
        lambda bundle: capture_clip_delete(bundle, 'parent/clip:clip-b'),
    ):
        call = capture(staged)
        calls.append(call)
        staged = commands.execute(staged, [call]).bundle

    accepted = submit(realm, *calls, label='Native recut', reads=[], steps=[
        {'id': 'recut', 'label': 'Native recut', 'intent': '', 'tags': [],
         'command_ordinals': list(range(len(calls))), 'patch': []}])
    remaining = accepted['data']['canonical_document']['parent_revision']['payload']['clips']
    assert [row['id'] for row in remaining] == ['right']
    undone = realm.call([inverse_command([accepted['receipt']['receipt_id']])])
    assert undone['data']['canonical_document'] == original
