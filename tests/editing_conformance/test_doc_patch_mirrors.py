"""Sequential parent clip changes use the production recorder and commit provider."""
from __future__ import annotations

import copy

import pytest

from astrid.core.timeline.authoring_bundle import AuthoringBundleError
from astrid.sdk.timeline._command_state import ABSENT, present
from astrid.sdk.timeline.commands import execute
from astrid.sdk.timeline.commit import _digest, bundle_from_document, document_from_bundle
from tests.editing_conformance.test_doc_patch_v2 import change, patch, step, submit
from tests.sdk.test_timeline_commit_provider import CommitRejected, DetachedRealm, inverse
from tests.sdk.test_timeline_commit_provider import runtime_abi as runtime_abi
from tests.sdk.test_timeline_commit_provider_document import document_fixture


def original_document():
    document = document_fixture()
    parent = document['parent_revision']
    parent['payload']['config']['output']['file'] = 'out.mp4'
    parent['content_digest'] = _digest(parent['payload'])
    return document


def split_patch(document, path, output_first, *, insert=True):
    source = document['parent_revision']['payload']['clips'][0]
    right = {**copy.deepcopy(source), 'id': 'clip-right', 'at': 2, 'hold': 1}
    changes = [
        change(path, ABSENT, right, op='list.insert', id='clip-right',
               order_before=['clip-a', 'clip-b'], after_id='clip-a'),
        change([*path, 'id:clip-a', 'hold'], present(2), 1),
        change([*path, 'id:clip-a', 'label'], ABSENT, None),
    ]
    output = change(['config', 'output', 'file'], present('out.mp4'), 'output.mp4')
    changes.insert(0 if output_first else len(changes), output)
    # These guarded operands consume earlier writes in this same command.
    changes.extend([
        change([*path, 'id:clip-a', 'hold'], present(1), 1.5),
        change([*path, 'id:clip-a', 'label'], present(None), op='remove'),
    ])
    if not insert:
        changes = [item for item in changes if item['op'] != 'list.insert']
    return patch(*changes, created=['parent/clip:clip-right'] if insert else [])


def declared_reads():
    return [
        {'kind': 'field', 'ref': 'parent', 'path': ['clips', 'id:clip-a', 'hold'],
         'state': present(2)},
        {'kind': 'collection', 'ref': 'parent', 'path': ['clips'],
         'members': ['clip-a', 'clip-b'], 'order': ['clip-a', 'clip-b']},
    ]


@pytest.mark.parametrize('path', [['clips'], ['config', 'clips']])
@pytest.mark.parametrize('output_first', [False, True])
def test_sequential_clip_mirrors_execute_provider_history_and_inverse(path, output_first):
    document = original_document()
    original = bundle_from_document(document)
    realm = DetachedRealm(document)
    base = copy.deepcopy(realm.head)
    call = split_patch(document, path, output_first, insert=False)
    controls = {'label': 'Clip and output', 'steps': [step('clip', 0, call)],
                'merge': 'fields', 'reads': declared_reads(),
                'base_revision_id': base['revision_id'], 'base_publication_id': base['publication_id']}
    plan = execute(original, [call], commit_request=controls, evaluated_head=base)
    assert original == bundle_from_document(document)
    expected = copy.deepcopy(document['parent_revision']['payload'])
    expected['clips'][0]['hold'] = 1.5
    expected['config']['clips'] = copy.deepcopy(expected['clips'])
    expected['config']['output']['file'] = 'output.mp4'
    assert document_from_bundle(plan.bundle)['parent_revision']['payload'] == expected

    accepted = submit(realm, call, **controls)
    assert set(accepted) == {'data', 'receipt'}
    assert accepted['data']['status'] == 'accepted'
    landed = accepted['data']['canonical_document']
    assert landed == document_from_bundle(plan.bundle)
    assert landed['parent_revision']['payload'] == expected
    for key in ('shot_revisions', 'internal_timeline_revisions', 'dependency_manifest'):
        assert landed[key] == document[key]
    entry = realm.entries[-1]
    assert len(realm.entries) == 2
    assert entry['previous'] == base
    assert entry['receipt'] == accepted['receipt']
    records = entry['records']
    assert records['label'] == controls['label']
    assert records['merge_mode'] == 'fields'
    assert records['commands'] == plan.records
    assert records['steps'] == plan.steps
    effects = records['steps'][0]['patch']
    expected_effects = copy.deepcopy(call['args']['changes'])
    for effect in expected_effects:
        if effect['path'][:1] == ['clips']:
            effect['path'].insert(0, 'config')
    assert effects == expected_effects
    dependencies = records['steps'][0]['dependencies']
    for read in declared_reads():
        assert {**read, 'path': ['config', *read['path']]} in dependencies
    record = records['commands'][0]
    assert record['created'] == []
    leaf_writes = [write for write in record['writes'] if write['ref'] == 'parent/clip:clip-a']
    assert [(write['path'], write['before'], write['after']) for write in leaf_writes] == [
        (['hold'], present(2), present(1)), (['label'], ABSENT, present(None)),
        (['hold'], present(1), present(1.5)), (['label'], present(None), ABSENT),
    ]
    assert len(record['derived_writes']) == 4
    for derived in record['derived_writes']:
        assert derived['ref'] == 'parent'
        assert derived['path'] == ['config', 'clips']
        assert derived['cause'] == 0
        assert derived['before']['value'][1:] == derived['after']['value'][1:]
    bindings = [binding for binding in record['bindings'] if binding['key'].startswith('doc.patch:')]
    assert [binding['change'] for binding in bindings] == call['args']['changes']
    assert all(binding['physical_ref'] == 'parent' for binding in bindings)
    restored = submit(realm, inverse([accepted['receipt']['receipt_id']]))
    assert restored['data']['canonical_document'] == document
    assert realm.entries[-1]['records']['undo_of'] == [accepted['receipt']['receipt_id']]


