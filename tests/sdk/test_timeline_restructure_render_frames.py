from __future__ import annotations

from astrid.sdk.timeline_restructure import (
    Chapter,
    _active_stack,
    _cut_safety_reason,
    differences,
    split,
)
from tests.sdk.test_timeline_restructure import flat_film


def _clip_frames(stack, clip_id):
    return [frame for frame, active in enumerate(stack)
            if any(row[1] == clip_id for row in active)]


def test_legacy_half_frame_cut_is_compared_on_projected_renderer_frames(monkeypatch):
    flat = flat_film()
    clip = {"id": "legacy-start-round", "clipType": "image", "track": "sprite",
            "asset": "R", "at": 3.58333334, "hold": 2.0}
    flat["shots"]["film"]["internal_timeline"]["clips"].append(clip)

    before = _active_stack(flat)
    with monkeypatch.context() as patch:
        patch.setattr("astrid.sdk.timeline_restructure._cut_safety_reason",
                      lambda *args, **kwargs: None)
        cut = split(flat)
    after = _active_stack(cut)

    assert _clip_frames(before, clip["id"]) == list(range(108, 168))
    assert _clip_frames(after, clip["id"]) == list(range(107, 167))
    assert any("frame 107" in item for item in differences(flat, cut))


def test_safe_six_decimal_roundtrip_keeps_the_same_renderer_frames():
    flat = flat_film()
    original = {"id": "safe-same-frame", "clipType": "image", "track": "sprite",
                "asset": "R", "at": 3.5, "hold": 2.0}
    flat["shots"]["film"]["internal_timeline"]["clips"].append(original)

    assert differences(flat, split(flat)) == []


def test_media_source_phase_checks_the_first_projected_piece():
    clip = {"id": "fractional-first-source", "clipType": "media", "track": "plate",
            "asset": "P", "at": 3.5083333333, "from": 0.01666666666,
            "hold": 2.0, "speed": 2.0}
    chapters = [Chapter("A", 0, 4000), Chapter("B", 4000, 8000)]

    reason = _cut_safety_reason(clip, [4.0], 30.0, chapters=chapters)

    assert reason is not None
    assert "source" in reason or "phase" in reason


def test_sprite_cut_source_clock_accounts_for_playback_speed():
    clip = {"id": "speed-sprite", "clipType": "am-sprite", "track": "sprite",
            "asset": "R", "at": 3.5, "hold": 1.0, "speed": 0.5,
            "params": {"stepFrames": 2}}
    chapters = [Chapter("A", 0, 4000), Chapter("B", 4000, 8000)]

    assert _cut_safety_reason(clip, [4.0], 30.0, chapters=chapters) is None
