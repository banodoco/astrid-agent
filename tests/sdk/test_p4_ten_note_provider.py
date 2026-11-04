"""P4's ten-note commit and guarded per-step undo through the Python provider."""
from __future__ import annotations

import copy
import json
from pathlib import Path
from typing import Any

import pytest

from astrid.sdk.timeline.commit import (
    _digest,
    bundle_from_document,
    create_runtime_commit_service,
)
from astrid.sdk.timeline.emitter import emit_document_commands
from tests.sdk.test_timeline_commit_provider import (
    CommitRejected,
    DetachedPort,
    DetachedRealm,
    detached,
    inverse,
)
from tests.sdk.test_timeline_commit_provider import runtime_abi as runtime_abi
from tests.sdk.test_timeline_commit_provider_document import document_fixture

ROOT = Path(__file__).parents[2]
CASES = ROOT / "tests" / "sdk" / "fixtures" / "p4_commit_cases" / "cases.json"


def _notes() -> list[dict[str, Any]]:
    data = json.loads(CASES.read_text(encoding="utf-8"))
    case = next(case for case in data["cases"] if case["id"] == "ten-notes-one-commit")
    return case["notes"]


NOTES = _notes()
NOTE_IDS = [note["id"] for note in NOTES]
NOTE_VALUES = {note["id"]: note["text"] for note in NOTES}
NOTE_PATH_PREFIX = ["clips", "id:clip-a", "app", "org.example", "p4_note"]


def _reload(value: Any) -> Any:
    """Use the existing JSON boundary without implying Runtime persistence."""
    return json.loads(json.dumps(value, allow_nan=False))


def _seed_document() -> dict[str, Any]:
    document = document_fixture()
    payload = document["parent_revision"]["payload"]
    for clips in (payload["clips"], payload["config"]["clips"]):
        clips[0]["app"]["org.example"]["p4_note"] = {}
    document["parent_revision"]["content_digest"] = _digest(payload)
    return _reload(document)


def _set_note(bundle: dict[str, Any], note_id: str, value: Any) -> None:
    for clips in (bundle["parent"]["clips"], bundle["parent"]["config"]["clips"]):
        clips[0]["app"]["org.example"]["p4_note"][note_id] = value


def _note_values(document: dict[str, Any]) -> dict[str, Any]:
    payload = document["parent_revision"]["payload"]
    clip = payload["clips"][0]
    notes = copy.deepcopy(clip["app"]["org.example"]["p4_note"])
    assert payload["config"]["clips"][0]["app"]["org.example"]["p4_note"] == notes
    return notes


def _submit(realm: DetachedRealm, calls: list[dict[str, Any]], *, steps=None):
    context = realm.context(calls)
    context["request"].update({
        "label": "Add ten review notes",
        "merge": "fields",
        "reads": [],
        "steps": steps,
    })
    context["invocation"]["body"] = copy.deepcopy(context["request"])
    context["request_hash"] = _digest(context["invocation"])
    return create_runtime_commit_service().commit(context, DetachedPort(realm, context))


def _step(note: dict[str, Any], ordinal: int, command: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": note["id"],
        "label": note["id"],
        "intent": note["text"],
        "tags": ["review"],
        "command_ordinals": [ordinal],
        "patch": copy.deepcopy(command["args"]["changes"]),
    }


def _ten_note_commit() -> tuple[DetachedRealm, dict[str, Any], dict[str, Any]]:
    original = _seed_document()
    realm = DetachedRealm(_reload(original))
    bundle = bundle_from_document(realm.documents[realm.head["revision_id"]])
    calls: list[dict[str, object]] = []
    steps: list[dict[str, object]] = []
    for ordinal, note in enumerate(NOTES):
        before = copy.deepcopy(bundle)
        after = copy.deepcopy(bundle)
        _set_note(after, note["id"], note["text"])
        emitted = emit_document_commands(before, after)
        assert len(emitted) == 1
        call = emitted[0]
        assert call["args"]["changes"][0]["path"] == [*NOTE_PATH_PREFIX, note["id"]]
        assert call["args"]["changes"][0]["before"] == {"state": "absent"}
        calls.append(call)
        steps.append(_step(note, ordinal, call))
        bundle = after

    assert len(calls) == len(steps) == 10
    result = _submit(realm, calls, steps=steps)
    assert result["data"]["status"] == "accepted"
    return realm, original, result


def _without_notes(document: dict[str, Any]) -> dict[str, Any]:
    value = copy.deepcopy(document["parent_revision"]["payload"])
    for clips in (value["clips"], value["config"]["clips"]):
        clips[0]["app"]["org.example"].pop("p4_note", None)
    return value


