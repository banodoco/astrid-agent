"""P4's original ten-note case through Runtime's normal v2 HTTP boundary.

The test is source preparation for the compatible G3/R2 client and Runtime
join.  It deliberately uses the existing daemon fixture and generated
transport; no transport or provider is substituted here.
"""
from __future__ import annotations

import copy
import hashlib
import json
from pathlib import Path
from typing import Any

import pytest

from astrid.sdk import timeline
from astrid.sdk.timeline.commands import execute
from astrid.sdk.timeline.commit import _digest, _json, bundle_from_document, document_from_bundle
from astrid.sdk.timeline.emitter import emit_document_commands
from tests.editing_conformance import test_native_batch_ingress
from tests.helpers.trusted_seed import seed_trusted_parent
from tests.sdk.test_timeline_commit_provider_document import MEDIA, document_fixture

pytestmark = pytest.mark.integration
runtime = test_native_batch_ingress.runtime

ROOT = Path(__file__).parents[1]
CASES = ROOT / "sdk" / "fixtures" / "p4_commit_cases" / "cases.json"
NOTE_PATH_PREFIX = ["clips", "id:clip-a", "app", "org.example", "p4_note"]


def _case() -> dict[str, Any]:
    data = json.loads(CASES.read_text(encoding="utf-8"))
    return next(case for case in data["cases"] if case["id"] == "ten-notes-one-commit")


def _notes() -> list[dict[str, Any]]:
    return _case()["notes"]


def _original_publication(project: str, media_id: str) -> dict[str, Any]:
    """Clone the canonical shared fixture, mapping only its physical media pin.

    The note namespace is an isolated empty-container genesis setup: the
    original fixture has no note keys, while doc.patch requires the parent
    object to exist before setting each independently addressed child.
    """
    original = document_fixture(shared=True)
    source = copy.deepcopy(original)
    media_paths = [
        ("parent_revision", "payload", "registry", "assets", "picture", "media_id"),
        ("shot_revisions", 0, "payload", "items", 0, "media_id"),
        ("internal_timeline_revisions", 0, "payload", "registry", "assets", "picture", "media_id"),
    ]
    found = []

    def map_media(value: Any, path: tuple[Any, ...] = ()) -> Any:
        if isinstance(value, dict):
            mapped = {}
            for key, child in value.items():
                if key == "media_id":
                    assert child == MEDIA
                    found.append((*path, key))
                    mapped[key] = media_id
                else:
                    mapped[key] = map_media(child, (*path, key))
            return mapped
        if isinstance(value, list):
            return [map_media(child, (*path, index)) for index, child in enumerate(value)]
        return value

    for key in ("parent_revision", "shot_revisions", "internal_timeline_revisions"):
        source[key] = map_media(source[key], (key,))
    assert found == media_paths
    restored = copy.deepcopy(source)
    for path in media_paths:
        target = restored
        for part in path[:-1]:
            target = target[part]
        assert target[path[-1]] == media_id
        target[path[-1]] = MEDIA
    assert _json(restored) == _json(original)

    source["project_id"] = project
    source["timeline_id"] = "film"
    source["parent_revision"]["revision_id"] = "p4-ten-note-baseline"
    payload = source["parent_revision"]["payload"]
    for clips in (payload["clips"], payload["config"]["clips"]):
        clip = next(row for row in clips if row["id"] == "clip-a")
        clip["app"]["org.example"]["p4_note"] = {}
    for image in [source["parent_revision"], *source["shot_revisions"],
                  *source["internal_timeline_revisions"]]:
        image["content_digest"] = _digest(image["payload"])
    assert [(row["shot_id"], row["shot_revision_id"]) for row in payload["occurrences"]] == [
        ("chapter-shot", "shot-1"), ("chapter-shot", "shot-1"),
    ]
    assert len(source["shot_revisions"]) == len(source["internal_timeline_revisions"]) == 1
    return {
        "project_id": project,
        "timeline_id": "film",
        "expected_head": None,
        "parent_revision_id": "p4-ten-note-baseline",
        "parent_composition": payload,
        "shot_revisions": source["shot_revisions"],
        "internal_timeline_revisions": source["internal_timeline_revisions"],
    }


