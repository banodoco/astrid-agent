"""A refused function rerun must not release a replacement outbox flight."""
from __future__ import annotations

import copy
from contextlib import contextmanager

from astrid.sdk.timeline.outbox import Outbox
from astrid.sdk.timeline.sync import TimelineSession


def test_save_interrupted_preserves_flight_replaced_before_lock(tmp_path, monkeypatch):
    identity = {"realm_id": "realm", "actor_id": "actor"}
    scope = {**identity, "project_id": "project", "timeline_id": "film"}
    observed_flight = {
        "body": "observed request",
        "count": 1,
        "key": "observed-key",
        "evaluation": {"kind": "function", "count": 1},
    }
    replacement_pending = [{"op": "timeline.move", "args": {"on": 20}}]
    replacement_flight = {
        "body": "replacement request",
        "count": 1,
        "key": "replacement-key",
        "evaluation": {"kind": "function", "count": 2},
    }
    initial = {
        "scope": scope,
        "base": {"revision_id": "r0", "publication_id": "pub0"},
        "pending": [{"op": "timeline.move", "args": {"on": 10}}],
        "flight": observed_flight,
        "conflict": {"details": {"status": "conflict"}},
        "evaluation": {"kind": "function", "count": 1},
    }
    outbox = Outbox(tmp_path / "outbox.json")
    outbox.write(initial)
    session = TimelineSession(object(), outbox, identity, "project", "film")
    monkeypatch.setattr("astrid.sdk.timeline.sync._connection", lambda _client: identity)
    monkeypatch.setattr("astrid.sdk.timeline.sync._pending_diff", lambda _session, _record: None)
    monkeypatch.setattr("astrid.sdk.timeline.sync._pending_clash", lambda _session, record: record)

    original_locked = outbox.locked

    @contextmanager
    def replace_then_lock():
        # Model another writer replacing the conflicted flight after this
        # evaluation observed it, but before save_interrupted acquires the lock.
        state = outbox.read()
        state["pending"] = copy.deepcopy(replacement_pending)
        state["flight"] = copy.deepcopy(replacement_flight)
        state["base"] = {"revision_id": "r2", "publication_id": "pub2"}
        outbox.write(state)
        with original_locked():
            yield

    monkeypatch.setattr(outbox, "locked", replace_then_lock)

    refused_commands = [{"op": "timeline.move", "args": {"on": 5}}]
    refused_evaluation = {"kind": "function", "count": 2}
    error = session.save_interrupted(
        refused_commands,
        base={"revision_id": "r0", "publication_id": "pub0"},
        label="Refused rerun",
        steps=[],
        merge="atomic",
        reads=None,
        key="refused-key",
        evaluation=refused_evaluation,
        failure={"code": "intervening_write", "details": {"status": "conflict"}},
        release_flight=observed_flight,
    )

    state = Outbox(outbox.path).read()
    assert state["pending"] == replacement_pending
    assert state["flight"] == replacement_flight
    assert state["base"] == {"revision_id": "r2", "publication_id": "pub2"}
    record = state["pending_changes"][error.pending_id]
    assert record["origin"] == "function"
    assert record["key"] == "refused-key"
    assert record["parts"][0]["commands"] == refused_commands
    assert record["parts"][0]["evaluation"] == refused_evaluation
