"""A prepared non-16:9 source keeps its fitted pixels, audio, and mask clock."""

from __future__ import annotations

import subprocess
from pathlib import Path

import numpy as np
import pytest

from astrid.core.generation.engine_facts import comfyui_facts
from astrid.packs.h3_av.shared import engine_facts
from astrid.packs.h3_av.shared.compile import CompilationError, compile_preparation
from astrid.packs.h3_av.shared.compose import compose_candidate
from astrid.packs.h3_av.shared.prepare import prepare_request
from astrid.packs.h3_av.shared.request import normalize_request
from astrid.packs.h3_av.shared.verify import verify_candidate
from tests.packs.h3_av.test_source_frame_fit import (
    TinyTensor,
    _decode_audio,
    _decode_video,
    _evaluate_padding_graph,
)


INPUT_WIDTH, INPUT_HEIGHT = 640, 480
OUTPUT_WIDTH, OUTPUT_HEIGHT, FPS, FRAMES = 1024, 576, 24, 24
MASK_EDGE = 256


def _ffmpeg(*args: str, input_bytes: bytes | None = None) -> None:
    subprocess.run(
        ["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", *args],
        input=input_bytes,
        check=True,
        capture_output=True,
        timeout=60,
    )


def _source(path: Path) -> Path:
    _ffmpeg(
        "-f", "lavfi", "-i", f"testsrc2=size={INPUT_WIDTH}x{INPUT_HEIGHT}:rate=24:duration=1.5",
        "-f", "lavfi", "-i", "sine=frequency=440:sample_rate=48000:duration=1.5",
        "-t", "1.5", "-c:v", "libx264", "-crf", "12", "-pix_fmt", "yuv420p",
        "-c:a", "aac", str(path),
    )
    return path


