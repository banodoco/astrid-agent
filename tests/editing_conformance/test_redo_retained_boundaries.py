"""Redo uses accepted ancestor inputs and exact retained physical pins."""
from __future__ import annotations

import copy

import pytest

from astrid.sdk.timeline._clip_scalar_commands import capture_clip_set
from astrid.sdk.timeline._command_state import CommandError, present
from astrid.sdk.timeline._commit_patch import _managed_boundaries, _materialization_sites
from astrid.sdk.timeline._doc_patch import Closure
from astrid.sdk.timeline.commit import bundle_from_document
from tests.editing_conformance.test_doc_patch_removal_guards import document, remove_call, send
from tests.editing_conformance.test_doc_patch_v2 import change, patch
from tests.editing_conformance.test_provider_step_undo_v2 import undo
from tests.sdk.test_timeline_clip_scalar_provider import CLEAR, app_document, current
from tests.sdk.test_timeline_commit_provider import CommitRejected, DetachedRealm, inverse
from tests.sdk.test_timeline_commit_provider import runtime_abi as runtime_abi
from tests.sdk.test_timeline_inverse_fork_guard import land


def ancestor_undo():
    original = document()
    realm = DetachedRealm(original)
    bundle = bundle_from_document(original)
    before = bundle['parent']['config']
    after = copy.deepcopy(before)
    after['opaque']['extra'] = None
    landed = send(realm, patch(change(['config'], present(before), after),
                               remove_call(bundle)['args']['changes'][0]))
    undone = send(realm, undo(landed['receipt']['receipt_id']))
    assert undone['data']['canonical_document'] == original
    return realm, landed, undone


@pytest.mark.parametrize('damage', ['config', 'descendant', 'source', 'native-range', 'effect-range'])
def test_ancestor_redo_rejects_damaged_accepted_partition(damage):
    realm, _, undone = ancestor_undo()
    record = realm.entries[-1]['records']['commands'][0]
    stage = next(binding for binding in record['bindings'] if binding['key'] == 'inverse.stage')
    if damage == 'config':
        record['writes'][-1]['before']['value']['opaque']['extra'] = False
    elif damage == 'descendant':
        record['writes'][0]['after']['value'].reverse()
    elif damage == 'source':
        stage['boundary']['publication_id'] = 'bootstrap'
    elif damage == 'native-range':
        stage['boundary']['native_write_range'][0] = 1
    else:
        stage['boundary']['effect_range'][1] -= 1
    snapshot = copy.deepcopy((realm.head, realm.entries, realm.documents, realm.accepted, realm.prepared))
    for dry_run in (True, False):
        with pytest.raises(CommitRejected) as error:
            send(realm, undo(undone['receipt']['receipt_id']), dry_run=dry_run)
        assert error.value.failure['details']['reason'] == 'history_unavailable'
        assert (realm.head, realm.entries, realm.documents, realm.accepted, realm.prepared) == snapshot


@pytest.mark.parametrize('damage', ['missing-pin', 'owners', 'pin', 'source', 'native-range',
                                   'effect-range', 'stage-owner', 'parent-edge'])
def test_retained_fork_redo_rejects_damaged_accepted_partition(damage):
    realm = DetachedRealm(app_document())
    first = land(realm, capture_clip_set(current(realm), CLEAR, clear=['asset']))
    undone = land(realm, inverse([first['receipt']['receipt_id']]))
    entry = realm.entries[-1]
    record = entry['records']['commands'][0]
    stage = next(binding for binding in record['bindings'] if binding['key'] == 'inverse.stage')
    pin = next(binding for binding in record['bindings']
               if binding['key'] == 'commit.effect' and binding['ref'].startswith('internal-revision:'))
    if damage == 'missing-pin':
        record['bindings'].remove(pin)
    elif damage == 'owners':
        pin['owners'] = ['occurrence:shared-sibling/shot']
    elif damage == 'pin':
        pin['revision_id'] = 'forged'
    elif damage == 'source':
        stage['boundary']['publication_id'] = 'bootstrap'
    elif damage == 'native-range':
        stage['boundary']['native_write_range'][0] = 1
    elif damage == 'effect-range':
        stage['boundary']['effect_range'][1] -= 1
    elif damage == 'stage-owner':
        stage['ref'] = 'occurrence:shared-sibling'
    else:
        entry['records']['steps'][0]['patch'][0]['value'] = 'forged'
    snapshot = copy.deepcopy((realm.head, realm.entries, realm.documents, realm.accepted, realm.prepared))
    for dry_run in (True, False):
        with pytest.raises(CommitRejected) as error:
            land(realm, inverse([undone['receipt']['receipt_id']]), dry_run=dry_run)
        assert error.value.failure['details']['reason'] == 'history_unavailable'
        assert (realm.head, realm.entries, realm.documents, realm.accepted, realm.prepared) == snapshot


