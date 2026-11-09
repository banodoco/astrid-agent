from __future__ import annotations

import json
import subprocess
from pathlib import Path
from typing import Any
from unittest.mock import patch

import pytest

from astrid.core.media import (
    MediaProbeError,
    MediaProbe,
    ffprobe_duration_seconds,
    ffprobe_metadata,
    ffprobe_metadata_strict,
)
from astrid.core.verify.checks import ffprobe_duration_seconds as checks_ffprobe_duration_seconds


def test_ffprobe_duration_seconds_uses_duration_only_probe() -> None:
    calls: list[tuple[list[str], dict[str, Any]]] = []

    def runner(cmd: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        calls.append((cmd, kwargs))
        return subprocess.CompletedProcess(cmd, 0, stdout="12.5\n", stderr="")

    assert ffprobe_duration_seconds("clip.mp4", runner=runner) == 12.5

    cmd, kwargs = calls[0]
    assert cmd == [
        "ffprobe",
        "-v",
        "error",
        "-show_entries",
        "format=duration",
        "-of",
        "default=noprint_wrappers=1:nokey=1",
        "clip.mp4",
    ]
    assert kwargs["check"] is True
    assert kwargs["capture_output"] is True
    assert kwargs["stdin"] == subprocess.DEVNULL
    assert kwargs["text"] is True
    assert "PATH" in kwargs["env"]
    assert "OPENAI_API_KEY" not in kwargs["env"]


def test_ffprobe_duration_seconds_accepts_explicit_env() -> None:
    calls: list[tuple[list[str], dict[str, Any]]] = []

    def runner(cmd: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        calls.append((cmd, kwargs))
        return subprocess.CompletedProcess(cmd, 0, stdout="12.5\n", stderr="")

    assert (
        ffprobe_duration_seconds(
            "clip.mp4", runner=runner, env={"FFPROBE_DATADIR": "/tmp/ffprobe"}
        )
        == 12.5
    )

    assert calls[0][1]["env"]["FFPROBE_DATADIR"] == "/tmp/ffprobe"


def test_updated_duration_helpers_preserve_float_parsing(tmp_path: Path) -> None:
    media = tmp_path / "clip.mp4"
    media.write_bytes(b"not-real-media")

    def fake_runner(cmd: list[str], **_kwargs: Any) -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess(cmd, 0, stdout="7.25\n", stderr="")

    assert ffprobe_duration_seconds(media, runner=fake_runner) == 7.25


def test_verify_uses_canonical_media_helper() -> None:
    assert checks_ffprobe_duration_seconds is ffprobe_duration_seconds


# ---------------------------------------------------------------------------
# MediaProbe and ffprobe_metadata tests (mocked subprocess, no real ffprobe)
# ---------------------------------------------------------------------------


HAPPY_FFPROBE_JSON = json.dumps(
    {
        "format": {
            "duration": "12.5",
            "format_name": "mov,mp4,m4a,3gp,3g2,mj2",
        },
        "streams": [
            {
                "codec_type": "video",
                "codec_name": "h264",
                "profile": "High",
                "level": 41,
                "pix_fmt": "yuv420p",
                "width": 1920,
                "height": 1080,
                "avg_frame_rate": "30000/1001",
                "r_frame_rate": "30000/1001",
                "time_base": "1/30000",
                "disposition": {"attached_pic": 0},
            },
            {
                "codec_type": "audio",
                "codec_name": "aac",
                "sample_rate": "48000",
                "channel_layout": "stereo",
            },
        ],
    }
)


class TestMediaProbeDefaults:
    """MediaProbe dataclass starts with all-None and _raw empty."""

    def test_default_all_none(self) -> None:
        probe = MediaProbe()
        assert probe.duration_seconds is None
        assert probe.fps is None
        assert probe.resolution is None
        assert probe.width is None
        assert probe.height is None
        assert probe._raw == {}
        assert probe.fps_rational is None
        assert probe.time_base is None
        assert probe.video_codec is None
        assert probe.pixel_format is None
        assert probe.audio_codec is None
        assert probe.audio_sample_rate is None
        assert probe.audio_channel_layout is None
        assert probe.has_video_stream is False
        assert probe.has_audio_stream is False

    def test_partial_construction(self) -> None:
        probe = MediaProbe(
            duration_seconds=5.0,
            width=640,
            height=480,
            resolution="640x480",
        )
        assert probe.duration_seconds == 5.0
        assert probe.fps is None
        assert probe.resolution == "640x480"
        assert probe.width == 640
        assert probe.height == 480


class TestFfprobeMetadataHappy:
    """ffprobe_metadata extracts all fields from valid JSON output."""

    def test_extracts_all_fields(self) -> None:
        with patch("subprocess.run") as mock_run, patch(
            "shutil.which", return_value="/usr/bin/ffprobe"
        ):
            mock_run.return_value = subprocess.CompletedProcess(
                [], 0, stdout=HAPPY_FFPROBE_JSON, stderr=""
            )
            probe = ffprobe_metadata("video.mp4")

        assert probe.duration_seconds == pytest.approx(12.5)
        assert probe.fps == pytest.approx(30000 / 1001)
        assert probe.resolution == "1920x1080"
        assert probe.width == 1920
        assert probe.height == 1080
        assert probe.fps_rational == (30000, 1001)
        assert probe.time_base == (1, 30000)
        assert probe.video_codec == "h264"
        assert probe.video_codec == "h264"
        assert probe.video_profile == "High"
        assert probe.video_level == "41"
        assert probe.pixel_format == "yuv420p"
        assert probe.audio_codec == "aac"
        assert probe.audio_sample_rate == 48000
        assert probe.audio_channel_layout == "stereo"
        assert probe.container == "mp4"
        assert probe.duration_rational == (25, 2)
        assert probe.has_video_stream is True
        assert probe.has_audio_stream is True
        assert probe._raw  # raw JSON preserved

    def test_channels_reported_without_inferred_layout(self) -> None:
        """Probes that report channel COUNT without channel_layout (e.g.
        QuickTime sowt) must stay honest: layout stays None, channels is
        reported, and validation compares counts (never guessed layouts)."""
        import json as _json

        payload = json.loads(HAPPY_FFPROBE_JSON)
        for stream in payload["streams"]:
            if stream.get("codec_type") == "audio":
                del stream["channel_layout"]
                stream["channels"] = 2
        with patch(
            "astrid.core.media.subprocess.run",
            return_value=subprocess.CompletedProcess(
                [], 0, stdout=_json.dumps(payload), stderr=""
            ),
        ):
            probe = ffprobe_metadata("video.mp4")
        assert probe.audio_channel_layout is None
        assert probe.audio_channels == 2

    def test_accepts_path_object(self, tmp_path: Path) -> None:
        vid = tmp_path / "clip.mp4"
        vid.write_bytes(b"dummy")
        with patch("subprocess.run") as mock_run, patch(
            "shutil.which", return_value="/usr/bin/ffprobe"
        ):
            mock_run.return_value = subprocess.CompletedProcess(
                [], 0, stdout=HAPPY_FFPROBE_JSON, stderr=""
            )
            probe = ffprobe_metadata(vid)

        assert probe.duration_seconds == pytest.approx(12.5)

    def test_prefers_average_frame_rate_and_skips_attached_picture(self) -> None:
        payload = json.dumps(
            {
                "format": {"duration": "2", "format_name": "matroska,webm"},
                "streams": [
                    {
                        "codec_type": "video",
                        "codec_name": "mjpeg",
                        "disposition": {"attached_pic": 1},
                    },
                    {
                        "codec_type": "video",
                        "codec_name": "vp9",
                        "pix_fmt": "yuv420p",
                        "width": 1280,
                        "height": 720,
                        "avg_frame_rate": "24/1",
                        "r_frame_rate": "30/1",
                        "time_base": "1/1000",
                    },
                ],
            }
        )
        with patch("subprocess.run") as mock_run, patch(
            "shutil.which", return_value="/usr/bin/ffprobe"
        ):
            mock_run.return_value = subprocess.CompletedProcess(
                [], 0, stdout=payload, stderr=""
            )
            probe = ffprobe_metadata("video.webm")

        assert probe.video_codec == "vp9"
        assert probe.fps_rational == (24, 1)
        assert probe.container == "webm"

    def test_uses_r_frame_rate_when_average_is_unusable(self) -> None:
        payload = json.dumps(
            {
                "format": {"duration": "1"},
                "streams": [
                    {
                        "codec_type": "video",
                        "width": 320,
                        "height": 240,
                        "avg_frame_rate": "0/0",
                        "r_frame_rate": "25/1",
                    }
                ],
            }
        )
        with patch("subprocess.run") as mock_run, patch(
            "shutil.which", return_value="/usr/bin/ffprobe"
        ):
            mock_run.return_value = subprocess.CompletedProcess(
                [], 0, stdout=payload, stderr=""
            )
            probe = ffprobe_metadata("video.mp4")

        assert probe.fps_rational == (25, 1)
        assert probe.fps == 25.0


class TestFfprobeMetadataDegraded:
    """ffprobe_metadata returns all-None MediaProbe on errors / missing ffprobe."""

    def test_no_ffprobe_on_path(self) -> None:
        with patch("shutil.which", return_value=None):
            probe = ffprobe_metadata("video.mp4")
        assert probe.duration_seconds is None
        assert probe.fps is None
        assert probe.resolution is None
        assert probe._raw == {}

    def test_nonzero_returncode(self) -> None:
        with patch("subprocess.run") as mock_run, patch(
            "shutil.which", return_value="/usr/bin/ffprobe"
        ):
            mock_run.return_value = subprocess.CompletedProcess(
                [], 1, stdout="", stderr="error"
            )
            probe = ffprobe_metadata("video.mp4")
        assert probe.duration_seconds is None

    def test_strict_probe_raises_on_nonzero_returncode(self) -> None:
        with patch("subprocess.run") as mock_run, patch(
            "shutil.which", return_value="/usr/bin/ffprobe"
        ):
            mock_run.return_value = subprocess.CompletedProcess(
                [], 1, stdout="", stderr="bad input"
            )
            with pytest.raises(MediaProbeError, match="ffprobe failed"):
                ffprobe_metadata_strict("video.mp4")

    def test_invalid_json(self) -> None:
        with patch("subprocess.run") as mock_run, patch(
            "shutil.which", return_value="/usr/bin/ffprobe"
        ):
            mock_run.return_value = subprocess.CompletedProcess(
                [], 0, stdout="not json", stderr=""
            )
            probe = ffprobe_metadata("video.mp4")
        assert probe.duration_seconds is None

    def test_timeout(self) -> None:
        with patch("subprocess.run") as mock_run, patch(
            "shutil.which", return_value="/usr/bin/ffprobe"
        ):
            mock_run.side_effect = subprocess.TimeoutExpired(cmd=[], timeout=1.0)
            probe = ffprobe_metadata("video.mp4")
        assert probe.duration_seconds is None

    def test_oserror(self) -> None:
        with patch("subprocess.run") as mock_run, patch(
            "shutil.which", return_value="/usr/bin/ffprobe"
        ):
            mock_run.side_effect = OSError("bad things")
            probe = ffprobe_metadata("video.mp4")
        assert probe.duration_seconds is None

    def test_missing_format_block(self) -> None:
        no_fmt = json.dumps({"streams": []})
        with patch("subprocess.run") as mock_run, patch(
            "shutil.which", return_value="/usr/bin/ffprobe"
        ):
            mock_run.return_value = subprocess.CompletedProcess(
                [], 0, stdout=no_fmt, stderr=""
            )
            probe = ffprobe_metadata("video.mp4")
        assert probe.duration_seconds is None

    def test_non_numeric_duration(self) -> None:
        bad_dur = json.dumps(
            {"format": {"duration": "nope"}, "streams": []}
        )
        with patch("subprocess.run") as mock_run, patch(
            "shutil.which", return_value="/usr/bin/ffprobe"
        ):
            mock_run.return_value = subprocess.CompletedProcess(
                [], 0, stdout=bad_dur, stderr=""
            )
            probe = ffprobe_metadata("video.mp4")
        assert probe.duration_seconds is None

    def test_no_video_stream(self) -> None:
        no_video = json.dumps(
            {
                "format": {"duration": "3.0"},
                "streams": [{"codec_type": "audio"}],
            }
        )
        with patch("subprocess.run") as mock_run, patch(
            "shutil.which", return_value="/usr/bin/ffprobe"
        ):
            mock_run.return_value = subprocess.CompletedProcess(
                [], 0, stdout=no_video, stderr=""
            )
            probe = ffprobe_metadata("audio.aac")
        assert probe.duration_seconds == 3.0
        assert probe.fps is None
        assert probe.resolution is None

    def test_division_by_zero_fps(self) -> None:
        zero_den = json.dumps(
            {
                "format": {"duration": "1.0"},
                "streams": [
                    {
                        "codec_type": "video",
                        "width": 100,
                        "height": 100,
                        "r_frame_rate": "30/0",
                    }
                ],
            }
        )
        with patch("subprocess.run") as mock_run, patch(
            "shutil.which", return_value="/usr/bin/ffprobe"
        ):
            mock_run.return_value = subprocess.CompletedProcess(
                [], 0, stdout=zero_den, stderr=""
            )
            probe = ffprobe_metadata("video.mp4")
        assert probe.duration_seconds == 1.0
        assert probe.resolution == "100x100"
        assert probe.fps is None  # division by zero swallowed
