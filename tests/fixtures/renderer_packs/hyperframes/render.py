#!/usr/bin/env python3
"""HyperFrames render backend (third-party pack, pure stdlib).

Usage: python3 render.py render|support --request <abs.json> --result <abs.json>

Adapts a BOUNDED subset of the Astrid timeline to a HyperFrames composition:

* canvas (theme_overrides.visual.canvas) -> root ``#root`` with
  ``data-composition-id``, ``data-start="0"``, ``data-duration`` (compile-time
  ``max(at + duration)``), ``data-width``, ``data-height``, ``data-fps``;
* each ``text`` clip -> a direct child ``<section class="clip">`` with
  ``data-start`` / ``data-duration`` (the HyperFrames seek window), an
  inner ``<p>`` carrying the escaped text, CSS ``z-index`` from the Astrid
  visual track paint order, and a bounded CSS subset (fontSize, color,
  textAlign, fontWeight, textShadow, maxWidth, offsets/anchors);
* each SILENT ``media`` clip -> a ``<video>`` element that IS the clip
  (HyperFrames collects ``video[data-start]`` as a media clip): ``data-start``
  / ``data-duration`` (composition window), ``data-mediaStart`` (source
  offset = clip.from) and ``data-playback-rate`` (= clip.speed) so the engine
  seeks the exact source window.  The asset file is copied into the workspace
  ``assets/`` dir and referenced relative to the composition.  Audible media
  (effective gain > 0) is rejected: the adapter emits visual-only MP4s
  (``audio_ownership: none``) and would silently drop the source audio.
* theme/canvas background -> a full-bleed child of the root (never the root
  background, which capture drops).

Deliberately rejected with an explicit support reason: audible media, effect
clips, audio clips/tracks, fades/transitions, opacity != 1, and non-integer
FPS.  The adapter never tweens ``.clip`` visibility — HyperFrames owns the
seek window; visibility-only is the v1 contract.

Renders via ``npx --yes hyperframes@<pin> render <workdir> -c index.html
-o <output_name> --fps <int> --no-best-effort --strict`` and writes the
frozen RenderResult (visual-only, ``audio_ownership: none``) with a
backend fragment.
"""

from __future__ import annotations

import argparse
import hashlib
import html
import json
import os
import re
import shutil
import subprocess
from pathlib import Path

BACKEND_ID = "hyperframes.renderer"
_BACKEND_VERSION = "1.0.0"
_HYPERFRAMES_PIN = "0.7.107"
_MIN_NODE_MAJOR = 22


def _diagnostic_tail(value: str) -> str:
    lines = [line for line in value.splitlines() if not re.match(r"^\s*npm\s+(?:warn|notice)\b", line, re.IGNORECASE)]
    return "\n".join(lines[-40:]).encode("utf-8")[-8192:].decode("utf-8", errors="ignore")


def find_node22() -> str | None:
    """Return a ``node`` executable >= 22 (HyperFrames' requirement), or None.

    The lane's PATH node is the repo's pinned Node (``.node-version``, which
    Remotion requires), so HyperFrames looks for its own: every ``node`` on
    PATH, then the GitHub runner tool cache (a second ``actions/setup-node``
    step leaves Node 22 there), then nvm versions.
    """
    candidates: list[str] = []
    for entry in os.environ.get("PATH", "").split(os.pathsep):
        if entry and (Path(entry) / "node").is_file():
            candidates.append(str(Path(entry) / "node"))
    roots = [os.environ.get("RUNNER_TOOL_CACHE"), "/opt/hostedtoolcache"]
    for root in roots:
        if root:
            candidates.extend(
                str(path) for path in sorted(Path(root).glob("node/*/*/bin/node"), reverse=True)
            )
    nvm = Path.home() / ".nvm" / "versions" / "node"
    if nvm.is_dir():
        candidates.extend(
            str(path / "bin" / "node") for path in sorted(nvm.iterdir(), reverse=True)
        )
    for node in dict.fromkeys(candidates):
        try:
            version = subprocess.run(
                [node, "--version"], capture_output=True, text=True, check=False
            ).stdout.strip()
            if int(version.lstrip("v").split(".")[0]) >= _MIN_NODE_MAJOR:
                return node
        except (OSError, ValueError, IndexError):
            continue
    return None

