"""Timeline, metadata, and EDL builders for the hype cut executor.

Extracted from ``run.py``. Contains all
functions that construct the hype.timeline.json, hype.metadata.json,
hype.edl.csv, and associated artefacts.
"""

from __future__ import annotations

import argparse
import csv
import hashlib
from pathlib import Path
from typing import Any

from astrid.pack_api import AuditContext
from astrid.pack_api import AstridError
from astrid.core.foundation.paths import PACKAGE_ROOT
from astrid.pack_api import METADATA_VERSION, AssetRegistry, PipelineMetadata, TimelineConfig, canonical_timeline_config, materialize_output
from astrid.pack_api import utc_now_seconds
from astrid.pack_api import compile_arrangement_plan

_LEGACY_DEFAULT_CLIP_SEC = 4.0

# Effects whose animation arrays should be sourced from the theme/effect
# defaults.json — never from the LLM brief output. The brief's job is content,
# not styling. New branded effects should be added here as they're authored.
_BRANDED_EFFECT_IDS = frozenset({"section-hook", "art-card", "resource-card", "cta-card"})

_TEXT_STYLE_FONT = "Inter, system-ui, sans-serif"


def _theme_default_clip_sec(theme: dict[str, Any] | None) -> float | None:
    pacing = theme.get("pacing") if isinstance(theme, dict) else None
    value = pacing.get("default_clip_sec") if isinstance(pacing, dict) else None
    return float(value) if isinstance(value, (int, float)) else None


def _drop_brand_animation_overrides(effect_id: str, params: dict[str, Any]) -> dict[str, Any]:
    """Remove entrance/exit/sustain keys from params for branded effects.

    For branded effects, the theme's effect defaults.json is authoritative. The LLM
    brief sometimes proposes entrance/exit arrays anyway; we silently drop them
    rather than threading them through to the timeline.
    """
    if effect_id not in _BRANDED_EFFECT_IDS or not params:
        return params
    return {key: value for key, value in params.items() if key not in {"entrance", "exit", "sustain"}}


def _clip_bounds_for_duration(entry: dict[str, Any], duration: float, *, start: float | None = None) -> dict[str, float]:
    start_sec = float(entry["src_start"] if start is None else start)
    source_end = float(entry["src_end"])
    if start_sec < 0 or source_end < start_sec:
        raise AstridError(
            f"Invalid source bounds for pool entry {entry.get('id')!r}: "
            f"{start_sec:.3f}-{source_end:.3f}",
            recovery_command="check pool entry source timestamps and re-run pool_build with corrected bounds",
        )
    source_duration = max(0.0, source_end - start_sec)
    visible_duration = min(source_duration, duration)
    bounds = {"from_": start_sec, "to": start_sec + visible_duration}
    hold = max(0.0, duration - source_duration)
    if hold > 0:
        bounds["hold"] = hold
    return bounds


def _text_style_preset_to_attrs(preset: Any) -> dict[str, Any]:
    if not isinstance(preset, str):
        return {}
    normalized = preset.lower().replace("_", "-")
    if "title" in normalized or "bold" in normalized:
        return {"fontFamily": _TEXT_STYLE_FONT, "fontSize": 64, "color": "#ffffff", "bold": True, "align": "center"}
    if "caption" in normalized:
        return {"fontFamily": _TEXT_STYLE_FONT, "fontSize": 36, "color": "#ffffff", "italic": "italic" in normalized, "align": "center"}
    if "closer" in normalized or "closing" in normalized:
        return {"fontFamily": _TEXT_STYLE_FONT, "fontSize": 48, "color": "#ffffff", "italic": True, "align": "center"}
    return {"fontFamily": _TEXT_STYLE_FONT, "fontSize": 36, "color": "#ffffff", "align": "center"}


def _merged_theme_for_output(theme: dict[str, Any], theme_overrides: dict[str, Any]) -> dict[str, Any]:
    """Apply theme_overrides.visual onto the loaded theme so materialize_output()
    sees the canvas the renderer will actually use (e.g. when the source asset's
    resolution/fps diverges from the brand canvas).
    """
    if not theme_overrides:
        return theme
    visual_override = theme_overrides.get("visual")
    if not isinstance(visual_override, dict):
        return theme
    merged = dict(theme)
    base_visual = dict(theme.get("visual") or {})
    canvas = dict(base_visual.get("canvas") or {})
    canvas_override = visual_override.get("canvas")
    if isinstance(canvas_override, dict):
        canvas.update(canvas_override)
    base_visual["canvas"] = canvas
    merged["visual"] = base_visual
    return merged


