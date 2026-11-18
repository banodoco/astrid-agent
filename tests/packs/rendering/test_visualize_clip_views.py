"""Pictures for understanding a clip: motion map, mask fit, region drift and take-vs-source differences."""
from __future__ import annotations

import sys
from pathlib import Path

import numpy as np
from PIL import Image, ImageDraw

from astrid.packs.rendering.actions.timeline_visualize import clip_views as cv
from astrid.packs.rendering.actions.timeline_visualize.layers import base as layer_base

sys.path.insert(0, str(Path(__file__).parent))


def _frame(x: int, *, size=(320, 180), square=40, colour=(230, 230, 230)) -> Image.Image:
    image = Image.new("RGB", size, (30, 30, 40))
    draw = ImageDraw.Draw(image)
    for gx in range(0, size[0], 16):  # texture so edges exist everywhere
        draw.line((gx, 0, gx, size[1]), fill=(50, 50, 60))
    draw.rectangle((x, 60, x + square, 60 + square), fill=colour)
    return image


def _mask(box, size=(320, 180)) -> Image.Image:
    mask = Image.new("L", size, 0)
    ImageDraw.Draw(mask).ellipse(box, fill=255)
    return mask


def test_change_map_puts_motion_where_the_square_moves():
    frames = [_frame(100, colour=(230, 230, 230) if i % 2 else (90, 90, 90)) for i in range(10)]
    freq, steps = cv.change_map(frames, width=None)
    assert steps == 9 and freq.shape == (180, 320)
    assert freq[60:100, 100:140].min() == 1.0  # a blinking square changes on every step
    assert freq[:40, :].max() == 0.0  # the still top band never changes
    hot = cv.hotspot(freq, (320, 180))
    assert hot is not None and 90 <= hot[0] and hot[2] <= 190 and 50 <= hot[1] and hot[3] <= 110
    assert cv.where_words(hot, (320, 180)) == "the centre"


def test_heat_overlay_colours_change_and_greys_the_rest():
    frames = [_frame(100 + 3 * i) for i in range(10)]
    freq, _ = cv.change_map(frames, width=None)
    overlay = np.asarray(cv.heat_overlay(frames[5], freq), dtype=np.int16)
    still = overlay[10, 10]
    assert abs(int(still[0]) - int(still[1])) <= 1 and abs(int(still[1]) - int(still[2])) <= 1  # grey
    moving = overlay[80, 100]
    assert int(moving[0]) - int(moving[2]) > 40  # warm colour where it changes


def test_mask_fit_tells_inside_from_outside():
    frames = [_frame(100 + 3 * i) for i in range(10)]
    freq, _ = cv.change_map(frames, width=None)
    covering = cv.mask_fit(freq, _mask((80, 40, 200, 120)), (320, 180))
    elsewhere = cv.mask_fit(freq, _mask((230, 10, 300, 60)), (320, 180))
    assert covering.inside > 0.9 and elsewhere.inside < 0.05
    assert elsewhere.outside_box is not None
    assert 0 < covering.mask_area < 0.3


def test_region_drift_measures_a_moving_and_a_still_box():
    moving = [_frame(100 + 4 * i).crop((60, 20, 260, 160)) for i in range(5)]
    drift = cv.region_drift(moving)
    assert drift[0] == (0.0, 0.0)
    assert abs(drift[-1][0] - 16) <= 2 and abs(drift[-1][1]) <= 2
    level, sentence, most = cv.drift_verdict(drift, [0.0, 0.1, 0.2, 0.3, 0.4])
    assert level == "MOVES" and "16 px right" in sentence and "0.40 s" in sentence
    still = [_frame(100).crop((60, 20, 260, 160)) for _ in range(5)]
    level, sentence, _ = cv.drift_verdict(cv.region_drift(still), [0, 1, 2, 3, 4])
    assert level == "HOLDS"


def test_mask_outline_and_named_regions():
    mask = _mask((80, 40, 200, 120))
    edge = np.asarray(cv.mask_outline(mask, width=1)) > 0
    assert edge.any() and not edge[80, 140]  # a line, not the filled mask
    assert np.asarray(cv.mask_outline(mask, width=3)).astype(bool).sum() > edge.sum()
    assert cv.named_region("top-left", (1280, 720)) == (0, 0, 640, 360)
    assert cv.named_region("lenses", (1280, 720)) is None
    assert cv.mask_box(mask, pad=0) == (80, 40, 201, 121)


def test_load_mask_reads_white_as_edited_and_resizes():
    image = Image.new("RGB", (640, 360), "black")
    ImageDraw.Draw(image).rectangle((0, 0, 319, 179), fill="white")
    mask = cv.load_mask(image, (320, 180))
    assert mask.size == (320, 180) and mask.getpixel((10, 10)) == 255 and mask.getpixel((300, 170)) == 0


