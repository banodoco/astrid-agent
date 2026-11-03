"""P4's collected note-04 conflict and public callback rerun case."""
from __future__ import annotations

import copy
import json
from pathlib import Path
from typing import Any

from astrid.sdk import timeline
from astrid.sdk.timeline.commit import _digest, bundle_from_document
from astrid_runtime_client import ApiError
from tests.sdk.test_timeline_commit_provider_document import document_fixture
from tests.sdk.test_timeline_sync import connected as connected_fixture  # noqa: F401

ROOT = Path(__file__).parents[2]
CASES = ROOT / "tests/sdk/fixtures/p4_commit_cases/cases.json"
NOTE_PATH = ["clips", "id:clip-a", "app", "org.example", "p4_note", "note-04"]
SUFFIX = " — reviewed"


def _case() -> dict[str, Any]:
    data = json.loads(CASES.read_text(encoding="utf-8"))
    return next(case for case in data["cases"] if case["id"] == "ten-notes-one-commit")


def _notes() -> dict[str, str]:
    return {note["id"]: note["text"] for note in _case()["notes"]}


def _set_note(bundle: dict[str, Any], value: str) -> None:
    for clips in (bundle["parent"]["clips"], bundle["parent"]["config"]["clips"]):
        clip = next(row for row in clips if row["id"] == "clip-a")
        clip["app"]["org.example"].setdefault("p4_note", {})["note-04"] = value


def _note(bundle: dict[str, Any]) -> str | None:
    values = []
    for clips in (bundle["parent"]["clips"], bundle["parent"]["config"]["clips"]):
        clip = next(row for row in clips if row["id"] == "clip-a")
        values.append(clip["app"]["org.example"].get("p4_note", {}).get("note-04"))
    assert values[0] == values[1]
    return values[0]


def _seed_document() -> dict[str, Any]:
    case_notes = _notes()
    document = document_fixture(shared=True)
    document.update(project_id="p", timeline_id="t")
    document["parent_revision"]["revision_id"] = "r0"
    payload = document["parent_revision"]["payload"]
    original_notes = {note_id: value for note_id, value in case_notes.items() if note_id != "note-04"}
    for clips in (payload["clips"], payload["config"]["clips"]):
        clip = next(row for row in clips if row["id"] == "clip-a")
        clip["app"]["org.example"]["p4_note"] = copy.deepcopy(original_notes)
    document["parent_revision"]["content_digest"] = _digest(payload)
    return document


def _open_real_document(connected_fixture) -> tuple[Any, Any, dict[str, Any]]:  # noqa: F811
    client, generated, path = connected_fixture
    document = _seed_document()
    generated.bundles = {"r0": bundle_from_document(document)}
    generated.head = {"revision_id": "r0", "publication_id": "pub0"}
    generated.publications["peer-revision"] = "peer-publication"
    return timeline.open("p", "t", client=client, outbox_path=path), generated, document


