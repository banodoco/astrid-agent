"""Shared timeline inspection grammar, status projection, and input geometry.

The option and geometry helpers are deliberately pure. They are used by the product CLI, SDK
admission, and the executor so the three entry points cannot disagree about
component names or half-open time semantics.  It does not render, retry, or
read media bytes. The offline inspector below reads only verified bundle members.
"""
from __future__ import annotations

import math
import hashlib
import json
import shlex
from collections import defaultdict
from fractions import Fraction
from pathlib import Path, PurePosixPath
from typing import Any, Iterable, Mapping

from astrid.core.timeline.duration import clip_end_frame, clip_start_frame

COMPONENTS = ("output", "text", "audio", "inputs")
DEFAULT_COMPONENTS = ("output", "text", "audio")
_COMPONENT_ALIASES = {"input": "inputs", "video": "output", "waveform": "audio"}


def _tokens(value: Any) -> list[str]:
    if value is None:
        return []
    values = value if isinstance(value, (list, tuple, set)) else [value]
    result: list[str] = []
    for item in values:
        if not isinstance(item, str):
            raise ValueError("components must be comma-separated names")
        result.extend(part.strip().lower() for part in item.split(",") if part.strip())
    return result


def normalize_components(show: Any = None, hide: Any = None) -> dict[str, Any]:
    """Resolve the small public component grammar into a stable ordered set."""
    shown: list[str] = []
    hidden: list[str] = []
    for source, target in ((_tokens(show), shown), (_tokens(hide), hidden)):
        for token in source:
            token = _COMPONENT_ALIASES.get(token, token)
            if token not in COMPONENTS:
                raise ValueError(
                    f"unknown inspection component {token!r}; choose {', '.join(COMPONENTS)}"
                )
            if token not in target:
                target.append(token)
    conflict = sorted(set(shown) & set(hidden), key=COMPONENTS.index)
    if conflict:
        raise ValueError("component requested by both show and hide: " + ", ".join(conflict))
    resolved = [name for name in COMPONENTS if name in DEFAULT_COMPONENTS]
    for name in shown:
        if name not in resolved:
            resolved.append(name)
    resolved = [name for name in resolved if name not in hidden]
    return {
        "show": shown,
        "hide": hidden,
        "resolved": resolved,
        "default": not shown and not hidden,
    }


def _finite(value: Any, label: str, *, nonnegative: bool = True) -> Fraction:
    if isinstance(value, bool):
        raise ValueError(f"{label} must be a finite number")
    try:
        raw = str(value).strip()
        if ":" in raw:
            fields = raw.split(":")
            if len(fields) > 3:
                raise ValueError
            nums = [Fraction(part) for part in fields]
            if any(item >= 60 for item in nums[1:]):
                raise ValueError
            result = Fraction(0)
            for item in nums:
                result = result * 60 + item
        else:
            result = Fraction(raw)
    except (TypeError, ValueError, ZeroDivisionError):
        raise ValueError(f"{label} must be a finite number") from None
    if nonnegative and result < 0:
        raise ValueError(f"{label} must be non-negative")
    return result


def _seconds(value: Any, *, milliseconds: bool = False) -> Fraction:
    """Parse a timeline time without routing through binary floating point."""
    result = _finite(value, "timeline time")
    return result / 1000 if milliseconds else result


def normalize_input_window(
    *, range_value: Any = None, at: Any = None, context: Any = 2,
) -> dict[str, Any] | None:
    """Normalize an exact half-open seconds window without float rounding."""
    if range_value is not None and at is not None:
        raise ValueError("choose range or at/context, not both")
    if range_value is None and at is None:
        return None
    if at is not None:
        center = _finite(at, "at")
        radius = _finite(context, "context")
        if radius <= 0:
            raise ValueError("context must be positive when focusing a timestamp")
        start, end, selected = max(Fraction(0), center - radius), center + radius, center
    else:
        if isinstance(range_value, str):
            pieces = range_value.split("..")
        else:
            pieces = range_value
        if not isinstance(pieces, (list, tuple)) or len(pieces) != 2:
            raise ValueError("range must be START..END")
        start, end = (_finite(part, "range bound") for part in pieces)
        if end <= start:
            raise ValueError("range end must follow its start")
        selected = None
    return {
        "start": [start.numerator, start.denominator],
        "end": [end.numerator, end.denominator],
        "half_open": True,
        **({"selected": [selected.numerator, selected.denominator]} if selected is not None else {}),
    }


def inspection_options(values: Mapping[str, Any]) -> dict[str, Any]:
    """Validate and normalize all U1 selectors while retaining old fields."""
    components = normalize_components(values.get("show"), values.get("hide"))
    tracks = _tokens(values.get("track"))
    if any(not item or any(ord(char) < 32 for char in item) for item in tracks):
        raise ValueError("track must be a non-empty identifier")
    window = normalize_input_window(
        range_value=values.get("range"), at=values.get("at"),
        context=values.get("context") if values.get("context") is not None else 3.0,
    )
    detail = values.get("detail", False)
    if not isinstance(detail, bool):
        raise ValueError("detail must be a boolean")
    occurrence = values.get("occurrence")
    if occurrence not in (None, "") and (
        not isinstance(occurrence, str) or len(occurrence) > 256 or "data:" in occurrence.lower()
    ):
        raise ValueError("occurrence must be a bounded occurrence identifier")
    return {
        "components": components,
        "tracks": tracks,
        "clip": values.get("clip") or None,
        "occurrence": occurrence or None,
        "shot": values.get("shot") or None,
        "asset": values.get("asset") or None,
        "window": window,
        "detail": detail,
    }


def canonical_clip_identity(
    clip: Mapping[str, Any], *, timeline_id: str | None = None,
    occurrence_id: str | None = None, shot_id: str | None = None,
) -> dict[str, Any]:
    """Project occurrence, authored clip, and reusable shot identities separately.

    The occurrence is a placement in this timeline.  ``shot_id`` identifies the
    reusable source, while ``clip_id`` names the authored timeline element.
    Missing occurrence identity stays missing instead of being guessed from a
    shot id or a repeated media selector.
    """
    clip_id = clip.get("id") or clip.get("clip_id")
    occurrence = _clip_occurrence_id(clip) or occurrence_id
    shot = clip.get("shot_id") or clip.get("shotId") or shot_id
    target = {
        "kind": "clip",
        "timeline_id": timeline_id,
        "occurrence_id": occurrence if isinstance(occurrence, str) and occurrence else None,
        "clip_id": clip_id if isinstance(clip_id, str) and clip_id else None,
        "shot_id": shot if isinstance(shot, str) and shot else None,
    }
    target["addressable"] = bool(target["clip_id"] or target["occurrence_id"])
    return target


def _clip_occurrence_id(clip: Mapping[str, Any]) -> Any:
    """Read the canonical occurrence spelling before legacy aliases."""
    return clip.get("occurrence_id") or clip.get("shot_occurrence_id") or clip.get("occurrenceId")


def classify_output_records(
    outputs: Any, *, current_head: Any = None, current_output_id: Any = None,
) -> list[dict[str, Any]]:
    """Classify explicit output records without treating a candidate as current.

    A record without a matching head or explicit current pointer remains
    unverified.  This is intentionally conservative for old outputs lacking
    provenance.
    """
    if not isinstance(outputs, (list, tuple)):
        return []
    result = []
    for raw in outputs:
        if not isinstance(raw, Mapping):
            continue
        row = {key: _small_scalar(raw[key]) for key in (
            "output_id", "id", "run_id", "digest", "content_hash", "state",
            "created_at", "source_head", "timeline_head", "config_version",
        ) if key in raw}
        output_id = raw.get("output_id") or raw.get("id") or raw.get("run_id")
        disposition = str(raw.get("disposition") or raw.get("kind") or raw.get("status") or "").lower()
        metadata = raw.get("metadata") if isinstance(raw.get("metadata"), Mapping) else {}
        provenance = raw.get("provenance") if isinstance(raw.get("provenance"), Mapping) else {}
        render_mode = str(
            raw.get("render_mode") or metadata.get("render_mode")
            or provenance.get("render_mode") or ""
        ).lower()
        candidate_provenance = render_mode in {
            "authoring_candidate_preview", "authoring_preview", "candidate_preview",
            "unpublished_candidate_preview",
        }
        if candidate_provenance:
            row["render_mode"] = render_mode
        if raw.get("is_candidate") is True or raw.get("candidate") is True or candidate_provenance or disposition in {
            "candidate", "preview", "unpublished", "draft",
        }:
            classification = "candidate"
        elif output_id is not None and current_output_id is not None and str(output_id) == str(current_output_id):
            classification = "current" if raw.get("is_candidate") is not True else "candidate"
        elif current_head is not None and (raw.get("source_head") or raw.get("timeline_head")) == current_head:
            classification = "current" if raw.get("is_candidate") is not True else "candidate"
        elif disposition in {"historical", "history", "superseded", "stale"}:
            classification = "historical"
        else:
            classification = "unverified"
        row["classification"] = classification
        row["current_for_head"] = classification == "current"
        result.append(row)
    return result


