"""R13 acceptance: the evidence pack is self-contained, deterministic, and hashed."""

from __future__ import annotations

import hashlib
import io
import json
import re
import shutil
from copy import deepcopy
from dataclasses import replace
from pathlib import Path

import pytest
from jsonschema import Draft202012Validator
from PIL import Image
from referencing import Registry, Resource

from astrid.core.timeline.snapshot import TimelineSnapshot, snapshot_from_runtime
from astrid.packs.rendering.actions.timeline_visualize.emit import (
    emit_action_index,
    emit_asset_index,
    emit_diagnostics,
    emit_ground_truth,
    emit_metric_definitions,
    emit_reading_guide,
    emit_structure_md,
    emit_transcript_index,
)
from astrid.packs.rendering.actions.timeline_visualize.evidence_pack import (
    FROZEN_AT_SENTINEL,
    PACK_HASHES_NAME,
    write_evidence_pack,
)
from astrid.packs.rendering.actions.timeline_visualize.layout import (
    layout_timeline,
)
from astrid.packs.rendering.actions.timeline_visualize.model import (
    build_model,
)
from astrid.packs.rendering.actions.timeline_visualize.navigation import (
    build_identity_map,
)
from astrid.packs.rendering.actions.timeline_visualize.render_png import (
    render_page_png,
)
from astrid.packs.rendering.actions.timeline_visualize.schemas import (
    DEFS_PATH,
    SCHEMAS,
)
from astrid.packs.rendering.actions.timeline_visualize.scope import (
    select_scope,
)
from astrid.core.timeline.snapshot_digest import (
    canonical_json_bytes,
    sha256_bytes,
)

TESTS_ROOT = Path(__file__).resolve().parents[2]
FIXTURE_ROOT = TESTS_ROOT / "fixtures" / "timeline_visualize"
SLICE_DIR = FIXTURE_ROOT / "desert_slice"
TRUTH = json.loads((FIXTURE_ROOT / "desert_truth.json").read_text(encoding="utf-8"))
PROJECT_SLUG = "desert-plant-growth"

#: Absolute manifest path R9 embeds in every --from-view argv; R13 must rewrite
#: it to the pack-relative path so a copied pack stays self-contained.
MANIFEST_ARGV_PATH = "/tmp/agent-view/manifest.json"

