from __future__ import annotations

import dataclasses
import json
import subprocess
from pathlib import Path
from unittest import mock

import yaml
import pytest

from astrid.core.media import MediaProbe
from astrid.core.rendering.contracts import (
    SCHEMA_VERSION,
    AudioOwnership,
    RendererManifest,
    RenderProfile,
    RenderRequest,
    RenderResult,
    SupportReport,
    VideoArtifact,
)
from astrid.core.rendering.transport import CommandTransport
from astrid.packs.rendering.rendering.renderers.ffmpeg import audio_reactive_colour, command
from astrid.packs.rendering.rendering.renderers.ffmpeg import run as ffmpeg
from astrid.packs.rendering.actions.render import audio_reactive_colour as legacy_audio_reactive
from astrid.packs.rendering.actions.render import run as facade

ROOT = Path(__file__).resolve().parents[3]


def _media_timeline(*, include_audio: bool = True) -> dict:
    tracks = [{"id": "v", "kind": "visual", "label": "Video"}]
    clips = [
        {
            "id": "video",
            "at": 0,
            "track": "v",
            "clipType": "media",
            "asset": "main",
            "from": 0,
            "to": 2,
            "speed": 1,
            "volume": 0,
        }
    ]
    if include_audio:
        tracks.append({"id": "a", "kind": "audio", "label": "Audio"})
        clips.append(
            {
                "id": "audio",
                "at": 0,
                "track": "a",
                "clipType": "media",
                "asset": "main",
                "from": 0,
                "to": 2,
                "speed": 1,
                "volume": 0.75,
            }
        )
    return {
        "theme": "banodoco-default",
        "theme_overrides": {
            "visual": {
                "canvas": {"width": 1920, "height": 1080, "fps": 30}
            }
        },
        "tracks": tracks,
        "clips": clips,
    }


def _text_timeline() -> dict:
    data = _media_timeline()
    data["clips"].append(
        {
            "id": "title",
            "at": 0.5,
            "track": "v",
            "clipType": "text-card",
            "hold": 1,
        }
    )
    return data


def _write_inputs(
    tmp_path: Path,
    *,
    timeline_data: dict | None = None,
    registered: bool = True,
    source_resolution: str = "1920x1080",
) -> tuple[Path, Path]:
    timeline_path = tmp_path / "timeline.json"
    assets_path = tmp_path / "assets.json"
    source_path = tmp_path / "source.mp4"
    source_path.write_bytes(b"placeholder")
    timeline_path.write_text(
        json.dumps(timeline_data or _media_timeline()),
        encoding="utf-8",
    )
    assets = (
        {
            "main": {
                "file": source_path.name,
                "type": "video/mp4",
                "duration": 2,
                "resolution": source_resolution,
                "fps": 30,
            }
        }
        if registered
        else {}
    )
    assets_path.write_text(json.dumps({"assets": assets}), encoding="utf-8")
    return timeline_path, assets_path


def _request(timeline_path: Path, assets_path: Path) -> RenderRequest:
    return RenderRequest(
        schema_version=SCHEMA_VERSION,
        timeline_path=str(timeline_path),
        assets_registry_path=str(assets_path),
        output_name="result.mp4",
        backend_config={ffmpeg.BACKEND_ID: {}},
    )


def _profile() -> RenderProfile:
    return RenderProfile(
        width=1920,
        height=1080,
        fps_rational=(30, 1),
        time_base=(1, 15360),
        container="mp4",
        video_codec="h264",
        video_profile="High",
        video_level="4.0",
        pixel_format="yuv420p",
        audio_codec="aac",
        audio_sample_rate=48000,
        audio_channel_layout="stereo",
    )


