"""Public G2 sessions: real native execution, durable transport and conflict reruns.

The generated-client boundary is fake; provider-v2 and G3 emitter integration
remain separately owned. No test supplies a second patch executor.
"""
from __future__ import annotations

import copy
import hashlib
import json
import sys
from concurrent.futures import ThreadPoolExecutor
from threading import Event

import pytest

from astrid.sdk import timeline
from astrid.sdk.timeline import api, commit_session, retry as retry_module
from astrid.sdk.timeline.commit_session import _changed_ranges
from astrid.sdk.timeline._common import TimelineEditError
from astrid.sdk.timeline.commands import execute
from astrid.sdk.timeline.commit import bundle_from_document
from astrid.sdk.timeline.sync import CommitConflict, CommitOutcomeUnknown, CommitRefused
from astrid.sdk.workspace_client import WorkspaceClientError
from astrid_runtime_client import ApiError
from tests.sdk.test_timeline_commit_provider_document import document_fixture
from tests.sdk.test_timeline_sync import connected  # noqa: F401


def opened(connected):
    client, _, path = connected
    return timeline.open("p", "t", client=client, outbox_path=path)


def at(tl, clip="a"):
    return next(row["at"] for row in tl.bundle["parent"]["clips"] if row["id"] == clip)


def advance(generated, ordinal, *, clip="a"):
    base = generated.bundles[generated.head["revision_id"]]
    command = {"schema_version": 1, "op": "clip.move", "version": "1",
               "select": "parent/clip:" + clip, "args": {"on": ordinal}}
    revision = "other-" + str(ordinal)
    generated.bundles[revision] = execute(base, [command]).bundle
    generated.head = {"revision_id": revision, "publication_id": "pub-" + revision}
    # The peer head is produced by the real command engine above; keep the
    # public reader's authenticated revision/publication pair in sync with it.
    generated.publications[revision] = generated.head["publication_id"]


def conflict(generated, *, status="conflict", current=True):
    return ApiError(409 if status != "invalid" else 422, "conflict", "changed", details={
        "status": status, "reason": "value_conflict", "code": "value_conflict",
        "path": ["clips", "id:a", "at"], "hint": "read the head", "suggested_command": "timeline show",
        "current": copy.deepcopy(generated.head) if current else None,
    })


def test_open_exports_and_default_connection(connected, monkeypatch):
    from astrid.sdk import AstridClient

    client, _, path = connected
    monkeypatch.setattr(AstridClient, "open_from_launcher", lambda **kwargs: client)
    tl = timeline.open(outbox_path=path)
    assert isinstance(tl, timeline.Timeline)
    assert api.open is timeline.open
    assert api.Timeline is timeline.Timeline
    assert api.CommitConflict is timeline.CommitConflict
    assert tl.bundle == client.generated.bundles["r0"]
    assert tl.head == {"revision_id": "r0", "publication_id": "pub0"}
    assert client.reads == ["r0"]


def test_native_edits_commit_once_with_label_and_steps(connected):
    tl = opened(connected)
    _, generated, path = connected
    tl.move("parent/clip:a", 3).move("parent/clip:b", 6)
    assert generated.calls == []
    result = tl.commit("Move both", reads=[])
    assert len(generated.calls) == 1
    body = json.loads(generated.calls[0][0])
    assert body["label"] == "Move both"
    assert body["merge"] == "fields" and body["reads"] == []
    assert [step["command_ordinals"] for step in body["steps"]] == [[0], [1]]
    assert all(step["patch"] == [] for step in body["steps"])
    assert (at(tl), at(tl, "b")) == (3, 6)
    assert tl.head == {"revision_id": result["data"]["accepted_revision_id"],
                       "publication_id": result["receipt"]["receipt_id"]}
    assert json.loads(path.read_text())["pending"] == []
    assert tl._journal == []


@pytest.mark.parametrize("ref", ["id", "key"])
def test_commit_with_pending_closes_record(connected, ref):
    tl = opened(connected)
    record = {"id": "pc_close_me", "state": "pending", "rev": 2,
              "created_at": "2026-10-30T00:00:00Z", "key": "old-edit"}
    with tl.session.outbox.locked():
        state = tl.session._state()
        state["pending_changes"] = {record["id"]: record}
        tl.session.outbox.write(state)

    result = tl.commit(lambda working: working.move("parent/clip:a", 4),
                       label="Re-derived edit", pending=record["id"] if ref == "id" else record["key"])

    saved = tl.pending_show(record["id"])
    assert saved["state"] == "landed"
    assert saved["closed_by"] == {
        "actor_id": "actor", "publication_id": result["receipt"]["receipt_id"], "how": "rerun",
    }


def test_commit_key_rederives_and_closes_matching_pending_record(connected):
    tl = opened(connected)
    record = {"id": "pc_key_close", "state": "pending", "rev": 2,
              "created_at": "2026-10-30T00:00:00Z", "key": "saved-edit-key"}
    with tl.session.outbox.locked():
        state = tl.session._state()
        state["pending_changes"] = {record["id"]: record}
        tl.session.outbox.write(state)

    result = tl.commit(lambda working: working.move("parent/clip:a", 4), key=record["key"])

    saved = tl.pending_show(record["id"])
    assert saved["state"] == "landed"
    assert saved["closed_by"] == {
        "actor_id": "actor", "publication_id": result["receipt"]["receipt_id"], "how": "rerun",
    }


def test_commit_with_pending_closes_matching_other_actor_record(connected):
    from astrid.sdk.timeline.outbox import Outbox

    tl = opened(connected)
    tl.session._explicit_outbox = False
    peer_path = tl.session.outbox.path.parent / "peer.json"
    record = {"id": "pc_peer_close", "state": "pending", "rev": 2,
              "created_at": "2026-10-30T00:00:00Z", "key": "peer-edit"}
    Outbox(peer_path).write({
        "scope": {**tl.session._state()["scope"], "actor_id": "peer"},
        "pending_changes": {record["id"]: record},
    })

    result = tl.commit(lambda working: working.move("parent/clip:a", 5),
                       label="Close peer edit", pending=record["id"])

    closed = Outbox(peer_path).read()["pending_changes"][record["id"]]
    assert closed["state"] == "landed"
    assert closed["closed_by"]["publication_id"] == result["receipt"]["receipt_id"]


