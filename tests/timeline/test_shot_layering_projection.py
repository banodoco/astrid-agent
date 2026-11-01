"""Shot-layer grouping in both renderer-facing projection entry points."""

from __future__ import annotations

import copy
import hashlib
import json
import logging
from pathlib import Path

import pytest

from astrid.core.timeline.duration import clip_timeline_duration
from astrid.core.timeline.shot_composition_projection import (
    ShotCompositionProjectionError,
    project_runtime_parent_composition,
    project_shot_composition,
)
from tests.helpers.runtime import locate_runtime

FIXTURE = Path(__file__).parents[1] / "fixtures" / "timeline" / "shot-layering-v1.json"
# Exact Runtime conformance/fixtures/shot-layering-v1.json, shared with the App.
FIXTURE_SHA256 = "4900be369f31bd03736b7eadaf2f8595a7ce48df5218296d405162cb13cfc16c"
CASES = json.loads(FIXTURE.read_bytes())["cases"]


def test_fixture_matches_canonical_runtime_bytes():
    data = FIXTURE.read_bytes()
    assert hashlib.sha256(data).hexdigest() == FIXTURE_SHA256
    runtime = locate_runtime()
    if runtime is not None:
        canonical = runtime / "conformance" / "fixtures" / FIXTURE.name
        if canonical.is_file():
            assert data == canonical.read_bytes()


def runtime_projection(graph):
    """Translate the canonical fixture's pins to the Runtime closure boundary."""
    parent = graph["parent_composition"]
    return project_runtime_parent_composition(
        {
            "project_id": graph["project"]["project_id"],
            "timeline_id": graph["primary_timeline"]["document_id"],
            "revision_id": graph["primary_timeline"]["head"]["revision_id"],
            "payload": {
                **parent,
                "occurrences": copy.deepcopy(graph["occurrences"]),
            },
        },
        shot_revisions=[
            {**revision, "internal_timeline_revision_id": revision["internal_timeline_revision"]["revision_id"]}
            for revision in graph["shot_revisions"]
        ],
        internal_timeline_revisions=[
            {"revision_id": revision["internal_timeline_revision"]["revision_id"],
             "payload": revision["internal_timeline_revision"]["timeline"]}
            for revision in graph["shot_revisions"]
        ],
    )


@pytest.fixture(params=[project_shot_composition, runtime_projection], ids=["graph", "runtime"])
def project(request):
    return request.param


@pytest.mark.parametrize("case", CASES, ids=lambda case: case["id"])
def test_shared_fixture_projection(project, case, caplog):
    graph = copy.deepcopy(case["input"])
    before = copy.deepcopy(graph)
    with caplog.at_level(logging.WARNING, logger="astrid.core.timeline.shot_composition_projection"):
        projection = project(graph)
    expected = case["expected"]
    tracks = projection.config["tracks"]
    tracks_by_id = {track["id"]: track for track in tracks}
    assert len(tracks_by_id) == len(tracks)
    assert all(clip["track"] in tracks_by_id for clip in projection.config["clips"])
    assert [track["id"] for track in tracks] == expected["track_order"]
    assert [{"id": track["id"], **({"role": track["role"]} if "role" in track else {}),
             "app": {"group": track["app"]["group"]}}
            for track in tracks if "group" in track.get("app", {})] == expected["group_tracks"]
    assert {
        clip.get("app", {}).get("astrid_shot_composition", {}).get("source_clip_id", clip["id"]): clip["track"]
        for clip in projection.config["clips"]
    } == expected["clip_tracks"]
    assert [{"severity": record.levelname.lower(), "code": record.code, "message": record.getMessage()}
            for record in caplog.records] == expected["warnings"]
    assert graph == before
    assert projection.config["clips"][:len(graph["parent_composition"]["config"]["clips"])] == graph["parent_composition"]["config"]["clips"]
    if "track_kinds" in expected:
        assert {track["id"]: track["kind"] for track in tracks} == expected["track_kinds"]
    for start_ms, end_ms in expected.get("transparent_windows_ms", []):
        visual_tracks = {track["id"] for track in tracks if "group" in track.get("app", {})}
        assert not any(
            clip["track"] in visual_tracks
            and clip["at"] < end_ms / 1000
            and clip["at"] + clip_timeline_duration(clip) > start_ms / 1000
            for clip in projection.config["clips"]
        )


