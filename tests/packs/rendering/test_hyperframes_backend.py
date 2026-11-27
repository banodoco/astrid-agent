"""HyperFrames third-party backend — end-to-end proof of the pluggable
renderer contract.

Locks the epic's core promise: a trusted pack can add a timeline render
backend (here: heygen-com/hyperframes, HTML→MP4) without editing core.
The pack lives at ``tests/fixtures/renderer_packs/hyperframes`` and is
discovered via an extra pack root.  The test exercises:

* discovery + inspect through the registry/CLI surface;
* honest support (text-only accepted; media/fade timelines rejected);
* a real render through the public service (Node 22+ and FFmpeg required);
* the artifact probe/validation and provenance sidecar.

The real-render case SKIPS when node/ffmpeg are absent or the pinned
hyperframes CLI cannot be installed (network), so CI stays honest without
pretending to render.
"""

from __future__ import annotations

import hashlib
import importlib.util
import json
import shutil
import subprocess
from pathlib import Path

import pytest

from tests.lanes import unavailable

from astrid.core.rendering.contracts import AudioOwnership, RenderRequest, SCHEMA_VERSION
from astrid.core.rendering.registry import load_default_registries
from astrid.core.rendering.service import RenderService
from astrid.sdk.rendering import support
from tests.helpers.remotion_runtime import configure_remotion_runtime, remotion_install_problems
from tests.packs.rendering._helpers import _execution_env, _source_video

ROOT = Path(__file__).resolve().parents[3]
PACK_ROOT = ROOT / "tests" / "fixtures" / "renderer_packs"
HYPERFRAMES_ROOT = PACK_ROOT / "hyperframes"
HYPERFRAMES_ID = "hyperframes.renderer"

