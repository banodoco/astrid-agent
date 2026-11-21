"""The on-screen model (TRUTH.md §2.2): what is visible on a frame, and how each frame is covered.

The config is a plain render-config dict: tracks top first, render clips with clipType/track/at/hold/
params/asset/opacity/effects and app.cut/app.layer for their addresses.
"""
from __future__ import annotations

import pytest

FPS = 30
CANVAS = {"width": 1920, "height": 1080}
ELEMENTS = {
    "am-snap-plate": {"coverage": "picture", "background_unless": ["asset", "params.src"]},
    "am-footage": {"coverage": "picture"},
    "am-type": {"coverage": "content"},
    "am-sprite": {"coverage": "content"},
}


def _clip(cid, clip_type, track, *, at=0.0, hold=1.0, cut="c01", layer="l", **fields):
    clip = {"id": cid, "clipType": clip_type, "track": track, "at": at, "hold": hold, "params": {},
            "app": {"cut": cut, "layer": layer}}
    clip.update(fields)
    return clip


def _config(clips, tracks=("fx", "type", "sprite", "plate")):
    return {"tracks": [{"id": t} for t in tracks], "clips": clips}


def _plate(cid="plate", *, asset="P", at=0.0, hold=1.0, **fields):
    return _clip(cid, "am-snap-plate", "plate", asset=asset, at=at, hold=hold, **fields)


# --- visible_at -------------------------------------------------------------------------------------------

def test_visible_at_lists_layers_top_first_with_role_and_opacity_product():
    from astrid.core.timeline.onscreen import visible_at

    config = _config(
        [
            _plate("plate", asset="P", opacity=0.5, app={"cut": "c01", "layer": "field"}),
            _clip("type", "am-type", "type", cut="c01", layer="title", opacity=0.5),
            _clip("fx", "am-footage", "fx", cut="c01", layer="grain", asset="P"),
        ],
    )
    config["tracks"][1]["opacity"] = 0.8  # the type track
    layers = visible_at(config, 0, FPS, CANVAS, ELEMENTS)
    # z order is track order, top first: fx, then type, then plate.
    assert [layer.address for layer in layers] == ["c01.grain", "c01.title", "c01.field"]
    assert [layer.role for layer in layers] == ["picture", "content", "picture"]
    # opacity = clip opacity x track opacity x fades: type is 0.5 x 0.8 = 0.4; the plate is 0.5 x 1.0.
    assert layers[1].opacity == pytest.approx(0.4)
    assert layers[2].opacity == pytest.approx(0.5)


# --- full_frame: crop and inset -----------------------------------------------------------------------------

def test_a_cropped_plate_and_an_inset_are_not_full_frame_but_a_default_plate_is():
    from astrid.core.timeline.onscreen import visible_at

    default = visible_at(_config([_plate()]), 0, FPS, CANVAS, ELEMENTS)
    cropped = visible_at(_config([_plate(cropLeft=0.2)]), 0, FPS, CANVAS, ELEMENTS)
    inset = visible_at(_config([_clip("fx", "am-footage", "fx", asset="P",
                                      params={"x": 1200, "y": 600, "scale": 0.3})]), 0, FPS, CANVAS, ELEMENTS)
    assert default[0].full_frame is True
    assert cropped[0].full_frame is False
    assert inset[0].full_frame is False


# --- coverage: per-frame classes, run-length encoded --------------------------------------------------------

def test_coverage_run_length_encodes_covered_gap_and_covered_but_empty():
    from astrid.core.timeline.onscreen import coverage

    config = _config([
        # frames 0–30: a plate with an asset (a picture): COVERED, with CONTENT.
        _plate("c01-plate", asset="P", at=0.0, hold=1.0, app={"cut": "c01", "layer": "field"}),
        # frames 30–45: nothing on screen: GAP.
        # frames 45–90: a background-only plate (no asset, no src): COVERED, but EMPTY.
        _clip("c03-plate", "am-snap-plate", "plate", at=1.5, hold=1.5, params={"fill": "#F7F4ED"},
              app={"cut": "c03", "layer": "field"}),
    ])
    spans = coverage(config, FPS, CANVAS, ELEMENTS)
    assert [(s.start_frame, s.end_frame, s.cls) for s in spans] == [
        (0, 30, "COVERED"), (30, 45, "GAP"), (45, 90, "COVERED"),
    ]
    assert spans[0].content == "CONTENT"
    assert spans[2].content == "EMPTY"


def test_a_plain_media_clip_is_the_picture_on_whatever_track_it_sits():
    from astrid.core.timeline.onscreen import coverage, element_coverage

    # The real element table, not a hand-written one: `media` declares nothing in an element.yaml, so this is the compat entry.
    elements = element_coverage(["media"])
    clips = [
        {"id": "a", "clipType": "media", "track": "V2", "at": 0.0, "hold": 1.0, "asset": "A"},
        {"id": "b", "clipType": "media", "track": "V2", "at": 1.0, "hold": 1.0},  # its asset was cleared
    ]
    spans = coverage({"tracks": [{"id": "V2"}], "clips": clips}, FPS, CANVAS, elements, start_frame=0, end_frame=90)
    assert [(span.start_frame, span.end_frame, span.cls, span.content) for span in spans] == [
        (0, 30, "UNKNOWN", "CONTENT"),  # placement requires intrinsic media facts
        (30, 60, "UNKNOWN", "EMPTY"),  # no asset: coverage remains unresolved
        (60, 90, "GAP", None),  # and past the last clip nothing covers the canvas
    ]


def test_unknown_media_does_not_hide_definite_background_coverage():
    from astrid.core.timeline.onscreen import coverage, element_coverage

    media = _clip('media', 'media', 'fx', asset='A')
    background = _plate(asset=None)
    elements = {**ELEMENTS, **element_coverage(['media'])}
    alone = coverage(_config([media]), FPS, CANVAS, elements)
    with_background = coverage(_config([media, background]), FPS, CANVAS, elements)
    assert alone[0].cls == 'UNKNOWN'
    assert with_background[0].cls == 'COVERED'