def test_source_role_visual_track_is_grouped(project):
    case = next(case for case in CASES if case["id"] == "film-picture-over-type")
    projection = project(case["input"])
    tracks = projection.config["tracks"]
    assert tracks[2] == {
        "id": "occ-vision~source-plate", "kind": "visual", "role": "output",
        "app": {"group": {"id": "occ-vision", "sourceTrack": "source-plate"}},
    }
    assert {
        "id": "occ-vision~source-plate", "role": "output",
        "app": {"group": {"id": "occ-vision", "sourceTrack": "source-plate"}},
    } in case["expected"]["group_tracks"]
    assert [track["id"] for track in tracks[:4]] == [
        "occ-vision~plate", "occ-vision~type", "occ-vision~source-plate", "picture",
    ]
    clip = next(clip for clip in projection.config["clips"] if clip["id"] == "occ-vision:vision-source-plate")
    assert clip["track"] == "occ-vision~source-plate"


def source_graph():
    path = Path(__file__).parents[1] / "fixtures" / "timeline" / "shot_composition.json"
    graph = json.loads(path.read_text(encoding="utf-8"))
    graph["occurrences"] = graph["occurrences"][:1]
    graph["occurrences"][0]["track"] = "picture"
    graph["parent_composition"] = {
        "config": {"tracks": [{"id": "picture", "kind": "visual"}], "clips": []},
        "registry": {"assets": {}},
    }
    return graph


@pytest.mark.parametrize("track_id", ["picture", "undeclared"])
def test_undeclared_visual_tracks_stay_in_the_occurrence_group(project, track_id):
    graph = source_graph()
    parent = graph["parent_composition"]["config"]
    parent["tracks"].append({"id": "audio", "kind": "audio"})
    parent["clips"] = [{"id": "loose", "clipType": "text", "track": "picture", "at": 0, "hold": 2}]
    revision = next(r for r in graph["shot_revisions"] if r["shot_id"] == "shot-alpha" and r["revision_id"] == "rev-a")
    timeline = revision["internal_timeline_revision"]["timeline"]
    timeline["tracks"] = []
    timeline["clips"] = [
        {"id": "visual-1", "clip_type": "media", "track": track_id, "at": 0, "hold": 2},
        {"id": "visual-2", "clip_type": "text", "track": track_id, "at": 0, "hold": 2},
        {"id": "audio", "clip_type": "media", "track": "audio", "at": 0, "hold": 2},
    ]
    before = copy.deepcopy(graph)
    projection = project(graph)
    assert graph == before
    assert projection.config["tracks"] == [
        {"id": f"occ-1~{track_id}", "kind": "visual", "label": track_id,
         "app": {"group": {"id": "occ-1", "sourceTrack": track_id}}},
        *parent["tracks"],
    ]
    assert [clip["track"] for clip in projection.config["clips"]] == [
        "picture", f"occ-1~{track_id}", f"occ-1~{track_id}", "audio",
    ]
    assert projection.config["clips"][0] == parent["clips"][0]


def test_missing_row_occurrences_keep_input_order_in_one_fallback_block(project, caplog):
    case = next(case for case in CASES if case["id"] == "two-shots-one-row")
    graph = copy.deepcopy(case["input"])
    for occurrence in graph["occurrences"]:
        occurrence.update(track=f"missing-{occurrence['occurrence_id']}", at_ms=0)
    before = copy.deepcopy(graph)
    with caplog.at_level(logging.WARNING, logger="astrid.core.timeline.shot_composition_projection"):
        projection = project(graph)
    assert graph == before
    assert [track["id"] for track in projection.config["tracks"]] == [
        "occ-first~plate", "occ-second~plate", "occ-second~type", "picture", "type",
    ]
    assert [track["app"]["group"]["id"] for track in projection.config["tracks"][:3]] == [
        "occ-first", "occ-second", "occ-second",
    ]
    assert [record.getMessage() for record in caplog.records] == [
        f'shot-layering: track "missing-{occurrence_id}" not found — placing occurrence "{occurrence_id}" at top of stack'
        for occurrence_id in ("occ-first", "occ-second")
    ]
    assert all(record.code == "timeline-patch/target-not-found" for record in caplog.records)