def _clip_asset_keys(clip: Mapping[str, Any]) -> tuple[str, ...]:
    result: list[str] = []
    for value in (clip.get("asset"), clip.get("asset_id")):
        if isinstance(value, str) and value and value not in result:
            result.append(value)
    source = clip.get("source")
    if isinstance(source, str) and source and source not in result:
        result.append(source)
    elif isinstance(source, Mapping):
        for key in ("asset", "asset_id", "assetKey", "key", "id"):
            value = source.get(key)
            if isinstance(value, str) and value and value not in result:
                result.append(value)
    return tuple(result)


def _clip_is_muted(clip: Mapping[str, Any], *, inherited: bool = False) -> bool:
    """Resolve the small, explicit mute contract used by inspection.

    A muted parent lane must not make a nested selected asset look active.  We
    deliberately do not infer silence from missing audio or from arbitrary
    effect fields; those remain unresolved diagnostics rather than invented
    state.
    """
    volume = clip.get("volume")
    own = clip.get("muted") is True or (
        isinstance(volume, (int, float)) and not isinstance(volume, bool) and volume <= 0
    )
    return inherited or own


def _iter_clip_records(
    clips: Any, *, inherited_muted: bool = False, path: str = "",
) -> Iterable[tuple[Any, Mapping[str, Any] | None, bool]]:
    """Walk direct and common nested clip containers without changing them."""
    if not isinstance(clips, (list, tuple)):
        return
    for index, raw in enumerate(clips):
        location = f"{path}[{index}]" if path else index
        if not isinstance(raw, Mapping):
            yield location, None, inherited_muted
            continue
        # Volume is audio gain, not visual visibility. Propagate only an
        # explicit mute bit into nested records; semantic inventory resolves
        # audio gain and visual participation separately below.
        muted = inherited_muted or raw.get("muted") is True
        yield location, raw, muted
        for key in ("children", "clips", "elements"):
            nested = raw.get(key)
            if isinstance(nested, (list, tuple)):
                yield from _iter_clip_records(
                    nested, inherited_muted=muted,
                    path=f"{location}.{key}",
                )


def semantic_media_inventory(
    clips: Iterable[Mapping[str, Any]], registry: Mapping[str, Any],
) -> dict[str, Any]:
    """Describe selected use separately from retained alternatives and history."""
    raw_assets = registry.get("assets", registry)
    if isinstance(raw_assets, Mapping):
        assets = {str(key): value for key, value in raw_assets.items()}
    elif isinstance(raw_assets, (list, tuple)):
        assets = {
            str(item.get("asset_key") or item.get("key") or f"asset-{i}"): item
            for i, item in enumerate(raw_assets) if isinstance(item, Mapping)
        }
    else:
        assets = {}
    uses: dict[str, list[dict[str, Any]]] = defaultdict(list)
    diagnostics: list[dict[str, Any]] = []
    for index, clip, inherited_muted in _iter_clip_records(clips):
        if not isinstance(clip, Mapping):
            diagnostics.append({"code": "invalid_clip_record", "clip_index": index, "message": "clip must be an object"})
            continue
        clip_id = clip.get("id") or clip.get("clip_id") or f"clip-{index}"
        try:
            start = Fraction(str(clip.get("at", clip.get("at_ms", 0))))
            duration = clip.get("hold", clip.get("duration", clip.get("duration_ms")))
            if "at_ms" in clip and "at" not in clip:
                start /= 1000
            if duration is not None and "duration_ms" in clip and "hold" not in clip and "duration" not in clip:
                duration = Fraction(str(duration)) / 1000
            if start < 0 or (duration is not None and Fraction(str(duration)) <= 0):
                raise ValueError
        except (TypeError, ValueError, ZeroDivisionError):
            diagnostics.append({"code": "invalid_clip_timing", "clip_id": clip_id,
                                "message": "clip has invalid or non-positive timing"})
        track = clip.get("track") or clip.get("track_id")
        audio_muted = _clip_is_muted(clip, inherited=inherited_muted)
        explicit_visual_mute = inherited_muted or clip.get("muted") is True
        track_name = str(track or "").lower()
        kind_name = str(clip.get("clipType") or clip.get("kind") or "").lower()
        asset_types = {
            str(_mapping_type.get("media_type") or _mapping_type.get("type") or "").lower()
            for asset_key in _clip_asset_keys(clip)
            for _mapping_type in [assets.get(asset_key) if isinstance(assets.get(asset_key), Mapping) else {}]
        }
        visual_use = (
            track_name in {"picture", "video", "visual", "image", "overlay", "output"}
            or any(token in kind_name for token in ("image", "video", "picture", "visual"))
            or bool(asset_types & {"image", "video", "image/png", "image/jpeg", "video/mp4", "video/webm"})
        )
        audio_use = (
            track_name in {"audio", "voice", "voiceover", "vo", "music", "sound", "sfx"}
            or any(token in kind_name for token in ("audio", "voice", "music", "sound"))
            or bool(clip.get("audio_source") or clip.get("audio"))
        )
        visual_muted = explicit_visual_mute if visual_use else audio_muted
        state = "muted" if visual_muted else "active"
        for asset_key in _clip_asset_keys(clip):
            use = {"clip_id": clip_id, "track_id": track, "state": state}
            if visual_use:
                use["visual_state"] = "muted" if explicit_visual_mute else "active"
            if audio_use:
                use["audio_state"] = "muted" if audio_muted else "active"
            uses[asset_key].append(use)
            if asset_key not in assets:
                diagnostics.append({"code": "selected_media_missing_registry", "clip_id": clip_id,
                                   "asset_key": asset_key, "message": "selected media key is absent from registry"})
    rows = []
    for asset_key, entry in sorted(assets.items()):
        metadata = entry if isinstance(entry, Mapping) else {}
        role = str(metadata.get("role") or metadata.get("kind") or "unspecified").lower()
        references = uses.get(asset_key, [])
        if references:
            state = "active" if any(ref["state"] == "active" for ref in references) else "muted"
        elif role in {"alternative", "candidate", "variant", "option"}:
            state = "alternative"
        elif role in {"history", "historical", "previous_output", "superseded_output"}:
            state = "historical"
        elif role in {"generation_reference", "generation_output", "thumbnail_only", "rendered_sample"}:
            state = role
        else:
            state = "unplaced"
        rows.append({
            "asset_key": asset_key, "state": state, "role": role,
            "media_id": _small_scalar(metadata.get("media_id") or metadata.get("object_id")),
            "digest": _small_scalar(metadata.get("content_sha256") or metadata.get("sha256") or metadata.get("digest")),
            "uses": references,
        })
    return {"items": rows, "diagnostics": diagnostics,
            "counts": {state: sum(row["state"] == state for row in rows)
                       for state in sorted({row["state"] for row in rows})}}


