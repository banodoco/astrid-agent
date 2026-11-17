"""visualize clip views on the client: region over a span, mask fit, take vs source (from captured frames)."""
from __future__ import annotations

import io
import json
from argparse import Namespace
from pathlib import Path
from types import SimpleNamespace

import pytest
from PIL import Image, ImageDraw

from astrid.packs.timeline import cli_visualize as cv
from astrid.packs.timeline import cli_visualize_clip as clip

CANVAS = (320, 180)


def _frame(x: int, colour=(230, 230, 230)) -> Image.Image:
    image = Image.new("RGB", CANVAS, (30, 30, 40))
    draw = ImageDraw.Draw(image)
    for gx in range(0, CANVAS[0], 16):
        draw.line((gx, 0, gx, CANVAS[1]), fill=(55, 55, 65))
    draw.rectangle((x, 60, x + 40, 100), fill=colour)
    return image


def _captured(tmp_path: Path, *, moving: bool = True) -> dict:
    root = tmp_path / "cache" / "digest"
    (root / "frames").mkdir(parents=True)
    cards = []
    for i in range(10):
        t = 2.0 + i * 0.1
        name = f"frames/frame-{i:04d}.png"
        _frame(100 + (4 * i if moving else 0)).save(root / name)
        cards.append({"time_seconds": t, "image": name})
    (root / "frame-index.json").write_text(json.dumps({"cards": cards}))
    (root / "render-snapshot.json").write_text(json.dumps({
        "config": {"theme_overrides": {"visual": {"canvas": {"width": CANVAS[0], "height": CANVAS[1]}}}},
        "fps_rational": [10, 1], "clips": [], "registry": {"assets": {}}}))
    page = root / "motion-cut-03.png"
    Image.new("RGB", (10, 10)).save(page)
    return {"pages": [str(page)], "primary_page": str(page), "window": {"start": 2.0, "end": 3.0}}


def _png(image: Image.Image) -> bytes:
    out = io.BytesIO()
    image.save(out, format="PNG")
    return out.getvalue()


class _Media:
    def __init__(self, items):
        self.items = items  # object_id -> (row, bytes)

    def list(self, project, cursor=None, **_kw):
        rows = [row for row, _data in self.items.values()]
        if cursor is None:
            return SimpleNamespace(ok=True, data=[rows[:1], "next" if len(rows) > 1 else None])
        return SimpleNamespace(ok=True, data=[rows[1:], None])

    def show(self, project, ref):
        if ref in self.items:
            return SimpleNamespace(ok=True, data=self.items[ref][0], error=None)
        return SimpleNamespace(ok=False, data=None, error=SimpleNamespace(message="media object is not in the selected project"))

    def read_bytes(self, object_id):
        return self.items[object_id][1]


def _client(**media):
    items = {}
    for name, (image, media_type) in media.items():
        object_id = "sha256:" + (name * 64)[:64]
        items[object_id] = ({"object_id": object_id, "media_type": media_type, "filename": f"{name}.png"}, _png(image))
    return SimpleNamespace(media=_Media(items)), {name: "sha256:" + (name * 64)[:64] for name in media}


def _parsed(client, **kw):
    values = dict(project="demo", client=client, region=None, zoom=None, mask=None, compare_media=None,
                  against=None, cut=None, range=None, at=None, frame=None, shot=None, size=None, resolution=None)
    values.update(kw)
    return Namespace(**values)


def _mask_image():
    mask = Image.new("L", CANVAS, 0)
    ImageDraw.Draw(mask).ellipse((80, 40, 200, 120), fill=255)
    return mask


def test_region_with_a_cut_is_a_clip_view_and_needs_a_time():
    assert clip.wants_clip_view(Namespace(region="1,2,3,4", zoom=None, at=None, frame=None, mask=None,
                                          compare_media=None))
    assert not clip.wants_clip_view(Namespace(region="1,2,3,4", zoom=None, at="14", frame=None, mask=None,
                                              compare_media=None))  # one frame: the --at region page
    client, _ids = _client()
    with pytest.raises(clip.ClipError, match="need a time"):
        clip.prepare(_parsed(client, region="1,2,3,4"))
    with pytest.raises(clip.ClipError, match="--against goes with --compare-media"):
        clip.prepare(_parsed(client, region="1,2,3,4", cut="3", against="cut"))
    request = clip.prepare(_parsed(client, region="1,2,3,4", cut="3"))
    assert request["region"] == "1,2,3,4" and request["against"] == "cut"


def test_a_media_id_that_does_not_resolve_fails_before_any_capture():
    client, ids = _client(a=(_mask_image(), "image/png"), b=(_mask_image(), "image/png"))
    with pytest.raises(clip.ClipError, match="no media item"):
        clip.prepare(_parsed(client, cut="3", mask="sha256:ffffff"))
    # a short id (as pages print) resolves across list pages
    assert clip.resolve_media(_parsed(client), "sha256:bbbbbbbb")["object_id"] == ids["b"]


def test_capture_inputs_ask_for_source_resolution_and_dense_frames():
    inputs = {"range": "2..6"}
    clip.capture_inputs(_parsed(None), inputs, (1280, 720))
    assert inputs["resolution"] == "1280x720" and inputs["every"] == 0.1