def _seed_original(runtime: Any) -> dict[str, Any]:
    """Install real bytes and the original shared closure before observing edits."""
    service = runtime.service()
    media = ROOT / "packs" / "builtin" / "generate_image" / "fixtures" / "tiny.png"
    data = media.read_bytes()
    expected = "sha256:" + hashlib.sha256(data).hexdigest()
    ingested = service.ingest(
        runtime.project, data, media_type="image/png", original_name=media.name,
        expected_digest=expected, idempotency_key="p4-ten-note-source",
    )
    managed = ingested["data"]
    assert managed["object_id"] == managed["digest"] == expected
    assert managed["relation"] == "managed"
    assert managed["size"] == len(data)
    publication = _original_publication(runtime.project, managed["object_id"])
    # Keep the original internal owner, rather than rewriting its source scope.
    for image in publication["internal_timeline_revisions"]:
        service.create_timeline(
            runtime.project, image["timeline_id"], idempotency_key="p4-ten-note-internal-owner",
        )
    seeded = seed_trusted_parent(
        service, publication, idempotency_key="p4-ten-note-baseline"
    )
    runtime.restart()
    return seeded


def _note_values(bundle: dict[str, Any]) -> dict[str, Any]:
    values = []
    for clips in (bundle["parent"]["clips"], bundle["parent"]["config"]["clips"]):
        clip = next(row for row in clips if row["id"] == "clip-a")
        values.append(copy.deepcopy(clip["app"]["org.example"].get("p4_note", {})))
    assert values[0] == values[1]
    return values[0]


def _without_notes(bundle: dict[str, Any]) -> dict[str, Any]:
    """Keep every authored field and closure image except the tested namespace."""
    value = {
        key: copy.deepcopy(bundle[key])
        for key in ("parent", "base_parent_payload", "placements", "shots")
    }
    for parent_key in ("parent", "base_parent_payload"):
        parent = value[parent_key]
        for clips in (parent["clips"], parent["config"]["clips"]):
            clip = next(row for row in clips if row["id"] == "clip-a")
            clip["app"]["org.example"].pop("p4_note", None)
    return value


def _assert_step_patch(step: dict[str, Any], note: dict[str, Any]) -> None:
    assert step["id"] == f"step-{note['ordinal'] + 1}"
    assert step["label"] == note["id"]
    assert step["intent"] == note["text"]
    assert step["tags"] == ["review"]
    assert step["command_ordinals"] == [note["ordinal"]]
    assert len(step["patch"]) == 1
    change = step["patch"][0]
    # Authoritative effects use the parent's canonical mirror path; submitted
    # operands below retain the emitter's logical clips path.
    physical_path = ["config", *NOTE_PATH_PREFIX, note["id"]]
    assert change == {
        "op": "set",
        "ref": "parent",
        "path": physical_path,
        "before": {"state": "absent"},
        "value": note["text"],
    }
    assert step["dependencies"] == [{
        "kind": "field", "ref": "parent", "path": physical_path,
        "state": {"state": "absent"},
    }]


def _run_cli(runtime: Any, capsys: Any, *args: str) -> tuple[int, dict[str, Any]]:
    from astrid.packs.timeline.cli import build_parser

    parsed = build_parser(runtime.client()).parse_args([
        *args, "--project", runtime.project, "--json",
    ])
    code = parsed.handler(parsed)
    return code, json.loads(capsys.readouterr().out)


