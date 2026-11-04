"""Wording of commit outcomes against the locked Runtime.

E14 asserts behaviour (receipts, counts, exception types). These tests pin the
strings an agent reads: headline, fix, outcome, the Runtime reason, and the
success summary. Each expected string follows the T2 rules: say what happened
and what to do next, never imply an unknown commit failed, never say a retry
redid work, never say a preview reserved anything, and keep the raw detail.
"""
from __future__ import annotations

import copy
import json

import pytest

from tests.sdk.test_timeline_editing_acceptance_runtime import (  # noqa: F401  (fixture re-exported)
    _history,
    _snapshot,
    editing_case,
)

UNKNOWN_HEADLINE = "No reply could be verified; this edit may or may not have been committed"
UNKNOWN_DETAIL = "The exact request is saved. No new request will be sent until it is confirmed."
UNKNOWN_UNKEYED_FIX = (
    "Call tl.commit() again with no arguments to confirm the saved request; "
    "the edit is not re-run blindly."
)


def _unknown_keyed_fix(key):
    return f'Call tl.commit(key="{key}") again with no other arguments to confirm the saved request.'


def _lost_first_reply(monkeypatch, client):
    """Accept the first commit on the Runtime, then lose its reply."""
    from astrid_runtime_client import ApiError

    generated = client._remote._transport._generated
    original = generated.commit_timeline
    sent = []

    def lose_first_response(*args, **kwargs):
        sent.append((copy.deepcopy(args[2]), kwargs["idempotency_key"]))
        result = original(*args, **kwargs)
        if len(sent) == 1:
            raise ApiError(503, "not_ready", "simulated lost response after acceptance")
        return result

    monkeypatch.setattr(generated, "commit_timeline", lose_first_response)
    return sent


def test_case1_fps_removal_refusal_reason_and_wording(editing_case):
    _daemon, client, project, timeline = editing_case
    from astrid.sdk.timeline.sync import CommitConflict
    from tests.editing_conformance.test_doc_patch_v2 import change, patch

    fps = _snapshot(timeline)["parent_revision"]["payload"]["config"].get("output", {}).get("fps")
    if fps is None:
        timeline.commit(lambda t: t.bundle["parent"]["config"].setdefault("output", {}).update({"fps": 30}),
                        label="Declare FPS for refusal wording", key="e14r1-seed-fps")
        fps = _snapshot(timeline)["parent_revision"]["payload"]["config"]["output"]["fps"]
    before = _snapshot(timeline)
    history_before = _history(client, project)
    removal = change(["config", "output", "fps"], {"state": "present", "value": fps}, op="remove")
    with pytest.raises(CommitConflict) as info:
        timeline.commit([patch(removal)], label="Refused arbitrary FPS removal", key="e14r1-refused-fps")
    exc = info.value
    assert exc.failure["details"]["status"] == "invalid"
    assert exc.failure["details"]["reason"] == "invalid_document"
    assert exc.headline == "This edit is not valid; nothing was committed"
    assert exc.fix == (
        f'Inspect the saved edit with tl.pending_show("{exc.pending_id}"), '
        "fix the edit, then retry or drop it."
    )
    assert exc.outcome == "not_applied"
    assert exc.detail == exc.failure["message"] == (
        "candidate: all authored fps declarations must retain their typed locations")
    assert len(_history(client, project)) == len(history_before)
    assert _snapshot(timeline) == before
    state = json.loads(timeline.session.outbox.path.read_bytes())
    assert state["flight"] is None
    saved = state["pending_changes"][exc.pending_id]
    assert saved["state"] == "pending"
    assert saved["failure"]["details"] == exc.failure["details"]


