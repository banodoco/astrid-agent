"""Canonical intrinsic dimensions for timeline asset registry entries."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any


def canonicalize_asset_dimensions(asset_id: str, entry: Mapping[str, Any]) -> dict[str, Any]:
    """Return an entry with intrinsic size represented only by resolution.

    All other fields and values are retained unchanged. Legacy numeric fields
    are accepted only when they are positive integers and agree with resolution.
    """
    result = dict(entry)
    has_width, has_height = "width" in result, "height" in result
    resolution = result.get("resolution")
    if not has_width and not has_height:
        return result
    width, height = result.get("width"), result.get("height")
    if (
        not has_width or not has_height
        or type(width) is not int or type(height) is not int
        or width <= 0 or height <= 0
    ):
        raise ValueError(
            f"asset {asset_id!r} has invalid intrinsic dimensions: "
            f"width={width!r}, height={height!r}"
        )
    expected = f"{width}x{height}"
    if resolution is not None and resolution != expected:
        raise ValueError(
            f"asset {asset_id!r} has conflicting intrinsic dimensions: "
            f"width={width!r}, height={height!r}, resolution={resolution!r}"
        )
    result.pop("width", None)
    result.pop("height", None)
    result["resolution"] = expected
    return result
