from __future__ import annotations

import hashlib
import json
import subprocess
from pathlib import Path
from typing import Any

import pytest

from astrid.core.generation.engine_facts import comfyui_facts
from astrid.core.contracts.errors import coerce_astrid_error
from astrid.packs.h3_av.shared import engine_facts, prepare as prepare_module
from astrid.packs.h3_av.shared.prepare import prepare_request
from astrid.packs.h3_av.shared.errors import PreparationError
from astrid.packs.h3_av.shared.request import normalize_request

def _install_engine_facts() -> None:
    engine_facts.install(comfyui_facts())


def _v1(operation: str) -> Any:
    raw: dict[str, Any] = {
        "version": 1,
        "operation": operation,
        "output": {"duration": 1},
        "content": {"prompt": "Keep the source second intact."},
        "changes": {"video": [], "audio": []},
        "references": [],
        "overrides": {"seed": 42, "steps": 8},
    }
    if operation == "generate":
        raw["references"] = [{"asset": "image-0", "purpose": "appearance"}]
    else:
        raw["source"] = {"asset": "source", "range": [0, 1]}
    return normalize_request(raw)


def _v2(*, continuation: bool) -> Any:
    raw: dict[str, Any] = {
        "version": 2,
        "prompt": "Keep the source second intact.",
        "duration": 1,
        "media": [
            {
                "id": "source",
                "asset": "source.mp4",
                "role": "timeline",
                "modality": "video",
                "at": {"frame": 0},
                "range": [0, 1],
            }
        ],
        "settings": {},
    }
    if continuation:
        raw["continuation"] = True
    return normalize_request(raw)


def _v2_source_free() -> Any:
    return normalize_request(
        {
            "version": 2,
            "prompt": "A quiet room.",
            "duration": 1,
            "media": [{"id": "look", "asset": "look.png", "role": "reference", "modality": "image"}],
            "settings": {},
        }
    )


def _ffmpeg(*args: str) -> None:
    subprocess.run(["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", *args], check=True, capture_output=True, text=True)


def _make_23976_source(path: Path) -> None:
    """A real 1 s clip at 24000/1001 fps (23.976) with a 1 s AAC audio stream."""
    _ffmpeg(
        "-f", "lavfi", "-i", "color=size=1024x576:rate=24000/1001:duration=1",
        "-f", "lavfi", "-i", "sine=frequency=440:sample_rate=48000:duration=1",
        "-c:v", "libx264", "-pix_fmt", "yuv420p", "-c:a", "aac", str(path),
    )


def _conform(source: Path, out: Path) -> None:
    """Run the exact conform command the rejection message prints, with IN and OUT filled in."""
    _ffmpeg(
        "-i", str(source), "-vf", "fps=24,scale=1024:576,setsar=1", "-c:v", "libx264", "-crf", "12", "-c:a", "aac", str(out),
    )


def _probe_streams(path: Path) -> list[dict[str, Any]]:
    result = subprocess.run(
        ["ffprobe", "-v", "error", "-show_entries",
         "stream=codec_type,width,height,r_frame_rate,avg_frame_rate,nb_frames,duration",
         "-of", "json", str(path)],
        check=True, capture_output=True, text=True,
    )
    return json.loads(result.stdout)["streams"]