_PROFILE = {
    "width": 640,
    "height": 360,
    "fps_rational": [24, 1],
    "time_base": [1, 12288],
    "container": "mp4",
    "video_codec": "h264",
    "video_profile": None,
    "video_level": None,
    "pixel_format": "yuv420p",
    "audio_codec": None,
    "audio_sample_rate": None,
    "audio_channel_layout": None,
    "duration_tolerance": 1,
}

_SUPPORTED_TEXT_KEYS = {
    "content",
    "fontSize",
    "color",
    "align",
    "weight",
    "textShadow",
    "maxWidth",
    "anchor",
    "offsetX",
    "offsetY",
}

_CSS_COLORS = {
    "black": "#000000",
    "white": "#ffffff",
    "red": "#ff0000",
    "green": "#008000",
    "blue": "#0000ff",
    "yellow": "#ffff00",
}


def _write(path: Path, payload: dict) -> None:
    path.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")


def _error(
    result_path: Path,
    kind: str,
    message: str,
    recovery_command: str | None = None,
    details: dict | None = None,
) -> None:
    _write(
        result_path,
        {
            "schema_version": 1,
            "kind": kind,
            "backend": BACKEND_ID,
            "message": message,
            "recovery_command": recovery_command,
            "details": details or {},
        },
    )


def _canvas(timeline: dict) -> tuple[int, int, int] | None:
    overrides = timeline.get("theme_overrides") or {}
    visual = overrides.get("visual") or {}
    canvas = visual.get("canvas") or {}
    width = canvas.get("width")
    height = canvas.get("height")
    fps = canvas.get("fps")
    if not all(isinstance(v, int) and v > 0 for v in (width, height, fps)):
        return None
    return int(width), int(height), int(fps)


def _clip_duration(clip: dict) -> float:
    if clip.get("clipType") == "media":
        to = clip.get("to")
        frm = clip.get("from")
        if isinstance(to, (int, float)) and isinstance(frm, (int, float)):
            source_span = float(to) - float(frm)
            if source_span > 0:
                return source_span / _clip_speed(clip)
        return 1.0
    hold = clip.get("hold")
    if isinstance(hold, (int, float)) and hold > 0:
        return float(hold)
    return 1.0


def _clip_speed(clip: dict) -> float:
    speed = clip.get("speed")
    if isinstance(speed, (int, float)) and speed > 0:
        return float(speed)
    return 1.0


def _effective_gain(clip: dict, tracks: list) -> float:
    """Exact timeline gain (track muted/volume x clip volume), 0..1."""
    track_id = clip.get("track")
    track = next(
        (t for t in tracks if isinstance(t, dict) and t.get("id") == track_id),
        None,
    )
    if isinstance(track, dict) and track.get("muted") is True:
        return 0.0
    if isinstance(track, dict):
        track_volume = track.get("volume")
        track_gain = 1.0 if track_volume is None else float(track_volume)
    else:
        track_gain = 1.0
    clip_volume = clip.get("volume")
    clip_gain = 1.0 if clip_volume is None else float(clip_volume)
    return max(0.0, min(1.0, track_gain * clip_gain))


def _materialized_source(request: dict, entry: dict) -> Path | None:
    """Resolve and verify one host-provided managed-media handoff."""

    media_id = entry.get("media_id")
    digest = entry.get("content_sha256")
    root_value = request.get("materialized_root")
    objects = request.get("materialized_objects")
    if not isinstance(media_id, str) or not isinstance(digest, str):
        return None
    if not isinstance(root_value, str) or not isinstance(objects, dict):
        return None
    source_value = objects.get(media_id)
    if not isinstance(source_value, str):
        return None
    root = Path(root_value).resolve()
    source = Path(source_value).resolve()
    try:
        source.relative_to(root)
    except ValueError:
        return None
    expected = digest.removeprefix("sha256:")
    if not source.is_file() or len(expected) != 64:
        return None
    if hashlib.sha256(source.read_bytes()).hexdigest() != expected:
        return None
    return source


