from __future__ import annotations

import copy
import importlib.util
import json
import sys
import types
import uuid
from pathlib import Path
from types import SimpleNamespace

import pytest

from astrid.sdk.timeline._common import TimelineEditError
from astrid.sdk.timeline_duplicate import _duplicate, copy_content, id_map
from astrid.sdk.timeline_import_prepare import _document_id, check_import_manifest, prepare_import
from astrid.sdk.timeline_restructure import is_split


def _source_bundle() -> dict:
    path = Path(__file__).parents[1] / "editing_scenarios" / "fixtures" / "empty.json"
    return json.loads(path.read_text(encoding="utf-8"))


def _chapter_bundle() -> dict:
    helper_path = Path(__file__).with_name("test_timeline_restructure.py")
    spec = importlib.util.spec_from_file_location("timeline_restructure_fixture", helper_path)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    flat = module.flat_film()
    for shot in flat["shots"].values():
        assets = shot["internal_timeline"]["registry"]["assets"]
        for index, asset in enumerate(assets.values(), start=1):
            asset["media_id"] = "sha256:" + f"{index:064x}"
        shot["base_internal_timeline"] = copy.deepcopy(shot["internal_timeline"])
    flat["project_id"] = "source-project"
    stored = module.split(flat)
    stored["parent"]["config"]["bundle"] = {
        "transcriptReferences": {
            "schema": "media.transcript.references/v1",
            "associations": [{"assetId": "P", "documentId": "transcript:source"}],
        },
    }
    return stored


def _inventory(monkeypatch, entries):
    module = types.ModuleType("astrid.sdk.timeline_export")
    module.inventory_elements = lambda _bundle, *, elements=None: copy.deepcopy(entries)
    monkeypatch.setitem(sys.modules, "astrid.sdk.timeline_export", module)


def test_copy_content_accepts_a_bundle_file(tmp_path):
    source = _source_bundle()
    path = tmp_path / "timeline.json"
    path.write_text(json.dumps(source), encoding="utf-8")
    copied, _ = copy_content(path, copy.deepcopy(source), new_slug="file-copy")
    assert list(copied["shots"]) == ["file-copy-A"]


def test_copy_content_preserves_parent_extensions_and_remaps_ids():
    source = _source_bundle()
    source_shot = source["placements"][0]["shot_id"]
    source_occurrence = source["placements"][0]["occurrence_id"]
    source["parent"]["extension"] = {
        "shot": source_shot,
        "occurrence": source_occurrence,
        "nested": [{"shot": source_shot}],
    }
    source["parent"]["occurrences"] = [{"shot_id": source_shot, "occurrence_id": source_occurrence}]

    copied, _ = copy_content(source, {"parent": {}, "shots": {}, "placements": []}, new_slug="extension-copy")
    target_shot = copied["placements"][0]["shot_id"]
    target_occurrence = copied["placements"][0]["occurrence_id"]
    assert copied["parent"]["extension"] == {
        "shot": target_shot,
        "occurrence": target_occurrence,
        "nested": [{"shot": target_shot}],
    }
    assert "occurrences" not in copied["parent"]


def test_duplicate_accepts_a_file_source_through_shared_copy_path(tmp_path, monkeypatch):
    source = _source_bundle()
    path = tmp_path / "timeline.json"
    path.write_text(json.dumps(source), encoding="utf-8")

    calls = []

    class Timelines:
        def import_timeline(self, project, body, **kwargs):
            calls.append((project, body, kwargs))
            return SimpleNamespace(ok=True, data={"timeline_id": body["timeline_id"], "new_head": "new-head"})

    class Client:
        timelines = Timelines()
        projects = SimpleNamespace(show=lambda ref: SimpleNamespace(ok=True, data={"project_id": ref}))

    monkeypatch.setattr("astrid.sdk.timeline_export._list_transcript_documents", lambda *_args: [])
    result = _duplicate(Client(), "p", path, slug="file-duplicate", name=None, revision_id=None)
    assert result["timeline_id"] == "file-duplicate"
    assert result["shots"] == 1
    assert calls[0][1]["source"] == {"via": "python"}
    assert calls[0][1]["publication"]["expected_head"] is None


