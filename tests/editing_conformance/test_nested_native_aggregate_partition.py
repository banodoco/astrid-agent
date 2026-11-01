"""Nested aggregates complete compiler images without supplying mutation patches."""
from __future__ import annotations

import copy

import pytest

from astrid.sdk.timeline._collections import AcceptedRecords
from astrid.sdk.timeline._command_state import CommandError
from astrid.sdk.timeline._step_undo import inverse_stages, validate_nested_history
from astrid.sdk.timeline.inverse import _stage_restoration_orders
from tests.editing_conformance.test_doc_patch_v2 import submit
from tests.editing_conformance.test_inverse_clip_mirrors import land_split, original_document
from tests.editing_conformance.test_provider_step_undo_v2 import undo
from tests.sdk.test_timeline_commit_provider import DetachedRealm
from tests.sdk.test_timeline_commit_provider import runtime_abi as runtime_abi


@pytest.fixture
def aggregate_history():
    realm = DetachedRealm(original_document())
    landed = land_split(realm, ['clips'], False)
    submit(realm, undo(landed['receipt']['receipt_id']), reads=[])
    return realm


def test_native_aggregate_completes_stage_order_without_changing_mirrors(aggregate_history):
    realm = aggregate_history
    entry = realm.entries[-1]
    original = copy.deepcopy(entry)
    records = AcceptedRecords([entry], entry['records']['commands'], None)
    validate_nested_history(entry, {item['publication_id']: item for item in realm.entries}.__getitem__)
    stage = list(inverse_stages(records))[0][2][0]
    assert stage['derived_writes'] == entry['records']['commands'][0]['derived_writes']
    assert [row['id'] for row in stage['derived_writes'][-1]['after']['value']] == [
        'clip-a', 'clip-right', 'clip-b']
    assert _stage_restoration_orders(records, stage)['parent', 'clip'] == (
        ['parent/clip:clip-a', 'parent/clip:clip-right', 'parent/clip:clip-b'],
        ['parent/clip:clip-a', 'parent/clip:clip-b'])
    assert entry == original


@pytest.mark.parametrize('selected', [False, True], ids=['whole', 'selected'])
def test_aggregate_and_mirror_stages_keep_exact_images_across_inverse_chains(selected):
    document = original_document()
    realm = DetachedRealm(document)
    landed = land_split(realm, ['config', 'clips'], True, note=True)
    source = copy.deepcopy(realm.entries[-1])
    result = submit(realm, undo(landed['receipt']['receipt_id'], *(['split'] if selected else [])), reads=[])
    expected = copy.deepcopy(document)
    if selected:
        expected['parent_revision']['payload']['unknown']['note'] = 'kept'
        assert result['data']['canonical_document']['parent_revision']['payload'] == expected['parent_revision']['payload']
    else:
        assert result['data']['canonical_document'] == expected
    undone = result['data']['canonical_document']
    for target in (landed['data']['canonical_document'], undone, landed['data']['canonical_document']):
        result = submit(realm, undo(result['receipt']['receipt_id']), reads=[])
        assert result['data']['canonical_document'] == target
    assert realm.entries[1] == source


@pytest.mark.parametrize('damage', [
    'missing-mirror', 'duplicate-mirror', 'mirror-before', 'mirror-after', 'mirror-cause',
    'mirror-site', 'native-before', 'native-after', 'native-site', 'native-order',
    'missing-aggregate', 'effect-before', 'effect-after', 'range', 'source', 'step', 'ordinal',
])
def test_aggregate_partition_refuses_inconsistent_trusted_observations(aggregate_history, damage):
    # These are local guard probes, not evidence that a caller can rewrite stored history.
    history = copy.deepcopy(aggregate_history.entries)
    entry = history[-1]
    record = entry['records']['commands'][0]
    mirrors = record['derived_writes']
    aggregate = next(write for write in record['writes'] if write['path'] == ['config', 'clips'])
    boundary = next(binding['boundary'] for binding in record['bindings'] if binding['key'] == 'inverse.stage')
    effect = entry['records']['steps'][0]['patch'][0]
    if damage == 'missing-mirror':
        mirrors.pop()
    elif damage == 'duplicate-mirror':
        mirrors.insert(1, copy.deepcopy(mirrors[1]))
    elif damage == 'mirror-before':
        mirrors[1]['before']['value'][0]['hold'] = 999
    elif damage == 'mirror-after':
        mirrors[0]['after']['value'][0]['hold'] = 999
    elif damage == 'mirror-cause':
        mirrors[0]['cause'] = True
    elif damage == 'mirror-site':
        mirrors[0]['path'] = ['clips']
    elif damage == 'native-before':
        aggregate['before']['value'][0]['hold'] = 999
    elif damage == 'native-after':
        aggregate['after']['value'].reverse()
    elif damage == 'native-site':
        aggregate['ref'] = 'occurrence:foreign'
    elif damage == 'native-order':
        record['writes'][0], record['writes'][1] = record['writes'][1], record['writes'][0]
    elif damage == 'missing-aggregate':
        record['writes'].remove(aggregate)
    elif damage == 'effect-before':
        effect['before']['value'][0]['hold'] = 999
    elif damage == 'effect-after':
        effect['value'].reverse()
    elif damage == 'range':
        boundary['native_write_range'][0] = True
    elif damage == 'source':
        boundary['publication_id'] = 'foreign'
    elif damage == 'step':
        boundary['step_id'] = 'foreign'
    else:
        boundary['command_ordinals'] = [True]
    original = copy.deepcopy(history)
    with pytest.raises(CommandError) as error:
        validate_nested_history(entry, {item['publication_id']: item for item in history}.__getitem__)
    assert error.value.kind == 'history_unavailable'
    assert history == original