@pytest.mark.parametrize("case_id", ["child-audio-parent-visual", "child-visual-parent-audio"])
def test_child_track_kind_wins_parent_collision_for_playback(project, case_id):
    case = next(case for case in CASES if case["id"] == case_id)
    graph = copy.deepcopy(case["input"])
    parent = graph["parent_composition"]["config"]
    timeline = graph["shot_revisions"][0]["internal_timeline_revision"]["timeline"]
    before = copy.deepcopy(graph)
    projection = project(graph)
    assert graph == before
    parent_ids = {track["id"] for track in parent["tracks"]}
    assert [track for track in projection.config["tracks"] if track["id"] in parent_ids] == parent["tracks"]
    projected_tracks = {track["id"]: track for track in projection.config["tracks"]}
    child_tracks = {track["id"]: track for track in timeline["tracks"]}
    source_clips = {clip["id"]: clip for clip in timeline["clips"]}
    audio_collision = case_id == "child-audio-parent-visual"
    expected_timing = {
        "trim-inherited": (0.125, 2.125, None, None, 2),
        "hold-inherited": (0.125, None, 2, None, 2),
        "trim-child-speed": (0.125, 4.125, None, 2, 2),
        "hold-child-speed": (0.125, None, 4, 2, 2),
        "compatible-audio": (0.125, 2.125, None, None, 2),
    }
    if not audio_collision:
        expected_timing.update({
            "trim-inherited": (0.475, 2.475, None, 1.5, 4 / 3),
            "hold-inherited": (0.475, None, 3, 1.5, 2),
            "trim-child-speed": (0.475, 4.475, None, 2, 2),
            "hold-child-speed": (0.475, None, 4, 2, 2),
        })
    for clip in projection.config["clips"][len(parent["clips"]):]:
        provenance = clip["app"]["astrid_shot_composition"]
        source = source_clips[provenance["source_clip_id"]]
        target = projected_tracks[clip["track"]]
        assert target["kind"] == child_tracks[source["track"]]["kind"]
        assert ("group" in target.get("app", {})) == (target["kind"] == "visual")
        assert provenance["source_offset"] == 350
        assert provenance["speed"] == 1.5
        source_id = provenance["source_clip_id"]
        expected_from, expected_to, expected_hold, expected_speed, expected_duration = expected_timing[source_id]
        assert clip["at"] == pytest.approx(1)
        assert clip["from"] == pytest.approx(expected_from), source_id
        if expected_to is not None:
            assert clip["to"] == pytest.approx(expected_to), source_id
        if expected_hold is not None:
            assert clip["hold"] == pytest.approx(expected_hold), source_id
        assert clip.get("speed") == expected_speed, source_id
        assert clip_timeline_duration(clip) == pytest.approx(expected_duration), source_id


@pytest.mark.parametrize("reverse", [False, True])
def test_track_resolution_is_independent_of_other_occurrences(project, reverse):
    case = next(case for case in CASES if case["id"] == "two-shots-one-row")
    graph = copy.deepcopy(case["input"])
    for index, revision in enumerate(graph["shot_revisions"]):
        timeline = revision["internal_timeline_revision"]["timeline"]
        timeline["tracks"] = [] if index == 0 else [{"id": "shared", "kind": "audio"}]
        timeline["clips"] = [{
            "id": f"clip-{index}", "clip_type": "media", "track": "shared",
            "at": 0, "from": 0.125, "to": 4.125,
        }]
    for occurrence in graph["occurrences"]:
        occurrence.update(at_ms=0, source_offset=350, speed=1.5)
    if reverse:
        graph["occurrences"].reverse()
        for index, occurrence in enumerate(graph["occurrences"]):
            occurrence["ordinal"] = index
    before = copy.deepcopy(graph)
    projection = project(graph)
    assert graph == before
    tracks = {track["id"]: track for track in projection.config["tracks"]}
    clips = {clip["shot_occurrence_id"]: clip for clip in projection.config["clips"]}
    visual, audio = clips["occ-first"], clips["occ-second"]
    assert visual["track"] == "occ-first~shared"
    assert tracks[visual["track"]]["kind"] == "visual"
    assert audio["track"] == "shared"
    assert tracks[audio["track"]]["kind"] == "audio"
    assert visual["speed"] == 1.5
    assert visual["from"] == pytest.approx(0.475)
    assert visual["to"] == pytest.approx(3.475)
    assert "speed" not in audio
    assert audio["from"] == 0.125
    assert audio["to"] == 2.125
    assert all(clip_timeline_duration(clip) == pytest.approx(2) for clip in clips.values())


