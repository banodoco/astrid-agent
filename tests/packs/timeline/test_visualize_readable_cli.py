"""Readable visualize CLI output (astrid-dev#798): copyable next steps, page path last, region zoom, queue notes."""
from __future__ import annotations

import json
import sys
from argparse import Namespace
from pathlib import Path

import pytest
from PIL import Image

from astrid.packs.timeline import cli
from astrid.packs.timeline import cli_visualize as cv

FULL = "authoring-parent-revision-08dee21c156b3da1816a656e4c155803edb880b6bb7f3c215b8a2acd1523ae3e"


def test_installed_copy_prints_the_launcher_not_the_internal_module(tmp_path, monkeypatch):
    monkeypatch.setenv("ASTRID_INSTALL_PROGRAM_ROOT", str(tmp_path))
    monkeypatch.setattr(sys, "orig_argv", ["/py/bin/python", "-X", "utf8", "-I", "-m", "astrid.omp_agent"], raising=False)
    monkeypatch.setattr(sys, "argv", [str(tmp_path / "versions/v/a/astrid/omp_agent.py"), "timelines"])
    monkeypatch.setattr("shutil.which", lambda name: None)
    assert cli._astrid_cli_command() == [str(tmp_path / "run-astrid")]
    shim = tmp_path / "bin" / "astrid"
    shim.parent.mkdir()
    shim.write_text(f"#!/bin/sh\n# Astrid managed command shim\nexec {tmp_path}/run-astrid \"$@\"\n")
    monkeypatch.setattr("shutil.which", lambda name: str(shim) if name == "astrid" else None)
    assert cli._astrid_cli_command() == ["astrid"]


def _summary(parsed: Namespace, *, view="motion", preset="frame", window=None) -> str:
    outputs = {"pages": ["/x/view.png"], "primary_page": "/x/view.png",
               "snapshot_label": {"status": "committed", "label": f"committed revision {FULL}",
                                  "actual_revision_id": FULL},
               "window": window or {"preset": "frame", "start": 14.0, "end": 14.04, "earlier": [13.96, 14.0],
                                    "later": [14.04, 14.08], "zoom_in": [14.01, 14.03], "cut": 8},
               "timing": {"frames": 1, "capture_s": 3.0, "compose_s": 1.0, "wall_s": 5.0, "queued_s": 1.0}}
    return cv._visualization_summary(outputs, parsed=parsed,
                                     inputs={"view": view, "preset": preset, "timeline_slug": "rough-cut",
                                             "revision_id": FULL})


def test_next_steps_omit_the_pinned_published_revision_and_say_time_not_zoom(monkeypatch):
    monkeypatch.setattr(cli, "_astrid_cli_command", lambda: ["astrid"])
    published = Namespace(project="minkhole", beats=None, _source_revision_id=FULL, _revision_explicit=False)
    text = _summary(published)
    assert "--revision-id" not in text
    assert "previous frame (13.96 s)" in text and "next frame (14.04 s)" in text
    assert "zoom in" not in text and "the middle" not in text
    assert "astrid timelines visualize rough-cut --project minkhole --at 14.00 --region X,Y,W,H" in text
    assert "the cut it is in (cut 8)" in text
    pinned = Namespace(project="minkhole", beats=None, _source_revision_id=FULL, _revision_explicit=True)
    text = _summary(pinned)
    assert "--revision-id 08dee21c " in text and FULL not in text.split("next:")[1]


def test_window_navigation_names_time_windows_plainly():
    window = {"preset": "scan", "start": 5.0, "end": 10.0, "zoom_in": [6.5, 9.0], "zoom_out": [2.5, 12.5],
              "busiest": 7.75}
    rows = dict((name, " ".join(argv)) for name, argv in cv._window_navigation(window, ["v"]))
    assert rows["shorter time window around the busiest moment 7.75 s"].endswith("--range 6.50..9.00")
    assert rows["longer time window 2.50–12.50 s"].endswith("--preset scan --range 2.50..12.50")


def test_region_request_needs_one_frame_and_a_valid_box():
    assert cv._region_request(Namespace(region=None, zoom=None, at="14")) is None
    assert cv._region_request(Namespace(region="1,2,3,4", zoom="c08", at="14")) == "choose --region or --zoom, not both"
    assert "need --at" in cv._region_request(Namespace(region="1,2,3,4", zoom=None, at=None, frame=None))
    assert "positive width" in cv._region_request(Namespace(region="1,2,0,4", zoom=None, at="14"))
    assert cv._region_request(Namespace(region="300,0,1200,450", zoom=None, at="14"))["region"] == (300, 0, 1500, 450)


