"""Physical clip births retain raw parent storage through guarded undo/redo."""
from __future__ import annotations

import copy

import pytest

from astrid.sdk.timeline._command_state import ABSENT, CommandError, State, present
from astrid.sdk.timeline.commands import execute
from astrid.sdk.timeline.commit import _digest, bundle_from_document
from astrid.sdk.timeline.inverse import coalesce
from tests.editing_conformance.test_doc_patch_root_assignment import replacement_calls
from tests.editing_conformance.test_doc_patch_v2 import change, patch, step, submit
from tests.editing_conformance.test_inverse_clip_mirrors import snapshot
from tests.editing_conformance.test_provider_step_undo_v2 import undo
from tests.editing_conformance.test_recipe_m1_commands import inverse
from tests.sdk.test_timeline_commit_provider import CommitRejected, DetachedRealm
from tests.sdk.test_timeline_commit_provider import runtime_abi as runtime_abi
from tests.sdk.test_timeline_commit_provider_document import document_fixture


@pytest.mark.parametrize('separate', [False, True])
def test_clip_birth_coalesces_only_authentic_physical_landing_values(separate):
    document = document_fixture()
    calls, expected = replacement_calls(document, separate=separate)
    plan = execute(bundle_from_document(document), calls, commit_request={'reads': []})
    records = copy.deepcopy(plan.records)
    head = State(plan.bundle)
    changes = coalesce(plan.records, head)
    assert {'ref': 'parent', 'path': ['config'],
            'before': present(document['parent_revision']['payload']['config']),
            'after': present(expected['config'])} in changes
    assert {'ref': 'parent', 'path': ['clips'],
            'before': present(document['parent_revision']['payload']['clips']),
            'after': present(expected['clips'])} in changes
    assert all(head.get(write['ref'], write['path']) == write['after'] for write in changes)
    assert plan.records == records


def test_clip_birth_then_config_only_storage_keeps_each_source_stage():
    document = document_fixture()
    calls, _ = replacement_calls(document)
    rows = copy.deepcopy(calls[0]['args']['changes'][1]['value'])
    rows[0]['hold'] = 3
    final = copy.deepcopy(rows)
    for row in final:
        row['id'] = 'final-' + row['id']
    calls[0]['args']['changes'].extend([
        change(['clips'], present(rows), op='remove'),
        change(['config', 'clips'], ABSENT, final),
        change(['config', 'clips', 'id:final-new-clip-a', 'hold'], present(3), 4),
    ])
    calls[0]['created'].extend('parent/clip:' + row['id'] for row in final)
    plan = execute(bundle_from_document(document), calls, commit_request={'reads': []})
    records = copy.deepcopy(plan.records)
    head = State(plan.bundle)
    assert head.get('parent', ['clips']) == ABSENT
    changes = coalesce(plan.records, head)
    assert all(head.get(write['ref'], write['path']) == write['after'] for write in changes)
    assert plan.records == records


@pytest.mark.parametrize('separate', [False, True])
@pytest.mark.parametrize('tag', [ABSENT, present(None), present({'opaque': [None, False, 0]})])
def test_clip_birth_roundtrip_preserves_raw_storage_and_recorded_boundaries(separate, tag):
    document = document_fixture()
    parent = document['parent_revision']['payload']
    if tag != ABSENT:
        parent['caption'] = copy.deepcopy(tag['value'])
        parent['config']['caption'] = copy.deepcopy(tag['value'])
    document['parent_revision']['content_digest'] = _digest(parent)
    calls, _ = replacement_calls(document, separate=separate)
    calls[-1]['args']['changes'].append(change(['caption'], tag, 'changed'))
    realm = DetachedRealm(document)
    landed = submit(realm, *calls, reads=[])
    original = copy.deepcopy(realm.entries[-1])
    records = original['records']['commands']
    head = State(bundle_from_document(landed['data']['canonical_document']))
    for write in coalesce(records, head):
        assert head.get(write['ref'], write['path']) == write['after']
    assert realm.entries[-1] == original
    undone = submit(realm, undo(landed['receipt']['receipt_id']), reads=[])
    assert undone['data']['canonical_document'] == document
    assert realm.entries[-1]['records']['commands'][0]['writes'] == [
        {**write, 'before': write['after'], 'after': write['before']}
        for record in reversed(records) for write in reversed(record['writes'])]
    redone = submit(realm, undo(undone['receipt']['receipt_id']), reads=[])
    assert redone['data']['canonical_document'] == landed['data']['canonical_document']
    assert realm.entries[-3] == original