def project_timeline_document(
    document: Mapping[str, Any], *, limit: int = 50, cursor: str | None = None,
    clip: str | None = None, occurrence: str | None = None, shot: str | None = None,
    track: Any = None, asset: str | None = None, range_value: Any = None,
    detail: bool = False,
) -> dict[str, Any]:
    """Return the shared bounded textual scope consumed by show and visualizer."""
    if type(limit) is not int or not 1 <= limit <= 100:
        raise ValueError("limit must be between 1 and 100")
    config = document.get("config") if isinstance(document.get("config"), Mapping) else document
    raw_clips = config.get("clips", []) if isinstance(config, Mapping) else []
    if not isinstance(raw_clips, list):
        raw_clips = []
    registry = document.get("registry") if isinstance(document.get("registry"), Mapping) else {}
    tracks = track if isinstance(track, (list, tuple, set)) else (track,) if track else ()
    window = normalize_input_window(range_value=range_value) if range_value is not None else None
    occurrences = document.get("occurrences") or (config.get("occurrences", []) if isinstance(config, Mapping) else [])
    occurrence_for_clip: dict[str, Mapping[str, Any]] = {}
    if isinstance(occurrences, list):
        for row in occurrences:
            if isinstance(row, Mapping):
                clip_id = row.get("clip_id") or row.get("clipId")
                if isinstance(clip_id, str):
                    occurrence_for_clip.setdefault(clip_id, row)
    timeline_id = document.get("timeline_id") or document.get("id")
    fps = document.get("fps") or config.get("theme_overrides", {}).get("visual", {}).get("canvas", {}).get("fps", 30)
    try:
        fps = Fraction(str(fps))
        if fps <= 0:
            fps = Fraction(30, 1)
    except (TypeError, ValueError, ZeroDivisionError):
        fps = Fraction(30, 1)
    selected = []
    diagnostics: list[dict[str, Any]] = []
    for index, raw in enumerate(raw_clips):
        if not isinstance(raw, Mapping):
            diagnostics.append({"code": "invalid_clip_record", "clip_index": index, "message": "clip must be an object"})
            continue
        occurrence_row = occurrence_for_clip.get(str(raw.get("id") or ""), {})
        identity = canonical_clip_identity(raw, timeline_id=str(timeline_id) if timeline_id else None,
                                           occurrence_id=occurrence_row.get("occurrence_id"),
                                           shot_id=occurrence_row.get("shot_id"))
        if clip and identity.get("clip_id") != clip:
            continue
        if occurrence and identity.get("occurrence_id") != occurrence:
            continue
        if shot and identity.get("shot_id") != shot:
            continue
        track_id = raw.get("track") or raw.get("track_id")
        if tracks and track_id not in tracks:
            continue
        keys = _clip_asset_keys(raw)
        if asset and asset not in keys:
            continue
        at = raw.get("at", raw.get("at_ms", 0))
        at_is_ms = "at_ms" in raw and "at" not in raw
        hold = raw.get("hold", raw.get("duration", raw.get("duration_ms")))
        hold_is_ms = hold is not None and "duration_ms" in raw and "hold" not in raw and "duration" not in raw
        source_to = raw.get("to")
        speed = raw.get("speed", 1)
        if hold is None and source_to is not None:
            source_from = raw.get("from", 0)
            try:
                hold = max(Fraction(0), _finite(source_to, "source end") - _finite(source_from, "source start"))
            except (TypeError, ValueError, ZeroDivisionError):
                hold = None
        try:
            start_sec = _seconds(at, milliseconds=at_is_ms)
            speed_value = _finite(speed, "speed", nonnegative=False)
            if speed_value <= 0:
                raise ValueError("speed must be positive")
            duration_sec = _seconds(hold, milliseconds=hold_is_ms) / speed_value if hold is not None else None
            end_sec = start_sec + duration_sec if duration_sec is not None else None
            if start_sec < 0 or (end_sec is not None and end_sec <= start_sec):
                raise ValueError("clip timing must have non-negative start and positive duration")
        except (TypeError, ValueError, ZeroDivisionError) as exc:
            diagnostics.append({"code": "invalid_clip_timing", "clip_id": identity.get("clip_id"), "message": str(exc)})
            start_sec, end_sec = None, None
        if window is not None:
            if start_sec is None or end_sec is None:
                continue
            if end_sec <= Fraction(*window["start"]) or start_sec >= Fraction(*window["end"]):
                continue
        compact = {
            **identity, "track_id": track_id,
            "target": identity,
            "clip_type": raw.get("clipType") or raw.get("clip_type") or raw.get("type"),
            "at_seconds": [start_sec.numerator, start_sec.denominator] if start_sec is not None else None,
            "end_seconds": [end_sec.numerator, end_sec.denominator] if end_sec is not None else None,
            "duration_seconds": [(end_sec - start_sec).numerator, (end_sec - start_sec).denominator] if start_sec is not None and end_sec is not None else None,
            "selected_media": [{"asset_key": key, "media_id": (registry.get("assets", {}).get(key, {}) or {}).get("media_id") if isinstance(registry.get("assets"), Mapping) and isinstance(registry.get("assets", {}).get(key), Mapping) else None,
                                "digest": (registry.get("assets", {}).get(key, {}) or {}).get("content_sha256") if isinstance(registry.get("assets"), Mapping) and isinstance(registry.get("assets", {}).get(key), Mapping) else None}
                               for key in keys],
        }
        composition_app = raw.get("app") if isinstance(raw.get("app"), Mapping) else {}
        composition = (
            composition_app.get("astrid_shot_composition")
            if isinstance(composition_app.get("astrid_shot_composition"), Mapping)
            else None
        )
        if composition is not None:
            compact["composition"] = {
                key: _small_scalar(composition[key])
                for key in (
                    "project_id", "timeline_id", "shot_id", "shot_revision_id",
                    "internal_timeline_revision_id", "occurrence_id", "source_clip_id",
                    "output_identity", "stable_deep_link", "source_offset", "speed",
                    "gain", "muted",
                )
                if key in composition
            }
        source_time = {
            key: _small_scalar(raw[key])
            for key in ("from", "to", "speed", "source_offset")
            if key in raw
        }
        if source_time:
            compact["source_time"] = source_time
        geometry = {
            key: _small_scalar(raw[key])
            for key in ("x", "y", "width", "height", "rotation", "opacity", "transform")
            if key in raw and not isinstance(raw[key], (list, Mapping))
        }
        if geometry:
            compact["geometry"] = geometry
        text_value = raw.get("text")
        text = text_value.get("content") if isinstance(text_value, Mapping) else text_value
        if isinstance(text, str):
            compact["text_role"] = raw.get("text_role") or raw.get("role") or "visible_text"
            compact["text_length"] = len(text)
            if detail or len(text) <= 512:
                compact["text"] = text if detail else text[:512]
            if not detail and len(text) > 512:
                compact["text_truncated"] = True
        project_ref = document.get("project_slug") or document.get("project_id") or "<project>"
        timeline_ref = document.get("slug") or timeline_id or "<timeline>"
        scope_args: list[str] = []
        # Actions are row-addressable, not merely query-addressable.  If the
        # caller opened the overview, expanding a repeated occurrence must not
        # silently fall back to its reusable shot or an ambiguous clip id.
        row_occurrence = identity.get("occurrence_id") if not occurrence else occurrence
        row_shot = identity.get("shot_id") if not shot else shot
        for flag, value in (("--occurrence", row_occurrence), ("--shot", row_shot), ("--asset", asset)):
            if value:
                scope_args.extend([flag, str(value)])
        if tracks:
            for track_value in tracks:
                scope_args.extend(["--track", str(track_value)])
        if range_value is not None:
            scope_args.extend(["--range", str(range_value)])
        compact["actions"] = {
            "expand": action_argv(
                "astrid", "timelines", "show", "--project", str(project_ref), str(timeline_ref),
                "--summary", "--clip", str(identity.get("clip_id") or "<clip>"), "--detail", *scope_args,
            ),
            "visualize": action_argv(
                "astrid", "timelines", "visualize", "--project", str(project_ref),
                "--timeline-slug", str(timeline_ref), "--clip", str(identity.get("clip_id") or "<clip>"),
                "--show", "inputs", *scope_args,
            ),
        }
        selected.append(compact)
    selected.sort(key=lambda row: (row["at_seconds"] is None,
                                   Fraction(*row["at_seconds"]) if row["at_seconds"] else Fraction(0),
                                   str(row.get("track_id") or ""), str(row.get("clip_id") or "")))
    head = document.get("head_hash") or document.get("head_event_id") or document.get("config_version")
    parent_revision = (
        document.get("parent_revision_id") or document.get("parent_head_id")
        or document.get("parent_revision")
    )
    head_revision = document.get("head_revision_id") or document.get("revision_id")
    candidate_digest = document.get("candidate_digest") or document.get("candidate_hash")
    render_run_id = document.get("render_run_id") or document.get("current_render_run_id")
    query = {"timeline_id": timeline_id,
             "project_id": document.get("project_id") or document.get("project_slug"),
             "head": head,
             "parent_revision_id": parent_revision,
             "head_revision_id": head_revision,
             "candidate_digest": candidate_digest,
             "render_run_id": render_run_id,
             "clip": clip, "occurrence": occurrence, "shot": shot, "track": list(tracks), "asset": asset, "range": range_value, "detail": detail, "limit": limit}
    binding = hashlib.sha256(json.dumps(query, sort_keys=True, separators=(",", ":")).encode()).hexdigest()[:20]
    offset = 0
    if cursor:
        parts = cursor.split(":", 1)
        if len(parts) != 2 or parts[0] != binding or not parts[1].isdigit():
            raise ValueError("cursor does not match this query")
        offset = int(parts[1])
    page = selected[offset:offset + limit]
    next_cursor = f"{binding}:{offset + limit}" if offset + limit < len(selected) else None
    inventory = semantic_media_inventory(raw_clips, registry)
    diagnostics.extend(inventory["diagnostics"])
    outputs = document.get("outputs") or document.get("render_outputs") or []
    config_mapping = config if isinstance(config, Mapping) else {}
    track_rows = config_mapping.get("tracks", [])
    duration = config_mapping.get("duration") or config_mapping.get("duration_seconds")
    if duration is None and selected:
        ends = [row["end_seconds"] for row in selected if row.get("end_seconds")]
        if ends:
            duration = [max(Fraction(*value) for value in ends).numerator,
                        max(Fraction(*value) for value in ends).denominator]
    summary = {
        "fps": [fps.numerator, fps.denominator],
        "duration_seconds": duration,
        "clip_count": len(selected),
        "track_count": len(track_rows) if isinstance(track_rows, (list, tuple, Mapping)) else 0,
        "diagnostic_count": len(diagnostics),
        "authority": "canonical_head" if head is not None else "unverified_document",
        "parent_revision_id": parent_revision,
        "head_revision_id": head_revision,
        "candidate_digest": candidate_digest,
        "render_run_id": render_run_id,
    }
    return {
        "kind": "timeline-inspection", "timeline_id": timeline_id,
        "project_id": document.get("project_id"), "slug": document.get("slug"),
        "timeline_name": document.get("name"), "head": head,
        "version": document.get("config_version", document.get("version")),
        "summary": summary,
        "query": query, "targets": [row["target"] for row in page], "clips": page,
        "media": inventory, "outputs": classify_output_records(
            outputs, current_head=head, current_output_id=document.get("current_output_id")),
        "diagnostics": diagnostics,
        "pagination": {"limit": limit, "offset": offset, "total": len(selected), "next_cursor": next_cursor},
    }