@pytest.mark.parametrize('path', [['clips'], ['config', 'clips']])
@pytest.mark.parametrize('output_first', [False, True])
def test_split_insert_then_clip_and_output_changes_keep_mirrors(path, output_first):
    document = original_document()
    realm = DetachedRealm(document)
    call = split_patch(document, path, output_first)
    accepted = submit(realm, call, reads=declared_reads())
    assert accepted['data']['status'] == 'accepted'
    parent = accepted['data']['canonical_document']['parent_revision']['payload']
    expected = copy.deepcopy(document['parent_revision']['payload'])
    expected['clips'].insert(1, {**copy.deepcopy(expected['clips'][0]),
                                'id': 'clip-right', 'at': 2, 'hold': 1})
    expected['clips'][0]['hold'] = 1.5
    expected['config']['clips'] = copy.deepcopy(expected['clips'])
    expected['config']['output']['file'] = 'output.mp4'
    assert parent == expected
    assert realm.entries[-1]['records']['commands'][0]['created'] == [
        {'ref': 'parent/clip:clip-right', 'kind': 'clip', 'owner': 'parent'}]


def test_initial_contradictory_mirrors_still_refuse():
    original = bundle_from_document(original_document())
    original['parent']['config']['clips'][0]['hold'] = 9
    before = copy.deepcopy(original)
    with pytest.raises(AuthoringBundleError, match='parent clips and config.clips disagree'):
        execute(original, [patch(change(['config', 'output', 'file'],
                                        present('out.mp4'), 'output.mp4'))],
                commit_request={'reads': []})
    assert original == before


@pytest.mark.parametrize('damage', ['previous', 'order', 'read'])
def test_sequential_split_bad_original_operands_refuse_atomically(damage):
    document = original_document()
    realm = DetachedRealm(document)
    call = split_patch(document, ['clips'], False)
    reads = declared_reads()
    if damage == 'previous':
        call['args']['changes'][1]['before'] = present(9)
    elif damage == 'order':
        call['args']['changes'][0]['order_before'] = ['clip-b', 'clip-a']
    else:
        reads[0]['state'] = present(9)
    base = copy.deepcopy(realm.head)
    with pytest.raises(CommitRejected) as error:
        submit(realm, call, merge='fields', reads=reads)
    assert error.value.failure['details']['reason'] == 'previous_value_mismatch'
    assert realm.head == base
    assert len(realm.entries) == 1
    assert realm.documents == {base['revision_id']: document}


def test_sequential_split_stale_same_field_still_conflicts():
    document = original_document()
    realm = DetachedRealm(document)
    base = copy.deepcopy(realm.head)
    submit(realm, patch(change(['clips', 'id:clip-a', 'hold'], present(2), 3)), reads=[])
    head, entries, documents = copy.deepcopy((realm.head, realm.entries, realm.documents))
    with pytest.raises(CommitRejected) as error:
        submit(realm, split_patch(document, ['clips'], False), base=base, reads=[])
    assert error.value.failure['details']['reason'] == 'value_conflict'
    assert (realm.head, realm.entries, realm.documents) == (head, entries, documents)
