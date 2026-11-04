"""Resolve canonical static regions to binary masks and aligned previews.

White pixels are editable and black pixels are preserved, matching H3 compose.
Preview pixels use cyan for editable and dark gray for preserved. Both artifacts
are PNG bytes, with no source metadata, so the same inputs give the same bytes.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from io import BytesIO
from pathlib import Path
from typing import Mapping, NoReturn

from PIL import Image

from astrid.packs.h3_av.shared.compose import CompositionError, probe_mask
from astrid.packs.h3_av.shared.request import H3RequestError
from astrid.packs.h3_av.shared.spec import Region


class StaticRegionError(H3RequestError):
    """A static region cannot be resolved without guessing its meaning."""


def _fail(code: str, message: str, field: str, fix: str) -> NoReturn:
    raise StaticRegionError(message, code=f"h3.region.{code}", field=field, fix=fix)


@dataclass(frozen=True)
class StaticRegion:
    width: int
    height: int
    mask_png: bytes
    preview_png: bytes


def _png(image: Image.Image) -> bytes:
    output = BytesIO()
    image.save(output, format="PNG", optimize=False, compress_level=9)
    return output.getvalue()


def _image_pixels(path: Path, name: str, width: int, height: int, threshold: int | None) -> bytes:
    # The shared probe recognises image masks and rejects malformed files. Do
    # not pass video through iter_mask_frames: this resolver is still-only, and
    # decoding directly avoids conversion or resizing before binary validation.
    try:
        info = probe_mask(path, mask_id=name)
    except (CompositionError, OSError) as exc:
        _fail("unsupported_image", str(exc), "mask_asset", "Supply a decodable still image mask")
    if info["kind"] != "image":
        _fail("unsupported_image", "A static region needs an image, not video", "mask_asset", "Supply a still image")
    try:
        with Image.open(path) as image:
            if image.format not in {"BMP", "JPEG", "PNG", "TIFF", "WEBP"} or getattr(image, "n_frames", 1) != 1:
                _fail("unsupported_image", "Unsupported or animated mask image", "mask_asset", "Supply a supported single-frame image")
            if image.size != (width, height):
                _fail("image_size", f"Mask image is {image.width}x{image.height}; expected {width}x{height}", "mask_asset", "Supply a mask matching the source dimensions")
            if image.getexif().get(274, 1) != 1:
                _fail("unsupported_image", "Mask orientation metadata is unsupported", "mask_asset", "Supply an image with normal orientation")
            # Alpha, palette, high-bit-depth and color masks have ambiguous
            # permission semantics. RGB is accepted only if strictly gray.
            if image.mode not in {"1", "L", "RGB"}:
                _fail("unsupported_image", f"Unsupported mask image mode {image.mode}", "mask_asset", "Supply a grayscale image without alpha")
            if image.mode == "RGB":
                raw = image.tobytes()
                if any(raw[i] != raw[i + 1] or raw[i] != raw[i + 2] for i in range(0, len(raw), 3)):
                    _fail("unsupported_image", "Color mask channels disagree", "mask_asset", "Supply grayscale pixels")
                pixels = raw[::3]
            else:
                pixels = image.convert("L").tobytes()
    except StaticRegionError:
        raise
    except (OSError, ValueError, SyntaxError) as exc:
        _fail("unsupported_image", f"Cannot decode mask image: {exc}", "mask_asset", "Supply a decodable still image mask")
    if threshold is None:
        if not set(pixels) <= {0, 255}:
            _fail("non_binary", "Mask contains values other than 0 and 255", "mask_asset", "Supply a binary mask or an explicit threshold")
        return pixels
    return bytes(255 if value >= threshold else 0 for value in pixels)


def resolve_static_region(
    region: Region,
    *,
    width: int,
    height: int,
    assets: Mapping[str, str | Path] | None = None,
    polarity: str | None = None,
    threshold: int | None = None,
) -> StaticRegion:
    """Resolve a Region at source dimensions without resampling.

    Image assets require explicit black-preserve/white-edit polarity; the three
    aliases accepted by the v2 normalizer have the same meaning. A threshold,
    if given, is an integer 1..254 and maps values >= threshold to white.
    Boxes are [x, y, width, height], half-open, and must fit within the frame;
    a pixel is selected exactly when its center lies inside the box. Polarity
    and threshold are image options and must not be supplied for a box.
    """
    if type(width) is not int or type(height) is not int or width <= 0 or height <= 0:
        _fail("dimensions", "Dimensions must be positive integers", "dimensions", "Supply the source width and height")
    if not isinstance(region, Region) or (region.mask_asset is None) == (region.box is None):
        _fail("selector", "A region requires exactly one mask asset or box", "region", "Supply one canonical Region selector")
    if region.mask_asset is not None:
        if polarity not in ("black_preserve_white_edit", "black=preserve_white=editable", "black_protected_white_editable"):
            _fail("polarity", "Mask polarity must explicitly be black-preserve/white-edit", "polarity", "Declare black_preserve_white_edit")
        if threshold is not None and (type(threshold) is not int or not 1 <= threshold <= 254):
            _fail("threshold", "Threshold must be an integer from 1 to 254", "threshold", "Choose a threshold from 1 to 254")
        if not isinstance(region.mask_asset, str) or not region.mask_asset.strip() or assets is None or region.mask_asset not in assets:
            _fail("asset", "Mask asset is not resolved", "mask_asset", "Provide a path for the region's asset ID")
        try:
            path = Path(assets[region.mask_asset])
        except (TypeError, ValueError):
            _fail("asset", "Mask asset path is invalid", "mask_asset", "Provide a valid image path")
        pixels = _image_pixels(path, region.mask_asset, width, height, threshold)
    else:
        if polarity is not None or threshold is not None:
            _fail("selector", "Image polarity and threshold cannot be applied to a box", "region", "Remove image options from the box")
        box = region.box
        if not isinstance(box, (tuple, list)) or len(box) != 4 or any(type(n) not in (int, float) or not math.isfinite(n) for n in box):
            _fail("box", "Box must contain four finite numbers", "box", "Supply [x, y, width, height]")
        x, y, w, h = box
        if x < 0 or y < 0 or w <= 0 or h <= 0 or x + w > width or y + h > height:
            _fail("box", "Box must have positive size and fit inside the frame", "box", "Correct the pixel coordinates")
        left, top = max(0, math.ceil(x - 0.5)), max(0, math.ceil(y - 0.5))
        right, bottom = min(width, math.ceil(x + w - 0.5)), min(height, math.ceil(y + h - 0.5))
        data = bytearray(width * height)
        for row in range(top, bottom):
            data[row * width + left:row * width + right] = b"\xff" * (right - left)
        pixels = bytes(data)
    mask = Image.frombytes("L", (width, height), pixels)
    preview = Image.new("RGB", (width, height), (32, 32, 32))
    preview.paste((0, 220, 255), mask=mask)
    return StaticRegion(width, height, _png(mask), _png(preview))


__all__ = ["StaticRegion", "StaticRegionError", "resolve_static_region"]
