"""Historical item codecs for a root deleted after earlier native writes."""
from __future__ import annotations

import copy
import json
import sqlite3
from types import SimpleNamespace

import pytest

from astrid.sdk.timeline._command_state import CommandError, State
from astrid.sdk.timeline.commit import _digest, create_runtime_commit_service
from astrid.sdk.timeline.inverse import _input_read_state
from tests.editing_conformance.test_captured_nested_redo import captured as retained_capture
from tests.editing_conformance.test_captured_nested_redo import replay
from tests.sdk.test_timeline_commit_provider import DetachedPort
from tests.sdk.test_timeline_commit_provider import runtime_abi as runtime_abi


@pytest.fixture
def captured():
    return retained_capture.__wrapped__()


def test_captured_nested_redo_reaches_production_provider_preview(captured):
    connection = sqlite3.connect(':memory:')
    try:
        connection.executescript('\n'.join(captured['before']['database']))
        documents = [json.loads(row[0])['canonical_document'] for row in connection.execute(
            "SELECT result_json FROM command_idempotency WHERE command_kind='timeline.commit'")]
        realm_id = connection.execute('SELECT id FROM realm').fetchone()[0]
    finally:
        connection.close()
    history = captured['before']['history']
    # Replay the retained realm through the existing in-memory port; no new
    # realm, acceptance, source identity or materialization is manufactured.
    realm = SimpleNamespace(documents={doc['parent_revision']['revision_id']: doc for doc in documents},
                            head=history['head'], entries=history['items'], reads=[], page_size=100)
    request = {**copy.deepcopy(captured['request']['body']), 'dry_run': True}
    invocation = {'operation': 'commitTimeline', 'path': captured['request']['invocation_path'],
                  'body': copy.deepcopy(request)}
    context = {'request': request, 'invocation': invocation,
               'idempotency_key': captured['request']['idempotency_key'],
               'request_hash': _digest(invocation), 'publisher': captured['request']['publisher'],
               'realm_id': realm_id}
    snapshot = copy.deepcopy((captured, realm.documents, realm.entries, realm.head, context))
    result = create_runtime_commit_service().commit(context, DetachedPort(realm, context))
    assert result['receipt'] is None
    data = result['data']
    assert data['status'] == 'preview' and data['would_status'] == 'accepted'
    assert data['canonical_document'] == captured['expected_document']
    assert data['document_digest'] == _digest(captured['expected_document'])
    assert len(data['commands']) == len(request['commands']) == 1
    assert all(data['commands'][0][key] == value for key, value in request['commands'][0].items())
    assert data['id_map'] == {}
    assert data['base_revision_id'] == request['base_revision_id']
    assert data['base_publication_id'] == request['base_publication_id']
    assert data['label'] == request['label'] and data['merge_mode'] == request['merge']
    assert data['steps'][0]['id'] == request['steps'][0]['id']
    assert data['steps'][0]['command_ordinals'] == [0] and data['steps'][0]['patch']
    assert (captured, realm.documents, realm.entries, realm.head, context) == snapshot


def restored_input(captured):
    _, records = replay(captured)
    before, _ = records.load_boundary(records.entries[0])
    state = State(copy.deepcopy(before))
    record = records[0]
    read = next(read for read in record['reads']
                if read['ref'].endswith('/shot') and read['path'] == ['payload'])
    shot = state.occurrence(read['ref'].removesuffix('/shot'))['shot_id']
    # This is the root restored by the native reversal, before compilation.
    del state.bundle['source_mapping']['shots'][shot]
    state.bundle['shots'][shot]['_created'] = True
    return state, read, record, records, shot


def test_sequential_deletion_input_keeps_its_own_codec_without_changing_source(captured):
    state, read, record, records, shot = restored_input(captured)
    deletion = next(write for write in record['writes'] if write['ref'] == 'shot:' + shot)
    # The deletion snapshot follows the earlier clip edit; it is not this
    # command's input. Both belong to the same accepted root and native trace.
    assert deletion['before'] != state.get('shot:' + shot, [])
    snapshot = copy.deepcopy((state.bundle, records.entries, captured))
    view = _input_read_state(state, read, record, records)
    assert view.matches(read['ref'], read['path'], read['state'])
    assert view.bundle['source_mapping']['shots'][shot]['shot_id'] == shot
    assert (state.bundle, records.entries, captured) == snapshot


def test_equal_restored_content_without_accepted_history_cannot_supply_codec(captured):
    state, read, record, records, _ = restored_input(captured)
    snapshot = copy.deepcopy((state.bundle, records.entries, captured))
    view = _input_read_state(state, read, record, list(records))
    assert view is state
    with pytest.raises(CommandError):
        view.get(read['ref'], read['path'])
    assert (state.bundle, records.entries, captured) == snapshot


@pytest.mark.parametrize('damage', [
    'payload', 'internal', 'item-identity', 'owner', 'not-restored',
    'missing-deletion', 'foreign-deletion', 'not-deleted',
])
def test_restored_input_codec_requires_exact_owner_root_and_native_deletion(captured, damage):
    state, read, record, records, shot = restored_input(captured)
    root = state.bundle['shots'][shot]
    deletion = next(write for write in record['writes'] if write['ref'] == 'shot:' + shot)
    if damage == 'payload':
        root['payload']['name'] = 'Changed'
    elif damage == 'internal':
        root['internal_timeline']['clips'][0]['hold'] += 1
    elif damage == 'item-identity':
        root['payload']['items'][0]['item_id'] = 'unrecorded-item'
    elif damage == 'owner':
        state.occurrence(read['ref'].removesuffix('/shot'))['shot_id'] = state.bundle['placements'][1]['shot_id']
    elif damage == 'not-restored':
        root['_created'] = False
    elif damage == 'missing-deletion':
        record['writes'].remove(deletion)
    elif damage == 'foreign-deletion':
        deletion['ref'] = 'shot:foreign-root'
    else:
        deletion['after'] = copy.deepcopy(deletion['before'])
    snapshot = copy.deepcopy((state.bundle, records.entries, captured))
    view = _input_read_state(state, read, record, records)
    assert view is state
    if damage != 'owner':
        with pytest.raises(CommandError):
            view.get(read['ref'], read['path'])
    assert (state.bundle, records.entries, captured) == snapshot