def _support_reasons(
    timeline: dict,
    registry: dict | None = None,
    *,
    request: dict | None = None,
) -> list[str]:
    """Return reasons this timeline is unsupported.

    * ``registry``: parsed assets registry (``{"assets": {id: entry}}``) or
      ``None``.  Media clips require a registry whose ``assets`` map resolves
      the referenced asset id to a file, and the clip's effective gain must
      be 0 (the adapter emits visual-only MP4s and would silently drop any
      source audio otherwise).
    """
    reasons: list[str] = []
    tracks = timeline.get("tracks") or []
    clips = timeline.get("clips") or []
    audio_tracks = [
        track.get("id")
        for track in tracks
        if isinstance(track, dict) and track.get("kind") == "audio"
    ]
    if audio_tracks:
        reasons.append(
            f"audio tracks are not supported by the HyperFrames adapter: {audio_tracks}"
        )
    asset_entries = {}
    if isinstance(registry, dict) and isinstance(registry.get("assets"), dict):
        asset_entries = registry["assets"]
    for index, clip in enumerate(clips):
        if not isinstance(clip, dict):
            reasons.append(f"clip[{index}] is not an object")
            continue
        clip_type = clip.get("clipType", "media")
        if clip_type not in ("text", "media"):
            reasons.append(
                f"clip[{index}] clipType {clip_type!r} is not supported "
                "(text and silent-media only)"
            )
        if clip.get("track") in audio_tracks:
            reasons.append(f"clip[{index}] sits on an audio track")
        if clip.get("effects"):
            reasons.append(f"clip[{index}] effects are not supported in v1")
        if clip.get("transition"):
            reasons.append(f"clip[{index}] transitions are not supported in v1")
        if clip.get("opacity") not in (None, 1):
            reasons.append(f"clip[{index}] opacity != 1 is not supported in v1")
        if clip_type == "media":
            asset_id = clip.get("asset")
            if not isinstance(asset_id, str) or not asset_id:
                reasons.append(f"clip[{index}] media clip needs an asset id")
            entry = asset_entries.get(asset_id) if isinstance(asset_id, str) else None
            has_file = isinstance(entry, dict) and isinstance(entry.get("file"), str)
            has_handoff = (
                isinstance(entry, dict)
                and request is not None
                and _materialized_source(request, entry) is not None
            )
            if not has_file and not has_handoff:
                reasons.append(
                    f"clip[{index}] asset {asset_id!r} is not in the assets registry"
                )
            if _effective_gain(clip, tracks) > 0:
                reasons.append(
                    f"clip[{index}] media clip carries audio; the HyperFrames "
                    "adapter is visual-only in v1 (set clip/track volume to 0)"
                )
        else:
            params = clip.get("params") or {}
            if isinstance(params, dict):
                unknown = sorted(set(params) - _SUPPORTED_TEXT_KEYS)
                if unknown:
                    reasons.append(
                        f"clip[{index}] unsupported text params: {unknown}"
                    )
            text_field = clip.get("text")
            if text_field is not None and not isinstance(text_field, dict):
                reasons.append(f"clip[{index}] text must be an object")
    width, height, fps = _canvas(timeline) or (None, None, None)
    if width is None:
        reasons.append("canvas width/height/fps must be positive integers")
    if isinstance(fps, int) and fps <= 0:
        reasons.append("canvas fps must be a positive integer")
    return reasons


def _esc(text: str) -> str:
    return html.escape(text, quote=True)


def _css_color(value: object) -> str | None:
    if not isinstance(value, str) or not value:
        return None
    lowered = value.strip().lower()
    return _CSS_COLORS.get(lowered, value if value.startswith("#") else None)


