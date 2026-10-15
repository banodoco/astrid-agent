"""`check --at` / `edit --verify` in plain words (restored from the freeze's fast-lane tests).

The tip's fast lane compares frames decoded from the two managed renders (no Python worker), so these tests
fake `prepare_pair` and `decode_frames` instead of a worker. What they pin is what the person reads: what each
side shows on the frame, the exact pixel share and the changed area, a full-resolution zoom, and the
"hidden under a higher layer" note when a change cannot be seen.
"""
from __future__ import annotations

from pathlib import Path

import pytest
from PIL import Image, ImageDraw

from astrid.packs.rendering.actions.timeline_visualize import fast_lane
from astrid.packs.rendering.actions.timeline_visualize.fast_lane import FastLaneError, Moment, frame_of

FPS = 30.0


def _sparks(at, hold, **params):
    return {"id": "occ:c41-03-am-sprite", "track": "sprite", "clipType": "am-sprite", "at": at, "hold": hold,
            "asset": "SPARKS", "params": {"x": 230, **params}, "app": {"astrid": {"cut": "c41", "layer": "sparks"}}}


def _cfg(*clips):
    return {"tracks": [{"id": "fx", "kind": "visual"}, {"id": "sprite", "kind": "visual"},
                       {"id": "plate", "kind": "visual"}], "clips": list(clips)}


def test_a_moved_start_says_the_old_layer_is_still_there_and_the_new_one_is_not_yet():
    published, working = _cfg(_sparks(121.0, 1.0)), _cfg(_sparks(121.867, 1.0))
    before = Moment(121.833, "c41.sparks: the frame before it starts", "occ:c41-03-am-sprite")
    shows = fast_lane.describe_sides(before, frame_of(before.t, FPS), FPS, published, working)
    assert shows == ("c41.sparks on screen", "no c41.sparks yet (it starts at 121.87 s)")
    after_end = Moment(122.5, "c41.sparks: the first frame without it", "occ:c41-03-am-sprite")
    shows = fast_lane.describe_sides(after_end, frame_of(after_end.t, FPS), FPS, published, working)
    assert shows == ("no c41.sparks any more (it ended at 122.00 s)", "c41.sparks on screen")
    moved = _cfg(_sparks(121.0, 1.0, x=260))
    shows = fast_lane.describe_sides(Moment(121.5, "mid", "c41-03-am-sprite"), frame_of(121.5, FPS), FPS, published, moved)
    assert shows == ("c41.sparks on screen, before your change", "c41.sparks on screen with your change (params)")


def test_a_bare_moment_names_the_layers_only_one_side_has_and_the_changed_ones():
    cover = {"id": "occ:c30-01-am-sprite", "track": "sprite", "clipType": "am-sprite", "at": 91.5, "hold": 2.0,
             "asset": "MYSTERY", "app": {"astrid": {"cut": "c30", "layer": "cover"}}}
    icon = {"id": "occ:c30-02-am-sprite", "track": "sprite", "clipType": "am-sprite", "at": 93.4, "hold": 1.0,
            "asset": "ICON", "app": {"astrid": {"cut": "c30", "layer": "icon"}}}
    shows = fast_lane.describe_sides(Moment(93.45, "93.45"), frame_of(93.45, FPS), FPS, _cfg(cover),
                                     _cfg({**cover, "hold": 1.8}, icon))
    assert shows == ("c30.cover (not in the working copy here)", "c30.icon (new here)")


def test_a_few_changed_pixels_read_as_less_than_a_tenth_of_a_percent_with_their_box(tmp_path):
    a, b = tmp_path / "a.png", tmp_path / "b.png"
    Image.new("RGB", (640, 360), "#204050").save(a)
    image = Image.new("RGB", (640, 360), "#204050")
    for x in (300, 301, 302):
        image.putpixel((x, 100), (40, 74, 90))  # a faint, 1-px connector end: delta 10 per channel
    image.save(b)
    share, count, box, size = fast_lane.difference(a, b)
    assert count == 3 and box == (300, 100, 303, 101)
    line = fast_lane.pixel_line(share, count, box, size, (1920, 1080))
    assert line.startswith("pixels: <0.1% (3 px) differ, changed area (old ∪ new) 900,300–909,303 (9x3 canvas px")
    assert fast_lane.pixel_line(0.03041, 7000, (10, 10, 20, 20), (640, 360), (1920, 1080)).startswith(
        "pixels: 3.04% differ")