def test_region_strip_over_a_cut_measures_drift_and_mask_fit(tmp_path):
    client, ids = _client(m=(_mask_image(), "image/png"))
    parsed = _parsed(client, cut="3", region="60,20,200,140", mask=ids["m"])
    request = clip.prepare(parsed)
    drawn = clip.draw(_captured(tmp_path), request, parsed=parsed, inputs={"cut": "3", "title": "Rough cut"})
    assert Path(drawn["page"]).name.startswith("view-clip-region-cut-03-region-60-20-200x140-mask-")
    assert len(drawn["pages"]) == 2 and Path(drawn["pages"][1]).name.startswith("view-clip-heat-")
    drift = next(line for line in drawn["findings"] if line.startswith("DRIFT"))
    assert "MOVES" in drift and "px right" in drift  # the square slides 36 px across the cut
    assert any(line.startswith("MASK") and "inside the mask" in line for line in drawn["findings"])
    assert drawn["label"] == "region over time" and drawn["box"] == [60, 20, 260, 160]
    with Image.open(drawn["page"]) as page:
        assert page.size[0] >= 2 * 200  # crops at source resolution
    rows = dict(clip.next_commands(["astrid", "timelines", "visualize", "T", "--project", "demo"], request,
                                   {"cut": "3"}, drawn))
    assert any("--compare-media" in " ".join(argv) for argv in rows.values())
    assert any(name.startswith("the region at") for name in rows)


def test_a_steady_region_holds(tmp_path):
    client, _ids = _client()
    parsed = _parsed(client, cut="3", region="top")
    drawn = clip.draw(_captured(tmp_path, moving=False), clip.prepare(parsed), parsed=parsed, inputs={"cut": "3"})
    assert any(line.startswith("DRIFT  HOLDS") for line in drawn["findings"])


def test_mask_alone_uses_its_area_and_a_named_mask_region(tmp_path):
    client, ids = _client(m=(_mask_image(), "image/png"))
    parsed = _parsed(client, cut="3", mask=ids["m"], region="mask")
    drawn = clip.draw(_captured(tmp_path), clip.prepare(parsed), parsed=parsed, inputs={"cut": "3"})
    x0, y0, x1, y1 = drawn["box"]
    assert x0 < 80 and y0 < 40 and x1 > 200 and y1 > 120  # the mask's box, padded


def test_take_vs_cut_reports_a_spill_over_the_mask_edge(tmp_path):
    outputs = _captured(tmp_path, moving=False)
    source = _frame(100)
    spill = Image.new("L", CANVAS, 0)
    ImageDraw.Draw(spill).ellipse((74, 34, 206, 126), fill=255)  # 6 px past the mask all round
    take = Image.composite(Image.new("RGB", CANVAS, (0, 200, 0)), source, spill)
    client, ids = _client(m=(_mask_image(), "image/png"), t=(take, "image/png"))
    parsed = _parsed(client, cut="3", mask=ids["m"], compare_media=ids["t"])
    drawn = clip.draw(outputs, clip.prepare(parsed), parsed=parsed, inputs={"cut": "3"})
    assert Path(drawn["page"]).name.startswith("view-clip-compare-") and drawn["label"] == "take vs source"
    compare = [line for line in drawn["findings"] if line.startswith("COMPARE")]
    assert any("SEAM" in line for line in compare)
    assert any("MATCH" in line for line in compare)  # away from the mask the take leaves the picture alone


def test_take_against_another_media_item(tmp_path):
    outputs = _captured(tmp_path, moving=False)
    client, ids = _client(t=(_frame(100), "image/png"), s=(_frame(100), "image/png"))
    parsed = _parsed(client, cut="3", compare_media=ids["t"], against=ids["s"])
    drawn = clip.draw(outputs, clip.prepare(parsed), parsed=parsed, inputs={"cut": "3"})
    assert any(line.startswith("COMPARE MATCH") for line in drawn["findings"])
    assert "-vs-ssssssss" in drawn["suffix"]


def test_stable_names_for_clip_pages_and_layers():
    clip_outputs = {"clip_page": {"suffix": "cut-08-region-300-0-980x450-mask-5da3b6fb",
                                  "page": "/x/view-clip-region-cut-08.png"}}
    assert cv._stable_name({"view": "motion", "preset": "cut", "cut": "8"}, clip_outputs) \
        == "region-cut-08-region-300-0-980x450-mask-5da3b6fb"
    assert cv._stable_name({"view": "motion", "preset": "cut", "cut": "8", "layers": ["heat"]}, {}) == "cut-08-heat"


def test_visualize_parser_takes_mask_compare_and_against():
    from astrid.packs.timeline import cli

    parsed = cli.build_parser(object()).parse_args(["visualize", "T1", "--project", "demo", "--cut", "8", "--region", "mask",
                                "--mask", "sha256:5da3b6fbe0eb", "--compare-media", "take", "--against", "cut"])
    assert parsed.mask == "sha256:5da3b6fbe0eb" and parsed.compare_media == "take" and parsed.against == "cut"


def test_shot_uses_its_planned_span_mask_and_latest_take():
    from astrid.sdk.shot_plan import PLAN_KEY

    client, ids = _client(m=(_mask_image(), "image/png"), t=(_frame(100), "image/png"))
    plan = {"version": 1, "method": ["M"], "status": "take_ready", "inputs": {"masks": [ids["m"]]},
            "takes": [{"generation": "run-1", "media": ids["t"]}]}
    bundle = {"shots": {"s1": {"payload": {"metadata": {"name": "02c Blue pill", PLAN_KEY: plan}}}},
              "placements": [{"shot_id": "s1", "occurrence_id": "o1", "placement": {"start_ms": 12167},
                              "duration_ms": 4290}]}
    parsed = _parsed(client, shot="02c", compare_media="take")
    parsed._bundle = bundle
    request = clip.prepare(parsed)
    assert parsed.range == "12.167..16.457" and parsed.shot is None
    assert request["mask"] == ids["m"] and "inputs.masks" in request["mask_from"]
    assert request["take"] == ids["t"] and "latest take" in request["take_from"]
    parsed = _parsed(client, shot="nope", region="top")
    parsed._bundle = bundle
    with pytest.raises(clip.ClipError, match="no shot"):
        clip.prepare(parsed)
