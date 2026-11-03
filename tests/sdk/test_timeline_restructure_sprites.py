"""Faithful sprite cuts and explicit fallbacks for uncuttable visual crossers."""
from __future__ import annotations

from pathlib import Path

import jsonschema
import pytest

from astrid.core.element.schema import load_element_definition
from astrid.core.timeline.cuts import clip_duration
from astrid.sdk.timeline_restructure import describe, differences, flatten, plan, split
from tests.sdk.test_timeline_restructure import flat_film, published


def test_sprite_pieces_preserve_animation_params_and_carry_the_source_clock():
    flat = flat_film()
    flat["shots"]["film"]["internal_timeline"]["clips"].append({
        "id": "sprite-cross", "clipType": "am-sprite", "track": "sprite", "asset": "R",
        "at": 3.6333333333333333, "hold": 5.0,
        "params": {"x": 1, "y": 2, "flipX": False, "stepFrames": 2,
                    "keyframes": [{"frame": 2, "x": 4}, {"frame": 8, "y": 9},
                                  {"frame": 11, "flipX": True}, {"frame": 30, "x": 12}],
                    "frames": {"frameWidth": 16, "frameHeight": 16, "count": 4, "fps": 8},
                    "blinkAt": [10, 18, 40], "silhouette": {"until": 35},
                    "outline": {"px": 1, "pulse": 6}, "enter": "slideIn"},
    })

    planned = plan(flat)
    pieces = [c for chapter in planned.chapters for c in chapter.clips
              if c.get("app", {}).get("astrid", {}).get("timeline_restructure_piece", {}).get("id") == "sprite-cross"]

    assert len(pieces) == 3
    intervals = sorted((chapter.start_ms / 1000 + piece["at"],
                        chapter.start_ms / 1000 + piece["at"] + clip_duration(piece))
                       for chapter in planned.chapters for piece in chapter.clips
                       if piece.get("app", {}).get("astrid", {}).get("timeline_restructure_piece", {}).get("id") == "sprite-cross")
    assert intervals[0][0] == pytest.approx(3.6333333333333333)
    assert intervals[-1][1] == pytest.approx(8.633333333333333)
    assert all(left[1] == pytest.approx(right[0]) for left, right in zip(intervals, intervals[1:]))
    second = pieces[1]
    params = second["params"]
    assert params["sourceClock"] == {"offsetFrames": 11, "speed": 1.0}
    assert second["speed"] == 1.0
    assert (params["x"], params["y"], params["flipX"]) == (4, 9, False)
    assert params["keyframes"] == [{"frame": 0, "flipX": True}, {"frame": 19, "x": 12}]
    assert params["blinkAt"] == [10, 18, 40]
    assert params["silhouette"] == {"until": 35}
    assert params["frames"] == {"frameWidth": 16, "frameHeight": 16, "count": 4, "fps": 8}
    assert params["outline"] == {"px": 1, "pulse": 6}
    assert params["enter"] == "slideIn"
    element = load_element_definition(
        Path(__file__).resolve().parents[2]
        / "astrid" / "packs" / "astrid_motion" / "rendering" / "elements" / "effects" / "am-sprite",
        kind="effects", source="pack:astrid_motion", editable=False, priority=30)
    for piece in pieces:
        jsonschema.validate(piece["params"], element.schema)


def test_uncuttable_keyframed_am_clips_use_noncolliding_over_row_and_round_trip_provenance():
    flat = flat_film()
    tracks = flat["parent"]["config"]["tracks"]
    tracks.append({"id": "picture-over", "kind": "visual", "opacity": 0.6})
    tracks[0]["opacity"] = 0.8  # the occurrence row supplies only the insertion position
    flat["shots"]["film"]["internal_timeline"]["tracks"][1]["opacity"] = 0.4
    clips = flat["shots"]["film"]["internal_timeline"]["clips"]
    clips.append({"id": "footage-cross", "name": "Footage", "clipType": "am-footage", "track": "sprite",
                  "asset": "R", "at": 3.5, "hold": 1.0, "params": {"in": 4, "zoom": 2, "keyframes": [{"frame": 0}]}})

    planned = plan(flat)
    fallback = next(c for c in planned.film_clips if c["id"] == "footage-cross")
    assert fallback["track"] == "picture-over-2"
    over_definition = next(track for track in planned.tracks if track["id"] == "picture-over-2")
    assert over_definition["opacity"] == 0.4
    assert fallback["app"]["astrid"]["timeline_restructure_over"] == {"track": "sprite", "row": "picture"}
    assert [t["id"] for t in planned.tracks][:3] == ["picture-over-2", "picture", "picture-over"]
    report = "\n".join(describe(flat))
    assert "footage-cross (Footage) [am-footage] -> picture-over-2" in report

    stored = published(split(flat))
    flattened = flatten(stored)
    restored = next(c for c in flattened["shots"][flattened["placements"][0]["shot_id"]]["internal_timeline"]["clips"]
                    if c["id"] == "footage-cross")
    assert restored["track"] == "sprite"
    assert "timeline_restructure_over" not in restored.get("app", {}).get("astrid", {})
    assert restored["params"] == {"in": 4, "zoom": 2, "keyframes": [{"frame": 0}]}


