"""Real Runtime mixed editing and original scalar claims across lost ACK/restart.

Run centrally with commit-integration --files tests/editing_conformance/test_commit_runtime_scalar.py.
"""
from __future__ import annotations

import copy
import json

import pytest

from astrid.sdk.timeline._command_state import State
from astrid.sdk.timeline._scalar_commands import capture_clip_set_params, capture_track_set
from astrid.sdk.timeline.commit import bundle_from_document
from tests.editing_conformance import test_commit_runtime_m1 as m1

film, renderer_off, wire = m1.film, m1.renderer_off, m1.wire

pytestmark = pytest.mark.integration


def test_move_then_scalar_outbox_handoff_reload_and_guarded_inverse(
    tmp_path, monkeypatch, film, wire, real_runtime,
):
    from astrid.sdk.timeline.sync import TimelineSession
    from astrid.sdk.workspace_client import WorkspaceClientError

    fixture, _, _ = film
    monkeypatch.setenv('ASTRID_RUNTIME_DATA_ROOT', str(tmp_path / 'client-data'))
    observed, lose = wire
    support = tmp_path / 'support'
    daemon = real_runtime(fixture.duplicate_root, support_root=support, authoring_profile='astrid')
    client = m1.open_client(daemon)
    outbox = tmp_path / 'scalar-outbox.json'
    session = TimelineSession.open(fixture.project_id, fixture.timeline_id,
                                   client=client, outbox_path=outbox)
    session.queue_move('parent/clip:music-bed', 1)
    lose.add('scalar-move-flight')
    with pytest.raises(WorkspaceClientError):
        session.flush(idempotency_key='scalar-move-flight')
    moved = copy.deepcopy(observed[-1]['response'])
    flight = json.loads(outbox.read_bytes())['flight']
    # Use the existing capture callback seam; no scalar convenience method or
    # second queue is added to sync.py by this bounded command task.
    label = session._queue(lambda state: capture_track_set(
        session._bundle(state), 'parent/track:music', values={'label': 'Music'}))
    sealed = json.dumps(label, sort_keys=True)
    port = daemon.httpd.server_port
    daemon.stop()
    daemon = real_runtime(fixture.duplicate_root, support_root=support,
                          port=port, authoring_profile='astrid')
    client = m1.open_client(daemon)
    session = TimelineSession.open(fixture.project_id, fixture.timeline_id,
                                   client=client, outbox_path=outbox)
    assert json.loads(outbox.read_bytes())['flight'] == flight
    count = len(m1.history(client, fixture)['items'])
    retry = session.flush()
    assert retry['receipt'] == moved['receipt']
    assert len(m1.history(client, fixture)['items']) == count
    assert json.loads(outbox.read_bytes())['pending'] == [label]
    accepted = session.flush()
    assert json.loads(observed[-1]['body'])['commands'] == [label]
    assert json.dumps(accepted['data']['commands'][0], sort_keys=True) == sealed
    m1.assert_readback(client, fixture, accepted)
    bundle = bundle_from_document(accepted['data']['canonical_document'])
    assert State(bundle).entity('parent/clip:music-bed')['at'] == 1
    assert State(bundle).entity('parent/track:music')['label'] == 'Music'
    # A new session reloads accepted state before undoing the scalar edit.
    reloaded = TimelineSession.open(fixture.project_id, fixture.timeline_id,
                                    client=client, outbox_path=tmp_path / 'reload-outbox.json')
    undone = reloaded.undo([accepted['receipt']['receipt_id']])
    assert undone['data']['canonical_document'] == moved['data']['canonical_document']
    m1.assert_readback(client, fixture, undone)
    redone = reloaded.undo([undone['receipt']['receipt_id']])
    assert redone['data']['canonical_document'] == accepted['data']['canonical_document']

    # Occurrence params publish through the same command door without touching
    # parent params or another occurrence. Unknown JSON and null remain literal.
    ref = 'occurrence:occurrence-1/clip:clip-1'
    reloaded._queue(lambda state: capture_clip_set_params(
        reloaded._bundle(state), ref, values={'null': None, 'opaque': {'keep': [None, {}]}}))
    params = reloaded.flush()
    m1.assert_readback(client, fixture, params)
    changed = State(bundle_from_document(params['data']['canonical_document']))
    assert changed.entity(ref)['params'] == {'null': None, 'opaque': {'keep': [None, {}]}}
    assert 'params' not in changed.entity('parent/clip:music-bed')
    assert 'params' not in changed.entity('occurrence:occurrence-2/clip:clip-2')
    reloaded._queue(lambda state: capture_clip_set_params(reloaded._bundle(state), ref, clear=['null']))
    cleared = reloaded.flush()
    assert 'null' not in State(bundle_from_document(cleared['data']['canonical_document'])).entity(ref)['params']
    restored = reloaded.undo([cleared['receipt']['receipt_id']])
    assert restored['data']['canonical_document'] == params['data']['canonical_document']