def test_check_import_manifest_reports_unpinned_missing_and_different():
    manifest = {
        "format": "astrid.timeline/1",
        "elements": [
            {"packId": "astrid_motion", "kind": "effect", "id": "gone", "revision": "sha256:old", "pinned": False},
            {"packId": "astrid_motion", "kind": "effect", "id": "changed", "revision": "sha256:old", "pinned": False},
        ],
    }
    result = check_import_manifest(manifest, elements=[
        {"packId": "astrid_motion", "kind": "effect", "id": "changed", "revision": "sha256:new"},
    ])
    assert [row["id"] for row in result["missing_effects"]] == ["gone"]
    assert [row["id"] for row in result["different_effects"]] == ["changed"]


def test_check_import_manifest_accepts_only_unpinned_unresolved_empty_pack_identity():
    unresolved = {"packId": "", "kind": "effect", "id": "not-installed", "revision": "", "pinned": False}
    result = check_import_manifest({"format": "astrid.timeline/1", "elements": [unresolved]}, elements=[])
    assert result == {"missing_effects": [unresolved], "different_effects": []}

    for invalid in (
        {"kind": "effect", "id": "not-installed", "pinned": False},
        {"packId": None, "kind": "effect", "id": "not-installed", "pinned": False},
        {"packId": 7, "kind": "effect", "id": "not-installed", "pinned": False},
    ):
        with pytest.raises(TimelineEditError, match="packId to be a string"):
            check_import_manifest({"format": "astrid.timeline/1", "elements": [invalid]}, elements=[])

    for invalid in (
        {**unresolved, "revision": "rev-a", "pinned": True},
        {key: value for key, value in unresolved.items() if key != "pinned"},
    ):
        with pytest.raises(TimelineEditError, match="requires pinned=false when packId is empty"):
            check_import_manifest({"format": "astrid.timeline/1", "elements": [invalid]}, elements=[])


def test_blank_manifest_pack_cannot_hide_catalogued_project_pack_element():
    source = _source_bundle()
    clip = {"id": "project-effect", "clipType": "project-glow", "track": "effects", "at": 0, "hold": 1}
    source["parent"]["clips"] = [clip]
    source["parent"]["config"]["clips"] = [copy.deepcopy(clip)]
    manifest = {"format": "astrid.timeline/1", "elements": [
        {"packId": "", "kind": "effect", "id": "project-glow", "revision": "", "pinned": False},
    ]}
    with pytest.raises(TimelineEditError, match="inventory does not match"):
        prepare_import(
            source, manifest, {}, "target", elements=[
                {"packId": "project_0123456789abcdefabcd", "kind": "effect", "id": "project-glow", "revision": "rev"},
            ], timeline_id="target-timeline",
        )


def test_builtin_hold_clip_round_trips_through_inventory_and_prepare():
    from astrid.core.element.catalog import list_element_descriptors
    from astrid.sdk.timeline_export import inventory_elements

    source = _source_bundle()
    builtins = [
        {"id": "legacy-hold", "clipType": "hold", "track": "type", "at": 0, "hold": 1},
        {"id": "effect-layer", "clipType": "effect-layer", "track": "effects", "at": 0, "hold": 1},
    ]
    source["parent"]["clips"] = builtins
    source["parent"]["config"]["clips"] = copy.deepcopy(builtins)
    catalog = list_element_descriptors()
    elements = inventory_elements(source, elements=catalog)
    manifest = {"format": "astrid.timeline/1", "name": "Hold fixture", "elements": elements}

    prepared = prepare_import(source, manifest, {}, "target", elements=catalog, timeline_id="target-timeline")

    assert not any(row["id"] in {"hold", "effect-layer", "legacy-hold"} for row in elements)
    assert all(row["packId"] for row in elements)
    assert prepared["differences"] == {"missing_effects": [], "different_effects": []}
    assert prepared["publication"]["parent_composition"]["clips"] == builtins


