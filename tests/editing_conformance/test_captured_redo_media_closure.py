"""The retained 1338 redo compiles through the ordinary request port, offline."""
from __future__ import annotations

import copy
import hashlib
import json
import sqlite3
import threading
from pathlib import Path
from types import SimpleNamespace

import pytest

from astrid.core.timeline.authoring_bundle import AuthoringBundleError
from astrid.sdk.timeline._command_state import CommandError
from astrid.sdk.timeline._commit_patch import inverse_closure
from astrid.sdk.timeline.commit import create_runtime_commit_service, document_from_bundle
from tests.helpers.runtime import use_runtime


@pytest.fixture
def captured():
    path = Path(__file__).parents[1] / 'fixtures' / 'timeline_redo_media_closure_1338.json'
    assert hashlib.sha256(path.read_bytes()).hexdigest() == (
        'a20636115ffb6e986ad7731d57bc90dd7ed98a2330b65d3dc71b83a3707ddb37')
    return json.loads(path.read_text())


@pytest.mark.integration
def test_exact_captured_redo_reaches_prepared_with_original_ids_without_writes(captured, tmp_path):
    use_runtime()
    from runtime_protocol.cas import ContentAddressedStore
    from runtime_protocol.timeline_commit_port import RequestCommitPort
    from runtime_protocol.timeline_commit_store import TimelineCommitStore
    from runtime_protocol.timeline_commit_validation import validate_commit_value
    from runtime_protocol.util import canonical_json

    before, request = captured['before'], captured['request']
    database_path = tmp_path / 'realm.sqlite3'
    connection = sqlite3.connect(database_path, isolation_level=None)
    connection.row_factory = sqlite3.Row
    connection.executescript('\n'.join(before['database']))
    store = SimpleNamespace(
        conn=connection, db_path=database_path, _mutex=threading.RLock(), cas_root=tmp_path / 'cas')
    # Exact bytes from the original seed_shared_occurrence_parent, verified
    # against the retained pins; no new source or accepted history is created.
    pins = {pin['media_id'] for pin in before['document']['dependency_manifest']['media']}
    for index in range(2):
        data = f'connected-occurrence-source-{index}'.encode()
        assert 'sha256:' + hashlib.sha256(data).hexdigest() in pins
        ContentAddressedStore(store.cas_root).put(data)
    commits = TimelineCommitStore(store, validate_prepared=lambda p: validate_commit_value(p, 'PreparedCommit'))
    body = request['body']
    context = {
        'request': body,
        'invocation': {'operation': 'commitTimeline', 'path': request['invocation_path'], 'body': body},
        'idempotency_key': request['idempotency_key'], 'publisher': request['publisher'],
        'realm_id': connection.execute('SELECT id FROM realm').fetchone()[0],
        'request_hash': hashlib.sha256(canonical_json({
            'operation': 'commitTimeline', 'path': request['invocation_path'],
            'publisher': request['publisher'], 'request': body}).encode()).hexdigest(),
    }
    port = RequestCommitPort(SimpleNamespace(store=store), commits, context, lambda: None)
    prepared = []

    class PreparedReached(Exception):
        pass

    def observe(value):
        validate_commit_value(value, 'PreparedCommit')
        prepared.append(copy.deepcopy(value))
        raise PreparedReached

    port.accept = observe
    try:
        with pytest.raises(PreparedReached):
            create_runtime_commit_service().commit(context, port)
        assert len(prepared) == 1
        assert prepared[0]['canonical_document'] == captured['expected_document']
        assert prepared[0]['records']['undo_of'] == body['commands'][0]['args']['publications']
        assert prepared[0]['expected_head'] == before['history']['head']
        assert prepared[0]['records']['steps'][0]['patch']
        assert list(connection.iterdump()) == before['database']
    finally:
        port.close()
        connection.close()


def test_restored_baseline_is_native_only_and_preserves_all_authored_bytes(captured):
    stage = captured['failing_stage']
    bundle = stage['bundle']
    original = copy.deepcopy(bundle)
    sources = copy.deepcopy(stage['sources'])
    closure = inverse_closure(bundle, stage['sources'])
    assert type(bundle['shots']['selected-shot']).__name__ == '_RestoredShot'
    assert bundle == original
    assert closure.document == captured['expected_document']
    assert stage['sources'] == sources
    with pytest.raises(AuthoringBundleError, match='competing selected media'):
        document_from_bundle(json.loads(json.dumps(bundle)), source_bundles=stage['sources'])


@pytest.mark.parametrize('mutation', [
    'missing-owner', 'extra-owner', 'payload', 'internal', 'source-digest', 'source-scope',
])
def test_captured_restored_baseline_refuses_mismatched_history_without_mutation(captured, mutation):
    stage = captured['failing_stage']
    bundle, sources = stage['bundle'], stage['sources']
    if mutation == 'missing-owner':
        bundle['placements'][0]['occurrence_id'] = 'foreign-owner'
    elif mutation == 'extra-owner':
        bundle['placements'][1]['shot_id'] = 'selected-shot'
    elif mutation == 'payload':
        bundle['shots']['selected-shot']['payload']['opaque_shot']['future_schema'] = 10
    elif mutation == 'internal':
        bundle['shots']['selected-shot']['internal_timeline']['opaque_timeline']['future_schema'] = 10
    elif mutation == 'source-digest':
        sources[0]['source_mapping']['_runtime_document']['shot_revisions'][0]['content_digest'] = 'sha256:' + '0' * 64
    else:
        sources[0]['source_mapping']['_runtime_document']['project_id'] = 'foreign-project'
    before = copy.deepcopy((bundle, sources))
    with pytest.raises((AuthoringBundleError, CommandError, ValueError)):
        inverse_closure(bundle, sources)
    assert (bundle, sources) == before


def test_restored_selection_guard_remains_active_after_baseline_recovery(captured):
    stage = captured['failing_stage']
    bundle = stage['bundle']
    inverse_closure(bundle, stage['sources'])
    shot = bundle['shots']['selected-shot']
    shot['payload']['items'][0]['media_id'] = 'sha256:' + '9' * 64
    before = copy.deepcopy(bundle)
    with pytest.raises(AuthoringBundleError, match='competing selected media'):
        document_from_bundle(bundle, source_bundles=stage['sources'])
    assert bundle == before
