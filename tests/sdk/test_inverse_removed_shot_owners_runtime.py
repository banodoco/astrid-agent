"""Real Runtime ADD/SWAP inverse coverage for removed local shot owners."""
from __future__ import annotations

import copy

from tests.editing_conformance import test_occurrence_hold_current_runtime as runtime_case

editing = runtime_case.editing


def test_occurrence_add_or_swap_then_undo_restores_the_authenticated_baseline(editing):
    _commit_and_undo(editing)


def _commit_and_undo(editing):
    from astrid.sdk.timeline.commands import execute
    from astrid.sdk.timeline.commit import bundle_from_document
    from astrid.sdk.timeline.native_occurrence_fork import capture_occurrence_fork
    from astrid.sdk.timeline.native_occurrence_media_commands import capture_occurrence_swap_media
    from astrid.sdk.timeline.native_occurrence_shared import capture_occurrence_place_shared

    shared = capture_occurrence_place_shared(
        editing.bundle, runtime_case.OWNER, "occ-b", on=10,
    )
    shared_result = runtime_case._commit(
        editing,
        runtime_case._commit_body(editing, [shared], label="D110 shared source", key="d110-shared"),
        "d110-shared",
    )
    editing.bundle = bundle_from_document(shared_result["data"]["canonical_document"])

    fork = capture_occurrence_fork(editing.bundle, runtime_case.OWNER, "local-shot")
    fork_bundle = execute(editing.bundle, [fork]).bundle

    if editing.operation == "add":
        row = {
            "id": "clip-a", "clipType": "hold", "asset": editing.new["asset"], "track": "picture",
            "at": 2, "hold": 2, "params": None, "opaque": [None, False, {"keep": None}],
        }
        command = editing.add_media(
            fork_bundle, runtime_case.OWNER, row, editing.new,
            verified_media=[editing.new_verified],
        )
    else:
        command = capture_occurrence_swap_media(
            fork_bundle, runtime_case.TARGET, editing.old, editing.new,
            verified_media=[editing.old_verified, editing.new_verified],
        )

    operation_document = shared_result["data"]["canonical_document"]
    accepted = runtime_case._commit(
        editing,
        runtime_case._commit_body(editing, [fork, command], label="D110 edit", key="d110-edit"),
        "d110-edit",
    )
    assert accepted["data"]["canonical_document"] != operation_document
    accepted_document = accepted["data"]["canonical_document"]
    accepted_occurrences = {
        row["occurrence_id"]: row
        for row in accepted_document["parent_revision"]["payload"]["occurrences"]
    }
    operation_occurrences = {
        row["occurrence_id"]: row
        for row in operation_document["parent_revision"]["payload"]["occurrences"]
    }
    assert accepted_occurrences["occ-b"] == operation_occurrences["occ-b"]
    assert any(row["shot_id"] == operation_occurrences["occ-b"]["shot_id"]
               for row in accepted_document["shot_revisions"])

    inverse = {
        "schema_version": 1, "op": "timeline.inverse", "version": "1", "select": None,
        "args": {"publications": [accepted["receipt"]["receipt_id"]]},
    }
    undone = runtime_case._commit(
        editing,
        runtime_case._commit_body(editing, [inverse], label="D110 undo", key="d110-undo"),
        "d110-undo",
    )
    assert undone["data"]["canonical_document"] == operation_document

    restored = undone["data"]["canonical_document"]
    assert restored["parent_revision"]["payload"]["occurrences"] == (
        operation_document["parent_revision"]["payload"]["occurrences"]
    )
    assert restored["shot_revisions"] == operation_document["shot_revisions"]
    assert restored["internal_timeline_revisions"] == operation_document["internal_timeline_revisions"]
    assert restored["dependency_manifest"] == operation_document["dependency_manifest"]
    history = editing.service.list_timeline_commits(
        editing.project, "film", authorize=lambda: None,
    )
    assert [entry["publication_id"] for entry in history["items"][-2:]] == [
        accepted["receipt"]["receipt_id"], undone["receipt"]["receipt_id"]]
    assert copy.deepcopy(history["head"]) == {
        "revision_id": undone["data"]["accepted_revision_id"],
        "publication_id": undone["receipt"]["receipt_id"],
    }