def test_p4_note_conflict_reruns_public_function_from_verified_peer_document(connected_fixture, monkeypatch):  # noqa: F811
    tl, generated, original_document = _open_real_document(connected_fixture)
    client, _, path = connected_fixture
    case = _case()
    note_values = _notes()
    peer_value = case["conflict"]["peer_edit"]["value"]
    base_bundle = copy.deepcopy(generated.bundles["r0"])
    evaluations: list[tuple[dict[str, str], str | None]] = []
    sent: list[tuple[dict[str, Any], str]] = []
    original_commit = generated.commit_timeline

    def conflict_then_accept(*args, **kwargs):
        body = copy.deepcopy(args[2])
        sent.append((body, kwargs["idempotency_key"]))
        if len(sent) == 1:
            peer_bundle = copy.deepcopy(base_bundle)
            _set_note(peer_bundle, peer_value)
            generated.bundles["peer-revision"] = peer_bundle
            generated.head = {"revision_id": "peer-revision", "publication_id": "peer-publication"}
            raise ApiError(409, "conflict", "changed", details={
                "status": "conflict",
                "reason": "value_conflict",
                "code": "value_conflict",
                "path": NOTE_PATH,
                "hint": "read the returned peer head",
                "suggested_command": "timeline show",
                "current": copy.deepcopy(generated.head),
            })
        return original_commit(*args, **kwargs)

    monkeypatch.setattr(generated, "commit_timeline", conflict_then_accept)

    def append_review_suffix(working):
        current = _note(working.bundle)
        evaluations.append((working.head, current))
        source = current if current is not None else note_values["note-04"]
        _set_note(working.bundle, source + SUFFIX)

    result = tl.commit(append_review_suffix, idempotency_key="p4-note-original")

    assert len(evaluations) == 2
    assert [head["revision_id"] for head, _ in evaluations] == ["r0", "peer-revision"]
    assert [value for _, value in evaluations] == [None, peer_value]
    assert [key for _, key in sent][0] == "p4-note-original"
    assert sent[1][1] != sent[0][1]
    assert [body["base_revision_id"] for body, _ in sent] == ["r0", "peer-revision"]
    assert [body["base_publication_id"] for body, _ in sent] == ["pub0", "peer-publication"]
    assert client.reads == ["r0", "r0", "peer-revision", "peer-revision"]

    first_body, second_body = (body for body, _ in sent)
    assert first_body["api_version"] == second_body["api_version"] == 1
    assert first_body["project_id"] == second_body["project_id"] == "p"
    assert first_body["timeline_id"] == second_body["timeline_id"] == "t"
    assert first_body["label"] == second_body["label"] == "append_review_suffix"
    assert first_body["merge"] == second_body["merge"] == "atomic"
    assert "reads" not in first_body and "reads" not in second_body
    assert first_body["source"] == second_body["source"] == {"via": "python"}
    assert first_body["dry_run"] is second_body["dry_run"] is False

    first_change = first_body["commands"][0]["args"]["changes"]
    second_change = second_body["commands"][0]["args"]["changes"]
    assert first_body["commands"][0]["op"] == second_body["commands"][0]["op"] == "doc.patch"
    assert first_change == [{
        "op": "set", "ref": "parent", "path": NOTE_PATH,
        "before": {"state": "absent"}, "value": note_values["note-04"] + SUFFIX,
    }]
    assert second_change == [{
        "op": "set", "ref": "parent", "path": NOTE_PATH,
        "before": {"state": "present", "value": peer_value}, "value": peer_value + SUFFIX,
    }]
    for body, change in ((first_body, first_change), (second_body, second_change)):
        assert body["steps"] == [{
            "id": "step-1", "label": "Edit document", "intent": "", "tags": [],
            "command_ordinals": [0], "patch": change,
            "reads": [{"kind": "scope", "ref": "timeline", "path": []}],
        }]

    assert result["data"]["status"] == "accepted"
    assert len(generated.accepted) == 1
    canonical = result["data"]["canonical_document"]
    expected_bundle = copy.deepcopy(generated.bundles["peer-revision"])
    _set_note(expected_bundle, peer_value + SUFFIX)
    landed_bundle = generated.bundles[result["data"]["accepted_revision_id"]]
    assert landed_bundle == expected_bundle
    parent = canonical["parent_revision"]["payload"]
    expected_parent = expected_bundle["parent"]
    assert {key: parent[key] for key in ("config", "clips", "registry", "name", "unknown")} == {
        key: expected_parent[key] for key in ("config", "clips", "registry", "name", "unknown")
    }
    assert _note(landed_bundle) == peer_value + SUFFIX
    assert parent["clips"][0]["app"]["org.example"]["literal"] == "clip-a"
    assert parent["clips"][0]["app"]["org.example"]["clear"] is None
    assert parent["config"]["opaque"] == {"timestamp": "authored timestamp", "v": [None, False, 0]}
    assert canonical["shot_revisions"]
    assert canonical["internal_timeline_revisions"]
    assert canonical["shot_revisions"][0]["payload"]["unknown"] == {"keep": None}
    assert canonical["internal_timeline_revisions"][0]["payload"]["unknown"] == [1, 2]
    assert canonical["dependency_manifest"]["media"] == original_document["dependency_manifest"]["media"]
    assert original_document["parent_revision"]["payload"]["unknown"] == expected_parent["unknown"]
    assert json.loads(path.read_text(encoding="utf-8"))["pending"] == []
    assert json.loads(path.read_text(encoding="utf-8"))["flight"] is None
