"""Created-root redo guards the stage input, including its earlier child writes."""
from __future__ import annotations

import copy

import pytest

from astrid.sdk.timeline import _root_inverse, commit
from astrid.sdk.timeline._collections import AcceptedRecords
from astrid.sdk.timeline._command_state import ABSENT, CommandError, State, equal, present
from astrid.sdk.timeline._recording import guard
from astrid.sdk.timeline._step_undo import inverse_stages
from tests.editing_conformance import test_recorded_graph_roots as graph_fixtures
from tests.editing_conformance.test_doc_patch_v2 import step, submit
from tests.editing_conformance.test_recorded_graph_roots import child_ops as child_ops
from tests.editing_conformance.test_recorded_graph_roots import root_ops as root_ops
from tests.sdk.test_timeline_commit_provider import DetachedRealm, inverse
from tests.sdk.test_timeline_commit_provider import runtime_abi as runtime_abi

pytestmark = pytest.mark.usefixtures('child_ops')


@pytest.fixture
def created_undo(monkeypatch):
    roots = graph_fixtures.root_values

    def values():
        result = roots()
        result[1]['value']['duration_ms'] = 3000
        return result

    monkeypatch.setattr(graph_fixtures, 'root_values', values)
    realm = DetachedRealm()
    create = graph_fixtures.create_command()
    accepted = submit(realm, create, label='Create and edit child', merge='fields', reads=[],
                      steps=[step('create', 0, create, reads=[])])
    command = inverse([accepted['receipt']['receipt_id']])
    undone = submit(realm, command, label='Undo child creation', merge='fields', reads=[],
                    steps=[step('undo', 0, command, reads=[])])
    return realm, accepted, undone


@pytest.fixture
def offline_stage(created_undo):
    realm, accepted, undone = created_undo
    entry = copy.deepcopy(realm.entries[-1])
    before = commit.bundle_from_document(accepted['data']['canonical_document'])
    after = commit.bundle_from_document(undone['data']['canonical_document'])
    records = AcceptedRecords([entry], entry['records']['commands'], lambda _: (before, after))
    publication, _, commands = next(inverse_stages(records))
    state = State(copy.deepcopy(after))
    for record in reversed(commands):
        for write in reversed(record['writes']):
            guard(state, write['ref'], write['path'], write['after'], kind='history_unavailable')
            state.set(write['ref'], write['path'], write['before'])
    return state, records, publication, commands


def test_created_root_restores_stage_input_instead_of_deletion_input(offline_stage):
    state, records, publication, commands = offline_stage
    before, after = records.load_boundary(records.entries[0])
    deletion = commands[0]['writes'][-1]
    assert deletion['ref'] == 'shot:new-shot' and deletion['after'] == ABSENT
    assert deletion['before']['value']['internal_timeline']['clips'][0]['hold'] == 2
    assert state.get('occurrence:new-occ/clip:title', ['hold']) == present(3)
    assert equal(state.get('shot:new-shot', []), State(before).get('shot:new-shot', []))
    assert not state.matches('shot:new-shot', [], deletion['before'])
    assert State(after).get('shot:new-shot', []) == ABSENT
    original = copy.deepcopy((state.bundle, records.entries, commands))
    _root_inverse.restore_root_baselines(state, records, publication, commands)
    assert state.bundle['shots']['new-shot'].source == before['source_mapping']['_runtime_document']
    assert (state.bundle, records.entries, commands) == original


@pytest.mark.parametrize('damage', ['child-before', 'deletion-before'])
def test_stage_entry_match_cannot_authorize_corrupt_intermediate_values(offline_stage, damage):
    state, records, publication, commands = offline_stage
    source = commands[0]['_inverse_stage'][0]
    # Even matching recorder/partition views cannot replace sequential guards.
    source['writes'] = copy.deepcopy(source['writes'])
    write = source['writes'][0 if damage == 'child-before' else -1]
    if damage == 'child-before':
        write['before'] = present(999)
    else:
        write['before']['value']['internal_timeline']['clips'][0]['hold'] = 999
    commands[0]['writes'] = copy.deepcopy(source['writes'])
    original = copy.deepcopy((state.bundle, records.entries, commands))
    with pytest.raises(CommandError) as error:
        _root_inverse.restore_root_baselines(state, records, publication, commands)
    assert error.value.kind == 'history_unavailable'
    assert (state.bundle, records.entries, commands) == original


@pytest.mark.parametrize('selected', [False, True], ids=['whole', 'selected'])
def test_created_child_public_redo_preview_and_acceptance(created_undo, selected):
    realm, accepted, undone = created_undo
    history = copy.deepcopy(realm.entries)
    command = inverse([undone['receipt']['receipt_id']])
    if selected:
        command['args']['steps'] = {undone['receipt']['receipt_id']: ['undo']}
    controls = {'label': 'Redo child creation', 'merge': 'fields', 'reads': [],
                'steps': [step('redo', 0, command, reads=[])]}
    before = copy.deepcopy((realm.head, realm.entries, realm.documents, realm.accepted, realm.prepared))
    preview = submit(realm, command, dry_run=True, **controls)
    assert preview['receipt'] is None
    assert preview['data']['would_status'] == 'accepted'
    assert (realm.head, realm.entries, realm.documents, realm.accepted, realm.prepared) == before
    redone = submit(realm, command, **controls)
    assert redone['data']['status'] == 'accepted'
    assert redone['receipt']['result'] == redone['data']
    assert redone['data']['canonical_document'] == accepted['data']['canonical_document']
    assert redone['data']['canonical_document'] == preview['data']['canonical_document']
    assert redone['data']['commands'][0]['created'] == ['shot:new-shot', 'occurrence:new-occ']
    assert realm.entries[:-1] == history
    assert realm.entries[-1]['records']['undo_steps'] == command['args'].get('steps')
