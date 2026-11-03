"""Bounded P4 API/engine vectors through the mounted G3 emitter."""
from __future__ import annotations

import copy
import json
from typing import Any

from astrid.sdk import timeline
from astrid.sdk.timeline.commit import _digest, bundle_from_document
from astrid.sdk.timeline.emitter import emit_document_commands
from tests.sdk.test_timeline_commit_provider_document import document_fixture
from tests.sdk.test_timeline_sync import connected as connected_fixture  # noqa: F401

NOTES = (
    "Confirm opening timing",
    "Check first clip framing",
    "Review source trim",
    "Verify dialogue level",
    "Confirm cut continuity",
    "Check transition intent",
    "Review asset provenance",
    "Confirm end timing",
    "Check caption wording",
    "Ready for review",
)


def _joined_bundle(generated: Any) -> dict[str, Any]:
    document = document_fixture(shared=False)
    document.update(project_id="p", timeline_id="t")
    document["parent_revision"]["revision_id"] = "r0"
    parent = document["parent_revision"]["payload"]
    # The original P4 edit is parent-only. Keep a complete Runtime document,
    # but remove the fixture's unrelated child closure so the fake transport's
    # real command-engine result
    # can be read back without adding an unrelated child edit.
    parent["occurrences"] = []
    document["shot_revisions"] = []
    document["internal_timeline_revisions"] = []
    document["dependency_manifest"]["shots"] = []
    document["dependency_manifest"]["internal_timelines"] = []
    for clips in (parent["clips"], parent["config"]["clips"]):
        clip = next(row for row in clips if row["id"] == "clip-a")
        clip["at"] = 0
        clip.setdefault("app", {}).setdefault("org.example", {})["p4_note"] = {}
    document["parent_revision"]["content_digest"] = _digest(parent)
    bundle = bundle_from_document(document)
    generated.bundles = {"r0": bundle}
    generated.head = {"revision_id": "r0", "publication_id": "pub0"}
    return bundle


def _open_joined(connection):
    client, generated, path = connection
    _joined_bundle(generated)
    return timeline.open("p", "t", client=client, outbox_path=path), generated


def test_p4_single_edit_uses_python_facade_g3_and_real_engine_fake_transport(connected_fixture):  # noqa: F811
    tl, generated = _open_joined(connected_fixture)
    before = tl.bundle["parent"]["clips"][0]["at"]
    assert before == 0
    for clips in (tl.bundle["parent"]["clips"], tl.bundle["parent"]["config"]["clips"]):
        next(row for row in clips if row["id"] == "clip-a")["at"] = 5

    result = tl.commit("Move clip")

    assert result["data"]["status"] == "accepted"
    canonical = result["data"]["canonical_document"]
    clip = next(row for row in canonical["parent_revision"]["payload"]["clips"] if row["id"] == "clip-a")
    mirrored = next(row for row in canonical["parent_revision"]["payload"]["config"]["clips"] if row["id"] == "clip-a")
    assert clip["at"] == 5
    assert mirrored["at"] == 5
    assert len(generated.calls) == 1
    body = json.loads(generated.calls[0][0])
    assert body["label"] == "Move clip"
    assert body["merge"] == "atomic"
    assert body["commands"][0]["op"] == "doc.patch"
    change = next(change for change in body["commands"][0]["args"]["changes"] if change["path"][-1] == "at")
    assert change["before"] == {"state": "present", "value": before}
    assert change["value"] == 5
    assert body["steps"][0]["command_ordinals"] == [0]
    assert body["steps"][0]["patch"] == [change]


def test_p4_ten_notes_are_one_python_commit_with_ten_fake_transport_steps(connected_fixture):  # noqa: F811
    tl, generated = _open_joined(connected_fixture)
    for index, value in enumerate(NOTES, 1):
        note_id = f"note-{index:02d}"
        before = copy.deepcopy(tl.bundle)
        after = copy.deepcopy(tl.bundle)
        for clips in (after["parent"]["clips"], after["parent"]["config"]["clips"]):
            clip = next(row for row in clips if row["id"] == "clip-a")
            clip["app"]["org.example"]["p4_note"][note_id] = value
        commands = emit_document_commands(before, after)
        assert len(commands) == 1
        tl.command(
            commands[0],
            label=f"Add {note_id}",
            intent="add an independently undoable review note",
            tags=["review", note_id],
            reads=[],
        )

    result = tl.commit("Add ten review notes", merge="fields", reads=[])

    assert result["data"]["status"] == "accepted"
    assert len(generated.calls) == 1
    body = json.loads(generated.calls[0][0])
    assert body["label"] == "Add ten review notes"
    assert body["merge"] == "fields"
    assert body["reads"] == []
    assert len(body["commands"]) == len(body["steps"]) == 10
    assert [step["id"] for step in body["steps"]] == [f"step-{index}" for index in range(1, 11)]
    assert [step["label"] for step in body["steps"]] == [f"Add note-{index:02d}" for index in range(1, 11)]
    assert all(step["intent"] == "add an independently undoable review note" for step in body["steps"])
    assert [step["tags"] for step in body["steps"]] == [
        ["review", f"note-{index:02d}"] for index in range(1, 11)
    ]
    assert [step["command_ordinals"] for step in body["steps"]] == [[index] for index in range(10)]
    assert [step["patch"][0]["path"][-1] for step in body["steps"]] == [
        f"note-{index:02d}" for index in range(1, 11)
    ]
    assert all(step["reads"] == [] for step in body["steps"])

    canonical = result["data"]["canonical_document"]
    clip = next(row for row in canonical["parent_revision"]["payload"]["clips"] if row["id"] == "clip-a")
    mirrored = next(row for row in canonical["parent_revision"]["payload"]["config"]["clips"] if row["id"] == "clip-a")
    notes = clip["app"]["org.example"]["p4_note"]
    assert notes == {f"note-{index:02d}": value for index, value in enumerate(NOTES, 1)}
    assert clip["at"] == 0
    assert mirrored["at"] == 0
    assert clip["asset"] == "picture"
    assert clip["track"] == "picture"
    assert result["receipt"]["command_kind"] == "timeline.commit"
