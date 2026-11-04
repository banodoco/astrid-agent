from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

from astrid.packs.h3_av.actions.compile.compile import CompilationError, compile_preparation
from astrid.packs.h3_av.shared.request import normalize_request

PILL = {
    "version": 2, "duration": 5,
    "prompt": "The pill in the open palm is deep blue, not red; keep hand, lighting and motion identical.",
    "media": [
        {"id": "src", "asset": "matrix-clip", "role": "timeline", "modality": "video",
         "at": {"frame": 0}, "range": [90, 95],
         "edit": [{"stream": "video", "during": [1.5, 3.5], "mask": {"asset": "pill-mask"}, "guides": ["blue"]}]},
        {"id": "blue", "asset": "blue-pill-ref", "role": "reference", "modality": "image"}],
}
WATERMARK = {
    "version": 2, "duration": 12,
    "prompt": "Clean background in the lower-left corner, no logo or text.",
    "media": [{"id": "src", "asset": "clip", "role": "timeline", "modality": "video",
               "at": {"frame": 0}, "range": [0, 12],
               "edit": [{"stream": "video", "during": [0, 12], "mask": {"asset": "wm-mask"}}]}],
}


def _prepare(raw: dict, tmp_path: Path) -> dict:
    request = normalize_request(raw)
    assets = []
    masks = {
        e["mask"]["asset"] for m in raw["media"] for e in m.get("edit", []) if "asset" in e["mask"]
    }
    for asset in {m["asset"] for m in raw["media"]} | {
        e["mask"]["asset"] for m in raw["media"] for e in m.get("edit", []) if "asset" in e["mask"]
    }:
        path = tmp_path / f"{asset}.bin"
        if asset in masks:
            from PIL import Image
            path = tmp_path / f"{asset}.png"
            Image.new("L", (1024, 576), 255).save(path)
        else:
            path.write_bytes(f"fixture {asset}".encode())
        assets.append({"asset": asset, "status": "resolved", "path": str(path), "size": path.stat().st_size,
                       "sha256": hashlib.sha256(path.read_bytes()).hexdigest()})
    return {"kind": "h3_av_preparation", "status": "prepared", "runtime_submission": "eligible",
            "request": request.value, "request_digest": request.digest, "assets": assets}


def _graph(manifest: dict) -> str:
    return "\n".join(Path(row["path"]).read_text() for name, row in manifest["workflow"].items()
                     if name.endswith((".py", ".json")))


@pytest.mark.parametrize("raw", [PILL, WATERMARK], ids=["pill_recolour", "watermark"])
def test_doc_examples_compile_to_native_nodes(raw, tmp_path):
    manifest = compile_preparation(_prepare(raw, tmp_path), out_dir=tmp_path / "out")
    assert manifest["status"] == "compiled"
    text = _graph(manifest)
    for needed in ("H3V2VGranularFractionalDenoise", "VHS_LoadVideoFFmpeg"):
        assert needed in text
    assert "LanPaint" not in text


def _v2v_block(manifest: dict) -> str:
    text = Path(manifest["workflow"]["workflow.py"]["path"]).read_text()
    start = text.index("raw_call('H3V2VGranularFractionalDenoise'")
    return text[start:text.index(")\n", text.index("source_mask=", start))]


def test_mask_node_is_spatial_crop_disabled_audio_untouched(tmp_path):
    manifest = compile_preparation(_prepare(WATERMARK, tmp_path), out_dir=tmp_path / "out")
    block = _v2v_block(manifest)
    for fragment in ("mode='spatial'", "crop='disabled'", "audio_strength=0,",
                     "inside_strength=1.0,", "outside_strength=0,",
                     "source_mask=loadimagemask.out("):
        assert fragment in block, fragment
    from vibecomfy.cli_loader import load_workflow_any
    api = load_workflow_any(manifest["workflow"]["workflow.py"]["path"]).compile(
        "api", run_inputs=manifest["workflow_inputs"])
    edit = next(row["inputs"] for row in api.values() if row["class_type"] == "H3V2VGranularFractionalDenoise")
    audio = api[edit["source_audio"][0]]
    assert audio["class_type"] == "AudioConcat"
    assert audio["inputs"]["audio1"] == ["99", 2]
    assert audio["inputs"]["direction"] == "after"
    silence = api[audio["inputs"]["audio2"][0]]
    assert silence["class_type"] == "EmptyAudio"
    assert silence["inputs"]["duration"] == manifest["capabilities"]["generation_timing"]["trim_tail_frames"] / 24
    text = Path(manifest["workflow"]["workflow.py"]["path"]).read_text()
    assert "LoadImageMask(channel='red'" in text
    # The sampler and scheduler take the V2V node's model and latent.
    assert "latent_image=h3v2vgranularfractionaldenoise.out('latent')" in text
    assert "model=h3v2vgranularfractionaldenoise.out('model')" in text


