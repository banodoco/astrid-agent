#!/usr/bin/env python3
"""Build a read-only HTML storyboard for timeline shot input images."""
# ruff: noqa: E402

from __future__ import annotations

from astrid.pack_api import guard_canonical_entrypoint, run_pack_main

guard_canonical_entrypoint("rendering.timeline_storyboard")

import argparse
import html
import mimetypes
import re
from collections.abc import Mapping
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from astrid.core import timeline
from astrid.pack_api import build_manifest, write_manifest
from astrid.pack_api import AstridError
from astrid.pack_api import write_json_atomic
from astrid.core.foundation.atomic_io import write_text_atomic
from astrid.pack_api import require_runtime_materialized_file

_DISCORD_INPUT_RE = re.compile(r"^input_media(?:_([2-9]|10))?$")
CONTACT_SHEET_COLUMNS = 2
CONTACT_SHEET_CELL_WIDTH = 640
CONTACT_SHEET_CELL_HEIGHT = 420
CONTACT_SHEET_GUTTER = 16
_SHEET_BACKGROUND = (8, 10, 14)
_CELL_BACKGROUND = (17, 20, 27)
_MISSING_BACKGROUND = (45, 25, 31)
_CELL_BORDER = (58, 65, 78)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="rendering.timeline_storyboard",
        description="Build a static storyboard of image inputs associated with timeline shots.",
    )
    parser.add_argument("--timeline", type=Path, required=True, help="Timeline JSON file.")
    parser.add_argument(
        "--assets-registry",
        type=Path,
        required=True,
        help="Timeline asset registry JSON file.",
    )
    parser.add_argument("--shot-id", help="Only include the pinned shot with this id.")
    parser.add_argument("--out", type=Path, required=True, help="Output directory.")
    return parser


def _string(value: Any) -> str | None:
    return value if isinstance(value, str) and value else None


def _number(value: Any) -> float | None:
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return float(value)
    return None


def _normalized_asset_id(value: Any, assets: Mapping[str, Any]) -> str | None:
    candidate = _string(value)
    if candidate is None:
        return None
    if candidate.startswith("asset:"):
        candidate = candidate[len("asset:") :]
    # Bare ids are accepted even when missing so the preview can show a useful
    # placeholder instead of silently dropping a broken reference.
    if candidate in assets or candidate:
        return candidate
    return None


def _reference_asset_ids(generation: Any, assets: Mapping[str, Any]) -> list[str]:
    if not isinstance(generation, Mapping):
        return []
    references = generation.get("references")
    if not isinstance(references, list):
        return []
    result: list[str] = []
    for reference in references:
        if not isinstance(reference, Mapping):
            continue
        raw_id = next(
            (
                reference.get(key)
                for key in ("asset", "assetKey", "id")
                if _string(reference.get(key)) is not None
            ),
            None,
        )
        asset_id = _normalized_asset_id(raw_id, assets)
        if asset_id is not None:
            result.append(asset_id)
    return result


def _discord_asset_ids(generation: Any, assets: Mapping[str, Any]) -> list[str]:
    if not isinstance(generation, Mapping):
        return []
    matched: list[tuple[int, str]] = []
    for key, raw_id in generation.items():
        if not isinstance(key, str):
            continue
        match = _DISCORD_INPUT_RE.fullmatch(key)
        if match is None:
            continue
        order = int(match.group(1)) if match.group(1) is not None else 1
        asset_id = _normalized_asset_id(raw_id, assets)
        if asset_id is not None:
            matched.append((order, asset_id))
    return [asset_id for _order, asset_id in sorted(matched, key=lambda item: item[0])]


def _clip_duration(clip: Mapping[str, Any]) -> float:
    hold = _number(clip.get("hold"))
    if hold is not None:
        return max(0.0, hold)
    source_from = _number(clip.get("from_"))
    if source_from is None:
        source_from = _number(clip.get("from"))
    source_to = _number(clip.get("to"))
    if source_from is None or source_to is None:
        return 0.0
    speed = _number(clip.get("speed")) or 1.0
    if speed <= 0:
        return 0.0
    return max(0.0, source_to - source_from) / speed


