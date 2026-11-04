"""Nested native stages retain exact root guards and accepted compiler provenance."""
from __future__ import annotations

import copy

import pytest

from astrid.sdk.timeline import _root_inverse
from astrid.sdk.timeline._command_state import CommandError, present
from astrid.sdk.timeline.commit import bundle_from_document
from astrid.sdk.timeline.native_occurrence_create import capture_occurrence_create
from tests.editing_conformance.test_doc_patch_v2 import change, patch, step, submit
from tests.editing_conformance.test_provider_step_undo_v2 import undo
from tests.sdk.test_timeline_commit_provider import DetachedRealm
from tests.sdk.test_timeline_commit_provider import runtime_abi as runtime_abi


@pytest.fixture
def root_stage(monkeypatch):
    realm = DetachedRealm()
    bundle = bundle_from_document(realm.documents[realm.head['revision_id']])
    create = capture_occurrence_create(bundle, name='New', track='picture', on=10, duration=2,
                                      occurrence_id='new-occ', shot_id='new-shot')
    edit = patch(change(['name'], present('New'), 'Edited', ref='shot:new-shot'))
    landed = submit(realm, create, edit, reads=[], steps=[step('create', 0, create), step('edit', 1, edit)])
    undone = submit(realm, undo(landed['receipt']['receipt_id']), reads=[])
    captured = []

    class StageReached(Exception):
        pass

    def capture(state, records, publication, commands):
        captured.append((state, records, publication, commands))
        raise StageReached

    before = copy.deepcopy((realm.head, realm.entries, realm.documents, realm.accepted, realm.prepared))
    with monkeypatch.context() as scoped:
        scoped.setattr(_root_inverse, 'restore_root_baselines', capture)
        with pytest.raises(StageReached):
            submit(realm, undo(undone['receipt']['receipt_id']), reads=[])
    assert (realm.head, realm.entries, realm.documents, realm.accepted, realm.prepared) == before
    assert len(captured) == 1
    return captured[0]


def test_native_stage_keeps_authored_root_and_full_accepted_baseline(root_stage):
    state, records, publication, commands = root_stage
    before, _ = records.load_boundary(records.entries[0])
    assert before['shots']['new-shot']['payload']['name'] == 'Edited'
    assert state.bundle['shots']['new-shot']['payload']['name'] == 'New'
    original = copy.deepcopy((state.bundle, list(records), records.entries, commands))
    _root_inverse.restore_root_baselines(state, records, publication, commands)
    shot = state.bundle['shots']['new-shot']
    assert type(shot).__name__ == '_RestoredShot'
    assert shot.source == before['source_mapping']['_runtime_document']
    assert (state.bundle, list(records), records.entries, commands) == original


@pytest.mark.parametrize('damage', [
    'payload', 'internal', 'owner', 'source-command', 'range', 'stage-writes',
    'native-before', 'native-after', 'source-after',
])
def test_native_stage_root_or_source_damage_refuses_without_mutation(root_stage, damage):
    state, records, publication, commands = root_stage
    original = commands[0]['_inverse_stage'][0]
    accepted = records.entries[0]['records']['commands'][0]
    if damage == 'payload':
        state.bundle['shots']['new-shot']['payload']['name'] = 'Wrong'
    elif damage == 'internal':
        state.bundle['shots']['new-shot']['internal_timeline']['tracks'].append({'id': 'foreign'})
    elif damage == 'owner':
        next(row for row in state.bundle['placements'] if row['shot_id'] == 'new-shot')[
            'occurrence_id'] = 'foreign'
    elif damage in ('source-command', 'range'):
        source, step_record, boundary = copy.deepcopy(commands[0]['_inverse_stage'])
        if damage == 'source-command':
            source['ordinal'] = 100
        else:
            boundary['native_write_range'] = [0, 2]
        commands[0]['_inverse_stage'] = source, step_record, boundary
    elif damage == 'stage-writes':
        commands[0]['writes'] = copy.deepcopy(commands[0]['writes'])
        commands[0]['writes'][-1]['before']['value']['payload']['name'] = 'Wrong'
    elif damage in ('native-before', 'native-after'):
        # Corrupt both views so equality with the accepted recorder alone is
        # insufficient: native replay must still prove its boundary values.
        for record in {id(original): original, id(accepted): accepted}.values():
            record['writes'] = copy.deepcopy(record['writes'])
            if damage == 'native-before':
                record['writes'][0]['before'] = present('Wrong')
            else:
                removal = record['writes'][-1]
                record['writes'].append({**copy.deepcopy(removal),
                                        'before': copy.deepcopy(removal['after']),
                                        'after': copy.deepcopy(removal['before'])})
        commands[0]['writes'] = copy.deepcopy(original['writes'][1:3])
    else:
        load = records.load_boundary

        def changed(entry):
            before, after = load(entry)
            after = copy.deepcopy(after)
            after['shots']['new-shot'] = copy.deepcopy(before['shots']['new-shot'])
            return before, after

        records.load_boundary = changed
    before = copy.deepcopy((state.bundle, list(records), records.entries, commands))
    with pytest.raises(CommandError) as error:
        _root_inverse.restore_root_baselines(state, records, publication, commands)
    assert error.value.kind == 'history_unavailable'
    assert (state.bundle, list(records), records.entries, commands) == before