def _pack(tmp_path: Path, size=(1920, 1080)) -> Path:
    root = tmp_path / "minkhole" / ("a" * 64)
    (root / "frames").mkdir(parents=True)
    frame = Image.new("RGB", size, (10, 10, 10))
    for x in range(900, 1000):
        for y in range(100, 140):
            frame.putpixel((x, y), (250, 0, 0))
    frame.save(root / "frames" / "frame-000000336.png")
    (root / "frame-index.json").write_text(json.dumps({"cards": [
        {"image": "frames/frame-000000336.png", "time_seconds": 14.0}]}))
    (root / "render-snapshot.json").write_text(json.dumps({"config": {}, "fps_rational": [24, 1]}))
    Image.new("RGB", (400, 300)).save(root / "view-frame-14.00-14.04.png")
    return root


def test_region_page_crops_the_frame_at_source_resolution(tmp_path):
    root = _pack(tmp_path)
    outputs = {"pages": [str(root / "view-frame-14.00-14.04.png")]}
    page = cv._draw_region_page(outputs, {"region": (900, 100, 1000, 140), "label": "region 900,100,100,40"},
                                parsed=Namespace(), inputs={"title": "Rough cut C2"})
    with Image.open(page["crop"]) as crop:
        assert crop.size == (100, 40) and crop.getpixel((50, 20)) == (250, 0, 0)  # 1 canvas px = 1 crop px
    assert page["box"] == [900, 100, 1000, 140] and Path(page["page"]).is_file()
    assert page["finding"].startswith("REGION 14.00s region 900,100,100,40") and "source resolution" in page["finding"]
    with pytest.raises(ValueError, match="outside"):
        cv._draw_region_page(outputs, {"region": (5000, 5000, 5100, 5100)}, parsed=Namespace(), inputs={})


def test_stable_page_is_named_by_timeline_view_and_time(tmp_path):
    root = _pack(tmp_path)
    primary = root / "view-frame-14.00-14.04.png"
    outputs = {"primary_page": str(primary), "window": {"start": 14.0}}
    parsed = Namespace(draft=None)
    page = cv._stable_page(outputs, parsed=parsed, inputs={"view": "motion", "preset": "frame", "title": "Rough cut C2"})
    assert page == str(tmp_path / "minkhole" / "pages" / "rough-cut-c2" / "frame-14.00s.png")
    assert Path(page).read_bytes() == primary.read_bytes()
    assert cv._stable_name({"view": "contact"}, {}) == "overview"
    assert cv._stable_name({"view": "motion", "preset": "cut", "cut": "8"}, {}) == "cut-08"
    assert cv._stable_name({"view": "motion", "preset": "frame"}, {"window": {"start": 14.0},
                                                                    "region_page": {"box": [300, 0, 1500, 450]}}) \
        == "frame-14.00s-region-300-0-1200x450"
    pinned = Namespace(draft=None, _revision_explicit=True)
    page = cv._stable_page(outputs, parsed=pinned, inputs={"view": "contact", "title": "Rough cut C2", "revision_id": FULL})
    assert page.endswith("pages/rough-cut-c2@08dee21c/overview.png")


def test_a_queued_view_says_it_waited_behind_other_work():
    assert cv._queue_note({"timing": {"queued_s": 86.0}}).startswith("waited 86 s for the Runtime")
    assert cv._queue_note({"timing": {"queued_s": 2.0}}) is None
    text = cv._visualization_summary({"timing": {"queued_s": 40.0}}, parsed=Namespace(project="p", beats=None),
                                     inputs={"view": "contact", "timeline_slug": "t"})
    assert "no picture produced (no PNG page was returned; it waited 40 s behind other Runtime work)" in text


def test_tile_plan_label_fits_a_tile():
    assert cv.tile_plan_label({"method": ["LM", "R"], "status": "planned", "takes": [{"generation": "run-1"}, {"media": "sha256:x"}], "line": "x"}) \
        == "LM+R · planned · 2 takes"
    assert cv.tile_plan_label(None) == "no plan"


def test_visualize_parser_takes_region_and_zoom():
    parsed = cli.build_parser(object()).parse_args(
        ["visualize", "T1", "--project", "demo", "--at", "14", "--region", "300,0,1200,450"])
    assert parsed.region == "300,0,1200,450" and parsed.zoom is None