def action_argv(*argv: str) -> dict[str, Any]:
    """Return a copyable action in both argv and shell forms."""
    args = [str(item) for item in argv]
    return {"argv": args, "command": shlex.join(args)}


def render_status(
    *, lifecycle: Any = None, output: Mapping[str, Any] | None = None,
    expected_digest: str | None = None, owner_ok: bool | None = None,
    fresh: bool | None = None, project: str | None = None,
) -> dict[str, Any]:
    """Classify render lifecycle/output independently and add next actions.

    The caller supplies already-admitted runtime facts.  No state here starts
    work or treats a missing output as a successful render.
    """
    state = str(lifecycle or "absent").lower().strip()
    record = dict(output or {})
    if owner_ok is False:
        kind, label = "wrong_owner", "rendered output is not owned by the selected project"
    elif expected_digest and record.get("digest") and record.get("digest") != expected_digest:
        kind, label = "integrity_mismatch", "output digest does not match the admitted render"
    elif state in {"queued", "pending", "admitted", "starting"}:
        kind, label = "pending", "render is pending"
    elif state in {"running", "started", "in_progress", "stopped", "timeout", "timed_out"}:
        kind, label = "running", "render is still running or waiting; timeout is not failure"
    elif state in {"failed", "error", "cancelled", "canceled"}:
        kind, label = "failed", "render did not complete successfully"
    elif state in {"completed", "succeeded", "success"} and not record.get("available", record.get("object_id")):
        kind, label = "lifecycle_success_missing_output", "render lifecycle succeeded but video output is unavailable"
    elif state in {"completed", "succeeded", "success"} and fresh is False:
        kind, label = "stale", "successful render is stale relative to current inputs"
    elif state in {"completed", "succeeded", "success"}:
        kind, label = "succeeded", "verified rendered output is available"
    else:
        kind, label = "absent", "No successful render is available (no successful managed render is available)"
    project_arg = str(project or "<project>")
    timeline_arg = str(record.get("timeline") or "<timeline>")
    run_id = str(record.get("run_id") or "<run>")
    actions: list[dict[str, Any]] = []
    if kind in {"absent", "lifecycle_success_missing_output"}:
        actions += [
            {"id": "inspect_inputs", **action_argv("astrid", "timelines", "visualize", "--project", project_arg, "--timeline-slug", timeline_arg, "--show", "inputs", "--hide", "output")},
            {"id": "render", **action_argv("astrid", "timelines", "render", timeline_arg, "--project", project_arg)},
        ]
    elif kind in {"pending", "running"}:
        actions += [
            {"id": "follow", **action_argv("astrid", "tasks", "follow", str(record.get("task_id") or "<task>"), "--project", project_arg)},
            {"id": "events", **action_argv("astrid", "tasks", "events", str(record.get("task_id") or "<task>"), "--project", project_arg)},
        ]
    elif kind == "failed":
        actions += [
            {"id": "inspect_frozen_inputs", **action_argv("astrid", "timelines", "visualize", "--project", project_arg, "--render-run", run_id, "--show", "inputs", "--hide", "output")},
            {"id": "retry", **action_argv("astrid", "runs", "retry", run_id, "--project", project_arg)},
        ]
    elif kind == "stale":
        actions += [{"id": "inspect_old_render", **action_argv("astrid", "timelines", "visualize", "--project", project_arg, "--render-run", run_id)}]
    elif kind in {"wrong_owner", "integrity_mismatch"}:
        actions += [{"id": "inspect_inputs", **action_argv("astrid", "timelines", "visualize", "--project", project_arg, "--timeline-slug", timeline_arg, "--show", "inputs", "--hide", "output")}]
    return {
        "kind": kind, "status": kind, "label": label, "lifecycle": state,
        "output": {"available": bool(record.get("available", record.get("object_id"))),
                    "digest": record.get("digest"), "run_id": record.get("run_id")},
        "fresh": fresh, "owner_ok": owner_ok, "actions": actions,
        "next_actions": actions,
    }


def _rational(value: Any) -> Fraction:
    if isinstance(value, (list, tuple)) and len(value) == 2:
        return Fraction(int(value[0]), int(value[1]))
    return Fraction(str(value))


def _source_preview(clip: Mapping[str, Any], integrity: Mapping[str, Any] | Any | None) -> dict[str, Any]:
    if integrity is not None and not isinstance(integrity, Mapping):
        integrity = {name: getattr(integrity, name, None) for name in ("state", "observed_sha256", "expected_sha256", "reason")}
    integrity = integrity or {}
    state = str(integrity.get("state") or "missing")
    digest = integrity.get("observed_sha256") or integrity.get("expected_sha256")
    if state == "verified_original":
        # ``path`` is an attempt-local preview locator only.  It is never used
        # as authority (the digest/managed identity admission is the authority)
        # and is copied into the result pack before a browser consumes it.
        result = {
            "status": "verified", "digest": digest, "source_time": None,
            "media_type": integrity.get("media_type"),
        }
        if integrity.get("path"):
            result["path"] = integrity.get("path")
        return result
    return {"status": "placeholder", "reason": str((integrity or {}).get("reason") or state), "digest": digest}


