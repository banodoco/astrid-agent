"""Ranged render filmstrips address frames in the timeline's clock."""

from fractions import Fraction

import pytest

from astrid.packs.rendering.actions.timeline_visualize.filmstrip_cards import (
    _extract,
    plan_filmstrip,
)
from astrid.packs.rendering.actions.timeline_visualize.render_clock import RenderClock


def _snapshot(*, fps=(24, 1), duration_frames=960, timeline_range=None):
    metadata = {}
    if timeline_range is not None:
        metadata["timeline_range"] = timeline_range
    return {
        "project_slug": "demo",
        "timeline_id": "main",
        "timeline_name": "Film",
        "render_run_id": "ranged-run",
        "video_digest": "sha256:abc",
        "fps_rational": list(fps),
        "duration_frames": duration_frames,
        "metadata": metadata,
        "clips": [],
        "scripts": [],
    }


def test_ranged_render_maps_timeline_times_and_limits_half_open_extent():
    snap = _snapshot(timeline_range={
        "start_frame": 144,
        "end_frame": 336,
        "fps_rational": [24, 1],
    })

    first = plan_filmstrip(snap, {"at": 6.0})["cards"][0]
    last = plan_filmstrip(snap, {"at": 13.96})["cards"][0]

    assert first["frame"] == 144
    assert first["time_label"] == "6.000s"
    assert last["frame"] == 335
    assert last["time_label"] == "13.958s"
    with pytest.raises(ValueError, match=r"Requested time 14.*outside the rendered range.*6.*14"):
        plan_filmstrip(snap, {"at": 14.0})
    with pytest.raises(ValueError, match=r"Requested time 3.*outside the rendered range.*6.*14"):
        plan_filmstrip(snap, {"at": 3.0})

    ranged = plan_filmstrip(snap, {"range": [6.0, 7.0], "every": 0.5})
    assert [card["frame"] for card in ranged["cards"]] == [144, 156]
    overview = plan_filmstrip(snap, {})
    assert overview["cards"]
    assert min(card["frame"] for card in overview["cards"]) >= 144
    assert max(card["frame"] for card in overview["cards"]) < 336
    assert overview["coverage"]["window_seconds"] == [6.0, 14.0]


def test_ranged_render_uses_recorded_fractional_fps_for_absolute_frame_and_label():
    fps = Fraction(30000, 1001)
    snap = _snapshot(
        fps=(fps.numerator, fps.denominator),
        duration_frames=900,
        timeline_range={
            "start_frame": 180,
            "end_frame": 420,
            "fps_rational": [fps.numerator, fps.denominator],
        },
    )

    card = plan_filmstrip(snap, {"at": 6.54})["cards"][0]

    assert card["frame"] == 196
    assert Fraction(*card["time_rational"]) == Fraction(196 * 1001, 30000)
    assert card["time_label"] == "6.540s"


def test_legacy_snapshot_without_timeline_range_keeps_zero_based_frames():
    card = plan_filmstrip(_snapshot(), {"at": 1.0})["cards"][0]

    assert card["frame"] == 24
    assert card["time_label"] == "1.000s"


def test_extraction_translates_absolute_timeline_frames_to_render_file_indices(tmp_path, monkeypatch):
    from astrid.packs.rendering.actions.timeline_visualize import filmstrip_cards

    calls = []

    def fake_run(args, **kwargs):
        calls.append(args)
        # _extract expects one output per selected frame.
        (tmp_path / "frames" / "sample-000001.jpg").write_bytes(b"frame")
        return type("Result", (), {"returncode": 0, "stderr": ""})()

    monkeypatch.setattr(filmstrip_cards.subprocess, "run", fake_run)

    clock = RenderClock(_snapshot(
        fps=(30000, 1001),
        timeline_range={"start_frame": 180, "end_frame": 420,
                        "fps_rational": [30000, 1001]},
    ))
    _extract(
        "render.mp4",
        [{"frame": 196, "image": "frame-196.jpg"}],
        tmp_path,
        clock=clock,
    )

    assert "select='eq(n,16)'" in (tmp_path / "frame-selection.txt").read_text()
    assert (tmp_path / "frame-196.jpg").read_bytes() == b"frame"
