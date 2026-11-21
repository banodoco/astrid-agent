"""A dry run leaves no durable state: no in-flight request, no pending record.

The Runtime keeps nothing for a preview, so a dry run that gets no answer, or
is refused, must not block later commits or leave clutter to clean up. A stuck
dry-run request written by an older client is cleared by ``timelines discard``;
a real request with an unknown outcome is still kept for its exact retry.
"""
from __future__ import annotations

import json

import pytest

from astrid.sdk.timeline import sync
from astrid_runtime_client import ApiError
from tests.packs.timeline.test_cli_commit_move import command  # noqa: F401 - pytest fixture
from tests.sdk.test_timeline_sync import connected as sync_connection  # noqa: F401 - pytest fixture


def _cut_dry_runs(generated, monkeypatch):
    """The connection drops while a dry run is outstanding; real commits pass."""
    send = generated.commit_timeline
    cut = []

    def commit_timeline(project, timeline, body, *, idempotency_key):
        if body.get("dry_run", False):
            # The request was written durably before sending (the fake checks
            # it); the answer never arrives.
            assert json.loads(generated.path.read_bytes())["flight"] is not None
            cut.append(idempotency_key)
            raise ConnectionError("connection reset during preview")
        return send(project, timeline, body, idempotency_key=idempotency_key)

    monkeypatch.setattr(generated, "commit_timeline", commit_timeline)
    return cut


def _outbox(generated):
    return json.loads(generated.path.read_bytes())


def test_cut_dry_run_edit_then_real_commit_succeeds_and_leaves_no_flight(command, capsys, monkeypatch):
    run, generated = command
    cut = _cut_dry_runs(generated, monkeypatch)
    assert run("edit", "t", "--project", "p", "--clip", "a", "--on", "5", "--dry-run", "--json") == 1
    output = json.loads(capsys.readouterr().out)
    assert not output["ok"]
    assert cut
    state = _outbox(generated)
    assert state["flight"] is None
    assert state.get("pending_changes", {}) == {}

    assert run("status", "t", "--project", "p", "--json") == 0
    assert json.loads(capsys.readouterr().out)["data"]["connected_session"]["status"] != "in_flight"

    assert run("edit", "t", "--project", "p", "--clip", "a", "--on", "5", "--json") == 0
    assert json.loads(capsys.readouterr().out)["data"]["accepted_publication_id"] == "pub1"
    state = _outbox(generated)
    assert state["flight"] is None and state["pending"] == []


def test_cut_undo_dry_run_does_not_block_later_commits(command, capsys, monkeypatch):
    """The live case: an undo dry run with an explicit key timed out."""
    run, generated = command
    assert run("edit", "t", "--project", "p", "--clip", "a", "--on", "5", "--json") == 0
    capsys.readouterr()
    _cut_dry_runs(generated, monkeypatch)
    assert run("undo", "t", "--project", "p", "--commit", "pub1", "--dry-run",
               "--idempotency-key", "edit1-undo-dry", "--json") == 1
    capsys.readouterr()
    state = _outbox(generated)
    assert state["flight"] is None
    assert state["pending"] == []
    assert "commit_options" not in state

    assert run("edit", "t", "--project", "p", "--clip", "b", "--on", "7", "--json") == 0
    assert json.loads(capsys.readouterr().out)["data"]["accepted_publication_id"] == "pub2"
    assert _outbox(generated)["flight"] is None


def test_sdk_cut_dry_run_reports_nothing_kept_and_real_commit_succeeds(sync_connection, monkeypatch):  # noqa: F811
    from tests.sdk.test_timeline_sync import open_session

    client, generated, path = sync_connection
    session = open_session(sync_connection)
    _cut_dry_runs(generated, monkeypatch)
    with pytest.raises(sync.PreviewUnanswered) as unanswered:
        session.move("parent/clip:a", 5, dry_run=True, idempotency_key="preview")
    assert unanswered.value.outcome == "not_applied"
    assert "nothing was committed" in str(unanswered.value)
    assert json.loads(path.read_bytes())["flight"] is None
    accepted = open_session(sync_connection).move("parent/clip:a", 5, idempotency_key="real")
    assert accepted["receipt"]["receipt_id"] == "pub1"
    state = json.loads(path.read_bytes())
    assert state["flight"] is None and state["pending"] == []


