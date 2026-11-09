"""Explicit fallbacks for uncuttable visual crossers (over-row placement and provenance)."""
from __future__ import annotations


from astrid.sdk.timeline_restructure import describe, differences, flatten, plan, split
from tests.sdk.test_timeline_restructure import flat_film, published


def test_uncuttable_keyframed_clips_use_noncolliding_over_row_and_round_trip_provenance():
    flat = flat_film()
    tracks = flat["parent"]["config"]["tracks"]
    tracks.append({"id": "picture-over", "kind": "visual", "opacity": 0.6})
    tracks[0]["opacity"] = 0.8  # the occurrence row supplies only the insertion position
    flat["shots"]["film"]["internal_timeline"]["tracks"][1]["opacity"] = 0.4
    clips = flat["shots"]["film"]["internal_timeline"]["clips"]
    clips.append({"id": "footage-cross", "name": "Footage", "clipType": "text-card", "track": "sprite",
                  "asset": "R", "at": 3.5, "hold": 1.0, "params": {"in": 4, "zoom": 2, "keyframes": [{"frame": 0}]}})

    planned = plan(flat)
    fallback = next(c for c in planned.film_clips if c["id"] == "footage-cross")
    assert fallback["track"] == "picture-over-2"
    over_definition = next(track for track in planned.tracks if track["id"] == "picture-over-2")
    assert over_definition["opacity"] == 0.4
    assert fallback["app"]["astrid"]["timeline_restructure_over"] == {"track": "sprite", "row": "picture"}
    assert [t["id"] for t in planned.tracks][:3] == ["picture-over-2", "picture", "picture-over"]
    report = "\n".join(describe(flat))
    assert "footage-cross (Footage) [text-card] -> picture-over-2" in report

    stored = published(split(flat))
    flattened = flatten(stored)
    restored = next(c for c in flattened["shots"][flattened["placements"][0]["shot_id"]]["internal_timeline"]["clips"]
                    if c["id"] == "footage-cross")
    assert restored["track"] == "sprite"
    assert "timeline_restructure_over" not in restored.get("app", {}).get("astrid", {})
    assert restored["params"] == {"in": 4, "zoom": 2, "keyframes": [{"frame": 0}]}


def test_topmost_fallback_compares_by_logical_track_but_detects_real_inversion():
    flat = flat_film()
    flat["shots"]["film"]["internal_timeline"]["clips"].append({
        "id": "top-footage", "clipType": "text-card", "track": "type", "at": 3.5,
        "hold": 1.0, "params": {"in": 0},
    })
    assert differences(flat, split(flat)) == []

    inverted = flat_film()
    clips = inverted["shots"]["film"]["internal_timeline"]["clips"]
    clips.extend([
        {"id": "lower-footage", "clipType": "text-card", "track": "sprite", "at": 3.5,
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
        "id": "fallback", "clipType": "text-card", "track": "sprite", "at": 3.5,
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


