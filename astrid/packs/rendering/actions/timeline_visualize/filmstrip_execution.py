"""Attempt-local rendering of an SDK-admitted, immutable filmstrip snapshot."""
from __future__ import annotations

import hashlib
import json
import math
import os
import subprocess
import tempfile
import time
import zipfile
from collections.abc import Mapping
from copy import deepcopy
from fractions import Fraction
from pathlib import Path

from runtime_protocol.platform import durable_replace

from astrid.core._shared.result_manifest import build_manifest, write_manifest

from .audio_analysis import AudioAnalysisError, analyze_audio, audio_analysis_identity
from .filmstrip_cards import (
    _attach_input_navigation,
    _navigation_usage,
    attach_input_audio_waveforms,
    attach_frame_addresses,
    build_filmstrip_pack,
)
from .filmstrip_options import filmstrip_options
from .inspection_contract import canonical_clip_identity, compact_render_receipt, project_input_window
from .shot_selector import resolve_shot_selector


_FILMSTRIP_CAPABILITY_ID = "rendering.timeline_visualize"
_MANAGED_COVERAGE_REASONS = frozenset(
    {"interval", "before_cut", "after_cut", "clip_first", "shot_midpoint"}
)


def _write_zip_atomic(destination: Path, source_root: Path) -> None:
    """Publish a complete bundle in one rename.

    The outer result manifest is the runtime publication marker, but callers
    can still inspect an attempt directory while a worker is running.  Build
    the zip beside its destination and replace it only after the archive has
    been closed and fsync'd, so an interrupted bundle can never look like a
    complete delivery (or be harvested with a truncated central directory).
    """

    destination = Path(destination)
    source_root = Path(source_root)
    destination.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary_name = tempfile.mkstemp(
        prefix=f".{destination.name}.", suffix=".tmp", dir=destination.parent
    )
    os.close(fd)
    temporary = Path(temporary_name)
    try:
        with zipfile.ZipFile(temporary, "w", compression=zipfile.ZIP_DEFLATED) as archive:
            for path in sorted(source_root.rglob("*")):
                if path.is_file():
                    archive.write(path, path.relative_to(source_root).as_posix())
        with temporary.open("rb") as stream:
            os.fsync(stream.fileno())
        durable_replace(temporary, destination)
    finally:
        temporary.unlink(missing_ok=True)


def _asset_integrity_from_registry(
    registry,
    *,
    materialized_objects: Mapping[str, object] | None = None,
    materialized_root: Path | None = None,
) -> dict[str, dict[str, object]]:
    """Verify registry-local originals when an admitted path is available.

    Input inspection never invents a preview.  A registry entry is promoted to
    ``verified_original`` only after hashing its declared local file; digest
    declarations without a materialized file remain explicit unavailable
    placeholders.
    """
    if not isinstance(registry, Mapping):
        return {}
    assets = registry.get("assets") if isinstance(registry.get("assets"), Mapping) else registry
    result: dict[str, dict[str, object]] = {}
    for asset_id, raw in assets.items():
        if not isinstance(raw, Mapping):
            continue
        expected = raw.get("sha256") or raw.get("digest") or raw.get("content_sha256") or raw.get("content_hash") or raw.get("object_id")
        if isinstance(expected, str):
            expected = expected.removeprefix("sha256:")
        path_value = raw.get("file") or raw.get("path") or raw.get("local_path") or raw.get("source_path")
        path = Path(path_value).expanduser() if isinstance(path_value, str) and path_value else None
        # A local path alone is not ownership evidence.  Only a runtime
        # admitted digest paired with a managed object identity can become a
        # verified original; otherwise keep a truthful unverified placeholder.
        managed_id = (raw.get("media_id") or raw.get("object_id") or
                       raw.get("managed_media_id") or raw.get("runtime_object_id"))
        admitted = isinstance(expected, str) and bool(managed_id)
        # Host materialization uses extensionless, attempt-local paths. Resolve
        # those paths by the admitted object identity/digest rather than by
        # filename suffix.
        from_handoff = False
        if path is None and admitted and isinstance(materialized_objects, Mapping):
            for candidate in (str(managed_id), str(expected), f"sha256:{expected}"):
                value = materialized_objects.get(candidate)
                if isinstance(value, (str, Path)) and value:
                    path = Path(str(value)).expanduser()
                    from_handoff = True
                    break
        if from_handoff and materialized_root is not None:
            try:
                root = Path(materialized_root).expanduser().resolve(strict=True)
                if not path.resolve(strict=True).is_relative_to(root):
                    path = None
            except (OSError, ValueError):
                path = None
        if path is not None and path.is_file() and admitted:
            try:
                with path.open("rb") as stream:
                    observed = hashlib.file_digest(stream, "sha256").hexdigest()
            except OSError:
                observed = None
            if observed and observed == expected:
                result[str(asset_id)] = {"state": "verified_original", "observed_sha256": observed, "managed_id": str(managed_id), "path": str(path), "media_type": raw.get("type")}
            elif observed:
                result[str(asset_id)] = {"state": "tampered", "observed_sha256": observed, "expected_sha256": expected, "managed_id": str(managed_id), "path": str(path), "media_type": raw.get("type"), "reason": "registry digest mismatch"}
            else:
                result[str(asset_id)] = {"state": "unavailable", "expected_sha256": expected, "path": str(path), "media_type": raw.get("type"), "reason": "registry source could not be read"}
        else:
            reason = "managed source is not materialized in this attempt"
            if not admitted:
                reason = "registry lacks admitted digest and managed ownership identity"
            elif path is not None and not path.is_file():
                reason = "managed source is not materialized in this attempt"
            result[str(asset_id)] = {"state": "unavailable", "expected_sha256": expected, "media_type": raw.get("type"), "reason": reason}
    return result


def execute_input_only(args, authority):
    """Emit a render-free input inspection pack from an admitted snapshot."""
    requested_view = getattr(args, "view", "filmstrip") or "filmstrip"
    if requested_view != "filmstrip":
        raise ValueError(
            "input-only mode supports only view=filmstrip; contact and motion require composed output authority"
        )
    snapshot = authority.get("input_snapshot")
    if not isinstance(snapshot, Mapping):
        raise ValueError("Input-only inspection requires an immutable timeline snapshot.")
    values = vars(args).copy()
    values["range"] = args.range_value
    # The input-only authority is already the explicit no-render projection;
    # make its component metadata truthful even for an older caller that did
    # not repeat ``--show inputs --hide output`` on the executor handoff.
    if not values.get("show"):
        values["show"] = ["inputs"]
    if not values.get("hide"):
        values["hide"] = ["output"]
    options = filmstrip_options(values)
    options["shot"] = resolve_shot_selector(options.get("shot"), snapshot)
    from fractions import Fraction
    fps = Fraction(*(snapshot.get("fps_rational") or [30, 1]))
    total = int(snapshot.get("duration_frames") or 0)
    if total <= 0:
        raise ValueError("Input-only inspection requires a positive timeline extent.")
    window = options.get("input_window")
    if isinstance(window, Mapping):
        start = Fraction(*window["start"])
        end = Fraction(*window["end"])
    else:
        start, end = Fraction(0), Fraction(total, 1) / fps
    ceil_frame = lambda value: (value.numerator + value.denominator - 1) // value.denominator
    start_frame = max(0, ceil_frame(start * fps))
    end_frame = min(total, ceil_frame(end * fps))
    if end_frame <= start_frame:
        raise ValueError("Requested input window contains no timeline frames.")
    integrity = _asset_integrity_from_registry(
        snapshot.get("registry"),
        materialized_objects=getattr(args, "materialized_objects", None),
        materialized_root=getattr(args, "materialized_root", None),
    )
    projection = project_input_window(
        snapshot.get("clips") or [], start_frame=start_frame, end_frame=end_frame,
        fps=fps, track_ids=options.get("track_ids") or (), clip_id=options.get("clip"),
        shot_id=options.get("shot"), occurrence_id=options.get("occurrence"),
        asset_id=options.get("asset"), integrity=integrity,
        shot_groups=snapshot.get("pinned_shots") or snapshot.get("pinnedShotGroups") or (),
        shot_occurrences=snapshot.get("shot_occurrences") or (),
    )
    if options.get("occurrence") is not None:
        first_input = next(
            (
                clip for track in projection.get("tracks", [])
                for clip in (track.get("clips") or [])
                if isinstance(clip, Mapping)
                and str(clip.get("occurrence_id")) == str(options["occurrence"])
            ),
            None,
        )
        if first_input is not None:
            index_target = canonical_clip_identity(
                first_input,
                timeline_id=str(snapshot.get("timeline_id")) if snapshot.get("timeline_id") else None,
                occurrence_id=str(options["occurrence"]),
                shot_id=options.get("shot"),
            )
        else:
            index_target = None
    else:
        index_target = None
    index = {
        "schema": "astrid.timeline-input-inspection.v1",
        "provenance": {key: snapshot.get(key) for key in ("project_slug", "timeline_id", "timeline_name", "render_run_id", "fps_rational", "duration_frames", "metadata")},
        "sampling": {"mode": "input_only", "window": projection["window"], "shared_window": [float(Fraction(start_frame, 1) / fps), float(Fraction(end_frame, 1) / fps)], "range": [float(Fraction(start_frame, 1) / fps), float(Fraction(end_frame, 1) / fps)], "step_frames_rational": [1, 1], "include_cuts": False, "explicit_interval": False, "options": options},
        "input_projection": projection, "cards": [], "navigation": {"frames": [], "tracks": [], "clips": [], "shots": [], "phrases": [], "gaps": [], "waveforms": [], "targets": {}},
        "components": options.get("components"), "render": {"status": "not_requested", "auto_render": False},
        "inspection": {
            "scope": {"timeline_id": snapshot.get("timeline_id"), "occurrence_id": options.get("occurrence")},
            "target": {"kind": "occurrence" if options.get("occurrence") else "timeline",
                       "timeline_id": snapshot.get("timeline_id"), "occurrence_id": options.get("occurrence")},
        },
    }
    if index_target is not None:
        index["inspection"]["target"] = index_target
    index["navigation"]["usage"] = _navigation_usage(snapshot, options)
    out_root = args.out.expanduser().resolve()
    # Keep input-only packs under the same runtime-approved namespace as the
    # paired/output views.  The host deliberately accepts ``filmstrip-view``
    # as a managed-output namespace (and strips it to a direct leaf filename);
    # a bespoke ``input-view`` namespace would fail output custody before the
    # thumbnails could be consumed.
    pack_root = out_root / "filmstrip-view"
    pack_root.mkdir(parents=True, exist_ok=True)
    attach_input_audio_waveforms(projection, integrity=integrity, out_root=pack_root)
    _materialize_input_previews(projection, pack_root)
    _attach_input_navigation(index, projection, track_meta=snapshot.get("tracks"))
    # Input-only is a first-class filmstrip view, so its public pages use the
    # same ``filmstrip-###.png`` entrypoint naming as rendered/paired output.
    png_pages = _render_input_projection_png(
        projection, snapshot, pack_root, filename_prefix="filmstrip",
    )
    (pack_root / "render-snapshot.json").write_text(json.dumps(snapshot, indent=2, ensure_ascii=False), encoding="utf-8")
    # Input-only deliveries use the same compact v2 receipt as output and
    # paired deliveries. The rich projection remains available in memory for
    # rendering, while the persisted entrypoint stays bounded and points back
    # to the verified snapshot for placement drill-down.
    persisted_index = compact_render_receipt(index, snapshot, pack_root)
    (pack_root / "frame-index.json").write_text(
        json.dumps(persisted_index, ensure_ascii=True, separators=(",", ":")),
        encoding="utf-8",
    )
    files = sorted(path for path in pack_root.rglob("*") if path.is_file())
    primary_png = Path(png_pages[0]).relative_to(pack_root).as_posix() if png_pages else None
    # Every concrete file in a universal result manifest needs a stable
    # identity.  The generic host uses this field when it harvests a receipt;
    # omitting it makes input-only visualization fail before the pack can be
    # consumed ("output ... must declare name or port").
    outputs = [
        {
            "name": path.relative_to(pack_root).as_posix(),
            "path": path.relative_to(pack_root).as_posix(),
            "type": "file",
            "role": "result",
            "is_primary": path.relative_to(pack_root).as_posix() == primary_png,
        }
        for path in files
    ]
    entrypoints = {"frames": "frame-index.json"}
    if primary_png:
        entrypoints["png"] = primary_png
    manifest = build_manifest(kind="timeline_filmstrip", created="1970-01-01T00:00:00Z", inputs={"timeline_id": snapshot.get("timeline_id"), "components": options.get("components"), "window": projection["window"], "render_requested": False}, outputs=outputs, entrypoints=entrypoints, timeline_ids=[snapshot.get("timeline_id")], request=options.get("request"))
    write_manifest(pack_root / "manifest.json", manifest)
    # Input-only runs are still first-class filmstrip deliveries.  Package
    # the preview stills, audio rails, frame index, and nested manifest so the
    # generic host can publish one durable artifact instead of returning a
    # manifest whose attempt-local PNGs disappear during cleanup.
    bundle = out_root / "filmstrip-bundle.zip"
    _write_zip_atomic(bundle, pack_root)
    outer_entrypoints = {"manifest": "filmstrip-view/manifest.json", "frame_index": "filmstrip-view/frame-index.json"}
    if primary_png:
        outer_entrypoints["png"] = f"filmstrip-view/{primary_png}"
    write_manifest(
        out_root / "manifest.json",
        build_manifest(
            kind="timeline_filmstrip_result",
            created="1970-01-01T00:00:00Z",
            inputs={"timeline_id": snapshot.get("timeline_id"), "render_requested": False},
            outputs=[
                {
                    "name": "filmstrip_manifest",
                    "path": "filmstrip-view/manifest.json",
                    "type": "file",
                    "role": "auxiliary",
                    "is_primary": False,
                },
                {
                    "name": "filmstrip_bundle",
                    "path": "filmstrip-bundle.zip",
                    "type": "file",
                    "role": "result",
                    "is_primary": True,
                },
            ],
            entrypoints=outer_entrypoints,
        ),
    )
    return {"returncode": 0, "run_root": str(out_root), "manifest_path": str(pack_root / "manifest.json"), "timeline_ids": [snapshot.get("timeline_id")], "outputs": {"pack_root": str(pack_root), "manifest_path": str(pack_root / "manifest.json"), "frame_index": str(pack_root / "frame-index.json"), "pages": [str(path) for path in png_pages], "filmstrip_bundle": str(bundle), "render_requested": False}}