def test_inventory_to_prepare_reports_unknown_unpinned_transition_from_source_pack():
    from astrid.core.element.catalog import list_element_descriptors
    from astrid.sdk.timeline_export import inventory_elements

    source = _source_bundle()
    clip = {
        "id": "legacy-unknown", "clipType": "media", "track": "v1", "at": 0, "hold": 1,
        "transition": {"packId": "source_motion", "id": "not-installed-transition", "revision": ""},
    }
    source["parent"]["clips"] = [clip]
    source["parent"]["config"]["clips"] = [copy.deepcopy(clip)]
    catalog = list_element_descriptors()
    elements = inventory_elements(source, elements=catalog)
    manifest = {"format": "astrid.timeline/1", "name": "Missing effect fixture", "elements": elements}

    prepared = prepare_import(source, manifest, {}, "target", elements=catalog, timeline_id="target-timeline")

    assert {"packId", "kind", "id", "revision", "pinned"} <= set(elements[0])
    assert {row["id"] for row in elements} == {"not-installed-transition", "am-snap-plate"}
    assert next(row for row in elements if row["id"] == "not-installed-transition") == {
        "packId": "source_motion", "kind": "transition", "id": "not-installed-transition", "revision": "", "pinned": False,
    }
    assert prepared["differences"]["missing_effects"] == [
        next(row for row in elements if row["id"] == "not-installed-transition")
    ]


def test_check_import_manifest_refuses_unknown_format_and_unsatisfied_pin():
    with pytest.raises(TimelineEditError, match="newer Astrid"):
        check_import_manifest({"format": "astrid.timeline/2", "elements": []}, elements=[])
    manifest = {"format": "astrid.timeline/1", "elements": [
        {"packId": "astrid_motion", "kind": "effect", "id": "pinned", "revision": "sha256:old", "pinned": True},
    ]}
    with pytest.raises(TimelineEditError, match="pins pinned"):
        check_import_manifest(manifest, elements=[])
    with pytest.raises(TimelineEditError, match="pins pinned without a revision"):
        check_import_manifest({"format": "astrid.timeline/1", "elements": [
            {"packId": "astrid_motion", "kind": "effect", "id": "pinned", "pinned": True},
        ]}, elements=[{"packId": "astrid_motion", "kind": "effect", "id": "pinned", "revision": "sha256:old"}])
    with pytest.raises(TimelineEditError, match="different revision"):
        check_import_manifest({"format": "astrid.timeline/1", "elements": [
            {"packId": "astrid_motion", "kind": "effect", "id": "pinned", "revision": "sha256:old", "pinned": True},
        ]}, elements=[{"packId": "astrid_motion", "kind": "effect", "id": "pinned"}])
    with pytest.raises(TimelineEditError, match="custom effect custom"):
        check_import_manifest({"format": "astrid.timeline/1", "elements": [
            {"packId": "project_custom", "kind": "effect", "id": "custom", "pinned": False},
        ]}, elements=[])


def test_prepare_import_rejects_manifest_that_hides_a_source_pin(monkeypatch):
    source = _source_bundle()
    _inventory(monkeypatch, [
        {"packId": "astrid_motion", "kind": "effect", "id": "pinned-effect", "revision": "sha256:" + "a" * 64, "pinned": True},
    ])
    with pytest.raises(TimelineEditError, match="inventory does not match"):
        prepare_import(source, {"format": "astrid.timeline/1", "elements": []}, {}, "target", elements=[], timeline_id="target-timeline")


def test_manifest_inventory_allows_target_revision_difference_for_unpinned_element(monkeypatch):
    source = _source_bundle()
    current = {"packId": "astrid_motion", "kind": "effect", "id": "unPinned", "revision": "sha256:new", "pinned": False}
    _inventory(monkeypatch, [current])
    manifest = {"format": "astrid.timeline/1", "name": "Fixture", "elements": [
        {**current, "revision": "sha256:old"},
    ]}
    result = prepare_import(source, manifest, {}, "target", elements=[current], timeline_id="target-timeline")
    assert result["differences"]["different_effects"][0]["id"] == "unPinned"