def test_regions_parse_and_a_zoom_fits_the_page_at_full_resolution():
    assert fast_lane.parse_region("100,50,300,200") == (100, 50, 400, 250)
    assert fast_lane.zoom_scale((747, 377)) == 1.0  # full resolution when it fits the page
    assert fast_lane.zoom_scale((100, 50)) == 6.0 and fast_lane.zoom_scale((1968, 400)) == 0.5
    with pytest.raises(FastLaneError, match="x,y,w,h"):
        fast_lane.parse_region("100,50")


def _pair(tmp_path, published, working, paint):
    """Stands in for the two managed renders: ``paint(side, frame)`` returns the decoded frame image."""
    pair = {"base_snapshot": published, "working_snapshot": working,
            "base_video": tmp_path / "base" / "v.mp4", "working_video": tmp_path / "work" / "v.mp4",
            "base_run": "run-base", "working_run": "run-work", "base_media": {}, "working_media": {}}

    def prepare_pair(client, project, timeline, draft, *, media_root):
        return pair

    def decode_frames(video, frames, out_dir):
        side = Path(out_dir).name
        out = {}
        for frame in frames:
            path = Path(out_dir) / f"f{int(frame):06d}.png"
            path.parent.mkdir(parents=True, exist_ok=True)
            paint(side, int(frame)).save(path)
            out[int(frame)] = path
        return out

    return prepare_pair, decode_frames


class _Draft:
    def document(self):
        return {"doc": True}


def _snapshot(clips, *, tracks=None):
    tracks = tracks or [{"id": "fx", "kind": "visual"}, {"id": "sprite", "kind": "visual"},
                        {"id": "plate", "kind": "visual"}]
    return {"fps_rational": [30, 1], "config": {"tracks": tracks, "clips": clips}, "registry": {"assets": {}},
            "occurrences": [], "tracks": tracks}


def test_verify_says_what_each_side_shows_and_the_exact_pixels(tmp_path, monkeypatch):
    published = _snapshot([_sparks(1.0, 2.0)])
    working = _snapshot([_sparks(1.0, 2.0, x=260)])

    def paint(side, frame):
        image = Image.new("RGB", (640, 360), "#204050")
        if side == "working":
            ImageDraw.Draw(image).rectangle((100, 50, 160, 90), fill="#ffcc00")
        return image

    prepare_pair, decode_frames = _pair(tmp_path, published, working, paint)
    monkeypatch.setattr(fast_lane, "prepare_pair", prepare_pair)
    monkeypatch.setattr(fast_lane, "decode_frames", decode_frames)
    result = fast_lane.verify(object(), "demo", "tl", [Moment(1.5, "c41.sparks mid-clip", "occ:c41-03-am-sprite")],
                              draft=_Draft(), footer=["lint on c41: clean"], root=tmp_path / "root")
    (row,) = result["rows"]
    assert row["published_shows"] == "c41.sparks on screen, before your change"
    assert row["working_shows"] == "c41.sparks on screen with your change (params)"
    assert row["pixels"].startswith("pixels: 1.09% differ, changed area (old ∪ new) 300,150–483,273")
    assert result["lines"][:3] == [
        "verify  c41.sparks mid-clip · 1.50 s (f45)",
        "        published: c41.sparks on screen, before your change · working copy: "
        "c41.sparks on screen with your change (params)",
        "        " + row["pixels"],
    ]
    with Image.open(result["page"]) as page:
        assert page.size[0] == 24 + 2 * (fast_lane.TILE[0] + 12)


def test_verify_with_zoom_crops_both_sides_at_full_resolution(tmp_path, monkeypatch):
    from tests.packs.rendering.test_timeline_visualize_motion_views import layered_snapshot

    snapshot = layered_snapshot()
    snapshot["config"] = {"tracks": snapshot["tracks"], "clips": [dict(c) for c in snapshot["clips"]]}

    def paint(side, frame):
        image = Image.new("RGB", (1920, 1080), "#204050")
        if side == "working":
            ImageDraw.Draw(image).rectangle((1500, 150, 1503, 152), fill="#ffffff")  # a connector end moved
        return image

    prepare_pair, decode_frames = _pair(tmp_path, snapshot, snapshot, paint)
    monkeypatch.setattr(fast_lane, "prepare_pair", prepare_pair)
    monkeypatch.setattr(fast_lane, "decode_frames", decode_frames)
    result = fast_lane.verify(object(), "demo", "tl", [Moment(3.8, "card")], draft=_Draft(), zoom="card",
                              root=tmp_path / "root")
    (row,) = result["rows"]
    zoom = row["zoom"]
    x0, y0, x1, y1 = zoom["region"]
    with Image.open(zoom["published"]) as crop:
        assert crop.size == (x1 - x0, y1 - y0)  # canvas px, not a thumbnail
    assert "full resolution" in zoom["label"] and "changed area (old ∪ new) 1500,150–1504,153" in zoom["label"]
    assert any(line.strip().startswith("zoom card:") for line in result["lines"])


