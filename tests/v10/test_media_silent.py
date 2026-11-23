"""``astrid media silent-video``: a still becomes an exact-frame video with a silent audio track."""

from __future__ import annotations

import io
import json
import shutil
import subprocess
from pathlib import Path
from typing import Any

import pytest
from PIL import Image

from astrid.core.cli.domain_product import run_product_family
from astrid.core.media import ffprobe_metadata_strict
from astrid.core.media_silent import frame_count, parse_size, silent_plan, silent_video
from tests.v10.test_media_crop import _FakeClient, _image_row

FFMPEG = shutil.which("ffmpeg") and shutil.which("ffprobe")
needs_ffmpeg = pytest.mark.skipif(not FFMPEG, reason="ffmpeg/ffprobe not installed")


def _png(width: int = 64, height: int = 48) -> bytes:
    image = Image.new("RGB", (width, height), (200, 40, 40))
    image.paste((20, 20, 220), (0, 0, width // 2, height))
    out = io.BytesIO()
    image.save(out, format="PNG")
    return out.getvalue()


def _samples(path: Path) -> int:
    raw = subprocess.run(["ffmpeg", "-nostdin", "-v", "error", "-i", str(path), "-map", "0:a:0", "-ac", "1",
                          "-ar", "48000", "-f", "s16le", "-"], capture_output=True, check=True).stdout
    return len(raw) // 2


def test_parse_size_and_frame_count() -> None:
    assert parse_size("1024x576") == (1024, 576)
    for bad in ("1024", "1023x576", "0x4", "ax4"):
        with pytest.raises(ValueError):
            parse_size(bad)
    assert frame_count(2.5, 24) == 60
    for bad in (0, -1):
        with pytest.raises(ValueError):
            frame_count(bad, 24)
    with pytest.raises(ValueError, match="frame boundary"):
        frame_count(2.51, 24)


def test_plan_counts_frames_and_samples_and_evens_the_size() -> None:
    plan = silent_plan(65, 49, duration=2.5, fps=24, sample_rate=48000, size=None)
    assert (plan["frames"], plan["samples"], plan["width"], plan["height"], plan["fit"]) == (
        60, 120000, 64, 48, "source_size")
    plan = silent_plan(65, 49, duration=2.5, fps=24, sample_rate=48000, size=(1024, 576))
    assert (plan["width"], plan["height"], plan["fit"]) == (1024, 576, "cover")


@needs_ffmpeg
def test_silent_video_is_exact_frames_with_digital_silence(tmp_path: Path) -> None:
    client = _FakeClient({"M-still": _image_row("O-still", _png())})

    result = silent_video(client, project="P", ref="M-still", duration=2.5, size=(128, 72))

    assert result.ok, result.error
    assert result.data["plan"]["frames"] == 60
    (payload,) = client.media.imported.values()
    out = tmp_path / "out.mp4"
    out.write_bytes(payload)
    probe = ffprobe_metadata_strict(out)
    assert (probe.width, probe.height) == (128, 72)
    assert probe.audio_codec == "aac" and probe.audio_sample_rate == 48000
    count = subprocess.run(["ffprobe", "-v", "error", "-count_frames", "-select_streams", "v:0",
                            "-show_entries", "stream=nb_read_frames", "-of", "csv=p=0", str(out)],
                           capture_output=True, text=True, check=True).stdout.strip()
    assert int(count) == 60
    # AAC priming can add up to one frame of samples when decoded.
    assert abs(_samples(out) - 120000) <= 1024
    raw = subprocess.run(["ffmpeg", "-nostdin", "-v", "error", "-i", str(out), "-map", "0:a:0", "-ac", "1",
                          "-ar", "48000", "-f", "s16le", "-"], capture_output=True, check=True).stdout
    assert max(abs(int.from_bytes(raw[i:i + 2], "little", signed=True)) for i in range(0, len(raw), 2)) <= 2
    # The still is untouched.
    assert client.media.rows["M-still"][1] == _png()


@needs_ffmpeg
def test_silent_video_records_provenance_and_dry_run_imports_nothing() -> None:
    client = _FakeClient({"M-still": _image_row("O-still", _png())})

    dry = silent_video(client, project="P", ref="M-still", duration=2.5, dry_run=True)
    assert dry.ok and dry.data["dry_run"] is True and dry.data["plan"]["frames"] == 60
    assert "import_file" not in [verb for verb, _ in client.media.calls]

    result = silent_video(client, project="P", ref="M-still", duration=2.5)
    assert result.ok, result.error
    relate = [args for verb, args in client.media.calls if verb == "relate"][0]
    assert relate["kind"] == "derived_from" and relate["to_object_id"] == "O-still"
    assert relate["metadata"]["operation"] == "silent-video"
    assert relate["metadata"]["plan"]["frames"] == 60
    assert result.data["provenance_recorded"] == "new"


def test_silent_video_refuses_a_non_image_and_an_off_grid_duration() -> None:
    video = {"object_id": "O-vid", "digest": "d", "filename": "a.mp4", "media_type": "video/mp4"}
    client = _FakeClient({"M-vid": (video, b"x")})
    result = silent_video(client, project="P", ref="M-vid", duration=2.5)
    assert not result.ok and result.error.code == "unsupported_operation"
    assert "import_file" not in [verb for verb, _ in client.media.calls]


@needs_ffmpeg
def test_off_grid_duration_is_a_validation_error_and_imports_nothing() -> None:
    client = _FakeClient({"M-still": _image_row("O-still", _png())})
    result = silent_video(client, project="P", ref="M-still", duration=2.51)
    assert not result.ok and result.error.code == "validation_error"
    assert "import_file" not in [verb for verb, _ in client.media.calls]


@needs_ffmpeg
def test_cli_silent_video_json_envelope_and_size_validation(capsys: Any) -> None:
    client = _FakeClient({"M-still": _image_row("O-still", _png())})
    rc = run_product_family("media", ["silent-video", "--project", "P", "M-still", "--duration", "2.5",
                                      "--dry-run", "--json"], client=client)
    assert rc == 0
    envelope = json.loads(capsys.readouterr().out)
    assert envelope["ok"] is True and envelope["data"]["plan"]["frames"] == 60

    rc = run_product_family("media", ["silent-video", "--project", "P", "M-still", "--duration", "2.5",
                                      "--size", "1023x575", "--json"], client=client)
    assert rc == 2
    with pytest.raises(SystemExit) as excinfo:
        run_product_family("media", ["silent-video", "--project", "P", "M-still"], client=client)
    assert excinfo.value.code == 2
