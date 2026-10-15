from __future__ import annotations

import copy
import dataclasses
import importlib
import json
import struct
import subprocess
import zlib
from pathlib import Path
from unittest import mock

import pytest

from astrid.core.media import MediaProbe
from astrid.core.rendering.contracts import (
    SCHEMA_VERSION,
    AudioOwnership,
    FrameWindow,
    RenderRequest,
    RenderResult,
)
from astrid.packs.rendering.rendering.renderers.ffmpeg import audio_reactive_colour, command
from astrid.packs.rendering.rendering.renderers.ffmpeg import run as ffmpeg
from astrid.packs.rendering.rendering.renderers.ffmpeg.support import support as evaluate_support

support_module = importlib.import_module(
    "astrid.packs.rendering.rendering.renderers.ffmpeg.support"
)


def _timeline(*, include_audio: bool = True, duration: float = 4.0) -> dict:
    tracks = [{"id": "v", "kind": "visual", "label": "Video"}]
    clips = [
        {
            "id": "video",
            "at": 0,
            "track": "v",
            "clipType": "media",
            "asset": "video",
            "from": 0,
            "to": duration,
            "speed": 1,
            "volume": 0,
        }
    ]
    if include_audio:
        tracks.append(
            {
                "id": "a",
                "kind": "audio",
                "label": "Audio",
                "volume": 0.5,
            }
        )
        clips.append(
            {
                "id": "audio",
                "at": 0,
                "track": "a",
                "clipType": "media",
                "asset": "audio",
                "from": 0,
                "to": duration,
                "speed": 1,
                "volume": 0.4,
            }
        )
    return {
        "theme": "banodoco-default",
        "theme_overrides": {
            "visual": {"canvas": {"width": 640, "height": 360, "fps": 30}}
        },
        "tracks": tracks,
        "clips": clips,
    }


def _assets(tmp_path: Path, *, duration: float = 4.0) -> dict:
    return {
        "assets": {
            "video": {
                "file": "video.mp4",
                "type": "video/mp4",
                "duration": duration,
                "resolution": "640x360",
                "fps": 30,
            },
            "audio": {
                "file": "audio.wav",
                "type": "audio/wav",
                "duration": duration,
            },
        }
    }


def _video_probe(*, audio: bool = False, duration: float = 4.0) -> MediaProbe:
    return MediaProbe(
        duration_seconds=duration,
        width=640,
        height=360,
        fps=30,
        time_base=(1, 15360),
        resolution="640x360",
        video_codec="h264",
        pixel_format="yuv420p",
        audio_codec="aac" if audio else None,
        video_stream_present=True,
        audio_stream_present=audio,
    )


def _audio_probe(*, duration: float = 4.0, present: bool = True) -> MediaProbe:
    return MediaProbe(
        duration_seconds=duration,
        audio_codec="pcm_s16le" if present else None,
        video_stream_present=False,
        audio_stream_present=present,
    )


def _alpha_png(width: int, height: int, *, alpha: int | None = 128) -> bytes:
    def chunk(kind: bytes, payload: bytes) -> bytes:
        return (struct.pack(">I", len(payload)) + kind + payload +
                struct.pack(">I", zlib.crc32(kind + payload) & 0xFFFFFFFF))
    colour_type = 6 if alpha is not None else 2
    pixel = bytes((255, 0, 0, alpha)) if alpha is not None else bytes((255, 0, 0))
    rows = b"".join(b"\x00" + pixel * width for _ in range(height))
    return (b"\x89PNG\r\n\x1a\n" +
            chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, colour_type, 0, 0, 0)) +
            chunk(b"IDAT", zlib.compress(rows)) + chunk(b"IEND", b""))


def _png_overlay_timeline(duration: float = 4.0) -> dict:
    data = _timeline(duration=duration)
    data["tracks"].insert(0, {"id": "overlay", "kind": "visual", "label": "Overlay"})
    data["clips"].insert(0, {
        "id": "overlay", "at": 0, "track": "overlay", "clipType": "media",
        "asset": "overlay", "hold": duration,
    })
    return data