def _shot_bounds(clips: list[Mapping[str, Any]]) -> tuple[float, float]:
    if not clips:
        return 0.0, 0.0
    starts = [_number(clip.get("at")) or 0.0 for clip in clips]
    ends = [
        (_number(clip.get("at")) or 0.0) + _clip_duration(clip)
        for clip in clips
    ]
    return min(starts), max(ends)


def _resolved_asset(
    asset_id: str,
    assets: Mapping[str, Any],
    *,
    registry_dir: Path,
) -> dict[str, Any]:
    raw_entry = assets.get(asset_id)
    entry = raw_entry if isinstance(raw_entry, Mapping) else {}
    asset_type = _string(entry.get("type"))
    file_value = _string(entry.get("file"))

    src: str | None = None
    missing = not bool(entry)
    if file_value is not None:
        try:
            file_path = require_runtime_materialized_file(
                file_value, label=f"asset {asset_id!r}"
            )
        except ValueError:
            # Invalid locators remain visible as a missing card.  In
            # particular, never fall back to URL or thumbnail fields: the
            # neutral runtime must materialize and verify bytes before this
            # pack receives them.
            file_path = None
        if file_path is not None:
            src = str(file_path)
            missing = False
    if src is None:
        missing = True

    if asset_type is None and src is not None:
        guessed, _encoding = mimetypes.guess_type(src)
        asset_type = guessed
    if asset_type is not None and not (
        asset_type.lower() == "image" or asset_type.lower().startswith("image/")
    ):
        missing = True

    return {
        "asset_id": asset_id,
        "src": src,
        "type": asset_type,
        "missing": missing,
    }


def _group_asset_ids(
    group: Mapping[str, Any],
    clips_by_id: Mapping[str, Mapping[str, Any]],
    assets: Mapping[str, Any],
) -> list[str]:
    clip_ids = [item for item in group.get("clipIds", []) if isinstance(item, str)]
    group_clips = [clips_by_id[clip_id] for clip_id in clip_ids if clip_id in clips_by_id]

    references: list[str] = []
    for clip in group_clips:
        references.extend(_reference_asset_ids(clip.get("generation"), assets))
    if references:
        return references

    discord_inputs: list[str] = []
    for clip in group_clips:
        discord_inputs.extend(_discord_asset_ids(clip.get("generation"), assets))
    if discord_inputs:
        return discord_inputs

    if group.get("mode") == "images":
        return [
            asset_id
            for clip in group_clips
            if (asset_id := _normalized_asset_id(clip.get("asset"), assets)) is not None
        ]

    if group.get("mode") == "video":
        snapshots = group.get("imageClipSnapshot")
        if not isinstance(snapshots, list):
            return []
        return [
            asset_id
            for snapshot in snapshots
            if isinstance(snapshot, Mapping)
            and (
                asset_id := _normalized_asset_id(snapshot.get("assetKey"), assets)
            )
            is not None
        ]

    return []


def _clip_is_media_less(clip: Mapping[str, Any]) -> bool:
    """True when a clip carries no media of its own (placeholder slot)."""
    if _string(clip.get("asset")) is not None:
        return False
    clip_type = _string(clip.get("clipType"))
    return clip_type != "media"


def _group_prompt(clips: list[Mapping[str, Any]]) -> str | None:
    """First non-empty ``generation.prompt`` across a shot's member clips."""
    for clip in clips:
        generation = clip.get("generation")
        if isinstance(generation, Mapping):
            prompt = generation.get("prompt")
            if isinstance(prompt, str) and prompt.strip():
                return prompt
    return None


def _group_metadata(clips: list[Mapping[str, Any]]) -> dict[str, Any]:
    """Merge ``generation.metadata`` maps across a shot's member clips."""
    merged: dict[str, Any] = {}
    for clip in clips:
        generation = clip.get("generation")
        if isinstance(generation, Mapping):
            metadata = generation.get("metadata")
            if isinstance(metadata, Mapping):
                merged.update(metadata)
    return merged


