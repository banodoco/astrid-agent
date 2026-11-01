"""Runtime acceptance for reviewed doc.patch lists and atomic functions."""
from __future__ import annotations

import copy

import pytest

from astrid.sdk.timeline._command_state import ABSENT, present
from astrid.sdk.timeline import Timeline
from astrid.sdk.timeline.sync import CommitConflict
from tests.editing_conformance.test_commit_runtime_m1 import open_client
from tests.sdk.test_timeline_editing_acceptance_runtime import editing_case


def patch(*changes):
    result = {"schema_version": 1, "op": "doc.patch", "version": "1", "select": None,
              "args": {"changes": list(changes)}}
    created = [f"parent/track:{change['id']}" for change in changes
               if change.get("op") == "list.insert" and change.get("path") == ["config", "tracks"]]
    if created:
        result["created"] = created
    return result


def field_change(identity, field, before, value):
    return {"op": "set", "ref": "parent",
            "path": ["config", "tracks", "id:" + identity, field],
            "before": copy.deepcopy(before), "value": copy.deepcopy(value)}


def add_track(identity, order_before=(), after_id=None):
    return patch({"op": "list.insert", "ref": "parent", "path": ["config", "tracks"],
                  "id": identity, "before": ABSENT.copy(),
                  "value": {"id": identity, "kind": "visual", "label": identity},
                  "order_before": list(order_before), "after_id": after_id})


def actor_timeline(editing_case, tmp_path, actor, timeline_id="film"):
    daemon, _, project, _ = editing_case
    scopes = ['handshake', 'projects:read', 'projects:write', 'objects:read', 'objects:write',
              'tasks:read', 'tasks:write']
    _, credential = daemon.credentials.provision(actor, scopes)
    client = open_client(daemon, actor=actor, credential=credential)
    return Timeline.open(project, timeline_id, client=client,
                         outbox_path=tmp_path / f"{actor}.json")


def seed_tracks(timeline):
    tracks = timeline._runtime_document["parent_revision"]["payload"].get("config", {}).get("tracks", [])
    order = [row["id"] for row in tracks]
    after = order[-1] if order else None
    result = timeline.commit([patch(
        add_track("c3", order, after)["args"]["changes"][0],
        add_track("c9", [*order, "c3"], "c3")["args"]["changes"][0],
    )], label="Seed reviewed rows",
                             merge="atomic", reads=[], key="seed-reviewed-rows")
    assert result["data"]["status"] == "accepted", result
    return [*order, "c3", "c9"]


def test_disjoint_reviewed_doc_patch_list_merges(editing_case, tmp_path):
    _, _, _, timeline = editing_case
    seed_tracks(timeline)
    reviewer = actor_timeline(editing_case, tmp_path, "reviewer-c9")
    stale_c3 = actor_timeline(editing_case, tmp_path, "reviewer-c3")

    first = stale_c3.commit([patch(field_change("c3", "label", present("c3"), "C3 approved"))],
                            label="Approve C3", merge="fields", reads=[], key="review-c3")
    assert first["data"]["status"] == "accepted", first
    merged = reviewer.commit([patch(field_change("c9", "label", present("c9"), "C9 approved"))],
                             label="Approve C9", key="review-c9")
    assert merged["data"]["status"] == "merged", merged
    rows = merged["data"]["canonical_document"]["parent_revision"]["payload"]["config"]["tracks"]
    labels = {row["id"]: row["label"] for row in rows}
    assert labels["c3"] == "C3 approved" and labels["c9"] == "C9 approved"

    stale_atomic = actor_timeline(editing_case, tmp_path, "atomic-c3")
    peer = actor_timeline(editing_case, tmp_path, "atomic-peer")
    atomic_peer = peer.commit([patch(field_change("c9", "label", present("C9 approved"), "C9 newer"))],
                              label="Change C9", merge="fields", reads=[], key="atomic-peer-write")
    assert atomic_peer["data"]["status"] == "accepted", atomic_peer
    with pytest.raises(CommitConflict) as raised:
        stale_atomic.commit([patch(field_change("c3", "label", present("C3 approved"), "C3 atomic"))],
                            label="Atomic C3", merge="atomic", key="atomic-c3-write")
    failure = getattr(raised.value, "failure", {})
    details = failure.get("details", {})
    assert details.get("status") == "conflict", failure
    assert details.get("conflicts"), failure