def test_prepare_import_builds_fresh_children_and_target_transcript_ids(monkeypatch):
    source = _chapter_bundle()
    source_project = source["project_id"]
    first_shot_id = source["placements"][0]["shot_id"]
    first_shot = source["shots"][first_shot_id]
    media_id = next(iter(first_shot["internal_timeline"]["registry"]["assets"].values()))["media_id"]
    source_document_id = _document_id(source_project, media_id, "default")
    source["parent"]["config"]["bundle"]["transcriptReferences"]["associations"][0]["documentId"] = source_document_id
    first_shot["payload"]["text_bindings"] = [{
        "binding_id": "source-binding",
        "kind": "voiceover_script",
        "slot": None,
        "head": 8,
        "media_id": media_id,
        "content_hash": media_id,
        "byte_size": 12,
        "mime_type": "text/plain; charset=utf-8",
        "created_at": "source timestamp",
        "updated_at": "source timestamp",
    }]
    first_shot["payload"]["items"] = [{
        "item_id": "source-item",
        "media_id": media_id,
        "selected_item_id": "source-item",
        "metadata": {"role": "primary_visual"},
    }]
    _inventory(monkeypatch, [])
    manifest = {"format": "astrid.timeline/1", "name": "Fixture", "elements": []}
    content = {"schema": "media.transcript/v1", "media_id": media_id, "source_id": "default", "segments": []}
    unreferenced = {"schema": "media.transcript/v1", "media_id": media_id, "source_id": "other", "segments": []}
    result = prepare_import(
        source,
        manifest,
        {"transcripts/fixture.json": {"document_id": source_document_id, "kind": "media.transcript", "content": content},
         "transcripts/unreferenced.json": unreferenced},
        "target-project",
        timeline_id="target-timeline",
    )
    assert result["timeline_id"] == "target-timeline"
    assert result["publication"]["expected_head"] is None
    assert result["publication"]["internal_timeline_revisions"]
    assert all(row["timeline_id"] == "target-timeline" for row in result["publication"]["internal_timeline_revisions"])
    assert len(result["publication"]["shot_revisions"]) == len(source["placements"])
    assert {row["shot_id"] for row in result["publication"]["shot_revisions"]}.isdisjoint(
        {row["shot_id"] for row in source["placements"]}
    )
    new_document_id = next(row["document_id"] for row in result["transcripts"] if row["content"]["source_id"] == "default")
    assert len(result["transcripts"]) == 2
    assert new_document_id != source_document_id
    assert result["publication"]["parent_composition"]["config"]["bundle"]["transcriptReferences"]["associations"][0]["documentId"] == new_document_id
    registration = result["text_bindings"][0]
    assert registration["media_id"] == media_id and registration["shot_id"] != first_shot_id
    copied_pin = next(row["payload"]["text_bindings"][0] for row in result["publication"]["shot_revisions"]
                      if row["payload"].get("text_bindings"))
    assert copied_pin["head"] == 1 and copied_pin["binding_id"] != "source-binding"
    assert "created_at" not in copied_pin and "updated_at" not in copied_pin
    assert {row["media_id"] for row in result["publication"]["dependency_manifest"]["media"]} >= {media_id}
    copied_item = next(row["payload"]["items"][0] for row in result["publication"]["shot_revisions"]
                       if row["payload"].get("items"))
    assert copied_item["item_id"] != "source-item"
    assert copied_item["selected_item_id"] == copied_item["item_id"]
    assert source_project != "target-project"