def _joined_segment_text(segment_ids: list[int], transcript: list[dict[str, Any]] | None) -> str | None:
    if transcript is None:
        return None
    parts = [
        str(transcript[index].get("text", "")).strip()
        for index in segment_ids
        if 0 <= index < len(transcript)
    ]
    joined = " ".join(part for part in parts if part).strip()
    return joined or None


def _pool_entry_caption_kind(entry: dict[str, Any]) -> str:
    return "dialogue" if entry.get("category") == "dialogue" else "visual"


def _tool_fingerprints(tools_dir: Path) -> dict[str, str]:
    # Short sha1 of each tool's source bytes — enough to detect "same code ran
    # this clip" without leaking filesystem paths or pretending to be semver.
    prints: dict[str, str] = {}
    for name in ("cut.py", "timeline.py", "transcribe.py", "scenes.py", "shots.py",
                 "triage.py", "scene_describe.py", "quote_scout.py", "pool_build.py", "arrange.py",
                 "text_match.py", "llm_clients.py", "quality_zones.py", "refine.py",
                 "inspect_cut.py", "enriched_arrangement.py",
                 "render_remotion.py"):
        path = tools_dir / name
        if path.is_file():
            prints[name] = hashlib.sha1(path.read_bytes()).hexdigest()[:10]
    return prints


def _ref_path_for_metadata(ref: Path | None, out_dir: Path) -> str | None:
    # Store refs relative to out_dir when the file lives inside it, so the
    # metadata moves with the run directory. Fall back to absolute otherwise.
    if ref is None:
        return None
    resolved_ref = ref.resolve()
    resolved_out = out_dir.resolve()
    try:
        return str(resolved_ref.relative_to(resolved_out))
    except ValueError:
        return str(resolved_ref)