def _compose_html(
    timeline: dict,
    *,
    width: int,
    height: int,
    fps: int,
    asset_srcs: dict | None = None,
) -> str:
    clips = timeline.get("clips") or []
    tracks = timeline.get("tracks") or []
    visual_track_ids = [
        track.get("id")
        for track in tracks
        if isinstance(track, dict) and track.get("kind") == "visual"
    ]
    # data-track-index is a TIMING LANE (non-overlap), not paint order: give
    # each Astrid track its own lane so same-lane overlaps are impossible.
    # Astrid visual tracks paint in reversed array order (later = on top).
    z_index = {
        track_id: len(visual_track_ids) - index
        for index, track_id in enumerate(visual_track_ids)
    }
    asset_srcs = asset_srcs or {}
    duration = max(
        (float(clip.get("at", 0) or 0) + _clip_duration(clip) for clip in clips),
        default=1.0,
    )

    parts: list[str] = []
    parts.append("<!DOCTYPE html>")
    parts.append('<html><head><meta charset="utf-8"><style>')
    parts.append("body{margin:0;background:#000;}")
    parts.append(
        f"#root{{position:relative;width:{width}px;height:{height}px;"
        "overflow:hidden;}"
    )
    parts.append(
        ".clip{position:absolute;top:0;left:0;width:100%;height:100%;"
        "display:flex;align-items:center;justify-content:center;}"
    )
    parts.append(
        "video.clip{object-fit:cover;background:#000;}"
    )
    parts.append("</style></head><body>")
    parts.append(
        f'<div id="root" data-composition-id="astrid" data-start="0" data-no-timeline '
        f'data-layout-allow-overlap data-duration="{duration:g}" data-width="{width}" '
        f'data-height="{height}" data-fps="{fps}">'
    )
    # Full-bleed background child (capture drops the root background).
    bg = "#000000"
    overrides = timeline.get("theme_overrides") or {}
    visual = overrides.get("visual") or {}
    if isinstance(visual.get("background"), str) and visual["background"].startswith("#"):
        bg = visual["background"]
    parts.append(
        f'<div style="position:absolute;inset:0;background:{bg};"></div>'
    )

    for index, clip in enumerate(clips):
        if not isinstance(clip, dict):
            continue
        clip_type = clip.get("clipType")
        at = float(clip.get("at", 0) or 0)
        dur = _clip_duration(clip)
        track_id = clip.get("track")
        zi = z_index.get(track_id, 0)
        clip_id = f"clip-{clip.get('id', index)}"
        if clip_type == "media":
            src = asset_srcs.get(clip.get("asset"))
            if not src:
                continue
            media_start = float(clip.get("from", 0) or 0)
            speed = _clip_speed(clip)
            parts.append(
                f'<video id="{clip_id}" class="clip" src="{_esc(src)}" muted '
                f'data-start="{at:g}" data-duration="{dur:g}" '
                f'data-mediaStart="{media_start:g}" data-playback-rate="{speed:g}" '
                f'data-track-index="{zi}" style="z-index:{zi};"></video>'
            )
            continue
        if clip_type != "text":
            continue
        hold = dur
        params = clip.get("params") or {}
        content = clip.get("text")
        if not isinstance(content, dict):
            content = {}
        raw_text = content.get("content")
        if raw_text is None:
            raw_text = params.get("content")
        text = _esc(str(raw_text) if raw_text is not None else "")
        styles: list[str] = []
        color = _css_color(params.get("color"))
        if color:
            styles.append(f"color:{color};")
        font_size = params.get("fontSize")
        if isinstance(font_size, (int, float)) and font_size > 0:
            styles.append(f"font-size:{font_size:g}px;")
        align = params.get("align")
        if align in ("left", "center", "right"):
            justify = {"left": "flex-start", "center": "center", "right": "flex-end"}[align]
            styles.append(f"justify-content:{justify};")
        weight = params.get("weight")
        if isinstance(weight, (int, str)) and str(weight).isdigit():
            styles.append(f"font-weight:{weight};")
        shadow = params.get("textShadow")
        if isinstance(shadow, str) and shadow:
            styles.append(f"text-shadow:{shadow};")
        max_width = params.get("maxWidth")
        if isinstance(max_width, (int, float)) and max_width > 0:
            styles.append(f"max-width:{max_width:g}px;")
        offset_x = params.get("offsetX")
        offset_y = params.get("offsetY")
        if isinstance(offset_x, (int, float)):
            styles.append(f"margin-left:{offset_x:g}px;")
        if isinstance(offset_y, (int, float)):
            styles.append(f"margin-top:{offset_y:g}px;")
        parts.append(
            f'<section id="{clip_id}" class="clip" data-start="{at:g}" '
            f'data-duration="{hold:g}" data-track-index="{zi}" '
            f'style="z-index:{zi};">'
        )
        parts.append(f"<p style=\"{''.join(styles)}\">{text}</p>")
        parts.append("</section>")
    parts.append("</div>")
    parts.append("</body></html>")
    return "\n".join(parts)


