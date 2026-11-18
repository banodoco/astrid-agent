from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

from astrid.packs.h3_av.shared.compile import CompilationError, compile_preparation
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

    prepared = _video_mask_prepare(tmp_path, 120)
    # The legacy 12px fixture below vanishes; use visible moving support here.
    import subprocess
    mask = tmp_path / "large-moving-mask.mkv"
    frames = []
    for index in range(120):
        pixels = bytearray(1024 * 576)
        x0 = 2 * index
        for y in range(8, 40):
            pixels[y * 1024 + x0:y * 1024 + x0 + 32] = b"\xff" * 32
        frames.append(bytes(pixels))
    subprocess.run(["ffmpeg", "-v", "error", "-y", "-f", "rawvideo", "-pix_fmt", "gray", "-s", "1024x576", "-r", "24", "-i", "-", "-c:v", "ffv1", "-pix_fmt", "gray", str(mask)], input=b"".join(frames), check=True, capture_output=True)
    for record in prepared["assets"]:
        if record["asset"] == "pill-mask":
            record.update(path=str(mask), size=mask.stat().st_size, sha256=hashlib.sha256(mask.read_bytes()).hexdigest())
    manifest = compile_preparation(prepared, out_dir=tmp_path / "out")
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


@pytest.mark.ffmpeg
def test_legacy_moving_mask_vanishing_grant_is_refused(tmp_path):
    with pytest.raises(CompilationError, match=r"grant occurrence-1\.edit\[0\] vanishes"):
        compile_preparation(_video_mask_prepare(tmp_path, 120), out_dir=tmp_path / "out")


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


def _multi_grant_request():
    return {"version": 2, "duration": 1, "prompt": "A scene", "media": [{
        "id": "source", "asset": "clip", "role": "timeline", "modality": "video",
        "at": {"frame": 0}, "range": [0, 1], "edit": [
            {"stream": "video", "during": [0, 1], "mask": {"rectangle": [0, 0, 128, 128]}, "strength": 0.25},
            {"stream": "video", "during": [0, 1], "mask": {"rectangle": [64, 0, 128, 128]}, "strength": 0.75},
        ],
    }]}


def test_multiple_grants_emit_independent_sampler_strength_mask(tmp_path):
    import io
    import subprocess
    import zipfile
    from PIL import Image

    raw = _multi_grant_request()
    raw["media"][0]["edit"][0]["during"] = [0, 0.75]
    raw["media"][0]["edit"][1]["during"] = [0.25, 1]
    compiled = compile_preparation(_prepare(raw, tmp_path), out_dir=tmp_path / "out")
    assert compiled["capabilities"]["sampler_mask_precision"] == "8-bit"
    row = next(row for row in compiled["managed_assets"]["manifest"]["assets"] if row["binding"] == "video_mask_0")
    with zipfile.ZipFile(compiled["managed_assets"]["path"]) as archive:
        mask = tmp_path / "strength.mkv"
        mask.write_bytes(archive.read(row["member"]))
    frame = subprocess.run(["ffmpeg", "-v", "error", "-i", str(mask), "-frames:v", "1", "-f", "image2pipe", "-vcodec", "png", "-"], check=True, capture_output=True).stdout
    with Image.open(io.BytesIO(frame)) as image:
        image = image.convert("L")
        assert image.getpixel((32, 32)) == 64
        assert image.getpixel((96, 32)) == 64
        assert image.getpixel((300, 32)) == 0
    overlap = subprocess.run(["ffmpeg", "-v", "error", "-i", str(mask), "-vf", "select=eq(n\\,12)", "-frames:v", "1", "-f", "image2pipe", "-vcodec", "png", "-"], check=True, capture_output=True).stdout
    with Image.open(io.BytesIO(overlap)) as image:
        assert image.convert("L").getpixel((96, 32)) == 191


def test_zero_strength_keeps_source_but_does_not_suppress_overlap():
    from astrid.packs.h3_av.shared.prepare import _v2_mask_schedule
    raw = _multi_grant_request()
    raw["media"][0]["edit"][0]["strength"] = 0
    schedule = _v2_mask_schedule(normalize_request(raw))
    assert len(schedule["video"]["changes"]) == 1
    assert schedule["video"]["generated_intervals"] == [[0.0, 1.0]]
    raw["media"][0]["edit"][1]["strength"] = 0
    schedule = _v2_mask_schedule(normalize_request(raw))
    assert schedule["video"]["changes"] == []
    assert schedule["video"]["protected_intervals"] == [[0.0, 1.0]]


def test_grant_that_vanishes_on_model_grid_names_the_grant(tmp_path):
    raw = _multi_grant_request()
    raw["media"][0]["edit"] = [raw["media"][0]["edit"][0]]
    raw["media"][0]["edit"][0]["mask"] = {"rectangle": [0, 0, 1, 1]}
    with pytest.raises(CompilationError, match=r"grant occurrence-1\.edit\[0\] vanishes"):
        compile_preparation(_prepare(raw, tmp_path), out_dir=tmp_path / "out")