def test_case2_stale_literal_list_conflict_is_not_rerun(editing_case, tmp_path, monkeypatch):
    daemon, client, project, timeline = editing_case
    from astrid.sdk.timeline.sync import CommitConflict
    from tests.editing_conformance.test_commit_runtime_m1 import open_client

    _, peer_credential = daemon.credentials.provision("e14r1-peer", [
        "handshake", "projects:read", "projects:write", "objects:read", "objects:write",
        "tasks:read", "tasks:write",
    ])
    peer_client = open_client(daemon, actor="e14r1-peer", credential=peer_credential)
    fixed = timeline.commit(
        lambda t: t.bundle["parent"].update({"custom": {"literal_edit": True}}),
        label="Preview fixed list", dry_run=True,
    )["data"]["commands"]
    stale = type(timeline).open(project, "film", client=client, outbox_path=tmp_path / "stale.json")
    other = type(timeline).open(project, "film", client=peer_client, outbox_path=tmp_path / "peer.json")
    peer_edit = other.commit(
        lambda t: t.bundle["parent"].update({"custom": {"peer_edit": True}}),
        label="Concurrent list change", key="e14r1-peer-change",
    )
    assert peer_edit["receipt"]["receipt_id"]
    generated = client._remote._transport._generated
    original = generated.commit_timeline
    attempts = []

    def count_submission(*args, **kwargs):
        attempts.append(copy.deepcopy(args[2]))
        return original(*args, **kwargs)

    monkeypatch.setattr(generated, "commit_timeline", count_submission)
    with pytest.raises(CommitConflict) as info:
        stale.commit(fixed, label="Apply reviewed list", key="e14r1-fixed-list")
    exc = info.value
    assert len(attempts) == 1
    assert exc.failure["details"]["status"] == "conflict"
    assert exc.headline == "The timeline changed since this list was made; nothing was committed"
    assert exc.detail == exc.failure["message"]
    assert exc.fix == (
        f'Someone else changed this timeline after your edit was made, so "Apply reviewed list" '
        f'was not committed. Your edit is saved as {exc.pending_id}; nothing is lost. '
        f'Retry it with tl.retry("{exc.pending_id}").')
    assert exc.outcome == "not_applied"
    saved = json.loads((tmp_path / "stale.json").read_bytes())
    assert not saved.get("rejected")
    assert len(saved["pending_changes"]) == 1
    record = saved["pending_changes"][exc.pending_id]
    assert record["state"] == "pending"
    assert record["parts"][0]["commands"] == fixed


def test_case2_stale_literal_list_merges_disjoint_fields_without_rerun(editing_case, tmp_path, monkeypatch):
    daemon, client, project, timeline = editing_case
    from tests.editing_conformance.test_commit_runtime_m1 import open_client

    _, peer_credential = daemon.credentials.provision("e14r1-peer", [
        "handshake", "projects:read", "projects:write", "objects:read", "objects:write",
        "tasks:read", "tasks:write",
    ])
    peer_client = open_client(daemon, actor="e14r1-peer", credential=peer_credential)
    fixed = timeline.commit(
        lambda t: t.bundle["parent"].update({"custom": {"literal_edit": True}}),
        label="Preview fixed list", dry_run=True,
    )["data"]["commands"]
    stale = type(timeline).open(project, "film", client=client, outbox_path=tmp_path / "stale.json")
    other = type(timeline).open(project, "film", client=peer_client, outbox_path=tmp_path / "peer.json")
    peer_edit = other.commit(lambda t: t.bundle["parent"].update({"name": "Concurrent list change"}),
                             label="Concurrent list change", key="e14r1-peer-change")
    assert peer_edit["receipt"]["receipt_id"]
    generated = client._remote._transport._generated
    original = generated.commit_timeline
    attempts = []

    def count_submission(*args, **kwargs):
        attempts.append(copy.deepcopy(args[2]))
        return original(*args, **kwargs)

    monkeypatch.setattr(generated, "commit_timeline", count_submission)
    applied = stale.commit(fixed, label="Apply reviewed list", key="e14r1-fixed-list")
    assert len(attempts) == 1
    assert applied["data"]["status"] == "merged"
    parent = applied["data"]["canonical_document"]["parent_revision"]["payload"]
    assert parent["name"] == "Concurrent list change"
    assert parent["custom"] == {"literal_edit": True}
    assert _snapshot(stale) == applied["data"]["canonical_document"]
    saved = json.loads((tmp_path / "stale.json").read_bytes())
    assert saved["flight"] is None
    assert saved.get("pending_changes", {}) == {}


@pytest.mark.parametrize("kind", ["function", "list"])
@pytest.mark.parametrize("keyed", [True, False])
def test_case3_lost_reply_unknown_wording(editing_case, monkeypatch, kind, keyed):
    _daemon, client, project, timeline = editing_case
    from astrid.sdk.timeline.sync import CommitOutcomeUnknown

    key = "e14r1-lost-reply" if keyed else None
    label = f"Lost reply {kind} {'keyed' if keyed else 'unkeyed'}"
    if kind == "function":
        def edit(t):
            t.bundle["parent"]["name"] = "Lost reply name"
        target = edit
    else:
        # Preview before the patch: a dry run is also a Runtime send.
        target = timeline.commit(
            lambda t: t.bundle["parent"].update({"name": "Lost reply name"}),
            label=label, dry_run=True,
        )["data"]["commands"]
    sent = _lost_first_reply(monkeypatch, client)
    with pytest.raises(CommitOutcomeUnknown) as info:
        timeline.commit(target, label=label, key=key)
    exc = info.value
    assert exc.outcome == "unknown"
    assert exc.headline == UNKNOWN_HEADLINE
    assert exc.detail == UNKNOWN_DETAIL
    assert exc.fix == (_unknown_keyed_fix(key) if keyed else UNKNOWN_UNKEYED_FIX)
    assert exc.details["outcome"] == "unknown"
    assert exc.details["cause"]["code"] == "not_ready"
    assert len(sent) == 1
    # The advertised call carries no list or function: it resends the saved request.
    retried = timeline.commit(key=key) if keyed else timeline.commit()
    assert retried["receipt"]["receipt_id"]
    assert len(sent) == 2 and sent[0] == sent[1]


