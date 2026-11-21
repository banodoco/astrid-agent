"""``astrid media trim``: frame-exact cut, audio by sample, provenance, dry run."""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path
from typing import Any

import numpy as np
import pytest

from astrid.core.cli.domain_product import run_product_family
from astrid.core.media import ffprobe_metadata_strict
from astrid.core.media_trim import parse_range, trim_media, trim_plan
from astrid.sdk.contracts import DomainResult, ErrorObject
from tests.v10.test_media_crop import _FakeClient

FFMPEG = shutil.which("ffmpeg") and shutil.which("ffprobe")
needs_ffmpeg = pytest.mark.skipif(not FFMPEG, reason="ffmpeg/ffprobe not installed")

W, H, FPS, SECONDS, SR = 160, 120, 24, 8, 48000


def _source(path: Path) -> Path:
    """Lossless source: every frame differs (testsrc2 counter) and the audio is a sine at 48 kHz mono."""
    subprocess.run([
        "ffmpeg", "-nostdin", "-v", "error", "-y",
        "-f", "lavfi", "-i", f"testsrc2=size={W}x{H}:rate={FPS}:duration={SECONDS}",
        "-f", "lavfi", "-i", f"sine=frequency=440:sample_rate={SR}:duration={SECONDS}",
        "-c:v", "ffv1", "-c:a", "flac", "-shortest", str(path),
    ], check=True)
    return path


def _decode_frames(path: Path) -> np.ndarray:
    raw = subprocess.run(["ffmpeg", "-nostdin", "-v", "error", "-i", str(path), "-f", "rawvideo",
                          "-pix_fmt", "rgb24", "-"], capture_output=True, check=True).stdout
    return np.frombuffer(raw, dtype=np.uint8).reshape(-1, H, W, 3).astype(np.int16)


def _decode_audio_samples(path: Path) -> int:
    raw = subprocess.run(["ffmpeg", "-nostdin", "-v", "error", "-i", str(path), "-map", "0:a:0", "-ac", "1",
                          "-ar", str(SR), "-f", "s16le", "-"], capture_output=True, check=True).stdout
    return len(raw) // 2


def _row(path: Path, object_id: str = "O-src") -> dict[str, Any]:
    import hashlib
    return {"object_id": object_id, "digest": hashlib.sha256(path.read_bytes()).hexdigest(),
            "filename": path.name, "media_type": "video/x-matroska"}


def test_parse_range_requires_forward_non_negative_seconds() -> None:
    assert parse_range("3.25,6.5") == (3.25, 6.5)
    for bad in ("6,3", "-1,2", "1", "a,b", "2,2"):
        with pytest.raises(ValueError):
            parse_range(bad)


def test_trim_plan_is_exact_in_frames_and_samples() -> None:
    from astrid.core.media import MediaProbe

    probe = MediaProbe(duration_seconds=8.0, fps_rational=(24, 1), audio_codec="flac", audio_sample_rate=48000)
    plan = trim_plan(probe, 3.25, 6.5)
    assert plan["bounds"] == {"start_frame": 78, "end_frame": 156, "frames": 78, "fps": [24, 1],
                              "start_sample": 156000, "end_sample": 312000, "samples": 156000,
                              "sample_rate": 48000}
    with pytest.raises(ValueError, match="frame boundary"):
        trim_plan(probe, 3.2, 6.5)
    with pytest.raises(ValueError, match="past the video duration"):
        trim_plan(probe, 3.25, 9.0)


@needs_ffmpeg
def test_trim_is_frame_exact_and_audio_matches_samples(tmp_path: Path) -> None:
    src = _source(tmp_path / "src.mkv")
    source_frames = _decode_frames(src)
    client = _FakeClient({"M-src": (_row(src), src.read_bytes())})

    result = trim_media(client, project="P", ref="M-src", start=3.25, end=6.5)

    assert result.ok, result.error
    bounds = result.data["bounds"]
    assert (bounds["start_frame"], bounds["end_frame"], bounds["frames"]) == (78, 156, 78)
    (payload,) = client.media.imported.values()
    out = tmp_path / "out.mp4"
    out.write_bytes(payload)
    clip = _decode_frames(out)
    # The first clip frame is the first generated frame (source frame 78), not a neighbour.
    distances = [np.abs(clip[0] - source_frames[index]).mean() for index in (77, 78, 79)]
    assert int(np.argmin(distances)) == 1, distances
    assert abs(clip.shape[0] - 78) <= 1
    # Audio is cut by sample: 3.25 s..6.5 s at 48 kHz is 156000 samples. AAC encoder padding can add
    # up to one AAC frame (1024 samples) when decoded, so the length is within that bound.
    assert abs(_decode_audio_samples(out) - bounds["samples"]) <= 1024
    # The source is untouched and never re-encoded.
    assert client.media.rows["M-src"][1] == src.read_bytes()


@needs_ffmpeg
def test_trim_records_provenance_and_dry_run_imports_nothing(tmp_path: Path) -> None:
    src = _source(tmp_path / "src.mkv")
    client = _FakeClient({"M-src": (_row(src), src.read_bytes())})

    dry = trim_media(client, project="P", ref="M-src", start=3.25, end=6.5, dry_run=True)
    assert dry.ok and dry.data["dry_run"] is True and dry.data["bounds"]["frames"] == 78
    assert "import_file" not in [verb for verb, _ in client.media.calls]

    result = trim_media(client, project="P", ref="M-src", start=3.25, end=6.5)
    assert result.ok, result.error
    relate = [args for verb, args in client.media.calls if verb == "relate"][0]
    assert relate["kind"] == "derived_from"
    assert relate["to_object_id"] == "O-src"
    assert relate["metadata"]["operation"] == "trim"
    assert relate["metadata"]["range"] == {"start": 3.25, "end": 6.5}
    assert relate["metadata"]["source_digest"] == _row(src)["digest"]
    assert result.data["provenance_recorded"] == "new"


@needs_ffmpeg
def test_trim_refuses_range_past_the_end_and_non_video(tmp_path: Path) -> None:
    src = _source(tmp_path / "src.mkv")
    client = _FakeClient({"M-src": (_row(src), src.read_bytes())})
    result = trim_media(client, project="P", ref="M-src", start=1, end=60)
    assert not result.ok and result.error.code == "validation_error"
    assert "import_file" not in [verb for verb, _ in client.media.calls]

    still = {"object_id": "O-img", "digest": "d", "filename": "a.png", "media_type": "image/png"}
    client = _FakeClient({"M-img": (still, b"x")})
    result = trim_media(client, project="P", ref="M-img", start=0, end=1)
    assert not result.ok and result.error.code == "unsupported_operation"


@needs_ffmpeg
def test_cli_trim_json_envelope_and_range_validation(tmp_path: Path, capsys) -> None:
    src = _source(tmp_path / "src.mkv")
    client = _FakeClient({"M-src": (_row(src), src.read_bytes())})
    rc = run_product_family("media", ["trim", "--project", "P", "M-src", "--range", "3.25,6.5",
                                      "--dry-run", "--json"], client=client)
    assert rc == 0
    envelope = json.loads(capsys.readouterr().out)
    assert envelope["ok"] is True and envelope["data"]["bounds"]["frames"] == 78

    with pytest.raises(SystemExit) as excinfo:
        run_product_family("media", ["trim", "--project", "P", "M-src"], client=client)
    assert excinfo.value.code == 2
