"""Explicit frame counts must survive admission, including invalid zero."""
import pytest
from astrid.core.generation import entries


@pytest.mark.parametrize("frames", [0, -1, 4, 363])
def test_h3_plain_invalid_frames_are_rejected(frames):
    namespace = entries.entry_hooks(entries.find_entry("h3"))["build"].__globals__
    with pytest.raises(ValueError, match="duration|frames"):
        document, _ = namespace["_plain_request"](
            {"prompt": "a kite", "frames": frames}, {"image_ref": "/x/look.png"}, object())
        namespace["_shared"]("request").normalize_request(document)


def test_h3_plain_omitted_frames_still_defaults_to_120():
    namespace = entries.entry_hooks(entries.find_entry("h3"))["build"].__globals__
    document, _ = namespace["_plain_request"](
        {"prompt": "a kite"}, {"image_ref": "/x/look.png"}, object())
    assert document["output"]["duration"] == 120 / 24