def test_commit_key_closes_unique_matching_pending_record_across_actors(connected):
    from astrid.sdk.timeline.outbox import Outbox

    tl = opened(connected)
    tl.session._explicit_outbox = False
    peer_path = tl.session.outbox.path.parent / "peer.json"
    record = {"id": "pc_peer_key_close", "state": "pending", "rev": 2,
              "created_at": "2026-10-30T00:00:00Z", "key": "peer-saved-key"}
    Outbox(peer_path).write({
        "scope": {**tl.session._state()["scope"], "actor_id": "peer"},
        "pending_changes": {record["id"]: record},
    })

    result = tl.commit(lambda working: working.move("parent/clip:a", 5), key=record["key"])

    closed = Outbox(peer_path).read()["pending_changes"][record["id"]]
    assert closed["state"] == "landed"
    assert closed["closed_by"]["publication_id"] == result["receipt"]["receipt_id"]


def test_retry_recovers_owner_close_after_local_ack_write_failure(connected, monkeypatch):
    from astrid.sdk.timeline.outbox import Outbox

    tl = opened(connected)
    _, generated, _ = connected
    tl.session._explicit_outbox = False
    owner_path = tl.session.outbox.path.parent / "assembly-owner.json"
    pending_id = "pc_assembly_owner"
    group_id = "preexisting-assembly-group"
    command = {"schema_version": 1, "op": "clip.move", "version": "1",
               "select": "parent/clip:a", "args": {"on": 4}}
    owner_record = {
        "id": pending_id, "state": "pending", "key": "assembly-edit", "label": "Assembly edit",
        "part": 0, "receipts": [], "closed_by": None,
        "parts": [{"base": {"revision_id": "r0", "publication_id": "pub0"},
                   "commands": [command], "group_id": group_id}],
    }
    Outbox(owner_path).write({
        "scope": {**tl.session._state()["scope"], "actor_id": "assembly-owner"},
        "pending_changes": {pending_id: owner_record},
    })

    original_write = Outbox.write
    failures = []

    def fail_owner_close_once(outbox, state):
        if outbox.path.resolve() == owner_path.resolve() and not failures:
            failures.append(True)
            raise OSError("simulated owner outbox write failure")
        return original_write(outbox, state)

    monkeypatch.setattr(Outbox, "write", fail_owner_close_once)
    result = tl.session.submit(
        [command], label="Assembly edit", steps=[], expected_state=tl.session._state(),
        pending_id=pending_id, pending_path=str(owner_path), pending_part=0,
        pending_how="retry", group_id=group_id,
    )

    assert result["status"] == "accepted"
    assert failures == [True]
    acknowledged = tl.session._state()["acknowledged"]
    saved_flight = next(flight for flight in acknowledged.values()
                        if flight.get("pending_id") == pending_id)
    receipt = saved_flight["pending_ack"]["receipt"]
    assert receipt["receipt_id"] == result["receipt"]["receipt_id"]
    assert Outbox(owner_path).read()["pending_changes"][pending_id]["state"] == "pending"
    assert len(generated.calls) == 1

    reopened = opened(connected)
    reopened.session._explicit_outbox = False
    recovered = reopened.retry(pending_id)

    closed = Outbox(owner_path).read()["pending_changes"][pending_id]
    assert recovered["status"] == "landed"
    assert closed["state"] == "landed"
    assert closed["closed_by"] == {
        "actor_id": "actor", "publication_id": receipt["receipt_id"], "how": "retry",
    }
    assert closed["receipts"] == [receipt["receipt_id"]]
    assert len(generated.calls) == 1


@pytest.mark.parametrize("mismatch", ["owner_path", "part"])
def test_retry_acknowledgement_requires_exact_owner_path_and_part(connected, mismatch, monkeypatch):
    from astrid.sdk.timeline.outbox import Outbox

    tl = opened(connected)
    owner_path = tl.session.outbox.path.parent / "assembly-owner.json"
    record = {"id": "pc_assembly_owner", "state": "pending", "part": 0,
              "parts": [{"base": {"revision_id": "r0", "publication_id": "pub0"}}]}
    Outbox(owner_path).write({
        "scope": {**tl.session._state()["scope"], "actor_id": "assembly-owner"},
        "pending_changes": {record["id"]: record},
    })
    flight = {
        "pending_id": record["id"], "pending_path": str(owner_path), "pending_part": 0,
        "pending_how": "retry",
        "pending_ack": {"revision_id": "r1", "publication_id": "pub1", "publisher": "actor",
                        "receipt": {"receipt_id": "pub1"}},
    }
    if mismatch == "owner_path":
        flight["pending_path"] = str(owner_path.with_name("other-owner.json"))
    else:
        flight["pending_part"] = 1
    with tl.session.outbox.locked():
        state = tl.session._state()
        state["acknowledged"] = {"assembly-retry": flight}
        tl.session.outbox.write(state)
    monkeypatch.setattr(retry_module.clash, "_history", lambda *args: (tl.head, []))

    assert retry_module._landed(tl, owner_path, record) is None
    assert Outbox(owner_path).read()["pending_changes"][record["id"]]["state"] == "pending"


def test_commit_key_refuses_ambiguous_pending_match_across_actors(connected):
    from astrid.sdk.timeline.outbox import Outbox

    tl = opened(connected)
    _, generated, _ = connected
    tl.session._explicit_outbox = False
    key = "shared-pending-key"
    local = {"id": "pc_local_ambiguous", "state": "pending", "key": key}
    peer = {"id": "pc_peer_ambiguous", "state": "pending", "key": key}
    with tl.session.outbox.locked():
        state = tl.session._state()
        state["pending_changes"] = {local["id"]: local}
        tl.session.outbox.write(state)
    peer_path = tl.session.outbox.path.parent / "peer.json"
    Outbox(peer_path).write({
        "scope": {**tl.session._state()["scope"], "actor_id": "peer"},
        "pending_changes": {peer["id"]: peer},
    })

    with pytest.raises(TimelineEditError, match="ambiguous"):
        tl.commit(lambda working: working.move("parent/clip:a", 5), key=key)
    assert generated.calls == []


def test_commit_key_preserves_unrelated_idempotency_key(connected):
    tl = opened(connected)
    _, generated, _ = connected

    result = tl.commit(lambda working: working.move("parent/clip:a", 6), key="ordinary-key")

    assert result["receipt"]["idempotency_key"] == "ordinary-key"
    assert generated.calls[0][1] == "ordinary-key"


