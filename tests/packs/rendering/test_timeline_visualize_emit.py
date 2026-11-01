"""R9 acceptance: every emitted semantic artifact validates and joins cleanly."""

from __future__ import annotations

import hashlib
import json
import re
from copy import deepcopy
from dataclasses import replace
from datetime import date, datetime
from pathlib import Path

import pytest
from jsonschema import Draft202012Validator
from referencing import Registry, Resource

from astrid.core.timeline.snapshot import TimelineSnapshot, snapshot_from_runtime
from astrid.packs.rendering.actions.timeline_visualize.emit import (
    emit_action_index,
    emit_asset_index,
    emit_diagnostics,
    emit_ground_truth,
    emit_reading_guide,
    emit_structure_md,
    emit_transcript_index,
)
from astrid.packs.rendering.actions.timeline_visualize.model import (
    TimelineInspectionModel,
    build_model,
)
from astrid.packs.rendering.actions.timeline_visualize.navigation import (
    build_identity_map,
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
PROJECT_SLUG = "desert-plant-growth"
TRUTH = json.loads((FIXTURE_ROOT / "desert_truth.json").read_text(encoding="utf-8"))

MANIFEST_PATH = "/tmp/agent-view/manifest.json"

_QUALIFIED_RE = re.compile(r"^TL\d{2,}(?:\.(?:SH|RG|CL|AS|TS|SP)\d{2,})?$")
_TIMESTAMP_RE = re.compile(r"^TL\d{2,}@(?:[0-9]{2,}:)?[0-5][0-9]:[0-5][0-9](?:\.[0-9]{3})?$")


def _digest(value: object) -> str:
    return sha256_bytes(canonical_json_bytes(value))


def _prepared_snapshot(tmp_path: Path) -> tuple[TimelineSnapshot, Path]:
    """Portable runtime slice; media classification comes from runtime admission only."""
    events = [
        json.loads(line)
        for line in (SLICE_DIR / "assembly.jsonl").read_text(encoding="utf-8").splitlines()
    ]
    identity = TRUTH["timeline"]
    snapshot = snapshot_from_runtime(
        timeline_id=identity["uuid"],
        timeline_ulid=identity["ulid"],
        slug=identity["slug"],
        project_slug=PROJECT_SLUG,
        events=events,
    )
    return snapshot, tmp_path


@pytest.fixture
def desert(
    tmp_path: Path,
) -> tuple[TimelineInspectionModel, object, TimelineSnapshot, Path]:
    snapshot, project_root = _prepared_snapshot(tmp_path)
    model = build_model(snapshot)
    identity_map = build_identity_map(
        model,
        root_sns=model.snapshot_sns,
        timeline_uuid=model.timeline_uuid,
        timeline_ulid=model.timeline_ulid,
    )
    return model, identity_map, snapshot, project_root


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


def _emit_all(model, identity_map, snapshot):
    ground = emit_ground_truth(model, identity_map, snapshot)
    actions = emit_action_index(model, identity_map, snapshot, MANIFEST_PATH)
    assets = emit_asset_index(model, identity_map, snapshot)
    transcript = emit_transcript_index(model, identity_map, snapshot)
    diagnostics = emit_diagnostics(model, identity_map, snapshot)
    return ground, actions, assets, transcript, diagnostics


def _iter_datetimes(value):
    if isinstance(value, (datetime, date)):
        yield value
    elif isinstance(value, dict):
        for nested in value.values():
            yield from _iter_datetimes(nested)
    elif isinstance(value, (list, tuple)):
        for nested in value:
            yield from _iter_datetimes(nested)


# ---------------------------------------------------------------------------
# 1. Schema validation + frozen desert facts.
# ---------------------------------------------------------------------------


def test_every_artifact_validates_against_its_schema(desert) -> None:
    model, identity_map, snapshot, _project_root = desert
    ground, actions, assets, transcript, diagnostics = _emit_all(
        model, identity_map, snapshot
    )
    for name, instance in (
        ("ground-truth", ground),
        ("action-index", actions),
        ("asset-index", assets),
        ("transcript-index", transcript),
        ("diagnostics", diagnostics),
    ):
        _validate(name, instance)


def test_asset_index_freezes_media_type_for_extensionless_managed_locators(desert) -> None:
    model, identity_map, snapshot, _project_root = desert
    assets = emit_asset_index(model, identity_map, snapshot)["assets"]
    media_types = {
        row["canonical_ref"]["authored_id"]: row["media_type"] for row in assets
    }
    assert media_types["plant-frame-1"] == "image"
    assert media_types["toccata-fugue"] == "audio"


def test_desert_frozen_facts(desert) -> None:
    model, identity_map, snapshot, _project_root = desert
    ground = emit_ground_truth(model, identity_map, snapshot)

    timeline = ground["timelines"][0]
    assert timeline["timeline_ref"] == "TL01"
    assert timeline["durations"]["authored_visual_only_end_seconds"] == pytest.approx(13.8667)
    assert timeline["durations"]["frame_quantized_visual_end"] == {
        "frames": 332,
        "seconds": 332 / 24,
    }
    assert timeline["durations"]["all_track_composition"] == {
        "frames": 2352,
        "seconds": 98.0,
    }

    storyboard, audio = timeline["tracks"]
    assert storyboard["authored_id"] == "storyboard"
    assert (storyboard["kind"], storyboard["config_order"], storyboard["paint_order"]) == (
        "visual",
        0,
        0,
    )
    assert audio["kind"] == "audio"
    assert audio["config_order"] == 1
    assert audio["paint_order"] is None

    assert len(timeline["clips"]) == 5
    first = timeline["clips"][0]
    assert first["stable_id"] == "CL01"
    assert first["qualified_ref"] == "TL01.CL01"
    assert first["canonical_ref"]["kind"] == "clip"
    assert first["canonical_ref"]["authored_id"] == "plant-frame-1"
    assert (first["start_frame"], first["end_frame"]) == (16, 86)
    assert first["transition"] is None

    snapshot_block = ground["snapshots"][0]
    assert snapshot_block["digest"] == snapshot.sns()
    assert snapshot_block["event_head"] == {
        "version": TRUTH["event_head"]["version"],
        "last_event_id": TRUTH["event_head"]["last_event_id"],
        "last_hash": TRUTH["event_head"]["last_hash"],
    }
    assert ground["scope"] == {
        "kind": "timeline",
        "ref": "TL01",
        "start_frame": 0,
        "end_frame": 2352,
        "start_seconds": 0.0,
        "end_seconds": 98.0,
    }

    object_refs = {entry["qualified_ref"] for entry in ground["objects"]}
    assert object_refs == {
        "TL01",
        "TL01.CL01",
        "TL01.CL02",
        "TL01.CL03",
        "TL01.CL04",
        "TL01.CL05",
        "TL01.AS01",
        "TL01.AS02",
        "TL01.AS03",
        "TL01.AS04",
        "TL01.AS05",
    }


# ---------------------------------------------------------------------------
# 2. Cross-artifact join.
# ---------------------------------------------------------------------------


def test_cross_artifact_join(desert) -> None:
    model, identity_map, snapshot, _project_root = desert
    ground, actions, assets, transcript, diagnostics = _emit_all(
        model, identity_map, snapshot
    )

    ground_objects = {entry["qualified_ref"] for entry in ground["objects"]}
    ground_by_ref = {entry["qualified_ref"]: entry for entry in ground["objects"]}

    # Every display id appears in ground truth and the action index.
    assert set(actions["entries"]) == ground_objects

    # Asset index covers exactly the AS objects of the ground truth.
    asset_refs = {entry["qualified_ref"] for entry in assets["assets"]}
    assert asset_refs == {ref for ref in ground_objects if ref.startswith("TL01.AS")}

    # Canonical refs are authoritative in ground truth and repeated verbatim.
    for ref, entry in actions["entries"].items():
        assert entry["canonical_ref"] == ground_by_ref[ref]["canonical_ref"]

    # Every relation target resolves through ground truth objects.
    for ref, entry in actions["entries"].items():
        relations = entry["relations"]
        for target in (
            relations["parent"],
            relations["previous"],
            relations["next"],
            *relations["children"],
        ):
            if target is not None:
                assert target in ground_objects, f"{ref} -> {target}"

    # Clip media joins the asset index.
    clip_asset_refs = {
        asset_ref
        for timeline in ground["timelines"]
        for clip in timeline["clips"]
        for asset_ref in clip["asset_refs"]
    }
    assert clip_asset_refs == asset_refs

    # Every argv with a focus is a valid --from-view/--focus pair.
    for ref, entry in actions["entries"].items():
        for action in entry["actions"].values():
            if action["focus"] is not None:
                assert "--from-view" in action["argv"]
                assert "--focus" in action["argv"]
                assert action["argv"][action["argv"].index("--view") + 1] == "structure"
                assert action["argv"][action["argv"].index("--project") + 1] == PROJECT_SLUG
                focus_value = action["argv"][action["argv"].index("--focus") + 1]
                assert focus_value == action["focus"]
                assert _QUALIFIED_RE.fullmatch(focus_value) or _TIMESTAMP_RE.fullmatch(
                    focus_value
                )
            else:
                assert "--focus" not in action["argv"]

    # Snapshot blocks are byte-identical across every artifact.
    expected_snapshots = ground["snapshots"]
    for artifact in (actions, assets, transcript, diagnostics):
        assert artifact["snapshots"] == expected_snapshots


# ---------------------------------------------------------------------------
# 3. Desert slice: navigation relations, refresh/focus actions, and one
#    recovery action per unavailable state.
# ---------------------------------------------------------------------------


def _unavailable(actions: dict) -> list[tuple[str, str, dict]]:
    return [
        (ref, name, action)
        for ref, entry in actions["entries"].items()
        for name, action in entry["actions"].items()
        if not action["available"]
    ]


def test_desert_slice_relations_and_refresh_focus_actions(desert) -> None:
    model, identity_map, snapshot, _project_root = desert
    actions = emit_action_index(model, identity_map, snapshot, MANIFEST_PATH)

    # refresh_root reads current state; focus actions read the snapshot.
    tl01 = actions["entries"]["TL01"]["actions"]
    assert tl01["refresh_root"]["reads"] == "current"
    assert tl01["refresh_root"]["focus"] == "TL01"
    assert "--refresh-root" in tl01["refresh_root"]["argv"]
    assert tl01["focus_timestamp"]["reads"] == "snapshot"
    assert tl01["focus_timestamp"]["result_scope"] == "timestamp"

    # Clip previous/next follow deterministic same-track order.
    assert actions["entries"]["TL01.CL01"]["relations"]["previous"] is None
    assert actions["entries"]["TL01.CL01"]["relations"]["next"] == "TL01.CL02"
    assert actions["entries"]["TL01.CL04"]["relations"]["previous"] == "TL01.CL03"
    assert actions["entries"]["TL01.CL04"]["relations"]["next"] is None
    assert actions["entries"]["TL01.CL05"]["relations"]["previous"] is None
    assert actions["entries"]["TL01.AS01"]["relations"]["parent"] == "TL01"


def test_desert_slice_one_recovery_action_per_unavailable_state(desert) -> None:
    model, identity_map, snapshot, _project_root = desert
    actions = emit_action_index(model, identity_map, snapshot, MANIFEST_PATH)

    # No asset is admitted by the runtime, so each of the five assets exposes
    # exactly one unavailable action (inspect_original), and nothing else.
    unavailable = _unavailable(actions)
    assert [(ref, name) for ref, name, _ in unavailable] == [
        (f"TL01.AS0{n}", "inspect_original") for n in range(1, 6)
    ]
    for _ref, _name, action in unavailable:
        assert action["unavailable_reason"] is not None
        assert action["unavailable_reason"].startswith("unsupported")
        assert action["reads"] == "snapshot"

    # Every unavailable action anywhere carries a reason; available ones none.
    for entry in actions["entries"].values():
        for action in entry["actions"].values():
            assert (action["unavailable_reason"] is None) == action["available"]


def test_runtime_admitted_asset_keeps_inspect_original_available(desert) -> None:
    """Admitted, unsupported and missing media stay distinguishable."""
    _model, _identity_map, snapshot, _project_root = desert
    digest = hashlib.sha256(b"admitted desert frame").hexdigest()
    registry = deepcopy(snapshot.registry)
    registry["assets"]["plant-frame-1"] = {
        "media_id": "object-1",
        "content_sha256": digest,
        "type": "image/png",
    }
    # toccata-fugue: no locator and no object id -> classified missing.
    del registry["assets"]["toccata-fugue"]["file"]
    snapshot = replace(snapshot, registry=registry, registry_sha256=_digest(registry))
    model = build_model(
        snapshot,
        media_snapshot=[
            {"object_id": "object-1", "content_hash": f"sha256:{digest}", "media_type": "image/png"}
        ],
    )
    identity_map = build_identity_map(
        model,
        root_sns=model.snapshot_sns,
        timeline_uuid=model.timeline_uuid,
        timeline_ulid=model.timeline_ulid,
    )
    actions = emit_action_index(model, identity_map, snapshot, MANIFEST_PATH)

    admitted = actions["entries"]["TL01.AS01"]["actions"]["inspect_original"]
    assert admitted["available"] is True
    assert admitted["unavailable_reason"] is None
    assert admitted["reads"] == "snapshot"

    unavailable = _unavailable(actions)
    assert [ref for ref, _name, _action in unavailable] == [
        "TL01.AS02",
        "TL01.AS03",
        "TL01.AS04",
        "TL01.AS05",
    ]
    assert all(action["unavailable_reason"] for _r, _n, action in unavailable)

    codes = {
        entry["object_ref"]: entry["code"]
        for entry in emit_diagnostics(model, identity_map, snapshot)["diagnostics"]
        if entry["code"] in {"MISSING_MEDIA", "UNSUPPORTED_MEDIA"}
    }
    assert codes["TL01.AS05"] == "MISSING_MEDIA"
    assert codes["TL01.AS02"] == "UNSUPPORTED_MEDIA"


# ---------------------------------------------------------------------------
# 4. Deterministic markdown + dicts, no datetimes.
# ---------------------------------------------------------------------------


def test_reading_guide_and_structure_deterministic(desert) -> None:
    model, identity_map, snapshot, _project_root = desert
    guide = emit_reading_guide(model, identity_map, snapshot)
    structure = emit_structure_md(model, identity_map, snapshot)

    assert guide == emit_reading_guide(model, identity_map, snapshot)
    assert structure == emit_structure_md(model, identity_map, snapshot)
    assert guide and structure

    for token in ("FOCUS", "SOURCE", "TEXT", "action-index.json", "TL01.CL03"):
        assert token in guide

    assert structure.startswith("# Structure")
    assert "SNAPSHOT · TL01" in structure
    assert "suggested next actions" in structure.lower()
    assert "TL01.CL01" in structure
    assert "compositor_version: 0.0.6" in structure
    assert "transition_default_fingerprint:" in structure


def test_determinism_no_datetimes(desert) -> None:
    model, identity_map, snapshot, _project_root = desert
    first = _emit_all(model, identity_map, snapshot)
    second = _emit_all(model, identity_map, snapshot)
    for left, right in zip(first, second):
        assert left == right
        assert list(_iter_datetimes(left)) == []
        assert json.loads(json.dumps(left)) == left  # JSON-round-trippable


# ---------------------------------------------------------------------------
# 5. Empty-valid transcript index.
# ---------------------------------------------------------------------------


def test_transcript_index_empty_valid(desert) -> None:
    model, identity_map, snapshot, _project_root = desert
    transcript = emit_transcript_index(model, identity_map, snapshot)
    _validate("transcript-index", transcript)
    assert transcript["sources"] == []
    assert transcript["speech_occurrences"] == []
    assert set(transcript) == {"schema_version", "snapshots", "sources", "speech_occurrences"}


def _mini_transition_snapshot(snapshot: TimelineSnapshot) -> TimelineSnapshot:
    assembly = {
        "theme_overrides": {"visual": {"canvas": {"fps": 24}}},
        "tracks": [
            {"id": "v1", "kind": "visual", "label": "Video"},
            {"id": "a1", "kind": "audio", "label": "Audio"},
        ],
        "clips": [
            {
                "id": "c1",
                "track": "v1",
                "clipType": "media",
                "at": 0.0,
                "hold": 2.0,
                "transition": {"id": "cross-fade", "durationFrames": 4},
            },
            {"id": "c2", "track": "v1", "clipType": "media", "at": 1.5, "hold": 2.0},
            {
                "id": "c3",
                "track": "v1",
                "clipType": "media",
                "at": 5.0,
                "hold": 1.0,
                "transition": {"id": "fade"},
            },
        ],
    }
    return replace(
        snapshot,
        assembly=deepcopy(assembly),
        assembly_sha256=_digest(assembly),
        registry={"assets": {}},
        registry_sha256=_digest({"assets": {}}),
        media_hashes={},
    )


def test_transition_accepted_and_ignored(desert) -> None:
    _model, _identity_map, snapshot, _project_root = desert
    snapshot = _mini_transition_snapshot(snapshot)
    model = build_model(snapshot)
    identity_map = build_identity_map(
        model,
        root_sns=model.snapshot_sns,
        timeline_uuid=model.timeline_uuid,
        timeline_ulid=model.timeline_ulid,
    )
    ground = emit_ground_truth(model, identity_map, snapshot)
    actions = emit_action_index(model, identity_map, snapshot, MANIFEST_PATH)
    diagnostics = emit_diagnostics(model, identity_map, snapshot)
    _validate("ground-truth", ground)
    _validate("action-index", actions)
    _validate("diagnostics", diagnostics)

    clips = {
        clip["canonical_ref"]["authored_id"]: clip
        for clip in ground["timelines"][0]["clips"]
    }
    c1 = clips["c1"]["transition"]
    assert c1["state"] == "accepted"
    assert c1["requested_duration_frames"] == 4
    assert c1["resolution_source"] == "explicit_frames"
    assert c1["resolved_duration_frames"] == 4
    assert c1["effective_interval"]["start_frame"] == 0
    assert c1["effective_interval"]["end_frame"] == 44
    assert c1["ignored_reason"] is None

    c3 = clips["c3"]["transition"]
    assert c3["state"] == "ignored"
    assert c3["ignored_reason"]
    assert c3["effective_interval"] is None
    assert c3["resolved_duration_frames"] is None

    ignored = [
        entry
        for entry in diagnostics["diagnostics"]
        if entry["code"] == "TRANSITION_IGNORED"
    ]
    assert [entry["object_ref"] for entry in ignored] == ["TL01.CL03"]


# ---------------------------------------------------------------------------
# 7. R9-FIX: scope-aware emission.
# ---------------------------------------------------------------------------


def test_scope_none_matches_full_timeline(desert) -> None:
    model, identity_map, snapshot, _project_root = desert
    default = emit_ground_truth(model, identity_map, snapshot)
    explicit_none = emit_ground_truth(model, identity_map, snapshot, None)
    full = select_scope(model, kind="timeline")
    scoped = emit_ground_truth(model, identity_map, snapshot, full)

    assert default == explicit_none == scoped
    assert scoped["scope"] == {
        "kind": "timeline",
        "ref": "TL01",
        "start_frame": 0,
        "end_frame": 2352,
        "start_seconds": 0.0,
        "end_seconds": 98.0,
    }
    # The other scope-aware emitters use the same explicit full-timeline scope.
    default_actions = emit_action_index(model, identity_map, snapshot, MANIFEST_PATH)
    assert default_actions == emit_action_index(model, identity_map, snapshot, MANIFEST_PATH, full)
    assert emit_diagnostics(model, identity_map, snapshot) == emit_diagnostics(
        model, identity_map, snapshot, full
    )


def test_range_scope_emission(desert) -> None:
    model, identity_map, snapshot, _project_root = desert
    scope = select_scope(model, kind="range", ref="range-97-120", start=97.0, end=120.0)
    ground = emit_ground_truth(model, identity_map, snapshot, scope)
    actions = emit_action_index(model, identity_map, snapshot, MANIFEST_PATH, scope)
    diagnostics = emit_diagnostics(model, identity_map, snapshot, scope)
    _validate("ground-truth", ground)
    _validate("action-index", actions)
    _validate("diagnostics", diagnostics)

    # The scope block keeps the authored range bounds [2328, 2880); the clip
    # intersection is clipped to the composition at [2328, 2352).
    sc = ground["scope"]
    assert sc["kind"] == "range"
    assert sc["ref"] == "TL01.RG01"
    assert (sc["start_frame"], sc["end_frame"]) == (2328, 2880)
    assert (sc["start_seconds"], sc["end_seconds"]) == (97.0, 120.0)

    timeline = ground["timelines"][0]
    # The timeline entry stays [0, composition); only the scope block narrows.
    assert timeline["durations"]["all_track_composition"]["frames"] == 2352
    # Only the audio clip intersects [2328, 2352); visual clips end far earlier.
    assert [clip["qualified_ref"] for clip in timeline["clips"]] == ["TL01.CL05"]
    assert [asset["qualified_ref"] for asset in timeline["assets"]] == ["TL01.AS05"]

    object_refs = {entry["qualified_ref"] for entry in ground["objects"]}
    assert object_refs == {"TL01", "TL01.CL05", "TL01.AS05", "TL01.RG01"}
    rg = next(entry for entry in ground["objects"] if entry["qualified_ref"] == "TL01.RG01")
    assert rg["canonical_ref"]["kind"] == "range"
    assert rg["canonical_ref"]["authored_id"] == "range-97-120"

    # The action index covers exactly the scoped objects; refresh_root remains.
    assert set(actions["entries"]) == object_refs
    tl01 = actions["entries"]["TL01"]
    assert tl01["actions"]["refresh_root"]["available"] is True
    assert tl01["relations"]["children"] == ["TL01.CL05", "TL01.AS05", "TL01.RG01"]
    assert actions["entries"]["TL01.CL05"]["relations"]["parent"] == "TL01"
    assert actions["entries"]["TL01.CL05"]["relations"]["previous"] is None
    assert actions["entries"]["TL01.CL05"]["relations"]["next"] is None
    assert "focus_context" in actions["entries"]["TL01.RG01"]["actions"]

    # The clipping warning surfaces in diagnostics against the range object.
    clipped = [
        entry for entry in diagnostics["diagnostics"] if entry["code"] == "CLIP_RANGE_CLIPPED"
    ]
    assert len(clipped) == 1
    assert clipped[0]["message"] == "range was clipped to the composition bounds"
    assert clipped[0]["object_ref"] == "TL01.RG01"

    # Minting RG ids never mutates the caller's identity map.
    assert identity_map.lookup_semantic("range", "range-97-120") is None


def test_range_scope_reemission_is_stable(desert) -> None:
    model, identity_map, snapshot, _project_root = desert
    scope = select_scope(model, kind="range", ref="range-97-120", start=97.0, end=120.0)
    first = emit_ground_truth(model, identity_map, snapshot, scope)
    second = emit_ground_truth(model, identity_map, snapshot, scope)
    assert first == second
    assert first["scope"]["ref"] == "TL01.RG01"
    assert identity_map.lookup_semantic("range", "range-97-120") is None


def test_clip_scope_emission(desert) -> None:
    model, identity_map, snapshot, _project_root = desert
    scope = select_scope(model, kind="clip", clip_id="plant-frame-1", context_seconds=3.0)
    ground = emit_ground_truth(model, identity_map, snapshot, scope)
    actions = emit_action_index(model, identity_map, snapshot, MANIFEST_PATH, scope)
    _validate("ground-truth", ground)
    _validate("action-index", actions)

    sc = ground["scope"]
    assert sc["kind"] == "clip"
    assert sc["ref"] == "TL01.CL01"
    assert (sc["start_frame"], sc["end_frame"]) == (0, 158)
    assert (sc["start_seconds"], sc["end_seconds"]) == (0.0, 158 / 24)

    timeline = ground["timelines"][0]
    assert [clip["qualified_ref"] for clip in timeline["clips"]] == [
        "TL01.CL01",
        "TL01.CL05",
    ]
    assert {asset["qualified_ref"] for asset in timeline["assets"]} == {
        "TL01.AS01",
        "TL01.AS05",
    }
    assert {entry["qualified_ref"] for entry in ground["objects"]} == {
        "TL01",
        "TL01.CL01",
        "TL01.CL05",
        "TL01.AS01",
        "TL01.AS05",
    }

    # The selector emphasizes the focused clip; every emphasized clip is still
    # emitted (the R3 schema has no emphasis field — see the R9 report).
    assert scope.emphasized_clip_ids == ("plant-frame-1",)
    emitted_clip_ids = {clip["canonical_ref"]["authored_id"] for clip in timeline["clips"]}
    assert set(scope.emphasized_clip_ids) <= emitted_clip_ids

    # Same-track relations stay resolved: CL01's next (CL02) is out of scope.
    assert actions["entries"]["TL01.CL01"]["relations"]["previous"] is None
    assert actions["entries"]["TL01.CL01"]["relations"]["next"] is None
    assert set(actions["entries"]) == {entry["qualified_ref"] for entry in ground["objects"]}


def test_timestamp_scope_emission(desert) -> None:
    model, identity_map, snapshot, _project_root = desert
    scope = select_scope(
        model,
        kind="timestamp",
        ref="TL01@00:00:02.000",
        at_seconds=2.0,
        context_seconds=3.0,
    )
    ground = emit_ground_truth(model, identity_map, snapshot, scope)
    actions = emit_action_index(model, identity_map, snapshot, MANIFEST_PATH, scope)
    _validate("ground-truth", ground)
    _validate("action-index", actions)

    sc = ground["scope"]
    assert sc["kind"] == "timestamp"
    assert sc["ref"] == "TL01@00:00:02.000"
    assert (sc["start_frame"], sc["end_frame"]) == (0, 120)
    assert (sc["start_seconds"], sc["end_seconds"]) == (0.0, 5.0)

    timeline = ground["timelines"][0]
    assert [clip["qualified_ref"] for clip in timeline["clips"]] == [
        "TL01.CL01",
        "TL01.CL02",
        "TL01.CL05",
    ]
    # The visual clip containing the instant is the emphasis target.
    assert scope.emphasized_clip_ids == ("plant-frame-1",)
    assert set(actions["entries"]) == {entry["qualified_ref"] for entry in ground["objects"]}


def test_timestamp_scope_without_ref_anchors_on_at_seconds(desert) -> None:
    """R9-FIX3: no ref supplied → locator comes from at_seconds, not the midpoint."""
    model, identity_map, snapshot, _project_root = desert
    scope = select_scope(
        model,
        kind="timestamp",
        ref=None,
        at_seconds=2.0,
        context_seconds=3.0,
    )
    assert scope.ref is None
    ground = emit_ground_truth(model, identity_map, snapshot, scope)
    actions = emit_action_index(model, identity_map, snapshot, MANIFEST_PATH, scope)
    _validate("ground-truth", ground)
    _validate("action-index", actions)

    sc = ground["scope"]
    assert sc["kind"] == "timestamp"
    # at_seconds=2.0 wins over the [0,5)s window midpoint (which would be 2.5).
    assert sc["ref"] == "TL01@00:00:02.000"
    assert (sc["start_frame"], sc["end_frame"]) == (0, 120)
    assert (sc["start_seconds"], sc["end_seconds"]) == (0.0, 5.0)
    assert set(actions["entries"]) == {entry["qualified_ref"] for entry in ground["objects"]}


def test_timestamp_scope_outside_bounds_warning(desert) -> None:
    model, identity_map, snapshot, _project_root = desert
    scope = select_scope(
        model,
        kind="timestamp",
        ref="TL01@00:01:40.000",
        at_seconds=100.0,
        context_seconds=3.0,
    )
    ground = emit_ground_truth(model, identity_map, snapshot, scope)
    diagnostics = emit_diagnostics(model, identity_map, snapshot, scope)
    _validate("ground-truth", ground)
    _validate("diagnostics", diagnostics)

    assert (ground["scope"]["start_frame"], ground["scope"]["end_frame"]) == (2328, 2352)
    warnings = [
        entry
        for entry in diagnostics["diagnostics"]
        if entry["code"] == "TIMESTAMP_CONTEXT_CLIPPED"
    ]
    assert len(warnings) == 1
    assert warnings[0]["message"] == "timestamp lies outside the composition bounds"
    # Locators are not valid diagnostics refs; the warning reports on TL01.
    assert warnings[0]["object_ref"] == "TL01"


def test_asset_scope_emission(desert) -> None:
    model, identity_map, snapshot, _project_root = desert
    scope = select_scope(model, kind="asset", asset_key="toccata-fugue")
    ground = emit_ground_truth(model, identity_map, snapshot, scope)
    _validate("ground-truth", ground)

    sc = ground["scope"]
    assert sc["kind"] == "asset"
    assert sc["ref"] == "TL01.AS05"
    assert (sc["start_frame"], sc["end_frame"]) == (12, 2352)
    assert (sc["start_seconds"], sc["end_seconds"]) == (0.5, 98.0)
    assert [clip["qualified_ref"] for clip in ground["timelines"][0]["clips"]] == ["TL01.CL05"]
    assert {entry["qualified_ref"] for entry in ground["objects"]} == {
        "TL01",
        "TL01.CL05",
        "TL01.AS05",
    }


# ---------------------------------------------------------------------------
# 8. R9-FIX2: per-clip emphasis + structured scope warning codes.
# ---------------------------------------------------------------------------


def _clips_by_authored_id(ground: dict) -> dict[str, dict]:
    return {
        clip["canonical_ref"]["authored_id"]: clip
        for clip in ground["timelines"][0]["clips"]
    }


def test_full_timeline_omits_emphasized_field(desert) -> None:
    """Unscoped (and empty-emphasis) emissions never carry the field."""
    model, identity_map, snapshot, _project_root = desert
    ground = emit_ground_truth(model, identity_map, snapshot)
    _validate("ground-truth", ground)

    for clip in ground["timelines"][0]["clips"]:
        assert "emphasized" not in clip


def test_clip_scope_marks_target_emphasized(desert) -> None:
    model, identity_map, snapshot, _project_root = desert
    scope = select_scope(model, kind="clip", clip_id="plant-frame-1", context_seconds=3.0)
    ground = emit_ground_truth(model, identity_map, snapshot, scope)
    _validate("ground-truth", ground)

    clips = _clips_by_authored_id(ground)
    assert clips["plant-frame-1"]["emphasized"] is True
    # The other context clip in scope (toccata-fugue audio) is not emphasized.
    assert clips["toccata-fugue"]["emphasized"] is False


def test_timestamp_scope_marks_active_stack_emphasized(desert) -> None:
    """The exact visual compositor stack at the instant is the emphasis set."""
    model, identity_map, snapshot, _project_root = desert
    scope = select_scope(
        model,
        kind="timestamp",
        ref="TL01@00:00:02.000",
        at_seconds=2.0,
        context_seconds=3.0,
    )
    ground = emit_ground_truth(model, identity_map, snapshot, scope)
    _validate("ground-truth", ground)

    assert scope.emphasized_clip_ids == ("plant-frame-1",)
    clips = _clips_by_authored_id(ground)
    assert clips["plant-frame-1"]["emphasized"] is True
    # Context contributors (neighbor visual clip + audio bed) are false.
    assert clips["plant-frame-2"]["emphasized"] is False
    assert clips["toccata-fugue"]["emphasized"] is False


def test_scope_warning_code_clip_absent_from_snapshot(desert) -> None:
    model, identity_map, snapshot, _project_root = desert
    scope = select_scope(model, kind="clip", clip_id="ghost-clip")
    diagnostics = emit_diagnostics(model, identity_map, snapshot, scope)
    _validate("diagnostics", diagnostics)

    matching = [
        entry
        for entry in diagnostics["diagnostics"]
        if "ghost-clip" in entry["message"]
    ]
    assert len(matching) == 1
    assert matching[0]["code"] == "CLIP_ABSENT_FROM_SNAPSHOT"
    assert matching[0]["message"] == "clip 'ghost-clip' is not present in the snapshot"
    assert {entry["code"] for entry in diagnostics["diagnostics"]} != {"SCOPE_WARNING"}


def test_scope_warning_code_asset_no_clip_uses(desert) -> None:
    model, identity_map, snapshot, _project_root = desert
    scope = select_scope(model, kind="asset", asset_key="ghost-asset")
    diagnostics = emit_diagnostics(model, identity_map, snapshot, scope)
    _validate("diagnostics", diagnostics)

    matching = [
        entry
        for entry in diagnostics["diagnostics"]
        if "ghost-asset" in entry["message"]
    ]
    assert len(matching) == 1
    assert matching[0]["code"] == "ASSET_NO_CLIP_USES"
    assert matching[0]["message"] == "asset 'ghost-asset' has no clip uses in the snapshot"
    assert {entry["code"] for entry in diagnostics["diagnostics"]} != {"SCOPE_WARNING"}


def test_scope_warning_code_shot_groups_absent(desert) -> None:
    model, identity_map, snapshot, _project_root = desert
    scope = select_scope(model, kind="shot", ref="ghost-shot")
    diagnostics = emit_diagnostics(model, identity_map, snapshot, scope)
    _validate("diagnostics", diagnostics)

    matching = [
        entry
        for entry in diagnostics["diagnostics"]
        if "ghost-shot" in entry["message"]
    ]
    assert len(matching) == 1
    assert matching[0]["code"] == "SHOT_GROUPS_ABSENT"
    assert (
        matching[0]["message"]
        == "pinned shot 'ghost-shot' is unavailable; timeline.pinnedShotGroups has no match"
    )
    assert {entry["code"] for entry in diagnostics["diagnostics"]} != {"SCOPE_WARNING"}


def _boundsless_shot_snapshot(snapshot: TimelineSnapshot) -> TimelineSnapshot:
    """Minimal valid assembly with one pinned shot that has no bounds/members."""
    assembly = {
        "theme_overrides": {"visual": {"canvas": {"fps": 24}}},
        "tracks": [{"id": "v1", "kind": "visual", "label": "Video"}],
        "clips": [
            {"id": "c1", "track": "v1", "clipType": "media", "at": 0.0, "hold": 2.0},
        ],
        "pinnedShotGroups": [{"shotId": "empty-shot", "clipIds": []}],
    }
    return replace(
        snapshot,
        assembly=deepcopy(assembly),
        assembly_sha256=_digest(assembly),
        registry={"assets": {}},
        registry_sha256=_digest({"assets": {}}),
        media_hashes={},
    )


def test_scope_warning_code_shot_bounds_absent(desert) -> None:
    _model, _identity_map, snapshot, _project_root = desert
    snapshot = _boundsless_shot_snapshot(snapshot)
    model = build_model(snapshot)
    identity_map = build_identity_map(
        model,
        root_sns=model.snapshot_sns,
        timeline_uuid=model.timeline_uuid,
        timeline_ulid=model.timeline_ulid,
    )
    scope = select_scope(model, kind="shot", ref="empty-shot")
    diagnostics = emit_diagnostics(model, identity_map, snapshot, scope)
    _validate("diagnostics", diagnostics)

    matching = [
        entry
        for entry in diagnostics["diagnostics"]
        if "empty-shot" in entry["message"]
    ]
    assert len(matching) == 1
    assert matching[0]["code"] == "SHOT_BOUNDS_ABSENT"
    assert (
        matching[0]["message"]
        == "pinned shot 'empty-shot' has neither valid authored bounds nor present member clips"
    )
    # The pinned shot resolves through the identity map to a qualified ref.
    assert matching[0]["object_ref"] == "TL01.SH01"
    assert {entry["code"] for entry in diagnostics["diagnostics"]} != {"SCOPE_WARNING"}