def build_multitrack_timeline(
    arrangement: dict[str, Any],
    pool: dict[str, Any],
    registry: AssetRegistry,
    primary_asset: str | None,
    compiled_plan: list[dict[str, Any]] | None = None,
    theme: dict[str, Any] | None = None,
    theme_slug: str | None = None,
) -> TimelineConfig:
    """Build a standalone Remotion timeline from arrangement + pool data.

    Returns a ``TimelineConfig`` that is serialized as ``hype.timeline.json`` — a
    standalone attempt output, not a project-timeline container. A caller may
    deliberately compile it into a detached authoring candidate for canonical
    parent-composition publication; the worker never opens timeline authority.
    """
    clips: list[dict[str, Any]] = []
    if primary_asset is None and "rant" in registry["assets"]:
        clips.append(
            {
                "id": "clip_a_rant",
                "at": 0,
                "track": "a1",
                "clipType": "media",
                "asset": "rant",
                "from": 0,
                "to": float(registry["assets"]["rant"]["duration"]),
            }
        )
    for plan in compiled_plan or compile_arrangement_plan(arrangement, pool):
        order = plan["order"]
        at = plan["at"]
        duration = plan["duration"]
        audio_entry = plan["audio_entry"]
        overlay_entry = plan["overlay_entry"]
        if audio_entry is not None:
            clips.append(
                {
                    "id": f"clip_a_{order}",
                    "source_uuid": plan["uuid"],
                    "at": at,
                    "track": "a1",
                    "clipType": "media",
                    "asset": audio_entry["asset"],
                    **_clip_bounds_for_duration(audio_entry, duration, start=plan["audio_trim_start"]),
                }
            )
            # v1 shows the speaker — the audio source at the same timestamp.
            clips.append(
                {
                    "id": f"clip_v1_{order}",
                    "source_uuid": plan["uuid"],
                    "at": at,
                    "track": "v1",
                    "clipType": "media",
                    "asset": audio_entry["asset"],
                    "volume": 0.0,
                    **_clip_bounds_for_duration(audio_entry, duration, start=plan["audio_trim_start"]),
                }
            )
        if overlay_entry is not None:
            if overlay_entry.get("kind") == "generative":
                # extends prior plan Step 12
                # INERT until prior Steps 7+10+12 land — validate_pool currently rejects kind == 'generative'
                params = dict(overlay_entry.get("defaults", {}))
                if isinstance(plan.get("visual_params"), dict):
                    params.update(plan["visual_params"])
                effect_id = overlay_entry["effect_id"]
                # Brand-controlled effects own their entrance/exit choice via
                # themes/<theme>/effects/<id>/defaults.json. Drop any animation
                # arrays the LLM put in the brief — content lives in params, styling
                # lives in the effect.
                params = _drop_brand_animation_overrides(effect_id, params)
                # Fallback precedence: explicit plan duration -> theme pacing default -> legacy 4s default.
                generation_duration = float(plan.get("duration") or _theme_default_clip_sec(theme) or _LEGACY_DEFAULT_CLIP_SEC)
                # Pure-generative clips have no source media; omit source_uuid and
                # hoist orchestration metadata to the clip top level. The theme is
                # referenced once at top level (timeline.theme); leave per-clip
                # generation absent unless something actually diverges per clip.
                clips.append(
                    {
                        "id": f"clip_g_{order}",
                        "at": at,
                        "track": "v1" if plan.get("role") == "primary" else "v2",
                        "clipType": effect_id,
                        "hold": generation_duration,
                        "pool_id": overlay_entry.get("id"),
                        "clip_order": plan.get("order"),
                        "params": params,
                    }
                )
                continue
            overlay_play_duration = plan.get("overlay_play_duration") or duration
            clips.append(
                {
                    "id": f"clip_v2_{order}",
                    "source_uuid": plan["uuid"],
                    "at": at,
                    "track": "v2",
                    "clipType": "media",
                    "asset": overlay_entry["asset"],
                    "volume": 0.0,
                    **_clip_bounds_for_duration(overlay_entry, overlay_play_duration),
                }
            )
        text_overlay = plan["text_overlay"]
        if isinstance(text_overlay, dict) and isinstance(text_overlay.get("content"), str):
            text_data: dict[str, Any] = {"content": text_overlay["content"]}
            text_data.update(_text_style_preset_to_attrs(text_overlay.get("style_preset")))
            clips.append(
                {
                    "id": f"clip_t_{order}",
                    "source_uuid": plan["uuid"],
                    "at": at,
                    "track": "v2",
                    "clipType": "text",
                    "hold": duration,
                    "x": 0,
                    "y": 0,
                    "width": 640,
                    "height": 160,
                    "text": text_data,
                }
            )
    # If the source's resolution/fps doesn't match the theme canvas, surface that
    # via theme_overrides.visual.canvas so the renderer knows what to do. We only
    # write an override if it actually diverges from the theme.
    theme_overrides: dict[str, Any] = {}
    if (
        primary_asset is not None
        and primary_asset in registry["assets"]
        and theme is not None
    ):
        canvas = theme.get("visual", {}).get("canvas") or {}
        primary = registry["assets"][primary_asset]
        primary_resolution = primary.get("resolution")
        primary_fps = primary.get("fps")
        theme_resolution = (
            f"{int(canvas['width'])}x{int(canvas['height'])}"
            if isinstance(canvas.get("width"), (int, float)) and isinstance(canvas.get("height"), (int, float))
            else None
        )
        theme_fps = canvas.get("fps") if isinstance(canvas.get("fps"), (int, float)) else None
        canvas_override: dict[str, Any] = {}
        if primary_resolution and primary_resolution != theme_resolution:
            try:
                width_str, height_str = primary_resolution.split("x", 1)
                canvas_override["width"] = int(width_str)
                canvas_override["height"] = int(height_str)
            except ValueError:
                pass
        if primary_fps is not None and (theme_fps is None or float(primary_fps) != float(theme_fps)):
            canvas_override["fps"] = primary_fps
        if canvas_override:
            theme_overrides["visual"] = {"canvas": canvas_override}
    track_ids = {str(clip.get("track")) for clip in clips if clip.get("track")}
    if "a1" in track_ids:
        tracks = [
            {"id": "v1", "kind": "visual", "label": "Speaker"},
            {"id": "v2", "kind": "visual", "label": "B-roll"},
            {"id": "a1", "kind": "audio", "label": "Dialogue"},
        ]
    else:
        tracks = [{"id": "v1", "kind": "visual", "label": "Speaker"}] if "v1" in track_ids or not clips else []
        if "v2" in track_ids:
            tracks.append({"id": "v2", "kind": "visual", "label": "B-roll"})
        if "a1" in track_ids:
            tracks.append({"id": "a1", "kind": "audio", "label": "Dialogue"})
    config: dict[str, Any] = {}
    if theme_slug is not None:
        config["theme"] = theme_slug
    config.update({"tracks": tracks, "clips": clips})
    if theme_overrides:
        config["theme_overrides"] = theme_overrides
    # Every emitted timeline carries the canonical `output` block so
    # downstream consumers get
    # resolution/fps/file without re-resolving the theme. Sourced from
    # theme.visual.canvas via materialize_output() in the shared schema package;
    # background/background_scale pass through from any prior timeline.output.
    if theme is not None:
        merged_theme = _merged_theme_for_output(theme, theme_overrides)
        config["output"] = materialize_output(config, merged_theme)
    return config