@pytest.mark.parametrize("child_speed", [None, 2])
@pytest.mark.parametrize("visual_clip_type", ["media", "audio"])
def test_grouping_preserves_media_specific_speed_and_source_offset(project, child_speed, visual_clip_type):
    graph = source_graph()
    graph["occurrences"][0].update(speed=1.5, source_offset=350)
    revision = next(r for r in graph["shot_revisions"] if r["shot_id"] == "shot-alpha" and r["revision_id"] == "rev-a")
    timeline = revision["internal_timeline_revision"]["timeline"]
    timeline["registry"] = {"assets": {"alpha-audio": {"type": "audio/wav"}}}
    timeline["clips"] = [
        {"id": "visual", "clip_type": visual_clip_type, "track": "video",
         "asset_id": "alpha-audio", "at": 0, "from": 0.125, "to": 4.125},
        {"id": "audio", "clip_type": "media", "track": "audio",
         "asset_id": "alpha-audio", "at": 0, "from": 0.125, "to": 4.125},
    ]
    if child_speed is not None:
        for clip in timeline["clips"]:
            clip["speed"] = child_speed
    before = copy.deepcopy(graph)
    projection = project(graph)
    assert graph == before
    visual, audio = projection.config["clips"]
    assert visual["track"] == "occ-1~video"
    assert audio["track"] == "audio"
    assert projection.config["tracks"][0]["app"]["group"] == {
        "id": "occ-1", "sourceTrack": "video",
    }
    assert visual["speed"] == (child_speed or 1.5)
    assert audio.get("speed") == child_speed
    assert visual["from"] == pytest.approx(0.475)
    assert audio["from"] == 0.125
    assert visual["to"] == pytest.approx(0.475 + 2 * (child_speed or 1.5))
    assert audio["to"] == pytest.approx(0.125 + 2 * (child_speed or 1))
    for clip in (visual, audio):
        assert clip_timeline_duration(clip) == pytest.approx(2)
        provenance = clip["app"]["astrid_shot_composition"]
        assert provenance["source_offset"] == 350
        assert provenance["speed"] == 1.5


@pytest.mark.parametrize("placement", [
    {"transform": {"opacity": 0}}, {"transform": {"scale": 1.1}},
    {"transform": {"x": 0}}, {"transform": {"y": 0}},
    {"transform": {"width": 100}}, {"transform": {"height": 100}},
    {"transform": {"cropCustom": 0}}, {"opacity": 0},
])
@pytest.mark.parametrize("blank", [False, True])
def test_visual_placement_transform_is_refused_even_for_empty_shots(project, placement, blank):
    graph = source_graph()
    graph["occurrences"][0].update(placement)
    if blank:
        for revision in graph["shot_revisions"]:
            revision["internal_timeline_revision"]["timeline"]["clips"] = []
    with pytest.raises(ShotCompositionProjectionError) as exc:
        project(graph)
    assert str(exc.value) == (
        "Shot opacity and transforms are not supported yet. "
        "Remove them from the shot placement to render this timeline."
    )


@pytest.mark.parametrize("placement", [
    {}, {"transform": {}}, {"transform": None, "opacity": None},
    {"transform": {"opacity": 1}}, {"transform": {"scale": 1}},
    {"opacity": 1}, {"transform": {"opaque": True}},
])
def test_noop_placement_transform_is_accepted_and_keeps_child_geometry_and_audio(project, placement):
    graph = source_graph()
    graph["occurrences"][0].update(placement, gain=0.5, muted=True)
    revision = next(r for r in graph["shot_revisions"] if r["shot_id"] == "shot-alpha" and r["revision_id"] == "rev-a")
    timeline = revision["internal_timeline_revision"]["timeline"]
    timeline["tracks"][0]["app"] = {"keep": [1, 2]}
    timeline["clips"][0].update(x=10, width=200, opacity=0.4)
    before = copy.deepcopy(graph)
    projection = project(graph)
    assert graph == before
    visual, audio = projection.config["clips"]
    assert visual["track"] == "occ-1~video"
    assert {key: visual[key] for key in ("x", "width", "opacity")} == {"x": 10, "width": 200, "opacity": 0.4}
    assert visual["volume"] == 0
    assert audio["track"] == "audio"
    assert audio["hold"] == 2
    assert audio["volume"] == 0
    assert projection.config["tracks"][0]["app"] == {
        "keep": [1, 2], "group": {"id": "occ-1", "sourceTrack": "video"},
    }