@pytest.mark.parametrize("status", ["refused", "invalid"])
def test_refused_dry_run_leaves_pending_list_empty(command, capsys, status):
    run, generated = command
    generated.refusal = ApiError(409, "refused", "edit refused", details={"status": status})
    assert run("edit", "t", "--project", "p", "--clip", "a", "--on", "5", "--dry-run", "--json") == 1
    capsys.readouterr()
    state = _outbox(generated)
    assert state["flight"] is None
    assert state.get("pending_changes", {}) == {}
    assert run("pending", "list", "t", "--project", "p", "--json") == 0
    assert json.loads(capsys.readouterr().out)["data"]["pending_changes"] == []
    # The refusal is still reported, and a real commit is not blocked.
    generated.refusal = None
    assert run("edit", "t", "--project", "p", "--clip", "b", "--on", "7", "--json") == 0
    capsys.readouterr()
    assert len(generated.accepted) == 1


def test_sdk_refused_dry_run_saves_no_pending_record(sync_connection):  # noqa: F811
    from tests.sdk.test_timeline_sync import open_session

    client, generated, path = sync_connection
    session = open_session(sync_connection)
    generated.refusal = ApiError(409, "refused", "edit is not allowed",
                                 details={"status": "refused", "reason": "policy"})
    with pytest.raises(sync.CommitRefused) as refused:
        session.move("parent/clip:a", 5, dry_run=True)
    assert refused.value.pending_id is None
    state = json.loads(path.read_bytes())
    assert state["flight"] is None
    assert state["pending"] == []
    assert state.get("pending_changes", {}) == {}


def test_discard_clears_a_stuck_dry_run_flight(command, capsys, monkeypatch):
    """A dry-run flight left by an older client blocks commits until discard clears it."""
    run, generated = command
    assert run("edit", "t", "--project", "p", "--clip", "a", "--on", "5", "--json") == 0
    capsys.readouterr()
    _cut_dry_runs(generated, monkeypatch)
    # Reproduce the old client: the request is written before sending and
    # never cleared when no answer arrives.
    with monkeypatch.context() as old_client:
        old_client.setattr(sync, "_release_preview", lambda state, flight: None)
        assert run("undo", "t", "--project", "p", "--commit", "pub1", "--dry-run",
                   "--idempotency-key", "edit1-undo-dry", "--json") == 1
    capsys.readouterr()
    stuck = _outbox(generated)
    assert stuck["flight"] is not None and stuck["flight"]["key"] == "edit1-undo-dry"
    assert json.loads(stuck["flight"]["body"])["dry_run"] is True
    acknowledged = stuck.get("acknowledged")

    assert run("status", "t", "--project", "p", "--json") == 0
    session = json.loads(capsys.readouterr().out)["data"]["connected_session"]
    assert session["status"] == "in_flight" and session["dry_run"] is True

    # It blocks a real commit, as on the live install.
    assert run("edit", "t", "--project", "p", "--clip", "b", "--on", "7", "--json") == 1
    capsys.readouterr()
    assert _outbox(generated)["flight"] == stuck["flight"]

    assert run("discard", "t", "--project", "p") == 0
    out = capsys.readouterr().out
    assert "cleared an unanswered dry run" in out
    assert "outcome is unknown" not in out
    cleared = _outbox(generated)
    assert cleared["flight"] is None
    assert cleared["pending"] == []
    assert "commit_options" not in cleared
    assert cleared.get("acknowledged") == acknowledged
    assert cleared.get("pending_changes", {}) == stuck.get("pending_changes", {})

    assert run("edit", "t", "--project", "p", "--clip", "b", "--on", "7", "--json") == 0
    assert json.loads(capsys.readouterr().out)["data"]["accepted_publication_id"] == "pub2"


def test_discard_keeps_a_real_in_flight_commit(command, capsys):
    run, generated = command
    generated.lose_ack = True
    assert run("edit", "t", "--project", "p", "--clip", "a", "--on", "5", "--json") == 1
    capsys.readouterr()
    before = _outbox(generated)
    assert before["flight"] is not None
    assert json.loads(before["flight"]["body"]).get("dry_run", False) is False

    assert run("status", "t", "--project", "p", "--json") == 0
    assert "dry_run" not in json.loads(capsys.readouterr().out)["data"]["connected_session"]
    assert run("discard", "t", "--project", "p") == 0
    out = capsys.readouterr().out
    assert "outcome is unknown" in out and "did not undo it" in out
    assert "cleared an unanswered dry run" not in out
    assert _outbox(generated) == before
