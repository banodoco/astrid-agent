#!/usr/bin/env python3
"""Three.js timeline renderer and raw rendering-protocol v1 command adapter.

``rendering.threejs`` is a thin backend that renders complete Astrid
timelines through the ``ThreeTimelineComposition`` in the Remotion project
(three.js canvas, deterministic frame clock, texture text planes).  It has
its OWN identity and provenance (``engine="threejs"``, never claims
``rendering.remotion``) while reusing the Remotion backend's execution
helper and the shared Remotion render lock.

The public ``support``/``_protocol_render`` functions are the protocol-v1
surface used by the generic renderer transport: the command-line entry
point reads one request file and writes exactly one result or structured
error file, mirroring the Remotion and HyperFrames backends.
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import sys
from contextlib import ExitStack
from dataclasses import dataclass
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Any, Mapping, Sequence

# Raw renderer commands are deliberately executable without an installed
# Astrid wheel.  The command transport sanitizes PYTHONPATH, so direct script
# execution must make the owning checkout importable before SDK imports.
if __package__ in {None, ""}:
    _CHECKOUT_ROOT = Path(__file__).resolve().parents[6]
    if str(_CHECKOUT_ROOT) not in sys.path:
        sys.path.insert(0, str(_CHECKOUT_ROOT))

from astrid.core import timeline
from astrid.pack_api import write_json_atomic
from astrid.core.rendering.artifacts import validate_render_result
from astrid.core.rendering.assets import AssetMaterializer
from astrid.pack_api import SCHEMA_VERSION
from astrid.core.rendering.contracts import AudioOwnership, RenderRequest, RenderResult, SupportReport, VideoArtifact
from astrid.core.rendering.errors import (
    RendererException,
    make_renderer_error,
    raise_unsupported_error,
)
from astrid.core.rendering.remotion_runtime import resolve_remotion_project_dir
from astrid.core.rendering.service import RenderService
from astrid.packs.rendering.shared.rendering_backend import (
    _alpha_output_name,
    _canonical_profile,
    _duration_frames,
    _input_path,
    _load_registry_mapping,
    _parse_min_free_gb,
    _profile_mismatches,
    _reject_unknown_config,
    _remotion_mux_profile,
    _render_provenance_payload,
    _resolved_theme_for_render,
    _review_output_profile,
    _serialize_timeline,
    _timeline_alpha,
)
from astrid.packs.rendering.rendering.renderers.remotion import run as remotion_backend

# Reuse seam: only the side-effect-free execution helper. The shared
# Remotion render lock is acquired inside ``_execute_remotion``; this backend
# never adds a second lock or capture stack.
_execute_remotion = remotion_backend._execute_remotion


BACKEND_ID = "rendering.threejs"
BACKEND_VERSION = "1.0.0"
THREE_COMPOSITION_ID = "ThreeTimelineComposition"
THREE_VERSION = "0.185.1"

# Own-namespace config keys honored by support (render v1 takes no config).
_CONFIG_KEYS = frozenset({"theme_path", "min_free_gb"})

# The exact field set the ThreeTimelineComposition maps (composition contract):
# text.content/fontSize/color/align/bold and
# params.anchor/offsetX/offsetY/textShadow/maxWidth/weight.
_TEXT_TEXT_KEYS = frozenset({"content", "fontSize", "color", "align", "bold"})
_TEXT_PARAM_KEYS = frozenset({"anchor", "offsetX", "offsetY", "textShadow", "maxWidth", "weight"})
_LIVE_SCENE_CLIP_TYPE = "haus.astrid.liveScene"
_SUPPORTED_CLIP_TYPES = frozenset({"text", "media", _LIVE_SCENE_CLIP_TYPE})


@dataclass(frozen=True)
class _ThreeSettings:
    project_dir: Path
    theme_path: Path | None
    min_free_gb: float | None


# ---------------------------------------------------------------------------
# Pure timeline eligibility helpers
# ---------------------------------------------------------------------------


def _canvas(timeline: Mapping[str, Any], *, theme: Mapping[str, Any] | None = None) -> tuple[int, int, int] | None:
    from astrid.core.rendering.profile import resolve_render_profile

    try:
        profile = resolve_render_profile(timeline, theme=theme)
    except (TypeError, ValueError):
        return None
    numerator, denominator = profile.fps_rational
    if denominator != 1:
        return None
    return profile.width, profile.height, numerator


def _effective_gain(clip: Mapping[str, Any], tracks: Sequence[Any]) -> float:
    """Exact timeline gain for a text clip, 0..1.

    Text clips declare no audio by default, so a missing volume is silent
    (0.0), unlike media clips.  An explicit clip/track volume or an unmuted
    track with volume declares audible content, which the visual-only
    composition would silently drop.
    """
    track_id = clip.get("track")
    track = next(
        (t for t in tracks if isinstance(t, dict) and t.get("id") == track_id),
        None,
    )
    if isinstance(track, dict) and track.get("muted") is True:
        return 0.0
    track_volume = track.get("volume") if isinstance(track, dict) else None
    track_gain = float(track_volume) if track_volume is not None else 0.0
    clip_volume = clip.get("volume")
    clip_gain = float(clip_volume) if clip_volume is not None else 0.0
    return max(0.0, min(1.0, max(track_gain, clip_gain)))


def _support_reasons(
    timeline_data: Mapping[str, Any],
    registry: Mapping[str, Any] | None = None,
    *,
    theme: Mapping[str, Any] | None = None,
) -> list[str]:
    """Return stable, clip-specific reasons this serialized timeline is
    unsupported by the Three.js composition (text clips only, visual-only,
    exact text field set, background/empty accepted)."""
    reasons: list[str] = []
    tracks = timeline_data.get("tracks") or []
    clips = timeline_data.get("clips") or []
    audio_tracks = [
        track.get("id")
        for track in tracks
        if isinstance(track, dict) and track.get("kind") == "audio"
    ]
    for index, clip in enumerate(clips):
        if not isinstance(clip, dict):
            reasons.append(f"clip[{index}] is not an object")
            continue
        clip_type = clip.get("clipType", "media")
        if clip_type not in _SUPPORTED_CLIP_TYPES:
            reasons.append(
                f"clip[{index}] clipType {clip_type!r} is not supported (text or prepared live-scene clips only)"
            )
        is_audio = clip.get("track") in audio_tracks
        if is_audio and clip_type != "media":
            reasons.append(f"clip[{index}] audio tracks require ordinary media clips")
        if clip_type == "media" and not is_audio:
            reasons.append(f"clip[{index}] clipType 'media' is not supported on visual tracks")
        if clip.get("effects"):
            reasons.append(f"clip[{index}] effects are not supported in v1")
        if clip.get("transition"):
            reasons.append(f"clip[{index}] transitions are not supported in v1")
        if clip.get("animation"):
            reasons.append(f"clip[{index}] animation is not supported in v1")
        if clip.get("opacity") not in (None, 1):
            reasons.append(f"clip[{index}] opacity != 1 is not supported in v1")
        if not is_audio and _effective_gain(clip, tracks) > 0:
            reasons.append(
                f"clip[{index}] carries audio; the Three.js renderer is "
                "visual-only in v1 (set clip/track volume to 0)"
            )
        if clip_type == _LIVE_SCENE_CLIP_TYPE:
            package = (clip.get("app") or {}).get("liveScene")
            try:
                from astrid.packs.rendering.shared.live_scenes.package import validate_package
                validate_package(package)
                # Use the same source bounds/rate algebra as window slicing.
                RenderService._clip_end(
                    clip, clip_start=RenderService._timeline_number(clip.get("at", 0), "clip.at")
                )
            except (ValueError, TypeError) as exc:
                reasons.append(f"clip[{index}] invalid live scene: {exc}")
            continue
        if is_audio and clip_type == "media":
            assets = (registry or {}).get("assets", {})
            entry = assets.get(clip.get("asset")) if isinstance(assets, Mapping) else None
            if not isinstance(entry, Mapping) or not (entry.get("object_id") or entry.get("media_id")):
                reasons.append(f"clip[{index}] audio asset is missing from the registry")
            try:
                RenderService._clip_end(
                    clip, clip_start=RenderService._timeline_number(clip.get("at", 0), "clip.at")
                )
            except (ValueError, TypeError) as exc:
                reasons.append(f"clip[{index}] invalid audio timing: {exc}")
            continue
        text_field = clip.get("text")
        if text_field is not None and not isinstance(text_field, dict):
            reasons.append(f"clip[{index}] text must be an object")
        elif isinstance(text_field, dict):
            unknown_text = sorted(set(text_field) - _TEXT_TEXT_KEYS)
            if unknown_text:
                reasons.append(f"clip[{index}] unsupported text fields: {unknown_text}")
        params = clip.get("params")
        if params is not None and not isinstance(params, dict):
            reasons.append(f"clip[{index}] params must be an object")
        elif isinstance(params, dict):
            unknown_params = sorted(set(params) - _TEXT_PARAM_KEYS)
            if unknown_params:
                reasons.append(f"clip[{index}] unsupported text params: {unknown_params}")
    if _canvas(timeline_data, theme=theme) is None:
        reasons.append("canvas width/height/fps must be positive integers")
    return reasons


def _window_timeline(timeline_data: dict[str, Any], request: RenderRequest, *, theme: Mapping[str, Any] | None = None) -> dict[str, Any]:
    """Materialize only the requested frames with the shared timeline algebra."""
    window = request.window
    if window is None:
        return timeline_data
    canvas = _canvas(timeline_data, theme=theme)
    if canvas is None or window.fps_rational != (canvas[2], 1):
        raise ValueError("frame window FPS must match the authored canvas FPS")
    if window.source_range is not None or window.speed not in (None, 1):
        raise ValueError("frame window source_range/speed resampling is not supported; use clip from/to/speed")
    return RenderService._window_timeline(timeline_data, window)


# ---------------------------------------------------------------------------
# Environment / project preflight
# ---------------------------------------------------------------------------


def _threejs_project_reasons(project_dir: Path) -> list[str]:
    reasons: list[str] = []
    if not project_dir.exists():
        reasons.append(f"Remotion project directory not found: {project_dir}")
        return reasons
    package_json = project_dir / "package.json"
    if not package_json.exists():
        reasons.append(f"Remotion project is missing package.json: {package_json}")
    # Node_modules check is environment-dependent; planning should succeed
    # even when the JS environment is not installed. Heavy render tests guard
    # Keep support lenient here; heavy render tests guard the environment.
    return reasons


def _binaries_reasons() -> list[str]:
    return [
        f"required binary is unavailable: {binary}"
        for binary in ("node", "npx", "ffprobe")
        if shutil.which(binary) is None
    ]


# ---------------------------------------------------------------------------
# Own-namespace settings (never reads the rendering.remotion namespace)
# ---------------------------------------------------------------------------


def _settings_from_request(request: RenderRequest, workspace: Path) -> _ThreeSettings:
    config = dict(request.backend_config.get(BACKEND_ID, {}))
    _reject_unknown_config(config, _CONFIG_KEYS, BACKEND_ID)

    project_dir = resolve_remotion_project_dir()

    theme_value = config.get("theme_path")
    if theme_value is None:
        theme_path = None
    elif isinstance(theme_value, (str, os.PathLike)):
        theme_path = _input_path(os.fspath(theme_value), workspace)
    else:
        raise TypeError("theme_path must be a path string or null")

    min_free_gb = _parse_min_free_gb(config.get("min_free_gb"))

    return _ThreeSettings(
        project_dir=project_dir,
        theme_path=theme_path,
        min_free_gb=min_free_gb,
    )


def _default_settings() -> _ThreeSettings:
    return _ThreeSettings(
        project_dir=resolve_remotion_project_dir(),
        theme_path=None,
        min_free_gb=None,
    )


# ---------------------------------------------------------------------------
# Protocol surface: support + render
# ---------------------------------------------------------------------------


def support(request: RenderRequest, *, workspace: Path) -> SupportReport:
    """Return request-specific evidence for what the Three.js renderer can do."""

    reasons: list[str] = []
    features: dict[str, bool | str] = {
        "webgl": True,
        "textured_text_planes": True,
        "background_only": True,
        "deterministic_frame_clock": True,
        "capture_host": "remotion",
        "media_textures": False,
        "effects": False,
        "transitions": False,
        "full_timeline": True,
        "windows": True,
        "alpha_output": True,
    }

    try:
        settings = _settings_from_request(request, workspace)
    except (TypeError, ValueError) as exc:
        return SupportReport(
            schema_version=SCHEMA_VERSION,
            supported=False,
            reasons=[str(exc)],
            features=features,
            alternatives=[],
            backend=BACKEND_ID,
            backend_version=BACKEND_VERSION,
        )

    timeline_path = _input_path(request.timeline_path, workspace)
    assets_path = (
        _input_path(request.assets_registry_path, workspace)
        if request.assets_registry_path is not None
        else None
    )
    timeline_data: dict[str, Any] | None = None
    assets_data: dict[str, Any] | None = None
    active_theme: Mapping[str, Any] | None = None
    try:
        timeline_data = _serialize_timeline(timeline_path)
        active_theme = _resolved_theme_for_render(timeline_path, settings.theme_path)
        timeline_data = _window_timeline(timeline_data, request, theme=active_theme)
    except (OSError, ValueError, TypeError, json.JSONDecodeError) as exc:
        reasons.append(f"timeline is not renderable: {exc}")
    try:
        assets_data = _load_registry_mapping(assets_path)
    except (OSError, ValueError, TypeError, json.JSONDecodeError) as exc:
        reasons.append(f"assets registry is not renderable: {exc}")
    if assets_path is not None and assets_data is not None:
        try:
            with AssetMaterializer(
                assets_path,
                materialized_objects=request.materialized_objects,
                materialized_root=request.materialized_root,
                allow_derived_files=request.materialized_root is not None,
            ):
                pass
        except (OSError, ValueError, TypeError) as exc:
            reasons.append(f"local assets are not renderable: {exc}")

    if timeline_data is not None:
        reasons.extend(_support_reasons(timeline_data, assets_data, theme=active_theme))

    if timeline_data is not None and assets_data is not None:
        # Ordinary audio uses TimelineComposition's existing audio tracks.
        # The live scene remains silent; the capture host always muxes audio.
        features["audio_ownership"] = AudioOwnership.RENDERED.value
        if request.audio is not None and request.audio is not AudioOwnership.RENDERED:
            reasons.append(
                f"audio={request.audio.value!r} is incompatible with the "
                "Three.js renderer's always-rendered audio output"
            )
        if request.profile is not None:
            try:
                canonical = _canonical_profile(timeline_path, assets_data, settings.theme_path)
            except (OSError, ValueError, TypeError, json.JSONDecodeError) as exc:
                reasons.append(f"canonical Three.js profile cannot be resolved: {exc}")
            else:
                # The capture host muxes ProRes 4444/yuva444p12le/MOV for
                # alpha-stamped layer timelines and the frozen
                # H.264/yuv420p/MP4 otherwise.
                alpha = _timeline_alpha(timeline_data)
                mismatches = _profile_mismatches(
                    request.profile, _remotion_mux_profile(canonical, alpha=alpha)
                )
                if mismatches:
                    reasons.append(
                        "requested profile is not produced by the Three.js "
                        "renderer: " + "; ".join(mismatches)
                    )

    reasons.extend(_threejs_project_reasons(settings.project_dir))
    reasons.extend(_binaries_reasons())

    return SupportReport(
        schema_version=SCHEMA_VERSION,
        supported=not reasons,
        reasons=reasons,
        features=features,
        alternatives=[],
        backend=BACKEND_ID,
        backend_version=BACKEND_VERSION,
    )


def _protocol_render(request: RenderRequest, *, workspace: Path) -> RenderResult:
    # Render re-validates everything itself; it never trusts a prior support
    # verdict.
    if request.audio not in (None, AudioOwnership.RENDERED):
        raise_unsupported_error(
            backend=BACKEND_ID,
            message="Three.js capture requires rendered audio ownership",
            recovery_command="request rendered audio ownership",
        )
    # v1 render takes no backend configuration (plan: reject non-empty
    # own-namespace backend_config); unknown keys fail loudly.
    own_config = request.backend_config.get(BACKEND_ID)
    if own_config:
        try:
            _settings_from_request(request, workspace)
        except (TypeError, ValueError) as exc:
            raise_unsupported_error(
                backend=BACKEND_ID,
                message="invalid rendering.threejs configuration",
                recovery_command="remove rendering.threejs backend_config for v1 renders",
                details={"config_error": str(exc)},
            )
        raise_unsupported_error(
            backend=BACKEND_ID,
            message="rendering.threejs v1 renders accept no backend_config",
            recovery_command="remove rendering.threejs backend_config and retry",
            details={"backend_config": dict(own_config)},
        )
    try:
        settings = _default_settings()
    except ValueError as exc:
        raise_unsupported_error(
            backend=BACKEND_ID,
            message="Three.js render environment is not available",
            recovery_command="configure the server-owned Remotion project and retry",
            details={"reasons": [str(exc)]},
        )

    timeline_path = _input_path(request.timeline_path, workspace)
    requested_assets_path = (
        _input_path(request.assets_registry_path, workspace)
        if request.assets_registry_path is not None
        else None
    )

    try:
        timeline_data = _serialize_timeline(timeline_path)
        active_theme = _resolved_theme_for_render(timeline_path, settings.theme_path)
        timeline_data = _window_timeline(timeline_data, request, theme=active_theme)
    except (OSError, ValueError, TypeError, json.JSONDecodeError) as exc:
        raise_unsupported_error(
            backend=BACKEND_ID,
            message="timeline is not renderable by the Three.js renderer",
            recovery_command="resolve the reported timeline problem and retry",
            details={"error": str(exc)},
        )
    reasons = _support_reasons(timeline_data, _load_registry_mapping(requested_assets_path), theme=active_theme)
    if reasons:
        raise_unsupported_error(
            backend=BACKEND_ID,
            message="Three.js renderer does not support this render request",
            recovery_command="resolve the reported support reasons and retry",
            details={"reasons": reasons},
        )
    project_reasons = _threejs_project_reasons(settings.project_dir)
    if project_reasons:
        raise_unsupported_error(
            backend=BACKEND_ID,
            message="Three.js render environment is not available",
            recovery_command="install remotion project dependencies and retry",
            details={"reasons": project_reasons},
        )
    binary_reasons = _binaries_reasons()
    if binary_reasons:
        raise_unsupported_error(
            backend=BACKEND_ID,
            message="required binary is unavailable",
            recovery_command="install the missing binary and retry",
            details={"reasons": binary_reasons},
        )

    outputs_dir = workspace / "outputs"
    outputs_dir.mkdir(parents=True, exist_ok=True)
    # The service hardcodes ``segment-NNNN.mp4`` (service.py:1362) but
    # Remotion rejects .mp4 output names for --codec=prores; remap the actual
    # artifact to .mov when the alpha stamp is present so the declared
    # artifact path points at the real ProRes file.
    alpha = _timeline_alpha(timeline_data)
    output_name = _alpha_output_name(request.output_name) if alpha else request.output_name
    output_path = outputs_dir / output_name

    with ExitStack() as lifecycle:
        if request.window is not None:
            window_tmp = lifecycle.enter_context(
                TemporaryDirectory(prefix=".threejs-window-", dir=str(workspace))
            )
            timeline_path = Path(window_tmp) / "timeline.json"
            write_json_atomic(timeline_path, timeline_data)
        if requested_assets_path is None:
            empty_assets_tmp = lifecycle.enter_context(
                TemporaryDirectory(prefix=".threejs-empty-assets-", dir=str(workspace))
            )
            assets_path = Path(empty_assets_tmp) / "assets.json"
            timeline.save_registry({"assets": {}}, assets_path)
        else:
            assets_path = requested_assets_path
        assets_data = _load_registry_mapping(assets_path)
        canonical = _canonical_profile(timeline_path, assets_data, settings.theme_path)
        # Alpha-stamped timelines render ProRes 4444/yuva444p12le in MOV
        # through the shared Remotion capture host; everything else keeps the
        # frozen H.264/yuv420p MP4 contract.  The declared profile must match
        # the probed artifact exactly (strict validation).
        review = (
            json.loads(request.metadata["review"])
            if "review" in request.metadata
            else None
        )
        render_scale = None
        profile_for_output = request.profile or canonical
        if request.profile is None and review is not None:
            profile_for_output, render_scale = _review_output_profile(canonical, alpha=alpha)
        declared_profile = (
            profile_for_output
            if render_scale is not None
            else _remotion_mux_profile(profile_for_output, alpha=alpha)
        )
        # ``useVideoConfig()`` remains authored-canvas sized under Remotion's
        # native ``--scale`` flag.  Pin the emitted dimensions in the review
        # props so the shared overlay can identify a low-resolution artifact.
        if isinstance(review, dict):
            review = {
                **review,
                "render_dimensions": {
                    "width": declared_profile.width,
                    "height": declared_profile.height,
                },
            }
        ownership = AudioOwnership.RENDERED
        private_tmp = lifecycle.enter_context(
            TemporaryDirectory(
                prefix=f".{output_name}.threejs-",
                dir=str(outputs_dir),
            )
        )
        staged_video = Path(private_tmp) / output_name
        details = _execute_remotion(
            timeline_path,
            assets_path,
            staged_video,
            provenance_out_path=output_path,
            project_dir=settings.project_dir,
            composition_id=THREE_COMPOSITION_ID,
            composition_clip_types=_SUPPORTED_CLIP_TYPES,
            theme_path=settings.theme_path,
            min_free_gb=settings.min_free_gb,
            review=review,
            render_scale=render_scale,
            materialized_root=request.materialized_root,
            staging_parent=workspace,
            materialized_objects=request.materialized_objects,
            encode_aac_at_stitch=True,
        )
        output_path.unlink(missing_ok=True)
        os.replace(staged_video, output_path)

    try:
        backend_provenance = _render_provenance_payload(
            project_dir=settings.project_dir,
            composition_id=THREE_COMPOSITION_ID,
            theme_path=settings.theme_path,
            active_theme=details.active_theme,
            registry_state=details.registry_state,
            stage_summary=details.stage_summary,
        )
        video = VideoArtifact.from_file(
            path=output_path,
            workspace_root=workspace,
            profile=declared_profile,
            duration_frames=_duration_frames(output_path, declared_profile),
            audio=ownership,
        )
        result = RenderResult(
            schema_version=SCHEMA_VERSION,
            video=video,
            audio_ownership=ownership,
            backend_fragments={
                BACKEND_ID: {
                    "renderer": "threejs",
                    "renderer_version": BACKEND_VERSION,
                    "three_version": THREE_VERSION,
                    "capture_host": "remotion",
                    "composition": THREE_COMPOSITION_ID,
                    **backend_provenance,
                    "review_scale": render_scale,
                    "source_window": request.window.to_dict() if request.window else None,
                    "live_scenes": [
                        {
                            "clip_id": clip.get("id"),
                            "revision": clip["app"]["liveScene"]["revision"],
                            "source": clip["app"]["liveScene"]["source"],
                            **{key: clip.get(key) for key in ("at", "from", "to", "hold", "speed")},
                        }
                        for clip in timeline_data.get("clips", [])
                        if clip.get("clipType") == _LIVE_SCENE_CLIP_TYPE
                    ],
                }
            },
            normalization=[],
            logs=[],
            metadata=request.metadata,
        )
        validate_render_result(
            result,
            expected_profile=declared_profile,
            workspace_root=workspace,
        )
        return result
    except BaseException:
        output_path.unlink(missing_ok=True)
        raise


# ---------------------------------------------------------------------------
# Raw protocol v1 command entrypoint
# ---------------------------------------------------------------------------


def _load_request(path: Path) -> RenderRequest:
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, Mapping):
        raise TypeError("render request must contain a JSON object")
    return RenderRequest.from_dict(payload).for_backend(BACKEND_ID)


def _write_failure(result_path: Path, exc: BaseException, *, kind: str) -> None:
    if isinstance(exc, RendererException):
        error_kind = exc.error.kind
        message = exc.error.message
        recovery = exc.error.recovery_command
        details = exc.error.details
    else:
        error_kind = kind
        message = str(exc) or type(exc).__name__
        recovery = None
        details = {"error_type": type(exc).__name__}
    error = make_renderer_error(
        error_kind,
        backend=BACKEND_ID,
        message=message,
        recovery_command=recovery,
        details=details,
    )
    write_json_atomic(result_path, error.to_dict())


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("verb", choices=("render", "support"))
    parser.add_argument("--request", type=Path, required=True)
    parser.add_argument("--result", type=Path, required=True)
    args = parser.parse_args(argv)

    try:
        request_path = args.request.resolve(strict=True)
        result_path = args.result.resolve()
        if request_path == result_path:
            raise ValueError("--request and --result must be different paths")
        request = _load_request(request_path)
    except (OSError, ValueError, TypeError, json.JSONDecodeError, RendererException) as exc:
        _write_failure(args.result.resolve(), exc, kind="protocol")
        return 0

    try:
        workspace = request_path.parent
        response: RenderResult | SupportReport
        if args.verb == "support":
            response = support(request, workspace=workspace)
        else:
            response = _protocol_render(request, workspace=workspace)
        write_json_atomic(result_path, response.to_dict())
    except RendererException as exc:
        _write_failure(result_path, exc, kind=exc.error.kind)
    except FileNotFoundError as exc:
        _write_failure(result_path, exc, kind="binary_missing")
    except (ValueError, TypeError, json.JSONDecodeError) as exc:
        _write_failure(result_path, exc, kind="protocol")
    except BaseException as exc:
        if isinstance(exc, (KeyboardInterrupt, SystemExit)):
            raise
        _write_failure(result_path, exc, kind="internal")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())


__all__ = [
    "BACKEND_ID",
    "BACKEND_VERSION",
    "THREE_COMPOSITION_ID",
    "THREE_VERSION",
    "main",
    "support",
]
