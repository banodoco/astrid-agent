"""A retained shared source does not hide consumers of its authored fork."""
from __future__ import annotations

import copy

import pytest

from astrid.sdk.timeline._clip_scalar_commands import capture_clip_set
from astrid.sdk.timeline._command_state import ABSENT, present
from tests.editing_conformance.test_doc_patch_v2 import change, patch, step, submit
from tests.sdk.test_timeline_clip_scalar_provider import CLEAR, app_document, current
from tests.sdk.test_timeline_commit_provider import CommitRejected, DetachedRealm, inverse
from tests.sdk.test_timeline_commit_provider import runtime_abi as runtime_abi


def land(realm, command, *, dry_run=False):
    return submit(realm, command, label='Guard fork output', merge='fields', reads=[],
                  steps=[step('edit', 0, command, reads=[])], dry_run=dry_run)


@pytest.mark.parametrize('selected', [False, True], ids=['whole', 'step'])
@pytest.mark.parametrize('consumer', [False, True], ids=['equal-clear', 'declared-read'])
def test_fork_output_guard_survives_reachable_shared_source(selected, consumer):
    realm = DetachedRealm(app_document())
    first = land(realm, capture_clip_set(current(realm), CLEAR, clear=['asset']))
    source = copy.deepcopy(realm.entries[-1])
    materialization = next(binding for binding in source['records']['commands'][0]['bindings']
                           if binding['key'] == 'commit.materialization'
                           and binding['physical_ref'].startswith('internal-revision:'))
    document = first['data']['canonical_document']
    roots = {'internal-revision:' + row['revision_id']
             for row in document['internal_timeline_revisions']}
    assert {materialization['physical_ref'], materialization['successor']} <= roots
    assert first['receipt']['result'] == first['data']
    if consumer:
        command = patch(change(['meta'], ABSENT, {'guard': {'note': 'kept'}}))
        submit(realm, command, label='Consume fork', merge='fields', steps=[step('note', 0, command)],
               reads=[{'kind': 'field', 'ref': materialization['successor'],
                       'path': ['clips', 'id:f2-agent-clear-clip', 'asset'], 'state': ABSENT}])
    else:
        last = land(realm, capture_clip_set(current(realm), CLEAR, clear=['asset']))
        record = realm.entries[-1]['records']['commands'][0]
        assert record['writes'] == [{'ref': CLEAR, 'path': ['asset'],
                                     'before': ABSENT, 'after': ABSENT}]
        assert realm.entries[-1]['records']['steps'][0]['patch'] == []
        assert first['receipt']['receipt_id'] != last['receipt']['receipt_id']
    command = inverse([first['receipt']['receipt_id']])
    if selected:
        command['args']['steps'] = {first['receipt']['receipt_id']: ['edit']}
    before = copy.deepcopy((realm.head, realm.entries, realm.documents, realm.accepted, realm.prepared))
    for dry_run in (True, False):
        with pytest.raises(CommitRejected) as error:
            land(realm, command, dry_run=dry_run)
        details = error.value.failure['details']
        assert details['reason'] == ('inverse_conflict' if consumer else 'intervening_write')
        assert details['code'] == details['reason']
        assert details['hint'] and details['suggested_command']
        assert isinstance(details['path'], list)
        assert before == (realm.head, realm.entries, realm.documents, realm.accepted, realm.prepared)
    assert realm.entries[1] == source


def test_fork_output_guard_allows_independent_parent_edit_and_preview_redo():
    realm = DetachedRealm(app_document())
    first = land(realm, capture_clip_set(current(realm), CLEAR, clear=['asset']))
    note = patch(change(['meta'], ABSENT, {'guard': {'note': 'kept'}}))
    land(realm, note)
    command = inverse([first['receipt']['receipt_id']])
    before = copy.deepcopy((realm.head, realm.entries, realm.documents, realm.accepted, realm.prepared))
    preview = land(realm, command, dry_run=True)
    assert preview['receipt'] is None
    assert preview['data']['would_status'] == 'accepted'
    assert before == (realm.head, realm.entries, realm.documents, realm.accepted, realm.prepared)
    undone = land(realm, command)
    assert undone['data']['canonical_document'] == preview['data']['canonical_document']
    assert undone['receipt']['result'] == undone['data']
    assert undone['data']['canonical_document']['parent_revision']['payload']['meta'] == {'guard': {'note': 'kept'}}
    redone = land(realm, inverse([undone['receipt']['receipt_id']]))
    assert redone['receipt']['result'] == redone['data']
    assert redone['data']['canonical_document']['internal_timeline_revisions'] == (
        first['data']['canonical_document']['internal_timeline_revisions'])