def test_accepted_move_reports_exact_revision_pair_and_old_new_ranges(connected):
    tl = opened(connected)
    preview = tl.commit(lambda working: working.move("parent/clip:a", 3), dry_run=True)
    assert "changed_ranges" not in preview and "changed_ranges" not in preview["data"]

    accepted = tl.commit(preview["data"]["commands"], label="Move clip")
    data = accepted["data"]
    assert data["previous_revision_id"] == "r0"
    assert data["accepted_revision_id"] == accepted["data"]["canonical_document"]["parent_revision"]["revision_id"]
    assert accepted["changed_ranges"] == data["changed_ranges"] == [
        {"start": 0.0, "end": 1.0}, {"start": 3.0, "end": 4.0},
    ]


def test_accepted_removal_reports_the_removed_span(connected):
    tl = opened(connected)
    accepted = tl.commit([{"schema_version": 1, "op": "clip.delete", "version": "1",
                           "select": "parent/clip:a", "args": {}}], label="Remove clip")
    assert accepted["data"]["changed_ranges"] == [{"start": 0.0, "end": 1.0}]


def test_shortened_effect_reports_its_before_and_after_window():
    before = {"parent": {"config": {"output": {"fps": 30}}, "clips": [
        {"id": "shot", "track": "picture", "at": 2, "hold": 5,
         "exit": {"type": "fade", "duration": 2}},
    ]}}
    after = copy.deepcopy(before)
    after["parent"]["clips"][0]["exit"]["duration"] = 0.5
    assert _changed_ranges(before, after) == [{"start": 5.0, "end": 7.0}]


def test_occurrence_move_reports_its_old_and_new_spans():
    before = {"placements": [
        {"occurrence_id": "occ", "placement": {"start_ms": 1000}, "duration_ms": 2000},
    ]}
    after = {"placements": [
        {"occurrence_id": "occ", "placement": {"start_ms": 5000}, "duration_ms": 2000},
    ]}
    assert _changed_ranges(before, after) == [
        {"start": 1.0, "end": 3.0}, {"start": 5.0, "end": 7.0},
    ]


@pytest.mark.parametrize("selected", [None, ["step-2"]])
def test_public_session_undo_preserves_whole_shape_and_forwards_steps(connected, selected):  # noqa: F811
    tl = opened(connected)
    _, generated, _ = connected
    tl.move("parent/clip:a", 3).move("parent/clip:b", 6)
    accepted = tl.commit("Move both")
    publication = accepted["receipt"]["receipt_id"]

    selector = None if selected is None else {publication: selected}
    tl.undo(publications=[publication], steps=selector)

    inverse = json.loads(generated.calls[-1][0])["commands"][0]
    assert inverse["args"]["publications"] == [publication]
    if selector is None:
        assert "steps" not in inverse["args"]
    else:
        assert inverse["args"]["steps"] == selector


@pytest.mark.parametrize("status", ["merged", "rebased"])
def test_adopts_complete_merged_head_including_other_writer(connected, monkeypatch, status):
    tl = opened(connected)
    _, generated, _ = connected
    tl.move("parent/clip:a", 4)
    advance(generated, 9, clip="b")
    original = generated.commit_timeline

    def reply(*args, **kwargs):
        result = original(*args, **kwargs)
        if result["data"]["status"] == "rebased":
            result["data"]["status"] = status
            result["receipt"]["result"]["status"] = status
        return result

    monkeypatch.setattr(generated, "commit_timeline", reply)
    result = tl.commit("Move a")
    assert result["data"]["status"] == status
    assert (at(tl), at(tl, "b")) == (4, 9)
    tl.move("parent/clip:a", 5)
    tl.commit("Continue from merged head")
    assert json.loads(generated.calls[-1][0])["base_revision_id"] == result["data"]["accepted_revision_id"]
    assert at(tl, "b") == 9


def test_declared_request_and_step_reads_and_atomic_are_preserved(connected):
    tl = opened(connected)
    _, generated, _ = connected
    step_reads = [{"kind": "collection", "ref": "parent", "path": ["clips"],
                   "members": ["a", "b"], "order": ["a", "b"]}]
    request_reads = [{"kind": "scope", "ref": "internal-revision:physical-id", "path": []}]
    tl.command({"schema_version": 1, "op": "clip.move", "version": "1", "select": "parent/clip:a",
                "args": {"on": 2}}, label="Step", intent="place title", tags=["title"], reads=step_reads)
    step_reads.clear()
    tl.commit("Atomic batch", merge="atomic", reads=request_reads)
    body = json.loads(generated.calls[0][0])
    assert body["reads"] == request_reads and body["merge"] == "atomic"
    assert body["steps"][0]["reads"][0]["kind"] == "collection"
    assert body["steps"][0]["intent"] == "place title"
    assert body["steps"][0]["tags"] == ["title"]


def test_function_reads_each_returned_head_and_recomputes_with_new_keys(connected, monkeypatch):
    tl = opened(connected)
    client, generated, _ = connected
    # This fake advances heads without publication history; overlap behavior is
    # covered against Runtime in test_timeline_clash_back_runtime.py.
    monkeypatch.setattr(commit_session, "_function_rerun_conflict", lambda *args: None)
    original = generated.commit_timeline
    sent, evaluations = [], []

    def reply(*args, **kwargs):
        sent.append((copy.deepcopy(args[2]), kwargs["idempotency_key"]))
        if len(sent) < 3:
            advance(generated, len(sent) * 10)
            raise conflict(generated)
        return original(*args, **kwargs)

    monkeypatch.setattr(generated, "commit_timeline", reply)

    def increment(working):
        evaluations.append((working.head, at(working)))
        working.move("parent/clip:a", at(working) + 1)

    result = tl.commit(increment, idempotency_key="first-evaluation")
    assert [value for _, value in evaluations] == [0, 10, 20]
    assert [head["revision_id"] for head, _ in evaluations] == ["r0", "other-10", "other-20"]
    assert client.reads == ["r0", "r0", "other-10", "other-20", "other-20"]
    assert len({key for _, key in sent}) == 3
    assert sent[0][1] == "first-evaluation"
    assert [body["base_revision_id"] for body, _ in sent] == ["r0", "other-10", "other-20"]
    assert all(body["merge"] == "atomic" and "reads" not in body for body, _ in sent)
    assert all(body["steps"][0]["reads"] == [{"kind": "scope", "ref": "timeline", "path": []}]
               for body, _ in sent)
    assert at(tl) == 21
    assert result["attempts"] == 3
    assert "after re-reading the timeline (3 attempts)" in result["summary"]