def _request(
    tmp_path: Path,
    *,
    audio: AudioOwnership | None = None,
    window: FrameWindow | None = None,
) -> RenderRequest:
    return RenderRequest(
        schema_version=SCHEMA_VERSION,
        timeline_path=str(tmp_path / "timeline.json"),
        assets_registry_path=str(tmp_path / "assets.json"),
        output_name="result.mp4",
        window=window,
        audio=audio,
        backend_config={ffmpeg.BACKEND_ID: {}},
    )


def _evaluate(
    tmp_path: Path,
    timeline_data: dict,
    assets: dict,
    *,
    probes: dict[str, MediaProbe] | None = None,
    missing_files: set[str] | None = None,
    which=None,
    request: RenderRequest | None = None,
    asset_bytes: dict[str, bytes] | None = None,
):
    missing = missing_files or set()
    for entry in assets.get("assets", {}).values():
        file_value = entry.get("file")
        if isinstance(file_value, str) and file_value not in missing:
            (tmp_path / file_value).write_bytes((asset_bytes or {}).get(file_value, b"source"))
    (tmp_path / "timeline.json").write_text(
        json.dumps(timeline_data),
        encoding="utf-8",
    )
    (tmp_path / "assets.json").write_text(json.dumps(assets), encoding="utf-8")
    probe_map = probes or {
        "video.mp4": _video_probe(),
        "audio.wav": _audio_probe(),
    }

    return evaluate_support(
        request or _request(tmp_path),
        timeline_data,
        assets,
        probe=lambda path: probe_map[Path(path).name],
        which=which or (lambda binary: f"/usr/bin/{binary}"),
    )


def _build_command(tmp_path: Path, timeline_data: dict, assets: dict) -> list[str]:
    _evaluate(tmp_path, timeline_data, assets)
    return command.build_render_command(_request(tmp_path), tmp_path)


def test_supported_report_exposes_request_specific_evidence(tmp_path: Path) -> None:
    report = _evaluate(tmp_path, _timeline(), _assets(tmp_path))

    assert report.supported is True
    assert report.reasons == []
    assert report.alternatives == []
    assert report.features["whole_media"] is True
    assert report.features["stream_copy"] is True
    assert report.features["audio_reactive_colour"] is False
    assert report.features["audio_ownership"] == "rendered"


def test_compiled_still_image_uses_declared_window_when_probe_has_no_duration(
    tmp_path: Path,
) -> None:
    """A compiled still has finite render evidence despite no intrinsic duration."""
    timeline_data = _timeline(include_audio=False, duration=2.0)
    timeline_data["clips"][0]["asset"] = "plate"
    assets = {
        "assets": {
            "plate": {
                "file": "plate.png",
                "type": "image",
                "duration": 2.0,
            }
        }
    }
    still_probe = MediaProbe(
        duration_seconds=None,
        width=640,
        height=360,
        video_codec="png",
        video_stream_present=True,
        audio_stream_present=False,
    )

    report = _evaluate(
        tmp_path,
        timeline_data,
        assets,
        probes={"plate.png": still_probe},
    )
    assert report.supported is True

    argv = command.build_render_command(_request(tmp_path), tmp_path)
    image_index = argv.index(str((tmp_path / "plate.png").resolve()))
    assert argv[image_index - 5 : image_index] == [
        "-loop",
        "1",
        "-t",
        "2.000000",
        "-i",
    ]


def test_support_accepts_canvas_sized_alpha_png_overlay(tmp_path: Path) -> None:
    timeline_data = _png_overlay_timeline()
    assets = _assets(tmp_path)
    assets["assets"]["overlay"] = {
        "file": "overlay.png", "type": "image/png", "resolution": "640x360"
    }
    probes = {
        "video.mp4": _video_probe(), "audio.wav": _audio_probe(),
        "overlay.png": MediaProbe(width=640, height=360, video_codec="png",
                                   pixel_format="rgba", video_stream_present=True,
                                   audio_stream_present=False),
    }
    report = _evaluate(tmp_path, timeline_data, assets, probes=probes,
                       asset_bytes={"overlay.png": _alpha_png(640, 360)})
    assert report.supported is True
    assert report.features["static_image_overlay"] is True
    assert report.features["stream_copy"] is False


