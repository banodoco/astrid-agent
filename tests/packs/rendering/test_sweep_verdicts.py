"""The whole-film sweep (F-081: a re-flow left 6 s of black), on the tip's decode-the-two-renders design.

The freeze's sweep captured frames through a Python worker and skipped cuts whose frame inputs matched; the
tip decodes bounded samples from the exact base and working managed renders, so those internals are not
revived. What stays pinned is what the person reads: which cut changed, which change was not expected, and
an honest "black frame" label when a re-flow leaves a gap.
"""
from __future__ import annotations

from pathlib import Path

from PIL import Image, ImageDraw

from astrid.packs.rendering.actions.timeline_visualize import fast_lane
from astrid.packs.rendering.actions.timeline_visualize import sweep as sw
from tests.packs.astrid_motion.test_timeline_visualize_motion_views import _clip

FPS = 30.0
OCC = "occ-film"


def _snapshot(clips):
    tracks = [{"id": t, "kind": k} for t, k in (("type", "visual"), ("fx", "visual"), ("plate", "visual"),
                                                 ("vo", "audio"))]
    end = max(c["at"] + c["hold"] for c in clips)
    return {
        "project_slug": "demo", "timeline_id": "tl", "timeline_name": "Film", "render_run_id": "run",
        "video_digest": "sha256:" + "d" * 64, "fps_rational": [30, 1], "duration_frames": int(end * FPS),
        "clips": clips, "tracks": tracks,
        "occurrences": [{"occurrence_id": OCC, "shot_id": "film", "shot_name": "FILM", "start": 0.0, "end": end,
                         "start_frame": 0, "end_frame": int(end * FPS)}],
        "registry": {"assets": {}}, "scripts": [], "metadata": {"selection": "composed_frame_capture"},
        "config": {"tracks": tracks, "clips": [dict(c) for c in clips]},
    }


def _film(*, title="TWO", shift=0.0):
    """c01 0-2, c02 2-3 (+ a title at 2.2), c03 3-5 (+ a card). The working copy may restyle the title and re-flow
    c03 (c03 moves, the gap is left behind it)."""
    at3 = 3.0 + shift
    clips = [
        _clip("p1", "plate", "visual", 0.0, 2.0, OCC, clipType="am-snap-plate", params={"zoom": 1},
              app={"astrid": {"cut": "c01"}}),
        _clip("p2", "plate", "visual", 2.0, 1.0, OCC, clipType="am-snap-plate", params={},
              app={"astrid": {"cut": "c02"}}),
        _clip("title", "type", "visual", 2.2, 0.8, OCC, clipType="am-type", params={"text": title, "size": 20},
              app={"astrid": {"cut": "c02", "layer": "title"}}),
        _clip("p3", "plate", "visual", at3, 2.0, OCC, clipType="am-snap-plate", params={},
              app={"astrid": {"cut": "c03"}}),
        _clip("card", "fx", "visual", at3 + 0.5, 1.0, OCC, clipType="am-callout", params={"title": "X"},
              app={"astrid": {"cut": "c03", "layer": "card"}}),
        _clip("vo", "vo", "audio", 0.0, 5.0 + shift, OCC, clipType="media"),
    ]
    return _snapshot(clips)


def test_expected_cuts_ignore_clips_a_reflow_only_moved():
    cut_of = {"occ:title": "c02", "card": "c03", "p3": "c03", "new": "c04"}
    changes = [
        {"kind": "changed", "clip_id": "occ:title", "before": [2.2, 3.0], "after": [2.2, 3.0], "fields": ["params"]},
        {"kind": "changed", "clip_id": "card", "before": [3.5, 4.5], "after": [4.5, 5.5], "fields": ["start", "end"]},
        {"kind": "changed", "clip_id": "p3", "before": [3.0, 5.0], "after": [4.0, 5.5], "fields": ["start", "end"]},
        {"kind": "added", "clip_id": "new", "before": None, "after": [9.0, 10.0], "fields": []},
    ]
    assert sw.expected_cuts(changes, cut_of) == {"c02", "c03", "c04"}  # p3 changed length; card only moved
    assert sw.expected_cuts(changes[1:2], cut_of) == set()


