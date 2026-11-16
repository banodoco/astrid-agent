from __future__ import annotations

import pytest

from astrid.core.timeline.asset_dimensions import canonicalize_asset_dimensions


def test_agreeing_duplicate_dimensions_collapse_to_resolution():
    assert canonicalize_asset_dimensions("asset-a", {
        "media_id": "m-a", "width": 1920, "height": 1080, "resolution": "1920x1080",
    }) == {"media_id": "m-a", "resolution": "1920x1080"}


def test_missing_resolution_is_derived_from_legacy_dimensions():
    assert canonicalize_asset_dimensions("asset-a", {"width": 640, "height": 480}) == {
        "resolution": "640x480",
    }


def test_conflicting_dimensions_fail_with_asset_and_both_values():
    with pytest.raises(ValueError, match=r"asset-a.*width=640, height=480, resolution='800x600'"):
        canonicalize_asset_dimensions("asset-a", {
            "width": 640, "height": 480, "resolution": "800x600",
        })


@pytest.mark.parametrize("width,height", [(0, 2), (-1, 2), (1.0, 2), (True, 2), (2, None)])
def test_non_positive_or_non_integer_dimensions_fail(width, height):
    with pytest.raises(ValueError, match="asset-a.*width="):
        canonicalize_asset_dimensions("asset-a", {"width": width, "height": height})


def test_canonical_entry_is_untouched():
    entry = {"media_id": "m-a", "resolution": "1920x1080", "custom": {"keep": True}}
    result = canonicalize_asset_dimensions("asset-a", entry)
    assert result == entry
    assert result is not entry