@pytest.mark.parametrize('selected', [False, True], ids=['whole', 'step'])
def test_missing_fork_binding_fails_closed_before_equal_clear_inverse(selected):
    realm = DetachedRealm(app_document())
    first = land(realm, capture_clip_set(current(realm), CLEAR, clear=['asset']))
    bindings = realm.entries[-1]['records']['commands'][0]['bindings']
    internal = next(binding for binding in bindings
                    if binding['key'] == 'commit.materialization'
                    and binding['physical_ref'].startswith('internal-revision:'))
    bindings.remove(internal)
    assert any(binding['key'] == 'commit.materialization' for binding in bindings)
    land(realm, capture_clip_set(current(realm), CLEAR, clear=['asset']))
    assert realm.entries[-1]['records']['commands'][0]['writes'] == [
        {'ref': CLEAR, 'path': ['asset'], 'before': ABSENT, 'after': ABSENT}]
    command = inverse([first['receipt']['receipt_id']])
    if selected:
        command['args']['steps'] = {first['receipt']['receipt_id']: ['edit']}
    before = copy.deepcopy((realm.head, realm.entries, realm.documents, realm.accepted, realm.prepared))
    for dry_run in (True, False):
        with pytest.raises(CommitRejected) as error:
            land(realm, command, dry_run=dry_run)
        assert error.value.failure['details']['reason'] == 'history_unavailable'
        assert 'missing authenticated fork materialization' in error.value.failure['message']
        assert before == (realm.head, realm.entries, realm.documents, realm.accepted, realm.prepared)


@pytest.mark.parametrize('damage', [
    'missing-all', 'missing-shot', 'duplicate', 'ambiguous', 'owner', 'owners',
    'ref', 'physical_ref', 'revision_id', 'content_digest', 'successor',
    'edge_before', 'edge_after', 'step_id', 'boundary', 'policy', 'dependencies',
])
def test_fork_materializations_require_complete_unique_authenticated_edges(damage):
    realm = DetachedRealm(app_document())
    first = land(realm, capture_clip_set(current(realm), CLEAR, clear=['asset']))
    record = realm.entries[-1]['records']['commands'][0]
    bindings = record['bindings']
    internal = next(binding for binding in bindings
                    if binding['key'] == 'commit.materialization'
                    and binding['physical_ref'].startswith('internal-revision:'))
    if damage == 'missing-all':
        bindings[:] = [binding for binding in bindings if binding['key'] != 'commit.materialization']
    elif damage == 'missing-shot':
        bindings[:] = [binding for binding in bindings if binding['key'] != 'commit.materialization'
                       or not binding['physical_ref'].startswith('shot-revision:')]
    elif damage in ('duplicate', 'ambiguous'):
        duplicate = copy.deepcopy(internal)
        if damage == 'ambiguous':
            duplicate['successor'] = internal['physical_ref']
        bindings.append(duplicate)
    elif damage == 'owners':
        internal['owners'] = [internal['owner']]
    elif damage == 'owner':
        internal['owner'] = 'occurrence:shared-sibling/shot'
    elif damage == 'boundary':
        internal['boundary']['command_ordinal'] = 1
    elif damage == 'dependencies':
        internal['dependencies'] = ['forged']
    else:
        internal[damage] = 'forged'
    damaged = copy.deepcopy(realm.entries[-1])
    before = copy.deepcopy((realm.head, realm.entries, realm.documents, realm.accepted, realm.prepared))
    for selected in (False, True):
        command = inverse([first['receipt']['receipt_id']])
        if selected:
            command['args']['steps'] = {first['receipt']['receipt_id']: ['edit']}
        for dry_run in (True, False):
            with pytest.raises(CommitRejected) as error:
                land(realm, command, dry_run=dry_run)
            assert error.value.failure['details']['reason'] == 'history_unavailable'
            assert before == (realm.head, realm.entries, realm.documents, realm.accepted, realm.prepared)
    assert realm.entries[-1] == damaged