def _load_registry(request: dict, workspace: Path) -> tuple[dict | None, Path | None]:
    """Parse the request's assets registry (``{"assets": {id: entry}}``).

    Returns ``(data, registry_path)``; the path anchors relative ``file``
    fields.  ``(None, None)`` when the request has no usable registry.
    """
    raw = request.get("assets_registry_path")
    if not raw:
        return None, None
    registry_path = Path(raw)
    if not registry_path.is_absolute():
        registry_path = workspace / registry_path
    try:
        data = json.loads(registry_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None, None
    if not isinstance(data, dict) or not isinstance(data.get("assets"), dict):
        return None, None
    return data, registry_path


def _stage_assets(
    timeline: dict,
    registry: dict | None,
    registry_path: Path | None,
    workspace: Path,
    request: dict,
) -> dict:
    """Copy each referenced media asset into ``workspace/assets/``.

    Returns ``{asset_id: relative_src}`` for every media clip whose asset
    resolves to a real file (relative to the registry's directory).
    """
    if registry is None or registry_path is None:
        return {}
    assets_dir = workspace / "assets"
    staged: dict[str, str] = {}
    for clip in timeline.get("clips") or []:
        if not isinstance(clip, dict) or clip.get("clipType") != "media":
            continue
        asset_id = clip.get("asset")
        entry = registry.get("assets", {}).get(asset_id)
        if not isinstance(entry, dict):
            continue
        if asset_id in staged:
            continue
        file_value = entry.get("file")
        if isinstance(file_value, str):
            source = Path(file_value)
            if not source.is_absolute():
                source = registry_path.parent / source
        else:
            source = _materialized_source(request, entry)
            if source is None:
                continue
        if not source.is_file():
            continue
        assets_dir.mkdir(parents=True, exist_ok=True)
        target = assets_dir / source.name
        if not target.exists():
            shutil.copyfile(source, target)
        staged[asset_id] = f"assets/{source.name}"
    return staged


def _run(verb: str, request: dict, request_path: Path, result_path: Path) -> int:
    if request.get("schema_version") != 1:
        _error(result_path, "protocol", "unsupported request schema_version; expected 1")
        return 0
    output_name = request.get("output_name")
    if not isinstance(output_name, str) or not output_name.endswith(".mp4"):
        _error(result_path, "protocol", "output_name must be a .mp4 file name")
        return 0
    workspace = request_path.resolve().parent
    timeline_path = Path(request.get("timeline_path", ""))
    if not timeline_path.is_absolute():
        timeline_path = workspace / timeline_path
    try:
        timeline = json.loads(timeline_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        _error(result_path, "protocol", f"cannot read timeline: {exc}")
        return 0

    if verb == "support":
        registry, _registry_path = _load_registry(request, workspace)
        reasons = _support_reasons(timeline, registry=registry, request=request)
        _write(
            result_path,
            {
                "schema_version": 1,
                "supported": not reasons,
                "reasons": reasons,
                "features": {
                    "media": True,
                    "audio_mode": "none",
                    "bounded_css": True,
                    "hyperframes_pin": _HYPERFRAMES_PIN,
                },
                "alternatives": [],
                "backend": BACKEND_ID,
                "backend_version": _BACKEND_VERSION,
            },
        )
        return 0

    registry, registry_path = _load_registry(request, workspace)
    reasons = _support_reasons(timeline, registry=registry, request=request)
    if reasons:
        _error(
            result_path,
            "unsupported",
            "HyperFrames adapter does not support this timeline",
            recovery_command=(
                "use a text or silent-media timeline (volume 0) with integer "
                "canvas fps and resolvable registry assets"
            ),
            details={"reasons": reasons},
        )
        return 0
    if shutil.which("node") is None or shutil.which("ffmpeg") is None:
        _error(
            result_path,
            "binary_missing",
            "hyperframes requires node (>=22) and ffmpeg on PATH",
            recovery_command="install node 22+ and ffmpeg",
        )
        return 0

    width, height, fps = _canvas(timeline)
    assert width is not None and height is not None and fps is not None
    asset_srcs = _stage_assets(timeline, registry, registry_path, workspace, request)
    index_html = _compose_html(
        timeline,
        width=width,
        height=height,
        fps=fps,
        asset_srcs=asset_srcs,
    )
    index_path = workspace / "index.html"
    index_path.write_text(index_html, encoding="utf-8")
    out_dir = workspace / "outputs"
    out_dir.mkdir(parents=True, exist_ok=True)
    output_path = out_dir / output_name

    env = dict(os.environ)
    env["npm_config_foreground_scripts"] = "true"
    node22 = find_node22()
    if node22 is None:
        _error(
            result_path,
            "internal",
            "hyperframes requires node >= 22, which was not found",
            recovery_command="install node 22+",
        )
        return 0
    # npx must resolve Node 22 for the hyperframes CLI only; the lane's PATH keeps its pinned Node.
    env["PATH"] = str(Path(node22).resolve().parent) + os.pathsep + env.get("PATH", "")
    env.setdefault("NODE_OPTIONS", "--max-old-space-size=2048")
    completed = subprocess.run(
        [
            "npx",
            "--yes",
            f"hyperframes@{_HYPERFRAMES_PIN}",
            "render",
            str(workspace),
            "-c",
            "index.html",
            "-o",
            str(output_path),
            "--fps",
            str(fps),
            "--no-best-effort",
            "--strict",
        ],
        cwd=str(workspace),
        env=env,
        capture_output=True,
        text=True,
        timeout=600,
    )
    if completed.returncode != 0 or not output_path.is_file():
        _error(
            result_path,
            "internal",
            f"hyperframes render failed (exit {completed.returncode})",
            recovery_command="rerun hyperframes render in the workspace and inspect its output",
            details={
                "stderr": _diagnostic_tail(completed.stderr),
                "stdout": _diagnostic_tail(completed.stdout),
            },
        )
        return 0

    frames = int(round(duration_frames(timeline, fps)))
    media = output_path.read_bytes()
    profile = dict(_PROFILE)
    profile["width"] = width
    profile["height"] = height
    profile["fps_rational"] = [fps, 1]
    profile["time_base"] = [1, _mp4_time_base(fps)]
    _write(
        result_path,
        {
            "schema_version": 1,
            "video": {
                "path": f"outputs/{output_name}",
                "profile": profile,
                "sha256": hashlib.sha256(media).hexdigest(),
                "duration_frames": frames,
                "audio": "none",
                "attachments": {},
            },
            "backend_fragments": {
                BACKEND_ID: {
                    "renderer": "hyperframes",
                    "hyperframes_pin": _HYPERFRAMES_PIN,
                    "composition": "astrid",
                    "fps": fps,
                }
            },
            "audio_ownership": "none",
            "normalization": [],
            "logs": [],
            "metadata": {},
        },
    )
    return 0


def _mp4_time_base(fps: int) -> int:
    """MP4 video timescale: double fps until >= 10000 (Astrid's rule)."""
    scale = fps
    while scale < 10000:
        scale *= 2
    return scale


def duration_frames(timeline: dict, fps: int) -> float:
    clips = timeline.get("clips") or []
    duration = max(
        (float(clip.get("at", 0) or 0) + _clip_duration(clip) for clip in clips),
        default=1.0,
    )
    return duration * fps


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("verb", choices=["render", "support"])
    parser.add_argument("--request", type=Path, required=True)
    parser.add_argument("--result", type=Path, required=True)
    args = parser.parse_args(argv)
    try:
        request = json.loads(args.request.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        _error(args.result, "protocol", f"cannot read request: {exc}")
        return 0
    return _run(args.verb, request, args.request, args.result)


if __name__ == "__main__":
    raise SystemExit(main())