def _materialize_input_previews(projection: Mapping, pack_root: Path) -> None:
    """Materialize digest-verified visual stills/posters into the result pack."""
    from PIL import Image

    preview_root = pack_root / "source-previews"
    seen: set[str] = set()
    for track in projection.get("tracks") or []:
        for clip in track.get("clips") or []:
            preview = clip.get("source_preview") if isinstance(clip, Mapping) else None
            if not isinstance(preview, Mapping) or preview.get("status") != "verified":
                continue
            source = preview.get("path")
            if not isinstance(source, str) or not source:
                continue
            source_path = Path(source).expanduser()
            if not source_path.is_file():
                continue
            media_type = str(preview.get("media_type") or "").lower()
            media_kind = media_type.split("/", 1)[0]
            if media_kind == "audio":
                continue
            digest = str(preview.get("digest") or hashlib.sha256(source_path.as_posix().encode()).hexdigest())
            name = hashlib.sha256(digest.encode()).hexdigest()[:24] + ".png"
            destination = preview_root / name
            if name not in seen:
                preview_root.mkdir(parents=True, exist_ok=True)
                try:
                    if media_kind == "video":
                        subprocess.run(
                            [
                                "ffmpeg", "-hide_banner", "-loglevel", "error",
                                "-i", str(source_path), "-frames:v", "1",
                                "-vf", "scale=320:180:force_original_aspect_ratio=decrease",
                                "-y", str(destination),
                            ], check=True, stdout=subprocess.DEVNULL,
                            stderr=subprocess.DEVNULL,
                        )
                    else:
                        with Image.open(source_path) as source_image:
                            source_image.convert("RGBA").save(destination, format="PNG")
                except (OSError, ValueError, subprocess.SubprocessError):
                    continue
                seen.add(name)
            materialized = dict(preview)
            materialized.pop("path", None)
            materialized["preview"] = f"source-previews/{name}"
            materialized["preview_kind"] = "poster" if media_kind == "video" else "still"
            clip["source_preview"] = materialized