def test_source_window_comes_from_range(tmp_path):
    manifest = compile_preparation(_prepare(PILL, tmp_path), out_dir=tmp_path / "out")
    text = Path(manifest["workflow"]["workflow.py"]["path"]).read_text()
    assert "start_time=90.0" in text and "frame_load_cap=120" in text


def test_rectangle_masks_bind_resolved_static_region(tmp_path):
    raw = json.loads(json.dumps(WATERMARK))
    raw["media"][0]["edit"][0]["mask"] = {"rectangle": [0, 0, 10, 10]}
    manifest = compile_preparation(_prepare(raw, tmp_path), out_dir=tmp_path / "out")
    from vibecomfy.cli_loader import load_workflow_any
    workflow = load_workflow_any(manifest["workflow"]["workflow.py"]["path"])
    api = workflow.compile("api", run_inputs=manifest["workflow_inputs"])
    mask = next(row for row in api.values() if row["class_type"] == "LoadImageMask")
    assert mask["inputs"]["channel"] == "red"
    assert manifest["capabilities"]["edit_binding"]["during_frames"] == [0, 288]


def _video_mask_prepare(tmp_path: Path, frames: int) -> dict:
    from tests.packs.h3_av.test_moving_masks import _mask_video

    raw = json.loads(json.dumps(PILL))
    raw["media"][0]["edit"][0]["mask"] = {"asset": "pill-mask"}
    prepared = _prepare(raw, tmp_path)
    mask = tmp_path / "pill-mask.mkv"
    _mask_video(mask, frames=frames)
    for record in prepared["assets"]:
        if record["asset"] == "pill-mask":
            record.update(path=str(mask), size=mask.stat().st_size,
                          sha256=hashlib.sha256(mask.read_bytes()).hexdigest())
    return prepared


@pytest.mark.ffmpeg
def test_native_masked_edit_video_mask_binding(tmp_path):
    import zipfile

    manifest = compile_preparation(_video_mask_prepare(tmp_path, 120), out_dir=tmp_path / "out")
    text = Path(manifest["workflow"]["workflow.py"]["path"]).read_text()
    assert "start_time=0.0" in text and text.count("frame_load_cap=120") == 2
    assert "imagetomask = ImageToMask(" in text
    from vibecomfy.cli_loader import load_workflow_any
    api = load_workflow_any(manifest["workflow"]["workflow.py"]["path"]).compile(
        "api", run_inputs=manifest["workflow_inputs"])
    mask = next(row for row in api.values() if row["class_type"] == "ImageToMask")
    batch = api[mask["inputs"]["image"][0]]
    assert batch["class_type"] == "ImageBatch"
    loader = api[batch["inputs"]["image1"][0]]
    assert loader["class_type"] == "VHS_LoadVideoFFmpeg"
    assert loader["inputs"]["start_time"] == 0
    assert loader["inputs"]["frame_load_cap"] == 120
    preserve = api[batch["inputs"]["image2"][0]]
    assert preserve["class_type"] == "EmptyImage"
    assert preserve["inputs"]["color"] == 0
    assert preserve["inputs"]["batch_size"] == 4
    assert "channel='red'" in text and "LoadImageMask" not in text
    block = _v2v_block(manifest)
    assert "mode='spatial'" in block and "source_mask=imagetomask.out('MASK')" in block
    assert "node=ref('vhs_loadvideoffmpeg_2'), field='video'" in text and "'video_mask_0'" in text
    assert "vhs_loadvideoffmpeg_2.out('MASK')" not in text
    assert manifest["capabilities"]["mask_binding"] == "per-frame video mask"
    archives = [Path(row["path"]) for row in _all_files(manifest) if str(row["path"]).endswith("managed-assets.zip")]
    assert archives, "managed-assets.zip missing"
    row = next(row for row in manifest["managed_assets"]["manifest"]["assets"]
               if row["binding"] == "video_mask_0")
    with zipfile.ZipFile(archives[0]) as zf:
        assert hashlib.sha256(zf.read(row["member"])).hexdigest() == row["sha256"]


def _all_files(manifest: dict):
    stack = [manifest]
    while stack:
        item = stack.pop()
        if isinstance(item, dict):
            if "path" in item and isinstance(item["path"], str):
                yield item
            stack.extend(item.values())
        elif isinstance(item, list):
            stack.extend(item)


@pytest.mark.ffmpeg
def test_native_masked_edit_video_mask_frame_mismatch_raises(tmp_path):
    with pytest.raises(CompilationError, match="frames"):
        compile_preparation(_video_mask_prepare(tmp_path, 100), out_dir=tmp_path / "out")