def build_view_model(
    timeline_config: Mapping[str, Any],
    asset_registry: Mapping[str, Any],
    *,
    registry_dir: Path,
    shot_id: str | None = None,
) -> dict[str, Any]:
    # Canonical shot-composition graphs are already prepared by the editor
    # lane. Project them in memory for the preview; the storyboard must never
    # reconstruct a shot from legacy pinned membership.
    app = timeline_config.get("app") if isinstance(timeline_config, Mapping) else None
    graph = None
    if isinstance(timeline_config.get("shot_revisions"), list):
        graph = timeline_config
    elif isinstance(app, Mapping):
        candidate = app.get("shot_composition_graph") or app.get("canonical_shot_composition")
        if isinstance(candidate, Mapping) and isinstance(candidate.get("shot_revisions"), list):
            graph = candidate
    if graph is not None:
        from astrid.core.timeline.shot_composition_projection import project_shot_composition
        projected = project_shot_composition(
            graph,
            base_config=timeline_config if isinstance(timeline_config.get("clips"), list) else None,
            base_registry=asset_registry,
        )
        timeline_config = projected.config
        asset_registry = projected.registry
    clips_raw = timeline_config.get("clips")
    clips = clips_raw if isinstance(clips_raw, list) else []
    clips_by_id: dict[str, Mapping[str, Any]] = {
        clip_id: clip
        for clip in clips
        if isinstance(clip, Mapping)
        and (clip_id := _string(clip.get("id"))) is not None
    }
    canonical = timeline_config.get("app", {}).get("astrid_shot_composition") if isinstance(timeline_config.get("app"), Mapping) else None
    canonical_occurrences = canonical.get("occurrences") if isinstance(canonical, Mapping) else None
    if isinstance(canonical_occurrences, list):
        groups = []
        for occurrence in canonical_occurrences:
            if not isinstance(occurrence, Mapping):
                continue
            occurrence_id = occurrence.get("occurrence_id")
            if not isinstance(occurrence_id, str):
                continue
            member_ids = [
                str(clip.get("id"))
                for clip in clips
                if isinstance(clip, Mapping) and clip.get("shot_occurrence_id") == occurrence_id
            ]
            start = _number(occurrence.get("at", occurrence.get("at_ms", 0) / 1000 if isinstance(occurrence.get("at_ms"), (int, float)) else 0)) or 0.0
            duration = _number(occurrence.get("duration_seconds", occurrence.get("duration_ms", 0) / 1000 if isinstance(occurrence.get("duration_ms"), (int, float)) else 0)) or 0.0
            groups.append({
                "shotId": occurrence.get("shot_id") or occurrence_id,
                "name": occurrence.get("name"),
                "clipIds": member_ids,
                "start": start,
                "end": start + duration,
                "trackId": occurrence.get("track"),
                "occurrenceId": occurrence_id,
            })
    else:
        groups_raw = timeline_config.get("pinnedShotGroups")
        groups = groups_raw if isinstance(groups_raw, list) else []
    assets_raw = asset_registry.get("assets")
    assets = assets_raw if isinstance(assets_raw, Mapping) else {}

    shots: list[dict[str, Any]] = []
    for index, raw_group in enumerate(groups):
        if not isinstance(raw_group, Mapping):
            continue
        current_shot_id = _string(raw_group.get("shotId")) or f"shot-{index + 1}"
        if shot_id is not None and current_shot_id != shot_id:
            continue
        clip_ids = [
            item for item in raw_group.get("clipIds", []) if isinstance(item, str)
        ]
        group_clips = [
            clips_by_id[clip_id] for clip_id in clip_ids if clip_id in clips_by_id
        ]
        start, end = _shot_bounds(group_clips)
        # Authored bounds are honored only for a clip-less placeholder shot
        # (the epic's authored-bounds decision); clip-backed shots always
        # derive their extent from the member clips. Dangling clipIds do not
        # qualify, and reversed bounds are ignored.
        authored_start = _number(raw_group.get("start"))
        authored_end = _number(raw_group.get("end"))
        if (
            not clip_ids
            and authored_start is not None
            and authored_end is not None
            and authored_start < authored_end
        ):
            start, end = authored_start, authored_end
        track_id = _string(raw_group.get("trackId"))
        if track_id is None and group_clips:
            track_id = _string(group_clips[0].get("track"))
        input_ids = _group_asset_ids(raw_group, clips_by_id, assets)
        placeholder = (
            not group_clips
            or (
                not bool(input_ids)
                and all(_clip_is_media_less(clip) for clip in group_clips)
            )
        )
        shots.append(
            {
                "shot_id": current_shot_id,
                "track_id": track_id,
                "clip_ids": clip_ids,
                "start": start,
                "end": end,
                "placeholder": placeholder,
                "prompt": _group_prompt(group_clips),
                "metadata": _group_metadata(group_clips),
                "inputs": [
                    _resolved_asset(asset_id, assets, registry_dir=registry_dir)
                    for asset_id in input_ids
                ],
            }
        )

    if shot_id is not None and not shots:
        raise AstridError(
            f"pinned shot not found: {shot_id}",
            recovery_command="inspect timeline.pinnedShotGroups and pass an existing shot id",
        )
    return {"schema_version": 1, "shots": shots}