@pytest.mark.parametrize("mutation", ["opaque", "wrong_size", "short_hold", "wrong_order"])
def test_png_overlay_rejects_unsupported_shape(tmp_path: Path, mutation: str) -> None:
    timeline_data = _png_overlay_timeline()
    assets = _assets(tmp_path)
    assets["assets"]["overlay"] = {
        "file": "overlay.png", "type": "image/png", "resolution": "640x360"
    }
    width, height = (320, 360) if mutation == "wrong_size" else (640, 360)
    if mutation == "short_hold":
        timeline_data["clips"][0]["hold"] = 3.5
    elif mutation == "wrong_order":
        timeline_data["tracks"][0], timeline_data["tracks"][1] = timeline_data["tracks"][1], timeline_data["tracks"][0]
    probes = {
        "video.mp4": _video_probe(), "audio.wav": _audio_probe(),
        "overlay.png": MediaProbe(width=width, height=height, video_codec="png",
                                   pixel_format="rgba", video_stream_present=True,
                                   audio_stream_present=False),
    }
    report = _evaluate(tmp_path, timeline_data, assets, probes=probes,
                       asset_bytes={"overlay.png": _alpha_png(width, height, alpha=None if mutation == "opaque" else 128)})
    assert report.supported is False


def test_png_overlay_command_preserves_current_text_audio_input_order(tmp_path: Path) -> None:
    timeline_data = _png_overlay_timeline()
    assets = _assets(tmp_path)
    assets["assets"]["overlay"] = {
        "file": "overlay.png", "type": "image/png", "resolution": "640x360"
    }
    _evaluate(tmp_path, timeline_data, assets,
              probes={"video.mp4": _video_probe(), "audio.wav": _audio_probe(),
                      "overlay.png": MediaProbe(width=640, height=360,
                                                 video_codec="png", pixel_format="rgba",
                                                 video_stream_present=True,
                                                 audio_stream_present=False)},
              asset_bytes={"overlay.png": _alpha_png(640, 360)})
    argv = command.build_render_command(_request(tmp_path), tmp_path)
    filters = argv[argv.index("-filter_complex") + 1]
    assert "concat=n=1:v=1:a=0[vbase]" in filters
    assert "format=rgba[voverlay]" in filters
    assert "[vbase][voverlay]overlay=0:0:format=auto[vout]" in filters
    assert argv[argv.index("-c:v") + 1] == "libx264"