def test_manifest_registers_static_raw_command_backend() -> None:
    manifest_path = (
        ROOT
        / "astrid"
        / "packs"
        / "rendering"
        / "rendering"
        / "renderers"
        / "ffmpeg"
        / "renderer.yaml"
    )
    manifest = RendererManifest.from_dict(
        yaml.safe_load(manifest_path.read_text(encoding="utf-8"))
    )

    assert manifest.id == "rendering.ffmpeg"
    assert manifest.protocol_version == 1
    assert manifest.command == ("python3", "run.py")
    assert manifest.operations == ("render", "support")
    assert manifest.required_permissions == ("project_files", "subprocess")
    assert manifest.required_binaries == ("ffmpeg", "ffprobe")
    assert manifest.capabilities["clip_types"] == ["media", "text"]
    assert manifest.capabilities["features"] == {
        "media_only": False,
        "text_overlay": True,
        "fade_envelope": True,
        "stream_copy": True,
        "sequential_audio": True,
        "static_image_overlay": True,
    }
    assert (manifest_path.parent / manifest.command[1]).is_file()

    pack = yaml.safe_load(
        (manifest_path.parents[3] / "pack.yaml").read_text(encoding="utf-8")
    )
    entry = pack["rendering"]["ffmpeg"]
    assert entry["type"] == "renderer"
    assert entry["path"] == "rendering/renderers/ffmpeg/renderer.yaml"
    assert (manifest_path.parents[3] / entry["path"]).resolve() == manifest_path.resolve()


def test_support_is_strict_while_legacy_facade_eligibility_is_preserved(
    tmp_path: Path,
) -> None:
    timeline_path, assets_path = _write_inputs(tmp_path)
    source_probe = MediaProbe(
        duration_seconds=2,
        width=1920,
        height=1080,
        fps=30,
        time_base=(1, 15360),
        video_codec="h264",
        pixel_format="yuv420p",
        audio_codec="aac",
        video_stream_present=True,
        audio_stream_present=True,
    )

    with mock.patch.object(
        ffmpeg,
        "ffprobe_metadata_strict",
        return_value=source_probe,
    ):
        report = ffmpeg.support(
            _request(timeline_path, assets_path),
            workspace=tmp_path,
        )

    assert report.supported is True
    assert report.reasons == []
    assert report.backend == ffmpeg.BACKEND_ID
    assert report.features["audio_ownership"] == "rendered"
    assert report.features["whole_media"] is True


def test_support_rejects_non_media_timeline(tmp_path: Path) -> None:
    timeline_path, assets_path = _write_inputs(
        tmp_path,
        timeline_data=_text_timeline(),
    )

    report = ffmpeg.support(_request(timeline_path, assets_path), workspace=tmp_path)

    assert report.supported is False
    assert any("unsupported clip kind" in reason for reason in report.reasons)


def test_ffmpeg_canvas_declines_genuine_fps_ratio_but_keeps_integer_rates():
    from astrid.packs.rendering.rendering.renderers.ffmpeg.support import _canvas

    assert _canvas({"output": {"resolution": "640x360", "fps": 30}}) == (640, 360, 30)
    with pytest.raises(ValueError, match="positive integers"):
        _canvas({"output": {"resolution": "640x360", "fps": {"numerator": 30000, "denominator": 1001}}})


def test_raw_support_adapter_writes_authoritative_report(tmp_path: Path) -> None:
    timeline_path, assets_path = _write_inputs(tmp_path, registered=False)
    request_path = tmp_path / "request.json"
    result_path = tmp_path / "result.json"
    request_path.write_text(
        json.dumps(_request(timeline_path, assets_path).to_dict()),
        encoding="utf-8",
    )

    assert ffmpeg.main(
        [
            "support",
            "--request",
            str(request_path),
            "--result",
            str(result_path),
        ]
    ) == 0

    report = SupportReport.from_dict(
        json.loads(result_path.read_text(encoding="utf-8"))
    )
    assert report.supported is False
    assert report.alternatives == ["rendering.remotion"]
    assert report.backend == ffmpeg.BACKEND_ID


def test_manifest_command_dispatches_from_pack_root(tmp_path: Path) -> None:
    timeline_path, assets_path = _write_inputs(tmp_path, registered=False)
    request_path = tmp_path / "transport-request.json"
    result_path = tmp_path / "transport-result.json"
    request_payload = _request(timeline_path, assets_path).to_dict()
    request_payload["backend_config"] = {}
    request_path.write_text(
        json.dumps(request_payload),
        encoding="utf-8",
    )

    report = CommandTransport(ffmpeg.BACKEND_ID).run(
        "support",
        ("python3", "run.py"),
        request_path=request_path,
        result_path=result_path,
        cwd=ROOT / "astrid" / "packs" / "rendering",
    )

    assert isinstance(report, SupportReport)
    assert report.backend == ffmpeg.BACKEND_ID
    assert report.supported is False