def _html_source(src: str | None) -> str | None:
    if src is None:
        return None
    try:
        return require_runtime_materialized_file(src, label="storyboard image").as_uri()
    except ValueError:
        return None


def _pil() -> tuple[Any, Any, Any, Any]:
    try:
        from PIL import Image, ImageDraw, ImageFont, ImageOps
    except ModuleNotFoundError as exc:
        raise AstridError(
            "timeline storyboard requires Pillow to build preview.png",
            recovery_command="install the project dependencies and retry",
        ) from exc
    return Image, ImageDraw, ImageFont, ImageOps


def _contact_sheet_inputs(view_model: Mapping[str, Any]) -> list[Mapping[str, Any]]:
    inputs: list[Mapping[str, Any]] = []
    for shot in view_model.get("shots", []):
        if not isinstance(shot, Mapping):
            continue
        shot_inputs = shot.get("inputs")
        if not isinstance(shot_inputs, list):
            continue
        inputs.extend(item for item in shot_inputs if isinstance(item, Mapping))
    return inputs


def _contact_sheet_font() -> Any:
    _Image, _ImageDraw, ImageFont, _ImageOps = _pil()
    try:
        return ImageFont.truetype("DejaVuSans-Bold.ttf", 24)
    except OSError:
        return ImageFont.load_default()


def _short_label(value: Any, *, limit: int = 42) -> str:
    label = str(value or "unknown")
    if len(label) <= limit:
        return label
    return f"{label[: limit - 1]}…"


def _draw_missing_card(
    draw: Any,
    *,
    bounds: tuple[int, int, int, int],
    font: Any,
) -> None:
    left, top, right, bottom = bounds
    draw.rectangle(bounds, fill=_MISSING_BACKGROUND)
    inset = 28
    draw.line(
        (left + inset, top + inset, right - inset, bottom - inset),
        fill=(119, 58, 69),
        width=5,
    )
    draw.line(
        (right - inset, top + inset, left + inset, bottom - inset),
        fill=(119, 58, 69),
        width=5,
    )
    message = "MISSING IMAGE"
    text_box = draw.textbbox((0, 0), message, font=font)
    text_width = text_box[2] - text_box[0]
    text_height = text_box[3] - text_box[1]
    draw.rectangle(
        (
            left + (right - left - text_width) // 2 - 12,
            top + (bottom - top - text_height) // 2 - 10,
            left + (right - left + text_width) // 2 + 12,
            top + (bottom - top + text_height) // 2 + 10,
        ),
        fill=(24, 14, 18),
    )
    draw.text(
        (
            left + (right - left - text_width) // 2,
            top + (bottom - top - text_height) // 2,
        ),
        message,
        fill=(255, 192, 199),
        font=font,
    )


def _paste_contact_sheet_image(
    sheet: Any,
    item: Mapping[str, Any],
    *,
    bounds: tuple[int, int, int, int],
) -> bool:
    if bool(item.get("missing")):
        return False
    src = _string(item.get("src"))
    if src is None:
        return False
    try:
        path = require_runtime_materialized_file(src, label="storyboard image")
    except ValueError:
        return False

    Image, _ImageDraw, _ImageFont, ImageOps = _pil()
    left, top, right, bottom = bounds
    try:
        with Image.open(path) as opened:
            fitted = ImageOps.exif_transpose(opened).convert("RGB")
            fitted.thumbnail(
                (right - left, bottom - top),
                Image.Resampling.LANCZOS,
            )
            x = left + (right - left - fitted.width) // 2
            y = top + (bottom - top - fitted.height) // 2
            sheet.paste(fitted, (x, y))
    except (OSError, ValueError):
        return False
    return True


