"""DAMAGED-ART fires from the real pixel measurement on an asset with no stored provenance (the film's stickers).

These tests never inject an integrity value: the check reads a real PNG and runs
astrid.core.media.outline_integrity.
"""
from __future__ import annotations

import io
import sys

import numpy as np
from PIL import Image

from astrid.sdk.timeline_checkout import Checkout
from tests.sdk.test_timeline_checkout import bundle


def _sprite_png(path, *, dark_sides: str) -> None:
    """A 12x12 canvas with a 10x10 light square; the 1-px rim is dark on the named sides (t, b, l, r)."""
    arr = np.zeros((12, 12, 4), dtype=np.uint8)
    arr[1:11, 1:11] = (200, 200, 200, 255)
    dark = (10, 10, 10, 255)
    if "t" in dark_sides:
        arr[1, 1:11] = dark
    if "b" in dark_sides:
        arr[10, 1:11] = dark
    if "l" in dark_sides:
        arr[1:11, 1] = dark
    if "r" in dark_sides:
        arr[1:11, 10] = dark
    Image.fromarray(arr, "RGBA").save(path)


def _checked(path):
    data = bundle()
    internal = data["shots"]["A"]["internal_timeline"]
    # No pixel_provenance anywhere: only the file on disk can say how damaged the art is.
    internal["registry"]["assets"]["R"] = {"media_id": "m-r", "local_path": str(path)}
    return Checkout(data)


def test_damaged_art_fires_without_stored_provenance(tmp_path) -> None:
    png = tmp_path / "rocket.png"
    _sprite_png(png, dark_sides="tb")  # the side walls missing: 0.56
    notes = _checked(png).check().problems
    damaged = [line for line in notes if "DAMAGED-ART" in line]
    assert damaged and "a-rocket" in damaged[0], notes


def test_intact_and_outline_free_art_is_not_flagged(tmp_path) -> None:
    for sides in ("tblr", ""):  # complete outline (1.0), no outline by style (0.0)
        png = tmp_path / f"s-{sides or 'none'}.png"
        _sprite_png(png, dark_sides=sides)
        assert not [line for line in _checked(png).check().problems if "DAMAGED-ART" in line], sides


def test_native_integrity_check_does_not_import_pixel_pack(tmp_path, monkeypatch) -> None:
    png = tmp_path / "rocket.png"
    _sprite_png(png, dark_sides="tb")
    monkeypatch.setitem(sys.modules, "astrid.packs.pixel.actions._common", None)
    notes = _checked(png).check().problems
    assert any("DAMAGED-ART" in line for line in notes), notes


def test_pixel_provenance_measures_a_snap_output_the_same_way(monkeypatch, tmp_path) -> None:
    from astrid.sdk import media_handles

    png = tmp_path / "snap.png"
    _sprite_png(png, dark_sides="tb")
    buffer = io.BytesIO(png.read_bytes())

    class _Media:
        def read_bytes(self, digest):
            return buffer.getvalue() if digest == "sha256:" + "a" * 64 else b'{"grid": 4}'

    class _Tasks:
        def list_managed_outputs(self, task_id):
            return [{"output_port": "report", "digest": "sha256:" + "b" * 64}]

    class _Client:
        media = _Media()
        tasks = _Tasks()

    lineage = {"output": {"task_id": "t1", "output_port": "native", "run_id": "r1"}}
    got = media_handles._pixel_provenance(_Client(), lineage, "sha256:" + "a" * 64)
    assert 0.1 <= got["outline_integrity"]["fraction"] < 0.75