@pytest.mark.parametrize(
    ("case", "reason"),
    [
        ("unknown_track_kind", "unsupported kind"),
        ("unknown_clip_kind", "unsupported clip kind"),
        ("unknown_track", "unknown track"),
        ("invalid_bounds", "positive source bounds"),
        ("source_bound", "exceeds"),
        ("visual_gap", "Visual gap"),
        ("visual_overlap", "Visual overlap"),
        ("speed", "unsupported speed"),
        ("transform", "unsupported transforms"),
        ("track_transform", "transform semantics"),
        ("crop", "unsupported crop"),
        ("effects", "unsupported effects"),
        ("transition", "unsupported transition"),
        ("opacity", "non-default opacity"),
        ("discarded_visual_audio", "embedded audio"),
        ("overlapping_audio", "Overlapping audio"),
        ("fade", "ENVELOPE_UNRENDERED"),
        ("missing_source", "source is missing"),
        ("missing_video_stream", "no video stream"),
        ("missing_audio_stream", "no audio stream"),
        ("missing_binary", "required binary is unavailable"),
        ("window", "frame windows"),
    ],
)
def test_support_fails_closed_for_every_unsupported_semantic(
    tmp_path: Path,
    case: str,
    reason: str,
) -> None:
    timeline_data = _timeline()
    assets = _assets(tmp_path)
    probes = {
        "video.mp4": _video_probe(),
        "audio.wav": _audio_probe(),
    }
    missing_files: set[str] = set()
    which = lambda binary: f"/usr/bin/{binary}"
    request = _request(tmp_path)

    if case == "unknown_track_kind":
        timeline_data["tracks"][1]["kind"] = "captions"
    elif case == "unknown_clip_kind":
        timeline_data["clips"][0]["clipType"] = "text-card"
    elif case == "unknown_track":
        timeline_data["clips"][0]["track"] = "missing"
    elif case == "invalid_bounds":
        timeline_data["clips"][0]["to"] = 0
    elif case == "source_bound":
        timeline_data["clips"][0]["to"] = 5
    elif case == "visual_gap":
        timeline_data["clips"][0]["at"] = 0.25
    elif case == "visual_overlap":
        timeline_data["clips"][0]["to"] = 2
        timeline_data["clips"].append(
            {
                **copy.deepcopy(timeline_data["clips"][0]),
                "id": "video_2",
                "at": 1.5,
                "from": 2,
                "to": 4,
            }
        )
    elif case == "speed":
        timeline_data["clips"][0]["speed"] = 1.25
    elif case == "transform":
        timeline_data["clips"][0]["x"] = 10
    elif case == "track_transform":
        timeline_data["tracks"][0]["scale"] = 1.2
    elif case == "crop":
        timeline_data["clips"][0]["cropTop"] = 10
    elif case == "effects":
        timeline_data["clips"][0]["effects"] = {"fade_in": 0.2}
    elif case == "transition":
        timeline_data["clips"][0]["transition"] = {"type": "fade"}
    elif case == "opacity":
        timeline_data["clips"][0]["opacity"] = 0.5
    elif case == "discarded_visual_audio":
        timeline_data["clips"][0]["volume"] = 1
        probes["video.mp4"] = _video_probe(audio=True)
    elif case == "overlapping_audio":
        timeline_data["clips"][1]["to"] = 2
        timeline_data["clips"].append(
            {
                **copy.deepcopy(timeline_data["clips"][1]),
                "id": "audio_2",
                "at": 1.5,
                "from": 2,
                "to": 4,
            }
        )
    elif case == "fade":
        timeline_data["clips"][1]["params"] = {"fadeIn": 0.2, "fadeInCurve": "unknown"}
    elif case == "missing_source":
        missing_files.add("video.mp4")
    elif case == "missing_video_stream":
        probes["video.mp4"] = _audio_probe()
    elif case == "missing_audio_stream":
        probes["audio.wav"] = _audio_probe(present=False)
    elif case == "missing_binary":
        which = lambda binary: None if binary == "ffmpeg" else "/usr/bin/ffprobe"
    elif case == "window":
        request = _request(
            tmp_path,
            window=FrameWindow(
                start_frame=0,
                end_frame=30,
                fps_rational=(30, 1),
            ),
        )

    report = _evaluate(
        tmp_path,
        timeline_data,
        assets,
        probes=probes,
        missing_files=missing_files,
        which=which,
        request=request,
    )

    assert report.supported is False
    assert any(reason in item for item in report.reasons)
    assert report.alternatives == ["rendering.remotion"]
    assert all("." in backend for backend in report.alternatives)


@pytest.mark.parametrize(
    ("target", "value"),
    [
        ("track", -0.1),
        ("track", 1.1),
        ("clip", -0.1),
        ("clip", 1.1),
    ],
)
def test_support_rejects_malformed_gains(
    tmp_path: Path,
    target: str,
    value: float,
) -> None:
    timeline_data = _timeline()
    if target == "track":
        timeline_data["tracks"][1]["volume"] = value
    else:
        timeline_data["clips"][1]["volume"] = value

    report = _evaluate(tmp_path, timeline_data, _assets(tmp_path))

    assert report.supported is False
    assert any("between 0 and 1" in reason for reason in report.reasons)


def test_track_and_clip_gain_multiply_into_filter(tmp_path: Path) -> None:
    argv = _build_command(tmp_path, _timeline(), _assets(tmp_path))
    filters = argv[argv.index("-filter_complex") + 1]

    assert "volume=0.200000" in filters


@pytest.mark.parametrize(("muted", "clip_volume"), [(True, 0.9), (False, 0.0)])
def test_track_mute_and_clip_zero_force_silence(
    tmp_path: Path,
    muted: bool,
    clip_volume: float,
) -> None:
    timeline_data = _timeline()
    timeline_data["tracks"][1]["muted"] = muted
    timeline_data["clips"][1]["volume"] = clip_volume

    argv = _build_command(tmp_path, timeline_data, _assets(tmp_path))
    filters = argv[argv.index("-filter_complex") + 1]

    assert "volume=0.000000" in filters