def render_contact_sheet(
    view_model: Mapping[str, Any],
    out_path: Path,
) -> Path:
    Image, ImageDraw, _ImageFont, _ImageOps = _pil()
    items = _contact_sheet_inputs(view_model)
    card_count = max(1, len(items))
    rows = (card_count + CONTACT_SHEET_COLUMNS - 1) // CONTACT_SHEET_COLUMNS
    width = (
        CONTACT_SHEET_COLUMNS * CONTACT_SHEET_CELL_WIDTH
        + (CONTACT_SHEET_COLUMNS + 1) * CONTACT_SHEET_GUTTER
    )
    height = (
        rows * CONTACT_SHEET_CELL_HEIGHT
        + (rows + 1) * CONTACT_SHEET_GUTTER
    )
    sheet = Image.new("RGB", (width, height), _SHEET_BACKGROUND)
    draw = ImageDraw.Draw(sheet)
    font = _contact_sheet_font()

    for index in range(rows * CONTACT_SHEET_COLUMNS):
        col = index % CONTACT_SHEET_COLUMNS
        row = index // CONTACT_SHEET_COLUMNS
        left = CONTACT_SHEET_GUTTER + col * (
            CONTACT_SHEET_CELL_WIDTH + CONTACT_SHEET_GUTTER
        )
        top = CONTACT_SHEET_GUTTER + row * (
            CONTACT_SHEET_CELL_HEIGHT + CONTACT_SHEET_GUTTER
        )
        right = left + CONTACT_SHEET_CELL_WIDTH
        bottom = top + CONTACT_SHEET_CELL_HEIGHT
        bounds = (left, top, right, bottom)
        draw.rectangle(bounds, fill=_CELL_BACKGROUND)

        if index < len(items):
            item = items[index]
            if not _paste_contact_sheet_image(sheet, item, bounds=bounds):
                _draw_missing_card(draw, bounds=bounds, font=font)
            label = f"{index + 1}  {_short_label(item.get('asset_id'))}"
            label_box = draw.textbbox((0, 0), label, font=font)
            label_width = label_box[2] - label_box[0]
            label_height = label_box[3] - label_box[1]
            label_left = left + 12
            label_top = top + 12
            draw.rounded_rectangle(
                (
                    label_left - 8,
                    label_top - 6,
                    label_left + label_width + 8,
                    label_top + label_height + 8,
                ),
                radius=6,
                fill=(0, 0, 0),
            )
            draw.text(
                (label_left, label_top),
                label,
                fill=(255, 255, 255),
                font=font,
            )
        draw.rectangle(bounds, outline=_CELL_BORDER, width=2)

    if not items:
        message = "NO INPUT IMAGES"
        text_box = draw.textbbox((0, 0), message, font=font)
        draw.text(
            (
                (width - (text_box[2] - text_box[0])) // 2,
                (height - (text_box[3] - text_box[1])) // 2,
            ),
            message,
            fill=(184, 190, 202),
            font=font,
        )

    out_path = out_path.expanduser().resolve()
    out_path.parent.mkdir(parents=True, exist_ok=True)
    temporary = out_path.with_name(f".{out_path.name}.tmp")
    try:
        sheet.save(temporary, format="PNG", compress_level=9)
        temporary.replace(out_path)
    finally:
        temporary.unlink(missing_ok=True)
    return out_path


