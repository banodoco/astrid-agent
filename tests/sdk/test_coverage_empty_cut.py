"""Coverage per frame at check time (TRUTH.md §2): a cut left with only its background plate is EMPTY, an inset
or a translucent plate does not cover the canvas, and a declared fade is a DIP, not a GAP.
"""
from __future__ import annotations

import copy
import json
from pathlib import Path

from astrid.sdk.timeline_checkout import Checkout

FPS = 30
# A complete, publishable candidate (schema 1, pinned base, source mapping). check() refuses an incomplete
# candidate outright (valid False), so the skeleton is the repo fixture; only the shots and tracks are ours.
PUBLISHABLE = Path(__file__).resolve().parents[1] / "fixtures" / "timeline_editing" / "tiny_publishable.json"
TRACKS = [
    {"id": "fx", "kind": "visual"}, {"id": "type", "kind": "visual"},
    {"id": "sprite", "kind": "visual"}, {"id": "plate", "kind": "visual"},
]


def _shot(fixture_shot, clips):
    """The fixture's shot (its shot_id and base_* fields), with our clips on the four visual tracks."""
    shot = copy.deepcopy(fixture_shot)
    shot["payload"] = {"name": "SHOT"}
    shot["internal_timeline"] = {"tracks": TRACKS, "clips": clips, "registry": {
        "assets": {"P": {"media_id": "sha256:c57bc0c5ceafe2c7fff32d0d16a3a4b0b4bb7885238ac376c70dbf53bd669f44"}}}}
    return shot


def bundle(a, b):
    """Shot A (0–4 s) is cut c01, a picture. Shot B (4–8 s) is cut c02, the span under test."""
    doc = copy.deepcopy(json.loads(PUBLISHABLE.read_text()))
    fixture = doc["shots"]
    a_clips = [{"id": "a-plate", "clipType": "am-snap-plate", "track": "plate", "asset": "P", "at": 0.0, "hold": 4.0,
                "params": {}, "app": {"cut": "c01", "layer": "field"}}] + a
    doc["shots"] = {"A": _shot(fixture["A"], a_clips), "B": _shot(fixture["B"], b)}
    return doc


def _empty_cut_bundle(**plate_fields):
    """c02 with ONLY its background plate: no asset, no params.src (the element.yaml 'flat background-only plate')."""
    plate = {"id": "b-plate", "clipType": "am-snap-plate", "track": "plate", "at": 0.0, "hold": 4.0,
             "params": {"fill": "#F7F4ED"}, "app": {"cut": "c02", "layer": "field"}}
    plate.update(plate_fields)
    return bundle([], [plate])


def _advice(report, *words):
    """Scene findings are advisory (PLAN: checks never block a save): problems, never blocking."""
    assert report.valid is True and not report.blocking, report.blocking
    return [line for line in report.problems if all(w in line for w in words)]


def test_a_cut_left_with_only_a_background_plate_is_not_valid():
    report = Checkout(_empty_cut_bundle()).check()
    assert _advice(report, "EMPTY", "c02", "4.00"), report.problems


def test_a_deliberate_hold_waives_an_empty_cut():
    b = _empty_cut_bundle()
    # Without the hold: the empty cut is a sustained finding.
    report = Checkout(b).check()
    assert _advice(report, "EMPTY", "c02")
    # With the cut's deliberate hold (stored on the cut: parent.config.cut_notes): EMPTY only as info.
    held = Checkout(b)
    held.set_cut_note("c02", hold="deliberate")
    report = held.check()
    assert report.valid is True, report.blocking
    assert not any("EMPTY" in line for line in report.blocking)
    assert any("EMPTY" in line and "c02" in line for line in report.problems), report.problems


def test_an_inset_footage_does_not_cover_the_canvas():
    inset = {"id": "b-inset", "clipType": "am-footage", "track": "fx", "asset": "P", "at": 0.0, "hold": 4.0,
             "params": {"x": 1200, "y": 600, "scale": 0.3}, "app": {"cut": "c02", "layer": "inset"}}
    report = Checkout(bundle([], [inset])).check()
    assert _advice(report, "GAP") or _advice(report, "nothing covers the canvas"), report.problems


def test_a_translucent_plate_does_not_cover_the_canvas():
    report = Checkout(_empty_cut_bundle(asset="P", params={}, opacity=0.4)).check()
    assert _advice(report, "GAP") or _advice(report, "nothing covers the canvas"), report.problems


def test_a_declared_fade_in_is_a_dip_not_a_gap():
    report = Checkout(_empty_cut_bundle(asset="P", params={}, effects={"fade_in": 0.5})).check()
    assert report.valid is True, report.blocking
    assert not _advice(report, "GAP")
    assert any("DIP" in line and "c02" in line and "0.50" in line for line in report.problems), report.problems