def _assert_note_only_change(document: dict[str, Any], original: dict[str, Any]) -> None:
    assert _without_notes(document) == _without_notes(original)
    for key in ("shot_revisions", "internal_timeline_revisions", "dependency_manifest"):
        assert document[key] == original[key]


def _assert_original_history(realm: DetachedRealm, original_entry: dict[str, Any]) -> None:
    assert realm.entries[1] == original_entry
    assert [entry["publication_id"] for entry in realm.entries].count(original_entry["publication_id"]) == 1


@pytest.mark.parametrize("selected_note", NOTE_IDS)
def test_ten_note_commit_independently_undoes_each_step_and_redoes(selected_note: str) -> None:
    realm, original, landed = _ten_note_commit()
    original_entry = detached(realm.entries[-1])
    stored_steps = original_entry["records"]["steps"]
    assert original_entry["records"]["label"] == "Add ten review notes"
    assert original_entry["records"]["merge_mode"] == "fields"
    assert len(original_entry["records"]["commands"]) == 10
    assert len(stored_steps) == 10
    assert [step["id"] for step in stored_steps] == NOTE_IDS
    assert [step["label"] for step in stored_steps] == NOTE_IDS
    assert [step["intent"] for step in stored_steps] == [note["text"] for note in NOTES]
    assert [step["tags"] for step in stored_steps] == [["review"]] * 10
    assert [step["command_ordinals"] for step in stored_steps] == [[index] for index in range(10)]
    assert all(step["patch"] and step["dependencies"] for step in stored_steps)
    assert [step["patch"][0]["path"][-1] for step in stored_steps] == NOTE_IDS
    assert [step["patch"][0]["value"] for step in stored_steps] == [note["text"] for note in NOTES]
    assert _note_values(landed["data"]["canonical_document"]) == NOTE_VALUES

    original_record_count = len(realm.entries)
    undo_call = inverse([landed["receipt"]["receipt_id"]])
    undo_call["args"]["steps"] = {landed["receipt"]["receipt_id"]: [selected_note]}
    undo_result = realm.call([undo_call])
    assert undo_result["data"]["status"] == "accepted"
    # The provider stores the authenticated selection, not a caller-supplied inverse patch.
    assert realm.prepared[-1]["records"]["undo_steps"] == {
        landed["receipt"]["receipt_id"]: [selected_note]
    }
    expected_remaining = {note_id: text for note_id, text in NOTE_VALUES.items() if note_id != selected_note}
    undone_document = undo_result["data"]["canonical_document"]
    assert _note_values(undone_document) == expected_remaining
    _assert_note_only_change(undone_document, original)
    assert len(realm.entries) == original_record_count + 1
    assert len(realm.accepted) == 2
    _assert_original_history(realm, original_entry)

    reloaded_undo = _reload(undone_document)
    assert reloaded_undo == undone_document
    redo_result = realm.call([inverse([undo_result["receipt"]["receipt_id"]])])
    assert redo_result["data"]["status"] == "accepted"
    assert _note_values(redo_result["data"]["canonical_document"]) == NOTE_VALUES
    _assert_note_only_change(redo_result["data"]["canonical_document"], original)
    assert len(realm.accepted) == 3
    _assert_original_history(realm, original_entry)


def _emitted_note_change(realm: DetachedRealm, note_id: str, value: Any) -> dict[str, Any]:
    before = bundle_from_document(realm.documents[realm.head["revision_id"]])
    after = copy.deepcopy(before)
    _set_note(after, note_id, value)
    commands = emit_document_commands(before, after)
    assert len(commands) == 1
    return commands[0]


def test_same_note_later_write_aba_refuses_without_partial_acceptance() -> None:
    realm, _original, landed = _ten_note_commit()
    original_entry = detached(realm.entries[-1])
    note_id = "note-04"
    realm.call([_emitted_note_change(realm, note_id, "Peer reviewer owns this note")])
    realm.call([_emitted_note_change(realm, note_id, NOTE_VALUES[note_id])])
    before = (detached(realm.head), detached(realm.entries), detached(realm.documents))

    with pytest.raises(CommitRejected) as error:
        realm.call([inverse([landed["receipt"]["receipt_id"]])])

    assert error.value.failure["details"]["reason"] in {"intervening_write", "inverse_conflict"}
    assert (realm.head, realm.entries, realm.documents) == before
    assert realm.entries[1] == original_entry
    assert len(realm.accepted) == 3
