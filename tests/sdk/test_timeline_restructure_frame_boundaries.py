"""Frame-level regressions for cuts checked against the pinned compositor."""
from __future__ import annotations

import math

import pytest

from astrid.core.timeline.cuts import clip_duration
from astrid.sdk import timeline_restructure
from astrid.sdk.timeline_restructure import differences, plan, split
from tests.sdk.test_timeline_restructure import flat_film

FPS = 30


def _pinned_rendered_interval(start: float, hold: float, fps: int = FPS) -> tuple[int, int]:
    """Return compositor frames using composition/typescript/src/lib/duration.ts.

    The pinned compositor uses secondsToFrames = Math.round(seconds * fps),
    then max(1, round(hold / speed * fps)); its Sequence starts at the rounded
    start frame. Keep this tiny adapter local so the test checks the renderer's
    independent rounding, not Astrid's timeline interval helper.
    """
    start_frame = math.floor(start * fps + 0.5)  # JavaScript Math.round for nonnegative time.
    duration_frames = max(1, math.floor(hold * fps + 0.5))
    return start_frame, start_frame + duration_frames


def _pieces(stored, source_id):
    result = []
    starts = {row["shot_id"]: row["placement"]["start_ms"] / 1000 for row in stored["placements"]}
    for shot in stored["shots"].values():
        if shot["shot_id"] not in starts:
            continue
        start = starts[shot["shot_id"]]
        for clip in shot["internal_timeline"]["clips"]:
            marker = clip.get("app", {}).get("astrid", {}).get("timeline_restructure_piece", {})
            if marker.get("id") == source_id:
                result.append((start + clip["at"], clip["hold"]))
    return sorted(result)


@pytest.mark.parametrize("clip_type", ["media", "image", "video", "still", "text", "am-sprite"])
def test_fractional_cut_that_rounds_to_an_extra_compositor_frame_is_rejected(monkeypatch, clip_type):
    flat = flat_film()
    source_id = f"duration-phase-{clip_type}"
    clip = {"id": source_id, "clipType": clip_type, "track": "sprite", "asset": "R",
            "at": 3.51, "hold": 0.51, "params": {}}
    flat["shots"]["film"]["internal_timeline"]["clips"].append(clip)

    # Construct the exact legacy cut that the old path would have made. The
    # production safety check must be restored before differences() inspects it.
    with monkeypatch.context() as bypass:
        bypass.setattr(timeline_restructure, "_cut_safety_reason", lambda *_args, **_kwargs: None)
        legacy = split(flat)

    original_interval = _pinned_rendered_interval(clip["at"], clip["hold"])
    rendered_pieces = [_pinned_rendered_interval(start, hold) for start, hold in _pieces(legacy, source_id)]
    assert original_interval == (105, 120)
    assert rendered_pieces == [(105, 120), (120, 121)]
    problems = differences(flat, legacy)
    assert problems, "the renderer's extra frame must make this unsafe legacy cut differ"

    planned = plan(flat)
    fallback = next(clip for clip in planned.film_clips if clip["id"] == source_id)
    assert fallback["track"].endswith("-over")
    assert not _pieces(split(flat), source_id)


@pytest.mark.parametrize("clip_type", ["image", "text", "am-sprite"])
def test_fractional_start_whose_cut_frame_intervals_match_remains_cuttable(clip_type):
    flat = flat_film()
    source_id = f"duration-phase-safe-{clip_type}"
    clip = {"id": source_id, "clipType": clip_type, "track": "sprite", "asset": "R",
            "at": 3.51, "hold": 1.0, "params": {}}
    flat["shots"]["film"]["internal_timeline"]["clips"].append(clip)

    stored = split(flat)
    original_interval = _pinned_rendered_interval(clip["at"], clip["hold"])
    piece_intervals = [_pinned_rendered_interval(start, hold) for start, hold in _pieces(stored, source_id)]

    assert original_interval == (105, 135)
    assert piece_intervals == [(105, 120), (120, 135)]
    assert differences(flat, stored) == []