def test_declared_function_reads_allow_fields(connected):
    tl = opened(connected)
    _, generated, _ = connected
    tl.commit(lambda working: working.move("parent/clip:a", 5), reads=[])
    body = json.loads(generated.calls[0][0])
    assert body["merge"] == "fields" and body["reads"] == []
    assert "reads" not in body["steps"][0]


def test_three_evaluation_bound_saves_function_record(connected, monkeypatch):
    tl = opened(connected)
    _, generated, path = connected
    # This fake advances heads without publication history; overlap behavior is
    # covered against Runtime in test_timeline_clash_back_runtime.py.
    monkeypatch.setattr(commit_session, "_function_rerun_conflict", lambda *args: None)
    evaluations, sent = [], []

    def refuse(*args, **kwargs):
        sent.append(kwargs["idempotency_key"])
        advance(generated, len(sent))
        raise conflict(generated)

    monkeypatch.setattr(generated, "commit_timeline", refuse)

    def edit(working):
        evaluations.append(at(working))
        working.move("parent/clip:a", at(working) + 1)

    with pytest.raises(CommitConflict) as exhausted:
        tl.commit(edit)
    assert exhausted.value.retry_fn is edit
    assert exhausted.value.outcome == "not_applied"
    assert "edit" in exhausted.value.headline and "3 times" in exhausted.value.detail
    assert evaluations == [0, 1, 2] and len(set(sent)) == 3
    state = json.loads(path.read_text())
    assert state["flight"] is None and state["pending"] == []
    record = next(iter(state["pending_changes"].values()))
    assert record["parts"][0]["evaluation"]["count"] == 3
    assert record["parts"][0]["commands"]
    assert len(evaluations) == len(sent) == 3


def test_retry_delegates_pending_ref_and_retained_function(connected, monkeypatch):
    tl = opened(connected)
    calls = []
    monkeypatch.setattr(
        retry_module, "retry",
        lambda owner, ref, fn=None, *, overwrite=False:
            calls.append((owner, ref, fn, overwrite)) or "retried",
    )

    def edit(working):
        working.move("parent/clip:a", 9)

    error = CommitConflict({"details": {"status": "conflict"}}, pending_id="pc-1")
    error.retry_fn = edit
    assert tl.retry(error) == "retried"
    assert tl.retry("pc-2", fn=edit) == "retried"
    assert calls == [(tl, "pc-1", edit, False), (tl, "pc-2", edit, False)]


def test_commit_retry_preview_delegates_to_retry_module(connected, monkeypatch):
    tl = opened(connected)
    calls = []
    monkeypatch.setattr(
        retry_module, "commit_preview",
        lambda owner, preview: calls.append((owner, preview)) or "committed",
    )
    preview = {"status": "preview", "data": {"retry": {"pending_id": "pc-1", "token": "t"}}}

    assert tl.commit(preview) == "committed"
    assert calls == [(tl, preview)]


def test_commit_rederive_result_explains_to_follow_next(connected):
    tl = opened(connected)
    result = {"status": "re_derive", "data": {"conflicts": []}, "next": "re-derive the edit"}

    with pytest.raises(TimelineEditError, match=r"re-derive result is not a preview; follow r\['next'\]"):
        tl.commit(result)


@pytest.mark.parametrize("options", [
    {"label": "ignored"}, {"merge": "atomic"}, {"dry_run": True},
    {"reads": []}, {"key": "ignored"}, {"idempotency_key": "ignored"},
    {"pending": "pc-1"},
])
def test_commit_retry_preview_rejects_extra_options(connected, monkeypatch, options):
    tl = opened(connected)
    calls = []
    monkeypatch.setattr(
        retry_module, "commit_preview",
        lambda owner, preview: calls.append((owner, preview)) or "committed",
    )
    preview = {"status": "preview", "data": {"retry": {"pending_id": "pc-1", "token": "t"}}}

    with pytest.raises(TimelineEditError, match="retry preview commits do not accept"):
        tl.commit(preview, **options)
    assert calls == []


@pytest.mark.parametrize("status", ["refused", "invalid"])
def test_definitive_nonconflicts_never_rerun(connected, monkeypatch, status):
    tl = opened(connected)
    _, generated, _ = connected
    evaluations = []
    monkeypatch.setattr(generated, "commit_timeline", lambda *args, **kwargs: (_ for _ in ()).throw(conflict(generated, status=status)))

    def edit(working):
        evaluations.append(1)
        working.move("parent/clip:a", 3)

    with pytest.raises(CommitRefused) as caught:
        tl.commit(edit)
    assert caught.value.failure["details"]["status"] == status
    assert caught.value.retry_fn is edit
    assert len(evaluations) == 1
    assert caught.value.pending_id
    saved = tl.pending_show(caught.value.pending_id)
    assert saved["state"] == "pending"
    assert saved["origin"] == "function"
    assert saved["failure"] == caught.value.failure
    assert saved["parts"][0]["commands"]


@pytest.mark.parametrize("lost_ack", [False, True])
def test_unknown_outcome_retries_exact_request_without_reevaluation(connected, monkeypatch, lost_ack):
    tl = opened(connected)
    _, generated, path = connected
    original = generated.commit_timeline
    evaluations, sent = [], []
    generated.lose_ack = lost_ack

    def send(*args, **kwargs):
        sent.append((copy.deepcopy(args[2]), kwargs["idempotency_key"]))
        if not lost_ack and len(sent) == 1:
            raise ApiError(503, "not_ready", "unknown outcome")
        return original(*args, **kwargs)

    monkeypatch.setattr(generated, "commit_timeline", send)

    def edit(working):
        evaluations.append(1)
        working.move("parent/clip:a", 7)

    with pytest.raises(CommitOutcomeUnknown) as unknown:
        tl.commit(edit, idempotency_key="stable")
    forbidden = ("fail", "error", "lost", "not committed")
    assert unknown.value.outcome == "unknown"
    assert all(word not in unknown.value.headline.lower() for word in forbidden)
    assert "stable" in unknown.value.fix
    frozen = json.loads(path.read_text())["flight"]
    reopened = opened(connected)
    reopened.commit(edit)
    assert evaluations == [1]
    assert len(sent) == 2 and sent[0] == sent[1]
    assert sent[1][1] == frozen["key"] == "stable"
    assert len(generated.accepted) == 1 and at(reopened) == 7
    replay = reopened.commit(edit, idempotency_key="stable")
    assert replay["replayed"] is True
    assert not any(word in replay["summary"].lower() for word in ("again", "redo", "re-appl"))