def test_build_render_command_is_pure_and_preserves_stream_copy(
    tmp_path: Path,
) -> None:
    timeline_path, assets_path = _write_inputs(tmp_path)
    request = _request(timeline_path, assets_path)
    inputs = command.resolve_render_command_inputs(request, tmp_path)

    # Stream-copy is gated on probe evidence; the pure builder emits the copy
    # path only when the caller passes stream_copy_allowed=True. A placeholder
    # source (no real probe) must default to re-encoding.
    argv = command.build_render_command(request, tmp_path)
    assert argv[argv.index("-c:v") + 1] == "libx264"

    copy_argv = command.build_render_command_from_inputs(
        dataclasses.replace(inputs, stream_copy_allowed=True)
    )
    assert copy_argv[copy_argv.index("-c:v") + 1] == "copy"
    assert copy_argv[-1] == str((tmp_path / "outputs" / "result.mp4").resolve())
    assert not (tmp_path / "outputs").exists()


def test_build_render_command_encodes_visual_only_without_synthesizing_silence(
    tmp_path: Path,
) -> None:
    timeline_path, assets_path = _write_inputs(
        tmp_path,
        timeline_data=_media_timeline(include_audio=False),
        source_resolution="1280x720",
    )

    argv = command.build_render_command(
        _request(timeline_path, assets_path),
        tmp_path,
    )

    filters = argv[argv.index("-filter_complex") + 1]
    assert (
        "[0:v]trim=start=0.000000:end=2.000000,setpts=PTS-STARTPTS,"
        "scale=1920:1080:force_original_aspect_ratio=decrease:out_color_matrix=bt709:out_range=tv,"
        "pad=1920:1080:(ow-iw)/2:(oh-ih)/2,"
        "fps=30,format=yuv420p,setparams=colorspace=bt709:color_primaries=bt709:color_trc=bt709:range=tv[v0]" in filters
    )
    # The film says what it is: BT.709, limited range, the whole description (an incomplete one is decoded as BT.709 by Chrome).
    colour = argv[argv.index("-colorspace"):argv.index("-colorspace") + 8]
    assert colour == ["-colorspace", "bt709", "-color_primaries", "bt709", "-color_trc", "bt709", "-color_range", "tv"]
    assert "[v0]concat=n=1:v=1:a=0[vout]" in filters
    assert "anullsrc" not in filters
    assert "[aout]" not in argv
    assert "-c:a" not in argv
    assert "-an" in argv
    assert argv[argv.index("-c:v") + 1] == "libx264"
    assert argv[argv.index("-preset") + 1] == "veryfast"
    assert argv[argv.index("-crf") + 1] == "20"


