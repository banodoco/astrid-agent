"""Registered fork through the production provider's v2 commit entry point."""
from __future__ import annotations

import copy

import pytest

from astrid.sdk.timeline._command_state import present
from astrid.sdk.timeline.commit import bundle_from_document
from astrid.sdk.timeline.native_occurrence_fork import capture_occurrence_fork
from tests.editing_conformance.test_doc_patch_v2 import change, patch, step, submit
from tests.editing_conformance.test_recipe_occurrence_fork import fork_document
from tests.sdk.test_timeline_commit_provider import CommitRejected, DetachedRealm, inverse
from tests.sdk.test_timeline_commit_provider import runtime_abi as runtime_abi


def fork(realm):
    bundle = bundle_from_document(realm.documents[realm.head['revision_id']])
    return capture_occurrence_fork(bundle, 'occurrence:occ-0', 'shot-fork')


@pytest.mark.parametrize('distinct', [False, True])
def test_v2_registered_fork_reply_physical_effects_step_reads_and_undo(distinct):
    document = fork_document(distinct=distinct)
    realm = DetachedRealm(document)
    call = fork(realm)
    result = submit(realm, call, label='Fork selected occurrence', merge='fields', reads=[],
                    steps=[step('fork', 0, call)], source={'via': 'app'},
                    rationale='isolate selected child', group_id='gesture-1')
    data = result['data']
    assert data['status'] == 'accepted'
    assert data['canonical_document']['parent_revision']['payload']['occurrences'][1:] == document[
        'parent_revision']['payload']['occurrences'][1:]
    records = realm.prepared[-1]['records']
    assert records['commands'][0]['created'] == [{'ref': 'shot:shot-fork', 'kind': 'shot', 'owner': None}]
    assert records['steps'][0]['patch']
    assert any(effect['ref'] == 'parent' and effect['path'] == [
        'occurrences', 'occurrence_id:occ-0', 'shot_revision_id'] for effect in records['steps'][0]['patch'])
    assert any(binding.get('physical_ref') == 'internal-revision:internal-1'
               for binding in records['commands'][0]['bindings'])
    assert data['id_map'] == {}
    undone = submit(realm, inverse([result['receipt']['receipt_id']]), reads=[])
    assert undone['data']['canonical_document'] == document
    redone = submit(realm, inverse([undone['receipt']['receipt_id']]), reads=[])
    assert redone['data']['canonical_document'] == data['canonical_document']


def test_fork_and_edit_sequential_step_read_and_guarded_fork_step_undo():
    realm = DetachedRealm(fork_document())
    call = fork(realm)
    edit = patch(change(['name'], present('Chapter One'), 'Selected only', ref='shot:shot-fork'))
    result = submit(realm, call, edit, reads=[], steps=[step('fork', 0, call), step('edit', 1, edit, reads=[
        {'kind': 'field', 'ref': 'shot:shot-fork', 'path': ['name'], 'state': present('Chapter One')}])])
    publication = result['receipt']['receipt_id']
    undo = inverse([publication])
    undo['args']['steps'] = {publication: ['fork']}
    with pytest.raises(CommitRejected):
        submit(realm, undo, reads=[])
    undone = submit(realm, inverse([publication]), reads=[])
    assert undone['data']['canonical_document'] == fork_document()


def test_stale_source_and_aba_refused_with_original_head_unchanged():
    realm = DetachedRealm(fork_document())
    call, base = fork(realm), copy.deepcopy(realm.head)
    change_source = patch(change(['unknown'], present([1, 2]), [3], ref='internal:chapter-1'))
    submit(realm, change_source, reads=[])
    head = copy.deepcopy(realm.head)
    with pytest.raises(CommitRejected) as error:
        submit(realm, call, base=base, reads=[])
    assert error.value.failure['details']['status'] == 'conflict'
    assert realm.head == head
    submit(realm, patch(change(['unknown'], present([3]), [1, 2], ref='internal:chapter-1')), reads=[])
    head = copy.deepcopy(realm.head)
    with pytest.raises(CommitRejected):
        submit(realm, call, base=base, reads=[])
    assert realm.head == head


def test_preview_preserves_full_candidate_without_writes():
    realm = DetachedRealm(fork_document())
    head, entries = copy.deepcopy(realm.head), copy.deepcopy(realm.entries)
    call = fork(realm)
    result = submit(realm, call, dry_run=True, reads=[], label='Preview fork',
                    steps=[step('fork', 0, call)], merge='fields')
    assert result['receipt'] is None
    assert result['data']['status'] == 'preview'
    assert result['data']['would_status'] == 'accepted'
    assert result['data']['steps'][0]['patch']
    assert result['data']['canonical_document']['parent_revision']['payload']['occurrences'][0]['shot_id'] == 'shot-fork'
    assert realm.head == head and realm.entries == entries


def test_disjoint_head_merge_and_preview_keep_other_edit():
    realm = DetachedRealm(fork_document())
    call, base = fork(realm), copy.deepcopy(realm.head)
    submit(realm, patch(change(['name'], present('Renamed Film'), 'Other edit')), reads=[])
    preview = submit(realm, call, base=base, dry_run=True, reads=[], merge='fields')
    assert preview['data']['would_status'] == 'merged' and preview['receipt'] is None
    result = submit(realm, call, base=base, reads=[], merge='fields')
    # The historical detached port still spells this rebased; preserve its exact envelope.
    assert result is realm.accepted[-1] and result['data']['status'] == 'rebased'
    assert result['data']['canonical_document']['parent_revision']['payload']['name'] == 'Other edit'
    undone = submit(realm, inverse([result['receipt']['receipt_id']]), reads=[])
    assert undone['data']['canonical_document']['parent_revision']['payload']['name'] == 'Other edit'
