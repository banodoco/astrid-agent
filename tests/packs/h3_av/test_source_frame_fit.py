"""CPU regression for the H3 raw-frame rounding tail on source-backed edits."""
from __future__ import annotations

import subprocess
from pathlib import Path

import numpy as np
import pytest
from PIL import Image

from astrid.packs.h3_av.actions.compile.compile import compile_preparation
from astrid.packs.h3_av.actions.prepare.prepare import prepare_request
from astrid.packs.h3_av.shared.compose import compose_candidate
from astrid.packs.h3_av.shared.generation import build_generation_workflow, generation_timing
from astrid.packs.h3_av.shared.request import normalize_request
from tests.packs.h3_av.fixtures.source_fit_start_contract import (
    TinyTensor, audio_concat_execute, empty_audio_execute, fit_start,
    image_from_batch, pixel_frames, repeat_image_batch, temporal_shape,
)


SCENARIOS = [
    # request frames, original source start, moving mask
    (48, 1.0, False),
    (39, 0.0, False),
    (42, 0.0, True),
]


def _decode_video(path: Path, start: float, frames: int) -> np.ndarray:
    raw = subprocess.run([
        "ffmpeg", "-hide_banner", "-loglevel", "error", "-ss", str(start), "-i", str(path),
        "-frames:v", str(frames), "-vf", "scale=8:4:flags=neighbor", "-pix_fmt", "rgb24",
        "-f", "rawvideo", "-",
    ], check=True, capture_output=True, timeout=30).stdout
    pixels = np.frombuffer(raw, dtype=np.uint8)
    assert pixels.size % (4 * 8 * 3) == 0
    result = pixels.reshape(-1, 4, 8, 3).astype(np.float32) / 255
    assert len(result) == frames
    return result


def _decode_audio(path: Path, start: float, frames: int) -> np.ndarray:
    raw = subprocess.run([
        "ffmpeg", "-hide_banner", "-loglevel", "error", "-i", str(path),
        # Match pinned VHS get_audio: output seeking decodes the exact interval.
        "-ss", str(start), "-t", str(frames / 24),
        "-map", "0:a:0", "-ac", "2", "-ar", "44100", "-f", "f32le", "-",
    ], check=True, capture_output=True, timeout=30).stdout
    values = np.frombuffer(raw, dtype=np.float32).reshape(-1, 2)
    samples = frames * 44100 // 24
    values = values[:samples]
    assert len(values) == samples, "synthetic source audio is genuinely short"
    return values.T[None]


def _decode_pcm(path: Path, start: float) -> bytes:
    return subprocess.run([
        "ffmpeg", "-hide_banner", "-loglevel", "error", "-i", str(path),
        "-ss", str(start), "-t", "2", "-map", "0:a:0", "-ac", "2", "-ar", "44100",
        "-c:a", "pcm_s16le", "-f", "s16le", "-",
    ], check=True, capture_output=True, timeout=30).stdout


def _assets(tmp_path: Path, frames: int, *, start: float, moving_mask: bool):
    """Create and decode synthetic AV fixtures at the requested source range."""
    source = tmp_path / "source.mkv"
    total_duration = start + frames / 24
    subprocess.run([
        "ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
        "-f", "lavfi", "-i", f"testsrc2=s=1024x576:r=24:d={total_duration}",
        "-f", "lavfi", "-i", f"sine=frequency=440:sample_rate=44100:duration={total_duration}",
        "-c:v", "ffv1", "-pix_fmt", "yuv420p", "-c:a", "pcm_s16le", str(source),
    ], check=True, capture_output=True, timeout=30)
    look = tmp_path / "look.png"
    Image.new("RGB", (32, 32), (20, 40, 60)).save(look)
    result = {"source.mkv": str(source), "look.png": str(look)}
    if moving_mask:
        mask = tmp_path / "mask.mkv"
        subprocess.run([
            "ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
            "-f", "lavfi", "-i", (
                f"nullsrc=s=1024x576:r=24:d={frames / 24},"
                "geq=lum='if(lt(mod(X+N*128,1024),512),255,0)'"
            ),
            "-frames:v", str(frames), "-an", "-c:v", "ffv1", "-pix_fmt", "gray", str(mask),
        ], check=True, capture_output=True, timeout=30)
        result["mask.mkv"] = str(mask)
    else:
        mask = tmp_path / "mask.png"
        Image.new("L", (1024, 576), 255).save(mask)
        result["mask.png"] = str(mask)
    source_frames = _decode_video(source, start, frames)
    source_audio = _decode_audio(source, start, frames)
    mask_frames = _decode_video(mask, 0, frames) if moving_mask else None
    return result, source_frames, source_audio, mask_frames