def test_key_alias_replays_function_before_evaluation(connected):
    tl = opened(connected)
    _, generated, _ = connected
    evaluations = []

    def edit(working):
        evaluations.append(True)
        working.move("parent/clip:a", 7)

    first = tl.commit(edit, key="public-key")
    replay = opened(connected).commit(edit, key="public-key")
    assert first["receipt"] == replay["receipt"]
    assert replay["replayed"] is True
    assert evaluations == [True]
    assert len(generated.calls) == 2 and generated.calls[0] == generated.calls[1]


def test_key_alias_conflict_refuses_before_state_mutation(connected):
    tl = opened(connected)
    _, generated, path = connected
    before = path.read_bytes()
    with pytest.raises(TimelineEditError, match="only one of key or idempotency_key"):
        tl.commit(lambda working: working.move("parent/clip:a", 7),
                  key="new", idempotency_key="legacy")
    assert path.read_bytes() == before
    assert generated.calls == []


def test_key_alias_submits_fixed_command_list_with_exact_key(connected):
    tl = opened(connected)
    _, generated, _ = connected
    preview = tl.commit(lambda working: working.move("parent/clip:a", 6), dry_run=True)
    commands = copy.deepcopy(preview["data"]["commands"])
    accepted = tl.commit(commands, label="Fixed list", key="list-key")
    assert accepted["receipt"]["idempotency_key"] == "list-key"
    assert generated.calls[-1][1] == "list-key"
    assert json.loads(generated.calls[-1][0])["commands"] == commands


def test_fixed_list_replays_acknowledged_key_without_idempotency_mismatch(connected):
    tl = opened(connected)
    _, generated, _ = connected
    commands = [{"schema_version": 1, "op": "clip.move", "version": "1",
                 "select": "parent/clip:a", "args": {"on": 6}}]

    first = tl.commit(commands, label="Fixed list", key="fixed-list-key")
    replay = opened(connected).commit(commands, label="Fixed list", key="fixed-list-key")

    assert first["receipt"] == replay["receipt"]
    assert replay["replayed"] is True
    assert len(generated.calls) == 2
    assert generated.calls[0] == generated.calls[1]


def test_function_reruns_after_shared_outbox_advance_during_evaluation(connected, monkeypatch):
    tl = opened(connected)
    peer = opened(connected)
    _, generated, _ = connected
    # This fake's bundle omits the canonical document used by the production
    # overlap guard; overlap behavior is covered by the runtime-backed tests.
    monkeypatch.setattr(commit_session, "_function_rerun_conflict", lambda *args: None)
    observed = []

    def edit(working):
        observed.append(at(working, "b"))
        if len(observed) == 1:
            peer.commit(lambda other: other.move("parent/clip:b", 8),
                        label="Shared outbox peer", key="shared-outbox-peer")
        working.move("parent/clip:a", 4)

    result = tl.commit(edit, label="Disjoint shared outbox rerun", key="shared-outbox-function")

    assert result["receipt"]["receipt_id"]
    assert observed == [0, 8]
    assert at(tl) == 4
    assert at(tl, "b") == 8
    assert tl.pending_list() == []
    assert json.loads(tl.session.outbox.path.read_text())["pending"] == []
    assert len(generated.accepted) == 2


def test_function_rerun_guard_saves_flight_and_dependent_suffix_in_order(connected, monkeypatch):
    tl = opened(connected)
    _, generated, path = connected
    entered, release = Event(), Event()
    first_request = []

    def conflict_after_suffix(_project, _timeline, body, *, idempotency_key):
        first_request.append((copy.deepcopy(body), idempotency_key))
        entered.set()
        assert release.wait(10)
        raise ApiError(409, "conflict", "peer changed the timeline", details={
            "status": "conflict", "reason": "value_conflict",
            "current": copy.deepcopy(generated.head),
        })

    monkeypatch.setattr(generated, "commit_timeline", conflict_after_suffix)
    peer = execute(generated.bundles["r0"], [{
        "schema_version": 1, "op": "clip.move", "version": "1",
        "select": "parent/clip:a", "args": {"on": 9},
    }]).bundle
    generated.bundles["peer"] = peer
    generated.head = {"revision_id": "peer", "publication_id": "pub-peer"}
    generated.publications["peer"] = "pub-peer"

    evaluations = []
    target = [5]

    def edit(working):
        evaluations.append(working.head["revision_id"])
        working.move("parent/clip:a", target[0])

    original_base = {"revision_id": "r0", "publication_id": "pub0"}

    def overlap_on_rerun(_session, _original, current, *_bundles):
        if current != original_base:
            return {
                "code": "intervening_write",
                "message": "function rerun overlaps an intervening write",
                "details": {"status": "conflict", "reason": "intervening_write",
                            "current": copy.deepcopy(generated.head), "conflicts": []},
            }
        return None

    monkeypatch.setattr(commit_session, "_function_rerun_conflict", overlap_on_rerun)
    with ThreadPoolExecutor(max_workers=1) as pool:
        committing = pool.submit(tl.commit, edit, label="Rerun edit", key="rerun-key")
        try:
            assert entered.wait(10)
            queueing = opened(connected).session
            suffix = [queueing.queue_move("parent/clip:b", 7),
                      queueing.queue_move("parent/clip:b", 8)]
        finally:
            release.set()
        with pytest.raises(CommitConflict) as error:
            committing.result(timeout=10)

    durable = json.loads(path.read_text())
    assert evaluations == ["r0", "peer"]
    assert len(first_request) == 1
    assert first_request[0][1] == "rerun-key"
    assert generated.accepted == {}
    assert durable["flight"] is None and durable["pending"] == []
    assert len(durable["pending_changes"]) == 1
    record = durable["pending_changes"][error.value.pending_id]
    assert record["state"] == "pending"
    assert record["key"] == "rerun-key"
    assert record["flight"]["key"] == "rerun-key"
    assert len(record["parts"]) == 2
    assert record["parts"][0]["evaluation"]["kind"] == "function"
    assert record["parts"][0]["evaluation"]["count"] == 2
    assert json.loads(record["flight"]["body"])["commands"] == record["parts"][0]["commands"]
    assert record["parts"][1]["commands"] == suffix
    assert record["parts"][0]["base"] == original_base
    assert record["parts"][1]["base"] == original_base
    assert record["receipts"] == [] and record["closed_by"] is None

    saved_suffix = copy.deepcopy(record["parts"][1])
    target[0] = 6
    with pytest.raises(CommitConflict) as repeated:
        tl.commit(edit, label="Rerun edit", key="rerun-key")

    repeated_record = json.loads(path.read_text())["pending_changes"][error.value.pending_id]
    assert repeated.value.pending_id == error.value.pending_id
    assert repeated_record["part"] == 0
    assert repeated_record["parts"][0]["commands"] == [{
        "schema_version": 1, "op": "clip.move", "version": "1",
        "select": "parent/clip:a", "args": {"on": 6},
    }]
    assert repeated_record["parts"][1] == saved_suffix

    probed = []

    def accepted_preview(_session, request):
        probed.append(copy.deepcopy(request))
        return {"would_status": "accepted", "evaluated_head": copy.deepcopy(generated.head)}

    monkeypatch.setattr(retry_module, "_probe", accepted_preview)
    monkeypatch.setattr(retry_module, "_preview_diff", lambda *_args: {"changes": []})
    preview = tl.retry(error.value.pending_id)

    assert preview["status"] == "preview"
    assert preview["pending_id"] == error.value.pending_id
    assert preview["data"]["commands"] == repeated_record["parts"][0]["commands"]
    assert probed[0]["commands"] == repeated_record["parts"][0]["commands"]