def test_equal_payload_sibling_cannot_supply_fork_source_authority():
    from astrid.sdk.timeline.commit import _digest, _manifest

    document = app_document()
    internal = copy.deepcopy(document['internal_timeline_revisions'][0])
    internal['revision_id'] = 'equal-internal'
    shot = copy.deepcopy(document['shot_revisions'][0])
    shot.update(shot_id='equal-shot', revision_id='equal-shot-revision',
                internal_timeline_revision_id=internal['revision_id'])
    shot['payload']['internal_timeline_revision_id'] = internal['revision_id']
    for item in shot['payload'].get('items', []):
        item['item_id'] = 'equal-' + item['item_id']
    shot['content_digest'] = _digest(shot['payload'])
    occurrence = copy.deepcopy(document['parent_revision']['payload']['occurrences'][-1])
    occurrence.update(occurrence_id='equal-sibling', shot_id=shot['shot_id'], shot_revision_id=shot['revision_id'])
    document['parent_revision']['payload']['occurrences'].append(occurrence)
    document['parent_revision']['content_digest'] = _digest(document['parent_revision']['payload'])
    document['internal_timeline_revisions'].append(internal)
    document['shot_revisions'].append(shot)
    document['internal_timeline_revisions'].sort(key=lambda row: (row['timeline_id'], row['revision_id']))
    document['shot_revisions'].sort(key=lambda row: (row['shot_id'], row['revision_id']))
    manifest = document['dependency_manifest']
    document['dependency_manifest'] = _manifest(document['shot_revisions'], document['internal_timeline_revisions'],
                                                manifest['media'], manifest['documents'])
    realm = DetachedRealm(document)
    first = land(realm, capture_clip_set(current(realm), CLEAR, clear=['asset']))
    binding = next(binding for binding in realm.entries[-1]['records']['commands'][0]['bindings']
                   if binding['key'] == 'commit.materialization'
                   and binding['physical_ref'].startswith('internal-revision:'))
    original = next(row for row in document['internal_timeline_revisions']
                    if row['revision_id'] == binding['revision_id'])
    assert original['payload'] == internal['payload']
    assert original['content_digest'] == internal['content_digest']
    binding.update(ref='internal-revision:equal-internal', physical_ref='internal-revision:equal-internal',
                   revision_id=internal['revision_id'], content_digest=internal['content_digest'],
                   owners=['occurrence:equal-sibling/shot'], edge_before='shot-revision:equal-shot-revision')
    before = copy.deepcopy((realm.head, realm.entries, realm.documents, realm.accepted, realm.prepared))
    with pytest.raises(CommitRejected) as error:
        land(realm, inverse([first['receipt']['receipt_id']]))
    assert error.value.failure['details']['reason'] == 'history_unavailable', error.value.failure
    assert before == (realm.head, realm.entries, realm.documents, realm.accepted, realm.prepared)


@pytest.mark.parametrize('later', [
    'equal-payload', 'aba', 'disjoint-field', 'retained-sibling', 'retained-source-read',
])
def test_valid_fork_history_distinguishes_output_consumers_from_retained_sibling(later):
    realm = DetachedRealm(app_document())
    first = land(realm, capture_clip_set(current(realm), CLEAR, clear=['asset']))
    source = copy.deepcopy(realm.entries[-1])
    if later == 'aba':
        land(realm, capture_clip_set(current(realm), CLEAR, values={'hold': 4}))
        land(realm, capture_clip_set(current(realm), CLEAR, values={'hold': 3}))
    elif later in ('retained-sibling', 'retained-source-read'):
        sibling = CLEAR.replace('f2-occurrence', 'shared-sibling')
        if later == 'retained-source-read':
            # The original step's physical asset read still guards its source.
            command = patch(change(['clips', 'id:f2-agent-clear-clip', 'asset'],
                                   present('f2-clear'), 'f2-clear', ref='internal-revision:internal-1'))
        else:
            command = capture_clip_set(current(realm), sibling, values={'hold': 3})
        land(realm, command)
        if later == 'retained-sibling':
            assert realm.entries[-1]['records']['steps'][0]['patch'] == []
    elif later == 'equal-payload':
        land(realm, capture_clip_set(current(realm), CLEAR, values={'hold': 3}))
        assert realm.entries[-1]['records']['steps'][0]['patch'] == []
    else:
        land(realm, capture_clip_set(current(realm), CLEAR, values={'hold': 4}))
    command = inverse([first['receipt']['receipt_id']])
    if later == 'retained-sibling':
        undone = land(realm, command)
        from tests.editing_conformance.test_recipe_clip_scalar_fields import clip

        assert clip(current(realm), CLEAR)['asset'] == 'f2-clear'
        assert clip(current(realm), sibling)['hold'] == 3
        redone = land(realm, inverse([undone['receipt']['receipt_id']]))
        assert 'asset' not in clip(current(realm), CLEAR)
        assert clip(current(realm), sibling)['hold'] == 3
        assert redone['receipt']['result'] == redone['data']
    else:
        before = copy.deepcopy((realm.head, realm.entries, realm.documents, realm.accepted, realm.prepared))
        for dry_run in (True, False):
            with pytest.raises(CommitRejected) as error:
                land(realm, command, dry_run=dry_run)
            assert error.value.failure['details']['reason'] == 'intervening_write'
            assert before == (realm.head, realm.entries, realm.documents, realm.accepted, realm.prepared)
    assert realm.entries[1] == source
