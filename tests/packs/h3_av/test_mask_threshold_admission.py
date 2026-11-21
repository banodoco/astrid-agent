"""Thresholds are supported for still masks but never for moving masks."""

import pytest

from astrid.packs.h3_av.shared.request import H3RequestError, normalize_request


def _request(mask):
    return {"version": 2, "prompt": "Edit", "duration": 1, "media": [
        {"asset": "source", "role": "timeline", "modality": "video", "at": {"frame": 0},
         "edit": [{"stream": "video", "during": [0, 1], "mask": mask}]},
    ]}


@pytest.mark.parametrize("asset,modalities", [
    ({"id": "mask", "modality": "video"}, None),
    ({"id": "mask", "inspection": {"modality": "video"}}, None),
    ("mask", {"mask": "video"}),
])
def test_known_video_threshold_is_refused_at_normalization(asset, modalities):
    with pytest.raises(H3RequestError, match="unsupported field.*threshold"):
        normalize_request(_request({"asset": asset, "threshold": 128}), asset_modalities=modalities)


@pytest.mark.parametrize("threshold", [1, 128, 254])
def test_image_threshold_is_retained(threshold):
    result = normalize_request(_request({"asset": {"id": "mask", "modality": "image"}, "threshold": threshold}))
    assert result.value["media"][0]["edit"][0]["mask"]["threshold"] == threshold


@pytest.mark.parametrize("threshold", [0, 255, True, 1.5])
def test_image_threshold_limits_are_enforced(threshold):
    with pytest.raises(H3RequestError, match="threshold must be an integer 1..254"):
        normalize_request(_request({"asset": {"id": "mask", "modality": "image"}, "threshold": threshold}))