def _compile(tmp_path: Path, frames: int, start: float, moving_mask: bool):
    duration = f"{frames}/24"
    start_frame = round(start * 24)
    mask_asset = "mask.mkv" if moving_mask else "mask.png"
    raw = {
        "version": 2,
        "duration": duration,
        "prompt": "Synthetic source-fit boundary regression.",
        "media": [
            {"id": "source", "asset": "source.mkv", "role": "timeline", "modality": "video",
             "at": {"frame": 0}, "range": [f"{start_frame}/24", f"{start_frame + frames}/24"],
             "edit": [{"stream": "video", "during": [0, duration], "mask": {"asset": mask_asset}}]},
            {"id": "look", "asset": "look.png", "role": "reference", "modality": "image"},
        ],
        "settings": {},
    }
    assets, source_frames, source_audio, mask_frames = _assets(
        tmp_path, frames, start=start, moving_mask=moving_mask
    )
    preparation = prepare_request(normalize_request(raw), asset_map=assets)
    manifest = compile_preparation(preparation, out_dir=tmp_path / "compiled")
    from vibecomfy.cli_loader import load_workflow_any

    workflow = load_workflow_any(manifest["workflow"]["workflow.py"]["path"])
    api = workflow.compile("api", run_inputs=manifest["workflow_inputs"])
    return manifest, api, preparation, assets, source_frames, source_audio, mask_frames


def _nodes(api: dict, class_type: str) -> list[dict]:
    return [node for node in api.values() if node.get("class_type") == class_type]


def _linked(api: dict, port) -> dict:
    assert isinstance(port, list) and len(port) == 2
    return api[port[0]]