@pytest.mark.ffmpeg
def test_visible_moving_mask_composes_pixels_on_both_sides(tmp_path):
    import subprocess
    from tests.packs.h3_av.test_moving_masks import _fixtures, _prepare as prepare_composition
    from astrid.packs.h3_av.shared.compose import compose_candidate

    source, generated = _fixtures(tmp_path)
    mask = tmp_path / "visible-mask.mkv"
    frames = []
    for index in range(24):
        frame = bytearray(b"\x00\xff\x00" * (1024 * 576))
        for y in range(8, 40):
            x = index * 2
            offset = (y * 1024 + x) * 3
            frame[offset:offset + 32 * 3] = b"\xff\x00\x00" * 32
        frames.append(bytes(frame))
    subprocess.run(["ffmpeg", "-v", "error", "-y", "-f", "rawvideo", "-pix_fmt", "rgb24", "-s", "1024x576", "-r", "24", "-i", "-", "-c:v", "ffv1", "-pix_fmt", "gbrp", str(mask)], input=b"".join(frames), check=True, capture_output=True)
    preparation = prepare_composition(source, mask)
    result = compose_candidate(preparation=preparation, generated=generated, source=source, out_dir=tmp_path / "composed")
    def first_frame(path):
        return subprocess.run(["ffmpeg", "-v", "error", "-i", str(path), "-frames:v", "1", "-vf", "scale=1024:576:flags=lanczos,format=gbrp,format=rgb24", "-f", "rawvideo", "-pix_fmt", "rgb24", "-"], check=True, capture_output=True).stdout
    original, replacement, delivered = map(first_frame, (source, generated, result["candidate"]["path"]))
    def pixel(data, x, y):
        offset = (y * 1024 + x) * 3
        return data[offset:offset + 3]
    assert pixel(delivered, 12, 12) == pixel(replacement, 12, 12)
    assert pixel(delivered, 80, 12) == pixel(original, 80, 12)
    from astrid.packs.h3_av.shared.verify import verify_candidate
    verification = verify_candidate(preparation=preparation, composition=result, source=source)
    evidence = verification["preservation"]["spatial_complement"][0]
    assert evidence["protected_pixel_count_per_frame"] == [1024 * 576 - 32 * 32] * 24


@pytest.mark.parametrize("conflict", ["seed", "references"])
def test_shared_pass_refuses_conflicting_grant_controls(tmp_path, conflict):
    raw = _multi_grant_request()
    if conflict == "seed":
        raw["media"][0]["edit"][0]["seed"] = 1
        raw["media"][0]["edit"][1]["seed"] = 2
    else:
        raw["media"].extend([
            {"id": "a", "asset": "a", "role": "reference", "modality": "image"},
            {"id": "b", "asset": "b", "role": "reference", "modality": "image"},
        ])
        raw["media"][0]["edit"][0]["guides"] = ["a"]
        raw["media"][0]["edit"][1]["guides"] = ["b"]
    with pytest.raises(CompilationError, match="separate sequential passes"):
        compile_preparation(_prepare(raw, tmp_path), out_dir=tmp_path / "out")


@pytest.mark.ffmpeg
def test_zero_strength_composition_keeps_source_and_positive_overlap_wins(tmp_path):
    import subprocess
    from tests.packs.h3_av.test_moving_masks import _fixtures
    from astrid.packs.h3_av.shared.compose import compose_candidate
    from astrid.packs.h3_av.shared.prepare import prepare_request

    source, generated = _fixtures(tmp_path)
    raw = _multi_grant_request()
    raw["media"][0]["edit"][0]["strength"] = 0
    def decode(path):
        return subprocess.run(["ffmpeg", "-v", "error", "-i", str(path), "-frames:v", "1", "-vf", "scale=1024:576:flags=lanczos,format=gbrp,format=rgb24", "-f", "rawvideo", "-pix_fmt", "rgb24", "-"], check=True, capture_output=True).stdout
    def pixel(data, x, y):
        offset = (y * 1024 + x) * 3
        return data[offset:offset + 3]
    prepared = prepare_request(normalize_request(raw), asset_map={"clip": str(source)})
    result = compose_candidate(preparation=prepared, generated=generated, source=source, out_dir=tmp_path / "positive")
    original, replacement, delivered = map(decode, (source, generated, result["candidate"]["path"]))
    assert pixel(delivered, 32, 32) == pixel(original, 32, 32)
    assert pixel(delivered, 96, 32) == pixel(replacement, 96, 32)
    assert pixel(delivered, 300, 32) == pixel(original, 300, 32)
    raw["media"][0]["edit"][1]["strength"] = 0
    prepared = prepare_request(normalize_request(raw), asset_map={"clip": str(source)})
    result = compose_candidate(preparation=prepared, generated=generated, source=source, out_dir=tmp_path / "zero")
    assert Path(result["candidate"]["path"]).read_bytes() == source.read_bytes()


def test_mask_verification_uses_only_the_red_permission_channel():
    from astrid.packs.h3_av.shared.compose import _protected_from_rgb
    # Black, green-only and blue-only pixels are protected; red is editable.
    assert _protected_from_rgb(bytes((0, 0, 0, 0, 255, 0, 0, 0, 255, 255, 0, 0))) == [0, 1, 2]


