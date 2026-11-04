"""Root assignments retain sequential guards after config removes clip mirrors."""
from __future__ import annotations

import copy

import pytest

from astrid.sdk.timeline._command_state import ABSENT, present
from astrid.sdk.timeline.commands import execute
from astrid.sdk.timeline.commit import (
    _digest,
    _manifest,
    bundle_from_document,
    document_from_bundle,
)
from tests.editing_conformance.test_doc_patch_v2 import change, patch, step, submit
from tests.sdk.test_timeline_commit_provider import CommitRejected, DetachedRealm, inverse
from tests.sdk.test_timeline_commit_provider import runtime_abi as runtime_abi
from tests.sdk.test_timeline_commit_provider_document import document_fixture


def replacement_calls(document, *, separate=False):
    parent = document['parent_revision']['payload']
    config = copy.deepcopy(parent['config'])
    clips = config.pop('clips')
    for row in clips:
        row['id'] = 'new-' + row['id']
    changes = [change(['config'], present(parent['config']), config),
               change(['clips'], ABSENT, clips),
               change(['clips', 'id:new-clip-a', 'hold'], present(2), 3)]
    created = ['parent/clip:' + row['id'] for row in clips]
    calls = ([patch(changes[0]), patch(*changes[1:], created=created)] if separate
             else [patch(*changes, created=created)])
    expected = copy.deepcopy(parent)
    expected['config'] = config
    expected['clips'] = copy.deepcopy(clips)
    expected['clips'][0]['hold'] = 3
    return calls, expected


@pytest.mark.parametrize('separate', [False, True])
@pytest.mark.parametrize('merge', ['fields', 'atomic'])
def test_config_then_absent_root_clips_is_recorded_and_accepted(separate, merge):
    document = document_fixture()
    calls, expected = replacement_calls(document, separate=separate)
    realm = DetachedRealm(document)
    base = copy.deepcopy(realm.head)
    read = {'kind': 'field', 'ref': 'parent', 'path': ['unknown'],
            'state': present(expected['unknown'])}
    controls = {'label': 'Replace root clips', 'merge': merge, 'reads': [read],
                'source': {'via': 'python'}, 'steps': []}
    for ordinal, call in enumerate(calls):
        clips_read = {'kind': 'field', 'ref': 'parent', 'path': ['clips'],
                      'state': ABSENT if ordinal else present(document['parent_revision']['payload']['clips'])}
        item = step('replace-' + str(ordinal), ordinal, call, reads=[clips_read])
        item.update(intent='Restore authored clips', tags=['root-assignment'])
        controls['steps'].append(item)
    before = copy.deepcopy((realm.head, realm.documents, realm.entries, realm.accepted))
    preview = submit(realm, *calls, dry_run=True, **controls)
    assert preview['receipt'] is None
    assert preview['data']['status'] == 'preview'
    assert preview['data']['would_status'] == 'accepted'
    assert (realm.head, realm.documents, realm.entries, realm.accepted) == before
    result = submit(realm, *calls, **controls)
    assert result['data']['status'] == 'accepted'
    landed = result['data']['canonical_document']
    assert landed == preview['data']['canonical_document']
    assert landed['parent_revision']['payload'] == expected
    for key in ('shot_revisions', 'internal_timeline_revisions', 'dependency_manifest'):
        assert landed[key] == document[key]
    assert result['receipt']['result'] == result['data']
    assert result['data']['publisher'] == realm.entries[-1]['publisher'] == 'actor-1'
    assert realm.entries[-1]['previous'] == base
    assert realm.entries[-1]['receipt'] == result['receipt']
    records = realm.entries[-1]['records']
    assert records['label'] == controls['label']
    assert records['merge_mode'] == merge
    for submitted, recorded in zip(controls['steps'], records['steps']):
        assert all(recorded[key] == submitted[key]
                   for key in ('id', 'label', 'intent', 'tags', 'command_ordinals'))
        assert read in recorded['dependencies']
        clips_read = copy.deepcopy(submitted['reads'][0])
        clips_read['path'] = ['config', 'clips']
        assert clips_read in recorded['dependencies']
        assert recorded['patch']
    assert records['undo_steps'] is None
    writes = [write for record in records['commands'] for write in record['writes']]
    clips = calls[-1]['args']['changes'][-2]['value']
    assert {'ref': 'parent', 'path': ['clips'], 'before': ABSENT, 'after': present(clips)} in writes
    assert {'ref': 'parent/clip:new-clip-a', 'path': ['hold'],
            'before': present(2), 'after': present(3)} in writes
    bindings = [binding for record in records['commands'] for binding in record['bindings']
                if binding['key'].startswith('doc.patch:')]
    assert len(bindings) == 3
    assert bindings[1]['change']['before'] == ABSENT
    assert bindings[1]['path'] == ['config', 'clips']
    assert all(binding['boundary']['evaluated_head'] == base for binding in bindings)
    assert [created for record in records['commands'] for created in record['created']] == [
        {'ref': 'parent/clip:' + row['id'], 'kind': 'clip', 'owner': 'parent'} for row in clips]
    bundle = bundle_from_document(document)
    plan = execute(bundle, calls, commit_request=controls, evaluated_head=base)
    assert document_from_bundle(plan.bundle)['parent_revision']['payload'] == expected
    assert bundle == bundle_from_document(document)