def _mask(path: Path, *, size: tuple[int, int]) -> Path:
    width, height = size
    raw_frames = []
    for index in range(FRAMES):
        frame = bytearray(width * height)
        edge = min(MASK_EDGE, width, height)
        x0 = min(index * 12, width - edge)
        y0 = max(0, (height - edge) // 2)
        for y in range(y0, y0 + edge):
            frame[y * width + x0:y * width + x0 + edge] = b"\xff" * edge
        raw_frames.append(bytes(frame))
    _ffmpeg(
        "-f", "rawvideo", "-pix_fmt", "gray", "-s", f"{width}x{height}", "-r", str(FPS),
        "-i", "-", "-frames:v", str(FRAMES), "-an", "-c:v", "ffv1", "-pix_fmt", "gray",
        str(path), input_bytes=b"".join(raw_frames),
    )
    return path


def _request(fit: str):
    return normalize_request({
        "version": 1,
        "operation": "edit",
        "source": {"asset": "source.mp4", "range": [0, 1], "fit": fit},
        "output": {"duration": 1},
        "content": {"prompt": "Change only the moving masked region."},
        "changes": {
            "video": [{
                "during": [0, 1],
                "area": {"mask_asset": "mask.mkv"},
                "action": "generate",
            }],
            "audio": [],
        },
        "references": [],
        "overrides": {"steps": 8, "seed": 7},
    })


def _prepare_compile(tmp_path: Path, fit: str, mask_size: tuple[int, int], monkeypatch):
    monkeypatch.setattr(engine_facts, "_FACTS", comfyui_facts())
    source = _source(tmp_path / "source.mp4")
    mask = _mask(tmp_path / "mask.mkv", size=mask_size)
    preparation = prepare_request(
        _request(fit),
        asset_map={"source.mp4": str(source), "mask.mkv": str(mask)},
        out_dir=tmp_path / "prepared",
    )
    compiled = compile_preparation(preparation, out_dir=tmp_path / "compiled")
    return source, mask, preparation, compiled


def _generated(path: Path, raw_frames: int) -> Path:
    duration = raw_frames / FPS
    _ffmpeg(
        "-f", "lavfi", "-i", f"color=c=red:s={OUTPUT_WIDTH}x{OUTPUT_HEIGHT}:r={FPS}:d={duration}",
        "-f", "lavfi", "-i", f"sine=frequency=880:sample_rate=44100:duration={duration}",
        "-frames:v", str(raw_frames), "-t", str(duration), "-c:v", "ffv1", "-pix_fmt", "yuv420p",
        "-c:a", "pcm_s16le", str(path),
    )
    return path


@pytest.mark.ffmpeg
@pytest.mark.parametrize("fit", ["fill", "fit"])
def test_non16x9_source_masked_edit_preserves_prepared_baseline_on_cpu(
    tmp_path: Path, monkeypatch, fit: str,
) -> None:
    _original, mask_path, preparation, compiled = _prepare_compile(
        tmp_path, fit, (OUTPUT_WIDTH, OUTPUT_HEIGHT), monkeypatch,
    )
    baseline = Path(next(row["path"] for row in preparation["assets"] if row["asset"] == "source.mp4"))
    assert preparation["source_conform"]["output"]["fit"] == fit
    assert preparation["source_conform"]["output"]["width"] == OUTPUT_WIDTH
    assert preparation["source_conform"]["output"]["height"] == OUTPUT_HEIGHT

    from vibecomfy.cli_loader import load_workflow_any

    workflow = load_workflow_any(compiled["workflow"]["workflow.py"]["path"])
    api = workflow.compile("api", run_inputs=compiled["workflow_inputs"])
    baseline_frames = _decode_video(baseline, 0, FRAMES)
    baseline_audio = _decode_audio(baseline, 0, FRAMES)
    mask_frames = _decode_video(mask_path, 0, FRAMES)
    graph_images, graph_audio, graph_mask = _evaluate_padding_graph(
        api,
        FRAMES,
        FRAMES,
        baseline_frames,
        baseline_audio,
        mask_frames,
        moving_mask=True,
    )
    image_values = graph_images.value if isinstance(graph_images, TinyTensor) else graph_images
    mask_values = graph_mask.value if isinstance(graph_mask, TinyTensor) else graph_mask
    assert np.array_equal(image_values[:FRAMES], baseline_frames)
    assert np.array_equal(graph_audio[..., :baseline_audio.shape[-1]], baseline_audio)
    assert np.array_equal(mask_values[:FRAMES], mask_frames[..., 0])
    assert np.any(mask_values[:FRAMES] > 0) and np.any(mask_values[:FRAMES] == 0)

    raw_frames = compiled["capabilities"]["generation_timing"]["raw_frames"]
    generated = _generated(tmp_path / "generated.mkv", raw_frames)
    composition = compose_candidate(
        preparation=preparation,
        generated=generated,
        source=baseline,
        out_dir=tmp_path / "composition",
    )
    verification = verify_candidate(preparation=preparation, composition=composition, source=baseline)

    assert verification["status"] == "verified"
    assert verification["preservation"]["audio"] == [[0.0, 1.0]]
    spatial = verification["preservation"]["spatial_complement"][0]
    assert spatial["mask_kind"] == "video"
    assert spatial["protected_pixel_count_per_frame"] == [
        OUTPUT_WIDTH * OUTPUT_HEIGHT - MASK_EDGE * MASK_EDGE
    ] * FRAMES
    assert spatial["max_abs_channel_difference"] == 0
    audio = composition["preservation_evidence"]["protected_samples"]["audio"][0]
    assert audio["method"] == "ffmpeg-decoded-audio-similarity-v1"
    assert audio["similarity"] >= 0.985


@pytest.mark.ffmpeg
@pytest.mark.parametrize("fit", ["fill", "fit"])
def test_non16x9_mask_edit_rejects_mask_in_original_source_dimensions(
    tmp_path: Path, monkeypatch, fit: str,
) -> None:
    with pytest.raises(CompilationError, match=r"640x480.*1024x576"):
        _prepare_compile(tmp_path, fit, (INPUT_WIDTH, INPUT_HEIGHT), monkeypatch)
