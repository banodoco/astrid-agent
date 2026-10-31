"""Deterministic regressions for concurrent and interrupted function saves."""
from __future__ import annotations

import copy
import json
from concurrent.futures import ThreadPoolExecutor
from threading import Event

import pytest

from astrid.sdk.timeline import commit_session
from astrid.sdk.timeline._pending import snapshot
from astrid.sdk.timeline.outbox import Outbox, request_bytes
from astrid.sdk.timeline.sync import CommitConflict, _part_identity
from astrid_runtime_client import ApiError
from tests.sdk.test_timeline_open_commit_v2 import opened

pytest_plugins = ("tests.sdk.test_timeline_sync",)


def test_pending_record_written_during_function_evaluation_survives(connected):
    timeline = opened(connected)
    peer = opened(connected)
    _, _generated, path = connected
    evaluating, release = Event(), Event()
    peer_error = []

    def save_peer_record():
        assert evaluating.wait(10)
        peer_error.append(peer.session.save_interrupted(
            [{"op": "clip.move", "select": "parent/clip:b", "args": {"on": 8}}],
            base={"revision_id": "r0", "publication_id": "pub0"},
            label="Concurrent pending edit",
            steps=[], merge="atomic", reads=None, key="concurrent-pending",
            evaluation={"kind": "function", "count": 1},
            failure={"code": "conflict", "details": {
                "status": "conflict", "reason": "value_conflict",
                "current": {"revision_id": "r0", "publication_id": "pub0"},
            }},
        ))
        release.set()

    def edit(working):
        evaluating.set()
        assert release.wait(10)
        working.move("parent/clip:a", 4)

    with ThreadPoolExecutor(max_workers=1) as pool:
        writer = pool.submit(save_peer_record)
        with pytest.raises(CommitConflict):
            timeline.commit(edit, label="Function during pending write", key="function-during-pending")
        writer.result(timeout=10)

    state = json.loads(path.read_text())
    assert len(peer_error) == 1
    peer_record = state["pending_changes"][peer_error[0].pending_id]
    assert peer_record["key"] == "concurrent-pending"
    assert peer_record["state"] == "pending"


def test_external_interrupted_save_retry_does_not_duplicate_suffix(
    connected, monkeypatch,
):
    timeline = opened(connected)
    _, generated, local_path = connected
    owner_path = local_path.with_name("pending-owner.json")
    owner = Outbox(owner_path)
    command = {"schema_version": 1, "op": "clip.move", "version": "1",
               "select": "parent/clip:a", "args": {"on": 3}}
    suffix = {"schema_version": 1, "op": "clip.move", "version": "1",
              "select": "parent/clip:b", "args": {"on": 6}}
    pending_id = "pc_external_function"
    fence = "released-function-flight"
    failure = {"code": "intervening_write", "details": {
        "status": "conflict", "reason": "intervening_write",
        "current": {"revision_id": "r0", "publication_id": "pub0"},
    }}
    body = {"api_version": 1, "project_id": "p", "timeline_id": "t",
            "base_revision_id": "r0", "base_publication_id": "pub0",
            "commands": [command], "source": {"via": "python"}, "dry_run": False,
            "label": "External function edit", "steps": [], "merge": "atomic"}
    flight = {"path": timeline.session.path, "body": request_bytes(body).decode(), "count": 1,
              "key": "external-function-key", "fence": fence, "explicit_key": True,
              "logical_key": "external-function-key", "evaluation": {"kind": "function", "count": 1},
              "pending_id": pending_id, "pending_path": str(owner_path), "pending_part": 0,
              "pending_how": "rerun"}
    initial_state = {"base": {"revision_id": "r0", "publication_id": "pub0"},
                     "pending": [command, suffix], "flight": flight,
                     "evaluation": {"kind": "function", "count": 1},
                     "conflict": copy.deepcopy(failure)}
    owner_state = {**initial_state, "pending": [command]}
    record = snapshot(owner_state, flight, failure)
    record["id"] = pending_id
    record["key"] = "external-function-key"
    identity = _part_identity(record)
    flight["pending_identity"] = identity
    record["flight"] = copy.deepcopy(flight)
    owner.write({"scope": {**timeline.session._state()["scope"], "actor_id": "owner"},
                 "pending_changes": {pending_id: record}})
    timeline.session._explicit_outbox = False
    with timeline.session.outbox.locked():
        state = timeline.session._state()
        state.update(copy.deepcopy(initial_state))
        state["flight"]["pending_identity"] = identity
        timeline.session.outbox.write(state)

    monkeypatch.setattr(commit_session, "_function_rerun_conflict", lambda *args: {
        "code": "intervening_write", "message": "overlap", "details": copy.deepcopy(failure["details"]),
    })
    original_write = Outbox.write
    failed = []

    def fail_local_after_owner_write(outbox, state):
        if outbox.path.resolve() == local_path.resolve() and not failed:
            owner_record = owner.read()["pending_changes"][pending_id]
            assert len(owner_record["parts"]) == 2
            failed.append(True)
            raise OSError("simulated caller outbox write failure")
        return original_write(outbox, state)

    monkeypatch.setattr(Outbox, "write", fail_local_after_owner_write)
    rerun = lambda working: working.move("parent/clip:a", 5)
    with pytest.raises(OSError, match="caller outbox write failure"):
        timeline.commit(rerun, label="External function edit", key="external-function-key",
                        pending=pending_id)
    durable_flight = json.loads(local_path.read_text())["flight"]
    assert durable_flight["fence"] == fence
    assert durable_flight["pending_identity"] == identity

    monkeypatch.setattr(Outbox, "write", original_write)
    restarted = opened(connected)
    restarted.session._explicit_outbox = False
    monkeypatch.setattr(commit_session, "_function_rerun_conflict", lambda *args: {
        "code": "intervening_write", "message": "overlap", "details": copy.deepcopy(failure["details"]),
    })
    with pytest.raises(CommitConflict):
        restarted.commit(rerun, label="External function edit", key="external-function-key",
                         pending=pending_id)

    saved = owner.read()["pending_changes"][pending_id]
    assert json.loads(local_path.read_text())["flight"] is None
    assert failed == [True]
    assert saved["last_refusal_request"] == fence
    assert len(saved["parts"]) == 2
    assert saved["parts"][0]["commands"][0]["args"] == {"on": 5}
    assert saved["parts"][1]["commands"] == [suffix]
    assert len(generated.calls) == 0
