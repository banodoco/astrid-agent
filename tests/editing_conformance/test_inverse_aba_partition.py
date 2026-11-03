"""Native repeated-value stages retain their causal mirror partitions."""
from __future__ import annotations

import copy

import pytest

from astrid.sdk.timeline._collections import AcceptedRecords
from astrid.sdk.timeline._command_state import CommandError
from astrid.sdk.timeline._step_undo import inverse_stages, validate_nested_history
from astrid.sdk.timeline.capture import capture_move
from astrid.sdk.timeline.commands import execute
from astrid.sdk.timeline.commit import bundle_from_document
from tests.editing_conformance.test_doc_patch_v2 import step, submit
from tests.editing_conformance.test_inverse_clip_mirrors import land_split, original_document
from tests.editing_conformance.test_provider_step_undo_v2 import undo
from tests.sdk.test_timeline_commit_provider import CommitRejected, DetachedRealm
from tests.sdk.test_timeline_commit_provider import runtime_abi as runtime_abi


@pytest.fixture
def aba_history():
    realm = DetachedRealm(original_document())
    land_split(realm, ['config', 'clips'], False)
    bundle = bundle_from_document(realm.documents[realm.head['revision_id']])
    first = capture_move(bundle, 'parent/clip:clip-right', 2.25)
    second = capture_move(execute(bundle, [first]).bundle, 'parent/clip:clip-right',
                          2.2355666666666667)
    landed = submit(realm, first, second, label='Native ABA', merge='fields', reads=[],
                    source={'via': 'python'}, rationale='Return to the original clip position',
                    group_id='native-aba', steps=[step('a', 0, first), step('b', 1, second)])
    undone = submit(realm, undo(landed['receipt']['receipt_id']), reads=[], label='Undo native ABA')
    return realm, landed, undone


@pytest.mark.parametrize('selected', [False, True], ids=['whole', 'selected'])
def test_native_aba_zero_effect_stage_redoes_exact_closure(aba_history, selected):
    realm, landed, undone = aba_history
    history = copy.deepcopy(realm.entries)
    entry = realm.entries[-1]
    record = entry['records']['commands'][0]
    boundaries = [binding['boundary'] for binding in record['bindings'] if binding['key'] == 'inverse.stage']
    assert boundaries == [
        {'publication_id': landed['receipt']['receipt_id'], 'step_id': 'b', 'command_ordinals': [1],
         'native_write_range': [0, 9], 'effect_range': [0, 0]},
        {'publication_id': landed['receipt']['receipt_id'], 'step_id': 'a', 'command_ordinals': [0],
         'native_write_range': [9, 19], 'effect_range': [0, 2]},
    ]
    assert len(record['derived_writes']) == 4
    assert all(write['before'] == write['after'] for write in record['writes'][:9])
    stages = list(inverse_stages(AcceptedRecords([entry], [record], None)))
    assert [len(commands[0]['derived_writes']) for _, _, commands in stages] == [4, 0]
    assert [write for _, _, commands in reversed(stages) for write in commands[0]['derived_writes']] == (
        record['derived_writes'])
    for expected in (landed['data']['canonical_document'], undone['data']['canonical_document'],
                     landed['data']['canonical_document']):
        target = realm.entries[-1]
        selection = [target['records']['steps'][0]['id']] if selected else []
        command = undo(target['publication_id'], *selection)
        controls = {'label': 'Reverse native ABA', 'merge': 'fields', 'reads': [],
                    'steps': [step('reverse', 0, command)], 'source': {'via': 'python'},
                    'rationale': 'Guarded native ABA inverse', 'group_id': 'aba-inverse'}
        before = copy.deepcopy((realm.head, realm.entries, realm.documents, realm.accepted))
        preview = submit(realm, command, dry_run=True, **controls)
        assert preview['data']['status'] == 'preview'
        assert preview['data']['would_status'] == 'accepted'
        assert preview['receipt'] is None
        assert (realm.head, realm.entries, realm.documents, realm.accepted) == before
        result = submit(realm, command, dry_run=False, **controls)
        assert result['data']['status'] == 'accepted'
        assert result['receipt']['result'] == result['data']
        assert result['data']['canonical_document'] == expected
    assert realm.entries[:len(history)] == history


