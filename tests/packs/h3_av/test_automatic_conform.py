"""Request preparation creates a bounded, verified H3 source derivative."""
from __future__ import annotations

import hashlib
import json
import subprocess
from pathlib import Path

import pytest

from astrid.core.generation.engine_facts import comfyui_facts
from astrid.packs.h3_av.shared import engine_facts
from astrid.packs.h3_av.shared.errors import PreparationError
from astrid.packs.h3_av.shared.prepare import prepare_request
from astrid.packs.h3_av.shared.request import normalize_request


def _install_facts() -> None:
    engine_facts.install(comfyui_facts())


def _ffmpeg(*args: str) -> None:
    subprocess.run(
        ["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", *args],
        check=True,
        capture_output=True,
        text=True,
        timeout=60,
    )


def _source(
    path: Path, *, duration: float = 3, audio: bool = True, audio_duration: float | None = None,
) -> None:
    args = [
        "-f", "lavfi", "-i", f"testsrc2=size=640x480:rate=24000/1001:duration={duration}",
    ]
    if audio:
        sound_duration = duration if audio_duration is None else audio_duration
        args += ["-f", "lavfi", "-i", f"sine=frequency=440:sample_rate=48000:duration={sound_duration}"]
    args += ["-map", "0:v:0"]
    if audio:
        args += ["-map", "1:a:0"]
    args += ["-c:v", "libx264", "-pix_fmt", "yuv420p"]
    if audio:
        args += ["-c:a", "aac", "-ar", "48000"]
    args += [str(path)]
    _ffmpeg(*args)


def _request(*, fit: str | None = None):
    source = {"asset": "source", "range": [0.5, 2.0]}
    if fit is not None:
        source["fit"] = fit
    return normalize_request({
        "version": 1,
        "operation": "edit",
        "source": source,
        "output": {"duration": 1.5},
        "content": {"prompt": "Preserve the selected source interval."},
        "changes": {"video": [], "audio": []},
        "references": [],
        "overrides": {"seed": 7, "steps": 8},
    })


def _probe(path: Path) -> dict:
    result = subprocess.run(
        ["ffprobe", "-v", "error", "-show_entries",
         "stream=codec_type,width,height,r_frame_rate,avg_frame_rate,nb_frames,duration",
         "-of", "json", str(path)],
        check=True,
        capture_output=True,
        text=True,
        timeout=30,
    )
    return json.loads(result.stdout)


@pytest.mark.ffmpeg
@pytest.mark.parametrize(("fit", "expected_fit"), [(None, "fill"), ("fit", "fit")])
def test_prepare_request_conforms_nonzero_source_range_and_records_provenance(
    tmp_path: Path, fit: str | None, expected_fit: str,
) -> None:
    _install_facts()
    source = tmp_path / "source-23976.mp4"
    _source(source)
    request = _request(fit=fit)
    out_dir = tmp_path / "prepared"

    preparation = prepare_request(
        request,
        asset_map={"source": str(source)},
        out_dir=out_dir,
    )

    record = preparation["source_conform"]
    derived = out_dir / "conformed-source" / record["output"]["file"]
    assert derived.is_file()
    assert preparation["original_request_digest"] == request.digest
    assert record["source"]["sha256"] == hashlib.sha256(source.read_bytes()).hexdigest()
    assert record["source"]["range_seconds"] == ["0.5", "2.0"]
    assert record["output"] == {
        "file": "source.mp4",
        "sha256": hashlib.sha256(derived.read_bytes()).hexdigest(),
        "range_seconds": [0, 1.5],
        "frames": 36,
        "fps": 24,
        "width": 1024,
        "height": 576,
        "fit": expected_fit,
    }
    saved_manifest = json.loads((derived.parent / "manifest.json").read_text(encoding="utf-8"))
    assert saved_manifest == record

    prepared_asset = next(row for row in preparation["assets"] if row["asset"] == "source")
    assert prepared_asset["path"] == str(derived.resolve())
    assert prepared_asset["sha256"] == record["output"]["sha256"]
    assert preparation["request"]["source"]["range"] == [0.0, 1.5]
    assert preparation["request_digest"] != request.digest

    video = next(row for row in _probe(derived)["streams"] if row["codec_type"] == "video")
    assert (video["width"], video["height"]) == (1024, 576)
    assert video["r_frame_rate"] == video["avg_frame_rate"] == "24/1"
    assert int(video["nb_frames"]) == 36
    measured = record["decoded_av"]
    assert measured["video_frames"] == 36
    assert measured["source_frame_rate"] == measured["source_avg_frame_rate"] == "24/1"
    assert measured["audio_samples"] > 0
    assert measured["audio_sample_rate"] == 32000
    assert measured["expected_audio_samples"] == round(36 / 24 * 32000)
    assert measured["fractional_mismatch"] <= 0.005


@pytest.mark.ffmpeg
def test_prepare_request_keeps_video_only_source_without_adding_audio(tmp_path: Path) -> None:
    _install_facts()
    source = tmp_path / "video-only.mp4"
    _source(source, audio=False)

    preparation = prepare_request(
        _request(), asset_map={"source": str(source)}, out_dir=tmp_path / "prepared",
    )

    record = preparation["source_conform"]
    derived = tmp_path / "prepared" / "conformed-source" / record["output"]["file"]
    assert record["audio"] is None
    assert record["decoded_av"] == {"has_audio": False}
    assert [row["codec_type"] for row in _probe(derived)["streams"]] == ["video"]


@pytest.mark.ffmpeg
def test_prepare_request_rejects_source_range_not_covered_by_both_streams(tmp_path: Path) -> None:
    _install_facts()
    source = tmp_path / "short-audio.mp4"
    _source(source, duration=3, audio_duration=1)
    streams = _probe(source)["streams"]
    assert {row["codec_type"] for row in streams} == {"video", "audio"}
    durations = {row["codec_type"]: float(row["duration"]) for row in streams}
    assert durations["video"] >= 2.0
    assert durations["audio"] < 2.0

    with pytest.raises(PreparationError, match="source streams must cover the requested range"):
        prepare_request(_request(), asset_map={"source": str(source)}, out_dir=tmp_path / "prepared")