def test_http_ten_note_commit_per_step_inverse_and_redo(runtime, tmp_path, capsys):
    seeded = _seed_original(runtime)
    opened = timeline.open(
        runtime.project, "film", client=runtime.client(),
        outbox_path=tmp_path / "ten-note-open.json",
    )
    original = copy.deepcopy(opened.bundle)
    assert _note_values(original) == {}
    assert len(original["placements"]) == 2
    assert "_runtime_document" not in original["source_mapping"]
    base_head = {
        "revision_id": seeded["data"]["accepted_revision_id"],
        "publication_id": seeded["receipt"]["receipt_id"],
    }
    assert opened.head == base_head
    base_read = runtime.client().timelines.open_bundle(
        runtime.project, "film", revision_id=base_head["revision_id"],
        publication_id=base_head["publication_id"],
    )
    assert base_read.ok
    authenticated_base = base_read.data["bundle"]
    canonical = authenticated_base["source_mapping"]["_runtime_document"]
    assert authenticated_base == {
        **original,
        "source_mapping": {**original["source_mapping"], "_runtime_document": canonical},
        "base_parent": {**original["base_parent"], "publication_id": base_head["publication_id"]},
    }
    assert base_read.data["project_id"] == runtime.project
    assert base_read.data["timeline_id"] == "film"
    assert base_read.data["revision_id"] == base_head["revision_id"]
    assert base_read.data["is_current_head"] is True
    assert canonical["parent_revision"]["revision_id"] == base_head["revision_id"]
    assert set(canonical) == {
        "schema_version", "project_id", "timeline_id", "parent_revision",
        "shot_revisions", "internal_timeline_revisions", "dependency_manifest",
    }
    assert canonical["schema_version"] == 1
    assert canonical["project_id"] == runtime.project
    assert canonical["timeline_id"] == "film"
    canonical_parent = canonical["parent_revision"]
    physical_occurrences = canonical_parent["payload"]["occurrences"]
    assert [(row["occurrence_id"], row["shot_id"], row["shot_revision_id"])
            for row in physical_occurrences] == [
        ("occ-a", "chapter-shot", "shot-1"),
        ("occ-b", "chapter-shot", "shot-1"),
    ]
    source_fixture = document_fixture(shared=True)
    source_parent = source_fixture["parent_revision"]["payload"]
    source_shot = source_fixture["shot_revisions"][0]["payload"]
    source_internal = source_fixture["internal_timeline_revisions"][0]["payload"]
    assert physical_occurrences == source_parent["occurrences"]
    assert canonical_parent["payload"]["unknown"] == source_parent["unknown"]
    assert canonical_parent["payload"]["config"]["opaque"] == source_parent["config"]["opaque"]

    physical_shots = canonical["shot_revisions"]
    physical_internals = canonical["internal_timeline_revisions"]
    assert [(row["shot_id"], row["revision_id"])
            for row in physical_shots] == [("chapter-shot", "shot-1")]
    assert [(row["timeline_id"], row["revision_id"])
            for row in physical_internals] == [("chapter-1", "internal-1")]
    physical_shot = physical_shots[0]
    physical_internal = physical_internals[0]
    assert physical_shot["internal_timeline_revision_id"] == "internal-1"
    assert physical_shot["payload"]["internal_timeline_revision_id"] == "internal-1"
    assert physical_shot["payload"]["items"][0]["item_id"] == "item-1"
    assert physical_shot["payload"]["unknown"] == source_shot["unknown"]
    assert physical_internal["payload"]["unknown"] == source_internal["unknown"]
    assert physical_internal["payload"]["source_window"] == source_internal["source_window"]
    media_refs = [
        canonical_parent["payload"]["registry"]["assets"]["picture"]["media_id"],
        physical_shot["payload"]["items"][0]["media_id"],
        physical_internal["payload"]["registry"]["assets"]["picture"]["media_id"],
    ]
    assert len(set(media_refs)) == 1
    media_id = media_refs[0]
    assert canonical["dependency_manifest"]["media"] == [
        {"media_id": media_id, "content_digest": media_id}
    ]
    assert canonical_parent["content_digest"] == _digest(canonical_parent["payload"])
    assert physical_shot["content_digest"] == _digest(physical_shot["payload"])
    assert physical_internal["content_digest"] == _digest(physical_internal["payload"])
    assert canonical["dependency_manifest"]["shots"] == [{
        "shot_id": "chapter-shot", "revision_id": "shot-1",
        "internal_timeline_revision_id": "internal-1",
        "content_digest": physical_shot["content_digest"],
    }]
    assert canonical["dependency_manifest"]["internal_timelines"] == [{
        "timeline_id": "chapter-1", "revision_id": "internal-1",
        "content_digest": physical_internal["content_digest"],
    }]

    source_mapping = original["source_mapping"]
    working_aliases = [placement["shot_id"] for placement in original["placements"]]
    assert len(working_aliases) == len(set(working_aliases)) == 2
    assert set(source_mapping["placements"]) == {
        row["occurrence_id"] for row in physical_occurrences
    }
    assert set(source_mapping["shots"]) == set(working_aliases)
    for physical_occurrence, placement in zip(physical_occurrences, original["placements"]):
        assert source_mapping["placements"][physical_occurrence["occurrence_id"]] == placement
        source = source_mapping["shots"][placement["shot_id"]]
        assert (source["shot_id"], source["revision_id"]) == (
            physical_occurrence["shot_id"], physical_occurrence["shot_revision_id"]
        )
        assert (source["internal_timeline_id"], source["internal_timeline_revision_id"]) == (
            physical_internal["timeline_id"], physical_shot["internal_timeline_revision_id"]
        )
        assert source["content_digest"] == physical_shot["content_digest"]
        assert source["internal_timeline_content_digest"] == physical_internal["content_digest"]
    assert next(row for row in original["parent"]["clips"] if row["id"] == "clip-b")["at"] == 5

    for note in _notes():
        before = copy.deepcopy(opened.bundle)
        after = copy.deepcopy(before)
        for clips in (after["parent"]["clips"], after["parent"]["config"]["clips"]):
            clip = next(row for row in clips if row["id"] == "clip-a")
            clip["app"]["org.example"]["p4_note"][note["id"]] = note["text"]
        commands = emit_document_commands(before, after)
        assert len(commands) == 1
        assert commands[0]["args"]["changes"] == [{
            "op": "set", "ref": "parent", "path": [*NOTE_PATH_PREFIX, note["id"]],
            "before": {"state": "absent"}, "value": note["text"],
        }]
        opened.command(
            commands[0], label=note["id"], intent=note["text"], tags=["review"], reads=[]
        )

    result = opened.commit("Add ten review notes", merge="fields", reads=[],
                           idempotency_key="p4-ten-note-commit")
    data = result["data"]
    receipt = result["receipt"]
    assert data["status"] == "accepted"
    assert len(runtime.calls) == 1
    request = json.loads(runtime.calls[0][0])
    assert runtime.calls[0][1] == "p4-ten-note-commit"
    assert request["api_version"] == 1
    assert {key: request[key] for key in (
        "project_id", "timeline_id", "base_revision_id", "base_publication_id",
    )} == {
        "project_id": runtime.project,
        "timeline_id": "film",
        "base_revision_id": original["base_parent"]["revision_id"],
        "base_publication_id": seeded["receipt"]["receipt_id"],
    }
    assert len(request["commands"]) == len(request["steps"]) == 10
    assert request["label"] == "Add ten review notes"
    assert request["merge"] == "fields"
    assert request["reads"] == []
    assert request["dry_run"] is False
    assert request["source"] == {"via": "python"}
    for command, step, note in zip(request["commands"], request["steps"], _notes()):
        assert command["op"] == "doc.patch"
        assert step == {
            "id": f"step-{note['ordinal'] + 1}", "label": note["id"], "intent": note["text"],
            "tags": ["review"], "command_ordinals": [note["ordinal"]],
            "patch": command["args"]["changes"], "reads": [],
        }

    expected_plan = execute(
        bundle_from_document(canonical), request["commands"],
        commit_request=request, evaluated_head=base_head,
    )
    assert expected_plan.bundle["source_mapping"] == authenticated_base["source_mapping"]
    assert data["canonical_document"] == document_from_bundle(expected_plan.bundle)

    assert data["merge_mode"] == "fields"
    for key in (
        "canonical_document", "document_digest", "accepted_revision_id", "commands",
        "id_map", "steps", "merge_mode", "previous_revision_id",
        "previous_publication_id", "rebased_onto", "publisher",
        "provider_version", "compiler_version",
    ):
        assert key in data
    assert data["document_digest"] == _digest(data["canonical_document"])
    assert data["accepted_revision_id"] == data["canonical_document"]["parent_revision"]["revision_id"]
    assert data["accepted_publication_id"] == receipt["receipt_id"]
    assert data["previous_revision_id"] == base_head["revision_id"]
    assert data["previous_publication_id"] == base_head["publication_id"]
    assert data["id_map"] == {}
    assert data["rebased_onto"] is None
    assert data["publisher"]
    assert data["commands"] == [record["command"] for record in expected_plan.records]
    for submitted, record in zip(request["commands"], expected_plan.records):
        assert {key: record["command"][key] for key in submitted} == submitted
    assert data["steps"] == expected_plan.steps
    assert len(data["steps"]) == 10
    for step, note in zip(data["steps"], _notes()):
        _assert_step_patch(step, note)
    assert receipt["receipt_id"]
    assert receipt["command_kind"] == "timeline.commit"
    landed = bundle_from_document(data["canonical_document"])
    assert landed["source_mapping"] == {
        **original["source_mapping"], "_runtime_document": data["canonical_document"],
    }
    assert _note_values(landed) == {note["id"]: note["text"] for note in _notes()}
    assert _without_notes(landed) == _without_notes(original)

    show_code, shown = _run_cli(
        runtime, capsys, "show", "film", "--published",
        "--revision-id", data["accepted_revision_id"],
    )
    assert show_code == 0 and shown["ok"] is True
    assert any(row["clip_id"] == "clip-a" and row["start"] == 1 for row in shown["data"]["rows"])
    history_code, history = _run_cli(runtime, capsys, "history", "film", "--limit", "50")
    assert history_code == 0 and history["ok"] is True
    revision = next(
        item for item in history["data"]["revisions"]
        if item["id"] == data["accepted_revision_id"]
    )
    assert revision["message"] == "Add ten review notes"
    raw_history = runtime.client().timelines.history(runtime.project, "film", limit=50)
    assert raw_history.ok
    stored = next(item for item in raw_history.data["items"] if item["publication_id"] == receipt["receipt_id"])
    assert stored["label"] == "Add ten review notes"
    assert stored["publisher"]
    assert stored["merge_mode"] == "fields"
    assert stored["records"]["schema_version"] == 1
    assert stored["records"]["commands"] == expected_plan.records
    assert stored["records"]["steps"] == expected_plan.steps
    assert stored["records"]["label"] == expected_plan.label
    assert stored["records"]["merge_mode"] == expected_plan.merge_mode
    assert stored["records"]["undo_of"] == []
    assert stored["records"]["restored_from"] is None
    assert stored["records"]["undo_steps"] is None
    assert len(stored["steps"]) == 10
    for step, note in zip(stored["steps"], _notes()):
        _assert_step_patch(step, note)

    runtime.restart()
    reloaded = timeline.open(
        runtime.project, "film", client=runtime.client(),
        outbox_path=tmp_path / "ten-note-reloaded.json",
    )
    assert "_runtime_document" not in reloaded.bundle["source_mapping"]
    for note in _notes():
        step_id = f"step-{note['ordinal'] + 1}"
        session = reloaded.session
        undo = session.undo(
            [receipt["receipt_id"]],
            steps={receipt["receipt_id"]: [step_id]},
            idempotency_key=f"p4-ten-note-undo-{note['id']}",
        )
        undo_data = undo["data"]
        assert undo_data["status"] == "accepted"
        inverse_request = json.loads(runtime.calls[-1][0])
        assert inverse_request["commands"][0]["op"] == "timeline.inverse"
        assert inverse_request["commands"][0]["args"]["steps"] == {
            receipt["receipt_id"]: [step_id]
        }
        undone = bundle_from_document(undo_data["canonical_document"])
        expected = {item["id"]: item["text"] for item in _notes() if item != note}
        assert _note_values(undone) == expected
        assert _without_notes(undone) == _without_notes(original)

        redo = session.undo(
            [undo["receipt"]["receipt_id"]],
            idempotency_key=f"p4-ten-note-redo-{note['id']}",
        )
        assert redo["data"]["status"] == "accepted"
        redone = bundle_from_document(redo["data"]["canonical_document"])
        assert _note_values(redone) == {item["id"]: item["text"] for item in _notes()}
        assert _without_notes(redone) == _without_notes(original)