def test_sprite_phase_advances_at_clip_speed():
    flat = flat_film()
    flat["shots"]["film"]["internal_timeline"]["clips"].append({
        "id": "fast-sprite", "clipType": "am-sprite", "track": "sprite", "asset": "R",
        "at": 3.5, "hold": 4.0, "speed": 2.0, "params": {"x": 4, "y": 7},
    })
    planned = plan(flat)
    pieces = [c for chapter in planned.chapters for c in chapter.clips
              if c.get("app", {}).get("astrid", {}).get("timeline_restructure_piece", {}).get("id") == "fast-sprite"]
    assert pieces[1]["params"]["sourceClock"] == {"offsetFrames": 15, "speed": 2.0}
    assert pieces[1]["speed"] == 1.0
    intervals = sorted((chapter.start_ms / 1000 + piece["at"],
                        chapter.start_ms / 1000 + piece["at"] + clip_duration(piece))
                       for chapter in planned.chapters for piece in chapter.clips
                       if piece.get("app", {}).get("astrid", {}).get("timeline_restructure_piece", {}).get("id") == "fast-sprite")
    assert intervals[0][0] == pytest.approx(3.5)
    assert intervals[-1][1] == pytest.approx(5.5)
    assert all(left[1] == pytest.approx(right[0]) for left, right in zip(intervals, intervals[1:]))


@pytest.mark.parametrize("field", ["keyframes", "sourceClock"])
def test_edited_later_sprite_piece_is_not_rejoined(field):
    flat = flat_film()
    flat["shots"]["film"]["internal_timeline"]["clips"].append({
        "id": "edited-sprite", "clipType": "am-sprite", "track": "sprite", "at": 3.5,
        "hold": 5.0, "params": {"x": 1, "y": 2, "keyframes": [{"frame": 30, "x": 9}]},
    })
    stored = split(flat)
    candidates = [clip for shot in stored["shots"].values()
                  for clip in shot["internal_timeline"]["clips"]
                  if clip.get("app", {}).get("astrid", {}).get("timeline_restructure_piece", {}).get("id") == "edited-sprite"]
    later = next(clip for clip in candidates
                 if clip["app"]["astrid"]["timeline_restructure_piece"]["index"] == 1)
    if field == "sourceClock":
        later["params"]["sourceClock"]["offsetFrames"] += 1
    else:
        later["params"]["keyframes"][0]["frame"] += 1

    flattened = flatten(stored)
    clips = [clip for shot in flattened["shots"].values() for clip in shot["internal_timeline"]["clips"]]
    assert all(clip["id"] != "edited-sprite" for clip in clips)
    assert any(clip.get("app", {}).get("astrid", {}).get("timeline_restructure_piece", {}).get("id") == "edited-sprite"
               for clip in clips)


@pytest.mark.parametrize(("speed", "phase"), [(0.5, 7), (1.5, 22), (2.0, 30)])
def test_sprite_cut_phase_uses_the_renderer_source_clock(speed, phase):
    flat = flat_film()
    flat["shots"]["film"]["internal_timeline"]["clips"].append({
        "id": "clocked-sprite", "clipType": "am-sprite", "track": "sprite", "asset": "R",
        "at": 3.5, "hold": 2.0, "speed": speed, "params": {"x": 4, "y": 8, "frameOffset": 3},
    })
    planned = plan(flat)
    piece = next(c for c in planned.chapters[1].clips
                 if c.get("app", {}).get("astrid", {}).get("timeline_restructure_piece", {}).get("id") == "clocked-sprite")
    assert piece["params"]["frameOffset"] == 3
    assert piece["params"]["sourceClock"] == {"offsetFrames": 15, "speed": speed}
    assert piece["speed"] == 1.0


