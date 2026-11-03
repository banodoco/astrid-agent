"""Exact shared-source inverses through the real Runtime commit provider."""
from __future__ import annotations

import copy
from types import SimpleNamespace

import pytest

from tests.editing_conformance.test_commit_runtime_identity import occurrence_image
from tests.helpers.runtime import use_runtime

pytestmark = pytest.mark.integration


@pytest.fixture(params=[3, 1], ids=["original-third", "authored-first"])
def shared_editing(request, tmp_path, monkeypatch):
    runtime = use_runtime()
    monkeypatch.syspath_prepend(str(runtime / "tests"))
    from runtime_protocol.service import RuntimeService
    from runtime_protocol.timeline_commit_provider import load_timeline_commit_provider
    from test_timeline_commit_integration import Editing
    from timeline_commit_film_fixture import build_film_fixture

    from astrid.sdk.timeline._shot_commands import capture_shot_set
    from astrid.sdk.timeline.capture import capture_move
    from astrid.sdk.timeline.commands import execute
    from astrid.sdk.timeline.commit import (
        _digest,
        _manifest,
        bundle_from_document,
        document_from_bundle,
    )
    from astrid.sdk.timeline.emitter import _diff, emit_document_commands
    from astrid.sdk.timeline.native_occurrence_shared import capture_occurrence_place_shared

    fixture = build_film_fixture(tmp_path)
    service = RuntimeService(fixture.duplicate_root)
    edit = Editing(service, SimpleNamespace(fixture=fixture),
                   SimpleNamespace(move=capture_move, bundle=bundle_from_document))

    def submit_setup(commands, key):
        body = edit.body(commands)
        body["reads"] = [{"kind": "scope", "ref": "timeline", "path": []}]
        body["steps"] = [{
            "id": str(ordinal), "label": body["label"], "intent": "Fixture setup",
            "tags": ["integration"], "command_ordinals": [ordinal], "reads": [],
            "patch": copy.deepcopy(command["args"]["changes"])
            if command["op"] == "doc.patch" else [],
        } for ordinal, command in enumerate(commands)]
        return edit.commit(body, key)["data"]["canonical_document"]

    try:
        original = edit.document()
        service.register_timeline_commit_provider(load_timeline_commit_provider("astrid"))
        index = request.param
        original_bundle = bundle_from_document(original)
        setup_commands = []
        staged = original
        if index == 1:
            _, source_shot, _ = occurrence_image(original, "occurrence-1")
            shot_set = capture_shot_set(
                original_bundle, "occurrence:occurrence-1/shot",
                values={"name": source_shot["payload"]["metadata"]["title"]},
            )
            # Sharing captures an unchanged published source, after preparation.
            staged = submit_setup([shot_set], "shared-inverse-source")
        if index in (1, 3):
            host = copy.deepcopy(staged)
            host_occurrence = next(
                occurrence for occurrence in host["parent_revision"]["payload"]["occurrences"]
                if occurrence["occurrence_id"] == f"occurrence-{index}"
            )
            # Setup-only host windows contain the complete unchanged move/ABA/
            # sibling sequence; authored clip timing remains 1.8 seconds.
            host_occurrence["duration_ms"] = 9800 if index == 3 else 5800
            host_parent = host["parent_revision"]["payload"]
            host["parent_revision"]["content_digest"] = _digest(host_parent)
            staged_bundle = bundle_from_document(staged)
            host_bundle = copy.deepcopy(staged_bundle)
            host_bundle["parent"] = bundle_from_document(host)["parent"]
            placement_changes = []
            _diff(staged["parent_revision"]["payload"]["occurrences"],
                  host["parent_revision"]["payload"]["occurrences"],
                  "parent", ("occurrences",), placement_changes)
            if placement_changes:
                setup_commands.append({
                    "schema_version": 1, "op": "doc.patch", "version": "1", "select": None,
                    "args": {"changes": placement_changes},
                })
            setup_commands.extend(emit_document_commands(
                staged_bundle, host_bundle))
            staged = host
        staged_bundle = bundle_from_document(staged)
        shared = capture_occurrence_place_shared(
            staged_bundle, f"occurrence:occurrence-{index}", "repeated-third",
            on=13 if index == 1 else 6,
        )
        setup_commands.append(shared)
        staged = document_from_bundle(execute(staged_bundle, [shared]).bundle)
        target = copy.deepcopy(staged)
        parent = copy.deepcopy(staged["parent_revision"]["payload"])
        if index == 1:
            _, authored_shot, authored_internal = occurrence_image(target, "occurrence-1")
            authored_shot["payload"].update(
                unknown={"literal": [None, False, 0, {}, []]},
                items=[{
                    "item_id": "shared-source-item", "media_id": fixture.media_ids[0],
                    "metadata": {"literal": "shared-source-item", "keep": [None, False]},
                }],
            )
            authored_internal["payload"].update(
                source_window={"start": 4, "end": 9},
                unknown={"literal": [None, False, 0, {}, []]},
            )
            authored_internal["payload"]["clips"][0]["extension"] = {
                "literal": "shared-source-item", "clear": None,
            }
            for ordinal, occurrence in enumerate(
                    occurrence for occurrence in parent["occurrences"][1:]
                    if occurrence["occurrence_id"] != "repeated-third"):
                occurrence["placement"]["start_ms"] = 8000 + ordinal * 2000
            parent["unknown"] = {"literal": [None, False, 0, {}, []]}
        target["parent_revision"]["payload"] = parent
        target["parent_revision"]["content_digest"] = _digest(parent)
        for image in target["shot_revisions"]:
            image["content_digest"] = _digest(image["payload"])
        for image in target["internal_timeline_revisions"]:
            image["content_digest"] = _digest(image["payload"])
        target["dependency_manifest"] = _manifest(
            target["shot_revisions"], target["internal_timeline_revisions"],
            target["dependency_manifest"]["media"], target["dependency_manifest"]["documents"],
        )
        # Diff physical payloads once; reopening shared children remaps new item
        # IDs separately for each occurrence, outside the original item maps.
        payload_changes = []
        _diff(staged["parent_revision"]["payload"], target["parent_revision"]["payload"],
              "parent", (), payload_changes)
        for key, prefix in (("shot_revisions", "shot-revision:"),
                            ("internal_timeline_revisions", "internal-revision:")):
            for before, after in zip(staged[key], target[key]):
                _diff(before["payload"], after["payload"], prefix + before["revision_id"],
                      (), payload_changes)
        if payload_changes:
            setup_commands.append({
                "schema_version": 1, "op": "doc.patch", "version": "1", "select": None,
                "args": {"changes": payload_changes}, "created": [],
            })

        submit_setup(setup_commands, "shared-inverse-baseline")

        def unavailable(*args, **kwargs):
            raise AssertionError("save evaluated a scene")

        import runtime_protocol.service as service_module
        import runtime_protocol.visual_seam as visual_seam
        monkeypatch.setattr(service_module, "evaluate_closure", unavailable)
        monkeypatch.setattr(visual_seam, "evaluate_closure", unavailable)
        yield edit, index
    finally:
        edit.service.close()