def test_protocol_render_returns_explicit_rendered_audio_result(
    tmp_path: Path,
) -> None:
    timeline_path, assets_path = _write_inputs(tmp_path)
    seen: dict[str, list[str]] = {}

    def fake_run(argv: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        # Only the ffmpeg call writes its output. Other subprocess calls (e.g. a
        # `git ... HEAD` provenance check) must not write "ffmpeg-video" to a
        # file named HEAD in the working directory; they get empty output.
        if Path(str(argv[0])).name.startswith("ffmpeg"):
            seen["argv"] = argv
            output = Path(argv[-1])
            output.parent.mkdir(parents=True, exist_ok=True)
            output.write_bytes(b"ffmpeg-video")
        return subprocess.CompletedProcess(argv, 0, stdout="", stderr="")

    probe = MediaProbe(
        width=1920,
        height=1080,
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
        format_name="mov,mp4",
        duration_rational=(2, 1),
        video_stream_present=True,
        audio_stream_present=True,
    )
    with (
        mock.patch.object(ffmpeg.subprocess, "run", side_effect=fake_run),
        mock.patch.object(ffmpeg, "ffprobe_metadata_strict", return_value=probe),
        mock.patch.object(ffmpeg, "validate_render_result") as validate,
        mock.patch.object(
            ffmpeg.remotion_backend,
            "_effective_registry_state",
            return_value={"hash": "registry"},
        ),
        mock.patch.object(
            ffmpeg.remotion_backend._shared,
            "_active_pack_order_for_provenance",
            return_value=[],
        ),
    ):
        result = ffmpeg._protocol_render(
            _request(timeline_path, assets_path),
            workspace=tmp_path,
        )

    assert isinstance(result, RenderResult)
    assert result.video.path == "outputs/result.mp4"
    assert result.video.audio is AudioOwnership.RENDERED
    assert result.audio_ownership is AudioOwnership.RENDERED
    assert result.video.profile.audio_sample_rate == 48000
    assert result.video.duration_frames == 60
    assert result.backend_fragments[ffmpeg.BACKEND_ID]["renderer"] == "ffmpeg"
    assert seen["argv"][-1] == str(tmp_path / "outputs" / "result.mp4")
    validate.assert_called_once()


def test_raw_render_adapter_writes_render_result_json(tmp_path: Path) -> None:
    timeline_path, assets_path = _write_inputs(tmp_path)
    request_path = tmp_path / "request.json"
    result_path = tmp_path / "result.json"
    request_path.write_text(
        json.dumps(_request(timeline_path, assets_path).to_dict()),
        encoding="utf-8",
    )
    output_path = tmp_path / "outputs" / "result.mp4"
    output_path.parent.mkdir(parents=True)
    output_path.write_bytes(b"video")
    result = RenderResult(
        schema_version=SCHEMA_VERSION,
        video=VideoArtifact.from_file(
            path=output_path,
            workspace_root=tmp_path,
            profile=_profile(),
            duration_frames=60,
            audio=AudioOwnership.RENDERED,
        ),
        audio_ownership=AudioOwnership.RENDERED,
        backend_fragments={ffmpeg.BACKEND_ID: {"renderer": "ffmpeg"}},
    )

    with mock.patch.object(ffmpeg, "_protocol_render", return_value=result):
        assert ffmpeg.main(
            [
                "render",
                "--request",
                str(request_path),
                "--result",
                str(result_path),
            ]
        ) == 0

    parsed = RenderResult.from_dict(
        json.loads(result_path.read_text(encoding="utf-8"))
    )
    assert parsed.video.path == "outputs/result.mp4"
    assert parsed.audio_ownership is AudioOwnership.RENDERED


def test_facade_selector_ffmpeg_delegates_to_backend_seam(tmp_path: Path) -> None:
    timeline_path, assets_path = _write_inputs(tmp_path)
    out_path = tmp_path / "render" / "hype.mp4"
    sentinel = tmp_path / "sentinel.mp4"
    fake_service = mock.Mock()
    fake_service.render.return_value = sentinel

    with mock.patch.object(facade, "_default_service", return_value=fake_service):
        output = facade.render(
            timeline_path,
            assets_path,
            out_path,
            selector="rendering.ffmpeg",
        )

    assert output == sentinel
    fake_service.render.assert_called_once()
    kwargs = fake_service.render.call_args.kwargs
    assert kwargs["selector"] == "rendering.ffmpeg"


def test_facade_selector_remotion_keeps_explicit_renderer_policy(tmp_path: Path) -> None:
    timeline_path, assets_path = _write_inputs(tmp_path)
    out_path = tmp_path / "render" / "hype.mp4"
    sentinel = tmp_path / "sentinel.mp4"
    fake_service = mock.Mock()
    fake_service.render.return_value = sentinel

    # The legacy nominal-remotion auto-FFmpeg policy lives inside the
    # service now; the facade must pass the legacy selector through so the
    # service can apply it.
    with mock.patch.object(facade, "_default_service", return_value=fake_service):
        output = facade.render(
            timeline_path,
            assets_path,
            out_path,
            selector="rendering.remotion",
        )

    assert output == sentinel
    kwargs = fake_service.render.call_args.kwargs
    assert kwargs["selector"] == "rendering.remotion"


def test_audio_reactive_compatibility_path_is_same_module() -> None:
    assert legacy_audio_reactive is audio_reactive_colour