def _render_input_projection_png(
    projection, snapshot, pack_root: Path, *, width: int = 1500,
    filename_prefix: str = "input-band",
) -> list[Path]:
    """Render readable, paginated input lanes without creating media frames."""
    from PIL import Image, ImageDraw
    from .filmstrip_cards import _png_draw_text, _png_ellipsis, _png_font, _png_text_width
    tracks = projection.get("tracks") or []
    bands = projection.get("track_bands") or [{"track_ids": [t.get("track_id") for t in tracks], "label": "tracks"}]
    by_id = {str(t.get("track_id")): t for t in tracks}
    fps = Fraction(*(projection["window"].get("fps") or [30, 1]))
    start, end = projection["window"]["start_frame"], projection["window"]["end_frame"]
    span = max(1, end - start)
    # Reuse the same bundled font chain as the rendered contact sheets.  The
    # old default bitmap font made the lane labels both tiny and impossible to
    # clip to short placements, which caused adjacent labels to run together.
    font = _png_font(16)
    small_font = _png_font(13)
    paths: list[Path] = []
    for page, band in enumerate(bands, 1):
        ids = [str(item) for item in band.get("track_ids") or []]
        row_height, header = 96, 144
        row_heights = []
        for track_id in ids:
            track = by_id.get(track_id, {})
            clips = track.get("clips") or []
            track_name = str(track_id).lower()
            audio_track = (
                track_name in {"audio", "vo", "voiceover", "music", "sound", "sfx"}
                or any(
                    isinstance(clip, Mapping)
                    and (
                        isinstance(clip.get("audio_signifier"), Mapping)
                        or str((clip.get("source_preview") or {}).get("media_type") or "").lower().split("/", 1)[0] == "audio"
                    )
                    for clip in clips
                )
            )
            max_subrow = max((int(clip.get("subrow", 0)) for clip in clips), default=0)
            row_heights.append(max(112 if audio_track else row_height, 64 + (58 if audio_track else 48) * max_subrow))
        image = Image.new("RGB", (width, header + sum(row_heights or [row_height]) + 28), "#10161d")
        draw = ImageDraw.Draw(image)
        _png_draw_text(draw, (24, 16), f"{snapshot.get('timeline_name') or snapshot.get('timeline_id')} · input lanes", font, fill="#ebf2f5")
        _png_draw_text(draw, (24, 42), f"{band.get('label', f'tracks {page}')} · [{start / float(fps):.3f}s, {end / float(fps):.3f}s) · frozen source placements", small_font, fill="#9fb0bf")
        _png_draw_text(draw, (24, 66), "Source previews are shown only when digest-verified; placeholders never substitute rendered output.", small_font, fill="#9fb0bf")
        _png_draw_text(draw, (24, 90), "Teal = verified source / measured waveform · amber = timing fallback or unavailable · vertical guides = shared time samples.", small_font, fill="#9fb0bf")
        lane_left, lane_right = 24, width - 24
        ruler_y = 128
        draw.line((lane_left, ruler_y, lane_right, ruler_y), fill="#6edac7", width=1)
        # A visible ruler makes the input geometry use the same zero and end
        # points as the rendered surface; the old 210px label gutter made a
        # clip at t=0 look like it started late.
        tick_count = 4
        for tick in range(tick_count + 1):
            fraction = tick / tick_count
            x = lane_left + fraction * (lane_right - lane_left)
            draw.line((x, ruler_y - 6, x, ruler_y + 6), fill="#6edac7", width=1)
            seconds = start / float(fps) + fraction * (end - start) / float(fps)
            label = f"{seconds:.1f}s"
            _png_draw_text(draw, (x - _png_text_width(draw, label, small_font) / 2, 108), label, small_font, fill="#8ce0d0")
            draw.line((x, ruler_y + 7, x, image.height - 16), fill="#23343f", width=1)
        y = header
        for track_index, track_id in enumerate(ids):
            track = by_id.get(track_id, {})
            clips = track.get("clips") or []
            current_row_height = row_heights[track_index]
            draw.line((18, y, width - 18, y), fill="#304151", width=1)
            role = {"frame": "composited frame / overlay", "picture": "picture / shots", "vo": "voiceover", "audio": "audio"}.get(str(track_id), "source track")
            _png_draw_text(draw, (24, y + 12), f"{track_id} · {role}", font, fill="#ebf2f5")
            draw.line((lane_left, y + 28, lane_right, y + 28), fill="#304151", width=1)
            for clip in clips:
                clip_start, clip_end = clip["window"]
                x0 = lane_left + (clip_start - start) / span * (lane_right - lane_left)
                x1 = lane_left + (clip_end - start) / span * (lane_right - lane_left)
                track_name = str(track_id).lower()
                preview_meta = clip.get("source_preview") if isinstance(clip, Mapping) else None
                preview_type = str(preview_meta.get("media_type") or "").lower() if isinstance(preview_meta, Mapping) else ""
                is_audio_track = (
                    track_name in {"audio", "vo", "voiceover", "music", "sound", "sfx"}
                    or preview_type.split("/", 1)[0] == "audio"
                )
                verified = clip.get("source_preview", {}).get("status") == "verified"
                track_color = {"frame": "#55406b", "picture": "#305265", "vo": "#4f526f", "audio": "#355e58"}.get(str(track_id), "#405769")
                color = track_color if verified else "#594735"
                box_right = max(x0 + 4, x1)
                clip_width = max(0.0, box_right - x0)
                clip_y = y + 42 + 48 * int(clip.get("subrow", 0))
                # Give audio placements a little more vertical presence: the
                # rail is an intentional visual cue, not a measured waveform,
                # but it should still read in a dense full-timeline sheet.
                clip_height = 56 if is_audio_track else 34
                draw.rounded_rectangle((x0, clip_y, box_right, clip_y + clip_height), radius=5, fill=color, outline="#8ce0d0")
                preview = preview_meta
                preview_rel = preview.get("preview") if isinstance(preview, Mapping) else None
                # Visual placements reserve a compact preview slot on the
                # left. Audio placements are waveform/label-only and never
                # receive a crossed thumbnail placeholder.
                preview_slot = min(68.0, max(0.0, clip_width - 8.0)) if not is_audio_track else 0.0
                if preview_rel and preview.get("status") == "verified" and preview_slot >= 8:
                    try:
                        with Image.open(pack_root / str(preview_rel)) as source:
                            thumb = source.convert("RGB")
                            thumb.thumbnail((max(1, int(preview_slot - 4)), clip_height - 4))
                            image.paste(thumb, (int(x0 + 2 + max(0, (preview_slot - 4 - thumb.width) / 2)), int(clip_y + 2 + max(0, (clip_height - 4 - thumb.height) / 2))))
                    except (OSError, ValueError):
                        pass
                elif not is_audio_track and clip_width >= 22:
                    # A muted, crossed tile is an honest visual indicator that
                    # the source preview is unavailable; it never reuses the
                    # rendered output as a misleading stand-in.
                    icon_right = min(box_right - 2, x0 + 28)
                    draw.rectangle((x0 + 2, clip_y + 2, icon_right, clip_y + clip_height - 2), outline="#9f7b58", width=1)
                    draw.line((x0 + 4, clip_y + 5, icon_right - 2, clip_y + clip_height - 5), fill="#9f7b58", width=1)
                    draw.line((x0 + 4, clip_y + clip_height - 5, icon_right - 2, clip_y + 5), fill="#9f7b58", width=1)
                audio = clip.get("audio_signifier") if isinstance(clip, Mapping) else None
                if isinstance(audio, Mapping):
                    audio_present = audio.get("present") is True
                    if clip_width >= 34:
                        if audio_present:
                            audio_left = x0 + (preview_slot + 5 if preview_slot else 6)
                            audio_right = box_right - 6
                            if audio_right > audio_left:
                                rail_y = clip_y + clip_height / 2
                                waveform = audio.get("waveform")
                                display_values = waveform.get("display_amplitudes") if isinstance(waveform, Mapping) else None
                                measured_values = waveform.get("amplitudes") if isinstance(waveform, Mapping) else None
                                values = display_values if isinstance(display_values, list) else measured_values
                                if isinstance(values, list) and values:
                                    # Source waveforms use the same exact clip
                                    # span as the timing fallback, but their bar
                                    # heights come from this asset's measured
                                    # PCM peaks rather than a generic marker.
                                    draw.line((audio_left, rail_y, audio_right, rail_y), fill="#476b76", width=2)
                                    inner_width = max(1.0, audio_right - audio_left)
                                    for bar_index, value in enumerate(values):
                                        try:
                                            amplitude = max(0.0, min(1.0, float(value)))
                                        except (TypeError, ValueError):
                                            amplitude = 0.0
                                        if not math.isfinite(amplitude) or amplitude <= 0:
                                            continue
                                        bar_x = audio_left + inner_width * (bar_index + 0.5) / len(values)
                                        bar_height = max(2, int(round(amplitude * (clip_height - 10) / 2)))
                                        draw.line(
                                            (bar_x, rail_y - bar_height, bar_x, rail_y + bar_height),
                                            fill="#8ff6dd", width=3,
                                        )
                                else:
                                    # Unavailable/unanalysed sources retain a
                                    # truthful placement rail, never fabricated
                                    # amplitude bars.
                                    draw.line((audio_left, rail_y, audio_right, rail_y), fill="#ffc078", width=5)
                                    markers = max(3, min(32, int((audio_right - audio_left) // 12)))
                                    for marker_index in range(markers):
                                        marker_x = audio_left + (audio_right - audio_left) * marker_index / max(1, markers - 1)
                                        draw.line((marker_x, rail_y - 10, marker_x, rail_y + 10), fill="#ffe0a8", width=3)
                        elif str(audio.get("reason")) == "muted":
                            _png_draw_text(draw, (box_right - 16, clip_y + 9), "×", small_font, fill="#9fb0bf")
                # A lane image is a geometry overview, not the authoritative
                # identity surface.  Never paint text outside its clip box:
                # wide clips get the id/status, medium clips get a clipped id,
                # and tiny clips remain intentionally unlabeled blocks.
                box_width = max(0.0, clip_width - (preview_slot + 6 if preview_slot else 10))
                clip_id = str(clip.get("clip_id") or "clip")
                status = str(clip.get("source_preview", {}).get("status", "placeholder"))
                if box_width >= 86:
                    label = f"{clip_id} · {status}"
                elif box_width >= 28:
                    label = clip_id
                else:
                    label = ""
                if label:
                    if _png_text_width(draw, label, small_font) > box_width:
                        label = _png_ellipsis(draw, label, small_font, box_width)
                    label_x = x0 + preview_slot + 5 if preview_slot else x0 + 6
                    label_y = clip_y + 9
                    label_width = _png_text_width(draw, label, small_font)
                    # The rail runs behind the clip identity. Keep the chip
                    # inside the placement so narrow clips cannot bleed into
                    # neighboring timing regions.
                    draw.rounded_rectangle(
                        (
                            max(x0 + 2, label_x - 4),
                            max(clip_y + 2, label_y - 3),
                            min(box_right - 2, label_x + label_width + 4),
                            min(clip_y + clip_height - 2, label_y + 19),
                        ),
                        radius=4,
                        fill="#10161d",
                        outline="#304151",
                    )
                    _png_draw_text(draw, (label_x, label_y), label, small_font, fill="#ebf2f5")
            y += current_row_height
        path = pack_root / f"{filename_prefix}-{page:03d}.png"
        image.save(path)
        paths.append(path)
    return paths


def _filmstrip_managed_coverage(frame_index: Mapping[str, object]) -> dict[str, object]:
    """Project the verified frame index into the managed-output V1 shape.

    The frame index intentionally contains richer viewer-only coverage fields
    (boundary counts, page layout, and overview reason labels). Runtime
    managed outputs accept the stable sampling subset; retain exact frame
    bounds, density, step, and cards whenever their reason vocabulary is
    already part of that contract.
    """
    sampling = frame_index.get("sampling")
    coverage = frame_index.get("coverage")
    provenance = frame_index.get("provenance")
    if not isinstance(sampling, Mapping) or not isinstance(coverage, Mapping):
        raise ValueError("filmstrip frame index is missing canonical coverage")
    if not isinstance(provenance, Mapping):
        raise ValueError("filmstrip frame index is missing timing provenance")
    raw_fps = provenance.get("fps_rational")
    if (
        not isinstance(raw_fps, (list, tuple))
        or len(raw_fps) != 2
        or any(isinstance(value, bool) or not isinstance(value, int) or value <= 0 for value in raw_fps)
    ):
        raise ValueError("filmstrip frame index has invalid fps provenance")
    fps = Fraction(raw_fps[0], raw_fps[1])
    raw_window = coverage.get("window_seconds")
    if (
        not isinstance(raw_window, (list, tuple))
        or len(raw_window) != 2
        or any(isinstance(value, bool) or not isinstance(value, (int, float)) for value in raw_window)
        or any(not math.isfinite(float(value)) for value in raw_window)
    ):
        raise ValueError("filmstrip coverage has an invalid rendered window")

    def frame_boundary(seconds: int | float) -> int:
        value = Fraction(str(seconds)) * fps
        return (value.numerator + value.denominator - 1) // value.denominator

    raw_mode = sampling.get("mode")
    requested_frame = sampling.get("requested_frame")
    resolved_at_frame = sampling.get("resolved_at_frame")
    exact_frame = (
        requested_frame
        if raw_mode == "exact_frame" and isinstance(requested_frame, int) and requested_frame >= 0
        else resolved_at_frame
        if raw_mode == "exact_frame" and isinstance(resolved_at_frame, int) and resolved_at_frame >= 0
        else None
    )
    if exact_frame is not None:
        # Do not round-trip an exact frame through decimal seconds: values such
        # as 428/30 can stringify just above the integer boundary and make
        # ceil(start) equal ceil(end).
        start, end = exact_frame, exact_frame + 1
    else:
        start, end = (frame_boundary(value) for value in raw_window)
    if start < 0 or end <= start:
        raise ValueError("filmstrip coverage has an empty rendered window")
    mode = sampling.get("mode")
    if mode == "overview":
        mode = "interval"
    if mode == "exact_frame":
        mode = "interval"
    if mode == "motion":
        # --view motion samples one picture cut; the Runtime's stable
        # vocabulary calls that cut sampling.  Its frame reasons ("motion",
        # "cut_tile") are not in that vocabulary, so cards stay in the frame
        # index only (see _MANAGED_COVERAGE_REASONS).
        mode = "cuts"
    if mode not in {"interval", "clips", "cuts", "shots"}:
        raise ValueError("filmstrip sampling mode is not managed-output compatible")
    managed_sampling: dict[str, object] = {
        "mode": mode,
        "range": {"start": start, "end": end},
    }
    # Exact-frame identity remains in the frozen frame-index/cards. The
    # managed-output V1 coverage envelope intentionally stays compatible with
    # Runtime's stable sampling schema, where the one-frame range is the
    # machine-readable exact-frame witness.
    raw_step = sampling.get("step_frames_rational")
    if (
        isinstance(raw_step, (list, tuple))
        and len(raw_step) == 2
        and all(isinstance(value, int) and not isinstance(value, bool) for value in raw_step)
        and raw_step[0] >= 0
        and raw_step[1] > 0
    ):
        managed_sampling["step_frames_rational"] = {
            "numerator": raw_step[0], "denominator": raw_step[1]
        }
    density = sampling.get("density")
    if isinstance(density, Mapping):
        density_mode, density_value = density.get("mode"), density.get("value")
        if (
            mode == "interval"
            and density_mode == "every_seconds"
            and isinstance(density_value, (int, float))
            and not isinstance(density_value, bool)
            and math.isfinite(float(density_value))
            and density_value > 0
        ):
            managed_sampling["every"] = density_value
        elif (
            mode == "interval"
            and density_mode == "every_frames"
            and isinstance(density_value, int)
            and not isinstance(density_value, bool)
            and density_value > 0
        ):
            managed_sampling["every_frames"] = density_value

    cards = frame_index.get("cards")
    if isinstance(cards, list):
        normalized_cards = []
        for card in cards:
            if not isinstance(card, Mapping):
                normalized_cards = []
                break
            reasons = card.get("sample_reasons")
            if (
                not isinstance(reasons, list)
                or not reasons
                or not all(isinstance(reason, str) for reason in reasons)
                or not set(reasons).issubset(_MANAGED_COVERAGE_REASONS)
            ):
                normalized_cards = []
                break
            frame = card.get("frame")
            time_seconds = card.get("time_seconds")
            if (
                isinstance(frame, bool)
                or not isinstance(frame, int)
                or frame < 0
                or isinstance(time_seconds, bool)
                or not isinstance(time_seconds, (int, float))
                or not math.isfinite(float(time_seconds))
                or time_seconds < 0
            ):
                normalized_cards = []
                break
            time_rational = card.get("time_rational")
            if (
                not isinstance(time_rational, (list, tuple))
                or len(time_rational) != 2
                or any(isinstance(value, bool) or not isinstance(value, int) for value in time_rational)
                or time_rational[0] < 0
                or time_rational[1] <= 0
            ):
                normalized_cards = []
                break
            normalized_cards.append(
                {
                    "frame": frame,
                    "time_seconds": time_seconds,
                    "time_rational": {
                        "numerator": time_rational[0],
                        "denominator": time_rational[1],
                    },
                    "sample_reasons": list(reasons),
                }
            )
        if normalized_cards:
            managed_sampling["cards"] = normalized_cards
    return {"sampling": managed_sampling}


def _filmstrip_output_contract(
    snapshot: Mapping[str, object],
    options: Mapping[str, object],
    video_digest: str,
    coverage: Mapping[str, object],
) -> dict[str, object]:
    """Return explicit lifecycle metadata for one derived filmstrip result."""

    capture_mode = (snapshot.get('metadata') or {}).get('selection') == 'composed_frame_capture'
    exact_inputs = {
        "render_run_id": snapshot["render_run_id"],
        "timeline_id": snapshot["timeline_id"],
        "video_digest": video_digest,
        "options": dict(options),
    }
    if capture_mode:
        exact_inputs["evidence_source"] = "composed_frame_capture"
    recipe = {
        "capability_id": _FILMSTRIP_CAPABILITY_ID,
        "view": "filmstrip",
        "exact_inputs": exact_inputs,
    }
    recipe_digest = "sha256:" + hashlib.sha256(
        json.dumps(recipe, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode(
            "utf-8"
        )
    ).hexdigest()
    provenance = {
        "render_run_id": snapshot["render_run_id"],
        "timeline_id": snapshot["timeline_id"],
        "video_digest": video_digest,
    }
    if capture_mode:
        provenance["evidence_source"] = "composed_frame_capture"
    return {
        "producer": {"capability_id": _FILMSTRIP_CAPABILITY_ID, "view": "filmstrip"},
        "provenance": provenance,
        "regeneration": {
            "available": True,
            "capability_id": _FILMSTRIP_CAPABILITY_ID,
            "source_refs": [] if capture_mode else [video_digest],
            "recipe_digest": recipe_digest,
            "exact_inputs": exact_inputs,
        },
        "coverage": _filmstrip_managed_coverage(coverage),
    }


def _rendered_timing(video: Path, fps) -> tuple[int, float]:
    """Return decoded frame extent and duration, not authored timeline length.

    A managed render can contain an explicit tail that is absent from the
    authored picture timeline. Filmstrip sampling must follow the bytes being
    reviewed.
    """
    probe = subprocess.run(
        ["ffprobe", "-v", "error", "-count_frames", "-select_streams", "v:0",
         "-show_entries", "stream=nb_read_frames,nb_frames,duration,r_frame_rate",
         "-of", "json", str(video)],
        capture_output=True, text=True, check=False,
    )
    if probe.returncode:
        raise ValueError("Unable to probe rendered video duration: " + probe.stderr[-1000:])
    try:
        payload = json.loads(probe.stdout)
        stream = payload["streams"][0]
        try:
            frames = int(stream.get("nb_read_frames") or stream.get("nb_frames") or 0)
        except (TypeError, ValueError):
            frames = 0
        format_info = payload.get("format") or {}
        duration_value = stream.get("duration") or format_info.get("duration") or 0
        try:
            duration = float(duration_value)
        except (TypeError, ValueError):
            duration = 0.0
    except (ValueError, TypeError, KeyError, IndexError, json.JSONDecodeError) as exc:
        raise ValueError("Rendered video duration probe was invalid.") from exc
    if frames <= 0 and duration <= 0:
        raise ValueError("Rendered video has no usable duration.")
    if frames <= 0:
        frames = max(1, math.ceil(duration * float(fps)))
    if duration <= 0:
        duration = frames / float(fps)
    return frames, duration


def _rendered_frame_count(video: Path, fps) -> int:
    """Return the decoded video-frame count for compatibility with callers."""
    return _rendered_timing(video, fps)[0]


def _align_snapshot_to_render(snapshot: dict, video: Path) -> None:
    """Make the review snapshot describe the actual rendered composition.

    The authored timeline remains authoritative for shot/script identity.  If
    the render is longer, represent its unowned tail explicitly as a black
    render tail rather than pretending it belongs to the last shot.
    """
    from fractions import Fraction

    fps = Fraction(*snapshot["fps_rational"])
    rendered_frames, decoded_duration = _rendered_timing(video, fps)
    metadata = snapshot.get("metadata")
    authored_frames = int(
        metadata.get("authored_duration_frames")
        if isinstance(metadata, Mapping) and metadata.get("authored_duration_frames") is not None
        else snapshot.get("duration_frames") or 0
    )
    # Keep the complete admitted input clock before adapting presentation
    # metadata to decoded output. The legacy ``clips`` list remains clamped so
    # rendered cards cannot point past EOF; input lanes consume this frozen
    # copy and therefore retain authored clips after a short render.
    input_clips = deepcopy(snapshot.get("clips") or [])
    input_extent_frames = max([
        authored_frames,
        *(int(item.get("end_frame", 0)) for item in input_clips if isinstance(item, Mapping)),
    ])
    snapshot.setdefault("metadata", {})["input_extent_frames"] = input_extent_frames
    snapshot["metadata"]["input_extent_seconds"] = input_extent_frames / float(fps)
    snapshot["input_clips"] = input_clips
    snapshot["metadata"]["asset_integrity"] = _asset_integrity_from_registry(snapshot.get("registry"))
    snapshot["duration_frames"] = rendered_frames
    snapshot.setdefault("metadata", {})["rendered_duration_frames"] = rendered_frames
    snapshot["metadata"]["rendered_duration_seconds"] = decoded_duration
    snapshot["metadata"]["authored_duration_frames"] = authored_frames
    snapshot["metadata"]["authored_duration_seconds"] = authored_frames / float(fps)
    snapshot["metadata"]["duration_basis"] = "rendered_video"
    # Authored clips and proven shot occurrences cannot own frames beyond the
    # authored clock. Clamp both clocks to the decoded render EOF so no label
    # leaks into the excess region or points past a shorter materialization.
    for clip in snapshot.get("clips", []):
        start = min(max(int(clip.get("start_frame", 0)), 0), authored_frames, rendered_frames)
        end = min(max(int(clip.get("end_frame", rendered_frames)), 0), authored_frames, rendered_frames)
        clip["start_frame"], clip["end_frame"] = start, max(start, end)
        if clip.get("duration") is not None:
            clip["duration"] = max(0.0, (clip["end_frame"] - start) / float(fps))
    clamped_occurrences = []
    for occurrence in snapshot.get("occurrences", []):
        start = min(max(int(occurrence.get("start_frame", 0)), 0), authored_frames, rendered_frames)
        end = min(max(int(occurrence.get("end_frame", 0)), 0), authored_frames, rendered_frames)
        if end <= start:
            continue
        item = dict(occurrence, start_frame=start, end_frame=end,
                    start=start / float(fps), end=end / float(fps))
        clamped_occurrences.append(item)
    snapshot["occurrences"] = clamped_occurrences
    clamped_scripts = []
    for script in snapshot.get("scripts", []):
        start = min(max(float(script.get("start", 0)), 0.0), rendered_frames / float(fps))
        end = min(max(float(script.get("end", 0)), 0.0), rendered_frames / float(fps))
        if end > start:
            clamped_scripts.append(dict(script, start=start, end=end))
    snapshot["scripts"] = clamped_scripts
    tail_start = min(max(authored_frames, 0), rendered_frames)
    tail = None
    if tail_start < rendered_frames:
        tail = {
            "id": "__rendered_tail__",
            "label": "Unmapped rendered tail",
            "status": "unmapped",
            "mapping_status": "unmapped",
            "start_frame": tail_start,
            "end_frame": rendered_frames,
            "start_seconds": tail_start / float(fps),
            "end_seconds": rendered_frames / float(fps),
        }
        snapshot["unmapped_regions"] = [tail]
        snapshot.setdefault("clips", []).append({
            "id": tail["id"], "label": tail["label"], "status": tail["status"],
            "mapping_status": tail["mapping_status"], "at": tail["start_seconds"],
            "duration": tail["end_seconds"] - tail["start_seconds"],
            "start_frame": tail_start, "end_frame": rendered_frames,
            "track": "picture", "kind": "render_tail", "clipType": "render_tail",
            "render_tail": True,
        })
    else:
        snapshot["unmapped_regions"] = []
    snapshot["metadata"]["rendered_tail"] = tail


def _audio_cache_path(parent: Path, render_digest: str, settings: object) -> Path:
    key = hashlib.sha256(json.dumps(
        {"render_digest": render_digest, "settings": settings},
        sort_keys=True, separators=(",", ":"), default=str,
    ).encode("utf-8")).hexdigest()
    return parent / ".audio-analysis-cache" / f"{key}.json"


def _cached_audio(parent: Path, render_digest: str, settings: object) -> dict | None:
    path = _audio_cache_path(parent, render_digest, settings)
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        return None
    if not isinstance(value, dict) or value.get("render_digest") != render_digest:
        return None
    return value


def _store_audio_cache(parent: Path, render_digest: str, settings: object, value: dict) -> None:
    path = _audio_cache_path(parent, render_digest, settings)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, sort_keys=True, ensure_ascii=False) + "\n", encoding="utf-8")
    entries = sorted(path.parent.glob("*.json"), key=lambda item: item.stat().st_mtime, reverse=True)
    for stale in entries[256:]:
        stale.unlink(missing_ok=True)


def _render_time_anchored_output(
    frame_index: Mapping[str, object], pack_root: Path, *, options: Mapping[str, object],
) -> list[Path]:
    """Render sampled output cards whose left edges are exact timeline times."""
    from PIL import Image, ImageDraw
    from .filmstrip_cards import (
        _png_bounded_lines, _png_draw_text, _png_ellipsis, _png_font,
        _png_shot_label, _png_text_width, _png_waveform_for_card,
        _PNG_AUDIO_HEIGHT,
    )

    raw_cards = frame_index.get("cards")
    if not isinstance(raw_cards, list) or not raw_cards:
        return []
    cards = [card for card in raw_cards if isinstance(card, Mapping)]
    if not cards:
        return []
    provenance = frame_index.get("provenance") if isinstance(frame_index.get("provenance"), Mapping) else {}
    raw_fps = provenance.get("fps_rational") or [30, 1]
    try:
        fps = Fraction(int(raw_fps[0]), int(raw_fps[1]))
    except (TypeError, ValueError, ZeroDivisionError, IndexError):
        fps = Fraction(30, 1)
    input_window = frame_index.get("input_projection", {}).get("window") if isinstance(frame_index.get("input_projection"), Mapping) else None
    if isinstance(input_window, Mapping):
        try:
            start_frame, end_frame = int(input_window["start_frame"]), int(input_window["end_frame"])
            start_seconds, end_seconds = start_frame / float(fps), end_frame / float(fps)
        except (KeyError, TypeError, ValueError, ZeroDivisionError):
            start_seconds, end_seconds = 0.0, float(provenance.get("duration_frames") or 0) / float(fps)
    else:
        sampling = frame_index.get("sampling") if isinstance(frame_index.get("sampling"), Mapping) else {}
        bounds = sampling.get("range") if isinstance(sampling.get("range"), (list, tuple)) else None
        start_seconds = float(bounds[0]) if bounds and len(bounds) == 2 else 0.0
        end_seconds = float(bounds[1]) if bounds and len(bounds) == 2 else float(provenance.get("duration_frames") or 0) / float(fps)
    if end_seconds <= start_seconds:
        end_seconds = start_seconds + 1.0

    cards = sorted(cards, key=lambda card: (float(card.get("time_seconds") or 0.0), str(card.get("id") or "")))
    page_size = max(1, int(options.get("page_size") or 50))
    width, lane_left, lane_right = 1500, 24, 1476
    header, ruler_y = 144, 128
    title_font, font, small_font = _png_font(20), _png_font(16), _png_font(13)
    caption_font = _png_font(14)
    image_height = 202
    paths: list[Path] = []
    for page, offset in enumerate(range(0, len(cards), page_size), 1):
        group = cards[offset:offset + page_size]
        image = Image.new("RGB", (width, header + image_height + _PNG_AUDIO_HEIGHT + 96), "#111827")
        draw = ImageDraw.Draw(image)
        timeline_name = str(provenance.get("timeline_name") or provenance.get("timeline_id") or "timeline")
        render_id = str(provenance.get("render_run_id") or "managed render")
        _png_draw_text(draw, (16, 10), f"{timeline_name} · page {page} · time-anchored rendered samples", title_font, fill="white")
        _png_draw_text(draw, (16, 36), f"Render {render_id} · each card begins at its exact sample time", small_font, fill="#acbbcb")
        _png_draw_text(draw, (16, 58), "Shared ruler: sampled output above; continuous canonical input lanes below. Cards are point samples, not clip durations.", small_font, fill="#acbbcb")
        _png_draw_text(draw, (16, 82), "Teal = exact sample guide · card boundaries line up with the same time coordinates as the input lanes.", small_font, fill="#8ce0d0")
        draw.line((lane_left, ruler_y, lane_right, ruler_y), fill="#6edac7", width=1)
        tick_count = 4
        for tick in range(tick_count + 1):
            fraction = tick / tick_count
            x = lane_left + fraction * (lane_right - lane_left)
            draw.line((x, ruler_y - 6, x, ruler_y + 6), fill="#6edac7", width=1)
            seconds = start_seconds + fraction * (end_seconds - start_seconds)
            label = f"{seconds:.1f}s"
            _png_draw_text(draw, (x - _png_text_width(draw, label, small_font) / 2, 106), label, small_font, fill="#8ce0d0")
            draw.line((x, ruler_y + 7, x, image.height - 12), fill="#23343f", width=1)

        audio = frame_index.get("audio") if isinstance(frame_index.get("audio"), Mapping) else None
        for index, card in enumerate(group):
            global_index = offset + index
            try:
                time_seconds = float(card.get("time_seconds") or 0.0)
            except (TypeError, ValueError):
                continue
            x0 = lane_left + max(0.0, min(1.0, (time_seconds - start_seconds) / (end_seconds - start_seconds))) * (lane_right - lane_left)
            next_time = end_seconds
            if global_index + 1 < len(cards):
                try:
                    next_time = float(cards[global_index + 1].get("time_seconds") or end_seconds)
                except (TypeError, ValueError):
                    pass
            x1 = lane_left + max(0.0, min(1.0, (next_time - start_seconds) / (end_seconds - start_seconds))) * (lane_right - lane_left)
            box_left = int(round(x0))
            box_right = int(round(max(x0 + 4, x1 - 6)))
            box_width = max(4, box_right - box_left)
            box_y = header
            draw.rounded_rectangle((box_left, box_y, box_right, image.height - 10), radius=6, fill="#162630", outline="#405769", width=1)
            time_label = str(card.get("time_label") or f"{time_seconds:.3f}s")
            _png_draw_text(draw, (box_left + 8, box_y + 8), time_label, font, fill="#8ce0d0")
            shot_label = _png_shot_label(card)
            shot_lines, shot_excerpt = _png_bounded_lines(draw, shot_label, small_font, max(20, box_width - 100), 1)
            if shot_excerpt:
                shot_lines[-1] = _png_ellipsis(draw, shot_lines[-1], small_font, max(20, box_width - 100))
            if shot_lines:
                _png_draw_text(draw, (box_left + 84, box_y + 10), shot_lines[0], small_font, fill="#ebf2f5")
            frame_path = pack_root / str(card.get("image") or "")
            image_top = box_y + 30
            if frame_path.is_file() and box_width > 30:
                try:
                    with Image.open(frame_path) as source:
                        frame = source.convert("RGB")
                        frame.thumbnail((max(1, box_width - 10), image_height))
                        image.paste(frame, (box_left + max(5, (box_width - frame.width) // 2), image_top))
                except (OSError, ValueError):
                    pass
            waveform = _png_waveform_for_card(audio, card) if audio is not None else None
            if waveform is not None and box_width > 80:
                wave_y = box_y + image_height + 32
                wave_left, wave_right = box_left + 8, box_right - 8
                draw.rounded_rectangle((wave_left, wave_y, wave_right, wave_y + _PNG_AUDIO_HEIGHT), radius=4, fill="#101c24", outline="#2e4a58", width=1)
                center = wave_y + _PNG_AUDIO_HEIGHT // 2
                draw.line((wave_left + 4, center, wave_right - 4, center), fill="#31505e", width=1)
                amplitudes = waveform.get("display_amplitudes") or waveform.get("amplitudes") or []
                inner = max(1, wave_right - wave_left - 10)
                for bar_index, amplitude in enumerate(amplitudes):
                    bar_x = wave_left + 5 + inner * (bar_index + 0.5) / len(amplitudes)
                    bar_height = max(2, int(round(float(amplitude) * (_PNG_AUDIO_HEIGHT - 8) / 2))) if amplitude else 0
                    if bar_height:
                        draw.line((bar_x, center - bar_height, bar_x, center + bar_height), fill="#8ff6dd", width=4)
                cursor_x = wave_left + 5 + inner * float(waveform.get("cursor") or 0.0)
                draw.line((cursor_x, wave_y + 3, cursor_x, wave_y + _PNG_AUDIO_HEIGHT - 3), fill="#ffc276", width=2)
            # Planner output deliberately keeps ``scripts`` as complete shot
            # context, but populates ``display_scripts`` with each untimed
            # occurrence only on its first captured card.  Do not fall back
            # to the full context when that field is present and empty, or a
            # coarse five-card sample will print the same script five times.
            if card.get("captions"):
                text_items = card.get("captions") or []
            elif "display_scripts" in card:
                text_items = card.get("display_scripts") or []
            else:
                text_items = card.get("scripts") or []
            text = " ".join(str(item.get("canonical_text") or item.get("text") or "") for item in text_items if isinstance(item, Mapping)).strip()
            if text and box_width > 100:
                lines, _ = _png_bounded_lines(draw, f"“{text[:240]}”", caption_font, max(20, box_width - 16), 2)
                for line_index, line in enumerate(lines):
                    _png_draw_text(draw, (box_left + 8, box_y + image_height + _PNG_AUDIO_HEIGHT + 50 + line_index * 17), line, caption_font, fill="#e5e7eb")
        path = pack_root / f"time-anchored-{page:03d}.png"
        image.save(path)
        paths.append(path)
    return paths


def _compose_unified_panels(
    output_path: Path, input_paths: list[Path], *, pack_root: Path,
    time_bounds: tuple[float, float] = (0.0, 20.0),
) -> Path:
    """Compose compact output/input bodies under one shared timeline ruler."""
    from PIL import Image, ImageDraw
    from .filmstrip_cards import _png_draw_text, _png_font, _png_text_width

    with Image.open(output_path) as source:
        output = source.convert("RGB")
    input_images: list[Image.Image] = []
    try:
        for path in input_paths:
            with Image.open(path) as source:
                input_images.append(source.convert("RGB"))
        if not input_images:
            return output_path
        width = max(output.width, *(image.width for image in input_images))
        # Both renderers use a 144px chrome/ruler prefix. Cropping it here
        # leaves the content bodies on one coordinate system while the
        # unified surface supplies the only title, legend, and ruler.
        output_body = output.crop((0, min(144, output.height), output.width, output.height))
        input_bodies = [image.crop((0, min(144, image.height), image.width, image.height)) for image in input_images]
        panels = [output_body, *input_bodies]
        scaled: list[Image.Image] = []
        for panel in panels:
            if panel.width == width:
                scaled.append(panel)
            else:
                height = max(1, round(panel.height * width / panel.width))
                scaled.append(panel.resize((width, height), Image.Resampling.LANCZOS))
        header, ruler_y, section_gap = 120, 92, 28
        section_label_height = 28
        body_height = sum(panel.height for panel in scaled) + section_label_height * len(scaled) + section_gap * (len(scaled) - 1)
        sheet = Image.new("RGB", (width, header + body_height), "#111827")
        draw = ImageDraw.Draw(sheet)
        font, small = _png_font(20), _png_font(13)
        _png_draw_text(draw, (16, 10), "Synchronized timeline", font, fill="white")
        _png_draw_text(draw, (16, 38), "Rendered samples and canonical input layers share one time axis.", small, fill="#acbbcb")
        _png_draw_text(draw, (16, 62), "Cards are exact point samples; input blocks show duration. Muted/crossed tiles mean source preview unavailable.", small, fill="#acbbcb")
        draw.line((24, ruler_y, width - 24, ruler_y), fill="#6edac7", width=1)
        # The child panels already carry the same vertical guide coordinates;
        # repeat only the shared ruler ticks here, never a second ruler.
        start_seconds, end_seconds = time_bounds
        if end_seconds <= start_seconds:
            end_seconds = start_seconds + 1.0
        for tick in range(5):
            fraction = tick / 4
            x = 24 + fraction * (width - 48)
            draw.line((x, ruler_y - 6, x, ruler_y + 6), fill="#6edac7", width=1)
            label = f"{start_seconds + fraction * (end_seconds - start_seconds):.1f}s"
            _png_draw_text(draw, (x - _png_text_width(draw, label, small) / 2, 100), label, small, fill="#8ce0d0")
        y = header
        labels = ["OUTPUT · rendered samples", "INPUTS · canonical timeline layers"]
        for index, panel in enumerate(scaled):
            _png_draw_text(draw, (24, y + 4), labels[0] if index == 0 else labels[1], small, fill="#ebf2f5")
            y += section_label_height
            sheet.paste(panel, (0, y))
            y += panel.height
            if index < len(scaled) - 1:
                draw.rectangle((16, y, width - 16, y + section_gap), fill="#0b1118")
                draw.line((24, y + section_gap // 2, width - 24, y + section_gap // 2), fill="#6edac7", width=2)
                y += section_gap
        target = pack_root / output_path.name.replace("time-anchored-", "filmstrip-", 1)
        sheet.save(target)
        return target
    finally:
        output.close()
        for image in input_images:
            image.close()


def _paired_projection(projection: Mapping[str, object], start_frame: int, end_frame: int) -> dict:
    """Return one row's clipped input lanes without changing the frozen index."""
    value = deepcopy(projection)
    window = dict(value.get("window") or {})
    window["start_frame"], window["end_frame"] = start_frame, end_frame
    value["window"] = window
    for track in value.get("tracks") or []:
        clips = []
        for clip in track.get("clips") or []:
            raw_start, raw_end = clip.get("window", [0, 0])
            clipped_start, clipped_end = max(start_frame, int(raw_start)), min(end_frame, int(raw_end))
            if clipped_end <= clipped_start:
                continue
            clip["window"] = [clipped_start, clipped_end]
            clips.append(clip)
        track["clips"] = clips
    return value


def _active_paired_projection(projection: Mapping[str, object]) -> dict:
    """Keep only lanes with a placement in this paired-row window.

    The frozen ``input_projection`` remains untouched in ``frame-index.json``;
    this presentation copy prevents a canonical-but-empty track from creating
    a large blank band under an otherwise useful output row.
    """
    value = deepcopy(projection)
    tracks = [track for track in value.get("tracks") or []
              if isinstance(track, Mapping) and track.get("clips")]
    active_ids = {str(track.get("track_id")) for track in tracks}
    value["tracks"] = tracks
    bands = []
    for band in value.get("track_bands") or []:
        ids = [str(item) for item in band.get("track_ids") or [] if str(item) in active_ids]
        if ids:
            bands.append(dict(band, track_ids=ids))
    value["track_bands"] = bands
    return value


def _render_paired_rows(
    frame_index: Mapping[str, object], pack_root: Path, *, options: Mapping[str, object],
    snapshot: Mapping[str, object], input_projection: Mapping[str, object],
) -> tuple[list[str], list[dict], list[str]]:
    """Render five-card rows with the relevant input lanes directly below.

    Each row owns a half-open time window.  Card x positions and the input
    placements both use that same window, so irregular samples remain honest
    instead of being laid out as an unrelated grid.
    """
    from PIL import Image, ImageDraw
    from .filmstrip_cards import (
        _png_bounded_lines, _png_draw_text, _png_ellipsis, _png_font,
        _png_shot_label, _png_text_width, _png_waveform_for_card,
        _png_card_metrics, _png_frame_aspect, _PNG_CARD_WIDTH, _PNG_IMAGE_HEIGHT, _PNG_AUDIO_HEIGHT,
        _PNG_PAGE_WIDTH_STRIDE, _word_caption_text,
        _PNG_TEXT_LINE_HEIGHT,
    )

    cards = [card for card in frame_index.get("cards") or [] if isinstance(card, Mapping)]
    if not cards:
        return [], [], []
    cards = sorted(cards, key=lambda card: (float(card.get("time_seconds") or 0), str(card.get("id") or "")))
    provenance = frame_index.get("provenance") if isinstance(frame_index.get("provenance"), Mapping) else {}
    raw_fps = provenance.get("fps_rational") or [30, 1]
    fps = Fraction(int(raw_fps[0]), int(raw_fps[1]))
    window = input_projection.get("window") if isinstance(input_projection.get("window"), Mapping) else {}
    start_frame, end_frame = int(window.get("start_frame", 0)), int(window.get("end_frame", 0))
    start_seconds, end_seconds = start_frame / float(fps), end_frame / float(fps)
    columns = max(1, int(options.get("columns") or 5))
    # Keep the normal paired view to one readable row.  An explicit
    # ``--page-size`` remains an opt-in for denser pages (up to two rows),
    # while output-only/input-only views retain their existing pagination.
    page_size_explicit = bool(options.get("page_size_explicit", True))
    requested_page_size = max(1, int(options.get("page_size") or 50))
    page_size = (
        min(10, max(columns * 2, columns), requested_page_size)
        if page_size_explicit else columns
    )
    width = columns * _PNG_PAGE_WIDTH_STRIDE + 24
    lane_left, lane_right = 16, width - 16
    title_font, timestamp_font = _png_font(20), _png_font(14)
    name_font, script_font = _png_font(18), _png_font(16)
    small_font = _png_font(12)
    image_height = 200
    measure = ImageDraw.Draw(Image.new("RGB", (1, 1)))
    audio = frame_index.get("audio") if isinstance(frame_index.get("audio"), Mapping) else None
    show_text = "text" in set(frame_index.get("components") or ("output", "text", "audio"))
    show_audio = "audio" in set(frame_index.get("components") or ("output", "text", "audio"))
    rows_meta: list[dict] = []
    output_paths: list[str] = []
    standalone_paths: list[str] = []
    timeline_name = str(provenance.get("timeline_name") or provenance.get("timeline_id") or "timeline")
    render_id = str(provenance.get("render_run_id") or "managed render")

    def row_bounds(row_cards, global_start_index):
        first_time = max(start_seconds, float(row_cards[0].get("time_seconds") or start_seconds))
        next_index = global_start_index + len(row_cards)
        next_time = end_seconds
        if next_index < len(cards):
            next_time = min(end_seconds, float(cards[next_index].get("time_seconds") or end_seconds))
        if next_time <= first_time:
            next_time = end_seconds if end_seconds > first_time else first_time + 1 / float(fps)
        return first_time, next_time

    for page, offset in enumerate(range(0, len(cards), page_size), 1):
        group = cards[offset:offset + page_size]
        page_rows = [group[i:i + columns] for i in range(0, len(group), columns)]
        rendered_rows: list[Image.Image] = []
        page_row_meta: list[dict] = []
        for local_row, row_cards in enumerate(page_rows):
            global_start_index = offset + local_row * columns
            row_start, row_end = row_bounds(row_cards, global_start_index)
            row_width = row_end - row_start
            # Work out the actual card widths before measuring the body.  The
            # previous fixed 200px preview slot left a visible strip of empty
            # background under ordinary 16:9 stills (their fitted height is
            # about 180px at this width), separating the image from its
            # waveform and caption.  Size the shared row slot to the narrowest
            # card's real preview instead: sparse/full-width rows stay large,
            # while dense rows no longer reserve blank vertical space.
            card_widths: list[int] = []
            for index, card in enumerate(row_cards):
                time_value = float(card.get("time_seconds") or 0)
                card_start = max(row_start, min(row_end, time_value))
                next_time = row_end
                if index + 1 < len(row_cards):
                    try:
                        candidate = float(row_cards[index + 1].get("time_seconds") or row_end)
                        if candidate > time_value:
                            next_time = min(row_end, candidate)
                    except (TypeError, ValueError):
                        pass
                if next_time <= card_start:
                    next_time = min(row_end, card_start + row_width / max(1, len(row_cards)))
                left_fraction = max(0.0, min(1.0, (card_start - row_start) / row_width))
                right_fraction = max(left_fraction, min(1.0, (next_time - row_start) / row_width))
                box_left = int(round(lane_left + left_fraction * (lane_right - lane_left) + 4))
                box_right = int(round(lane_left + right_fraction * (lane_right - lane_left) - 4))
                card_widths.append(max(32, min(width - 8, box_right) - box_left))
            min_card_width = min(card_widths, default=width - 32)
            # The preview takes the frame's own aspect (the tallest frame in the
            # row, so none is cropped) at the narrowest card's width.
            row_aspect = max((_png_frame_aspect(pack_root, card) for card in row_cards), default=9 / 16)
            image_height = min(320, max(96, round((min_card_width - 10) * row_aspect)))
            layouts = [_png_card_metrics(measure, card, name_font, timestamp_font, script_font, audio if show_audio else None, show_text=show_text, image_height=image_height) for card in row_cards]
            # Header (time + label) is fixed at 54px in this row; the body is
            # sized from the tallest card's real preview, waveform and text.
            card_body_height = 54 + max((layout["body_height"] for layout in layouts), default=200)
            row_projection = _active_paired_projection(
                _paired_projection(input_projection, round(row_start * float(fps)), round(row_end * float(fps)))
            )
            temp_prefix = f".paired-input-{page:03d}-{len(page_row_meta):03d}"
            input_paths = (_render_input_projection_png(
                row_projection, snapshot, pack_root, width=width, filename_prefix=temp_prefix
            ) if row_projection.get("tracks") else [])
            body_images = []
            for input_path in input_paths:
                with Image.open(input_path) as source:
                    body_images.append(source.convert("RGB").crop((0, 144, source.width, source.height)))
                input_path.unlink(missing_ok=True)
            input_height = sum(image.height for image in body_images) + 34 * max(0, len(body_images) - 1)
            input_label_height = 26 if body_images else 0
            input_bottom_gap = 20 if body_images else 0
            row_height = 52 + card_body_height + 18 + input_label_height + input_height + input_bottom_gap
            row_image = Image.new("RGB", (width, row_height), "#111827")
            draw = ImageDraw.Draw(row_image)
            _png_draw_text(draw, (16, 8), f"{row_start:.3f}–{row_end:.3f}s", title_font, fill="white")
            _png_draw_text(draw, (width - 330, 12), f"{len(row_cards)} samples · row {len(rows_meta) + 1}", small_font, fill="#acbbcb")
            ruler_y = 43
            draw.line((lane_left, ruler_y, lane_right, ruler_y), fill="#6edac7", width=1)
            for tick in range(5):
                fraction = tick / 4
                x = lane_left + fraction * (lane_right - lane_left)
                draw.line((x, ruler_y - 5, x, ruler_y + 5), fill="#6edac7", width=1)
                label = f"{row_start + fraction * row_width:.1f}s"
                _png_draw_text(draw, (x - _png_text_width(draw, label, small_font) / 2, 25), label, small_font, fill="#8ce0d0")
            for index, (card, layout) in enumerate(zip(row_cards, layouts)):
                time_value = float(card.get("time_seconds") or 0)
                # A card owns the interval until the next sample (or the row
                # end). This fills a lone/sparse row while keeping every edge
                # truthful on the shared time axis.
                card_start = max(row_start, min(row_end, time_value))
                next_time = row_end
                if index + 1 < len(row_cards):
                    try:
                        candidate = float(row_cards[index + 1].get("time_seconds") or row_end)
                        if candidate > time_value:
                            next_time = min(row_end, candidate)
                    except (TypeError, ValueError):
                        pass
                if next_time <= card_start:
                    fallback = row_width / max(1, len(row_cards))
                    next_time = min(row_end, card_start + fallback)
                left_fraction = max(0.0, min(1.0, (card_start - row_start) / row_width))
                right_fraction = max(left_fraction, min(1.0, (next_time - row_start) / row_width))
                box_left = int(round(lane_left + left_fraction * (lane_right - lane_left) + 4))
                box_right = int(round(lane_left + right_fraction * (lane_right - lane_left) - 4))
                box_right = min(width - 8, box_right)
                box_right = max(box_left + 32, box_right)
                box_y = 52
                draw.rounded_rectangle((box_left, box_y, box_right, box_y + card_body_height), radius=6, fill="#162630", outline="#405769", width=1)
                _png_draw_text(draw, (box_left + 8, box_y + 8), str(card.get("time_label") or f"{time_value:.3f}s"), timestamp_font, fill="#8ce0d0")
                label = _png_shot_label(card)
                label_lines, _ = _png_bounded_lines(draw, label, name_font, max(20, box_right - box_left - 16), 1)
                if label_lines:
                    _png_draw_text(draw, (box_left + 8, box_y + 28), label_lines[0], name_font, fill="white")
                image_path = pack_root / str(card.get("image") or "")
                image_top = box_y + 54
                if image_path.is_file():
                    try:
                        with Image.open(image_path) as source:
                            thumb = source.convert("RGB")
                            thumb.thumbnail((max(1, box_right - box_left - 10), image_height))
                            row_image.paste(thumb, (box_left + (box_right - box_left - thumb.width) // 2, image_top))
                    except (OSError, ValueError):
                        pass
                if layout.get("waveform") is not None:
                    wave_y = image_top + image_height + 5
                    wave_left, wave_right = box_left + 6, box_right - 6
                    if wave_right > wave_left + 12:
                        draw.rounded_rectangle(
                            (wave_left, wave_y, wave_right, wave_y + _PNG_AUDIO_HEIGHT),
                            radius=5, fill="#0d1a22", outline="#467486", width=2,
                        )
                        center = wave_y + _PNG_AUDIO_HEIGHT // 2
                        draw.line(
                            (wave_left + 5, center, wave_right - 5, center),
                            fill="#476b76", width=2,
                        )
                        amplitudes = (
                            layout["waveform"].get("display_amplitudes")
                            or layout["waveform"].get("amplitudes") or []
                        )
                        inner_width = max(1, wave_right - wave_left - 12)
                        for bar_index, amplitude in enumerate(amplitudes):
                            if amplitude:
                                bx = wave_left + 6 + inner_width * (bar_index + .5) / max(1, len(amplitudes))
                                bh = max(2, int(float(amplitude) * (_PNG_AUDIO_HEIGHT - 10) / 2))
                                draw.line((bx, center - bh, bx, center + bh), fill="#8ff6dd", width=4)
                        cursor = float(layout["waveform"].get("cursor") or 0.0)
                        cursor_x = wave_left + 6 + inner_width * cursor
                        draw.line(
                            (cursor_x, wave_y + 3, cursor_x, wave_y + _PNG_AUDIO_HEIGHT - 3),
                            fill="#ffc276", width=2,
                        )
                if card.get("captions"):
                    text_items = card["captions"]
                else:
                    # Word-aligned VO text outranks the once-per-occurrence shot script.
                    words = _word_caption_text(card)
                    text_items = [{"text": words}] if words else (card.get("display_scripts") or [])
                text = " ".join(str(item.get("canonical_text") or item.get("text") or "") for item in text_items if isinstance(item, Mapping)).strip()
                if show_text and text:
                    # Give the caption panel enough lines to carry the actual
                    # spoken copy.  Two lines made longer shot text look
                    # missing even though the card had unused vertical room.
                    max_lines = 4 if box_right - box_left >= 240 else 3
                    lines, _ = _png_bounded_lines(draw, f'“{text[:240]}”', script_font, max(20, box_right - box_left - 16), max_lines)
                    # The body reserves a text panel below the preview and
                    # waveform. Center the caption block in that panel so a
                    # short transcript is neither bottom- nor left-biased.
                    audio_extra = layout.get("audio_extra") or 0
                    text_area_top = box_y + 54 + image_height + audio_extra + 8
                    text_area_height = max(0, card_body_height - (54 + image_height + audio_extra + 8))
                    text_block_height = _PNG_TEXT_LINE_HEIGHT * len(lines)
                    text_top = text_area_top + max(0, (text_area_height - text_block_height) // 2)
                    for line_index, line in enumerate(lines):
                        line_width = _png_text_width(draw, line, script_font)
                        text_x = box_left + 8 + max(0, (box_right - box_left - 16 - line_width) / 2)
                        _png_draw_text(draw, (text_x, text_top + line_index * _PNG_TEXT_LINE_HEIGHT), line, script_font, fill="#e5e7eb")
            y = 52 + card_body_height + 18
            if body_images:
                _png_draw_text(draw, (16, y), "INPUTS · placements active in this row", small_font, fill="#ebf2f5")
                y += 26
                for body_index, body in enumerate(body_images):
                    row_image.paste(body, (0, y))
                    y += body.height
                    if body_index < len(body_images) - 1:
                        draw.line((16, y + 16, width - 16, y + 16), fill="#6edac7", width=2)
                        y += 34
            rendered_rows.append(row_image)
            page_row_meta.append({
                "index": len(rows_meta) + 1,
                "card_ids": [str(card.get("id")) for card in row_cards],
                "time_range": [row_start, row_end],
                "start_seconds": row_start, "end_seconds": row_end,
                "output_card_count": len(row_cards),
                "input_tracks": [str(track.get("track_id")) for track in row_projection.get("tracks") or [] if track.get("clips")],
            })
            rows_meta.append(page_row_meta[-1])
        page_height = sum(row.height for row in rendered_rows) + 28 * max(0, len(rendered_rows) - 1) + 86
        sheet = Image.new("RGB", (width, page_height), "#0b1118")
        draw = ImageDraw.Draw(sheet)
        _png_draw_text(draw, (16, 12), f"{timeline_name} · page {page} · paired timeline", title_font, fill="white")
        _png_draw_text(draw, (16, 40), f"Render {render_id} · {columns} columns · output samples with synchronized input lanes", small_font, fill="#acbbcb")
        y = 78
        for row_index, row in enumerate(rendered_rows):
            sheet.paste(row, (0, y))
            y += row.height
            if row_index < len(rendered_rows) - 1:
                draw.line((16, y + 14, width - 16, y + 14), fill="#6edac7", width=2)
                y += 28
        path = pack_root / f"filmstrip-{page:03d}.png"
        sheet.save(path)
        output_paths.append(str(path))
    return output_paths, rows_meta, standalone_paths


def _compose_synchronized_surface(
    output_pages: list[str], input_pages: list[Path], *, pack_root: Path,
    frame_index: Mapping[str, object] | None = None,
    options: Mapping[str, object] | None = None,
) -> list[str]:
    """Put rendered samples and canonical input lanes on one static surface.

    The primary path is a paired-row surface: each row's sampled output cards
    and clipped input lanes share one linear time window.  A legacy fallback
    remains for deliberately skeletal/unit-test projections that have no
    declared tracks.
    """
    if not output_pages or not input_pages:
        return output_pages
    if (frame_index is not None
            and isinstance(frame_index.get("input_projection"), Mapping)
            and (frame_index.get("input_projection") or {}).get("tracks")):
        paired, rows, _ = _render_paired_rows(
            frame_index, pack_root, options=options or {},
            snapshot=frame_index.get("provenance") or {},
            input_projection=frame_index["input_projection"],
        )
        if isinstance(frame_index, dict):
            visible_components = ["output"]
            for component in ("text", "audio"):
                if component in set(frame_index.get("components") or ()):
                    visible_components.append(component)
            visible_components.append("inputs")
            effective_columns = max(1, int((options or {}).get("columns") or 5))
            effective_page_size_explicit = bool((options or {}).get("page_size_explicit", True))
            effective_requested_page_size = max(1, int((options or {}).get("page_size") or 50))
            effective_page_size = (
                min(10, effective_columns * 2, effective_requested_page_size)
                if effective_page_size_explicit else effective_columns
            )
            frame_index["static_surface"] = {
                "schema": "astrid.timeline-static-surface.v2",
                "mode": "paired_rows",
                "components": visible_components,
                "columns": effective_columns,
                "page_size": effective_page_size,
                "page_count": len(paired),
                "page_size_policy": (
                    "one row of paired cards by default; explicit page_size may opt into "
                    "up to two rows (max 10 cards)"
                ),
                "canonical_input_tracks": [
                    str(track.get("track_id"))
                    for track in ((frame_index.get("input_projection") or {}).get("tracks") or [])
                    if isinstance(track, Mapping)
                ],
                "rows": rows,
                "row_count": len(rows),
                "axis": "linear_half_open_seconds",
                "navigation": {
                    "pages": "Open filmstrip-001.png, filmstrip-002.png, ... in order.",
                    "drill_down": "Rerun with --range START..END --every 0.25 for a readable close-up.",
                    "selectors": "Use --shot first or --shot N, plus --track TRACK, to focus a review.",
                },
                "standalone_input_pages": [Path(path).name for path in input_pages],
            }
            # Keep the sampling coverage contract intact, but expose the
            # effective static pagination beside it so consumers do not infer
            # paired page count from the standalone contact-sheet defaults.
            coverage = frame_index.setdefault("coverage", {})
            if isinstance(coverage, dict):
                coverage["static_page_size"] = effective_page_size
                coverage["static_page_count"] = len(paired)
                coverage["static_layout"] = "paired_rows"
            frame_index["static_surface"]["pages"] = [Path(path).name for path in paired]
        return paired or output_pages
    from PIL import Image, ImageDraw

    input_images: list[Image.Image] = []
    try:
        for path in input_pages:
            with Image.open(path) as source:
                input_images.append(source.convert("RGB"))
        if not input_images:
            return output_pages

        raw_output_paths: list[Path] = []
        for output_path_value in output_pages:
            output_path = Path(output_path_value)
            if not output_path.is_file():
                continue
            raw_path = output_path.with_name(f"rendered-{output_path.name}")
            output_path.replace(raw_path)
            raw_output_paths.append(raw_path)

        anchored = _render_time_anchored_output(
            frame_index, pack_root, options=options or {},
        ) if frame_index is not None else []
        surface_paths = anchored or raw_output_paths
        composed_paths: list[str] = []
        time_bounds = (0.0, 20.0)
        if frame_index is not None:
            projection = frame_index.get("input_projection") if isinstance(frame_index.get("input_projection"), Mapping) else None
            window = projection.get("window") if isinstance(projection, Mapping) else None
            provenance = frame_index.get("provenance") if isinstance(frame_index.get("provenance"), Mapping) else None
            raw_fps = provenance.get("fps_rational") if isinstance(provenance, Mapping) else None
            try:
                fps = Fraction(int(raw_fps[0]), int(raw_fps[1])) if raw_fps else Fraction(30, 1)
                time_bounds = (int(window["start_frame"]) / float(fps), int(window["end_frame"]) / float(fps)) if isinstance(window, Mapping) else time_bounds
            except (KeyError, TypeError, ValueError, ZeroDivisionError, IndexError):
                pass
        for output_path in surface_paths:
            if not output_path.is_file():
                continue
            if anchored:
                target = _compose_unified_panels(
                    output_path, input_pages, pack_root=pack_root,
                    time_bounds=time_bounds,
                )
                output_path.unlink(missing_ok=True)
                composed_paths.append(str(target))
                continue
            rendered = None
            target_path = output_path
            try:
                with Image.open(output_path) as source:
                    rendered = source.convert("RGB")
                width = max(rendered.width, *(image.width for image in input_images))
                panels: list[Image.Image] = [rendered]
                panels.extend(input_images)
                # Scale the time-based input lanes to the rendered page width
                # so the shared window occupies the same horizontal surface.
                scaled: list[Image.Image] = []
                for panel in panels:
                    if panel.width == width:
                        scaled.append(panel)
                    else:
                        height = max(1, round(panel.height * width / panel.width))
                        scaled.append(panel.resize((width, height), Image.Resampling.LANCZOS))
                separator = 20
                total_height = sum(panel.height for panel in scaled) + separator * (len(scaled) - 1)
                sheet = Image.new("RGB", (width, total_height), "#0b1118")
                draw = ImageDraw.Draw(sheet)
                y = 0
                for index, panel in enumerate(scaled):
                    sheet.paste(panel, (0, y))
                    y += panel.height
                    if index < len(scaled) - 1:
                        draw.rectangle((0, y, width, y + separator), fill="#0b1118")
                        draw.line((16, y + separator // 2, width - 16, y + separator // 2), fill="#6edac7", width=2)
                        y += separator
                if output_path.name.startswith("time-anchored-"):
                    target_path = pack_root / output_path.name.replace("time-anchored-", "filmstrip-", 1)
                elif output_path.name.startswith("rendered-"):
                    target_path = pack_root / output_path.name.replace("rendered-", "", 1)
                sheet.save(target_path)
                if output_path.name.startswith("time-anchored-"):
                    output_path.unlink(missing_ok=True)
            finally:
                if rendered is not None:
                    rendered.close()
            composed_paths.append(str(target_path))
        return composed_paths or output_pages
    finally:
        for image in input_images:
            image.close()


def _materialized_asset_files(registry_path) -> dict[str, str]:
    """``{asset key: local file}`` from the host-materialized registry (audio lanes read these)."""
    if not registry_path:
        return {}
    try:
        registry = json.loads(Path(registry_path).read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        return {}
    assets = registry.get("assets") if isinstance(registry, Mapping) else None
    if not isinstance(assets, Mapping):
        return {}
    return {
        str(key): str(entry["file"])
        for key, entry in assets.items()
        if isinstance(entry, Mapping) and isinstance(entry.get("file"), str) and Path(entry["file"]).is_file()
    }


def execute_filmstrip(args, *, authority=None):
    started_at = time.time()
    if getattr(args, "view", None) == "diff":
        # Diff is a separate immutable-bundle consumer.  Do not enter the
        # filmstrip/composed-capture authority path or consult Runtime/current
        # head state for either revision.
        from .revision_diff import execute_revision_diff

        return execute_revision_diff(args)
    if args.filmstrip_authority:
        authority_value = args.filmstrip_authority
        try:
            parsed_authority = json.loads(authority_value)
        except json.JSONDecodeError:
            authority_path = Path(authority_value).expanduser()
            try:
                parsed_authority = json.loads(authority_path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError) as exc:
                raise ValueError("filmstrip authority must be JSON or a host handoff path") from exc
        if isinstance(parsed_authority, str):
            authority_path = Path(parsed_authority).expanduser()
            try:
                parsed_authority = json.loads(authority_path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError) as exc:
                raise ValueError("filmstrip authority handoff is not valid JSON") from exc
        authority = parsed_authority
    if not isinstance(authority, dict) or authority.get('mode') not in {'filmstrip', 'input_only', 'composed_capture'}:
        raise ValueError('Rendered filmstrips require managed SDK admission.')
    if authority.get('mode') == 'input_only':
        return execute_input_only(args, authority)
    capture_mode = (
        authority.get('mode') == 'composed_capture'
        or authority.get('frame_source') == 'composed_capture'
    )
    snapshot = authority.get('capture_snapshot' if capture_mode else 'filmstrip_snapshot')
    if not isinstance(snapshot, dict) or snapshot.get('project_slug') != args.project_slug:
        raise ValueError('Filmstrip authority does not match the project.')
    video = None if capture_mode else args.rendered_video
    if capture_mode:
        digest = str(authority.get('capture_identity') or snapshot.get('video_digest') or '')
        if not digest:
            raise ValueError('Composed frame capture authority has no composition identity.')
        if authority.get('include_media'):
            video = getattr(args, 'rendered_video', None)
            if video is None or not video.is_file():
                raise ValueError('include_media requires an admitted rendered video attachment.')
            with video.open('rb') as stream:
                attached_digest = 'sha256:' + hashlib.file_digest(stream, 'sha256').hexdigest()
            expected_attached_digest = authority.get('video_digest')
            if attached_digest != expected_attached_digest:
                raise ValueError('Materialized include_media video does not match its admitted digest.')
    else:
        if video is None or not video.is_file():
            raise ValueError('Admitted rendered video was not materialized.')
        with video.open('rb') as stream:
            digest = 'sha256:' + hashlib.file_digest(stream, 'sha256').hexdigest()
        if digest != authority.get('video_digest') or digest != snapshot.get('video_digest'):
            from .inspection_contract import render_status
            status = render_status(
                lifecycle='succeeded',
                output={'available': True, 'digest': digest, 'run_id': snapshot.get('render_run_id')},
                expected_digest=str(authority.get('video_digest') or snapshot.get('video_digest') or ''),
                project=args.project_slug,
            )
            error = ValueError('Materialized rendered video does not match admitted digest.')
            # Preserve a typed status payload for the generic host while retaining
            # ValueError compatibility for standalone executor callers.
            error.details = {'inspection_status': status, 'next_actions': status.get('next_actions', [])}
            raise error
    values = vars(args).copy()
    values['range'] = args.range_value
    options = filmstrip_options(values)
    snapshot = deepcopy(snapshot)
    options["shot"] = resolve_shot_selector(options.get("shot"), snapshot)
    frame_provider = None
    if capture_mode:
        if options.get('include_media') and video is None:
            raise ValueError(
                'include_media is unavailable for composed frame capture without an exact managed video attachment.'
            )
        options['frame_extension'] = 'png'
        from astrid.core.foundation.paths import REPO_ROOT
        from .composed_frame import RemotionFrameProvider
        timeline_path = getattr(args, 'timeline', None)
        assets_path = getattr(args, 'assets_registry', None)
        if timeline_path is None or assets_path is None:
            raise ValueError('Composed frame capture did not receive the materialized timeline and registry.')
        frame_provider = RemotionFrameProvider(
            snapshot,
            timeline_path=timeline_path,
            assets_path=assets_path,
            project_dir=Path(os.environ.get('ASTRID_REMOTION_PROJECT_DIR') or (REPO_ROOT / 'remotion')),
            materialized_root=getattr(args, 'materialized_root', None),
            materialized_objects=getattr(args, 'materialized_objects', None),
        )
    # Older/unit-test authorities may omit the timing envelope.  Real managed
    # renders always carry it; leave incomplete test authorities untouched so
    # their admission checks remain focused on digest verification.
    if not capture_mode and snapshot.get("fps_rational") and snapshot.get("duration_frames"):
        _align_snapshot_to_render(snapshot, video)
    # The frozen authority intentionally omits filesystem locators. The host
    # hands this subprocess the verified, attempt-local object map separately;
    # project it into inspection metadata without changing canonical registry
    # identity or publishing those locators as durable state.
    if isinstance(snapshot.get("registry"), Mapping):
        snapshot.setdefault("metadata", {})["asset_integrity"] = _asset_integrity_from_registry(
            snapshot.get("registry"),
            materialized_objects=getattr(args, "materialized_objects", None),
            materialized_root=getattr(args, "materialized_root", None),
        )
    out_root = args.out.expanduser().resolve()
    if capture_mode:
        analysis = {
            'schema_version': 1, 'analysis_version': 'astrid.audio-analysis.v1',
            'analysis_identity': audio_analysis_identity(digest, None, None, status='not_analyzed'),
            'status': 'not_analyzed', 'render_digest': digest,
            'waveform': {'levels': []}, 'quiet_gaps': [],
            'speech': {'status': 'no_transcript', 'phrases': []},
            'coverage': {'state': 'not_analyzed', 'reason': 'composed frame capture has no audio render'},
        }
    else:
        analysis_settings = authority.get('audio_analysis_settings')
        analysis = _cached_audio(out_root.parent, digest, analysis_settings)
        if analysis is None:
            try:
                analysis = analyze_audio(video, render_digest=digest, settings=analysis_settings)
                _store_audio_cache(out_root.parent, digest, analysis_settings, analysis)
            except AudioAnalysisError as exc:
                # A malformed/unsupported stream is useful evidence, not permission to
                # invent a waveform.  Keep the filmstrip itself usable and make the
                # failure visible in the sidecar/index.
                analysis = {
                    'schema_version': 1, 'analysis_version': 'astrid.audio-analysis.v1',
                    'analysis_identity': audio_analysis_identity(
                        digest, None, analysis_settings, status='analysis_error'
                    ),
                    'status': 'analysis_error', 'render_digest': digest,
                    'error': str(exc), 'waveform': {'levels': []}, 'quiet_gaps': [],
                    'speech': {'status': 'no_transcript', 'phrases': []},
                    'coverage': {'state': 'analysis_error'},
                }
    existing_audio = snapshot.get('audio') if isinstance(snapshot.get('audio'), dict) else {}
    if isinstance(existing_audio.get('speech'), dict):
        analysis['speech'] = existing_audio['speech']
    if 'fps_rational' in snapshot and 'duration_frames' in snapshot:
        snapshot['audio'] = analysis
    pack_root = out_root / 'filmstrip-view'
    if pack_root.exists() and any(pack_root.iterdir()):
        raise ValueError(f'evidence pack output is not empty: {pack_root}')
    try:
        result = build_filmstrip_pack(out_root=pack_root, video_path=video,
                                      snapshot=snapshot, options=options,
                                      frame_provider=frame_provider,
                                      asset_files=_materialized_asset_files(getattr(args, 'assets_registry', None)))
    except BaseException as operation_error:
        if frame_provider is not None:
            try:
                frame_provider.close(force=True)
            except BaseException as cleanup_error:
                raise operation_error from cleanup_error
        raise
    else:
        if frame_provider is not None:
            # Release the provider's lease while the shared owner remains
            # paused behind its bounded idle-expiry timer.
            frame_provider.close()
    if capture_mode and video is not None:
        media_record = result.get('frame_index', {}).get('media')
        if isinstance(media_record, dict):
            media_record['source_digest'] = authority.get('video_digest')
            if authority.get('render_run_id'):
                media_record['render_run_id'] = authority['render_run_id']
    # Keep optional input evidence as synchronized, full-width timeline panels
    # in the static delivery too.  When output and inputs are both selected,
    # compose those panels onto the same public PNG surface while retaining
    # the standalone input bands as auxiliary evidence.
    if 'inputs' in (options.get('components') or ()) and result.get('frame_index', {}).get('input_projection'):
        output_pages = list(result['paths'].get('png') or [])
        input_pages = _render_input_projection_png(
            result['frame_index']['input_projection'], snapshot, pack_root)
        if 'output' in (options.get('components') or ()):
            output_pages = _compose_synchronized_surface(
                output_pages, input_pages, pack_root=pack_root,
                frame_index=result.get('frame_index'), options=options,
            )
        result['paths']['png'] = output_pages + [str(path) for path in input_pages]
        if 'output' in (options.get('components') or ()):
            frame_index_path = pack_root / 'frame-index.json'
            frame_index = result.get('frame_index')
            if isinstance(frame_index, dict):
                surface = frame_index.setdefault('static_surface', {})
                surface.setdefault('mode', 'paired_rows')
                surface.setdefault('components', ['output', 'inputs'])
                surface['standalone_input_pages'] = [path.name for path in input_pages]
                try:
                    persisted_index = compact_render_receipt(frame_index, snapshot, pack_root)
                    attach_frame_addresses(persisted_index, snapshot, frame_index['cards'])
                except (KeyError, TypeError, ValueError):
                    # Keep admission-focused/unit-test builders and historical
                    # minimal fixtures readable; real packs always satisfy the
                    # compact receipt contract and take the bounded path.
                    persisted_index = frame_index
                frame_index_path.write_text(
                    json.dumps(persisted_index, ensure_ascii=True, separators=(',', ':')), encoding='utf-8'
                )
    elif isinstance(result.get('frame_index'), dict):
        # Output-only packs do not pass through the paired-surface rewrite, but
        # they use the same bounded receipt contract.
        try:
            persisted_index = compact_render_receipt(result['frame_index'], snapshot, pack_root)
            attach_frame_addresses(persisted_index, snapshot, result['frame_index']['cards'])
        except (KeyError, TypeError, ValueError):
            persisted_index = result['frame_index']
        (pack_root / 'frame-index.json').write_text(
            json.dumps(persisted_index, ensure_ascii=True, separators=(',', ':')), encoding='utf-8'
        )
    (pack_root / 'render-snapshot.json').write_text(
        json.dumps(snapshot, indent=2, ensure_ascii=False), encoding='utf-8')
    # Timing is measured here, inside the executor, and travels inside the
    # bundle (timing.json) because the host settles only the bundle; the
    # client adds queued_s = started_at - its own request time.
    composed_at = time.time()
    capture_started_at, capture_ended_at = result.get('capture_window', (composed_at, composed_at))
    timing = {
        'started_at': round(started_at, 3),
        'capture_started_at': round(capture_started_at, 3),
        'capture_ended_at': round(capture_ended_at, 3),
        'capture_s': round(capture_ended_at - capture_started_at, 3),
        'compose_s': round(composed_at - capture_ended_at, 3),
        'total_s': round(composed_at - started_at, 3),
        'frames': len(result.get('cards') or []),
        'tiles': ((result.get('frame_index') or {}).get('contact') or {}).get('tiles'),
        'view': options.get('view') or 'filmstrip',
        'resolution': options.get('resolution'),
        'evidence_source': 'composed_capture' if capture_mode else 'render_extract',
    }
    (pack_root / 'timing.json').write_text(json.dumps(timing, sort_keys=True), encoding='utf-8')
    findings = result.get('findings') or []
    if findings:
        (pack_root / 'findings.txt').write_text('\n'.join(findings) + '\n', encoding='utf-8')
    files = sorted(p for p in pack_root.rglob('*') if p.is_file())
    png_paths = [Path(path) for path in result['paths'].get('png') or []]
    primary_png = png_paths[0].relative_to(pack_root).as_posix() if png_paths else None
    entrypoints = {'frames': 'frame-index.json', 'markdown': 'filmstrip.md'}
    if png_paths:
        entrypoints['png'] = primary_png
    manifest = build_manifest(
        kind='timeline_filmstrip', created='1970-01-01T00:00:00Z',
        inputs={'render_run_id': snapshot['render_run_id'], 'video_digest': digest,
                'timeline_id': snapshot['timeline_id'], 'options': options,
                'request': options.get('request'),
                'analysis_identity': analysis.get('analysis_identity'),
                'media': result.get('frame_index', {}).get('media'),
                'audio_sidecar': result.get('frame_index', {}).get('audio_sidecar')},
        outputs=[{'path': p.relative_to(pack_root).as_posix(), 'type': 'file',
                  'role': 'result', 'is_primary': p.relative_to(pack_root).as_posix() == primary_png,
                  'label': p.relative_to(pack_root).as_posix()} for p in files],
        entrypoints=entrypoints,
        timeline_ids=[snapshot['timeline_id']],
        request=options.get('request'),
    )
    manifest_path = pack_root / 'manifest.json'
    write_manifest(manifest_path, manifest)
    bundle = out_root / 'filmstrip-bundle.zip'
    _write_zip_atomic(bundle, pack_root)
    # The generic pack host treats ``{out}/manifest.json`` as the universal
    # result receipt.  The filmstrip's own manifest intentionally lives inside
    # ``filmstrip-view/`` because it is part of the self-contained bundle, so
    # writing only that nested manifest leaves an admitted task queued forever
    # (the host reports "missing result manifest receipt").  Publish a small
    # host receipt at the assigned output root and keep the domain manifest
    # nested and authoritative for offline evidence verification.
    output_contract = _filmstrip_output_contract(
        snapshot,
        options,
        digest,
        result["frame_index"],
    )

    def _receipt_entry(name: str, path: Path, *, role: str = "auxiliary", primary: bool = False) -> dict:
        relative = path.relative_to(out_root).as_posix()
        return {
            "name": name,
            "path": relative,
            "content_hash": "sha256:" + hashlib.sha256(path.read_bytes()).hexdigest(),
            "bytes": path.stat().st_size,
            "role": role,
            "is_primary": primary,
            # Runtime disallows temporary primary outputs.  Keep the existing
            # bundle primary while the auxiliary nested manifest follows the
            # derived-artifact temporary lifecycle.
            "durability": "durable" if primary else "temporary",
            **output_contract,
        }

    # Keep the host receipt small and self-contained.  The bundle is the
    # canonical delivery artifact; it contains the nested manifest, static
    # PNG/Markdown pages, and (when requested) the verified video.
    # Publishing the same large members individually would expand the inline
    # settlement beyond the runtime request limit and duplicate the bundle.
    write_manifest(
        out_root / "manifest.json",
        build_manifest(
            kind="timeline_filmstrip_result",
            created="1970-01-01T00:00:00Z",
            inputs={
                "render_run_id": snapshot["render_run_id"],
                "timeline_id": snapshot["timeline_id"],
                "video_digest": digest,
                "request": options.get("request"),
            },
            request=options.get("request"),
            outputs=[
                _receipt_entry("filmstrip_manifest", pack_root / "manifest.json"),
                _receipt_entry("filmstrip_bundle", bundle, role="result", primary=True),
            ],
        ),
    )
    # Keep the read-only result envelope explicit about the identities that
    # the host can publish to CAS.  These are content locators, not local-path
    # authority and not a mutation receipt.  Entrypoints stay relative to the
    # assigned result root so both the disposable attempt and a rehydrated
    # bundle can resolve them without guessing from filenames.
    host_manifest = out_root / "manifest.json"
    manifest_digest = "sha256:" + hashlib.sha256(manifest_path.read_bytes()).hexdigest()
    bundle_digest = "sha256:" + hashlib.sha256(bundle.read_bytes()).hexdigest()
    host_manifest_digest = "sha256:" + hashlib.sha256(host_manifest.read_bytes()).hexdigest()
    identity = {
        "render": {
            "render_run_id": snapshot["render_run_id"],
            "timeline_id": snapshot["timeline_id"],
            "video_digest": digest,
        },
        "manifest": {
            "kind": "timeline_filmstrip",
            "content_hash": manifest_digest,
        },
        "host_receipt": {
            "kind": "timeline_filmstrip_result",
            "content_hash": host_manifest_digest,
        },
        "bundle": {"content_hash": bundle_digest},
    }
    cas = {
        "filmstrip_manifest": manifest_digest,
        "filmstrip_bundle": bundle_digest,
    }
    if not capture_mode:
        cas["rendered_video"] = digest
    entrypoints = {
        "manifest": "filmstrip-view/manifest.json",
        "frame_index": "filmstrip-view/frame-index.json",
        "bundle": "filmstrip-bundle.zip",
    }
    if primary_png:
        entrypoints["png"] = f"filmstrip-view/{primary_png}"
    entrypoints["markdown"] = "filmstrip-view/filmstrip.md"
    finished_at = time.time()
    timing['compose_s'] = round(finished_at - capture_ended_at, 3)
    timing['total_s'] = round(finished_at - started_at, 3)
    return {'returncode': 0, 'run_root': str(out_root),
            'manifest_path': str(manifest_path), 'timeline_ids': [snapshot['timeline_id']],
            'identity': identity, 'cas': cas, 'entrypoints': entrypoints,
            'request': options.get('request'), 'timing': timing,
            'outputs': {'pack_root': str(pack_root), 'manifest_path': str(manifest_path),
                        'identity': identity, 'cas': cas, 'entrypoints': entrypoints,
                        'request': options.get('request'), 'timing': timing,
                        **result['paths'], 'pages': result['paths']['png'],
                        'primary_page': (result['paths']['png'] or [None])[0],
                        'findings': findings,
                        'filmstrip_bundle': str(bundle)}}