def test_partial_window_mask_preprocessing_preserves_red_and_green_channels(tmp_path):
    import subprocess
    from astrid.packs.h3_av.shared.compile import _bounded_edit_mask

    raw = bytearray(b"\x00\xff\x00" * (1024 * 576))
    for y in range(8, 40):
        start = (y * 1024 + 8) * 3
        raw[start:start + 32 * 3] = b"\xff\x00\x00" * 32
    mask = tmp_path / "colored-mask.mkv"
    subprocess.run(["ffmpeg", "-v", "error", "-y", "-f", "rawvideo", "-pix_fmt", "rgb24", "-s", "1024x576", "-r", "24", "-i", "-", "-c:v", "ffv1", "-pix_fmt", "gbrp", str(mask)], input=bytes(raw) * 3, check=True, capture_output=True)
    bounded = _bounded_edit_mask(mask, kind="video", frames=3, during=(1, 2), destination=tmp_path)
    decoded = subprocess.run(["ffmpeg", "-v", "error", "-i", str(bounded), "-f", "rawvideo", "-pix_fmt", "rgb24", "-"], check=True, capture_output=True).stdout
    frame_bytes = 1024 * 576 * 3
    def pixel(frame, x, y):
        offset = frame * frame_bytes + (y * 1024 + x) * 3
        return decoded[offset:offset + 3]
    assert pixel(0, 12, 12) == b"\x00\x00\x00"
    assert pixel(1, 12, 12) == b"\xff\x00\x00"
    assert pixel(1, 80, 12) == b"\x00\xff\x00"
    assert pixel(2, 12, 12) == b"\x00\x00\x00"
    assert set(decoded[0::3]) <= {0, 255}


@pytest.mark.ffmpeg
def test_intermediate_red_video_permission_is_refused(tmp_path):
    import subprocess

    raw = _multi_grant_request()
    raw["media"][0]["edit"] = [raw["media"][0]["edit"][0]]
    raw["media"][0]["edit"][0]["mask"] = {"asset": "mask"}
    prepared = _prepare(raw, tmp_path)
    mask = tmp_path / "nonbinary-mask.mkv"
    frame = bytes((128, 255, 0)) * (1024 * 576)
    subprocess.run(["ffmpeg", "-v", "error", "-y", "-f", "rawvideo", "-pix_fmt", "rgb24", "-s", "1024x576", "-r", "24", "-i", "-", "-c:v", "ffv1", "-pix_fmt", "gbrp", str(mask)], input=frame * 24, check=True, capture_output=True)
    for record in prepared["assets"]:
        if record["asset"] == "mask":
            record.update(path=str(mask), size=mask.stat().st_size, sha256=hashlib.sha256(mask.read_bytes()).hexdigest())
    with pytest.raises(CompilationError, match=r"grant occurrence-1\.edit\[0\].*binary \(0 or 255\)"):
        compile_preparation(prepared, out_dir=tmp_path / "out")


def test_masked_edit_accepts_nine_images_and_video_reference(tmp_path):
    from vibecomfy.cli_loader import load_workflow_any
    raw = _multi_grant_request()
    raw["media"][0]["edit"] = [raw["media"][0]["edit"][0]]
    raw["media"].extend({"id": f"image-{i}", "asset": f"image-{i}", "role": "reference", "modality": "image"} for i in range(9))
    raw["media"].append({"id": "video", "asset": "video", "role": "reference", "modality": "video", "audio": False})
    compilation = compile_preparation(_prepare(raw, tmp_path), out_dir=tmp_path / "out")
    caps = compilation["capabilities"]
    assert caps["effective_reference_count"] == 10
    assert caps["reference_modality_counts"] == {"image": 9, "video": 1, "audio": 0}
    workflow = load_workflow_any(compilation["workflow"]["workflow.py"]["path"])
    api = workflow.compile("api", run_inputs=compilation["workflow_inputs"])
    target = next(node for node in api.values() if node["class_type"] == "MiniMaxH3ReferenceToVideo")
    assert sum(name.startswith("ref_images.") for name in target["inputs"]) == 9
    assert "ref_videos.ref_video_0" in target["inputs"]


@pytest.mark.parametrize("images,videos,audios,limit", [(10, 0, 0, "builder slots"), (0, 4, 0, "builder slots"), (0, 0, 4, "builder slots"), (9, 3, 1, "conditioning files")])
def test_masked_edit_enforces_each_reference_limit(tmp_path, images, videos, audios, limit):
    raw = _multi_grant_request()
    raw["media"][0]["edit"] = [raw["media"][0]["edit"][0]]
    for modality, count in (("image", images), ("video", videos), ("audio", audios)):
        for index in range(count):
            item = {"id": f"{modality}-{index}", "asset": f"{modality}-{index}", "role": "reference", "modality": modality}
            if modality == "video":
                item["audio"] = False
            raw["media"].append(item)
    with pytest.raises(CompilationError, match=limit):
        compile_preparation(_prepare(raw, tmp_path), out_dir=tmp_path / "out")