def test_failed_ack_persistence_reconciles_without_running_function(connected, monkeypatch):
    tl = opened(connected)
    _, generated, _ = connected
    write = tl.session.outbox.write

    def fail_ack(state):
        if state["flight"] is None and not state["pending"]:
            raise OSError("ack storage unavailable")
        write(state)

    monkeypatch.setattr(tl.session.outbox, "write", fail_ack)
    with pytest.raises(OSError):
        tl.commit(lambda working: working.move("parent/clip:a", 7))
    restarted = opened(connected)
    restarted.commit(lambda working: pytest.fail("accepted function evaluated again"))
    assert generated.calls[0] == generated.calls[1]
    assert len(generated.accepted) == 1 and at(restarted) == 7


def test_dry_run_has_no_history_or_key_reservation_and_returns_commands_and_diff(connected):
    tl = opened(connected)
    _, generated, path = connected
    before = copy.deepcopy(tl.bundle)
    initial_head = tl.head
    preview = tl.commit(lambda working: working.move("parent/clip:a", 6), dry_run=True,
                        key="caller-key")
    assert preview["receipt"] is None and preview["data"]["status"] == "preview"
    assert "nothing committed or reserved" in preview["summary"]
    assert preview["undo"] is None
    assert preview["data"]["commands"]
    assert preview["data"]["diff"]["changes"]
    assert preview["data"]["diff"]["changes"][0]["op"] == "clip.move"
    assert tl.bundle == before and tl.head == initial_head
    assert tl._journal == [] and generated.accepted == {} and generated.records == {}
    state = json.loads(path.read_text())
    assert state["base"] == initial_head and state["pending"] == [] and state["flight"] is None
    assert "preview_key" not in state
    assert "commit_options" not in state and "evaluation" not in state and "acknowledged" not in state
    accepted = tl.commit(preview["data"]["commands"], label="Accept preview", key="caller-key")
    assert accepted["receipt"] is not None
    assert accepted["undo"] == {"publications": [accepted["receipt"]["receipt_id"]]}
    assert accepted["summary"].startswith('Committed "Accept preview" as ')
    assert json.loads(generated.calls[1][0])["commands"] == preview["data"]["commands"]
    assert generated.calls[0][1] != generated.calls[1][1]
    assert len(generated.accepted) == 1 and at(tl) == 6
    tl.undo(publications=[accepted["receipt"]["receipt_id"]])
    assert generated.bundles[generated.head["revision_id"]]["parent"] == before["parent"]


def test_fixed_list_does_not_recompute_after_conflict(connected, monkeypatch):
    tl = opened(connected)
    _, generated, path = connected
    preview = tl.commit(lambda working: working.move("parent/clip:a", 6), dry_run=True)
    commands = copy.deepcopy(preview["data"]["commands"])
    sent = []

    def refuse(*args, **kwargs):
        sent.append(copy.deepcopy(args[2]))
        raise conflict(generated)

    monkeypatch.setattr(generated, "commit_timeline", refuse)
    with pytest.raises(CommitConflict):
        tl.commit(commands, label="Fixed")
    assert len(sent) == 1 and sent[0]["commands"] == commands
    state = json.loads(path.read_text())
    assert state["pending"] == [] and state["flight"] is None
    record = next(iter(state["pending_changes"].values()))
    assert record["parts"][0]["commands"] == commands
    assert record["failure"]["details"]["status"] == "conflict"


def test_media_preview_list_reuse_keeps_verified_selection_without_reimport(connected, monkeypatch, tmp_path):
    from astrid.sdk.timeline import media_add
    from tests.sdk import test_timeline_sync as sync_test

    tl = opened(connected)
    _, generated, _ = connected
    source = tmp_path / "preview.png"
    payload = b"preview image"
    source.write_bytes(payload)
    digest = "sha256:" + hashlib.sha256(payload).hexdigest()
    pin = {"asset": "preview-asset", "object_id": digest,
           "generation_id": "preview-generation", "variant_id": "preview-variant",
           "catalog_digest": "sha256:" + "a" * 64}
    verified = {"selection": copy.deepcopy(pin), "media_type": "image/png", "size": len(payload),
                "width": 1, "height": 1}
    imports = []
    monkeypatch.setattr("astrid.sdk.authoring_media._metadata", lambda _path: {
        "digest": digest, "media_type": "image/png", "filename": source.name,
        "width": 1, "height": 1,
    })
    monkeypatch.setattr(media_add, "_selection", lambda *_args: (imports.append(pin) or pin, verified))
    execute = sync_test.execute
    monkeypatch.setattr(sync_test, "execute", lambda bundle, commands, **kwargs: execute(
        bundle, commands, verified_media=[verified], **kwargs))

    preview = tl.commit(lambda working: working.add(source, at=0, hold=2, id="preview-clip"), dry_run=True)
    commands = copy.deepcopy(preview["data"]["commands"])
    assert preview["receipt"] is None
    assert preview["imports"] == [{"object_id": digest, "generation_id": pin["generation_id"],
                                    "variant_id": pin["variant_id"], "catalog_write": True,
                                    "retained_on_undo": True}]
    accepted = tl.commit(commands, label="Apply media preview")
    submitted = json.loads(generated.calls[-1][0])["commands"]
    assert submitted == commands and submitted[-1]["args"]["media"] == pin
    assert len(imports) == 1 and accepted["receipt"] is not None
    assert "1 file(s) in the catalog; undo keeps them" in accepted["summary"]