def test_non_overlapping_audio_clips_concat_with_positional_silence(
    tmp_path: Path,
) -> None:
    timeline_data = _timeline()
    timeline_data["clips"][1]["to"] = 1
    timeline_data["clips"].append(
        {
            **copy.deepcopy(timeline_data["clips"][1]),
            "id": "audio_2",
            "at": 2,
            "from": 1,
            "to": 3,
        }
    )

    report = _evaluate(tmp_path, timeline_data, _assets(tmp_path))
    argv = command.build_render_command(_request(tmp_path), tmp_path)
    filters = argv[argv.index("-filter_complex") + 1]

    assert report.supported is True
    assert "anullsrc=r=48000:cl=stereo,atrim=duration=1.000000" in filters
    assert filters.count("volume=0.200000") == 2
    assert "concat=n=3:v=0:a=1[aout]" in filters


def test_visual_only_command_has_no_synthesized_audio_and_reports_none(
    tmp_path: Path,
) -> None:
    timeline_data = _timeline(include_audio=False)
    assets = _assets(tmp_path)
    del assets["assets"]["audio"]

    report = _evaluate(
        tmp_path,
        timeline_data,
        assets,
        probes={"video.mp4": _video_probe()},
    )
    argv = command.build_render_command(_request(tmp_path), tmp_path)

    assert report.supported is True
    assert report.features["audio_ownership"] == "none"
    # Without probe evidence of whole-source compatibility, stream-copy must
    # NOT be trusted from registry metadata: the builder re-encodes via
    # filter_complex with no audio mapping (-an).
    assert "-filter_complex" in argv
    assert "-c:a" not in argv
    assert "-an" in argv
    assert argv[argv.index("-c:v") + 1] == "libx264"


def test_visual_only_request_can_delegate_audio_as_passthrough(tmp_path: Path) -> None:
    timeline_data = _timeline(include_audio=False)
    assets = _assets(tmp_path)
    del assets["assets"]["audio"]

    report = _evaluate(
        tmp_path,
        timeline_data,
        assets,
        probes={"video.mp4": _video_probe()},
        request=_request(tmp_path, audio=AudioOwnership.PASSTHROUGH),
    )

    assert report.supported is True
    assert report.features["audio_ownership"] == "passthrough"


def test_visual_only_protocol_result_declares_none(tmp_path: Path) -> None:
    timeline_data = _timeline(include_audio=False)
    assets = _assets(tmp_path)
    del assets["assets"]["audio"]
    _evaluate(
        tmp_path,
        timeline_data,
        assets,
        probes={"video.mp4": _video_probe()},
    )
    probe = MediaProbe(
        duration_seconds=4,
        width=640,
        height=360,
        fps=30,
        fps_rational=(30, 1),
        time_base=(1, 15360),
        resolution="640x360",
        video_codec="h264",
        video_profile="High",
        video_level="40",
        pixel_format="yuv420p",
        container="mp4",
        duration_rational=(4, 1),
        video_stream_present=True,
        audio_stream_present=False,
    )
    seen: dict[str, list[str]] = {}

    def fake_run(argv: list[str], **_kwargs) -> subprocess.CompletedProcess[str]:
        seen["argv"] = argv
        output = Path(argv[-1])
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_bytes(b"video")
        return subprocess.CompletedProcess(argv, 0)

    with (
        mock.patch.object(support_module.shutil, "which", return_value="/usr/bin/tool"),
        mock.patch.object(ffmpeg, "ffprobe_metadata_strict", return_value=probe),
        mock.patch.object(ffmpeg.subprocess, "run", side_effect=fake_run),
        mock.patch.object(
            ffmpeg.remotion_backend,
            "_render_provenance_payload",
            return_value={"registry_hash": "test-registry"},
        ),
        # Provenance is already stubbed here; do not discover installed packs
        # under the process-wide subprocess mock (which treats Git as FFmpeg).
        mock.patch.object(ffmpeg.remotion_backend, "_effective_registry_state", return_value={}),
        mock.patch.object(ffmpeg, "validate_render_result"),
    ):
        result = ffmpeg._protocol_render(_request(tmp_path), workspace=tmp_path)

    assert result.audio_ownership is AudioOwnership.NONE
    assert result.video.audio is AudioOwnership.NONE
    assert result.video.profile.has_audio is False
    assert "-an" in seen["argv"]
    assert "-c:a" not in seen["argv"]


