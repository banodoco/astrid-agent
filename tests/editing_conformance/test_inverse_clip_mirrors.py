"""Guarded split/list and native leaf reversal through the commit provider."""
from __future__ import annotations

import copy

import pytest

from astrid.sdk.timeline._command_state import ABSENT, present
from tests.editing_conformance.test_doc_patch_mirrors import (
    declared_reads,
    original_document,
    split_patch,
)
from tests.editing_conformance.test_doc_patch_v2 import change, patch, step, submit
from tests.editing_conformance.test_provider_step_undo_v2 import undo
from tests.sdk.test_timeline_commit_provider import CommitRejected, DetachedRealm
from tests.sdk.test_timeline_commit_provider import runtime_abi as runtime_abi


def snapshot(realm):
    return copy.deepcopy((realm.head, realm.entries, realm.documents, realm.accepted, realm.prepared))


def land_split(realm, path, output_first, *, note=False):
    document = realm.documents[realm.head['revision_id']]
    call = split_patch(document, path, output_first)
    calls = [call]
    steps = [step('split', 0, call, reads=declared_reads())]
    if note:
        call = patch(change(['unknown', 'note'], ABSENT, 'kept'))
        calls.append(call)
        steps.append(step('note', 1, call))
    return submit(realm, *calls, label='Split and output', steps=steps, merge='fields', reads=[],
                  source={'via': 'python'}, rationale='Split with clip and output changes', group_id='split-group')


@pytest.mark.parametrize('path', [['clips'], ['config', 'clips']])
@pytest.mark.parametrize('output_first', [False, True])
def test_split_leaf_output_undo_redo_exact_document_and_causal_records(path, output_first):
    document = original_document()
    realm = DetachedRealm(document)
    landed = land_split(realm, path, output_first)
    assert set(landed) == {'data', 'receipt'}
    assert landed['data']['status'] == 'accepted'
    publication = landed['receipt']['receipt_id']
    original_entry = copy.deepcopy(realm.entries[-1])
    authored = original_entry['records']['commands'][0]['writes']
    assert any(write['ref'] == 'parent' and write['path'] == ['config', 'clips'] for write in authored)
    assert len(original_entry['records']['commands'][0]['derived_writes']) == 4

    undone = submit(realm, undo(publication), reads=[])
    assert undone['data']['canonical_document'] == document
    record = realm.entries[-1]['records']['commands'][0]
    assert record['writes'] == [{**write, 'before': write['after'], 'after': write['before']}
                                for write in reversed(authored)]
    assert len(record['derived_writes']) == 4
    assert record['derived_writes'] == [
        {**write, 'before': write['after'], 'after': write['before']}
        for write in reversed(original_entry['records']['commands'][0]['derived_writes'])]
    assert all(write['cause'] == 0 and write['ref'] == 'parent'
               and write['path'] == ['config', 'clips'] for write in record['derived_writes'])
    boundaries = [binding['boundary'] for binding in record['bindings'] if binding['key'] == 'inverse.stage']
    assert boundaries == [{'publication_id': publication, 'step_id': 'split', 'command_ordinals': [0],
                           'native_write_range': [0, len(authored)], 'effect_range': [0, 2]}]
    assert realm.entries[-2] == original_entry
    redone = submit(realm, undo(undone['receipt']['receipt_id']), reads=[])
    assert redone['data']['canonical_document'] == landed['data']['canonical_document']
    assert realm.entries[-3] == original_entry


@pytest.mark.parametrize('path', [['clips'], ['config', 'clips']])
@pytest.mark.parametrize('output_first', [False, True])
@pytest.mark.parametrize('selected', ['whole', 'split', 'note'])
def test_split_step_undo_retains_other_step_and_redoes_exactly(path, output_first, selected):
    document = original_document()
    realm = DetachedRealm(document)
    landed = land_split(realm, path, output_first, note=True)
    publication = landed['receipt']['receipt_id']
    original_entry = copy.deepcopy(realm.entries[-1])
    selection = [] if selected == 'whole' else [selected]
    undone = submit(realm, undo(publication, *selection), reads=[])
    expected = copy.deepcopy(document if selected != 'note' else landed['data']['canonical_document'])
    if selected == 'split':
        expected['parent_revision']['payload']['unknown']['note'] = 'kept'
    elif selected == 'note':
        del expected['parent_revision']['payload']['unknown']['note']
    actual = undone['data']['canonical_document']
    assert actual['parent_revision']['payload'] == expected['parent_revision']['payload']
    for key in ('shot_revisions', 'internal_timeline_revisions', 'dependency_manifest'):
        assert actual[key] == document[key]
    assert actual['parent_revision']['payload']['clips'] == actual['parent_revision']['payload']['config']['clips']
    if selected == 'whole':
        assert actual == document
    assert realm.entries[-1]['records']['undo_steps'] == (
        None if selected == 'whole' else {publication: [selected]})
    assert realm.entries[-2] == original_entry
    redone = submit(realm, undo(undone['receipt']['receipt_id']), reads=[])
    assert redone['data']['canonical_document'] == landed['data']['canonical_document']
    assert realm.entries[-3] == original_entry