def test_difference_separates_the_edit_from_a_spill_over_its_edge():
    source = _frame(100)
    mask = _mask((80, 40, 200, 120))
    clean = Image.composite(Image.new("RGB", source.size, (0, 200, 0)), source, mask)
    spill = Image.composite(Image.new("RGB", source.size, (0, 200, 0)), source, _mask((70, 30, 210, 130)))
    tidy = cv.difference(source, clean, mask)
    messy = cv.difference(source, spill, mask)
    assert tidy.outside < 1 and tidy.inside > 50
    verdicts = dict(cv.compare_verdicts([tidy], masked=True))
    assert "MATCH" in verdicts
    assert messy.seam > tidy.seam
    assert "SEAM" in dict(cv.compare_verdicts([messy], masked=True))
    changed = cv.difference(source, _frame(140), None)
    assert dict(cv.compare_verdicts([changed], masked=False)).get("DIFFERS")


def test_pages_have_a_plain_header_and_draw(tmp_path):
    frames = [_frame(100 + 3 * i) for i in range(8)]
    freq, _ = cv.change_map(frames)
    mask = _mask((80, 40, 200, 120))
    strip = cv.region_strip_page(title="T · cut 1", what="the region at 8 moments", sub=["s"], frames=frames,
                                 times=[i / 10 for i in range(8)], box=(60, 20, 260, 160), canvas=(320, 180),
                                 mask=mask, verdicts=[("MOVES", "it moves")], drift=[(0, 0)] * 8,
                                 heat=cv.heat_overlay(frames[4].crop((60, 20, 260, 160)), freq))
    heat = cv.heat_page(title="T", what="where it moves", sub=[], base=frames[4], freq=freq, mask=mask)
    rows = [(0.0, frames[0], frames[1], cv.difference(frames[0], frames[1], mask))]
    compare = cv.compare_page(title="T", what="take vs source", sub=[], rows=rows,
                              verdicts=cv.compare_verdicts([r[3] for r in rows], masked=True))
    for page in (strip, heat, compare):
        assert page.size[0] >= 900 and page.size[1] > 200
        assert page.getpixel((2, 2)) == Image.new("RGB", (1, 1), layer_base.PALETTE["bg"]).getpixel((0, 0))
    # the region is shown at source resolution (two 200 px crops per row fit easily)
    assert strip.size[0] >= 24 + 2 * 210


def test_heat_layer_draws_a_motion_map_with_a_finding(tmp_path, monkeypatch):
    from test_timeline_visualize_motion_views import _execute, layered_snapshot

    result = _execute(tmp_path, layered_snapshot(), {"view": "motion", "cut": "1", "layers": ["heat"]}, monkeypatch)
    outputs = result["outputs"]
    assert any(line.startswith("HEAT") for line in outputs["findings"])
    with Image.open(outputs["pages"][0]) as page:
        assert page.size[1] > 300  # the map itself, not an empty sheet


def test_a_real_seam_at_full_canvas_is_a_seam_not_a_change_away_from_the_mask():
    size = (1920, 1080)
    source = Image.new("RGB", size, (60, 50, 40))
    ImageDraw.Draw(source).rectangle((0, 0, 1919, 300), fill=(90, 80, 70))
    mask = Image.new("L", size, 0)
    ImageDraw.Draw(mask).ellipse((810, 390, 1110, 690), fill=255)  # a 300 px mask
    spill = Image.new("L", size, 0)
    ImageDraw.Draw(spill).ellipse((800, 380, 1120, 700), fill=255)  # the edit runs 10 px past it
    take = Image.composite(Image.new("RGB", size, (0, 200, 0)), source, spill)
    box = (700, 280, 1220, 800)  # cropped to the mask's area, still at canvas scale
    numbers = cv.difference(source.crop(box), take.crop(box), mask.crop(box))
    verdicts = dict(cv.compare_verdicts([numbers], masked=True))
    assert "MATCH" in verdicts and "DIFFERS" not in verdicts
    assert "SEAM" in verdicts


def test_a_square_mask_is_fitted_like_a_cover_clip_not_stretched():
    square = Image.new("L", (1000, 1000), 0)
    ImageDraw.Draw(square).ellipse((300, 300, 700, 700), fill=255)  # a circle in the middle
    mask = cv.load_mask(square, (1920, 1080))
    x0, y0, x1, y1 = mask.getbbox()
    assert abs((x1 - x0) - (y1 - y0)) <= 2  # still a circle (stretching would make it 1.78x wide)
    assert abs((x0 + x1) / 2 - 960) <= 2 and abs((y0 + y1) / 2 - 540) <= 2
    assert set(np.unique(np.asarray(mask))) <= {0, 255}


def test_drift_precision_is_stated_for_wide_regions():
    assert cv.drift_precision((600, 300)) == 1.0
    assert cv.drift_precision((1280, 450)) == 2.0
    assert cv.worst_moment([(0, 0), (5, 5), (8, 0)]) == 2  # Euclidean, like the verdict (L1 would pick 1)