def _audio_signifier(
    clip: Mapping[str, Any], track: str, *, start_frame: int, end_frame: int,
    overlap_start: int, overlap_end: int, fps: Fraction, speed: Fraction,
    source_start: Fraction, source_end: Fraction,
) -> dict[str, Any] | None:
    """Describe source audio presence on the clip's actual timeline window."""
    track_name = str(track).lower()
    kind = str(clip.get("kind") or clip.get("clipType") or "").lower()
    declared = clip.get("audio_source")
    if declared is None:
        declared = clip.get("source_audio")
    if declared is None and "audio" in clip:
        declared = clip.get("audio")
    if declared is False or (isinstance(declared, Mapping) and declared.get("enabled") is False):
        declared = None
    audio_track = track_name in {"audio", "vo", "voiceover", "music", "sound", "sfx"}
    audio_kind = kind in {"audio", "voiceover", "music", "sound", "sfx"}
    has_audio = audio_track or audio_kind or declared is not None or clip.get("has_audio") is True or clip.get("audio_enabled") is True
    if not has_audio:
        return None
    volume = clip.get("volume")
    muted = clip.get("muted") is True or (isinstance(volume, (int, float)) and not isinstance(volume, bool) and volume <= 0)
    def frame_seconds(frame: int) -> list[int]:
        value = Fraction(int(frame), 1) / fps
        return [value.numerator, value.denominator]

    if muted:
        return {
            "present": False, "reason": "muted", "source": declared,
            "window": [overlap_start, overlap_end],
            "window_seconds": [
                frame_seconds(overlap_start), frame_seconds(overlap_end),
            ],
            "visual_encoding": "timing_rail",
        }
    source_label = "embedded source audio" if declared is None else (
        str(declared.get("pool_id") or declared.get("asset") or declared.get("source") or "declared source audio")
        if isinstance(declared, Mapping) else str(declared)
    )
    return {
        "present": True, "source": source_label,
        "window": [overlap_start, overlap_end],
        "window_seconds": [
            frame_seconds(overlap_start), frame_seconds(overlap_end),
        ],
        "basis": "clip_placement",
        "visual_encoding": "timing_rail",
        "source_window": [
            [source_start.numerator, source_start.denominator],
            [source_end.numerator, source_end.denominator],
        ],
        "speed": [speed.numerator, speed.denominator],
    }