def test_shared_slot_move_inverse_redo_and_rematerialization(shared_editing):
    from test_timeline_commit_integration import inverse

    edit, index = shared_editing
    original = edit.document()
    baseline_history = copy.deepcopy(edit.history())
    baseline_closures = [edit.document(entry["revision_id"]) for entry in baseline_history]
    transcript = edit.service.get_document(edit.project, edit.film.fixture.transcript_document_id)
    binding = edit.service.get_project_shot_text_binding(edit.project, edit.film.fixture.text_binding_id)
    owner = f"occurrence:occurrence-{index}/clip:clip-{index}"
    move = edit.agent.move(edit.agent.bundle(original), owner, 3.5)
    first = edit.commit(edit.body([move]), "shared-first-move")
    edit.assert_readback(first)
    undone = edit.commit(edit.body([inverse(first["receipt"]["receipt_id"])]), "shared-undo")
    assert undone["data"]["canonical_document"] == original
    redone = edit.commit(edit.body([inverse(undone["receipt"]["receipt_id"])]), "shared-redo")
    assert redone["data"]["canonical_document"] == first["data"]["canonical_document"]
    undone_again = edit.commit(edit.body([inverse(redone["receipt"]["receipt_id"])]), "shared-undo-again")
    assert undone_again["data"]["canonical_document"] == original
    move = edit.agent.move(edit.agent.bundle(edit.document()), owner, 3.6)
    rematerialized = edit.commit(edit.body([move]), "shared-rematerialize")
    assert {s["shot_id"] for s in rematerialized["data"]["canonical_document"]["shot_revisions"]} == {
        s["shot_id"] for s in first["data"]["canonical_document"]["shot_revisions"]}
    for accepted in (first, undone, redone, undone_again, rematerialized):
        edit.assert_readback(accepted)
        for sibling in [*(f"occurrence-{n}" for n in range(1, 4) if n != index), "repeated-third"]:
            assert occurrence_image(accepted["data"]["canonical_document"], sibling) == occurrence_image(original, sibling)
    history = edit.history()
    assert history[:len(baseline_history)] == baseline_history
    assert [edit.document(entry["revision_id"]) for entry in baseline_history] == baseline_closures
    accepted_results = (first, undone, redone, undone_again, rematerialized)
    assert len(history) == len(baseline_history) + len(accepted_results)
    for entry, accepted in zip(history[len(baseline_history):], accepted_results):
        assert entry["receipt"] == accepted["receipt"]
        assert entry["revision_id"] == accepted["data"]["accepted_revision_id"]
        assert edit.document(entry["revision_id"]) == accepted["data"]["canonical_document"]
    assert edit.service.get_document(edit.project, edit.film.fixture.transcript_document_id) == transcript
    assert edit.service.get_project_shot_text_binding(edit.project, edit.film.fixture.text_binding_id) == binding