def _assert_prepared_conform(
    preparation: dict[str, Any], source: Path, out_dir: Path, *, audio: bool,
    frames: int = 24, duration: float = 1.0,
) -> None:
    assert preparation["status"] == "prepared"
    assert preparation["runtime_submission"] == "eligible"
    record = preparation["source_conform"]
    derived = out_dir / "conformed-source" / record["output"]["file"]
    assert derived.is_file()
    assert record["source"]["sha256"] == hashlib.sha256(source.read_bytes()).hexdigest()
    assert record["output"]["sha256"] == hashlib.sha256(derived.read_bytes()).hexdigest()
    assert record["output"]["range_seconds"] == [0, duration]
    assert record["output"]["frames"] == frames
    assert record["output"]["fps"] == 24
    assert record["output"]["width"] == 1024
    assert record["output"]["height"] == 576
    streams = _probe_streams(derived)
    video = next(row for row in streams if row["codec_type"] == "video")
    assert (video["width"], video["height"]) == (1024, 576)
    assert video["r_frame_rate"] == video["avg_frame_rate"] == "24/1"
    assert int(video["nb_frames"]) == frames
    output_audio = [row for row in streams if row["codec_type"] == "audio"]
    assert bool(output_audio) is audio
    if audio:
        assert record["decoded_av"]["video_frames"] == frames
        assert record["decoded_av"]["source_frame_rate"] == "24/1"
        assert record["decoded_av"]["audio_samples"] > 0
        assert record["decoded_av"]["fractional_mismatch"] <= 0.005
    else:
        assert record["audio"] is None
        assert record["decoded_av"] == {"has_audio": False}
    asset = next(row for row in preparation["assets"] if row["asset"] in {"source", "source.mp4"})
    assert Path(asset["path"]) == derived.resolve()
    assert asset["sha256"] == record["output"]["sha256"]


ROUTES = [
    pytest.param(lambda: _v1("continue"), "source", id="v1-continue"),
    pytest.param(lambda: _v1("edit"), "source", id="v1-edit"),
    pytest.param(lambda: _v2(continuation=False), "source.mp4", id="v2-source-backed"),
    pytest.param(lambda: _v2(continuation=True), "source.mp4", id="v2-extension-context"),
]


@pytest.mark.ffmpeg
@pytest.mark.parametrize(("make_request", "asset_id"), ROUTES)
def test_23976_source_is_conformed_on_every_source_backed_route(
    tmp_path: Path, make_request: Any, asset_id: str
) -> None:
    _install_engine_facts()
    source = tmp_path / "source-23976.mp4"
    _make_23976_source(source)
    out_dir = tmp_path / "prepared"
    preparation = prepare_request(
        make_request(), asset_map={asset_id: str(source)}, out_dir=out_dir,
    )
    _assert_prepared_conform(preparation, source, out_dir, audio=True)


@pytest.mark.ffmpeg
@pytest.mark.parametrize(("make_request", "asset_id"), ROUTES)
def test_conformed_24fps_source_passes_on_every_source_backed_route(
    tmp_path: Path, make_request: Any, asset_id: str
) -> None:
    source = tmp_path / "source-23976.mp4"
    conformed = tmp_path / "source-24.mp4"
    _make_23976_source(source)
    _conform(source, conformed)
    preparation = prepare_request(make_request(), asset_map={asset_id: str(conformed)})
    assert preparation["status"] == "prepared"
    assert preparation["runtime_submission"] == "eligible"


@pytest.mark.ffmpeg
@pytest.mark.parametrize("dimensions", ["576x1024", "640x480"])
def test_wrong_size_silent_source_is_conformed_without_adding_audio(
    tmp_path: Path, dimensions: str,
) -> None:
    _install_engine_facts()
    source = tmp_path / "wrong-size.mp4"
    _ffmpeg("-f", "lavfi", "-i", f"color=size={dimensions}:rate=24:duration=1",
            "-c:v", "libx264", "-pix_fmt", "yuv420p", str(source))
    source_video = next(row for row in _probe_streams(source) if row["codec_type"] == "video")
    assert f'{source_video["width"]}x{source_video["height"]}' == dimensions
    out_dir = tmp_path / "prepared"
    preparation = prepare_request(
        _v1("edit"), asset_map={"source": str(source)}, out_dir=out_dir,
    )
    _assert_prepared_conform(preparation, source, out_dir, audio=False)


