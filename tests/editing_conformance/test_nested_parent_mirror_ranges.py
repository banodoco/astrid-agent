"""Accepted parent mirrors stay attached to their immediate inverse stages."""
from __future__ import annotations

import copy

import pytest

from astrid.sdk.timeline._collections import AcceptedRecords
from astrid.sdk.timeline._command_state import CommandError
from astrid.sdk.timeline._doc_patch import inverse_changes
from astrid.sdk.timeline._step_undo import inverse_stages, validate_nested_history
from astrid.sdk.timeline.capture import capture_move
from astrid.sdk.timeline.commands import execute
from astrid.sdk.timeline.commit import bundle_from_document
from tests.editing_conformance.test_doc_patch_v2 import step, submit
from tests.editing_conformance.test_recipe_m2_parent_media import calls, captured, replaced
from tests.editing_conformance.test_recipe_occurrence_media import MediaRealm
from tests.sdk.test_timeline_commit_provider import DetachedRealm, inverse
from tests.sdk.test_timeline_commit_provider import runtime_abi as runtime_abi


@pytest.fixture(params=[False, True], ids=['add', 'replace'])
def history(request):
    document, _, command, verified = replaced() if request.param else captured('video')
    realm = MediaRealm(document, verified)
    created = realm.call(calls(command))
    realm.call([inverse([created['receipt']['receipt_id']])])
    return copy.deepcopy(realm.entries)


def records(history):
    entry = history[-1]
    return AcceptedRecords([entry], entry['records']['commands'], None)


@pytest.mark.parametrize('selected', [False, True], ids=['whole', 'selected'])
def test_provider_nested_mirrors_keep_each_immediate_partition(selected):
    document, _, _, _ = captured('video')
    realm = DetachedRealm(document)
    bundle = bundle_from_document(document)
    first = capture_move(bundle, 'parent/clip:clip-a', 3)
    second = capture_move(execute(bundle, [first]).bundle, 'parent/clip:clip-b', 7)
    landed = submit(realm, first, second, label='Move two clips', merge='fields', reads=[],
                    group_id='mirror-regression', steps=[step('a', 0, first), step('b', 1, second)])
    command = inverse([landed['receipt']['receipt_id']])
    if selected:
        command['args']['steps'] = {landed['receipt']['receipt_id']: ['a']}
    undone = submit(realm, command, reads=[], label='Undo moves')
    source = copy.deepcopy(realm.entries[-1])
    stages = list(inverse_stages(records(realm.entries)))
    assert len(stages) == (1 if selected else 2)
    assert all((publication, identity, commands[0]['ordinal']) == (
        source['publication_id'], source['records']['steps'][0]['id'], 0)
        for publication, identity, commands in stages)
    assert [write for _, _, commands in reversed(stages) for write in commands[0]['derived_writes']] == (
        source['records']['commands'][0]['derived_writes'])
    redone = submit(realm, inverse([undone['receipt']['receipt_id']]), reads=[], label='Redo moves')
    assert redone['data']['canonical_document'] == landed['data']['canonical_document']
    assert realm.entries[-2] == source


def test_accepted_mirror_chain_preserves_source_and_immediate_identity(history):
    original = copy.deepcopy(history)
    entry = history[-1]
    validate_nested_history(entry, {item['publication_id']: item for item in history}.__getitem__)
    groups = list(inverse_stages(records(history)))
    assert len(groups) == 1
    publication, step, commands = groups[0]
    assert (publication, step) == (entry['publication_id'], entry['records']['steps'][0]['id'])
    assert commands[0]['ordinal'] == entry['records']['commands'][0]['ordinal']
    assert commands[0]['writes'] == entry['records']['commands'][0]['writes']
    assert commands[0]['derived_writes'] == entry['records']['commands'][0]['derived_writes']
    assert commands[0]['_inverse_effects'] == inverse_changes(entry['records']['steps'][0]['patch'])
    assert history == original


@pytest.mark.parametrize('mutation', [
    'native-gap', 'native-overlap', 'native-bool', 'effect-gap', 'effect-overlap',
    'publication', 'step', 'ordinal', 'cause', 'foreign-derived', 'mirror-before',
    'mirror-after', 'effect-image', 'missing-source', 'foreign-source',
])
def test_nested_parent_mirror_authority_refuses_tampering(history, mutation):
    entry = history[-1]
    record = entry['records']['commands'][0]
    boundary = next(binding['boundary'] for binding in record['bindings'] if binding['key'] == 'inverse.stage')
    derived = record['derived_writes'][0]
    sources = {item['publication_id']: item for item in history}
    if mutation == 'native-gap':
        boundary['native_write_range'][0] += 1
    elif mutation == 'native-overlap':
        boundary['native_write_range'][0] = -1
    elif mutation == 'native-bool':
        boundary['native_write_range'][0] = False
    elif mutation == 'effect-gap':
        boundary['effect_range'][0] += 1
    elif mutation == 'effect-overlap':
        boundary['effect_range'][1] += 1
    elif mutation == 'publication':
        boundary['publication_id'] = 'foreign-publication'
    elif mutation == 'step':
        boundary['step_id'] = 'foreign-step'
    elif mutation == 'ordinal':
        boundary['command_ordinals'] = [999]
    elif mutation == 'cause':
        derived['cause'] = True
    elif mutation == 'foreign-derived':
        derived['ref'] = 'occurrence:foreign'
    elif mutation == 'mirror-before':
        derived['before']['value'] = []
    elif mutation == 'mirror-after':
        record['derived_writes'][-1]['after']['value'] = []
    elif mutation == 'effect-image':
        entry['records']['steps'][0]['patch'][0]['value'] = []
    elif mutation == 'missing-source':
        sources[history[-2]['publication_id']] = {}
    else:
        sources[history[-2]['publication_id']]['publication_id'] = 'foreign-source'
    original = copy.deepcopy(history)
    with pytest.raises(CommandError) as error:
        validate_nested_history(entry, sources.__getitem__)
    assert error.value.kind == 'history_unavailable'
    assert history == original