def _reactive_timeline() -> dict:
    return {
        "theme": "banodoco-default",
        "theme_overrides": {
            "visual": {"canvas": {"width": 640, "height": 360, "fps": 30}}
        },
        "tracks": [
            {"id": "v", "kind": "visual", "label": "Colour"},
            {
                "id": "a",
                "kind": "audio",
                "label": "Audio",
                "volume": 0.5,
            },
        ],
        "clips": [
            {
                "id": "colour",
                "at": 0,
                "track": "v",
                "clipType": "audio-reactive-colour",
                "hold": 1,
                "params": {
                    "schemaVersion": 1,
                    "initialColor": "#102030",
                    "events": [
                        {"id": "one", "frame": 3, "color": "#D47795"},
                        {"id": "two", "frame": 8, "color": "#26A7D0"},
                    ],
                },
            },
            {
                "id": "audio",
                "at": 0,
                "track": "a",
                "clipType": "media",
                "asset": "audio",
                "from": 0,
                "to": 1,
                "volume": 0.4,
            },
        ],
    }


def test_audio_reactive_support_gain_and_protocol_provenance_fragments(
    tmp_path: Path,
) -> None:
    timeline_data = _reactive_timeline()
    assets = {
        "assets": {
            "audio": {
                "file": "audio.wav",
                "type": "audio/wav",
                "duration": 1,
            }
        }
    }
    report = _evaluate(
        tmp_path,
        timeline_data,
        assets,
        probes={"audio.wav": _audio_probe(duration=1)},
    )
    spec = audio_reactive_colour.match_and_validate(
        timeline_data,
        assets,
        tmp_path / "assets.json",
    )

    assert report.supported is True
    assert report.features["audio_reactive_colour"] is True
    assert report.features["specialization"] == "audio-reactive-colour/v1"
    assert spec is not None
    assert spec.audio_volume == pytest.approx(0.2)

    output_probe = MediaProbe(
        width=640,
        height=360,
        fps_rational=(30, 1),
        time_base=(1, 15360),
        video_codec="h264",
        video_profile="High",
        video_level="40",
        pixel_format="yuv420p",
        audio_codec="aac",
        audio_sample_rate=48000,
        audio_channel_layout="stereo",
        audio_channels=2,
        container="mp4",
        duration_rational=(1, 1),
        video_stream_present=True,
        audio_stream_present=True,
    )

    def fake_render(
        _spec: audio_reactive_colour.AudioReactiveColourSpec,
        output: Path,
    ) -> Path:
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_bytes(b"video")
        return output

    def probe(path: Path) -> MediaProbe:
        return (
            _audio_probe(duration=1)
            if Path(path).name == "audio.wav"
            else output_probe
        )

    with (
        mock.patch.object(support_module.shutil, "which", return_value="/usr/bin/tool"),
        mock.patch.object(ffmpeg, "ffprobe_metadata_strict", side_effect=probe),
        mock.patch.object(audio_reactive_colour, "render", side_effect=fake_render),
        mock.patch.object(
            ffmpeg.remotion_backend,
            "_render_provenance_payload",
            return_value={"registry_hash": "test-registry"},
        ),
        # Provenance is already stubbed here; do not discover installed packs
        # under the process-wide subprocess mock (which treats Git as FFmpeg).
        mock.patch.object(ffmpeg.remotion_backend, "_effective_registry_state", return_value={}),
        mock.patch.object(ffmpeg, "validate_render_result"),
    ):
        result = ffmpeg._protocol_render(_request(tmp_path), workspace=tmp_path)

    assert isinstance(result, RenderResult)
    assert result.audio_ownership is AudioOwnership.RENDERED
    fragment = result.backend_fragments[ffmpeg.BACKEND_ID]["specialization"]
    assert fragment["id"] == "audio-reactive-colour/v1"
    assert [marker["frame"] for marker in fragment["markers"]] == [3, 8]
    assert fragment["event_count"] == 2
    assert fragment["frame_count"] == 30
    assert fragment["fps"] == 30
    assert len(fragment["marker_sha256"]) == 64


