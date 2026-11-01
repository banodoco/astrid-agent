"""Shared native roots use accepted boundaries without admitting managed writes."""
from __future__ import annotations

import copy

import pytest

from astrid.core.timeline.authoring_bundle import _RestoredShot
from astrid.sdk.timeline._collections import AcceptedRecords
from astrid.sdk.timeline._command_state import ABSENT, CommandError, State
from astrid.sdk.timeline._root_inverse import restore_root_baselines
from astrid.sdk.timeline.commit import _digest, _manifest, bundle_from_document
from astrid.sdk.timeline.native_timeline_duplicate import physical_content
from tests.sdk.test_timeline_commit_provider_document import document_fixture


def shared_root_boundary():
    before = bundle_from_document(document_fixture())
    physical = physical_content(before)
    source = State(physical)
    writes = [{'ref': 'occurrence:' + row['occurrence_id'], 'path': [],
               'before': source.get('occurrence:' + row['occurrence_id'], []), 'after': ABSENT}
              for row in reversed(physical['placements'])]
    writes.append({'ref': 'shot:chapter-shot', 'path': [],
                   'before': source.get('shot:chapter-shot', []), 'after': ABSENT})
    original = {'command': {'op': 'timeline.inverse'}, 'ordinal': 0, 'writes': writes,
                'reads': [], 'selections': [], 'created': [], 'derived_writes': [], 'bindings': []}
    entry = {'publication_id': 'accepted-undo', 'records': {'commands': [original]}}
    after_document = copy.deepcopy(before['source_mapping']['_runtime_document'])
    parent = after_document['parent_revision']
    parent['payload']['occurrences'] = []
    parent.update(revision_id='removed-root', content_digest=_digest(parent['payload']))
    after_document['shot_revisions'], after_document['internal_timeline_revisions'] = [], []
    manifest = after_document['dependency_manifest']
    after_document['dependency_manifest'] = _manifest([], [], manifest['media'], manifest['documents'])
    after = bundle_from_document(after_document)
    records = AcceptedRecords([entry], [original], lambda entry: (before, after))
    staged = {**original, '_inverse_effects': [], '_inverse_stage': (
        original, {}, {'native_write_range': [0, len(writes)]})}
    restored = copy.deepcopy(physical)
    restored['shots']['chapter-shot']['_created'] = True
    restored['source_mapping']['shots'] = {}
    return State(restored), records, staged


def test_shared_root_replay_uses_exact_physical_boundary_without_changing_history():
    state, records, staged = shared_root_boundary()
    original = copy.deepcopy(records.entries)
    snapshot = copy.deepcopy(state.bundle)
    restore_root_baselines(state, records, 'accepted-undo', [staged])
    assert state.bundle == snapshot
    assert not isinstance(state.bundle['shots']['chapter-shot'], _RestoredShot)
    assert records.entries == original
    assert {row['shot_id'] for row in state.bundle['placements']} == {'chapter-shot'}


@pytest.mark.parametrize('damage', ['managed-revision', 'forged-revision', 'unknown-field', 'source-owner',
                                  'source-pin', 'missing-creation', 'missing-owner'])
def test_shared_root_replay_refuses_forged_authored_or_source_images(damage):
    state, records, staged = shared_root_boundary()
    if damage in ('managed-revision', 'forged-revision', 'unknown-field', 'source-owner'):
        row = records[0]['writes'][0]['before']['value']
        if damage in ('managed-revision', 'forged-revision'):
            row['shot_revision_id'] = 'shot-1' if damage == 'managed-revision' else 'forged-revision'
        elif damage == 'unknown-field':
            row['unexpected'] = None
        else:
            row['shot_id'] = 'forged-shot'
    elif damage == 'source-pin':
        state.bundle['source_mapping']['shots']['chapter-shot'] = {'revision_id': 'forged-revision'}
    elif damage == 'missing-creation':
        state.bundle['shots']['chapter-shot']['_created'] = False
    else:
        state.bundle['placements'].pop()
    snapshot, history = copy.deepcopy(state.bundle), copy.deepcopy(records.entries)
    with pytest.raises(CommandError) as error:
        restore_root_baselines(state, records, 'accepted-undo', [staged])
    assert error.value.kind == 'history_unavailable'
    assert state.bundle == snapshot
    assert records.entries == history