def test_shared_inverse_preserves_newer_disjoint_parent_edit(shared_editing):
    from test_timeline_commit_integration import inverse

    edit, index = shared_editing
    original = edit.document()
    owner = f"occurrence:occurrence-{index}/clip:clip-{index}"
    moved = edit.commit(edit.body([edit.agent.move(edit.agent.bundle(original), owner, 3.5)]), "shared-move")
    parent_move = edit.agent.move(edit.agent.bundle(edit.document()), "parent/clip:music-bed", 1)
    disjoint = edit.commit(edit.body([parent_move]), "disjoint-parent-move")
    before = copy.deepcopy(edit.history())
    undone = edit.commit(edit.body([inverse(moved["receipt"]["receipt_id"])]), "shared-disjoint-undo")
    document = undone["data"]["canonical_document"]
    expected_parent = copy.deepcopy(disjoint["data"]["canonical_document"]["parent_revision"]["payload"])
    expected_parent["occurrences"] = original["parent_revision"]["payload"]["occurrences"]
    assert document["parent_revision"]["payload"] == expected_parent
    for key in ("shot_revisions", "internal_timeline_revisions", "dependency_manifest"):
        assert document[key] == original[key]
    assert edit.history()[:-1] == before
    for accepted in (moved, disjoint, undone):
        edit.assert_readback(accepted)


def test_shared_inverse_preserves_newer_disjoint_sibling_edit(shared_editing):
    from test_timeline_commit_integration import inverse

    edit, index = shared_editing
    original = edit.document()
    owner = f"occurrence:occurrence-{index}/clip:clip-{index}"
    moved = edit.commit(edit.body([edit.agent.move(edit.agent.bundle(original), owner, 3.5)]), "shared-move")
    sibling = f"occurrence:repeated-third/clip:clip-{index}"
    disjoint = edit.commit(edit.body([edit.agent.move(edit.agent.bundle(edit.document()), sibling, 14)]),
                           "disjoint-sibling-move")
    before = copy.deepcopy(edit.history())
    undone = edit.commit(edit.body([inverse(moved["receipt"]["receipt_id"])]), "shared-sibling-undo")
    document = undone["data"]["canonical_document"]
    assert occurrence_image(document, f"occurrence-{index}") == occurrence_image(original, f"occurrence-{index}")
    assert occurrence_image(document, "repeated-third") == occurrence_image(
        disjoint["data"]["canonical_document"], "repeated-third")
    assert edit.history()[:-1] == before
    for accepted in (moved, disjoint, undone):
        edit.assert_readback(accepted)


def test_shared_inverse_refuses_touching_aba_atomically(shared_editing):
    from runtime_protocol.errors import RuntimeErrorBase
    from test_timeline_commit_integration import inverse, rows

    edit, index = shared_editing
    owner = f"occurrence:occurrence-{index}/clip:clip-{index}"
    moved = edit.commit(edit.body([edit.agent.move(edit.agent.bundle(edit.document()), owner, 3.5)]), "shared-move")
    for ordinal, on in enumerate((4, 3.5)):
        edit.commit(edit.body([edit.agent.move(edit.agent.bundle(edit.document()), owner, on)]), f"aba-{ordinal}")
    document, history, stored = edit.document(), edit.history(), rows(edit.service)
    with pytest.raises(RuntimeErrorBase) as error:
        edit.commit(edit.body([inverse(moved["receipt"]["receipt_id"])]), "shared-aba-undo")
    assert error.value.details["reason"] == "intervening_write"
    assert edit.document() == document
    assert edit.history() == history
    assert rows(edit.service) == stored


def test_shared_redo_refuses_intervening_aba_after_exact_undo(shared_editing):
    from runtime_protocol.errors import RuntimeErrorBase
    from test_timeline_commit_integration import inverse, rows

    edit, index = shared_editing
    original = edit.document()
    owner = f"occurrence:occurrence-{index}/clip:clip-{index}"
    moved = edit.commit(edit.body([edit.agent.move(edit.agent.bundle(original), owner, 3.5)]), "shared-move")
    undone = edit.commit(edit.body([inverse(moved["receipt"]["receipt_id"])]), "shared-undo")
    assert undone["data"]["canonical_document"] == original
    touched = edit.commit(edit.body([edit.agent.move(edit.agent.bundle(edit.document()), owner, 3.6)]),
                          "shared-touch")
    restored = edit.commit(edit.body([inverse(touched["receipt"]["receipt_id"])]), "shared-touch-undo")
    assert restored["data"]["canonical_document"] == original
    history, stored = edit.history(), rows(edit.service)
    with pytest.raises(RuntimeErrorBase) as error:
        edit.commit(edit.body([inverse(undone["receipt"]["receipt_id"])]), "shared-aba-redo")
    assert error.value.details["reason"] == "intervening_write"
    assert edit.document() == original
    assert edit.history() == history
    assert rows(edit.service) == stored
    for accepted in (moved, undone, touched, restored):
        edit.assert_readback(accepted)