def test_pinned_video_profile_and_level_are_rejected_as_unguaranteed(
    tmp_path: Path,
) -> None:
    """A request pinning video_profile/video_level cannot be guaranteed by
    the FFmpeg command (encoder default or stream-copy preserves source
    values), so support must fail closed instead of failing strict
    post-render validation."""
    request = _request(tmp_path)
    from astrid.core.rendering.contracts import RenderProfile

    base_profile = RenderProfile(
        width=640,
        height=360,
        fps_rational=(30, 1),
        time_base=(1, 15360),
        container="mp4",
        video_codec="h264",
        video_profile=None,
        video_level=None,
        pixel_format="yuv420p",
    )
    request = dataclasses.replace(
        request,
        profile=dataclasses.replace(
            base_profile,
            video_profile="High",
            video_level="40",
        ),
    )
    report = _evaluate(tmp_path, _timeline(), _assets(tmp_path), request=request)
    assert report.supported is False
    assert any("video_profile" in reason or "video_level" in reason for reason in report.reasons)


def _text_clip(**overrides: object) -> dict:
    clip = {
        "id": "title",
        "at": 0.5,
        "track": "v",
        "clipType": "text",
        "hold": 1,
        "text": {"content": "Hello"},
    }
    clip.update(overrides)
    return clip


def _with_font(monkeypatch: pytest.MonkeyPatch, path: str | None) -> None:
    """Pin the support-time font resolver: a fake path accepts, None rejects."""
    monkeypatch.setattr(
        support_module,
        "_resolve_font_path",
        (lambda bold: Path(path)) if path else (lambda bold: None),
    )


