"""Undo transport failures retain intent without implying Runtime acceptance."""
from __future__ import annotations

import json
from typing import Any
from urllib.error import URLError

import pytest

from astrid.sdk.timeline._common import TimelineEditError
from astrid.sdk.workspace_client import GeneratedWorkspaceClient, WorkspaceClientError
from tests.sdk import test_timeline_sync
from tests.sdk.test_timeline_sync import open_session
from tests.sdk.test_timeline_sync_native import label

connected = test_timeline_sync.connected


@pytest.mark.parametrize("accepted_before_failure", [False, True], ids=["before-dispatch", "after-acceptance"])
@pytest.mark.parametrize("failure,code", [
    (TimeoutError("read timed out"), "timeline_outcome_unknown"),
    (URLError(TimeoutError("read timed out")), "timeline_outcome_unknown"),
    (ConnectionError("response connection closed"), "timeline_outcome_unknown"),
], ids=["timeout", "wrapped-timeout", "connection-loss"])
def test_undo_transport_failure_retains_exact_flight(
    connected: Any, accepted_before_failure: bool, failure: Exception, code: str,
) -> None:
    client, engine, path = connected
    session = open_session(connected)
    original = session.commit(label("Original"), idempotency_key="original")
    publication = original["receipt"]["receipt_id"]
    wire: list[tuple[bytes, str]] = []

    def transport(method: str, route: str, headers: dict[str, str], body: bytes) -> tuple:
        assert method == "POST" and route == session.path
        assert headers["Authorization"] == "Bearer offline-control"
        key = headers["Idempotency-Key"]
        wire.append((body, key))
        request = json.loads(body)
        result = None
        if accepted_before_failure or len(wire) > 1:
            result = engine.commit_timeline("p", "t", request, idempotency_key=key)
        if len(wire) == 1:
            raise failure
        return 200, {}, json.dumps(result).encode()

    generated = GeneratedWorkspaceClient(
        "http://127.0.0.1:1", "offline-control", transport=transport,
    )
    client._remote._transport._generated = generated
    with pytest.raises(WorkspaceClientError) as caught:
        session.undo([publication], idempotency_key="undo")
    assert (caught.value.status, caught.value.code) == (0, code)
    assert generated.timeout == 30.0
    assert len(wire) == 1
    assert ("undo" in engine.accepted) is accepted_before_failure
    accepted_count = len(engine.accepted)
    persisted = path.read_bytes()
    state = json.loads(persisted)
    assert state["base"] == {"revision_id": "r1", "publication_id": publication}
    assert state["flight"]["key"] == wire[0][1] == "undo"
    assert state["flight"]["body"].encode() == wire[0][0]
    request = json.loads(wire[0][0])
    assert request["commands"] == [{
        "schema_version": 1, "op": "timeline.inverse", "version": "1",
        "select": None, "args": {"publications": [publication]},
    }]
    assert "conflict" not in state

    reopened = open_session(connected)
    with pytest.raises(TimelineEditError, match="immutable"):
        reopened.undo([publication], idempotency_key="changed")
    assert path.read_bytes() == persisted and len(wire) == 1
    retried = reopened.flush(idempotency_key="undo")
    assert wire == [wire[0], wire[0]]
    assert len(engine.accepted) == accepted_count + (not accepted_before_failure)
    assert retried["receipt"] == engine.accepted["undo"][1]["receipt"]
    assert retried["data"]["accepted_publication_id"] == retried["receipt"]["receipt_id"]
    assert json.loads(path.read_bytes())["flight"] is None
    assert json.loads(path.read_bytes())["pending"] == []