def _load_pack_render_module():
    spec = importlib.util.spec_from_file_location("hyperframes_pack_render", HYPERFRAMES_ROOT / "render.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_hyperframes_preserves_actionable_child_diagnostics() -> None:
    render = _load_pack_render_module()
    text = "real failure\n" + "npm warn noisy\n" * 30 + "npm notice update\n"
    assert render._diagnostic_tail(text) == "real failure"


def test_hyperframes_enables_npm_foreground_script_diagnostics(tmp_path: Path, monkeypatch) -> None:
    render = _load_pack_render_module()
    monkeypatch.setattr(render.shutil, "which", lambda name: "/fake/" + name)
    monkeypatch.setattr(render, "find_node22", lambda: "/fake/node")

    def fake_run(command, **kwargs):
        assert command[0] == "npx"
        stderr = ""
        if kwargs["env"].get("npm_config_foreground_scripts") == "true":
            stderr = "onnxruntime-node postinstall failed: underlying detail"
        return subprocess.CompletedProcess(command, 1, stdout="", stderr=stderr)

    monkeypatch.setattr(render.subprocess, "run", fake_run)
    (tmp_path / "timeline.json").write_text(json.dumps({
        "theme_overrides": {"visual": {"canvas": {"width": 640, "height": 360, "fps": 24}}},
        "tracks": [{"id": "v1", "kind": "visual"}],
        "clips": [{"id": "title", "at": 0, "track": "v1", "clipType": "text", "hold": 1, "params": {"content": "test"}}],
    }), encoding="utf-8")
    request = {"schema_version": 1, "output_name": "output.mp4", "timeline_path": "timeline.json"}
    result_path = tmp_path / "result.json"
    assert render._run("render", request, tmp_path / "request.json", result_path) == 0
    result = json.loads(result_path.read_text(encoding="utf-8"))
    assert "onnxruntime-node postinstall failed: underlying detail" in result["details"]["stderr"]


def _require_hyperframes_environment() -> None:
    """Fail (these tests are in the ``integration`` lane) unless ffmpeg and a Node >= 22 are available.

    The pack finds its own Node 22 (render.find_node22: PATH, the runner tool cache, nvm), because the
    lane's PATH node is the pinned Node 20 that Remotion requires.
    """
    if shutil.which("ffmpeg") is None:
        unavailable("ffmpeg is unavailable")
    if _load_pack_render_module().find_node22() is None:
        unavailable("node >= 22 required for hyperframes (checked PATH, the runner tool cache and nvm)")


def _text_timeline(tmp_path: Path) -> Path:
    payload = {
        "theme": "banodoco-default",
        "theme_overrides": {
            "visual": {
                "canvas": {"width": 640, "height": 360, "fps": 24},
                "background": "#1a1a2e",
            }
        },
        "tracks": [
            {"id": "v1", "kind": "visual", "label": "Title"},
            {"id": "v2", "kind": "visual", "label": "Subtitle"},
        ],
        "clips": [
            {
                "id": "title",
                "at": 0.0,
                "track": "v1",
                "clipType": "text",
                "hold": 1.5,
                "params": {"content": "Hello HyperFrames", "fontSize": 64, "color": "#ffffff", "weight": 700},
            },
            {
                "id": "sub",
                "at": 1.0,
                "track": "v2",
                "clipType": "text",
                "hold": 1.0,
                "params": {"content": "rendered by Astrid", "fontSize": 28, "color": "#a0e0ff"},
            },
        ],
    }
    path = tmp_path / "timeline.json"
    path.write_text(json.dumps(payload), encoding="utf-8")
    return path


def test_hyperframes_pack_is_discovered_and_inspected() -> None:
    renderers, _planners, _finalizers = load_default_registries(
        extra_pack_roots=(str(PACK_ROOT),),
    )
    candidates = renderers.candidates(HYPERFRAMES_ID)
    assert len(candidates) == 1, [c.to_dict() for c in candidates]
    candidate = candidates[0]
    assert candidate.id == HYPERFRAMES_ID
    assert candidate.source_kind == "extra"
    assert candidate.manifest.command == ("python3", "render.py")
    assert "node" in candidate.manifest.required_binaries
    assert candidate.execution_eligible is True


def test_hyperframes_support_is_honest(tmp_path: Path) -> None:
    text = _text_timeline(tmp_path)
    report = support(
        HYPERFRAMES_ID,
        timeline_path=text,
        extra_pack_roots=(str(PACK_ROOT),),
    )
    assert report.supported is True

    media = tmp_path / "media.json"
    media.write_text(
        json.dumps(
            {
                "theme_overrides": {"visual": {"canvas": {"width": 640, "height": 360, "fps": 24}}},
                "tracks": [{"id": "v", "kind": "visual"}],
                "clips": [{"id": "m", "at": 0, "track": "v", "clipType": "media", "asset": "x"}],
            }
        ),
        encoding="utf-8",
    )
    # No registry -> the referenced asset cannot resolve; honest rejection.
    report = support(HYPERFRAMES_ID, timeline_path=media, extra_pack_roots=(str(PACK_ROOT),))
    assert report.supported is False
    assert any("media" in reason for reason in report.reasons)

    # Silent media WITH a resolvable registry asset -> supported.
    source = _source_video(tmp_path)
    registry = tmp_path / "media-assets.json"
    registry.write_text(
        json.dumps(
            {
                "assets": {
                    "src": {
                        "file": source.name,
                        "type": "video/mp4",
                        "duration": 1.0,
                        "resolution": "320x180",
                        "fps": 24,
                    }
                }
            }
        ),
        encoding="utf-8",
    )
    silent = tmp_path / "silent-media.json"
    silent.write_text(
        json.dumps(
            {
                "theme_overrides": {"visual": {"canvas": {"width": 640, "height": 360, "fps": 24}}},
                "tracks": [{"id": "v", "kind": "visual"}],
                "clips": [
                    {
                        "id": "m",
                        "at": 0,
                        "track": "v",
                        "clipType": "media",
                        "asset": "src",
                        "from": 0,
                        "to": 0.5,
                        "speed": 1,
                        "volume": 0,
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    report = support(
        HYPERFRAMES_ID,
        timeline_path=silent,
        assets_registry_path=registry,
        extra_pack_roots=(str(PACK_ROOT),),
    )
    assert report.supported is True, report.reasons

    # Audible media -> honest rejection (adapter is visual-only in v1).
    audible = tmp_path / "audible-media.json"
    audible.write_text(
        json.dumps(
            {
                "theme_overrides": {"visual": {"canvas": {"width": 640, "height": 360, "fps": 24}}},
                "tracks": [{"id": "v", "kind": "visual"}],
                "clips": [
                    {
                        "id": "m",
                        "at": 0,
                        "track": "v",
                        "clipType": "media",
                        "asset": "src",
                        "from": 0,
                        "to": 0.5,
                        "speed": 1,
                        "volume": 1,
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    report = support(
        HYPERFRAMES_ID,
        timeline_path=audible,
        assets_registry_path=registry,
        extra_pack_roots=(str(PACK_ROOT),),
    )
    assert report.supported is False
    assert any("audio" in reason for reason in report.reasons)

    fade = tmp_path / "fade.json"
    fade.write_text(
        json.dumps(
            {
                "theme_overrides": {"visual": {"canvas": {"width": 640, "height": 360, "fps": 24}}},
                "tracks": [{"id": "v", "kind": "visual"}],
                "clips": [{"id": "t", "at": 0, "track": "v", "clipType": "text", "hold": 1, "params": {"content": "x", "fadeIn": 0.3}}],
            }
        ),
        encoding="utf-8",
    )
    report = support(HYPERFRAMES_ID, timeline_path=fade, extra_pack_roots=(str(PACK_ROOT),))
    assert report.supported is False
    assert any("fadeIn" in reason for reason in report.reasons)


@pytest.mark.integration
@pytest.mark.timeout(600)
def test_hyperframes_real_render_through_public_service(tmp_path: Path) -> None:
    _require_hyperframes_environment()
    timeline = _text_timeline(tmp_path)
    output = tmp_path / "hyperframes.mp4"
    try:
        with _execution_env():
            published = RenderService(extra_pack_roots=(str(PACK_ROOT),)).render(
                RenderRequest(
                    schema_version=SCHEMA_VERSION,
                    timeline_path=str(timeline),
                    output_name=output.name,
                ),
                selector=HYPERFRAMES_ID,
                out_path=output,
            )
    except Exception as exc:  # noqa: BLE001 - network/CLI availability
        message = str(exc)
        if "npx" in message or "ENOTFOUND" in message or "network" in message:
            unavailable(f"hyperframes CLI unavailable: {message[:120]}")
        raise

    assert Path(published).is_file()
    assert Path(published).stat().st_size > 0
    sidecar = Path(f"{published}.provenance.json")
    assert sidecar.is_file()
    payload = json.loads(sidecar.read_text(encoding="utf-8"))
    assert payload["engine"] == HYPERFRAMES_ID
    assert payload["routing"]["resolved_backend"] == HYPERFRAMES_ID
    assert payload["audio_ownership"] == "none"
    assert payload["backend_fragments"][HYPERFRAMES_ID]["renderer"] == "hyperframes"
    probe = subprocess.run(
        [
            "ffprobe",
            "-v",
            "error",
            "-show_entries",
            "stream=codec_name,width,height",
            "-of",
            "csv",
            str(published),
        ],
        check=True,
        capture_output=True,
        text=True,
    ).stdout
    assert "h264" in probe
    assert "640" in probe and "360" in probe


@pytest.mark.integration
@pytest.mark.timeout(600)
def test_hyperframes_real_media_render_through_public_service(tmp_path: Path) -> None:
    """HyperFrames renders SILENT media clips via <video> source trimming.

    A media clip [0, 0.5s) of a 1s source renders as a 12-frame (0.5s) h264
    output; the engine's data-mediaStart/data-playback-rate trim the source
    window.  This locks the adapter's media capability end-to-end.
    """
    _require_hyperframes_environment()
    source = _source_video(tmp_path)
    assets = tmp_path / "media-assets.json"
    assets.write_text(
        json.dumps(
            {
                "assets": {
                    "src": {
                        "file": source.name,
                        "type": "video/mp4",
                        "duration": 1.0,
                        "resolution": "320x180",
                        "fps": 24,
                    }
                }
            }
        ),
        encoding="utf-8",
    )
    timeline = tmp_path / "media-timeline.json"
    timeline.write_text(
        json.dumps(
            {
                "theme_overrides": {
                    "visual": {"canvas": {"width": 320, "height": 180, "fps": 24}}
                },
                "tracks": [{"id": "v", "kind": "visual"}],
                "clips": [
                    {
                        "id": "m",
                        "at": 0,
                        "track": "v",
                        "clipType": "media",
                        "asset": "src",
                        "from": 0,
                        "to": 0.5,
                        "speed": 1,
                        "volume": 0,
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    output = tmp_path / "hyperframes-media.mp4"
    try:
        with _execution_env():
            published = RenderService(extra_pack_roots=(str(PACK_ROOT),)).render(
                RenderRequest(
                    schema_version=SCHEMA_VERSION,
                    timeline_path=str(timeline),
                    assets_registry_path=str(assets),
                    output_name=output.name,
                ),
                selector=HYPERFRAMES_ID,
                out_path=output,
            )
    except Exception as exc:  # noqa: BLE001 - network/CLI availability
        message = str(exc)
        if "npx" in message or "ENOTFOUND" in message or "network" in message:
            unavailable(f"hyperframes CLI unavailable: {message[:120]}")
        raise

    assert Path(published).is_file()
    assert Path(published).stat().st_size > 0
    probe = subprocess.run(
        [
            "ffprobe",
            "-v",
            "error",
            "-show_entries",
            "stream=codec_name,width,height,duration",
            "-of",
            "csv",
            str(published),
        ],
        check=True,
        capture_output=True,
        text=True,
    ).stdout
    assert "h264" in probe
    assert "320" in probe and "180" in probe
    duration = float(probe.strip().split(",")[-1])
    assert abs(duration - 0.5) < 0.1, probe
    sidecar = Path(f"{published}.provenance.json")
    assert sidecar.is_file()
    payload = json.loads(sidecar.read_text(encoding="utf-8"))
    assert payload["engine"] == HYPERFRAMES_ID
    assert payload["audio_ownership"] == "none"


def _combined_timeline(tmp_path: Path) -> Path:
    """Mixed timeline: text (HyperFrames), media with transition (Remotion),
    and a trailing SILENT media clip (HyperFrames again).  The planner tiles
    this into [0,12) + [32,44) -> hyperframes.renderer and [12,32) ->
    rendering.remotion, proving both engines share one render."""
    payload = {
        "theme": "banodoco-default",
        "theme_overrides": {
            "visual": {
                "canvas": {"width": 320, "height": 180, "fps": 24},
                "background": "#1a1a2e",
            }
        },
        "tracks": [
            {"id": "v1", "kind": "visual", "label": "Title"},
            {"id": "v2", "kind": "visual", "label": "Media"},
        ],
        "clips": [
            {
                "id": "title",
                "at": 0.0,
                "track": "v1",
                "clipType": "text",
                "hold": 0.5,
                "text": {"content": "Astrid + HyperFrames"},
            },
            {
                "id": "clip1",
                "at": 0.5,
                "track": "v2",
                "clipType": "media",
                "asset": "src",
                "from": 0,
                "to": 0.5,
                "speed": 1,
                "volume": 0,
                "transition": {"id": "cross-fade", "duration": 0.15},
            },
            {
                "id": "clip2",
                "at": 0.85,
                "track": "v2",
                "clipType": "media",
                "asset": "src",
                "from": 0.5,
                "to": 1.0,
                "speed": 1,
                "volume": 0,
            },
            {
                "id": "clip3",
                "at": 1.35,
                "track": "v2",
                "clipType": "media",
                "asset": "src",
                "from": 0,
                "to": 0.5,
                "speed": 1,
                "volume": 0,
            },
        ],
    }
    path = tmp_path / "combined-timeline.json"
    path.write_text(json.dumps(payload), encoding="utf-8")
    return path


@pytest.mark.integration
@pytest.mark.timeout(900)
def test_hyperframes_remotion_combined_render(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """hyperframes.planner collates HyperFrames + Remotion via FFmpeg concat.

    The mixed timeline's text window and trailing silent-media window render
    through hyperframes.renderer (the engine maps media clips to <video>
    elements with source trimming), the media-with-transition window through
    rendering.remotion; the pinned rendering.ffmpeg-finalizer concatenates
    all three and synthesizes audio.  This locks the planner's eligibility
    tiling, the qualified-planner-id selector, and the frame-count-authority
    duration validation (Remotion's audio-padded container must not reject a
    correct segment).
    """
    _require_hyperframes_environment()
    source = _source_video(tmp_path)
    assets = tmp_path / "real-assets.json"
    assets.write_text(
        json.dumps(
            {
                "assets": {
                    "src": {
                        "media_id": "source-object",
                        "content_sha256": hashlib.sha256(source.read_bytes()).hexdigest(),
                        "type": "video/mp4",
                        "duration": 1.0,
                        "resolution": "320x180",
                        "fps": 24,
                    }
                }
            }
        ),
        encoding="utf-8",
    )
    timeline = _combined_timeline(tmp_path)
    output = tmp_path / "combined.mp4"
    problems = remotion_install_problems()
    if problems:
        pytest.fail("the combined render needs the provisioned Remotion install: " + "; ".join(problems), pytrace=False)
    # RenderService exposes the extra pack root to its renderer children as ASTRID_PACKS_PATH, and Remotion's
    monkeypatch.setenv("ASTRID_PACKS_PATH", str(PACK_ROOT.resolve()))
    try:
        with _execution_env():
            configure_remotion_runtime(monkeypatch)
            published = RenderService(extra_pack_roots=(str(PACK_ROOT),)).render(
                RenderRequest(
                    schema_version=SCHEMA_VERSION,
                    timeline_path=str(timeline),
                    assets_registry_path=str(assets),
                    output_name=output.name,
                    audio=AudioOwnership.RENDERED,
                    backend_config={
                        "hyperframes.renderer": {},
                        "rendering.remotion": {},
                    },
                    materialized_root=str(tmp_path),
                    materialized_objects={"source-object": str(source)},
                ),
                selector="hyperframes.planner",
                out_path=output,
            )
    except Exception as exc:  # noqa: BLE001 - network/CLI availability
        message = str(exc)
        if "npx" in message or "ENOTFOUND" in message or "network" in message:
            unavailable(f"hyperframes CLI unavailable: {message[:120]}")
        raise

    assert Path(published).is_file()
    assert Path(published).stat().st_size > 0
    probe = subprocess.run(
        [
            "ffprobe",
            "-v",
            "error",
            "-show_entries",
            "stream=codec_name,width,height",
            "-of",
            "csv",
            str(published),
        ],
        check=True,
        capture_output=True,
        text=True,
    ).stdout
    assert "h264" in probe
    assert "aac" in probe
    assert "320" in probe and "180" in probe

    sidecar = Path(f"{published}.provenance.json")
    assert sidecar.is_file()
    payload = json.loads(sidecar.read_text(encoding="utf-8"))
    assert payload["routing"]["resolved_policy"]["planner"] == "hyperframes.planner"
    assert payload["routing"]["resolved_policy"]["finalizer"] == "rendering.ffmpeg-finalizer"
    segments = payload["segments_v2"]
    windows = [(s["renderer"]["id"], s["window"]["start_frame"], s["window"]["end_frame"]) for s in segments]
    # text -> HyperFrames; media-with-transition -> Remotion; silent media -> HyperFrames
    assert windows == [
        ("hyperframes.renderer", 0, 12),
        ("rendering.remotion", 12, 32),
        ("hyperframes.renderer", 32, 44),
    ], windows