def test_prepare_import_preserves_split_parent_authoring_fields(monkeypatch):
    source = _chapter_bundle()
    assert is_split(source)
    source_project = source["project_id"]
    shot_id = source["placements"][0]["shot_id"]
    occurrence_id = source["placements"][0]["occurrence_id"]
    media_id = next(iter(source["shots"][shot_id]["internal_timeline"]["registry"]["assets"].values()))["media_id"]
    source_document_id = _document_id(source_project, media_id, "default")
    source["parent"]["config"]["bundle"]["transcriptReferences"]["associations"][0]["documentId"] = source_document_id
    legacy_chapters = [{"name": "Opening", "shotId": shot_id, "occurrenceId": occurrence_id, "opaque": {"keep": True}}]
    source["parent"]["config"].setdefault("app", {})["astrid"] = {"chapters": copy.deepcopy(legacy_chapters)}
    source["parent"]["config"]["clips"] = copy.deepcopy(source["parent"]["clips"])
    source["parent"]["opaque_authoring"] = {
        "shot": shot_id,
        "occurrence": occurrence_id,
        "nested": [{"shot": shot_id, "occurrence": occurrence_id}],
    }
    _inventory(monkeypatch, [])
    content = {"schema": "media.transcript/v1", "media_id": media_id, "source_id": "default", "segments": []}
    prepared = prepare_import(
        source,
        {"format": "astrid.timeline/1", "name": "Split source", "elements": []},
        {"transcripts/source.json": {"document_id": source_document_id, "kind": "media.transcript", "content": content}},
        "target-project",
        timeline_id="target-timeline",
    )

    parent = prepared["publication"]["parent_composition"]
    target_shot = id_map([row["shot_id"] for row in source["placements"]], "target-timeline")[shot_id]
    target_occurrence = id_map([row["occurrence_id"] for row in source["placements"]], "occ-target-timeline")[occurrence_id]
    target_document_id = prepared["transcripts"][0]["document_id"]
    id_mapping = {
        **id_map([row["shot_id"] for row in source["placements"]], "target-timeline"),
        **id_map([row["occurrence_id"] for row in source["placements"]], "occ-target-timeline"),
    }

    def remap_parent(value):
        if isinstance(value, str):
            return id_mapping.get(value, value)
        if isinstance(value, list):
            return [remap_parent(item) for item in value]
        if isinstance(value, dict):
            return {key: remap_parent(item) for key, item in value.items()}
        return value

    expected_parent = remap_parent(source["parent"])
    expected_parent.pop("occurrences", None)
    expected_parent["config"]["bundle"]["transcriptReferences"]["associations"][0]["documentId"] = target_document_id
    parent_without_compiler_occurrences = {key: value for key, value in parent.items() if key != "occurrences"}
    assert parent_without_compiler_occurrences == expected_parent
    assert parent["config"]["app"]["astrid"]["chapters"] == [{
        "name": "Opening", "shotId": target_shot, "occurrenceId": target_occurrence, "opaque": {"keep": True},
    }]
    assert parent["config"]["clips"] == source["parent"]["config"]["clips"]
    assert parent["opaque_authoring"] == {
        "shot": target_shot,
        "occurrence": target_occurrence,
        "nested": [{"shot": target_shot, "occurrence": target_occurrence}],
    }
    assert parent["config"]["bundle"]["transcriptReferences"]["associations"][0]["documentId"] == target_document_id
    assert len(prepared["publication"]["shot_revisions"]) == len(source["placements"])
    assert {row["shot_id"] for row in prepared["publication"]["shot_revisions"]}.isdisjoint(
        {row["shot_id"] for row in source["placements"]}
    )


def test_prepare_import_requires_every_transcript_reference(monkeypatch):
    source = _source_bundle()
    source["parent"]["config"]["bundle"] = {"transcriptReferences": {
        "schema": "media.transcript.references/v1",
        "associations": [{"assetId": "P", "documentId": "transcript:missing"}],
    }}
    _inventory(monkeypatch, [])
    with pytest.raises(TimelineEditError, match="no carried transcript"):
        prepare_import(source, {"format": "astrid.timeline/1", "elements": []}, {}, "target", timeline_id=str(uuid.uuid4()))