def test_case4_keyed_replay_returns_original_receipt_without_evaluating(editing_case):
    _daemon, client, project, timeline = editing_case

    evaluations = []

    def edit(t):
        evaluations.append(True)
        t.bundle["parent"]["name"] = "Keyed replay"

    accepted = timeline.commit(edit, label="Keyed replay", key="e14r1-replay")
    replay = type(timeline).open(project, "film", client=client,
                                 outbox_path=timeline.session.outbox.path).commit(
        lambda _t: pytest.fail("an acknowledged keyed function must replay before evaluation"),
        label="Keyed replay", key="e14r1-replay",
    )
    assert replay["receipt"] == accepted["receipt"]
    assert replay["replayed"] is True
    assert replay["summary"] == (
        f'Already committed as {accepted["receipt"]["receipt_id"]}; nothing was run or changed now.')
    assert evaluations == [True]


def test_case5_preview_reserves_nothing(editing_case):
    _daemon, client, project, timeline = editing_case
    before = _snapshot(timeline)
    history_before = _history(client, project)
    preview = timeline.commit(lambda t: t.bundle["parent"].update({"name": "Preview only"}),
                              label="Keyed preview", dry_run=True, key="e14r1-preview")
    assert preview["receipt"] is None
    assert preview["summary"] == 'Preview of "Keyed preview": nothing committed or reserved.'
    assert _snapshot(timeline) == before
    assert len(_history(client, project)) == len(history_before)
    outbox_state = json.loads(timeline.session.outbox.path.read_text(encoding="utf-8"))
    assert outbox_state.get("pending", []) == [] and outbox_state.get("flight") is None
    assert "e14r1-preview" not in outbox_state.get("acknowledged", {})
    # Nothing was reserved, so the same key now commits as a fresh edit.
    accepted = timeline.commit(lambda t: t.bundle["parent"].update({"name": "Keyed preview"}),
                               label="Keyed preview", key="e14r1-preview")
    assert accepted["receipt"]["receipt_id"]
    assert accepted["replayed"] is False


def test_case6_duplicate_results_are_separate_and_lost_copy_matches_case3(
    editing_case, monkeypatch,
):
    _daemon, client, project, timeline = editing_case
    from astrid.sdk.timeline.commit import bundle_from_document
    from astrid.sdk.timeline.native_occurrence_create import capture_occurrence_create
    from astrid.sdk.timeline.commands import execute
    from astrid.sdk.timeline_duplicate import _CopyOutcomeUnknown
    from astrid_runtime_client import ApiError

    bundle = bundle_from_document(_snapshot(timeline))
    births = []
    for index in range(2):
        command = capture_occurrence_create(
            bundle, f"r1-occ-{index}", f"r1-shot-{index}", name=f"R1 shot {index}",
            track="V1", on=index * 2, duration=1,
        )
        births.append(command)
        bundle = execute(bundle, [command]).bundle
    seeded = timeline.commit(births, label="Seed duplicate wording", key="e14r1-seed-duplicate")
    assert seeded["receipt"]["receipt_id"]

    duplicated = timeline.duplicate(as_="variant-e14r1-ok")
    assert set(duplicated["destination_setup"]) == {"timeline_id", "idempotency_key", "receipt"}
    assert duplicated["destination_setup"]["timeline_id"] == "variant-e14r1-ok"
    assert duplicated["destination_setup"]["receipt"]
    assert duplicated["copy_receipt"]["receipt_id"]
    assert duplicated["copy_receipt"]["receipt_id"] != duplicated["destination_setup"]["receipt"].get("receipt_id")

    generated = client._remote._transport._generated
    original = generated.commit_timeline
    lost = []

    def lose_copy_response(project_id, timeline_id, body, **kwargs):
        if timeline_id == "variant-e14r1-lost" and any(
                command.get("op") == "timeline.duplicate" for command in body.get("commands", ())):
            lost.append(copy.deepcopy(body))
            result = original(project_id, timeline_id, body, **kwargs)
            if len(lost) == 1:
                raise ApiError(503, "not_ready", "simulated lost duplicate response")
            return result
        return original(project_id, timeline_id, body, **kwargs)

    monkeypatch.setattr(generated, "commit_timeline", lose_copy_response)
    with pytest.raises(_CopyOutcomeUnknown) as info:
        timeline.duplicate(as_="variant-e14r1-lost")
    exc = info.value
    assert exc.outcome == "unknown"
    assert exc.headline == UNKNOWN_HEADLINE
    assert exc.detail == UNKNOWN_DETAIL
    assert exc.fix == (
        "Call timeline.duplicate(...) again with the same name to confirm the saved request; "
        "the edit is not re-run blindly.")
    assert len(lost) == 1