def _evaluate_padding_graph(api: dict, frame_count: int, source_count: int,
                            source_frames: np.ndarray, source_audio: np.ndarray,
                            mask_frames: np.ndarray | None, *, moving_mask: bool):
    """Execute the compiled padding subgraph on decoded synthetic CPU arrays.

    The arrays stand for loader outputs after their requested frame cap. Each
    source frame and audio sample comes from the selected synthetic AV interval.
    This executes graph topology without H3 sampling or GPU dependencies.
    """
    evaluated: dict[str, object] = {}
    source_id = "99"
    sample_rate = 44100
    evaluated[source_id] = {"IMAGE": TinyTensor(source_frames[:source_count]),
                            "AUDIO": {"waveform": source_audio[..., :source_count * sample_rate // 24],
                                      "sample_rate": sample_rate}}
    if moving_mask:
        mask_loader_id = next(key for key, row in api.items()
                              if row.get("class_type") == "VHS_LoadVideoFFmpeg" and key != source_id)
        evaluated[mask_loader_id] = {"IMAGE": TinyTensor(mask_frames[:source_count])}

    def output(node_id: str, slot: str):
        if node_id in evaluated:
            return evaluated[node_id][slot]
        row = api[node_id]
        kind, inputs = row["class_type"], row["inputs"]
        if kind == "ImageFromBatch":
            images = output(inputs["image"][0], "IMAGE")
            index, length = inputs["batch_index"], inputs["length"]
            value = image_from_batch(images, index, length)
        elif kind == "RepeatImageBatch":
            images = output(inputs["image"][0], "IMAGE")
            value = repeat_image_batch(images, inputs["amount"])
        elif kind == "ImageBatch":
            one = output(inputs["image1"][0], "IMAGE")
            two = output(inputs["image2"][0], "IMAGE")
            one = one.value if isinstance(one, TinyTensor) else one
            two = two.value if isinstance(two, TinyTensor) else two
            if one.shape[1:] != two.shape[1:] and np.all(two == 0):
                # The pinned ImageBatch calls Comfy's bilinear resize for this
                # all-black EmptyImage tail; resizing black preserves zero.
                two = np.zeros((two.shape[0], *one.shape[1:]), dtype=one.dtype)
            value = np.concatenate((one, two), axis=0)
        elif kind == "EmptyImage":
            color = inputs["color"]
            rgb = (((color >> 16) & 255) / 255, ((color >> 8) & 255) / 255, (color & 255) / 255)
            value = np.empty((inputs["batch_size"], inputs["height"], inputs["width"], 3), dtype=np.float32)
            value[:] = rgb
        elif kind == "EmptyAudio":
            value = empty_audio_execute(inputs["duration"], inputs["sample_rate"], inputs["channels"])
        elif kind == "AudioConcat":
            first = output(inputs["audio1"][0], "AUDIO")
            second = output(inputs["audio2"][0], "AUDIO")
            value = audio_concat_execute(first, second, inputs["direction"])
        elif kind == "ImageToMask":
            images = output(inputs["image"][0], "IMAGE")
            images = images.value if isinstance(images, TinyTensor) else images
            value = images[..., 0]
        elif kind == "VHS_LoadVideoFFmpeg":
            raise AssertionError("synthetic loader output was not seeded")
        elif kind == "LoadImageMask":
            value = np.ones((1, 1, 1), dtype=np.float32)
        else:
            raise AssertionError(f"unexpected node in padding subgraph: {kind}")
        evaluated[node_id] = {slot: value}
        return value

    edit = _nodes(api, "H3V2VGranularFractionalDenoise")[0]["inputs"]
    images = output(edit["source_frames"][0], "IMAGE")
    audio = output(edit["source_audio"][0], "AUDIO")["waveform"]
    mask_node = _linked(api, edit["source_mask"])
    mask = output(edit["source_mask"][0], "MASK")
    return images, audio, mask


@pytest.mark.ffmpeg
@pytest.mark.parametrize("frames,start,moving_mask", SCENARIOS)
def test_compiled_native_edit_pads_only_raw_rounding_tail(
    tmp_path, frames, start, moving_mask, record_testsuite_property
):
    timing = generation_timing(frames / 24)
    raw_frames = timing["raw_frames"]
    _, target_latent_t, target_audio_steps = temporal_shape(raw_frames)
    wanted = pixel_frames(target_latent_t)
    assert wanted == raw_frames
    manifest, api, preparation, assets, source_frames, source_audio, mask_frames = _compile(
        tmp_path, frames, start, moving_mask
    )
    edit = _nodes(api, "H3V2VGranularFractionalDenoise")[0]["inputs"]
    images, audio, mask = _evaluate_padding_graph(
        api, frames, frames, source_frames, source_audio, mask_frames, moving_mask=moving_mask
    )
    image_values = images.value if isinstance(images, TinyTensor) else images
    # Run the exact pinned consumer check before any new topology or metadata
    # assertions. The former graph raises here for 48 available vs 56 wanted.
    assert fit_start(image_values.shape[0], wanted, "start") == 0
    label = f"synthetic_{frames}_frames"
    for name, count in {
        "decoded_source_frames": len(source_frames),
        "node_source_frames": image_values.shape[0],
        "target_frames": wanted,
        "node_audio_samples_44100_hz": audio.shape[-1],
        "node_mask_frames": mask.shape[0],
    }.items():
        record_testsuite_property(f"{label}.{name}", count)

    source = _linked(api, edit["source_frames"])
    assert source["class_type"] == (
        "ImageBatch" if raw_frames > frames else "VHS_LoadVideoFFmpeg"
    )
    loader = api["99"]["inputs"]
    assert loader["frame_load_cap"] == frames
    assert loader["start_time"] == start
    assert edit["source_fit"] == "start"
    assert manifest["capabilities"]["source_frame_fit"]["source_window_frames"] == frames
    assert manifest["capabilities"]["source_frame_fit"]["internal_tail_frames"] == raw_frames - frames
    assert manifest["capabilities"]["generation_timing"]["requested_frames"] == frames
    assert manifest["capabilities"]["generation_timing"]["raw_frames"] == raw_frames
    assert image_values.shape[0] == raw_frames
    assert np.array_equal(image_values[:frames], source_frames)
    assert np.array_equal(image_values[frames:], np.repeat(source_frames[-1:], raw_frames - frames, axis=0))
    assert audio.shape[-1] == raw_frames * 44100 // 24
    assert np.all(audio[..., source_audio.shape[-1]:] == 0)
    mask_node = _linked(api, edit["source_mask"])
    if moving_mask:
        assert mask_node["class_type"] == "ImageToMask"
        assert mask.shape[0] == raw_frames
        assert np.array_equal(mask[:frames], mask_frames[..., 0])
        assert not np.array_equal(mask[0], mask[1])
        assert np.all(mask[frames:] == 0)
    else:
        assert mask_node["class_type"] == "LoadImageMask"
        assert mask.shape[0] == 1

    # Pass one-frame-short decoded loader outputs through the real pinned
    # ImageFromBatch clamp and the graph's fixed R-N extension.
    short_images, short_audio, _ = _evaluate_padding_graph(
        api, frames, frames - 1, source_frames, source_audio, mask_frames, moving_mask=moving_mask
    )
    short_values = short_images.value if isinstance(short_images, TinyTensor) else short_images
    with pytest.raises(ValueError, match="source becomes"):
        fit_start(short_values.shape[0], wanted, "start")
    # H3 allows at most half a 40-Hz cell of endpoint overhang plus 1 ms;
    # this actual graph leaves 41.7 ms missing after its fixed tail silence.
    short_audio_seconds = short_audio.shape[-1] / 44100
    assert wanted / 24 - short_audio_seconds > 0.5 / 40 + 0.001
    assert target_audio_steps == round(wanted / 24 * 40)

    if frames == 48 and start == 1.0 and not moving_mask:
        generated = tmp_path / "generated-raw.mkv"
        subprocess.run([
            "ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
            "-f", "lavfi", "-i", f"color=c=red:s=1024x576:r=24:d={raw_frames / 24}",
            "-f", "lavfi", "-i", f"sine=frequency=880:sample_rate=44100:duration={raw_frames / 24}",
            "-frames:v", str(raw_frames), "-t", str(raw_frames / 24),
            "-c:v", "ffv1", "-pix_fmt", "yuv420p", "-c:a", "pcm_s16le", str(generated),
        ], check=True, capture_output=True, timeout=30)
        composition = compose_candidate(
            preparation=preparation, generated=generated, source=Path(assets["source.mkv"]),
            out_dir=tmp_path / "composition",
        )
        coverage = composition["coverage"]["candidate"]
        assert coverage["video"]["frames"] == frames
        assert coverage["video"]["fps"] == 24
        assert coverage["audio"]["duration"] == 2.0
        source_pcm = _decode_pcm(Path(assets["source.mkv"]), 1.0)
        candidate_pcm = _decode_pcm(Path(composition["candidate"]["path"]), 0.0)
        assert len(source_pcm) == 2 * 44100 * 2 * 2
        assert candidate_pcm == source_pcm
        record_testsuite_property(f"{label}.final_frames", coverage["video"]["frames"])
        record_testsuite_property(f"{label}.final_audio_pcm_bytes", len(candidate_pcm))


@pytest.mark.ffmpeg
def test_next_frame_after_native_boundary_keeps_maximum_internal_tail(tmp_path):
    # Exercise the next 39→40 frame boundary directly at the graph contract.
    # The legacy SPEC-MIN adapter reparses normalized seconds and currently
    # rejects non-terminating 40/24 intervals; it is outside source fitting.
    frames = 40
    timing = generation_timing(frames / 24)
    assets, source_frames, source_audio, mask_frames = _assets(
        tmp_path, frames, start=0, moving_mask=False)
    workflow = build_generation_workflow(
        references=[], prompt="Synthetic frame boundary.", frames=timing["raw_frames"],
        model="minimax_h3_ref2va_pruned_int8_convrot.safetensors", steps=8, seed=0,
        masked_edit={"source_binding": "source_video", "mask_binding": "mask_0",
                     "mask_channel": "red", "start_time": 0, "frame_load_cap": frames},
    )
    api = workflow.compile("api", run_inputs={"source_video": assets["source.mkv"],
                                             "mask_0": assets["mask.png"]})
    images, audio, _ = _evaluate_padding_graph(
        api, frames, frames, source_frames, source_audio, mask_frames, moving_mask=False)
    assert timing["trim_tail_frames"] == 16
    assert fit_start(len(images), 56, "start") == 0
    assert np.array_equal(images[:frames], source_frames)
    assert np.array_equal(images[frames:], np.repeat(source_frames[-1:], 16, axis=0))
    assert audio.shape[-1] == 102900