def test_clip_birth_step_undo_restores_absence_then_redoes_exactly():
    document = document_fixture()
    calls, _ = replacement_calls(document, separate=True)
    realm = DetachedRealm(document)
    landed = submit(realm, *calls, reads=[], steps=[step('config', 0, calls[0]), step('clips', 1, calls[1])])
    undone = submit(realm, undo(landed['receipt']['receipt_id'], 'clips'), reads=[])
    parent = undone['data']['canonical_document']['parent_revision']['payload']
    assert 'clips' not in parent
    assert parent['config'] == calls[0]['args']['changes'][0]['value']
    redone = submit(realm, undo(undone['receipt']['receipt_id']), reads=[])
    assert redone['data']['canonical_document'] == landed['data']['canonical_document']


@pytest.mark.parametrize('damage', ['field', 'aba', 'equal', 'order', 'membership', 'foreign'])
def test_clip_birth_intervening_guards_refuse_without_writes(damage):
    document = document_fixture()
    calls, _ = replacement_calls(document)
    realm = DetachedRealm(document)
    landed = submit(realm, *calls, reads=[])
    rows = landed['data']['canonical_document']['parent_revision']['payload']['clips']
    if damage in ('field', 'aba', 'equal'):
        value = 3 if damage == 'equal' else 4
        submit(realm, patch(change(['clips', 'id:new-clip-a', 'hold'], present(3), value)), reads=[])
        if damage == 'aba':
            submit(realm, patch(change(['clips', 'id:new-clip-a', 'hold'], present(value), 3)), reads=[])
    elif damage == 'order':
        submit(realm, patch(change(['clips'], present(rows[0]), op='list.move', id=rows[0]['id'],
                                  order_before=[row['id'] for row in rows], after_id=rows[1]['id'])), reads=[])
    elif damage == 'membership':
        submit(realm, patch(change(['clips'], present(rows[1]), op='list.remove', id=rows[1]['id'],
                                  order_before=[row['id'] for row in rows])), reads=[])
    else:
        extra = {**copy.deepcopy(rows[1]), 'id': 'foreign'}
        submit(realm, patch(change(['clips'], ABSENT, extra, op='list.insert', id='foreign',
                                  order_before=[row['id'] for row in rows], after_id=rows[-1]['id']),
                            created=['parent/clip:foreign']), reads=[])
    before = snapshot(realm)
    with pytest.raises(CommitRejected) as error:
        submit(realm, undo(landed['receipt']['receipt_id']), reads=[])
    assert error.value.failure['details']['reason'] == 'intervening_write'
    assert snapshot(realm) == before


@pytest.mark.parametrize('damage', ['before', 'source-order'])
def test_clip_birth_sequential_source_guards_refuse_without_mutation(damage):
    document = document_fixture()
    calls, _ = replacement_calls(document)
    plan = execute(bundle_from_document(document), calls, commit_request={'reads': []})
    records = copy.deepcopy(plan.records)
    writes = records[0]['writes']
    if damage == 'before':
        writes[-1]['before'] = present(99)
    else:
        writes[-1], writes[-2] = writes[-2], writes[-1]
    before = copy.deepcopy((plan.bundle, records))
    with pytest.raises(CommandError):
        inverse(plan.bundle, records)
    assert (plan.bundle, records) == before
