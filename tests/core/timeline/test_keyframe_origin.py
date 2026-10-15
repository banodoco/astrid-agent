from copy import deepcopy
import json
from pathlib import Path

import pytest

from astrid.core.timeline import clip_fields
from astrid.core.timeline.banodoco_schema import canonical_timeline_config
from astrid.sdk.timeline.compiler import compile_timing


def test_both_legacy_adapters_declare_origin_without_mutating_or_retiming_keys():
    clip = {"id": "sprite", "at": 12.345, "from": 7, "to": 20, "speed": 2,
            "keyframes": {"x": [{"time": 12.345, "value": 1}]},
            "params": {"keyframes": [{"frame": 6, "x": 1, "y": 2}]}}
    before = deepcopy(clip)
    app = clip_fields.declare_keyframe_origin(clip, "app-seconds")
    sprite = clip_fields.declare_keyframe_origin(clip, "sprite-frames")
    assert app["keyframeOrigin"] == -12.345
    assert sprite["keyframeOrigin"] == 0
    assert app["keyframes"] is clip["keyframes"]
    assert sprite["params"] is clip["params"]
    assert clip == before
    assert clip_fields.declare_keyframe_origin(app, "sprite-frames") == app


def test_codec_and_canonical_document_preserve_the_declared_origin():
    clip = {"id": "c", "at": 2, "track": "v", "hold": 3}
    clip_fields.write(clip, "keyframe_origin", -2)
    config = {"theme": "default", "tracks": [{"id": "v", "kind": "visual", "label": "v"}], "clips": [clip]}
    assert clip_fields.read(clip, "keyframe_origin") == -2
    assert canonical_timeline_config(config)["clips"][0]["keyframeOrigin"] == -2
    clip_fields.write(clip, "keyframe_origin", None)
    assert "keyframeOrigin" not in clip


@pytest.mark.parametrize("bad", [None, True, "0", float("nan"), float("inf")])
def test_invalid_declared_origins_are_refused(bad):
    with pytest.raises(ValueError, match="finite seconds"):
        clip_fields.declare_keyframe_origin({"at": 1, "keyframeOrigin": bad}, "sprite-frames")


def test_compiler_keeps_declared_key_times_and_origin_through_resolution_and_trim():
    # Source fixture is disposable. Added keys exercise the contract; this is
    # not historical tiny/golden/v8 keyframe parity evidence.
    path = Path(__file__).resolve().parents[2] / "fixtures/timeline_editing/tiny.json"
    bundle = json.loads(path.read_text())
    from astrid.sdk.timeline_checkout import Checkout
    clip = Checkout(bundle).clip("c1.rocket").data
    clip["params"] = dict(clip.get("params", {}), keyframes=[{"frame": 0, "x": 0}, {"frame": 12, "x": 2}])
    clip_fields.write(clip, "keyframe_origin", 0)
    expected = deepcopy(clip["params"]["keyframes"])
    for trimmed in [False, True]:
        if trimmed:
            clip.update({"from": 1, "to": 3, "speed": 2})
        result = compile_timing(bundle)
        actual = Checkout(result.bundle).clip("c1.rocket").data
        assert actual["keyframeOrigin"] == 0
        assert actual["params"]["keyframes"] == expected


def test_generated_remotion_clip_and_allowlist_declare_the_origin():
    from scripts.gen_remotion_types import generate
    generated = generate()
    assert "keyframeOrigin?: number;" in generated
    assert "'keyframeOrigin'" in generated.split("export const _CLIP_ALLOWED =", 1)[1].split("as const", 1)[0]