def test_am_snap_plate_animation_is_not_restarted_by_cutting():
    flat = flat_film()
    flat["shots"]["film"]["internal_timeline"]["clips"].append({
        "id": "plate-cross", "clipType": "am-snap-plate", "track": "plate", "asset": "P",
        "at": 3.5, "hold": 1.0, "params": {"punchAt": [2], "push": {"to": 3, "frames": 10}},
    })
    planned = plan(flat)
    clip = next(c for c in planned.film_clips if c["id"] == "plate-cross")
    assert clip["track"] == "picture-over"
    assert clip["params"] == {"punchAt": [2], "push": {"to": 3, "frames": 10}}


def test_topmost_fallback_compares_by_logical_track_but_detects_real_inversion():
    flat = flat_film()
    flat["shots"]["film"]["internal_timeline"]["clips"].append({
        "id": "top-footage", "clipType": "am-footage", "track": "type", "at": 3.5,
        "hold": 1.0, "params": {"in": 0},
    })
    assert differences(flat, split(flat)) == []

    inverted = flat_film()
    clips = inverted["shots"]["film"]["internal_timeline"]["clips"]
    clips.extend([
        {"id": "lower-footage", "clipType": "am-footage", "track": "sprite", "at": 3.5,
         "hold": 1.0, "params": {"in": 0}},
        {"id": "active-type", "clipType": "image", "track": "type", "asset": "R", "at": 3.5,
         "hold": 1.0},
    ])
    assert differences(inverted, split(inverted))


def test_repeated_split_reuses_generated_over_rows_and_keeps_source_settings():
    flat = flat_film()
    flat["parent"]["config"]["tracks"].append({"id": "picture-over", "kind": "visual", "opacity": 0.4})
    flat["shots"]["film"]["internal_timeline"]["tracks"][1]["opacity"] = 0.7
    flat["shots"]["film"]["internal_timeline"]["clips"].append({
        "id": "fallback", "clipType": "am-footage", "track": "sprite", "at": 3.5,
        "hold": 1.0, "params": {"in": 0},
    })
    first = split(flat)
    first_tracks = first["parent"]["config"]["tracks"]
    generated = [track for track in first_tracks if track.get("app", {}).get("astrid", {}).get("timeline_restructure_over_row")]
    assert len(generated) == 1 and generated[0]["id"] == "picture-over-2"
    assert generated[0]["opacity"] == 0.7

    second = split(flatten(first))
    second_generated = [track for track in second["parent"]["config"]["tracks"]
                        if track.get("app", {}).get("astrid", {}).get("timeline_restructure_over_row")]
    assert [track["id"] for track in second_generated] == ["picture-over-2"]


def test_sprite_pose_uses_sorted_sparse_keys_and_pending_keys_stay_admissible():
    flat = flat_film()
    clips = flat["shots"]["film"]["internal_timeline"]["clips"]
    clips.extend([
        {"id": "unsorted", "clipType": "am-sprite", "track": "sprite", "at": 3.6333333333333333,
         "hold": 2, "params": {"stepFrames": 2, "keyframes": [{"frame": 8, "x": 9}, {"frame": 2, "x": 4}]}},
        {"id": "pending", "clipType": "am-sprite", "track": "sprite", "at": 3.6333333333333333,
         "hold": 2, "params": {"stepFrames": 3, "keyframes": [{"frame": 10, "x": 7}]}},
    ])
    pieces = {clip["app"]["astrid"]["timeline_restructure_piece"]["id"]: clip
              for clip in plan(flat).chapters[1].clips
              if clip.get("app", {}).get("astrid", {}).get("timeline_restructure_piece", {}).get("id")
              in {"unsorted", "pending"}}
    assert pieces["unsorted"]["params"]["x"] == 9
    assert pieces["pending"]["params"]["keyframes"] == [{"frame": 0, "x": 7}]
    assert pieces["pending"]["params"].get("x", 0) == 0
    assert differences(flat, split(flat)) == []


@pytest.mark.parametrize("length", [{"hold": 4}, {"duration": 4}, {"from": 2, "to": 6}])
def test_sprite_lengths_and_frame_zero_keys_round_trip_at_speed(length):
    flat = flat_film()
    original = {"id": "length-shape", "clipType": "am-sprite", "track": "sprite", "at": 3.5,
                "speed": 2, "params": {"keyframes": [{"frame": 0, "x": 2}, {"frame": 30, "x": 8}]}, **length}
    flat["shots"]["film"]["internal_timeline"]["clips"].append(original)
    stored = split(flat)
    assert differences(flat, stored) == []
    back = flatten(stored)
    restored = next(clip for shot in back["shots"].values()
                    for clip in shot["internal_timeline"]["clips"] if clip["id"] == "length-shape")
    assert restored == original