def render_html(view_model: Mapping[str, Any]) -> str:
    cards: list[str] = []
    for shot in view_model.get("shots", []):
        if not isinstance(shot, Mapping):
            continue
        inputs_html: list[str] = []
        for index, item in enumerate(shot.get("inputs", []), start=1):
            if not isinstance(item, Mapping):
                continue
            asset_id = html.escape(str(item.get("asset_id") or "unknown"))
            src = _html_source(_string(item.get("src")))
            missing = bool(item.get("missing")) or src is None
            if missing:
                media = '<div class="missing" role="img" aria-label="Missing input image">Missing input image</div>'
            else:
                escaped_src = html.escape(src, quote=True)
                media = (
                    f'<a href="{escaped_src}" target="_blank" rel="noreferrer">'
                    f'<img src="{escaped_src}" alt="Input {index}: {asset_id}" loading="lazy"></a>'
                )
            inputs_html.append(
                '<figure class="input-card">'
                f"{media}"
                f"<figcaption><strong>Input {index}</strong><span>{asset_id}</span></figcaption>"
                "</figure>"
            )
        if not inputs_html:
            inputs_html.append(
                '<div class="empty">No associated input images were found for this shot.</div>'
            )
        shot_label = html.escape(str(shot.get("shot_id") or "Unknown shot"))
        track_label = html.escape(str(shot.get("track_id") or "Unassigned track"))
        start = float(shot.get("start") or 0.0)
        end = float(shot.get("end") or 0.0)
        placeholder = bool(shot.get("placeholder"))
        badge = (
            '<span class="badge placeholder">placeholder</span>'
            if placeholder
            else ""
        )
        prompt_text = _string(shot.get("prompt"))
        metadata = shot.get("metadata")
        prompt_html = (
            f'<p class="prompt">{html.escape(prompt_text)}</p>'
            if prompt_text is not None
            else ""
        )
        metadata_html = ""
        if isinstance(metadata, Mapping) and metadata:
            rows = "".join(
                f"<li><strong>{html.escape(str(key))}</strong>"
                f"<span>{html.escape(str(value))}</span></li>"
                for key, value in metadata.items()
            )
            metadata_html = f'<ul class="meta">{rows}</ul>'
        cards.append(
            '<section class="shot">'
            f'<header><div><h2>{shot_label}</h2><p>{track_label}</p></div>'
            f'<div class="header-right">{badge}'
            f'<time>{start:.2f}s–{end:.2f}s</time></div></header>'
            f"{prompt_html}{metadata_html}"
            f'<div class="inputs">{"".join(inputs_html)}</div>'
            "</section>"
        )

    body = "".join(cards) or '<p class="empty">This timeline has no pinned shot groups.</p>'
    return f"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>Timeline storyboard</title>
  <style>
    :root {{ color-scheme: dark; font-family: Inter, ui-sans-serif, system-ui, sans-serif; }}
    * {{ box-sizing: border-box; }}
    body {{ margin: 0; background: #090b10; color: #f4f5f7; }}
    main {{ width: min(1120px, calc(100% - 32px)); margin: 0 auto; padding: 40px 0 64px; }}
    h1 {{ margin: 0 0 24px; font-size: clamp(1.8rem, 4vw, 3rem); }}
    .shot {{ margin: 0 0 28px; padding: 20px; border: 1px solid #2b303b; border-radius: 18px; background: #11151d; }}
    header {{ display: flex; align-items: start; justify-content: space-between; gap: 20px; margin-bottom: 16px; }}
    h2 {{ margin: 0; font-size: 1.15rem; }}
    header p, time {{ margin: 5px 0 0; color: #9ba6b7; font: 0.82rem ui-monospace, monospace; }}
    .header-right {{ display: flex; align-items: center; gap: 10px; }}
    .badge {{ border-radius: 999px; padding: 3px 10px; font: 0.72rem ui-monospace, monospace; text-transform: uppercase; letter-spacing: 0.04em; }}
    .badge.placeholder {{ color: #ffd08a; background: #2b2216; border: 1px solid #4a3a20; }}
    .prompt {{ margin: 0 0 14px; color: #c6cede; font-size: 0.9rem; line-height: 1.5; }}
    .meta {{ display: flex; flex-wrap: wrap; gap: 8px; margin: 0 0 14px; padding: 0; list-style: none; }}
    .meta li {{ display: flex; gap: 8px; align-items: baseline; padding: 5px 10px; border: 1px solid #2b303b; border-radius: 9px; background: #0d1119; font-size: 0.78rem; }}
    .meta strong {{ color: #8f9bb0; font-weight: 600; }}
    .meta span {{ color: #e4e7ee; overflow-wrap: anywhere; }}
    .inputs {{ display: grid; grid-template-columns: repeat(2, minmax(0, 1fr)); gap: 14px; }}
    .input-card {{ min-width: 0; margin: 0; overflow: hidden; border: 1px solid #303744; border-radius: 13px; background: #090c12; }}
    .input-card a {{ display: block; aspect-ratio: 16 / 10; background: #06080c; }}
    .input-card img {{ width: 100%; height: 100%; display: block; object-fit: contain; }}
    .missing {{ display: grid; min-height: 190px; place-items: center; padding: 24px; color: #ffb4b4; background: repeating-linear-gradient(135deg, #23171b 0 12px, #1b1216 12px 24px); }}
    figcaption {{ display: flex; justify-content: space-between; gap: 12px; padding: 10px 12px; font-size: 0.8rem; }}
    figcaption span {{ overflow: hidden; color: #9ba6b7; text-overflow: ellipsis; white-space: nowrap; }}
    .empty {{ grid-column: 1 / -1; padding: 34px; border: 1px dashed #394151; border-radius: 12px; color: #9ba6b7; text-align: center; }}
    @media (max-width: 680px) {{ .inputs {{ grid-template-columns: 1fr; }} header {{ display: block; }} }}
  </style>
</head>
<body>
  <main>
    <h1>Timeline storyboard</h1>
    {body}
  </main>
</body>
</html>
"""


def build_storyboard(
    *,
    timeline_path: Path,
    assets_registry_path: Path,
    out_dir: Path,
    shot_id: str | None = None,
) -> dict[str, Any]:
    timeline_path = timeline_path.expanduser().resolve()
    assets_registry_path = assets_registry_path.expanduser().resolve()
    out_dir = out_dir.expanduser().resolve()

    timeline_config = timeline.load_timeline(timeline_path)
    asset_registry = timeline.load_registry(assets_registry_path)
    view_model = build_view_model(
        timeline_config,
        asset_registry,
        registry_dir=assets_registry_path.parent,
        shot_id=shot_id,
    )

    preview_json = out_dir / "preview.json"
    preview_png = out_dir / "preview.png"
    preview_html = out_dir / "preview.html"
    write_json_atomic(preview_json, view_model)
    render_contact_sheet(view_model, preview_png)
    write_text_atomic(preview_html, render_html(view_model))
    return {
        "view_model": view_model,
        "preview_json": preview_json,
        "preview_png": preview_png,
        "preview_html": preview_html,
    }


def main(argv: list[str] | None = None) -> int:
    def _run() -> int:
        args = build_parser().parse_args(argv)
        try:
            result = build_storyboard(
                timeline_path=args.timeline,
                assets_registry_path=args.assets_registry,
                out_dir=args.out,
                shot_id=args.shot_id,
            )
        except (FileNotFoundError, OSError, ValueError) as exc:
            raise AstridError(
                f"timeline storyboard failed: {exc}",
                recovery_command="check the timeline, asset registry, and output paths, then retry",
            ) from exc

        out_dir = args.out.expanduser().resolve()
        manifest = build_manifest(
            kind="timeline_storyboard",
            inputs={
                "timeline": str(args.timeline.expanduser().resolve()),
                "assets_registry": str(args.assets_registry.expanduser().resolve()),
                "shot_id": args.shot_id,
            },
            outputs=[
                {"path": "preview.json", "type": "file"},
                {"path": "preview.png", "type": "file"},
                {"path": "preview.html", "type": "file"},
            ],
            created=datetime.now(timezone.utc).isoformat(),
        )
        write_manifest(out_dir / "manifest.json", manifest)
        print(f"timeline-storyboard: wrote {result['preview_json']}")
        print(f"timeline-storyboard: wrote {result['preview_png']}")
        print(f"timeline-storyboard: wrote {result['preview_html']}")
        return 0

    return run_pack_main("rendering.timeline_storyboard", _run, argv=argv)


if __name__ == "__main__":
    raise SystemExit(main())