def test_a_changed_layer_under_an_opaque_higher_layer_is_named_as_hidden(tmp_path, monkeypatch):
    from tests.packs.rendering.test_timeline_visualize_motion_views import _clip, layered_snapshot

    occ = "occ-ch01"
    clips = [
        _clip("p1", "plate", "visual", 0.0, 2.0, occ, clipType="am-snap-plate", params={"zoom": 1}),
        _clip("churn", "sprite", "visual", 0.0, 2.0, occ, clipType="am-churn", params={},
              app={"astrid": {"cut": "c18", "layer": "churn"}}),
        _clip("board", "fx", "visual", 0.0, 2.0, occ, clipType="am-ui-sketch", params={},
              app={"astrid": {"cut": "c18", "layer": "board"}}),
    ]
    snapshot = layered_snapshot()
    snapshot.update(clips=clips, config={"tracks": snapshot["tracks"], "clips": [dict(c) for c in clips]},
                    occurrences=[{"occurrence_id": occ, "shot_id": "ch01", "shot_name": "01", "start": 0.0, "end": 2.0,
                                  "start_frame": 0, "end_frame": 60}])
    # Cached component boxes cannot prove opaque support. The shared evaluator
    # does not currently name an occluder; offline visibility is unknown.
    assert fast_lane.hidden_under(snapshot, "churn", 1.0, FPS) is None
    assert fast_lane.hidden_under(snapshot, "board", 1.0, FPS) is None  # nothing above it

    moved = {**snapshot, "config": {**snapshot["config"],
                                    "clips": [dict(c, params={"speed": 2}) if c["id"].endswith("churn") else dict(c)
                                              for c in snapshot["config"]["clips"]]}}
    prepare_pair, decode_frames = _pair(tmp_path, snapshot, moved, lambda side, frame: Image.new("RGB", (640, 360), "#204050"))
    monkeypatch.setattr(fast_lane, "prepare_pair", prepare_pair)
    monkeypatch.setattr(fast_lane, "decode_frames", decode_frames)
    result = fast_lane.verify(object(), "demo", "tl", [Moment(1.0, "c18.churn (params) mid-clip", f"{occ}:churn")],
                              draft=_Draft(), root=tmp_path / "root")
    assert result["rows"][0]["pixels"] == "pixels: identical to published"
    assert result["rows"][0]['visibility']['working']['visible'] == 'unknown'
    assert result['intervals']['status'] == 'unknown'


def test_check_takes_a_zoom_and_a_region(monkeypatch, capsys):
    """`check --at 6 --zoom a-rocket` reaches Checkout.verify; a zoom alone looks at the layer's middle."""
    from astrid.packs.timeline import cli
    from astrid.sdk import timeline_checkout

    calls = []

    class Report:
        valid = True

        def __str__(self):
            return "working copy valid"

    class Layer:
        start, end = 4.0, 8.0

    class FakeCheckout:
        def check(self):
            return Report()

        def clip(self, address):
            assert address == "a-rocket"
            return Layer()

        def verify(self, **kwargs):
            calls.append(kwargs)
            return {"lines": ["verify ok"]}

    class FakeCheckoutClass:
        @classmethod
        def load(cls, path):
            return FakeCheckout()

    monkeypatch.setattr(timeline_checkout, "find_draft", lambda *args, **kwargs: "draft.json")
    monkeypatch.setattr(timeline_checkout, "Checkout", FakeCheckoutClass)
    client = object()
    parser = cli.build_parser(client)
    help_text = parser._subparsers._group_actions[0].choices["check"].format_help()
    assert "--zoom ADDRESS" in help_text and "--region X,Y,W,H" in help_text

    parsed = parser.parse_args(["check", "main", "--project", "p", "--at", "6", "--zoom", "a-rocket"])
    assert parsed.handler(parsed) == 0
    assert calls == [{"at": ["6"], "client": client, "labels": False, "zoom": "a-rocket"}]
    calls.clear()
    parsed = parser.parse_args(["check", "main", "--project", "p", "--zoom", "a-rocket"])
    assert parsed.handler(parsed) == 0
    assert calls[0]["at"] == ["6.00"] and calls[0]["zoom"] == "a-rocket"
    calls.clear()
    parsed = parser.parse_args(["check", "main", "--project", "p", "--at", "6", "--region", "100,50,300,200"])
    assert parsed.handler(parsed) == 0
    assert calls[0]["region"] == "100,50,300,200" and "zoom" not in calls[0]
    assert "verify ok" in capsys.readouterr().out