def build_metadata_from_arrangement(
    arrangement: dict[str, Any],
    pool: dict[str, Any],
    registry: AssetRegistry,
    sources_meta: dict[str, dict[str, Any]],
    args: argparse.Namespace,
    primary_asset: str,
    transcript: list[dict[str, Any]] | None,
    *,
    quality_zones_ref: Path | None = None,
    pool_sha256: str,
    arrangement_sha256: str,
    brief_sha256: str,
    compiled_plan: list[dict[str, Any]] | None = None,
) -> PipelineMetadata:
    _ = registry
    clips: dict[str, dict[str, Any]] = {}
    for plan in compiled_plan or compile_arrangement_plan(arrangement, pool):
        order = plan["order"]
        audio_entry = plan["audio_entry"]
        overlay_entry = plan["overlay_entry"]
        rationale = plan["rationale"]
        if audio_entry is not None:
            audio_meta: dict[str, Any] = {
                "source_uuid": plan["uuid"],
                "pool_id": audio_entry["id"],
                "pool_kind": audio_entry["category"],
                "source_ids": dict(audio_entry.get("source_ids", {})),
                "arrangement_notes": rationale,
                "caption_kind": _pool_entry_caption_kind(audio_entry),
                "source_transcript_text": None,
            }
            if audio_entry.get("category") == "dialogue":
                segment_ids = audio_entry.get("source_ids", {}).get("segment_ids", [])
                if isinstance(segment_ids, list):
                    audio_meta["source_transcript_text"] = _joined_segment_text(segment_ids, transcript)
            clips[f"clip_a_{order}"] = audio_meta
            clips[f"clip_v1_{order}"] = {
                "source_uuid": plan["uuid"],
                "pool_id": audio_entry["id"],
                "pool_kind": audio_entry["category"],
                "source_ids": dict(audio_entry.get("source_ids", {})),
                "arrangement_notes": rationale,
                "caption_kind": "visual",
                "source_transcript_text": None,
            }

        if overlay_entry is not None:
            if overlay_entry.get("kind") == "generative":
                clips[f"clip_g_{order}"] = {
                    "source_uuid": plan["uuid"],
                    "pool_id": overlay_entry["id"],
                    "pool_kind": "visual",
                    "arrangement_notes": rationale,
                    "caption_kind": "visual",
                    "source_transcript_text": None,
                }
                continue
            clips[f"clip_v2_{order}"] = {
                "source_uuid": plan["uuid"],
                "pool_id": overlay_entry["id"],
                "pool_kind": overlay_entry["category"],
                "source_ids": dict(overlay_entry.get("source_ids", {})),
                "arrangement_notes": rationale,
                "caption_kind": "visual",
                "source_transcript_text": None,
            }
        text_overlay = plan["text_overlay"]
        if isinstance(text_overlay, dict) and isinstance(text_overlay.get("content"), str):
            clips[f"clip_t_{order}"] = {
                "source_uuid": plan["uuid"],
                "pool_id": None,
                "pool_kind": "text",
                "arrangement_notes": rationale,
                "caption_kind": "visual",
                "source_transcript_text": None,
                "text_overlay_content": text_overlay["content"],
            }

    out_dir = args.out.resolve()
    sources = {key: dict(value) for key, value in sources_meta.items()}
    if primary_asset is not None:
        primary_source = dict(sources.get(primary_asset, {}))
        primary_entry = registry["assets"].get(primary_asset, {})
        if isinstance(primary_entry.get("url"), str):
            primary_source["url"] = primary_entry["url"]
        primary_source["scenes_ref"] = _ref_path_for_metadata(args.scenes, out_dir)
        if args.transcript is not None:
            primary_source["transcript_ref"] = _ref_path_for_metadata(args.transcript, out_dir)
        if quality_zones_ref is not None:
            primary_source["quality_zones_ref"] = _ref_path_for_metadata(quality_zones_ref, out_dir)
        if args.shots is not None:
            primary_source["shots_ref"] = _ref_path_for_metadata(args.shots, out_dir)
        sources[primary_asset] = primary_source
    steps_run = ["cut"]
    if args.transcript is not None:
        steps_run.insert(0, "transcribe")
    if args.scenes is not None:
        steps_run.insert(-1, "scenes")
    if quality_zones_ref is not None:
        steps_run.insert(-1, "quality_zones")
    if args.shots is not None:
        steps_run.insert(-1, "shots")
    if args.arrangement is not None:
        steps_run.insert(-1, "arrange")
    return {
        "version": METADATA_VERSION,
        "generated_at": utc_now_seconds(),
        "pipeline": {
            "steps_run": steps_run,
            "tool_versions": _tool_fingerprints(PACKAGE_ROOT),
            "config_snapshot": {
                "primary_asset": primary_asset,
                "renderer": args.renderer,
                "mode": "arrangement",
            },
            "pool_provenance": {
                "pool_sha256": pool_sha256,
                "arrangement_sha256": arrangement_sha256,
                "brief_sha256": brief_sha256,
                "source_slug": arrangement.get("source_slug"),
                "brief_slug": arrangement.get("brief_slug"),
            },
        },
        "clips": clips,
        "sources": sources,
    }