def test_high_precision_piece_placement_is_checked_after_actual_cutter_normalization(monkeypatch):
    flat = flat_film()
    source_id = "coordinate-rounding-crosser"
    clip = {"id": source_id, "clipType": "text", "track": "sprite", "asset": "R",
            "at": 3.58333334, "hold": 2.0, "speed": 2.0, "params": {}}
    flat["shots"]["film"]["internal_timeline"]["clips"].append(clip)

    with monkeypatch.context() as bypass:
        bypass.setattr(timeline_restructure, "_cut_safety_reason", lambda *_args, **_kwargs: None)
        legacy = split(flat)

    original_interval = _pinned_rendered_interval(clip["at"], clip["hold"] / clip["speed"])
    legacy_pieces = [_pinned_rendered_interval(start, hold / clip["speed"])
                     for start, hold in _pieces(legacy, source_id)]
    assert original_interval == (108, 138)
    assert legacy_pieces == [(107, 119), (120, 138)]

    fallback = next(row for row in plan(flat).film_clips if row["id"] == source_id)
    assert fallback["track"].endswith("-over")
    assert timeline_restructure._cut_safety_reason(clip, [4.0], FPS, chapters=plan(flat).chapters) is not None


def test_nearby_high_precision_start_with_unchanged_intervals_stays_cuttable():
    flat = flat_film()
    clip = {"id": "coordinate-rounding-safe", "clipType": "text", "track": "sprite", "asset": "R",
            "at": 3.5000001, "hold": 1.0, "params": {}}
    flat["shots"]["film"]["internal_timeline"]["clips"].append(clip)

    stored = split(flat)
    assert timeline_restructure._cut_safety_reason(clip, [4.0], FPS, chapters=plan(flat).chapters) is None
    assert [_pinned_rendered_interval(start, hold) for start, hold in _pieces(stored, clip["id"])] == [
        (105, 120), (120, 135)]


@pytest.mark.parametrize("clip_type,speed,shape", [
    ("image", 1.0, "hold"), ("text", 1.0, "window"), ("media", 1.0, "window"),
    ("video", 2.0, "hold"), ("am-sprite", 1.0, "hold"),
])
def test_guard_acceptance_matches_actual_projected_intervals_around_half_frames(clip_type, speed, shape):
    # Probe both sides of each compositor half-frame threshold at >6dp. For
    # every accepted cut, the raw original and projected cutter pieces must
    # cover exactly the same frames with no overlap or gap.
    for frame in range(102, 120):
        threshold = (frame + 0.5) / FPS
        for delta in (-0.00000041, 0.00000041):
            start = threshold + delta
            duration = 4.0 - start + 0.8
            clip = {"id": "threshold", "clipType": clip_type, "track": "sprite", "asset": "R",
                    "at": start, "speed": speed, "params": {}}
            if shape == "window":
                clip.update({"from": 0.0, "to": duration * speed})
            else:
                clip["hold"] = duration * speed
            chapter_rows = plan(flat_film()).chapters
            reason = timeline_restructure._cut_safety_reason(clip, [4.0], FPS, chapters=chapter_rows)
            if reason is None:
                original = _pinned_rendered_interval(start, duration)
                chapter = next(row for row in chapter_rows if row.start_ms == 0)
                first = timeline_restructure._cutter_for(clip)(clip, start, 4.0, chapter,
                                                                 **({"fps": FPS} if clip_type == "am-sprite" else {}))
                second_chapter = next(row for row in chapter_rows if row.start_ms == 4000)
                second = timeline_restructure._cutter_for(clip)(clip, 4.0, start + duration, second_chapter,
                                                                  **({"fps": FPS} if clip_type == "am-sprite" else {}))
                projected_intervals = []
                from astrid.core.timeline.shot_composition_projection import _project_clip
                for piece, chapter_row in ((first, chapter), (second, second_chapter)):
                    projected = _project_clip(
                        piece, occurrence={"occurrence_id": "probe", "shot_id": "probe",
                                           "output_identity": {}, "stable_deep_link": {}, "source_offset": 0},
                        revision={"shot_id": "probe", "revision_id": "probe-revision",
                                  "internal_timeline_revision": {"revision_id": "probe-internal"}},
                        occurrence_start=chapter_row.start_ms / 1000,
                        occurrence_end=chapter_row.end_ms / 1000, occurrence_speed=1.0,
                        track_definitions=[], path="probe")
                    if projected:
                        projected_intervals.append(_pinned_rendered_interval(
                            projected["at"], clip_duration(projected)))
                assert projected_intervals[0][0] == original[0], (clip_type, speed, start, reason)
                assert projected_intervals[-1][1] == original[1], (clip_type, speed, start, reason)
                assert all(left[1] == right[0] for left, right in zip(projected_intervals, projected_intervals[1:])), \
                    (clip_type, speed, start, projected_intervals)