@pytest.mark.parametrize('damage', [
    'empty-all-effects', 'shift-native', 'effect-overlap', 'ordinal', 'step', 'source',
    'cause', 'site', 'mirror-before', 'mirror-after', 'missing-mirror', 'extra-mirror',
    'native-before', 'native-after', 'native-order', 'effect-before', 'effect-after',
])
def test_native_aba_partition_rejects_tampered_attribution(aba_history, damage):
    # Detached copies probe guards; the positive history is always provider-produced.
    history = copy.deepcopy(aba_history[0].entries)
    entry = history[-1]
    record = entry['records']['commands'][0]
    boundaries = [binding['boundary'] for binding in record['bindings'] if binding['key'] == 'inverse.stage']
    mirrors = record['derived_writes']
    if damage == 'empty-all-effects':
        boundaries[1]['effect_range'] = [0, 0]
    elif damage == 'shift-native':
        boundaries[0]['native_write_range'][1] += 1
        boundaries[1]['native_write_range'][0] += 1
    elif damage == 'effect-overlap':
        boundaries[1]['effect_range'][0] = -1
    elif damage == 'ordinal':
        boundaries[0]['command_ordinals'] = [0]
    elif damage == 'step':
        boundaries[0]['step_id'] = 'a'
    elif damage == 'source':
        boundaries[0]['publication_id'] = 'foreign'
    elif damage == 'cause':
        mirrors[0]['cause'] = True
    elif damage == 'site':
        mirrors[0]['path'] = ['clips']
    elif damage == 'mirror-before':
        mirrors[0]['before']['value'][0]['hold'] = 999
    elif damage == 'mirror-after':
        mirrors[0]['after']['value'][0]['hold'] = 999
    elif damage == 'missing-mirror':
        mirrors.pop(0)
    elif damage == 'extra-mirror':
        mirrors.insert(0, copy.deepcopy(mirrors[0]))
    elif damage == 'native-before':
        record['writes'][0]['before'] = {'state': 'present', 'value': 999}
    elif damage == 'native-after':
        record['writes'][0]['after'] = {'state': 'present', 'value': 999}
    elif damage == 'native-order':
        record['writes'][0], record['writes'][9] = record['writes'][9], record['writes'][0]
    elif damage == 'effect-before':
        entry['records']['steps'][0]['patch'][0]['before']['value'] = 999
    else:
        entry['records']['steps'][0]['patch'][-1]['value'] = 999
    before = copy.deepcopy(history)
    with pytest.raises(CommandError) as error:
        validate_nested_history(entry, {item['publication_id']: item for item in history}.__getitem__)
    assert error.value.kind == 'history_unavailable'
    assert history == before


@pytest.mark.parametrize('damage', ['writes', 'publication', 'step'])
def test_native_aba_public_inverse_refuses_forged_authority(aba_history, damage):
    realm, _, undone = aba_history
    command = undo(undone['receipt']['receipt_id'])
    if damage == 'writes':
        command['writes'] = copy.deepcopy(realm.entries[-1]['records']['commands'][0]['writes'])
    elif damage == 'publication':
        command['args']['publications'] = ['foreign-publication']
    else:
        command['args']['steps'] = {undone['receipt']['receipt_id']: ['foreign-step']}
    before = copy.deepcopy((realm.head, realm.entries, realm.documents, realm.accepted, realm.prepared))
    with pytest.raises(CommitRejected) as error:
        submit(realm, command, reads=[], merge='fields', label='Guard native ABA',
               source={'via': 'python'}, rationale='Reject forged inverse authority',
               group_id='aba-refusal', steps=[step('guard', 0, command)])
    assert error.value.failure['details']['reason'] == {
        'writes': 'invalid_edit_command', 'publication': 'history_unavailable', 'step': 'invalid_steps',
    }[damage]
    details = error.value.failure['details']
    if damage != 'publication':
        # The detached port's missing-history refusal has its own legacy shape.
        # Provider-originated failures retain the structured v2 details.
        assert details['code'] == details['reason']
        assert isinstance(details['path'], list)
        assert details['hint'] and details['suggested_command']
        assert details['current'] == (None if damage == 'writes' else realm.head)
    assert (realm.head, realm.entries, realm.documents, realm.accepted, realm.prepared) == before