@pytest.mark.ffmpeg
def test_vfr_source_is_conformed_to_24fps(tmp_path: Path) -> None:
    _install_engine_facts()
    source = tmp_path / "vfr.mp4"
    _ffmpeg("-f", "lavfi", "-i", "color=size=1024x576:rate=24:duration=1",
            "-vf", "select='not(mod(n,2))+not(mod(n,3))'", "-fps_mode", "vfr",
            "-c:v", "libx264", "-pix_fmt", "yuv420p", str(source))
    probe = subprocess.run(
        ["ffprobe", "-v", "error", "-select_streams", "v:0", "-show_entries",
         "stream=r_frame_rate,avg_frame_rate", "-of", "json", str(source)],
        check=True, capture_output=True, text=True,
    )
    stream = json.loads(probe.stdout)["streams"][0]
    assert stream["r_frame_rate"] != stream["avg_frame_rate"]
    request = normalize_request({
        "version": 2,
        "prompt": "Keep the source second intact.",
        "duration": 1,
        "media": [{
            "id": "source", "asset": "source.mp4", "role": "timeline",
            "modality": "video", "at": {"frame": 0}, "range": [0, 0.75],
        }],
        "settings": {},
    })
    out_dir = tmp_path / "prepared"
    preparation = prepare_request(
        request, asset_map={"source.mp4": str(source)}, out_dir=out_dir,
    )
    _assert_prepared_conform(preparation, source, out_dir, audio=False, frames=18, duration=0.75)


@pytest.mark.ffmpeg
def test_short_audio_range_fails_instead_of_synthesizing_missing_samples(tmp_path: Path) -> None:
    _install_engine_facts()
    source = tmp_path / "short-audio.mp4"
    _ffmpeg("-f", "lavfi", "-i", "color=size=1024x576:rate=24:duration=1",
            "-f", "lavfi", "-i", "sine=frequency=440:sample_rate=48000:duration=0.7",
            "-c:v", "libx264", "-pix_fmt", "yuv420p", "-c:a", "aac", str(source))
    streams = _probe_streams(source)
    durations = {row["codec_type"]: float(row["duration"]) for row in streams}
    assert durations["video"] >= 1.0
    assert durations["audio"] < 1.0
    out_dir = tmp_path / "prepared"
    with pytest.raises(PreparationError, match="source streams must cover the requested range"):
        prepare_request(_v1("continue"), asset_map={"source": str(source)}, out_dir=out_dir)
    assert not (out_dir / "conformed-source").exists()


def test_missing_ffprobe_has_coded_error(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    source = tmp_path / "source.mp4"
    source.write_bytes(b"source")
    monkeypatch.setattr(prepare_module.shutil, "which", lambda _: None)
    with pytest.raises(PreparationError) as raised:
        prepare_request(_v1("edit"), asset_map={"source": str(source)})
    envelope = coerce_astrid_error(raised.value).to_envelope()
    assert {key: envelope[key] for key in ("code", "message", "field", "fix")} == {
        "code": "H3_SOURCE_AV_FFPROBE",
        "message": "H3_SOURCE_AV_RATE: source 'source' rate check requires ffprobe",
        "field": "source.asset",
        "fix": "Install ffprobe and ensure it is available on PATH",
    }


def test_non_media_fixture_retains_existing_behavior(tmp_path: Path) -> None:
    source = tmp_path / "fixture.mp4"
    source.write_bytes(b"opaque non-media test fixture")
    assert prepare_request(_v1("edit"), asset_map={"source": str(source)})["runtime_submission"] == "eligible"


def test_source_free_generate_never_probes_a_source(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    probed: list[list[str]] = []
    real_run = subprocess.run

    def spy(argv: Any, *args: Any, **kwargs: Any) -> Any:
        probed.append([str(part) for part in argv])
        return real_run(argv, *args, **kwargs)

    reference = tmp_path / "reference.png"
    reference.write_bytes(b"reference-image")
    monkeypatch.setattr(prepare_module.subprocess, "run", spy)
    v1 = prepare_request(_v1("generate"), asset_map={"image-0": str(reference)})
    v2 = prepare_request(_v2_source_free(), asset_map={"look.png": str(reference)})
    assert v1["status"] == "prepared"
    assert v2["status"] == "prepared"
    assert not [argv for argv in probed if "ffprobe" in argv[0]]