def test_reviewed_list_before_state_conflicts(editing_case, tmp_path):
    _, _, _, timeline = editing_case
    seed_tracks(timeline)
    stale = actor_timeline(editing_case, tmp_path, "before-state-stale")
    writer = actor_timeline(editing_case, tmp_path, "before-state-writer")

    changed = writer.commit([patch(field_change("c3", "label", present("c3"), "C3 changed"))],
                            label="Change C3", merge="fields", reads=[], key="before-state-writer")
    assert changed["data"]["status"] == "accepted", changed
    with pytest.raises(CommitConflict) as before_state:
        stale.commit([patch(field_change("c3", "label", present("c3"), "C3 stale"))],
                     label="Stale C3 review", key="before-state-stale")
    assert before_state.value.failure.get("details", {}).get("status") == "conflict"


def test_reviewed_list_aba_order_membership_and_idless_still_conflict(editing_case, tmp_path):
    _, _, _, timeline = editing_case
    seed_tracks(timeline)
    stale = actor_timeline(editing_case, tmp_path, "aba-stale")
    writer = actor_timeline(editing_case, tmp_path, "aba-writer")

    for before, value, key in (("c3", "C3 temporary", "aba-away"),
                               ("C3 temporary", "c3", "aba-back")):
        result = writer.commit([patch(field_change("c3", "label", present(before), value))],
            label="Change then restore C3", merge="fields", reads=[], key=key)
        assert result["data"]["status"] == "accepted", result
    with pytest.raises(CommitConflict) as aba:
        stale.commit([patch(field_change("c3", "label", present("c3"), "C3 stale"))],
                     label="Stale C3 review", key="aba-stale-write")
    assert getattr(aba.value, "failure", {}).get("details", {}).get("status") == "conflict"

    # A stale list-order observation conflicts even when the reviewed row is
    # otherwise untouched.
    stale_order = actor_timeline(editing_case, tmp_path, "order-stale")
    order_writer = actor_timeline(editing_case, tmp_path, "order-writer")
    document = stale_order._runtime_document
    rows = document["parent_revision"]["payload"]["config"]["tracks"]
    order_before = [row["id"] for row in rows]
    c9 = next(row for row in rows if row["id"] == "c9")
    order_change = {"op": "list.move", "ref": "parent", "path": ["config", "tracks"],
                    "id": "c9", "before": present(copy.deepcopy(c9)),
                    "order_before": order_before, "after_id": None}
    move = order_writer.commit([patch(order_change)], label="Move C9", merge="fields", reads=[],
                               key="move-c9")
    assert move["data"]["status"] == "accepted", move
    with pytest.raises(CommitConflict) as order:
        stale_order.commit([patch({"op": "list.insert", "ref": "parent", "path": ["config", "tracks"],
                                  "id": "c7", "before": ABSENT.copy(),
                                  "value": {"id": "c7", "kind": "visual", "label": "c7"},
                                  "order_before": order_before, "after_id": "c3"})],
                           label="Stale order review", key="stale-order")
    assert getattr(order.value, "failure", {}).get("details", {}).get("status") == "conflict"

    stale_membership = actor_timeline(editing_case, tmp_path, "membership-stale")
    membership_writer = actor_timeline(editing_case, tmp_path, "membership-writer")
    membership_base = [row["id"] for row in
                       stale_membership._runtime_document["parent_revision"]["payload"]["config"]["tracks"]]
    inserted = membership_writer.commit([patch({
        "op": "list.insert", "ref": "parent", "path": ["config", "tracks"],
        "id": "c7", "before": ABSENT.copy(),
        "value": {"id": "c7", "kind": "visual", "label": "c7"},
        "order_before": membership_base, "after_id": membership_base[-1],
    })], label="Add C7", merge="fields", reads=[], key="membership-c7")
    assert inserted["data"]["status"] == "accepted", inserted
    with pytest.raises(CommitConflict) as membership:
        stale_membership.commit([patch({
            "op": "list.insert", "ref": "parent", "path": ["config", "tracks"],
            "id": "c8", "before": ABSENT.copy(),
            "value": {"id": "c8", "kind": "visual", "label": "c8"},
            "order_before": membership_base, "after_id": membership_base[-1],
        })], label="Stale membership review", key="stale-membership")
    assert membership.value.failure.get("details", {}).get("status") == "conflict"

    # `meta` is an idless JSON list: a reviewed whole-list replacement keeps
    # its captured before-state and conflicts after a concurrent replacement.
    current = actor_timeline(editing_case, tmp_path, "idless-current")
    payload = current._runtime_document["parent_revision"]["payload"]
    meta = copy.deepcopy(payload.get("meta", {}))
    meta["org.example.reviewed"] = {"rows": ["first", "second"]}
    before_meta = present(copy.deepcopy(payload["meta"])) if "meta" in payload else ABSENT.copy()
    initialize = current.commit([patch({"op": "set", "ref": "parent", "path": ["meta"],
                                        "before": before_meta, "value": meta})],
                                label="Initialize idless rows", merge="atomic", reads=[], key="meta-init")
    assert initialize["data"]["status"] == "accepted", initialize
    stale_idless = actor_timeline(editing_case, tmp_path, "idless-stale")
    winner = actor_timeline(editing_case, tmp_path, "idless-winner")
    winner_result = winner.commit([patch({"op": "set", "ref": "parent",
                                          "path": ["meta", "org.example.reviewed", "rows"],
                                          "before": present(["first", "second"]),
                                          "value": ["first", "winner"]})],
                                  label="Update idless rows", merge="fields", reads=[], key="meta-winner")
    assert winner_result["data"]["status"] == "accepted", winner_result
    with pytest.raises(CommitConflict) as idless:
        stale_idless.commit([patch({"op": "set", "ref": "parent",
                                   "path": ["meta", "org.example.reviewed", "rows"],
                                   "before": present(["first", "second"]),
                                   "value": ["reviewed", "second"]})],
                            label="Stale idless review", key="meta-stale")
    assert getattr(idless.value, "failure", {}).get("details", {}).get("status") == "conflict"


def test_function_commit_still_atomic(editing_case, tmp_path):
    _, _, _, timeline = editing_case
    seed_tracks(timeline)
    concurrent = actor_timeline(editing_case, tmp_path, "function-concurrent")
    calls = []

    def edit(current):
        calls.append(current)
        rows = current.bundle["parent"]["config"]["tracks"]
        c3 = next(row for row in rows if row["id"] == "c3")
        c3["label"] = "Function C3"
        if len(calls) == 1:
            winner = concurrent.commit(
                [patch(field_change("c9", "label", present("c9"), "Concurrent C9"))],
                label="Concurrent C9", merge="fields", reads=[], key="function-peer")
            assert winner["data"]["status"] == "accepted", winner

    result = timeline.commit(edit, label="Function commit", key="function-atomic")
    assert result["data"]["status"] == "accepted", result
    assert result["data"].get("attempts") == 2
    assert len(calls) == 2
    assert result["data"]["merge_mode"] == "atomic"
    rows = result["data"]["canonical_document"]["parent_revision"]["payload"]["config"]["tracks"]
    labels = {row["id"]: row["label"] for row in rows}
    assert labels["c3"] == "Function C3" and labels["c9"] == "Concurrent C9"