def test_support_accepts_text_overlay_without_fades(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _with_font(monkeypatch, "font.ttf")
    timeline_data = _timeline()
    timeline_data["clips"].append(_text_clip())

    report = _evaluate(tmp_path, timeline_data, _assets(tmp_path))

    assert report.supported is True
    assert report.reasons == []
    assert report.features["media_only"] is False
    assert report.features["text_overlay"] is True
    assert report.features["fade_envelope"] is False
    assert report.features["whole_media"] is False
    assert report.features["stream_copy"] is False


def test_support_accepts_text_overlay_with_fades_and_vetoes_stream_copy(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _with_font(monkeypatch, "font.ttf")
    timeline_data = _timeline()
    timeline_data["clips"].append(
        _text_clip(effects={"fade_in": 0.25, "fade_out": 0.5})
    )

    report = _evaluate(tmp_path, timeline_data, _assets(tmp_path))

    assert report.supported is True
    assert report.features["media_only"] is False
    assert report.features["text_overlay"] is True
    assert report.features["fade_envelope"] is True
    assert report.features["whole_media"] is False
    assert report.features["stream_copy"] is False


def test_support_accepts_text_on_media_track_and_extra_text_only_track(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _with_font(monkeypatch, "font.ttf")
    timeline_data = _timeline()
    timeline_data["tracks"].append(
        {"id": "brand", "kind": "visual", "label": "Brand"}
    )
    timeline_data["clips"].append(_text_clip(id="title_v"))
    timeline_data["clips"].append(
        _text_clip(id="title_brand", track="brand", at=0.25)
    )

    report = _evaluate(tmp_path, timeline_data, _assets(tmp_path))

    assert report.supported is True
    assert report.reasons == []
    assert report.features["text_overlay"] is True
    assert report.features["media_only"] is False
    assert report.features["whole_media"] is False
    assert report.features["stream_copy"] is False


def test_support_accepts_text_window_boundaries(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A text window ending exactly at media coverage and a fade envelope
    exactly filling its window are both legal."""
    _with_font(monkeypatch, "font.ttf")
    timeline_data = _timeline()
    timeline_data["clips"].append(_text_clip(at=3.0, hold=1.0))
    timeline_data["clips"].append(
        _text_clip(
            id="full_fade",
            at=1.0,
            hold=1.0,
            effects={"fade_in": 0.5, "fade_out": 0.5},
        )
    )

    report = _evaluate(tmp_path, timeline_data, _assets(tmp_path))

    assert report.supported is True
    assert report.reasons == []


@pytest.mark.parametrize(
    ("case", "reason"),
    [
        ("text_only_no_media", "needs at least one visual media clip"),
        ("extra_visual_media_track", "exactly one visual track carrying media clips"),
        ("empty_extra_visual_track", "has no clips"),
        ("text_from", "must not declare from"),
        ("text_position", "unsupported transforms"),
        ("text_audio_track", "must sit on a visual track"),
        ("text_asset", "must not reference an asset"),
        ("text_hold_zero", "positive duration"),
        ("text_missing_content", "non-empty text.content"),
        ("text_unknown_param", "unsupported text params"),
        ("text_unknown_fade_key", "unsupported effect keys"),
        ("text_entrance_effect", "unsupported effects"),
        ("text_bad_color", "color"),
        ("text_bad_shadow", "textShadow"),
        ("missing_font", "no TTF font"),
        ("text_past_media", "beyond the visual media coverage"),
        ("text_fade_envelope_too_long", "fade envelope"),
    ],
)
def test_support_fails_closed_for_text_semantics(
    tmp_path: Path,
    case: str,
    reason: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _with_font(monkeypatch, "font.ttf")
    timeline_data = _timeline()

    if case == "text_only_no_media":
        timeline_data = _timeline(include_audio=False)
        timeline_data["clips"] = [_text_clip()]
    elif case == "extra_visual_media_track":
        timeline_data["tracks"].append({"id": "v2", "kind": "visual", "label": "B"})
        timeline_data["clips"].append(
            {
                "id": "video_2",
                "at": 0,
                "track": "v2",
                "clipType": "media",
                "asset": "video",
                "from": 0,
                "to": 4,
                "speed": 1,
                "volume": 0,
            }
        )
    elif case == "empty_extra_visual_track":
        timeline_data["tracks"].append({"id": "v2", "kind": "visual", "label": "B"})
    elif case == "text_from":
        timeline_data["clips"].append(_text_clip(**{"from": 0}))
    elif case == "text_position":
        timeline_data["clips"].append(_text_clip(x=10, y=20))
    elif case == "text_audio_track":
        timeline_data["clips"].append(_text_clip(track="a"))
    elif case == "text_asset":
        timeline_data["clips"].append(_text_clip(asset="video"))
    elif case == "text_hold_zero":
        timeline_data["clips"].append(_text_clip(hold=0))
    elif case == "text_missing_content":
        timeline_data["clips"].append(_text_clip(text={}))
    elif case == "text_unknown_param":
        timeline_data["clips"].append(_text_clip(params={"banana": 1}))
    elif case == "text_unknown_fade_key":
        timeline_data["clips"].append(_text_clip(effects={"slide_in": 0.3}))
    elif case == "text_entrance_effect":
        timeline_data["clips"].append(_text_clip(entrance={"type": "slide"}))
    elif case == "text_bad_color":
        timeline_data["clips"].append(
            _text_clip(text={"content": "Hello", "color": "not-a-color"})
        )
    elif case == "text_bad_shadow":
        timeline_data["clips"].append(_text_clip(params={"textShadow": "1 2"}))
    elif case == "text_past_media":
        timeline_data["clips"].append(_text_clip(at=3.5, hold=2))
    elif case == "text_fade_envelope_too_long":
        timeline_data["clips"].append(
            _text_clip(effects={"fade_in": 0.75, "fade_out": 0.5})
        )
    elif case == "missing_font":
        _with_font(monkeypatch, None)
        timeline_data["clips"].append(_text_clip())

    report = _evaluate(tmp_path, timeline_data, _assets(tmp_path))

    assert report.supported is False
    assert any(reason in item for item in report.reasons)
    assert report.alternatives == ["rendering.remotion"]


def test_audio_legacy_fades_compile_to_sample_filter_once(tmp_path):
    timeline_data = _timeline()
    timeline_data["clips"][1]["params"] = {"fadeIn": .2, "fadeOut": .3, "fadeInCurve": "equal-power"}
    report = _evaluate(tmp_path, timeline_data, _assets(tmp_path))
    assert report.supported
    argv = command.build_render_command(_request(tmp_path), tmp_path)
    filters = argv[argv.index("-filter_complex") + 1]
    assert "aeval=exprs=" in filters and "sin(" in filters
    assert "afade=" not in filters and "volume=" not in filters
    assert "amix=inputs=1:normalize=0" in filters


def test_authored_duck_without_runtime_projection_refuses(tmp_path):
    timeline_data = _timeline()
    timeline_data["clips"][1]["app"] = {"astrid": {"duck": {"under": "voice", "by": "-10dB", "attack": "6f", "release": "12f"}}}
    report = _evaluate(tmp_path, timeline_data, _assets(tmp_path))
    assert not report.supported
    assert any("DUCK_UNRENDERED" in reason for reason in report.reasons)
