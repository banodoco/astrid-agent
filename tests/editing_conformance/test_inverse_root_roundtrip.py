"""Physical root proofs remain exact across repeated guarded inversions."""
from __future__ import annotations

import copy

import pytest

from astrid.sdk.timeline._collections import AcceptedRecords
from astrid.sdk.timeline._command_state import CommandError, present
from astrid.sdk.timeline._step_undo import inverse_stages
from tests.editing_conformance.test_doc_patch_root_assignment import replacement_calls
from tests.editing_conformance.test_doc_patch_v2 import step, submit
from tests.editing_conformance.test_provider_step_undo_v2 import undo
from tests.sdk.test_timeline_commit_provider import DetachedRealm
from tests.sdk.test_timeline_commit_provider import runtime_abi as runtime_abi
from tests.sdk.test_timeline_commit_provider_document import document_fixture


@pytest.mark.parametrize('selected', [False, True])
def test_physical_root_repeated_roundtrip_keeps_absence_and_sources(selected):
    document = document_fixture()
    calls, _ = replacement_calls(document, separate=True)
    realm = DetachedRealm(document)
    landed = submit(realm, *calls, reads=[],
                    steps=[step('config', 0, calls[0]), step('clips', 1, calls[1])])
    original = copy.deepcopy(realm.entries[-1])
    result = submit(realm, undo(landed['receipt']['receipt_id'], *(['clips'] if selected else [])), reads=[])
    absent = copy.deepcopy(result['data']['canonical_document'])
    if selected:
        assert 'clips' not in absent['parent_revision']['payload']
        assert 'clips' not in absent['parent_revision']['payload']['config']
    else:
        assert absent == document
    for expected in (landed['data']['canonical_document'], absent, landed['data']['canonical_document']):
        result = submit(realm, undo(result['receipt']['receipt_id']), reads=[])
        assert result['data']['canonical_document'] == expected
        for key in ('shot_revisions', 'internal_timeline_revisions', 'dependency_manifest'):
            assert result['data']['canonical_document'][key] == document[key]
    assert realm.entries[1] == original


@pytest.mark.parametrize('damage', ['before', 'source-order', 'order', 'mirror', 'landing'])
def test_physical_root_partition_rejects_damaged_native_and_effect_proofs(damage):
    document = document_fixture()
    calls, _ = replacement_calls(document)
    realm = DetachedRealm(document)
    landed = submit(realm, *calls, reads=[])
    submit(realm, undo(landed['receipt']['receipt_id']), reads=[])
    original = copy.deepcopy(realm.entries[-1])
    entry = copy.deepcopy(original)
    record = entry['records']['commands'][0]
    effect = entry['records']['steps'][0]['patch'][0]
    if damage == 'before':
        record['writes'][0]['before'] = present(99)
    elif damage == 'source-order':
        record['writes'][:2] = reversed(record['writes'][:2])
    elif damage == 'order':
        record['writes'][1]['before']['value'].reverse()
    elif damage == 'mirror':
        effect['before']['value']['config']['clips'] = copy.deepcopy(effect['before']['value']['clips'])
    else:
        effect['value']['clips'].reverse()
    captured = copy.deepcopy(entry)
    records = AcceptedRecords([entry], entry['records']['commands'], None)
    with pytest.raises(CommandError) as error:
        list(inverse_stages(records))
    assert error.value.kind == 'history_unavailable'
    assert entry == captured
    assert realm.entries[-1] == original