def test_preview_then_function_call_evaluates_fresh_intent(connected):
    tl = opened(connected)
    _, generated, path = connected
    evaluations = []

    def edit(working):
        evaluations.append(at(working))
        working.move("parent/clip:a", at(working) + 1)

    preview = tl.commit(edit, dry_run=True)
    accepted = tl.commit(edit)
    assert preview["receipt"] is None and accepted["receipt"] is not None
    assert evaluations == [0, 0]
    state = json.loads(path.read_text())
    assert state["pending"] == [] and state["flight"] is None
    assert len(generated.accepted) == 1 and at(tl) == 1


def test_preview_refusal_saves_edit_without_commit_or_key_reservation(connected):
    tl = opened(connected)
    _, generated, path = connected
    generated.refusal = ApiError(409, "conflict", "preview refused", details={"status": "refused"})
    with pytest.raises(CommitRefused) as caught:
        tl.commit(lambda working: working.move("parent/clip:a", 6), dry_run=True,
                  idempotency_key="caller-key")
    state = json.loads(path.read_text())
    assert state["pending"] == [] and state["flight"] is None
    assert "commit_options" not in state and "evaluation" not in state
    assert "acknowledged" not in state and "preview_key" not in state
    assert caught.value.pending_id
    saved = tl.pending_show(caught.value.pending_id)
    assert saved["state"] == "pending"
    assert saved["origin"] == "function"
    assert saved["failure"] == caught.value.failure
    assert saved["failure"]["details"]["status"] == "refused"
    assert saved["parts"][0]["commands"]
    assert len(generated.calls) == 1
    sent_body = json.loads(generated.calls[0][0])
    assert sent_body["dry_run"] is True
    assert generated.accepted == {}


def test_callback_exception_and_nested_commit_publish_nothing(connected):
    tl = opened(connected)
    _, generated, _ = connected
    before = copy.deepcopy(tl.bundle)

    def edit(working):
        working.move("parent/clip:a", 5)
        raise ValueError("failed computation")

    with pytest.raises(ValueError):
        tl.commit(edit)
    with pytest.raises(TimelineEditError, match="own evaluation"):
        tl.commit(lambda working: working.commit("nested"))
    assert tl.bundle == before and generated.calls == [] and tl._journal == []


def test_missing_g3_emitter_refuses_free_edits_without_publication(connected, monkeypatch):
    tl = opened(connected)
    _, generated, _ = connected
    # Keep the original unavailable-dependency control now that the real
    # emitter is mounted in this branch.  The ordinary G2 bundle intentionally
    # has no verified Runtime document, so allowing the import would exercise a
    # different dependency failure instead of the original refusal boundary.
    monkeypatch.setitem(sys.modules, "astrid.sdk.timeline.emitter", None)
    tl.bundle["parent"]["clips"][0]["at"] = 4
    with pytest.raises(TimelineEditError, match="G3 document emitter"):
        tl.commit("Free edit")
    assert generated.calls == [] and at(tl) == 4


def test_g3_emitter_uses_existing_provider_with_verified_runtime_document(connected):
    client, generated, path = connected
    document = document_fixture(shared=False)
    document.update(project_id="p", timeline_id="t")
    document["parent_revision"]["revision_id"] = "r0"
    generated.bundles = {"r0": bundle_from_document(document)}
    assert "_runtime_document" in generated.bundles["r0"]["source_mapping"]

    tl = timeline.open("p", "t", client=client, outbox_path=path)
    tl.bundle["parent"]["name"] = "Joined film"
    # The existing generated provider compiles only changed child images; keep
    # the fixture's complete closure valid through its real acceptance path.
    tl.bundle["shots"]["chapter-shot"]["payload"]["name"] = "Joined chapter"
    result = tl.commit("Rename film")

    assert result["data"]["status"] == "accepted"
    assert len(generated.calls) == 1
    body = json.loads(generated.calls[0][0])
    assert body["label"] == "Rename film"
    assert body["merge"] == "atomic"
    assert body["commands"][0]["op"] == "doc.patch"
    assert {
        "op": "set", "ref": "parent", "path": ["name"],
        "before": {"state": "present", "value": "Renamed Film"},
        "value": "Joined film",
    } in body["commands"][0]["args"]["changes"]
    assert {
        "op": "set", "ref": "shot-revision:shot-1", "path": ["name"],
        "before": {"state": "present", "value": "Chapter One"},
        "value": "Joined chapter",
    } in body["commands"][0]["args"]["changes"]
    assert body["steps"][0]["label"] == "Edit document"
    assert body["steps"][0]["patch"] == body["commands"][0]["args"]["changes"]
    assert generated.bundles[result["data"]["accepted_revision_id"]]["parent"]["name"] == "Joined film"


def test_g3_emitter_seam_captures_helpers_once(connected, monkeypatch):
    tl = opened(connected)
    _, generated, _ = connected
    calls = []

    # This lightweight connected fixture has no Runtime source mapping, so it
    # cannot exercise the separate media-admission scan.
    from astrid.sdk.timeline import publish
    monkeypatch.setattr(publish, "_media_admission_inputs", lambda *_: [])

    def emit(before, after):
        calls.append((before, after))
        return [{"schema_version": 1, "op": "clip.move", "version": "1", "select": "parent/clip:a",
                 "args": {"on": after["parent"]["clips"][0]["at"]}}]

    monkeypatch.setattr(commit_session, "emit_document_commands", emit)
    tl.bundle["parent"]["clips"][0]["at"] = 4
    tl.commit("Free edit")
    assert len(calls) == len(generated.calls) == 1 and at(tl) == 4
    assert calls[0][0]["parent"]["clips"][0]["at"] == 0
    body = json.loads(generated.calls[0][0])
    assert body["merge"] == "atomic" and "reads" not in body