def arrangement_edl_rows(
    arrangement: dict[str, Any],
    pool: dict[str, Any],
    transcript: list[dict[str, Any]] | None,
    compiled_plan: list[dict[str, Any]] | None = None,
) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for plan in compiled_plan or compile_arrangement_plan(arrangement, pool):
        source_entry = plan["audio_entry"] or plan["overlay_entry"]
        if source_entry is None or source_entry.get("kind") == "generative":
            continue
        source_ids = source_entry.get("source_ids", {})
        caption = None
        if source_entry.get("category") == "dialogue" and isinstance(source_ids, dict):
            segment_ids = source_ids.get("segment_ids")
            if isinstance(segment_ids, list):
                caption = _joined_segment_text(segment_ids, transcript)
        if caption is None:
            caption = (
                source_entry.get("text")
                or source_entry.get("subject")
                or source_entry.get("event_label")
                or ""
            )
        rows.append(
            {
                "asset": source_entry["asset"],
                "start": float(plan["audio_trim_start"]) if plan["audio_entry"] is not None else float(source_entry["src_start"]),
                "end": (
                    float(plan["audio_trim_start"]) + float(plan["duration"])
                    if plan["audio_entry"] is not None
                    else float(source_entry["src_start"]) + float(plan["duration"])
                ),
                "caption": caption,
            }
        )
    return rows


def write_edl(
    selected: list[dict[str, Any]],
    out_dir: Path,
    asset_paths: dict[str, Path],
    asset_urls: dict[str, str],
) -> Path:
    out_dir.mkdir(parents=True, exist_ok=True)
    edl_path = out_dir / "hype.edl.csv"
    with edl_path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.writer(handle)
        writer.writerow(["order", "src_start", "src_end", "src_path", "caption"])
        for order, item in enumerate(selected, start=1):
            asset_key = item["asset"]
            src_path = asset_urls.get(asset_key) or str(asset_paths[asset_key].resolve())
            writer.writerow(
                [
                    order,
                    f"{float(item['start']):.3f}",
                    f"{float(item['end']):.3f}",
                    src_path,
                    item["caption"],
                ]
            )
    return edl_path


def _register_cut_outputs(
    *,
    out_dir: Path,
    stage: str,
    parents: list[str] | None = None,
    rendered_path: Path | None = None,
    metadata: dict[str, Any] | None = None,
) -> None:
    audit = AuditContext.from_env()
    if audit is None:
        return
    parent_ids = list(parents or [])
    outputs = []
    for kind, filename, label in (
        ("edl", "hype.edl.csv", "Edit decision list"),
        ("timeline", "hype.timeline.json", "Timeline"),
        ("assets_registry", "hype.assets.json", "Asset registry"),
        ("metadata", "hype.metadata.json", "Pipeline metadata"),
    ):
        path = out_dir / filename
        if path.exists():
            outputs.append(audit.register_asset(kind=kind, path=path, label=label, parents=parent_ids, stage=stage, metadata=metadata))
    if rendered_path is not None and rendered_path.exists():
        outputs.append(audit.register_asset(kind="render", path=rendered_path, label="Rendered hype video", parents=outputs, stage=stage, metadata=metadata))
    audit.register_node(stage=stage, label="Build cut artifacts", parents=parent_ids, outputs=outputs, metadata=metadata or {})