@pytest.mark.parametrize('damage', ['revision_id', 'content_digest', 'payload', 'missing'])
def test_retained_destination_requires_the_same_complete_physical_pin(damage):
    realm = DetachedRealm(app_document())
    first = land(realm, capture_clip_set(current(realm), CLEAR, clear=['asset']))
    land(realm, inverse([first['receipt']['receipt_id']]))
    entry = realm.entries[-1]
    before = Closure(bundle_from_document(realm.documents[entry['previous']['revision_id']]))
    after = Closure(bundle_from_document(realm.documents[entry['revision_id']]))
    boundaries = list(_managed_boundaries(entry, before, after))
    assert len(boundaries) == 1
    old_images, old_rows, new_images, new_rows, records, step = copy.deepcopy(boundaries[0])
    shot = 'shot-revision:' + new_rows[0]['shot_revision_id']
    retained = 'internal-revision:' + new_images[shot]['internal_timeline_revision_id']
    assert old_images[retained] == new_images[retained]
    if damage == 'missing':
        old_images.pop(retained)
    elif damage == 'payload':
        old_images[retained]['payload']['opaque'] = None
    else:
        old_images[retained][damage] = 'forged'
    snapshot = copy.deepcopy(realm.entries)
    with pytest.raises(CommandError) as error:
        _materialization_sites(entry, old_images, old_rows, new_images, new_rows, records, step,
                               [], set(), [], include_inverse_stages=True)
    assert error.value.kind == 'history_unavailable'
    assert realm.entries == snapshot


@pytest.mark.parametrize('damage', ['source-owner', 'source-publication'])
def test_unchanged_retained_destination_requires_accepted_source_custody(damage):
    realm = DetachedRealm(app_document())
    first = land(realm, capture_clip_set(current(realm), CLEAR, clear=['asset']))
    land(realm, inverse([first['receipt']['receipt_id']]))
    entry = realm.entries[-1]
    before = Closure(bundle_from_document(realm.documents[entry['previous']['revision_id']]))
    after = Closure(bundle_from_document(realm.documents[entry['revision_id']]))
    old_images, old_rows, new_images, new_rows, records, step = next(
        _managed_boundaries(entry, before, after))
    if damage == 'source-owner':
        new_rows[:] = [row for row in new_rows if row['occurrence_id'] != 'shared-sibling']
    else:
        step['_inverse_stage']['boundary']['publication_id'] = 'forged-source'
    with pytest.raises(CommandError) as error:
        _materialization_sites(entry, old_images, old_rows, new_images, new_rows, records, step,
                               [], set(), [], include_inverse_stages=True)
    assert error.value.kind == 'history_unavailable'


@pytest.mark.parametrize('kind', ['ancestor', 'retained-fork'])
@pytest.mark.parametrize('selected', [False, True], ids=['whole', 'step'])
def test_repeated_redo_preserves_exact_documents_and_preview(kind, selected):
    if kind == 'ancestor':
        realm, landed, undone = ancestor_undo()
    else:
        realm = DetachedRealm(app_document())
        landed = land(realm, capture_clip_set(current(realm), CLEAR, clear=['asset']))
        undone = land(realm, inverse([landed['receipt']['receipt_id']]))
    result = undone
    for expected in (landed, undone, landed, undone):
        command = undo(result['receipt']['receipt_id'], 'edit') if selected else undo(
            result['receipt']['receipt_id'])
        snapshot = copy.deepcopy((realm.head, realm.entries, realm.documents, realm.accepted, realm.prepared))
        preview = send(realm, command, dry_run=True)
        assert preview['receipt'] is None and preview['data']['would_status'] == 'accepted'
        assert (realm.head, realm.entries, realm.documents, realm.accepted, realm.prepared) == snapshot
        result = send(realm, command)
        assert result['receipt']['result'] == result['data']
        assert result['data']['canonical_document'] == preview['data']['canonical_document']
        assert result['data']['canonical_document'] == expected['data']['canonical_document']
