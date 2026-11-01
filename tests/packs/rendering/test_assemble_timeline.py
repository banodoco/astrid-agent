from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

from astrid.packs.rendering.actions.assemble_timeline.run import (
    TimelineAuthoringError,
    build_authoring_proposal,
    derive_clip_id,
    main,
)

DIGEST = "sha256:" + "a" * 64


def _payload() -> dict:
    return {
        "continuation_id": "stitch-task-42",
        "spec": {
            "family": "stitch_finalization",
            "runtime_dependencies": {
                "edges": [
                    {"from_task_id": "child-1", "requires_event": "task.succeeded", "fence": "runtime_task"},
                    {"from_task_id": "child-2", "requires_event": "task.succeeded", "fence": "runtime_task"},
                ],
                "aggregation": {
                    "kind": "ordered_cas_inputs",
                    "order": ["cas-shared", "cas-shared"],
                },
            },
        },
        # Deliberately reversed: ordinal and the Runtime aggregation define order.
        "resolved_children": [
            {
                "task_id": "child-2",
                "ordinal": 1,
                "outputs": [{"object_id": "cas-shared", "media_id": "media-shared", "role": "primary_visual", "is_primary": True, "content_sha256": DIGEST, "media_type": "video/mp4", "duration": 2.5}],
            },
            {
                "task_id": "child-1",
                "ordinal": 0,
                "outputs": [{"object_id": "cas-shared", "media_id": "media-shared", "role": "primary_visual", "is_primary": True, "content_sha256": DIGEST, "media_type": "video/mp4", "duration": 1.5}],
            },
        ],
    }


def test_builds_runtime_ordered_two_child_canonical_timeline_and_preserves_duplicate_refs():
    proposal = build_authoring_proposal(_payload())
    clips = proposal["timeline"]["clips"]
    assert [clip["at"] for clip in clips] == [0.0, 1.5]
    assert clips[0]["id"] == derive_clip_id("stitch-task-42", 0)
    assert clips[1]["id"] == derive_clip_id("stitch-task-42", 1)
    assert clips[0]["id"] != clips[1]["id"]
    assert clips[0]["asset"] != clips[1]["asset"]
    assert [row["object_id"] for row in proposal["children"]] == ["cas-shared", "cas-shared"]
    assert proposal["publication"]["authority"] == "workspace_runtime"


def test_accepts_live_runtime_claim_envelope_without_aggregation_order():
    """The Runtime claim nests resolved children and materializes CAS order."""
    def digest(value: str) -> str:
        return "sha256:" + hashlib.sha256(value.encode()).hexdigest()
    first, second = digest("first"), digest("second")
    payload = {
        "attempt_id": "attempt-author",
        "task_id": "author-task",
        "capability_id": "rendering.assemble_timeline",
        "input_object_ids": [first, second],
        "spec": {
            "input_object_ids": [first, second],
            "schema_version": "1",
            "capability_digest": digest("assemble"),
            "spec": {
                "runtime_dependencies": {
                    "edges": [
                        {"from_task_id": "child-1", "to": "self", "requires_event": "task.succeeded", "fence": "runtime_task"},
                        {"from_task_id": "child-2", "to": "self", "requires_event": "task.succeeded", "fence": "runtime_task"},
                    ],
                    "aggregation": {"kind": "ordered_cas_inputs"},
                    "resolved_children": [
                        {"task_id": "child-2", "ordinal": 1, "outputs": [{"digest": second, "media_type": "video/mp4", "name": "primary", "role": "primary", "is_primary": True, "duration_seconds": 2}]},
                        {"task_id": "child-1", "ordinal": 0, "outputs": [{"digest": first, "media_type": "video/mp4", "name": "primary", "role": "primary", "is_primary": True, "duration_seconds": 1}]},
                    ],
                }
            },
        },
    }

    proposal = build_authoring_proposal(payload)
    assert proposal["continuation_id"] == "author-task"
    assert [clip["at"] for clip in proposal["timeline"]["clips"]] == [0.0, 1.0]
    assert [row["object_id"] for row in proposal["children"]] == [first, second]


@pytest.mark.parametrize(
    ("edit", "message"),
    [
        (lambda p: p["spec"].update({"family": "other"}), "unsupported continuation graph family"),
        (lambda p: p["spec"]["runtime_dependencies"]["edges"].pop(), "exactly two dependency edges"),
        (lambda p: p["spec"]["runtime_dependencies"]["edges"][0].update({"requires_event": "task.running"}), "must require task.succeeded"),
        (lambda p: p["spec"]["runtime_dependencies"]["aggregation"].update({"kind": "ordered_children"}), "unsupported continuation aggregation"),
        (lambda p: p["resolved_children"][0]["outputs"][0].update({"media_type": "audio/wav"}), "unsupported media type"),
    ],
)
def test_rejects_malformed_ambiguous_or_unsupported_continuations(edit, message):
    payload = _payload()
    edit(payload)
    with pytest.raises(TimelineAuthoringError, match=message):
        build_authoring_proposal(payload)


def test_rejects_ambiguous_primary_and_mismatched_aggregation():
    payload = _payload()
    payload["resolved_children"][0]["outputs"].append({"object_id": "cas-other", "role": "primary_visual", "content_sha256": DIGEST, "media_type": "video/mp4", "duration": 1})
    with pytest.raises(TimelineAuthoringError, match="exactly one primary_visual"):
        build_authoring_proposal(payload)

    payload = _payload()
    payload["spec"]["runtime_dependencies"]["aggregation"]["order"][1] = "cas-other"
    with pytest.raises(TimelineAuthoringError, match="does not match ordered_cas_inputs"):
        build_authoring_proposal(payload)


def test_cli_writes_timeline_registry_proposal_and_manifest(tmp_path: Path):
    source = tmp_path / "continuation.json"
    source.write_text(json.dumps(_payload()), encoding="utf-8")
    out = tmp_path / "out"
    assert main(["--resolved-children", str(source), "--out", str(out)]) == 0
    assert json.loads((out / "timeline.json").read_text()) ["clips"][1]["at"] == 1.5
    manifest = json.loads((out / "manifest.json").read_text())
    assert manifest["kind"] == "rendering.assemble_timeline"
    assert {entry["path"] for entry in manifest["outputs"]} == {"timeline.json", "assets.json", "authoring-proposal.json"}
