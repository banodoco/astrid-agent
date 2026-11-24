"""The interim timing edit may change only occurrence timing leaves."""
from __future__ import annotations

import copy

import pytest

from astrid.sdk.contracts import DomainResult
from astrid.sdk.timeline.commit import bundle_from_document
from astrid.sdk.timeline.commit_session import Timeline
from astrid.sdk.timeline.emitter import DocumentPatchError
from astrid.sdk.timeline.outbox import Outbox
from astrid.sdk.timeline.sync import CommitConflict, TimelineSession
from astrid_runtime_client import ApiError
from tests.editing_conformance.test_interim_ripple_public import _film, _open_film
from tests.sdk.test_timeline_arrange_commit import arranged_session, runtime_abi  # noqa: F401
from tests.sdk.test_timeline_commit_provider import CommitRejected

_GUARD_ERROR = "closure membership or provenance changed; use a semantic command"


@pytest.mark.parametrize("damage", ["membership", "order", "link", "provenance"])
def test_timing_change_does_not_mask_existing_occurrence_guard(arranged_session, damage):  # noqa: F811
    """An eligible start edit must not turn a forbidden closure edit into a write."""
    editor, realm, calls = arranged_session
    calls_before = len(calls)

    def edit(working):
        working.bundle["placements"][0]["placement"]["start_ms"] += 250
        if damage == "membership":
            working.bundle["placements"].pop()
        elif damage == "order":
            working.bundle["placements"].reverse()
        elif damage == "link":
            working.bundle["placements"][0]["shot_revision_id"] = "forged-revision"
        else:
            working.bundle["source_mapping"]["placements"]["occ-00"]["shot_revision_id"] = "forged-revision"

    with pytest.raises(DocumentPatchError) as caught:
        editor.commit(edit, label="Guarded occurrence timing", dry_run=True)

    assert type(caught.value) is DocumentPatchError
    assert str(caught.value) == _GUARD_ERROR
    assert len(calls) == calls_before
    assert realm.accepted == []


def _peer_writer(editor, realm, tmp_path):
    identity = editor.session.identity
    outbox = Outbox(tmp_path / "interim-ripple-peer.json")
    outbox.write({"scope": {**identity, "project_id": editor.session.project_id,
                            "timeline_id": editor.session.timeline_id},
                  "base": copy.deepcopy(realm.head), "pending": [], "flight": None})
    session = TimelineSession(editor.session.client, outbox, identity,
                              editor.session.project_id, editor.session.timeline_id)
    bundle = bundle_from_document(realm.documents[realm.head["revision_id"]])
    bundle["base_parent"]["publication_id"] = realm.head["publication_id"]
    return Timeline(session, bundle, realm.head)


def _set_start(working, start_ms):
    working.bundle["placements"][0]["placement"]["start_ms"] = start_ms


def _convert_provider_refusals(editor):
    client = editor.session.client
    client.timelines._client = client
    original = client.timelines.commit

    def commit(*args, **kwargs):
        try:
            return original(*args, **kwargs)
        except CommitRejected as exc:
            raise ApiError(409, "conflict", exc.failure["message"],
                           details=exc.failure["details"]) from exc

    client.timelines.commit = commit


def test_stale_fixed_occurrence_preview_keeps_its_original_timing_guard(tmp_path, monkeypatch):
    document, capture = _film(anchored=False)
    editor, realm, calls = _open_film(tmp_path, monkeypatch, document, capture)
    _convert_provider_refusals(editor)
    preview = editor.commit(lambda working: _set_start(working, 1000),
                            label="Review occurrence timing", dry_run=True)
    fixed = copy.deepcopy(preview["data"]["commands"])
    assert fixed

    peer = _peer_writer(editor, realm, tmp_path)
    accepted = peer.commit(lambda working: _set_start(working, 3000),
                           label="Concurrent occurrence timing", key="interim-peer-timing")
    assert accepted["receipt"]["receipt_id"]

    calls_before_stale = len(calls)
    with pytest.raises(CommitConflict):
        editor.commit(fixed, label="Apply reviewed occurrence timing", key="interim-stale-list")

    assert len(calls) == calls_before_stale + 1
    assert realm.head["revision_id"] != "parent-1"


@pytest.mark.parametrize("peer_starts", [[3000], [3000, 2000]], ids=["same-field", "aba"])
def test_callback_rerun_refuses_same_occurrence_timing_write_and_aba(
    tmp_path, monkeypatch, peer_starts,
):
    document, capture = _film(anchored=False)
    editor, realm, calls = _open_film(tmp_path, monkeypatch, document, capture)
    peer = _peer_writer(editor, realm, tmp_path)
    client = editor.session.client
    _convert_provider_refusals(editor)

    def history(_project, _timeline, *, cursor=None, limit=100):
        start = int(cursor or 0)
        end = min(len(realm.entries), start + limit)
        return DomainResult.success({"head": copy.deepcopy(realm.head),
                                     "items": copy.deepcopy(realm.entries[start:end]),
                                     "next_cursor": str(end) if end < len(realm.entries) else None})

    client.timelines.history = history
    original_send = client.timelines.commit
    injected = False
    evaluations = []

    def peer_write_then_submit(project, timeline, body, *, idempotency_key):
        nonlocal injected
        if body.get("label") == "Function occurrence timing" and not injected:
            injected = True
            for index, start_ms in enumerate(peer_starts):
                peer.commit(lambda working, value=start_ms: _set_start(working, value),
                            label="Concurrent occurrence timing", key=f"interim-peer-{index}")
        return original_send(project, timeline, body, idempotency_key=idempotency_key)

    client.timelines.commit = peer_write_then_submit

    def function_edit(working):
        evaluations.append(working.bundle["placements"][0]["placement"]["start_ms"])
        _set_start(working, 4000)

    with pytest.raises(CommitConflict):
        editor.commit(function_edit, label="Function occurrence timing", key="interim-function-timing")

    assert injected
    assert evaluations == [2000, peer_starts[-1]]
    assert sum(call["label"] == "Function occurrence timing" for call in calls) == 1
    assert realm.head["revision_id"] != "parent-1"
