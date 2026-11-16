"""Video masks must be indexed on the same zero-based 24 fps clock as H3."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from astrid.packs.h3_av.shared.compose import CompositionError, probe_mask
from astrid.packs.h3_av.shared.prepare import PreparationError, prepare_request
from astrid.packs.h3_av.shared.planner import plan_request
from astrid.packs.h3_av.shared.request import normalize_request


WIDTH, HEIGHT, FRAMES = 1024, 576, 24


def _ffmpeg(*args: str) -> None:
    subprocess.run(
        ["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", *args],
        check=True,
        capture_output=True,
        text=True,
    )


def _source(path: Path) -> Path:
    _ffmpeg(
        "-f", "lavfi", "-i", f"color=c=blue:s={WIDTH}x{HEIGHT}:r=24:d=1",
        "-frames:v", str(FRAMES), "-c:v", "ffv1", "-pix_fmt", "gray", str(path),
    )
    return path


def _mask(path: Path, variant: str) -> Path:
    source = f"color=c=white:s={WIDTH}x{HEIGHT}:r=24:d=1"
    if variant == "30fps":
        source = f"color=c=white:s={WIDTH}x{HEIGHT}:r=30:d=0.8"
    if variant == "short":
        source = f"color=c=white:s={WIDTH}x{HEIGHT}:r=24:d=1"

    args = ["-f", "lavfi", "-i", source]
    if variant == "vfr_same_average":
        # Twenty four frames span one second at 24fps, but one PTS gap is
        # 3/48s instead of 2/48s. The cadence is variable despite avg_frame_rate=24.
        args.extend(["-vf", "settb=1/48,setpts='if(gte(N,22),2*N+1,2*N)'", "-fps_mode", "passthrough"])
    args.extend([
        "-frames:v", str(FRAMES - 1 if variant == "short" else FRAMES),
        "-c:v", "ffv1", "-pix_fmt", "gray",
    ])
    if variant == "nonzero_start":
        args.extend(["-output_ts_offset", "2"])
    args.append(str(path))
    _ffmpeg(*args)
    return path


def _probe(path: Path) -> dict:
    result = subprocess.run(
        ["ffprobe", "-v", "error", "-count_frames", "-show_frames", "-show_streams",
         "-show_format", "-of", "json", str(path)],
        check=True,
        capture_output=True,
        text=True,
    )
    return json.loads(result.stdout)


def _request(mask: Path):
    return normalize_request({
        "version": 2,
        "prompt": "Keep changes inside the declared mask.",
        "duration": 1,
        "media": [{
            "id": "source",
            "asset": "source.mkv",
            "role": "timeline",
            "modality": "video",
            "at": {"frame": 0},
            "range": [0, 1],
            "edit": [{"stream": "video", "during": [0, 1], "mask": {"asset": mask.name}}],
        }],
        "settings": {},
    })


def _prepare(source: Path, mask: Path) -> dict:
    request = _request(mask)
    return prepare_request(request, asset_map={"source.mkv": str(source), mask.name: str(mask)})


@pytest.mark.ffmpeg
def test_lossless_mask_on_zero_based_24fps_clock_is_accepted(tmp_path: Path) -> None:
    source = _source(tmp_path / "source.mkv")
    mask = _mask(tmp_path / "mask.mkv", "valid")

    probe = _probe(mask)
    stream = next(row for row in probe["streams"] if row["codec_type"] == "video")
    assert stream["r_frame_rate"] == stream["avg_frame_rate"] == "24/1"
    assert stream["start_time"] == probe["format"]["start_time"] == "0.000000"
    assert int(stream["nb_read_frames"]) == FRAMES
    assert probe_mask(mask, mask_id=mask.name, source=source, duration=1.0) == {
        "kind": "video", "frames": FRAMES,
    }
    assert _prepare(source, mask)["runtime_submission"] == "eligible"


@pytest.mark.ffmpeg
@pytest.mark.parametrize(
    ("variant", "expected_detail"),
    [
        ("30fps", r"(?i)(30/1|30(?:\.0)?\s*fps).*(24/1|24(?:\.0)?\s*fps)"),
        ("vfr_same_average", r"(?i)(timestamp|frame).*(24/1|24(?:\.0)?\s*fps)"),
        ("nonzero_start", r"(?i)(?=.*24/1 CFR from zero)(?=.*stream_start=2\.000)"),
        ("short", r"23 frames.*expected 24"),
    ],
)
def test_mask_clock_must_match_zero_based_24fps_timeline_before_runtime(
    tmp_path: Path, variant: str, expected_detail: str,
) -> None:
    source = _source(tmp_path / "source.mkv")
    mask = _mask(tmp_path / f"{variant}.mkv", variant)
    probe = _probe(mask)
    stream = next(row for row in probe["streams"] if row["codec_type"] == "video")

    assert str(stream.get("nb_read_frames")) == str(FRAMES - 1 if variant == "short" else FRAMES)
    if variant == "30fps":
        assert stream["r_frame_rate"] == stream["avg_frame_rate"] == "30/1"
    elif variant == "vfr_same_average":
        assert stream["avg_frame_rate"] == "24/1"
        stamps = [float(row["best_effort_timestamp_time"]) for row in probe["frames"]]
        assert max(b - a for a, b in zip(stamps, stamps[1:])) > 1 / 24 + 1e-6
    elif variant == "nonzero_start":
        assert float(stream["start_time"]) == pytest.approx(2.0)
        assert float(probe["format"]["start_time"]) == pytest.approx(2.0)

    # Assert both the public source-backed prepare boundary and the lower-level
    # mask probe fail with evidence that names the observed and required clock.
    with pytest.raises(CompositionError, match=expected_detail):
        probe_mask(mask, mask_id=mask.name, source=source, duration=1.0)
    with pytest.raises(PreparationError, match=expected_detail):
        plan_request(
            _request(mask),
            asset_map={"source.mkv": str(source), mask.name: str(mask)},
            out_dir=tmp_path / "plan",
        )