_ABS_PATH_RE = re.compile(r"^/|^[A-Za-z]:[\\/]")
_DATETIME_RE = re.compile(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?(?:Z|[+-]\d{2}:\d{2})?")


def _iter_strings(value):
    if isinstance(value, str):
        yield value
    elif isinstance(value, dict):
        for nested in value.values():
            yield from _iter_strings(nested)
    elif isinstance(value, list):
        for nested in value:
            yield from _iter_strings(nested)


def _prepared_snapshot() -> TimelineSnapshot:
    """Portable desert slice projected through the runtime snapshot path."""
    events = [
        json.loads(line)
        for line in (SLICE_DIR / "assembly.jsonl").read_text(encoding="utf-8").splitlines()
    ]
    identity = TRUTH["timeline"]
    return snapshot_from_runtime(
        timeline_id=identity["uuid"],
        timeline_ulid=identity["ulid"],
        slug=identity["slug"],
        project_slug=PROJECT_SLUG,
        events=events,
    )


def _tiny_png(path: Path, seed: int) -> Path:
    """Deterministic 4x4 RGB PNG for filmstrip frame fixtures."""
    buffer = io.BytesIO()
    Image.new("RGB", (4, 4), (seed % 256, (seed * 7) % 256, (seed * 13) % 256)).save(
        buffer, format="PNG"
    )
    path.write_bytes(buffer.getvalue())
    return path


@pytest.fixture
def pack_inputs(tmp_path: Path) -> dict:
    """Everything write_evidence_pack needs for the clean desert root pack."""
    snapshot = _prepared_snapshot()
    model = build_model(snapshot)
    identity_map = build_identity_map(
        model,
        root_sns=model.snapshot_sns,
        timeline_uuid=model.timeline_uuid,
        timeline_ulid=model.timeline_ulid,
    )
    scope = select_scope(model, kind="timeline")
    pages = layout_timeline(model, identity_map, scope, layout="time-scaled")
    assert len(pages) == 2  # desert composition 2352fr/98s spans two windows

    frame_a = _tiny_png(tmp_path / "frame_a.png", seed=1)
    frame_b = _tiny_png(tmp_path / "frame_b.png", seed=2)

    return {
        "model": model,
        "identity_map": identity_map,
        "snapshot": snapshot,
        "scope": scope,
        "ground_truth": emit_ground_truth(model, identity_map, snapshot),
        "action_index": emit_action_index(model, identity_map, snapshot, MANIFEST_ARGV_PATH),
        "asset_index": emit_asset_index(model, identity_map, snapshot),
        "transcript_index": emit_transcript_index(model, identity_map, snapshot),
        "diagnostics": emit_diagnostics(model, identity_map, snapshot),
        "reading_guide": emit_reading_guide(model, identity_map, snapshot),
        "structure_md": emit_structure_md(model, identity_map, snapshot),
        "metric_definitions": emit_metric_definitions(model, identity_map, snapshot),
        "pages": pages,
        "svg_bytes": {},
        "png_bytes": {page.page_id: render_page_png(page) for page in pages},
        "filmstrips": {"TL01.AS01": [frame_a, frame_b]},
    }


def _schema_registry() -> Registry:
    documents = {"_defs": json.loads(DEFS_PATH.read_text(encoding="utf-8"))}
    documents.update({name: schema.load() for name, schema in SCHEMAS.items()})
    resources = [
        (document["$id"], Resource.from_contents(document))
        for document in documents.values()
    ]
    return Registry().with_resources(resources)


def _validate(name: str, instance: dict) -> None:
    validator = Draft202012Validator(
        SCHEMAS[name].load(),
        registry=_schema_registry(),
    )
    errors = sorted(validator.iter_errors(instance), key=str)
    assert not errors, f"{name} invalid: " + "; ".join(error.message for error in errors)


def _write_pack(tmp_path: Path, **overrides):
    out_root = tmp_path / "out"
    return write_evidence_pack(out_root=out_root, page_id_prefix="PG", **overrides), out_root


# ---------------------------------------------------------------------------
# 1. Full pack from the desert slice.
# ---------------------------------------------------------------------------


def test_full_pack_all_files_exist_and_validate(pack_inputs, tmp_path) -> None:
    _layout, out_root = _write_pack(tmp_path, **pack_inputs)

    expected_files = {
        "manifest.json",
        "ground-truth.json",
        "view-map.json",
        "action-index.json",
        "asset-index.json",
        "transcript-index.json",
        "diagnostics.json",
        "metric-definitions.json",
        "reading-guide.md",
        "structure.md",
        "PG001.png",
        "PG002.png",
        "filmstrip/TL01_AS01_film_00.png",
        "filmstrip/TL01_AS01_film_01.png",
        PACK_HASHES_NAME,
    }
    on_disk = {path.relative_to(out_root).as_posix() for path in out_root.rglob("*") if path.is_file()}
    assert on_disk == expected_files

    for name in (
        "manifest",
        "ground-truth",
        "view-map",
        "action-index",
        "asset-index",
        "transcript-index",
        "diagnostics",
        "metric-definitions",
    ):
        _validate(name, json.loads((out_root / f"{name}.json").read_text(encoding="utf-8")))

    # PNG count == page count; SVG output was removed, so none are written.
    assert len(pack_inputs["pages"]) == 2
    pngs = sorted(out_root.glob("PG*.png"))
    assert len(pngs) == len(pack_inputs["pages"]) == 2
    assert not list(out_root.glob("*.svg"))

    # Reading guide present; structure present (non-null path, null reason).
    assert (out_root / "reading-guide.md").is_file()
    manifest = json.loads((out_root / "manifest.json").read_text(encoding="utf-8"))
    assert manifest["inputs"]["source_mode"] == "kernel"
    assert manifest["inputs"]["resolved_project"] == {
        "slug": pack_inputs["snapshot"].project_slug
    }
    assert manifest["inputs"]["resolved_timelines"] == [
        manifest["snapshots"][0]["timeline"]
    ]
    assert manifest["inputs"]["canonical_timeline_identities"] == [
        {
            **manifest["snapshots"][0]["timeline"],
            "ulid": manifest["snapshots"][0]["timeline"]["ulid"].lower(),
        }
    ]
    uppercase_user_identity = json.loads(json.dumps(manifest))
    uppercase_user_identity["inputs"]["canonical_timeline_identities"][0][
        "ulid"
    ] = uppercase_user_identity["inputs"]["resolved_timelines"][0]["ulid"]
    with pytest.raises(AssertionError, match="manifest invalid"):
        _validate("manifest", uppercase_user_identity)
    # The comparison-safe identity projection is additive and optional; the
    # authority provenance (source_mode, resolved_project/timelines) is required.
    without_projection = json.loads(json.dumps(manifest))
    without_projection["inputs"].pop("canonical_timeline_identities")
    _validate("manifest", without_projection)
    for key in ("source_mode", "resolved_project", "resolved_timelines"):
        missing_provenance = json.loads(json.dumps(manifest))
        missing_provenance["inputs"].pop(key)
        with pytest.raises(AssertionError, match=f"'{key}' is a required property"):
            _validate("manifest", missing_provenance)
    assert manifest["entrypoints"]["structure"] == "structure.md"
    assert manifest["optional_formats"]["structure"] == {"path": "structure.md", "reason": None}
    assert manifest["entrypoints"]["reading_guide"] == "reading-guide.md"

    # Declared reading order matches view-map and the page tuple.
    page_ids = [page.page_id for page in pack_inputs["pages"]]
    assert manifest["reading_order"] == page_ids
    view_map = json.loads((out_root / "view-map.json").read_text(encoding="utf-8"))
    assert view_map["reading_order"] == page_ids
    assert manifest["page_count"] == len(page_ids)


def test_manifest_records_explicit_frozen_authority_mode(pack_inputs, tmp_path) -> None:
    _layout, out_root = _write_pack(
        tmp_path,
        **pack_inputs,
        source_mode="frozen",
    )

    manifest = json.loads((out_root / "manifest.json").read_text(encoding="utf-8"))
    _validate("manifest", manifest)
    assert manifest["inputs"]["source_mode"] == "frozen"
    assert manifest["inputs"]["resolved_timelines"] == [
        manifest["snapshots"][0]["timeline"]
    ]


# ---------------------------------------------------------------------------
# 2. Self-containment: no absolute paths; --from-view resolves inside the pack.
# ---------------------------------------------------------------------------


def test_self_containment_no_absolute_paths_and_argv_rewrite(pack_inputs, tmp_path) -> None:
    _layout, out_root = _write_pack(tmp_path, **pack_inputs)
    out_resolved = out_root.resolve()

    for rel in (
        "manifest.json",
        "ground-truth.json",
        "view-map.json",
        "action-index.json",
        "transcript-index.json",
        "diagnostics.json",
        "metric-definitions.json",
        PACK_HASHES_NAME,
    ):
        document = json.loads((out_root / rel).read_text(encoding="utf-8"))
        for value in _iter_strings(document):
            assert _ABS_PATH_RE.match(value) is None, f"{rel} contains absolute path {value!r}"

    # asset-index.json carries frozen-project provenance only; its
    # contained_path values are schema-blessed absolute paths and must never
    # point inside the pack (self-containment direction).
    asset_index = json.loads((out_root / "asset-index.json").read_text(encoding="utf-8"))
    for asset in asset_index["assets"]:
        contained = asset["contained_path"]
        if contained:
            assert not Path(contained).resolve().is_relative_to(out_resolved), (
                f"asset {asset['qualified_ref']} contained_path points inside the pack"
            )

    # Every --from-view argv element is the pack-relative manifest path and
    # resolves to a file inside the pack root.
    action_index = json.loads((out_root / "action-index.json").read_text(encoding="utf-8"))
    for ref, entry in action_index["entries"].items():
        for action_name, action in entry["actions"].items():
            argv = action["argv"]
            if "--from-view" not in argv:
                continue
            target = argv[argv.index("--from-view") + 1]
            assert target == "manifest.json", (
                f"{ref} {action_name} argv --from-view target {target!r} is not pack-relative"
            )
            assert (out_root / target).is_file()
            assert (out_root / target).resolve().is_relative_to(out_resolved)


# ---------------------------------------------------------------------------
# 3. Determinism: two writes -> identical bytes; PNG/SVG pass through.
# ---------------------------------------------------------------------------


def test_determinism_two_writes_identical_bytes(pack_inputs, tmp_path) -> None:
    out_a = tmp_path / "a"
    out_b = tmp_path / "b"
    layout_a = write_evidence_pack(out_root=out_a, page_id_prefix="PG", **pack_inputs)
    layout_b = write_evidence_pack(out_root=out_b, page_id_prefix="PG", **pack_inputs)

    assert layout_a.file_hashes == layout_b.file_hashes
    assert layout_a.total_bytes == layout_b.total_bytes
    for rel in layout_a.file_hashes:
        assert (out_a / rel).read_bytes() == (out_b / rel).read_bytes(), rel

    # PNG/SVG bytes are byte-identical to R11's bytes (pass-through).
    for page_id, png in pack_inputs["png_bytes"].items():
        assert (out_a / f"{page_id}.png").read_bytes() == png
    for page_id, svg in pack_inputs["svg_bytes"].items():
        assert (out_a / f"{page_id}.svg").read_bytes() == svg


def test_png_rendering_is_byte_deterministic_and_page_sized(pack_inputs) -> None:
    """render_page_png is a pure function of the layout page: same bytes twice."""
    for page in pack_inputs["pages"]:
        first = render_page_png(page)
        assert render_page_png(page) == first == pack_inputs["png_bytes"][page.page_id]
        with Image.open(io.BytesIO(first)) as image:
            assert image.format == "PNG"
            assert image.size == (page.width, page.height)


# ---------------------------------------------------------------------------
# 4. The pack works standalone, copied away from its parent project.
# ---------------------------------------------------------------------------


def test_pack_works_standalone_copied_elsewhere(pack_inputs, tmp_path) -> None:
    _layout, out_root = _write_pack(tmp_path, **pack_inputs)
    moved = tmp_path / "moved"
    shutil.copytree(out_root, moved)

    # Loaded without the model, identity map, snapshot, or scope.
    manifest = json.loads((moved / "manifest.json").read_text(encoding="utf-8"))
    ground_truth = json.loads((moved / "ground-truth.json").read_text(encoding="utf-8"))
    action_index = json.loads((moved / "action-index.json").read_text(encoding="utf-8"))

    # Every entrypoint and every hashed output resolves inside the moved pack.
    for key, rel in manifest["entrypoints"].items():
        if rel is not None:
            assert (moved / rel).is_file(), f"entrypoint {key} -> {rel!r} missing"
    for record in manifest["outputs"]:
        target = moved / record["path"]
        assert target.is_file(), f"output {record['path']!r} missing"
        actual = hashlib.sha256(target.read_bytes()).hexdigest()
        assert actual == record["sha256"], f"output {record['path']!r} hash mismatch"
        assert record["content_hash"] == f"sha256:{actual}"

    # IDs and refs resolve entirely within the pack.
    gt_refs = {obj["qualified_ref"] for obj in ground_truth["objects"]}
    assert set(action_index["entries"]) <= gt_refs
    assert manifest["snapshots"] == ground_truth["snapshots"]
    for ref, entry in action_index["entries"].items():
        assert ref in gt_refs
        for action in entry["actions"].values():
            argv = action["argv"]
            if "--from-view" in argv:
                target = argv[argv.index("--from-view") + 1]
                assert (moved / target).is_file()

    # pack-hashes.json verifies every file (including the manifest) in place.
    pack_hashes = json.loads((moved / PACK_HASHES_NAME).read_text(encoding="utf-8"))
    for rel, info in pack_hashes["files"].items():
        assert (moved / rel).is_file()
        assert hashlib.sha256((moved / rel).read_bytes()).hexdigest() == info["sha256"]
    assert pack_hashes["files"]["manifest.json"]["sha256"] == hashlib.sha256(
        (moved / "manifest.json").read_bytes()
    ).hexdigest()


# ---------------------------------------------------------------------------
# 5. file_hashes: exact match, no orphans.
# ---------------------------------------------------------------------------


def test_file_hashes_match_and_cover_every_file(pack_inputs, tmp_path) -> None:
    layout, out_root = _write_pack(tmp_path, **pack_inputs)

    on_disk = {path.relative_to(out_root).as_posix() for path in out_root.rglob("*") if path.is_file()}
    assert set(layout.file_hashes) == on_disk  # every file hashed, no orphans
    for rel, digest in layout.file_hashes.items():
        assert hashlib.sha256((out_root / rel).read_bytes()).hexdigest() == digest

    pack_hashes = json.loads((out_root / PACK_HASHES_NAME).read_text(encoding="utf-8"))
    # Self-excluded, but every other file (manifest included) is covered.
    assert set(pack_hashes["files"]) == on_disk - {PACK_HASHES_NAME}
    assert list(pack_hashes["files"])[0] == "manifest.json"  # reading order leads with root
    for rel, info in pack_hashes["files"].items():
        assert info["sha256"] == layout.file_hashes[rel]
        assert info["bytes"] == (out_root / rel).stat().st_size


# ---------------------------------------------------------------------------
# 6. Optional-format null/reason: structure.md omitted.
# ---------------------------------------------------------------------------


def test_optional_structure_null_with_reason(pack_inputs, tmp_path) -> None:
    inputs = dict(pack_inputs)
    inputs["structure_md"] = None
    _layout, out_root = _write_pack(tmp_path, **inputs)

    assert not (out_root / "structure.md").exists()
    manifest = json.loads((out_root / "manifest.json").read_text(encoding="utf-8"))
    assert manifest["entrypoints"]["structure"] is None
    assert manifest["optional_formats"]["structure"]["path"] is None
    assert manifest["optional_formats"]["structure"]["reason"]
    assert manifest["companions"]["structure"]["path"] is None
    assert manifest["companions"]["structure"]["reason"]
    assert manifest["companions"]["structure"]["content_kind"] == "factual_markdown"
    _validate("manifest", manifest)


# ---------------------------------------------------------------------------
# 7. Deterministic sentinel, no wall clock, no writes outside out_root.
# ---------------------------------------------------------------------------


def test_no_datetime_and_no_writes_outside_out_root(pack_inputs, tmp_path) -> None:
    out_root = tmp_path / "out"
    before = {
        path
        for path in tmp_path.rglob("*")
        if path.is_file() and out_root not in path.parents and path != out_root
    }
    write_evidence_pack(out_root=out_root, page_id_prefix="PG", **pack_inputs)
    after = {
        path
        for path in tmp_path.rglob("*")
        if path.is_file() and out_root not in path.parents and path != out_root
    }
    assert before == after  # nothing outside out_root was created or touched

    manifest = json.loads((out_root / "manifest.json").read_text(encoding="utf-8"))
    ground_truth = json.loads((out_root / "ground-truth.json").read_text(encoding="utf-8"))
    assert manifest["created"] == FROZEN_AT_SENTINEL
    assert ground_truth["timestamps"]["frozen_at"] == FROZEN_AT_SENTINEL

    for rel in (
        "manifest.json",
        "ground-truth.json",
        "view-map.json",
        "action-index.json",
        "asset-index.json",
        "transcript-index.json",
        "diagnostics.json",
        "metric-definitions.json",
        PACK_HASHES_NAME,
    ):
        document = json.loads((out_root / rel).read_text(encoding="utf-8"))
        for value in _iter_strings(document):
            if _DATETIME_RE.search(value) and value != FROZEN_AT_SENTINEL:
                pytest.fail(f"{rel} carries wall-clock datetime {value!r}")


def test_frozen_at_override_is_deterministic_and_materialized(pack_inputs, tmp_path) -> None:
    inputs = dict(pack_inputs)
    inputs["frozen_at"] = "2026-08-10T00:00:00Z"
    _layout, out_root = _write_pack(tmp_path, **inputs)

    manifest = json.loads((out_root / "manifest.json").read_text(encoding="utf-8"))
    ground_truth = json.loads((out_root / "ground-truth.json").read_text(encoding="utf-8"))
    assert manifest["created"] == "2026-08-10T00:00:00Z"
    assert ground_truth["timestamps"]["frozen_at"] == "2026-08-10T00:00:00Z"
    # The override changes no identity: SNS is untouched by timestamps.
    _validate("manifest", manifest)
    _validate("ground-truth", ground_truth)


# ---------------------------------------------------------------------------
# 8. R13 metric-definitions artifact (V4): shipped, validated, deterministic.
# ---------------------------------------------------------------------------


def _prepared_snapshot_with_missing_asset() -> TimelineSnapshot:
    """Like _prepared_snapshot but one asset carries no runtime object id."""
    snapshot = _prepared_snapshot()
    registry = deepcopy(snapshot.registry)
    # Without a locator or object id the asset classifies as ``missing``;
    # the locator-bearing assets stay ``unsupported``.
    del registry["assets"]["toccata-fugue"]["file"]
    return replace(
        snapshot,
        registry=registry,
        registry_sha256=sha256_bytes(canonical_json_bytes(registry)),
    )


def _pack_inputs_for(snapshot: TimelineSnapshot) -> dict:
    model = build_model(snapshot)
    identity_map = build_identity_map(
        model,
        root_sns=model.snapshot_sns,
        timeline_uuid=model.timeline_uuid,
        timeline_ulid=model.timeline_ulid,
    )
    scope = select_scope(model, kind="timeline")
    pages = layout_timeline(model, identity_map, scope, layout="time-scaled")
    return {
        "model": model,
        "identity_map": identity_map,
        "snapshot": snapshot,
        "scope": scope,
        "ground_truth": emit_ground_truth(model, identity_map, snapshot),
        "action_index": emit_action_index(model, identity_map, snapshot, MANIFEST_ARGV_PATH),
        "asset_index": emit_asset_index(model, identity_map, snapshot),
        "transcript_index": emit_transcript_index(model, identity_map, snapshot),
        "diagnostics": emit_diagnostics(model, identity_map, snapshot),
        "reading_guide": emit_reading_guide(model, identity_map, snapshot),
        "structure_md": emit_structure_md(model, identity_map, snapshot),
        "metric_definitions": emit_metric_definitions(model, identity_map, snapshot),
        "pages": pages,
        "svg_bytes": {},
        "png_bytes": {page.page_id: render_page_png(page) for page in pages},
        "filmstrips": {},
    }


_GT_KEY_TO_METRIC_IDS = {
    "authored_visual_only_end_seconds": {"authored_visual_only_end_seconds"},
    "frame_quantized_visual_end": {
        "frame_quantized_visual_end_frames",
        "frame_quantized_visual_end_seconds",
    },
    "all_track_composition": {
        "all_track_composition_frames",
        "all_track_composition_seconds",
    },
    "authored_all_track_composition": {
        "authored_all_track_composition_frames",
        "authored_all_track_composition_seconds",
    },
    "fps": {"fps"},
    "at_seconds": {"clip_authored_interval"},
    "start_frame": {"clip_frame_interval"},
    "end_frame": {"clip_frame_interval"},
    "duration_seconds": {"clip_source_duration_seconds"},
    "resolved_duration_frames": {"transition_resolved_duration_frames"},
    "effective_interval": {"clip_effective_interval"},
}


def _ground_truth_metric_keys(ground_truth: dict) -> set[str]:
    keys: set[str] = set()
    for timeline in ground_truth["timelines"]:
        keys.update(timeline["durations"].keys())
        for clip in timeline["clips"]:
            keys.update(("at_seconds", "start_frame", "end_frame"))
            keys.update(_GT_KEY_TO_METRIC_IDS.keys() & set(clip["source_bounds"].keys()))
            if clip.get("transition"):
                keys.update(("resolved_duration_frames", "effective_interval"))
    for snapshot in ground_truth["snapshots"]:
        keys.add("fps")
    return keys


def test_metric_definitions_artifact_shipped_validated_and_covering(
    pack_inputs, tmp_path
) -> None:
    _layout, out_root = _write_pack(tmp_path, **pack_inputs)

    path = out_root / "metric-definitions.json"
    assert path.is_file()
    document = json.loads(path.read_text(encoding="utf-8"))
    _validate("metric-definitions", document)

    assert document["schema_version"] == 1
    assert document["kind"] == "timeline_visualize_metric_definitions"
    assert document["compositor_version"] == "0.0.6"
    ids = [metric["id"] for metric in document["metrics"]]
    assert len(ids) == len(set(ids))  # unique ids
    for metric in document["metrics"]:
        assert metric["unit"] in {"frames", "seconds", "frames_per_second"}
        assert metric["scope"] in {"per-timeline", "per-scope"}
        assert "duration.py" in metric["derivation"] or "model.py" in metric["derivation"]

    # Every metric name referenced in ground-truth values is defined.
    ground_truth = json.loads((out_root / "ground-truth.json").read_text(encoding="utf-8"))
    referenced: set[str] = set()
    for key in _ground_truth_metric_keys(ground_truth):
        referenced.update(_GT_KEY_TO_METRIC_IDS[key])
    assert referenced
    assert referenced <= set(ids)

    # Deterministic: re-emission is byte-identical.
    assert emit_metric_definitions(
        pack_inputs["model"], pack_inputs["identity_map"], pack_inputs["snapshot"]
    ) == document

    # pack-hashes.json coverage references it and hashes it.
    pack_hashes = json.loads((out_root / PACK_HASHES_NAME).read_text(encoding="utf-8"))
    assert pack_hashes["coverage"]["metric_definitions"] == "metric-definitions.json"
    assert "metric-definitions.json" in pack_hashes["files"]

    # Manifest outputs document it (the closed entrypoints object cannot grow).
    manifest = json.loads((out_root / "manifest.json").read_text(encoding="utf-8"))
    assert any(record["path"] == "metric-definitions.json" for record in manifest["outputs"])

    # The reading guide points at the sibling artifact.
    guide = (out_root / "reading-guide.md").read_text(encoding="utf-8")
    assert "metric-definitions.json" in guide


# ---------------------------------------------------------------------------
# 9. R13 containment (V3): no absolute project paths in diagnostics/warnings.
# ---------------------------------------------------------------------------


def test_missing_asset_pack_emits_no_absolute_paths(tmp_path: Path) -> None:
    snapshot = _prepared_snapshot_with_missing_asset()
    inputs = _pack_inputs_for(snapshot)
    _layout, out_root = _write_pack(tmp_path, **inputs)

    root_str = str(tmp_path.resolve())

    # The asset without an object id is reported (MISSING_MEDIA) with a reason
    # that names no path; the locator-bearing assets are UNSUPPORTED_MEDIA.
    diagnostics = json.loads((out_root / "diagnostics.json").read_text(encoding="utf-8"))
    missing = [item for item in diagnostics["diagnostics"] if item["code"] == "MISSING_MEDIA"]
    assert [item["object_ref"] for item in missing] == ["TL01.AS05"]
    for item in missing:
        assert "object_id is required" in item["message"]
        assert root_str not in item["message"]
    unsupported = [
        item["object_ref"]
        for item in diagnostics["diagnostics"]
        if item["code"] == "UNSUPPORTED_MEDIA"
    ]
    assert unsupported == ["TL01.AS01", "TL01.AS02", "TL01.AS03", "TL01.AS04"]

    # Scan EVERY emitted JSON and MD file for the absolute output path.
    for rel in sorted(out_root.rglob("*")):
        if not rel.is_file() or rel.suffix not in (".json", ".md"):
            continue
        text = rel.read_text(encoding="utf-8")
        assert root_str not in text, f"{rel.relative_to(out_root)} leaks an absolute path"

    # Manifest warnings copy the sanitized diagnostics messages.
    manifest = json.loads((out_root / "manifest.json").read_text(encoding="utf-8"))
    assert any("MISSING_MEDIA" in warning for warning in manifest["warnings"])
    for warning in manifest["warnings"]:
        assert root_str not in warning

    # Runtime-managed media has no local path: the asset index never records one.
    asset_index = json.loads((out_root / "asset-index.json").read_text(encoding="utf-8"))
    states = {asset["stable_id"]: asset["integrity_state"] for asset in asset_index["assets"]}
    assert states == {
        "AS01": "unsupported",
        "AS02": "unsupported",
        "AS03": "unsupported",
        "AS04": "unsupported",
        "AS05": "missing",
    }
    assert all(asset["contained_path"] is None for asset in asset_index["assets"])


def test_diagnostic_sanitizer_redacts_space_and_colon_paths() -> None:
    """R13 containment: absolute paths with spaces or colon prefixes redact."""
    from astrid.packs.rendering.actions.timeline_visualize.emit import (
        _sanitize_diagnostic_message,
    )

    cases = [
        ("failed:/absolute/project/path", "failed:[redacted-path]"),
        ("MEDIA_MISSING: file not found: /Users/me/p/x.png", "MEDIA_MISSING: file not found: [redacted-path]"),
        ("MEDIA_MISSING:/My Project/sources/a.png", "MEDIA_MISSING:[redacted-path]"),
        ("warning: /tmp/My Folder/sub/file.txt gone", "warning: [redacted-path]"),
        ("missing", "missing"),
        ("", ""),
    ]
    for raw, expected in cases:
        assert _sanitize_diagnostic_message(raw) == expected, (raw, _sanitize_diagnostic_message(raw))
