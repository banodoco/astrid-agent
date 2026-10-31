from __future__ import annotations

from io import BytesIO

import pytest
from PIL import Image

from astrid.packs.h3_av.actions.prepare.static_regions import StaticRegionError, resolve_static_region
from astrid.packs.h3_av.shared.compose import iter_mask_frames, probe_mask
from astrid.packs.h3_av.shared.spec import Region

POLARITY = "black_preserve_white_edit"


def _pixels(png: bytes):
    with Image.open(BytesIO(png)) as image:
        return image.mode, image.size, list(image.getdata())


def _save(tmp_path, mode, values, *, size=(4, 3), name="mask.png"):
    image = Image.new(mode, size)
    image.putdata(values)
    path = tmp_path / name
    image.save(path)
    return path


def _resolve_image(path, **kwargs):
    return resolve_static_region(
        Region("region", mask_asset="mask"), width=4, height=3,
        assets={"mask": path}, **kwargs,
    )


def _assert_error(code, callback):
    with pytest.raises(StaticRegionError) as caught:
        callback()
    envelope = caught.value.to_envelope()
    assert envelope["code"] == f"h3.region.{code}"
    assert all(envelope[key] for key in ("message", "field", "fix"))


def test_box_geometry_preview_and_repeatability(tmp_path):
    region = Region("window", box=(1, 1, 2, 2))
    result = resolve_static_region(region, width=4, height=3)
    assert result == resolve_static_region(region, width=4, height=3)
    mask = [0, 0, 0, 0, 0, 255, 255, 0, 0, 255, 255, 0]
    assert _pixels(result.mask_png) == ("L", (4, 3), mask)
    assert _pixels(result.preview_png) == (
        "RGB", (4, 3), [(0, 220, 255) if x else (32, 32, 32) for x in mask],
    )
    path = tmp_path / "resolved.png"
    path.write_bytes(result.mask_png)
    assert probe_mask(path)["kind"] == "image"
    assert next(iter_mask_frames(path, start_frame=0, count=1, width=4, height=3)) == bytes(
        channel for value in mask for channel in (value, value, value)
    )


def test_fractional_box_uses_half_open_pixel_centers():
    result = resolve_static_region(Region("r", box=(0.5, 0.5, 2, 1)), width=4, height=3)
    assert _pixels(result.mask_png)[2] == [255, 255, 0, 0] + [0] * 8


@pytest.mark.parametrize("box", [(-1, 0, 1, 1), (0, 0, 0, 1), (3, 0, 2, 1), (0, 0, float("nan"), 1), (0, 0, True, 1)])
def test_bad_boxes_are_coded(box):
    _assert_error("box", lambda: resolve_static_region(Region("r", box=box), width=4, height=3))


def test_image_mask_binary_and_preview_repeatability(tmp_path):
    values = [0, 255, 255, 0] * 3
    path = _save(tmp_path, "L", values)
    result = _resolve_image(path, polarity=POLARITY)
    assert result == _resolve_image(path, polarity=POLARITY)
    assert _pixels(result.mask_png) == ("L", (4, 3), values)
    assert _pixels(result.preview_png)[2] == [(0, 220, 255) if x else (32, 32, 32) for x in values]


def test_threshold_has_explicit_inclusive_boundary(tmp_path):
    values = [0, 127, 128, 255] * 3
    path = _save(tmp_path, "L", values)
    _assert_error("non_binary", lambda: _resolve_image(path, polarity=POLARITY))
    result = _resolve_image(path, polarity=POLARITY, threshold=128)
    assert _pixels(result.mask_png)[2] == [0, 0, 255, 255] * 3


@pytest.mark.parametrize("threshold", [0, 255, -1, 1.5, True, "128"])
def test_bad_threshold_is_coded(tmp_path, threshold):
    path = _save(tmp_path, "L", [0] * 12)
    _assert_error("threshold", lambda: _resolve_image(path, polarity=POLARITY, threshold=threshold))


@pytest.mark.parametrize("polarity", [None, "white_preserve_black_edit", "", "unknown"])
def test_missing_or_bad_polarity_is_coded(tmp_path, polarity):
    path = _save(tmp_path, "L", [0] * 12)
    _assert_error("polarity", lambda: _resolve_image(path, polarity=polarity))


def test_image_size_is_exact(tmp_path):
    path = _save(tmp_path, "L", [0] * 6, size=(3, 2))
    _assert_error("image_size", lambda: _resolve_image(path, polarity=POLARITY))


@pytest.mark.parametrize("mode,values", [
    ("RGBA", [(0, 0, 0, 0)] * 12),
    ("RGB", [(255, 0, 0)] * 12),
    ("P", [0] * 12),
])
def test_ambiguous_image_modes_are_coded(tmp_path, mode, values):
    path = _save(tmp_path, mode, values)
    _assert_error("unsupported_image", lambda: _resolve_image(path, polarity=POLARITY))


def test_unsupported_format_and_invalid_file_are_coded(tmp_path):
    path = tmp_path / "mask.gif"
    Image.new("L", (4, 3)).save(path)
    _assert_error("unsupported_image", lambda: _resolve_image(path, polarity=POLARITY))
    path = tmp_path / "broken.png"
    path.write_bytes(b"not an image")
    _assert_error("unsupported_image", lambda: _resolve_image(path, polarity=POLARITY))


def test_grayscale_rgb_is_supported(tmp_path):
    values = [(0, 0, 0), (255, 255, 255)] * 6
    path = _save(tmp_path, "RGB", values)
    assert _pixels(_resolve_image(path, polarity=POLARITY).mask_png)[2] == [0, 255] * 6


def test_missing_asset_and_invalid_dimensions_are_coded():
    _assert_error("asset", lambda: resolve_static_region(Region("r", mask_asset="missing"), width=4, height=3, polarity=POLARITY))
    _assert_error("dimensions", lambda: resolve_static_region(Region("r", box=(0, 0, 1, 1)), width=True, height=3))
