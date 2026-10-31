"""Runtime-backed multipart retry race regressions."""

from __future__ import annotations

import json
import threading

import pytest

from tests.sdk.test_timeline_editing_acceptance_runtime import _media_file

pytest_plugins = ("tests.sdk.test_timeline_editing_acceptance_runtime",)


def test_losing_multipart_retry_keeps_caller_suffix_after_winner_advances_part(
    editing_case, tmp_path, monkeypatch,
):
    daemon, client, project, timeline = editing_case
    monkeypatch.setenv("ASTRID_RUNTIME_DATA_ROOT", str(tmp_path / "multipart-race-data"))

    from astrid.sdk.timeline import Timeline
    from astrid.sdk.timeline.sync import CommitConflict
    from astrid_runtime_client import ApiError
    from tests.editing_conformance.test_commit_runtime_m1 import open_client

    source = _media_file(tmp_path / "multipart-race-target.png")
    timeline.commit(
        lambda t: t.add(source, at=0, hold=3, track="V1", id="multipart-race-target"),
        label="Seed multipart race target", key="multipart-race-seed",
    )
    timeline = Timeline.open(project, "film", client=client)
    outbox_path = timeline.session.outbox.path
    entered, release = threading.Event(), threading.Event()
    generated = client._remote._transport._generated
    original = generated.commit_timeline

    _, credential = daemon.credentials.provision("multipart-race-peer", [
        "handshake", "projects:read", "projects:write", "objects:read", "objects:write",
        "tasks:read", "tasks:write",
    ])
    peer_client = open_client(daemon, actor="multipart-race-peer", credential=credential)

    def conflict_after_caller_queues_suffix(project_id, timeline_id, body, **kwargs):
        entered.set()
        if not release.wait(timeout=60):
            raise TimeoutError("multipart prefix was not released after caller queued suffix")
        raise ApiError(409, "conflict", "simulated retryable conflict", details={
            "status": "conflict", "reason": "injected_conflict",
            "conflicts": [], "current": dict(timeline.head),
        })

    monkeypatch.setattr(generated, "commit_timeline", conflict_after_caller_queues_suffix)
    prefix = timeline.session.queue_move("parent/clip:multipart-race-target", 5)
    outcomes = []

    def send_prefix():
        try:
            timeline.session.commit(
                prefix, idempotency_key="multipart-race-prefix", label="Multipart race prefix"
            )
        except Exception as exc:  # Preserve the worker thread's outcome for assertions.
            outcomes.append(exc)

    worker = threading.Thread(target=send_prefix)
    worker.start()
    try:
        assert entered.wait(timeout=60)
        suffix_caller = Timeline.open(
            project, "film", client=client, outbox_path=outbox_path,
        )
        suffix = suffix_caller.session.queue_move(
            "parent/clip:multipart-race-target", 7,
        )
        assert suffix_caller.session._state()["pending"] == [prefix, suffix]
    finally:
        release.set()
        worker.join(timeout=60)

    assert not worker.is_alive(), "multipart prefix did not finish after conflict"
    assert len(outcomes) == 1 and isinstance(outcomes[0], CommitConflict), outcomes
    pending_id = outcomes[0].pending_id
    assert pending_id
    saved_packet = json.loads(outbox_path.read_text(encoding="utf-8"))
    assert [part["commands"] for part in saved_packet["pending_changes"][pending_id]["parts"]] == [
        [prefix], [suffix],
    ]

    monkeypatch.setattr(generated, "commit_timeline", original)
    owner_preview = timeline.retry(pending_id)
    peer = Timeline.open(project, "film", client=peer_client)
    peer_preview = peer.retry(pending_id)
    assert owner_preview["status"] == peer_preview["status"] == "preview"
    assert peer_preview["_retry_path"] == str(outbox_path)

    winning = []
    refused = ApiError(409, "refused", "losing multipart retry refused", details={
        "status": "refused", "reason": "injected_refusal",
    })

    def peer_wins_then_refuse_loser(*args, **kwargs):
        assert json.loads(outbox_path.read_text(encoding="utf-8"))["flight"] is not None
        winning.append(peer.commit(peer_preview))
        assert winning[0]["status"] == "part_landed"
        raise refused

    monkeypatch.setattr(timeline.session.client.timelines, "commit", peer_wins_then_refuse_loser)
    loser = timeline.commit(owner_preview)

    state = json.loads(outbox_path.read_text(encoding="utf-8"))
    saved = state["pending_changes"][pending_id]
    winning_receipt_id = winning[0]["data"]["accepted_publication_id"]
    assert winning_receipt_id
    assert state["flight"] is None
    assert saved["state"] == "pending"
    assert saved["part"] == 1
    assert saved["receipts"] == [winning_receipt_id]
    assert [part["commands"] for part in saved["parts"]] == [[prefix], [suffix]]
    assert loser["status"] == "part_landed"
    assert loser["record"]["receipts"] == [winning_receipt_id]
    assert loser["data"]["receipt"] == {"receipt_id": winning_receipt_id}