def test_stale_local_handle_cannot_submit_at_another_sessions_base(connected):
    first = opened(connected)
    stale = opened(connected)
    _, generated, _ = connected
    stale.move("parent/clip:a", 6)
    first.move("parent/clip:b", 9).commit("Other handle")
    with pytest.raises(TimelineEditError, match="base changed"):
        stale.commit("Stale")
    assert len(generated.calls) == 1


def test_unknown_retry_then_conflict_continues_with_persisted_evaluation_count(connected, monkeypatch):
    tl = opened(connected)
    _, generated, path = connected
    # This fake advances heads without publication history; overlap behavior is
    # covered against Runtime in test_timeline_clash_back_runtime.py.
    monkeypatch.setattr(commit_session, "_function_rerun_conflict", lambda *args: None)
    original = generated.commit_timeline
    sent, evaluations = [], []

    def send(*args, **kwargs):
        sent.append((copy.deepcopy(args[2]), kwargs["idempotency_key"]))
        if len(sent) == 1:
            advance(generated, 10)
            raise conflict(generated)
        if len(sent) == 2:
            raise ApiError(503, "not_ready", "unknown")
        if len(sent) == 3:
            advance(generated, 20)
            raise conflict(generated)
        return original(*args, **kwargs)

    monkeypatch.setattr(generated, "commit_timeline", send)

    def edit(working):
        evaluations.append(at(working))
        working.move("parent/clip:a", at(working) + 1)

    with pytest.raises(WorkspaceClientError):
        tl.commit(edit)
    assert json.loads(path.read_text())["evaluation"]["count"] == 2
    reopened = opened(connected)
    reopened.commit(edit)
    assert evaluations == [0, 10, 20] and at(reopened) == 21
    assert sent[1] == sent[2]
    assert len({key for _, key in sent}) == 3


def test_unknown_payload_cannot_change_key_or_preview_mode(connected):
    tl = opened(connected)
    _, generated, path = connected
    generated.lose_ack = True
    tl.move("parent/clip:a", 4)
    with pytest.raises(WorkspaceClientError):
        tl.commit("Original", idempotency_key="original")
    frozen = path.read_bytes()
    with pytest.raises(TimelineEditError, match="immutable"):
        tl.commit("Original", idempotency_key="replacement")
    with pytest.raises(TimelineEditError, match="immutable"):
        tl.commit("Original", dry_run=True)
    assert path.read_bytes() == frozen and len(generated.accepted) == 1
    tl.commit("Original")
    assert len(generated.accepted) == 1


def test_conflict_without_verified_current_pair_does_not_rerun(connected, monkeypatch):
    tl = opened(connected)
    _, generated, _ = connected
    evaluations = []

    def refuse(*args, **kwargs):
        raise conflict(generated, current=False)

    monkeypatch.setattr(generated, "commit_timeline", refuse)

    def edit(working):
        evaluations.append(1)
        working.move("parent/clip:a", 4)

    with pytest.raises(TimelineEditError, match="verified publication"):
        tl.commit(edit)
    assert len(evaluations) == 1


def test_label_commit_does_not_reauthor_after_conflict(connected, monkeypatch):
    tl = opened(connected)
    _, generated, path = connected
    sent = []

    def refuse(*args, **kwargs):
        sent.append(kwargs["idempotency_key"])
        advance(generated, 10)
        raise conflict(generated)

    monkeypatch.setattr(generated, "commit_timeline", refuse)
    tl.move("parent/clip:a", 4)
    with pytest.raises(CommitConflict):
        tl.commit("Literal")
    frozen = path.read_bytes()
    with pytest.raises(TimelineEditError, match="no pending"):
        tl.commit("Literal")
    assert json.loads(path.read_bytes())["pending_changes"] == json.loads(frozen)["pending_changes"]
    assert len(sent) == 1


def test_conflict_replacement_preserves_later_pending_dependents(connected, monkeypatch):
    tl = opened(connected)
    _, generated, path = connected

    def refuse(*args, **kwargs):
        advance(generated, 10)
        raise conflict(generated)

    monkeypatch.setattr(generated, "commit_timeline", refuse)
    tl.move("parent/clip:a", 4)
    with pytest.raises(CommitConflict):
        tl.commit("Literal")
    with tl.session.outbox.locked():
        state = tl.session._state()
        record = next(iter(state["pending_changes"].values()))
        state["pending"].append(copy.deepcopy(record["parts"][0]["commands"][0]))
        tl.session.outbox.write(state)
    before = path.read_bytes()
    with pytest.raises(TimelineEditError, match="before another edit"):
        tl.session.submit(state["pending"][:1], label="Replacement", steps=[], expected_state=state,
                          base=generated.head)
    assert path.read_bytes() == before


def test_saved_fixed_list_conflict_suggests_retry(connected, monkeypatch):
    tl = opened(connected)
    client, generated, path = connected
    preview = tl.commit(lambda working: working.move("parent/clip:a", 6), dry_run=True)
    commands = copy.deepcopy(preview["data"]["commands"])

    def refuse(*args, **kwargs):
        raise conflict(generated)

    monkeypatch.setattr(generated, "commit_timeline", refuse)
    with pytest.raises(CommitConflict) as first:
        tl.commit(commands, label="Fixed")
    assert "tl.retry(" in first.value.fix
    record = next(iter(json.loads(path.read_text())["pending_changes"].values()))
    assert record["parts"][0]["commands"] == commands
    assert record["failure"]["details"]["status"] == "conflict"

    again = timeline.open("p", "t", client=client, outbox_path=path)
    for attempt in (
        lambda: again.commit(lambda working: working.move("parent/clip:a", 7), dry_run=True),
        lambda: again.commit(lambda working: working.move("parent/clip:a", 7)),
        lambda: again.commit(commands, label="Fixed again"),
        lambda: again.commit(commands, label="Fixed again", dry_run=True),
    ):
        with pytest.raises(CommitConflict) as second:
            attempt()
        assert "tl.retry(" in second.value.fix or "no pending" in second.value.fix, second.value.fix