def test_a_cut_id_on_two_picture_windows_pairs_in_order():
    def strip(*spans):
        return _snapshot([_clip(f"p{i}", "plate", "visual", at, hold, OCC, clipType="am-snap-plate", params={},
                                app={"astrid": {"cut": "c02"}}) for i, (at, hold) in enumerate(spans)])

    rows = sw.plan_sweep(strip((0.0, 1.0), (1.0, 1.0)), strip((0.0, 1.0), (1.5, 1.0)), FPS)
    cut_rows = [row for row in rows if row["key"].startswith("c02")]
    assert [row["key"] for row in cut_rows] == ["c02", "c02#2"]
    assert cut_rows[1]["moved"] == 0.5


def _frame(path: Path, *, black=False, mark=False) -> Path:
    image = Image.new("RGB", (320, 180), "#000000" if black else "#204050")
    if mark:
        ImageDraw.Draw(image).rectangle((100, 60, 160, 90), fill="#ffcc00")
    path.parent.mkdir(parents=True, exist_ok=True)
    image.save(path)
    return path


def _fake(monkeypatch, published, working, *, marked=lambda side, frame: False, black=lambda side, frame: False):
    """Stands in for the two managed renders: a decoded frame is black, marked (a yellow box) or plain."""
    pair = {"base_snapshot": published, "working_snapshot": working,
            "base_video": Path("base.mp4"), "working_video": Path("work.mp4"),
            "base_run": "run-base", "working_run": "run-work", "base_media": {}, "working_media": {}}
    decoded = {"published": [], "working": []}

    def prepare_pair(client, project, timeline, draft, *, media_root):
        return pair

    def decode_frames(video, frames, out_dir):
        side = Path(out_dir).name
        decoded[side].extend(int(f) for f in frames)
        return {int(f): _frame(Path(out_dir) / f"{int(f)}.png", black=black(side, int(f)), mark=marked(side, int(f)))
                for f in frames}

    monkeypatch.setattr(fast_lane, "prepare_pair", prepare_pair)
    monkeypatch.setattr(fast_lane, "decode_frames", decode_frames)
    return decoded


def test_the_sweep_names_an_unexpected_black_gap_a_reflow_left_behind(tmp_path, monkeypatch):
    published, working = _film(), _film(title="TWO!", shift=1.0)
    title_on = range(fast_lane.frame_of(2.2, FPS), fast_lane.frame_of(3.0, FPS))
    gap = range(fast_lane.frame_of(3.0, FPS), fast_lane.frame_of(4.0, FPS))  # the gap the re-flow left: black
    decoded = _fake(monkeypatch, published, working,
                    marked=lambda side, f: side == "working" and f in title_on,
                    black=lambda side, f: side == "working" and f in gap)
    result = sw.sweep(object(), "demo", "tl", draft=object(), expected={"c02"}, root=tmp_path)
    assert decoded["working"]
    by_key = {row["key"]: row for row in result["cuts"]}
    assert by_key["c02"]["kind"] == "changed" and "% differ" in by_key["c02"]["verdict"]
    assert by_key["gap at 3.00 s"]["verdict"] == (
        "black frame (mean luma < 8) — check coverage: 1.00 s with no picture clip (3.00–4.00 s)")
    assert "gap at 3.00 s" in result["unexpected"]
    assert any("UNEXPECTED" in line and "gap at 3.00 s" in line for line in result["lines"])
    assert any(line.startswith("gap at 3.00 s: black frame") for line in result["lines"])


def test_a_gap_that_is_not_black_says_so_instead_of_claiming_black(tmp_path, monkeypatch):
    published, working = _film(), _film(shift=1.0)
    _fake(monkeypatch, published, working)
    result = sw.sweep(object(), "demo", "tl", draft=object(), root=tmp_path)
    gap = next(row for row in result["cuts"] if row["key"].startswith("gap at"))
    assert gap["verdict"].endswith("the frame is not black — check coverage")