@pytest.mark.parametrize('selected', ['whole', 'move'])
def test_split_then_native_leaf_command_restores_each_command_boundary(selected):
    document = original_document()
    realm = DetachedRealm(document)
    split = split_patch(document, ['config', 'clips'], False)
    move = {'schema_version': 1, 'op': 'clip.move', 'version': '1', 'select': 'parent/clip:clip-right',
            'args': {'on': 2.25}}
    landed = submit(realm, split, move, reads=[], steps=[step('split', 0, split), step('move', 1, move)])
    publication = landed['receipt']['receipt_id']
    original_entry = copy.deepcopy(realm.entries[-1])
    undone = submit(realm, undo(publication, *(['move'] if selected == 'move' else [])), reads=[])
    if selected == 'whole':
        assert undone['data']['canonical_document'] == document
    else:
        expected = copy.deepcopy(landed['data']['canonical_document']['parent_revision']['payload'])
        expected['clips'][1]['at'] = 2
        # clip.move also records fixed-start intent; its undo restores the exact app object.
        expected['clips'][1]['app'] = copy.deepcopy(document['parent_revision']['payload']['clips'][0]['app'])
        expected['config']['clips'] = copy.deepcopy(expected['clips'])
        assert undone['data']['canonical_document']['parent_revision']['payload'] == expected
    redone = submit(realm, undo(undone['receipt']['receipt_id']), reads=[])
    assert redone['data']['canonical_document'] == landed['data']['canonical_document']
    assert realm.entries[-3] == original_entry


@pytest.mark.parametrize('damage', ['field', 'field-aba', 'equal', 'order', 'order-aba', 'consumer'])
@pytest.mark.parametrize('selected', ['whole', 'split'])
def test_later_clip_writes_order_aba_and_consumers_refuse_without_writes(damage, selected):
    realm = DetachedRealm(original_document())
    landed = land_split(realm, ['clips'], True, note=True)
    publication = landed['receipt']['receipt_id']
    if damage in ('field', 'field-aba', 'equal'):
        value = 1.5 if damage == 'equal' else 1.75
        submit(realm, patch(change(['config', 'clips', 'id:clip-a', 'hold'], present(1.5), value)), reads=[])
        if damage == 'field-aba':
            submit(realm, patch(change(['clips', 'id:clip-a', 'hold'], present(value), 1.5)), reads=[])
    elif damage in ('order', 'order-aba'):
        row = landed['data']['canonical_document']['parent_revision']['payload']['clips'][1]
        submit(realm, patch(change(['config', 'clips'], present(row), op='list.move', id='clip-right',
                                  order_before=['clip-a', 'clip-right', 'clip-b'], after_id='clip-b')), reads=[])
        if damage == 'order-aba':
            submit(realm, patch(change(['clips'], present(row), op='list.move', id='clip-right',
                                      order_before=['clip-a', 'clip-b', 'clip-right'], after_id='clip-a')), reads=[])
    else:
        submit(realm, patch(change(['unknown', 'consumer'], ABSENT, 'kept')), reads=[
            {'kind': 'field', 'ref': 'parent', 'path': ['config', 'clips', 'id:clip-right', 'hold'],
             'state': present(1)}])
    before = snapshot(realm)
    with pytest.raises(CommitRejected) as error:
        submit(realm, undo(publication, *(['split'] if selected == 'split' else [])), reads=[])
    assert error.value.failure['details']['reason'] == (
        'inverse_conflict' if damage == 'consumer' else 'intervening_write')
    assert snapshot(realm) == before


def test_same_publication_surviving_declared_consumer_blocks_split_step():
    realm = DetachedRealm(original_document())
    split = split_patch(original_document(), ['clips'], True)
    note = patch(change(['unknown', 'consumer'], ABSENT, 'kept'))
    landed = submit(realm, split, note, reads=[], steps=[step('split', 0, split), step('consumer', 1, note, reads=[
        {'kind': 'field', 'ref': 'parent', 'path': ['config', 'clips', 'id:clip-right', 'hold'],
         'state': present(1)}])])
    before = snapshot(realm)
    with pytest.raises(CommitRejected) as error:
        submit(realm, undo(landed['receipt']['receipt_id'], 'split'), reads=[])
    assert error.value.failure['details']['reason'] == 'inverse_conflict'
    assert snapshot(realm) == before


def test_merged_split_inverse_restores_landing_head_and_redoes_exactly():
    document = original_document()
    realm = DetachedRealm(document)
    base = copy.deepcopy(realm.head)
    other = submit(realm, patch(change(['unknown', 'note'], ABSENT, 'kept')), reads=[])
    call = split_patch(document, ['config', 'clips'], False)
    landed = submit(realm, call, base=base, merge='fields', reads=declared_reads(),
                    steps=[step('split', 0, call)])
    # DetachedRealm retains the historical rebased receipt spelling.
    assert landed['data']['status'] == 'rebased'
    assert realm.entries[-1]['previous']['publication_id'] == other['receipt']['receipt_id']
    undone = submit(realm, undo(landed['receipt']['receipt_id']), reads=[])
    assert undone['data']['canonical_document'] == other['data']['canonical_document']
    redone = submit(realm, undo(undone['receipt']['receipt_id']), reads=[])
    assert redone['data']['canonical_document'] == landed['data']['canonical_document']


@pytest.mark.parametrize('damage', ['missing-history', 'missing-commands', 'missing-steps'])
def test_unprovable_split_history_refuses_without_writes(damage):
    realm = DetachedRealm(original_document())
    landed = land_split(realm, ['clips'], True)
    publication = landed['receipt']['receipt_id']
    records = realm.entries[-1]['records']
    if damage == 'missing-history':
        realm.entries[-1]['records'] = None
    elif damage == 'missing-commands':
        del records['commands']
    else:
        del records['steps']
    before = snapshot(realm)
    with pytest.raises(CommitRejected) as error:
        submit(realm, undo(publication, 'split'), reads=[])
    assert error.value.failure['details']['reason'] == 'history_unavailable'
    assert snapshot(realm) == before
