"""The marked skill requests must pass public CPU dry-run admission unchanged."""
from __future__ import annotations

import copy
import re
import subprocess
from pathlib import Path

import pytest
import yaml

from astrid.core.generation import entries
from astrid.core.generation.preflight import GenerationReadinessError
from astrid.packs.h3_av.shared.request import normalize_request
from astrid.packs.h3_av.shared.spec import H3SpecError, canonical_spec
from astrid.sdk.generation import generate

pytestmark = pytest.mark.ffmpeg
SKILL = Path(__file__).resolve().parents[3] / "astrid/packs/h3_av/docs/SKILL.md"
EXAMPLES = ("temporal-edit", "first-frame-via-still", "regional-mask")


@pytest.fixture
def examples():
    blocks = re.findall(r"```yaml\n# example: ([\w-]+)\n(.*?)\n```", SKILL.read_text(), re.S)
    assert blocks, "no example found in h3 SKILL.md"
    assert len(blocks) == len(EXAMPLES) and {name for name, _ in blocks} == set(EXAMPLES)
    return {name: yaml.safe_load(body) for name, body in blocks}


def _ffmpeg(*args):
    return subprocess.run(
        ["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", *map(str, args)],
        check=True, capture_output=True, timeout=60,
    ).stdout


@pytest.fixture
def assets(tmp_path, examples):
    # Both the moving source and the still clip have bounded source audio.
    for name, pattern in (("bike.mp4", "testsrc2=s=1024x576:r=24:d=3"),
                          ("bike-still.mp4", "color=c=blue:s=1024x576:r=24:d=3")):
        _ffmpeg("-f", "lavfi", "-i", pattern, "-f", "lavfi", "-i",
                "sine=frequency=440:duration=3", "-t", "3", "-c:v", "libx264",
                "-preset", "ultrafast", "-pix_fmt", "yuv420p", "-c:a", "aac",
                "-shortest", tmp_path / name)
    _ffmpeg("-f", "lavfi", "-i", "color=c=black:s=1024x576:d=1",
            "-vf", "drawbox=x=384:y=144:w=256:h=288:color=white:t=fill",
            "-frames:v", "1", "-threads", "1", tmp_path / "frame-mask.png")
    return {name: str(tmp_path / name) for name in ("bike.mp4", "bike-still.mp4", "frame-mask.png")}


def _request_file(tmp_path, document):
    path = tmp_path / "request.yaml"
    path.write_text(yaml.safe_dump(document))
    return str(path)


@pytest.mark.parametrize("name", EXAMPLES)
def test_skill_example_is_admitted(name, examples, assets, tmp_path, monkeypatch):
    observed = {}
    real_build = entries.build_entry

    def capture_build(*args, **kwargs):
        # Observe the real hook before the SDK removes its temporary job directory.
        built = real_build(*args, **kwargs)
        observed["graph"] = built["workflow"].compile("api", run_inputs=built["bindings"])
        if "video_mask_0" in built["assets"]:
            observed["mask"] = _ffmpeg(
                "-i", built["assets"]["video_mask_0"], "-t", "3", "-vf", "scale=1:1",
                "-pix_fmt", "gray", "-f", "rawvideo", "pipe:1")
        return built

    monkeypatch.setattr(entries, "build_entry", capture_build)
    result = generate.video(entry="h3", mode="video-edit", placement="runpod", dry_run=True,
                            request=_request_file(tmp_path, examples[name]), asset_map=assets)
    assert result["status"] == "ready", result
    assert result["issues"] == [] and result["fixes"] == []
    graph = observed["graph"]
    native = [node for node in graph.values() if node["class_type"] == "H3V2VGranularFractionalDenoise"]
    assert len(native) == 1
    inputs = native[0]["inputs"]
    assert inputs["mode"] == "spatial" and inputs["crop"] == "disabled"
    assert inputs["outside_strength"] == 0 and inputs["audio_strength"] == 0
    assert graph["99"]["class_type"] == "VHS_LoadVideoFFmpeg"
    assert graph["99"]["inputs"]["frame_load_cap"] == 72
    assert not any(node["class_type"].startswith("LanPaint") for node in graph.values())
    if name == "regional-mask":
        loader = graph[inputs["source_mask"][0]]
        assert loader["class_type"] == "LoadImageMask"
        assert loader["inputs"]["channel"] == "red"
        assert "mask_0" in result["entry"]["bindings"]
        assert "video_mask_0" not in result["entry"]["bindings"]
    else:
        kept = 36 if name == "temporal-edit" else 4
        assert observed["mask"] == bytes(kept) + bytes([255]) * (72 - kept)
        assert "video_mask_0" in result["entry"]["bindings"]
        assert any(node["class_type"] == "ImageToMask" for node in graph.values())


def test_skill_edit_audio_generation_is_rejected(examples, assets, tmp_path):
    document = copy.deepcopy(examples["temporal-edit"])
    document["changes"]["audio"] = [{"during": [1.5, 3.0], "action": "generate"}]
    with pytest.raises(H3SpecError, match="Only video regeneration is supported") as pack_error:
        canonical_spec(normalize_request(document))
    assert pack_error.value.code == "h3.spec.unsupported_op"
    with pytest.raises(GenerationReadinessError, match="Only video regeneration is supported") as public_error:
        generate.video(entry="h3", mode="video-edit", placement="runpod", dry_run=True,
                       request=_request_file(tmp_path, document), asset_map=assets)
    assert public_error.value.code == "input_rejected"