@pytest.mark.parametrize('separate', [False, True])
@pytest.mark.parametrize('merge', ['fields', 'atomic'])
def test_config_then_absent_root_clips_inverse_restores_exact_document(separate, merge):
    document = document_fixture()
    calls, _ = replacement_calls(document, separate=separate)
    realm = DetachedRealm(document)
    result = submit(realm, *calls, reads=[], merge=merge)
    undone = realm.call([inverse([result['receipt']['receipt_id']])])
    assert undone['data']['canonical_document'] == document


@pytest.mark.parametrize('path', [['clips'], ['config', 'clips']])
def test_first_collection_assignment_then_native_identity_write(path):
    document = document_fixture()
    calls, _ = replacement_calls(document, separate=True)
    clips = calls[1]['args']['changes'][0]['value']
    calls[1]['args']['changes'][0]['path'] = path
    calls[1]['args']['changes'][1]['path'] = [*path, 'id:new-clip-a', 'hold']
    plan = execute(bundle_from_document(document), calls, commit_request={'reads': []})
    parent = document_from_bundle(plan.bundle)['parent_revision']['payload']
    if path == ['clips']:
        assert parent['clips'][0]['hold'] == 3
        assert 'clips' not in parent['config']
    else:
        assert parent['config']['clips'][0]['hold'] == 3
    assert [row['id'] for row in parent.get('clips', parent['config'].get('clips'))] == [
        row['id'] for row in clips]


def test_absent_tracks_and_tagged_null_config_keys_keep_native_effects_and_inverse():
    document = document_fixture()
    parent = document['parent_revision']['payload']
    clip = copy.deepcopy(parent['clips'][0])
    clip.update(id='new-clip', track='new-picture')
    parent.update(clips=[], occurrences=[])
    parent['config'].update(clips=[], tracks=[])
    document['shot_revisions'] = []
    document['internal_timeline_revisions'] = []
    document['dependency_manifest'] = _manifest([], [], document['dependency_manifest']['media'], [])
    document['parent_revision']['content_digest'] = _digest(parent)
    config = copy.deepcopy(parent['config'])
    config.pop('clips')
    config.pop('tracks')
    tracks = [{'id': 'new-picture', 'kind': 'visual'}]
    calls = [patch(change(['config'], present(parent['config']), config)),
             patch(change(['config', 'tracks'], ABSENT, tracks), change(['clips'], ABSENT, [clip]),
                   created=['parent/track:new-picture', 'parent/clip:new-clip'])]
    expected = copy.deepcopy(parent)
    expected.update(config={**config, 'tracks': tracks}, clips=[clip])
    for path in (['caption'], ['config', 'caption']):
        calls[1]['args']['changes'].extend([
            change(path, ABSENT, None), change(path, present(None), 'authored'),
            change(path, present('authored'), op='remove'),
        ])
    realm = DetachedRealm(document)
    result = submit(realm, *calls, reads=[])
    assert result['data']['canonical_document']['parent_revision']['payload'] == expected
    writes = realm.entries[-1]['records']['commands'][1]['writes']
    for path in (['caption'], ['config', 'caption']):
        assert [(row['before'], row['after']) for row in writes if row['path'] == path] == [
            (ABSENT, present(None)), (present(None), present('authored')),
            (present('authored'), ABSENT)]
    undone = realm.call([inverse([result['receipt']['receipt_id']])])
    assert undone['data']['canonical_document'] == document


@pytest.mark.parametrize('damage,reason', [
    ('previous', 'previous_value_mismatch'), ('null_previous', 'previous_value_mismatch'),
    ('order', 'previous_value_mismatch'), ('source', 'outside_closure'),
    ('media', 'dependency_changed'), ('contradictory', 'identity_conflict'),
    ('null_clips', 'invalid_edit_command'), ('null_tracks', 'invalid_edit_command'),
])
def test_root_assignment_guards_refuse_without_writes(damage, reason):
    document = document_fixture()
    calls, _ = replacement_calls(document)
    call = calls[0]
    changes = call['args']['changes']
    if damage in ('previous', 'null_previous'):
        changes[1]['before'] = present([] if damage == 'previous' else None)
    elif damage == 'order':
        rows = changes[1]['value']
        changes.append(change(['clips'], present(rows[0]), op='list.move', id=rows[0]['id'],
                              order_before=[row['id'] for row in reversed(rows)], after_id=None))
    elif damage == 'source':
        changes[1]['ref'] = 'internal-revision:foreign'
    elif damage == 'media':
        changes[1]['value'][0]['media_id'] = 'sha256:' + '9' * 64
    elif damage == 'contradictory':
        parent = copy.deepcopy(document['parent_revision']['payload'])
        parent['config']['clips'][0]['hold'] = 9
        call = patch(change([], present(document['parent_revision']['payload']), parent))
    elif damage == 'null_clips':
        changes[1]['value'] = None
    else:
        changes[0]['value']['tracks'] = None
    realm = DetachedRealm(document)
    before = copy.deepcopy((realm.head, realm.documents, realm.entries, realm.accepted, realm.prepared))
    with pytest.raises(CommitRejected) as error:
        submit(realm, call, reads=[])
    assert error.value.failure['details']['reason'] == reason
    assert (realm.head, realm.documents, realm.entries, realm.accepted, realm.prepared) == before