def project_input_window(
    clips: Iterable[Mapping[str, Any]], *, start_frame: int, end_frame: int, fps: Any,
    track_ids: Iterable[str] = (), clip_id: str | None = None,
    shot_id: str | None = None, occurrence_id: str | None = None,
    asset_id: str | None = None,
    integrity: Mapping[str, Mapping[str, Any]] | None = None,
    shot_groups: Iterable[Mapping[str, Any]] = (),
    shot_occurrences: Iterable[Mapping[str, Any]] = (),
    max_tracks: int = 10,
) -> dict[str, Any]:
    """Project every clip intersecting ``[start_frame,end_frame)``.

    Occurrence ids are never collapsed by shot id.  Subrows use greedy
    interval coloring in canonical input order, making overlap layout stable.
    """
    if start_frame < 0 or end_frame <= start_frame:
        raise ValueError("input window must be a non-empty half-open frame interval")
    clips = list(clips)
    fps_value = _rational(fps)
    wanted = set(str(item) for item in track_ids)
    # Canonical input snapshots retain pinned groups separately from the
    # flattened clip list.  Build a local membership index so shot filtering
    # works for inputs as well as render-admitted clips, without mutating the
    # frozen snapshot.
    shot_by_clip: dict[str, tuple[str, str | None]] = {}
    for group in shot_groups or ():
        if not isinstance(group, Mapping):
            continue
        group_id = group.get("shotId") or group.get("shot_id")
        if not isinstance(group_id, str) or not group_id:
            continue
        group_name = group.get("name") or group.get("shotName") or group.get("shot_name") or group.get("label")
        group_name = group_name if isinstance(group_name, str) and group_name else None
        members = group.get("clipIds") or group.get("clip_ids") or ()
        for member in members if isinstance(members, (list, tuple, set)) else ():
            if isinstance(member, str) and member:
                shot_by_clip.setdefault(member, (group_id, group_name))
    for occurrence in shot_occurrences or ():
        if not isinstance(occurrence, Mapping):
            continue
        occurrence_id = occurrence.get("occurrence_id")
        shot_value = occurrence.get("shot_id")
        if not isinstance(occurrence_id, str) or not occurrence_id:
            continue
        if not isinstance(shot_value, str) or not shot_value:
            continue
        name = occurrence.get("name") or occurrence.get("shot_name")
        name = name if isinstance(name, str) and name else None
        for raw in clips:
            if isinstance(raw, Mapping) and _clip_occurrence_id(raw) == occurrence_id:
                raw_id = raw.get("id")
                if isinstance(raw_id, str):
                    shot_by_clip.setdefault(raw_id, (shot_value, name))
    selected = []
    for index, raw in enumerate(clips):
        if not isinstance(raw, Mapping):
            continue
        track = str(raw.get("track") or raw.get("track_id") or "")
        if wanted and track not in wanted:
            continue
        if clip_id and str(raw.get("id") or raw.get("clip_id") or raw.get("occurrence_id")) != str(clip_id):
            continue
        raw_occurrence_id = _clip_occurrence_id(raw)
        if occurrence_id and str(raw_occurrence_id) != str(occurrence_id):
            continue
        raw_clip_id = str(raw.get("id") or raw.get("clip_id") or raw.get("occurrence_id") or f"clip-{index}")
        membership = shot_by_clip.get(raw_clip_id)
        raw_shot_id = str(raw.get("shot_id") or raw.get("shot")) if raw.get("shot_id") or raw.get("shot") else (membership[0] if membership else None)
        if shot_id and raw_shot_id != str(shot_id):
            continue
        if asset_id and str(raw.get("asset") or raw.get("asset_id") or raw.get("source")) != str(asset_id):
            continue
        timing = dict(raw)
        # Render snapshots normalize a clip's presented duration into
        # ``duration``; canonical authored clips use from/to/hold. Preserve
        # both forms instead of letting a missing source trim collapse a clip
        # to its one-frame placement.
        if raw.get("duration") is not None and raw.get("from") is None and raw.get("to") is None and raw.get("hold") is None:
            timing["hold"] = raw.get("duration")
            timing["speed"] = 1
        start = clip_start_frame(timing, float(fps_value))
        end = clip_end_frame(timing, float(fps_value))
        overlap_start, overlap_end = max(start, start_frame), min(end, end_frame)
        if overlap_start >= overlap_end:
            continue
        selected.append((index, raw, track, start, end, overlap_start, overlap_end))
    selected.sort(key=lambda item: (item[2], item[5], item[6], str(item[1].get("id", "")), item[0]))
    by_track: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for index, raw, track, start, end, overlap_start, overlap_end in selected:
        rows = by_track[track]
        subrow = 0
        while any(existing["subrow"] == subrow and existing["window"][0] < overlap_end and existing["window"][1] > overlap_start for existing in rows):
            subrow += 1
        asset_key = raw.get("asset") or raw.get("source")
        integrity_row = integrity.get(str(asset_key), {}) if integrity else {}
        raw_occurrence_id = _clip_occurrence_id(raw)
        speed = _rational(raw.get("speed", 1))
        trim_start = _rational(raw.get("from", raw.get("from_", 0)))
        source_start = trim_start + max(Fraction(0), Fraction(overlap_start - start, 1) / fps_value) * speed
        source_end = trim_start + max(Fraction(0), Fraction(overlap_end - start, 1) / fps_value) * speed
        occurrence = raw_occurrence_id or raw.get("id")
        row = {
            "clip_id": str(raw.get("id") or f"clip-{index}"), "occurrence_id": str(occurrence),
            "track_id": track, "asset_key": str(asset_key) if asset_key is not None else None,
            "window": [overlap_start, overlap_end],
            "window_seconds": [[(Fraction(overlap_start, 1) / fps_value).numerator, (Fraction(overlap_start, 1) / fps_value).denominator], [(Fraction(overlap_end, 1) / fps_value).numerator, (Fraction(overlap_end, 1) / fps_value).denominator]],
            "source_time": [[source_start.numerator, source_start.denominator], [source_end.numerator, source_end.denominator]],
            "speed": [speed.numerator, speed.denominator], "trim": {"from": [trim_start.numerator, trim_start.denominator]},
            "subrow": subrow, "continuation": overlap_start > start or overlap_end < end,
            "source_preview": _source_preview(raw, integrity_row),
        }
        if raw_shot_id is not None:
            row["shot_id"] = raw_shot_id
        raw_shot_name = raw.get("shot_name") or raw.get("shotName")
        if raw_shot_name is None and membership:
            raw_shot_name = membership[1]
        if isinstance(raw_shot_name, str) and raw_shot_name:
            row["shot_name"] = raw_shot_name
        audio = _audio_signifier(
            raw, track, start_frame=start, end_frame=end,
            overlap_start=overlap_start, overlap_end=overlap_end, fps=fps_value, speed=speed,
            source_start=source_start, source_end=source_end,
        )
        if audio is not None:
            row["audio_signifier"] = audio
        rows.append(row)
    ordered_tracks = sorted(by_track)
    visible_tracks = ordered_tracks[:max_tracks]
    # Bands paginate the complete ordered track set.  Slicing the first-page
    # ``visible_tracks`` here made every band after page one empty for dense
    # timelines, even though the projection itself retained those tracks.
    bands = [{"track_ids": ordered_tracks[i:i + max_tracks], "label": f"tracks {i + 1}–{min(i + max_tracks, len(ordered_tracks))} of {len(ordered_tracks)}"} for i in range(0, len(ordered_tracks), max_tracks)]
    return {
        "window": {"start_frame": start_frame, "end_frame": end_frame, "fps": [fps_value.numerator, fps_value.denominator], "half_open": True},
        "tracks": [{"track_id": track, "clips": by_track[track], "band": index // max_tracks} for index, track in enumerate(ordered_tracks)],
        "track_bands": bands, "visible_track_ids": visible_tracks,
        "hidden_track_count": max(0, len(ordered_tracks) - len(visible_tracks)),
        "source_preview_policy": "verified managed original only; missing, tampered, and unsupported media are labelled placeholders",
    }


__all__ = ["COMPONENTS", "DEFAULT_COMPONENTS", "normalize_components", "normalize_input_window", "inspection_options", "action_argv", "render_status", "project_input_window"]


INSPECTION_SECTIONS = ("summary", "pages", "cards", "placements", "audio", "boundaries")
INSPECTION_MAX_BYTES = 8192
_IDENTITY_KEYS = ("project_slug", "timeline_id", "timeline_name", "render_run_id", "video_digest", "fps_rational", "duration_frames")


def _small_scalar(value: Any, limit: int = 256) -> Any:
    if isinstance(value, str):
        if "data:" in value.lower() or len(value) > limit:
            return "[omitted: oversized or inline data]"
        return value
    if value is None or isinstance(value, (bool, int)):
        return value
    if isinstance(value, float) and math.isfinite(value):
        return value
    return None


def _projection(record: Mapping, keys: Iterable[str]) -> dict:
    return {key: _small_scalar(record[key]) for key in keys if key in record}


def _bounded_pair(value: Any) -> list[Any] | None:
    if not isinstance(value, (list, tuple)) or len(value) != 2:
        return None
    if any(isinstance(item, bool) or not isinstance(item, (int, float)) for item in value):
        return None
    if any(isinstance(item, float) and not math.isfinite(item) for item in value):
        return None
    return list(value)


def _component_list(value: Any) -> list[str] | None:
    """Keep the resolved component surface small and machine-readable."""
    if not isinstance(value, (list, tuple)):
        return None
    result: list[str] = []
    for item in value:
        if not isinstance(item, str) or item not in COMPONENTS or item in result:
            continue
        result.append(item)
    return result


def _component_request_projection(value: Any) -> dict[str, Any] | None:
    """Persist the user's component intent without copying arbitrary options."""
    if not isinstance(value, Mapping):
        return None
    result: dict[str, Any] = {}
    for key in ("show", "hide", "resolved"):
        values = _component_list(value.get(key))
        if values is not None:
            result[key] = values
    if isinstance(value.get("default"), bool):
        result["default"] = value["default"]
    return result or None


def _sampling_projection(value: Any) -> dict[str, Any]:
    """Project the settings that explain what was sampled and at what size."""
    if not isinstance(value, Mapping):
        return {}
    result = _projection(value, (
        "mode", "overview", "explicit_interval", "include_cuts",
        "requested_at", "requested_frame", "resolved_at_frame",
        "resolved_at_time", "rounding_rule",
    ))
    for key in ("range", "effective_range", "requested_range", "step_frames_rational", "resolution"):
        pair = _bounded_pair(value.get(key))
        if pair is not None:
            result[key] = pair
    density = value.get("density")
    if isinstance(density, Mapping):
        result["density"] = _projection(density, ("mode", "value"))
    return result


def _surface_projection(value: Any, *, include_rows: bool = True) -> dict[str, Any] | None:
    """Persist bounded paired-surface metadata for offline inspection."""
    if not isinstance(value, Mapping):
        return None
    result = _projection(value, (
        "schema", "mode", "columns", "page_size", "page_count",
        "page_size_policy", "row_count", "axis",
    ))
    for key in ("components", "canonical_input_tracks", "standalone_input_pages"):
        values = value.get(key)
        if isinstance(values, (list, tuple)):
            bounded = [item for item in values if isinstance(item, str) and len(item) <= 256]
            result[key] = bounded[:128]
    rows = value.get("rows")
    if include_rows and isinstance(rows, list):
        compact_rows: list[dict[str, Any]] = []
        for row in rows[:500]:
            if not isinstance(row, Mapping):
                continue
            projected = _projection(row, (
                "index", "start_seconds", "end_seconds", "output_card_count",
            ))
            for key in ("card_ids", "input_tracks"):
                values = row.get(key)
                if isinstance(values, (list, tuple)):
                    projected[key] = [item for item in values if isinstance(item, str) and len(item) <= 256][:64]
            compact_rows.append(projected)
        result["rows"] = compact_rows
    return result or None


def _capture_projection(value: Any) -> dict[str, Any] | None:
    """Persist bounded fresh/cache and renderer identity evidence."""
    if not isinstance(value, Mapping):
        return None
    result = _projection(value, (
        "evidence_source", "renderer", "cache_root",
        "cached_frames", "fresh_frames",
    ))
    environment = value.get("renderer_environment")
    if isinstance(environment, str) and environment:
        bounded: dict[str, Any] = {
            "digest": "sha256:" + hashlib.sha256(environment.encode("utf-8")).hexdigest(),
        }
        try:
            parsed = json.loads(environment)
        except (TypeError, json.JSONDecodeError):
            parsed = None
        if isinstance(parsed, Mapping):
            for key in ("renderer", "sources", "project_dir", "node"):
                item = parsed.get(key)
                if isinstance(item, str) and item and len(item) <= 128 and "data:" not in item.lower():
                    bounded[key] = item
        result["renderer_environment"] = bounded
    elif isinstance(environment, Mapping):
        result["renderer_environment"] = _projection(environment, ("digest", "renderer", "sources"))
    for key in ("resolution",):
        pair = _bounded_pair(value.get(key))
        if pair is not None:
            result[key] = pair
    requested = value.get("requested_frames")
    if isinstance(requested, (list, tuple)):
        result["requested_frame_count"] = len(requested)
        if requested and all(isinstance(item, int) and not isinstance(item, bool) for item in requested):
            result["requested_frame_bounds"] = [min(requested), max(requested)]
    worker = value.get("worker")
    if isinstance(worker, Mapping):
        result["worker"] = _projection(worker, ("batches", "serialized", "owner"))
    return result


def _relative_path(value: Any) -> str:
    if not isinstance(value, str) or not value or len(value) > 512 or any(ord(c) < 32 for c in value):
        raise ValueError("unsafe_member")
    path = PurePosixPath(value)
    if path.is_absolute() or ".." in path.parts or "\\" in value or ":" in value:
        raise ValueError("unsafe_member")
    return path.as_posix()


def compact_render_receipt(index: Mapping, snapshot: Mapping, root: Path) -> dict:
    """Persist sample evidence, never a duplicate timeline/navigation graph."""
    provenance = _projection(snapshot, _IDENTITY_KEYS)
    provenance["fps_rational"] = list(snapshot["fps_rational"])
    canonical = (snapshot.get("metadata") or {}).get("canonical_timeline") or {}
    timeline = _projection(canonical, ("config_version", "config_hash", "authority"))
    timeline["timeline_ref"] = provenance.get("timeline_id")
    cards = []
    if len(index.get("cards", [])) > 2000:
        raise ValueError("compact receipt exceeds 2000 samples")
    for raw in index.get("cards", []):
        card = _projection(raw, ("id", "frame", "time_seconds", "time_label"))
        card["image"] = _relative_path(raw["image"])
        card["time_rational"] = list(raw["time_rational"])
        reasons = raw.get("sample_reasons", [])
        if len(reasons) > 16:
            raise ValueError("compact receipt exceeds sample reason limit")
        card["sample_reasons"] = [_small_scalar(reason, 64) for reason in reasons]
        for target, key in (("clip_ids", "id"), ("shot_ids", "shot_id"), ("occurrence_ids", "occurrence_id")):
            values = sorted({str(item.get(key) or item.get("shot_occurrence_id")) for item in raw.get("clips", [])
                             if item.get(key) is not None or item.get("shot_occurrence_id") is not None})
            if len(values) > 64 or any(len(value) > 256 or "data:" in value.lower() for value in values):
                raise ValueError("compact receipt identity limit exceeded")
            card[target] = values
        # Keep the one useful navigation affordance without copying the full
        # action graph or arbitrary command payload into the receipt.
        actions = raw.get("actions")
        if isinstance(actions, Mapping) and isinstance(actions.get("focus_command"), str):
            command = actions["focus_command"]
            if len(command) > 1024 or any(ord(char) < 32 for char in command):
                raise ValueError("compact receipt action limit exceeded")
            card["actions"] = {"focus_command": command}
        cards.append(card)
    receipt = {"schema": "astrid.filmstrip.v2", "provenance": provenance,
               "canonical_timeline": timeline, "cards": cards,
               "sampling": _sampling_projection(index.get("sampling", {})),
               "coverage": _projection(index.get("coverage", {}), ("full_duration", "selected_frame_count", "page_count", "page_size", "all_boundaries_sampled")),
               "inspection": {"command": "python3 -m astrid timelines inspect --manifest MANIFEST --section summary",
                              "sections": list(INSPECTION_SECTIONS)}}
    components = _component_list(index.get("components"))
    if components is not None:
        receipt["components"] = components
    component_request = _component_request_projection(index.get("component_request"))
    if component_request is not None:
        receipt["component_request"] = component_request
    surface = _surface_projection(index.get("static_surface"))
    if surface is not None:
        receipt["static_surface"] = surface
    capture = _capture_projection(index.get("frame_capture"))
    if capture is not None:
        receipt["provenance"]["frame_capture"] = capture
    if isinstance(index.get("inspection"), Mapping):
        receipt["scope"] = _projection(index["inspection"].get("scope", {}), ("timeline_id", "render_run_id", "occurrence_id"))
        receipt["target"] = _projection(index["inspection"].get("target", {}), ("kind", "timeline_id", "occurrence_id", "clip_id", "shot_id", "asset_key"))
    for field, filename in (("snapshot_sidecar", "render-snapshot.json"), ("audio_sidecar", "audio-analysis.json")):
        path = root / filename
        if path.is_file():
            with path.open("rb") as stream:
                digest = hashlib.file_digest(stream, "sha256").hexdigest()
            receipt[field] = {"path": filename, "digest": "sha256:" + digest, "bytes": path.stat().st_size,
                              "render_digest": provenance.get("video_digest"), "verified": True}
    if isinstance(index.get("media"), Mapping):
        receipt["media"] = _projection(index["media"], ("path", "digest", "bytes", "verified", "kind", "source_digest", "render_run_id"))
    if len(json.dumps(receipt, ensure_ascii=True).encode()) > 2 * 1024 * 1024:
        raise ValueError("compact receipt exceeds 2 MiB")
    return receipt


def _inspection_error(code: str) -> dict:
    return {"ok": False, "data": None, "error": {"code": code, "message": "Offline inspection could not satisfy this bounded request."},
            "receipt": None, "idempotency_key": ""}


def inspect_filmstrip(manifest: str | Path, *, section: str = "summary", limit: int = 10,
                      cursor: str | None = None, frame: int | None = None, card: str | None = None,
                      shot: str | None = None, occurrence: str | None = None, clip: str | None = None,
                      track: str | None = None, asset: str | None = None, range_value: str | None = None) -> dict:
    """Read named scalar projections offline; never return arbitrary JSON objects.

    Legacy v1 indexes are read without rewriting them. Cursors bind to the exact
    manifest bytes and query. Every referenced member is confined and verified.
    The entire compact JSON envelope (including errors) is at most 8 KiB.
    """
    try:
        if section not in INSPECTION_SECTIONS or type(limit) is not int or not 1 <= limit <= 50:
            return _inspection_error("invalid_query")
        selectors = {"frame": frame, "card": card, "shot": shot, "occurrence": occurrence,
                     "clip": clip, "track": track, "asset": asset, "range": range_value}
        if frame is not None and (type(frame) is not int or frame < 0):
            return _inspection_error("invalid_selector")
        for key, value in selectors.items():
            if key != "frame" and value is not None and (not isinstance(value, str) or not value or len(value) > 256 or "data:" in value.lower()):
                return _inspection_error("invalid_selector")
        if section in ("summary", "pages") and any(value is not None for value in selectors.values()):
            return _inspection_error("unsupported_selector")
        if section == "audio" and any(value is not None for key, value in selectors.items() if key != "range"):
            return _inspection_error("unsupported_selector")
        window = normalize_input_window(range_value=range_value) if range_value is not None else None
        manifest_path = Path(manifest)
        if manifest_path.name != "manifest.json" or manifest_path.is_symlink() or manifest_path.stat().st_size > 2 * 1024 * 1024:
            return _inspection_error("invalid_manifest")
        root = manifest_path.resolve().parent
        manifest_bytes = manifest_path.read_bytes()
        document = json.loads(manifest_bytes)
        if not isinstance(document, dict) or document.get("kind") != "timeline_filmstrip":
            return _inspection_error("invalid_manifest")
        members = {}
        declared = document.get("outputs")
        if not isinstance(declared, list) or len(declared) > 10000:
            return _inspection_error("invalid_manifest")
        for member in declared:
            if not isinstance(member, dict):
                return _inspection_error("invalid_manifest")
            name = _relative_path(member.get("path"))
            if name in members:
                return _inspection_error("invalid_manifest")
            members[name] = member

        def verified(name: str) -> Path:
            name = _relative_path(name)
            path = root / name
            entry = members.get(name)
            if not entry or path.is_symlink() or not path.is_file() or not path.resolve().is_relative_to(root):
                raise ValueError("unsafe_member")
            if path.stat().st_size != entry.get("bytes"):
                raise ValueError("integrity_mismatch")
            with path.open("rb") as stream:
                digest = "sha256:" + hashlib.file_digest(stream, "sha256").hexdigest()
            if digest != entry.get("content_hash"):
                raise ValueError("integrity_mismatch")
            return path

        def read_json(name: str) -> dict:
            path = verified(name)
            if path.stat().st_size > 64 * 1024 * 1024:
                raise ValueError("member_too_large")
            value = json.loads(path.read_bytes())
            if not isinstance(value, dict):
                raise ValueError("invalid_member")
            return value

        index = read_json("frame-index.json")
        if index.get("schema") not in ("astrid.filmstrip.v1", "astrid.filmstrip.v2"):
            return _inspection_error("unsupported_schema")
        for field, filename in (("snapshot_sidecar", "render-snapshot.json"), ("audio_sidecar", "audio-analysis.json")):
            reference = index.get(field)
            if reference is not None:
                entry = members.get(filename, {})
                if (not isinstance(reference, dict) or reference.get("path") != filename
                        or reference.get("digest") != entry.get("content_hash") or reference.get("bytes") != entry.get("bytes")):
                    return _inspection_error("integrity_mismatch")
        identity = _projection(index.get("provenance", {}), _IDENTITY_KEYS)
        fps = Fraction(*(index.get("provenance", {}).get("fps_rational") or [1, 1]))
        identity["fps_rational"] = [fps.numerator, fps.denominator]
        if fps <= 0:
            return _inspection_error("invalid_member")
        query = {"section": section, "limit": limit, **selectors}
        binding = hashlib.sha256(manifest_bytes + json.dumps(query, sort_keys=True).encode()).hexdigest()
        offset = 0
        if cursor is not None:
            if not isinstance(cursor, str) or len(cursor) > 100:
                return _inspection_error("invalid_cursor")
            parts = cursor.split(":")
            if len(parts) != 2 or parts[0] != binding or not parts[1].isascii() or not parts[1].isdigit():
                return _inspection_error("invalid_cursor")
            offset = int(parts[1])
        cards = index.get("cards", [])
        if not isinstance(cards, list):
            return _inspection_error("invalid_member")
        snapshot = None

        def frozen():
            nonlocal snapshot
            if snapshot is None:
                if "render-snapshot.json" not in members:
                    raise ValueError("evidence_unavailable")
                snapshot = read_json("render-snapshot.json")
                for key in ("render_run_id", "video_digest", "timeline_id"):
                    if snapshot.get(key) != identity.get(key):
                        raise ValueError("integrity_mismatch")
            return snapshot

        def selected(item, *, is_card=False):
            if not isinstance(item, dict):
                raise ValueError("invalid_member")
            if is_card:
                if frame is not None and item.get("frame") != frame:
                    return False
                if card is not None and item.get("id") != card:
                    return False
                at = Fraction(item["frame"], 1) / fps
                if window and not Fraction(*window["start"]) <= at < Fraction(*window["end"]):
                    return False
                if any(value is not None for value in (shot, occurrence, clip, track, asset)):
                    return any(selected(row) and _frame_span_for_inspection(row, fps)[0] <= item["frame"] < _frame_span_for_inspection(row, fps)[1]
                               for row in frozen().get("clips", []))
                return True
            for wanted, fields in ((shot, ("shot_id", "shot_name")), (occurrence, ("occurrence_id", "shot_occurrence_id")),
                                   (clip, ("id",)), (track, ("track",)), (asset, ("asset",))):
                if wanted is not None and not any(item.get(key) == wanted for key in fields):
                    return False
            start, end = _frame_span_for_inspection(item, fps)
            if frame is not None and not start <= frame < end:
                return False
            if card is not None:
                matching = [row for row in cards if row.get("id") == card]
                if not matching or not start <= matching[0]["frame"] < end:
                    return False
            return not window or (Fraction(start, 1) / fps < Fraction(*window["end"]) and Fraction(end, 1) / fps > Fraction(*window["start"]))

        records = []
        if section == "summary":
            summary = {
                **identity,
                "sample_count": len(cards),
                "sections": list(INSPECTION_SECTIONS),
                "canonical_timeline": _projection(index.get("canonical_timeline", {}), ("timeline_ref", "config_version", "config_hash")),
                "note": "Samples are rendered evidence; placements and shot scripts are declarations, not proof of pixels, speech timing or silence.",
            }
            components = _component_list(index.get("components"))
            if components is not None:
                summary["components"] = components
            component_request = _component_request_projection(index.get("component_request"))
            if component_request is not None:
                summary["component_request"] = component_request
            sampling = _sampling_projection(index.get("sampling"))
            if sampling:
                summary["sampling"] = sampling
            # The bounded summary reports the surface contract, while the
            # receipt retains row-level timing/track metadata for offline
            # consumers that need to inspect the paired layout in detail.
            surface = _surface_projection(index.get("static_surface"), include_rows=False)
            if surface is not None:
                summary["static_surface"] = surface
            records = [summary]
        elif section == "pages":
            for name in sorted(members):
                if PurePosixPath(name).name.startswith("filmstrip-") and name.endswith(".png"):
                    verified(name)
                    records.append({"path": name})
        elif section == "cards":
            for item in cards:
                if selected(item, is_card=True):
                    verified(item["image"])
                    row = _projection(item, ("id", "frame", "time_seconds", "image"))
                    row["sample_reasons"] = [_small_scalar(reason, 64) for reason in item.get("sample_reasons", [])[:16]]
                    records.append(row)
        elif section in ("placements", "boundaries"):
            # Input-only projections may keep source placements in
            # ``input_clips`` while rendered cards use the composited ``clips``
            # list. Prefer the explicit source list when present, without
            # copying it into the compact receipt.
            placement_clips = frozen().get("input_clips") or frozen().get("clips", [])
            for item in placement_clips:
                if not selected(item):
                    continue
                start, end = _frame_span_for_inspection(item, fps)
                row = _projection(item, ("id", "shot_id", "shot_name", "occurrence_id", "shot_occurrence_id", "track", "asset", "kind", "from", "to", "speed"))
                row.update(start_frame=start, end_frame=end, evidence="declared_placement")
                if section == "placements":
                    records.append(row)
                else:
                    captured = {item["frame"] for item in cards}
                    for boundary in (start, end):
                        records.append({"clip_id": row.get("id"), "boundary_frame": boundary,
                                        "before_captured": boundary - 1 in captured, "after_captured": boundary in captured,
                                        "note": "Uncaptured adjacent frames require a pinned visualize --range refinement."})
        else:
            # Input-only filmstrips have placement-level audio signifiers but
            # no rendered composite to analyse. Return a truthful bounded
            # status instead of treating the absent sidecar as a malformed
            # bundle; rendered/paired packs still require the verified audio
            # analysis sidecar and digest match below.
            if "audio-analysis.json" not in members and identity.get("video_digest") is None:
                records = [{
                    "status": "not_available",
                    "analysis_identity": None,
                    "render_digest": None,
                    "evidence": "input-only placement audio signifiers; no composite audio analysis was requested",
                }]
                # Keep the shared bounded projection loop below inert for this
                # status-only response while avoiding a fabricated waveform.
                audio = {}
            else:
                audio = read_json("audio-analysis.json")
                if audio.get("render_digest") != identity.get("video_digest"):
                    return _inspection_error("integrity_mismatch")
                records = [_projection(audio, ("status", "analysis_identity", "render_digest"))]
                records[0]["evidence"] = "composite_audio_analysis; low energy does not establish perceptual silence"

            def audio_interval(start, end):
                start, end = _rational(start), _rational(end)
                if end <= start:
                    return None
                if window and not (start < Fraction(*window["end"]) and end > Fraction(*window["start"])):
                    return None
                return {"start_seconds": float(start), "end_seconds": float(end)}

            for gap in audio.get("quiet_gaps", []):
                interval = audio_interval(gap.get("start"), gap.get("end"))
                if interval:
                    records.append({"kind": "low_energy", **interval, **_projection(gap, ("threshold", "measurement"))})
            speech = audio.get("speech") or {}
            for phrase in speech.get("phrases", []):
                timing = phrase.get("render_interval") or phrase
                interval = audio_interval(timing.get("start"), timing.get("end"))
                if interval:
                    text = phrase.get("canonical_text") or phrase.get("text") or ""
                    safe_text = "[inline data omitted]" if not isinstance(text, str) or "data:" in text.lower() else text[:256]
                    records.append({"kind": "speech_annotation", **interval, "text_excerpt": safe_text,
                                    "truncated": isinstance(text, str) and len(text) > 256,
                                    **_projection(phrase, ("timing_basis", "timing_method", "status", "word_aligned"))})
            stream = audio.get("stream") or {}
            sample_rate = stream.get("sample_rate")
            levels = (audio.get("waveform") or {}).get("levels") or []
            if type(sample_rate) is int and sample_rate > 0 and levels:
                # One existing resolution only. Never expand every level into
                # target graphs or imply that a bounded excerpt is complete.
                level = min(levels, key=lambda item: len(item.get("bins") or []))
                origin = _rational((audio.get("presentation_origin") or {}).get("seconds", [0, 1]))
                bins = []
                for item in level.get("bins", []):
                    interval = audio_interval(origin + Fraction(item["start_sample"], sample_rate),
                                              origin + Fraction(item["end_sample"], sample_rate))
                    if interval:
                        bins.append({"kind": "waveform_bin", **interval,
                                     **_projection(item, ("index", "min", "max", "peak", "rms"))})
                records[0]["waveform_bins_available"] = len(bins)
                records[0]["waveform_bins_returned"] = min(128, len(bins))
                records[0]["waveform_excerpt"] = len(bins) > 128
                records.extend(bins[:128])
        if offset > len(records):
            return _inspection_error("invalid_cursor")
        batch = records[offset:offset + limit]
        while True:
            next_offset = offset + len(batch)
            data = {"section": section, "render_run_id": identity.get("render_run_id"), "records": batch,
                    "next_cursor": f"{binding}:{next_offset}" if next_offset < len(records) else None}
            result = {"ok": True, "data": data, "error": None, "receipt": None, "idempotency_key": ""}
            if len((json.dumps(result, ensure_ascii=True, separators=(",", ":")) + "\n").encode()) <= INSPECTION_MAX_BYTES:
                return result
            if len(batch) <= 1:
                return _inspection_error("record_too_large")
            batch.pop()
    except (OSError, UnicodeError, json.JSONDecodeError, TypeError, KeyError, AttributeError, OverflowError, RecursionError, ZeroDivisionError):
        return _inspection_error("invalid_bundle")
    except ValueError as exc:
        code = str(exc)
        return _inspection_error(code if code in {"unsafe_member", "integrity_mismatch", "member_too_large", "invalid_member", "evidence_unavailable"} else "invalid_query")


def _frame_span_for_inspection(item: Mapping, fps: Fraction) -> tuple[int, int]:
    if isinstance(item.get("start_frame"), int) and isinstance(item.get("end_frame"), int):
        return item["start_frame"], item["end_frame"]
    timing = dict(item)
    if item.get("duration") is not None:
        timing.update(hold=float(item["duration"]), speed=1)
    return clip_start_frame(timing, float(fps)), clip_end_frame(timing, float(fps))
