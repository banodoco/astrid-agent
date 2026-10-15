"""Read-only projection of the adopted music bed for timeline consumers.

The producer remains :mod:`music_beds` and :mod:`bed_adoption`: this module
assembles their validated ordinary output clips, resolved switch windows, and
shared seams into one consumer model.  It never materializes or edits spans.
"""
from __future__ import annotations

import copy
import json
import math
import shlex
from collections.abc import Mapping
from typing import Any

from astrid.core.timeline.compatibility import (
    ADOPTED_BED_CAPABILITY,
    WRITER_CAPABILITIES,
    TimelineCompatibilityError,
    require_writable_parent,
)
from astrid.core.timeline.cuts import clip_duration
from astrid.sdk.timeline.model import Clip


class AudioLaneError(ValueError):
    """An immutable timeline snapshot cannot represent an adopted bed safely."""


def _gain_db(value: float) -> float | None:
    return round(20 * math.log10(value), 6) if value > 0 else None


def _source_edit(owner: str, gain: float, *, project: str, timeline: str) -> dict[str, Any]:
    operation = {
        "op": "clip.set-look",
        "select": owner,
        "args": {"volume": gain},
    }
    command = shlex.join([
        "python3", "-m", "astrid", "timelines", "edit", timeline,
        "--project", project,
        "--command", json.dumps(operation, ensure_ascii=False, separators=(",", ":")),
    ])
    return {"operation": operation, "command": command}


def project(tl, *, capabilities=None) -> dict[str, Any] | None:
    """Return the complete adopted-bed projection, or ``None`` when no bed exists.

    The input checkout is not mutated. ``capabilities`` is passed to Astrid's
    existing compatibility guard so an older consumer refuses an adopted bed
    explicitly instead of silently omitting it.
    """
    from astrid.sdk.timeline import music_beds
    from astrid.sdk.timeline.bed_adoption import switch_windows
    from astrid.sdk.timeline.checkout import Checkout

    bed = music_beds.adopted(tl)
    if bed is None:
        return None
    supported = WRITER_CAPABILITIES if capabilities is None else capabilities
    require_writable_parent(tl.bundle.get("parent"), source="audio lane consumer", capabilities=supported)

    # Producer validation has read helpers that may fill absent containers via
    # setdefault. Work on a detached Checkout so this API is observably pure.
    view = Checkout(copy.deepcopy(tl.bundle))
    bed = music_beds.adopted(view)
    if bed is None:  # defensive against a malformed caller-side mutation
        return None
    seams = music_beds.validate(view) or []
    windows = switch_windows(view, bed)
    beds = music_beds.sources(view)
    arrangements = []
    timeline_id = str(view.bundle.get("timeline_id") or "<timeline>")
    project_id = str(view.bundle.get("project_id") or "<project>")

    for index, arrangement in enumerate(bed["arrangements"]):
        owner = arrangement["owner"]
        source = arrangement["source"]
        entry = beds.get(owner)
        if entry is None or entry.get("source") is not source:
            raise AudioLaneError(f"{owner}: adopted arrangement has no exact authored-source record")
        sid, clips = music_beds.locate(view, owner, source)
        ids = bed["outputs"].get(arrangement["id"])
        if not isinstance(ids, list):
            raise AudioLaneError(f"{arrangement['id']}: missing generated output ownership")
        by_id = {str(raw.get("id")): raw for raw in clips if isinstance(raw, Mapping)}
        missing = [clip_id for clip_id in ids if clip_id not in by_id]
        if missing:
            raise AudioLaneError(f"{owner}: generated output clips are missing: {', '.join(missing)}")

        gain = float(arrangement["gain"])
        source_edit = _source_edit(owner, gain, project=project_id, timeline=timeline_id)
        spans = []
        for clip_id in ids:
            raw = by_id[clip_id]
            clip = Clip(view, sid, raw)
            speed = float(raw.get("speed", 1) or 1)
            source_start = float(raw.get("from", 0) or 0)
            source_end = source_start + clip_duration(raw) * speed
            params = raw.get("params") if isinstance(raw.get("params"), Mapping) else {}
            spans.append({
                "id": clip_id,
                "asset": str(raw.get("asset") or arrangement["asset"]),
                "timeline": {
                    "start_s": clip.start,
                    "end_s": clip.end,
                    "start_frame": clip.start_frame,
                    "end_frame": clip.end_frame,
                },
                "source": {"start_s": round(source_start, 6), "end_s": round(source_end, 6)},
                "gain": {"linear": gain, "db": _gain_db(gain)},
                "fade_in_s": params.get("fadeIn"),
                "fade_out_s": params.get("fadeOut"),
                "generated": True,
                "read_only": True,
                "edit_source": copy.deepcopy(source_edit),
            })
        arrangements.append({
            "id": arrangement["id"],
            "owner": owner,
            "asset": arrangement["asset"],
            "rate": float(arrangement["rate"]),
            "gain": {"linear": gain, "db": _gain_db(gain)},
            "source_offset_s": float(arrangement["source_offset_s"]),
            "spans": spans,
            "edit_source": source_edit,
        })

    switches = []
    for index, (switch, bounds) in enumerate(zip(bed["switches"], windows)):
        lo, hi = map(float, bounds)
        center = (lo + hi) / 2
        switches.append({
            "id": switch["id"],
            "from_arrangement": bed["arrangements"][index]["id"],
            "to_arrangement": bed["arrangements"][index + 1]["id"],
            "on": {"time_ref": switch["on"], "center_s": center, "center_frame": view.frame(center)},
            "window": {
                "start_s": lo,
                "end_s": hi,
                "start_frame": view.frame(lo),
                "end_frame": view.frame(hi),
            },
            "curve": switch["curve"],
        })

    seam_rows = []
    for at, delta in seams:
        at_s, delta_s = float(at), float(delta)
        seam_rows.append({
            "at_s": at_s,
            "frame": view.frame(at_s),
            "action": "repeat" if delta_s > 0 else "skip" if delta_s < 0 else "none",
            "delta_s": delta_s,
            "duration_s": abs(delta_s),
        })

    return {
        "schema": "astrid.audio-lane.v1",
        "capability": ADOPTED_BED_CAPABILITY,
        "owner": bed["owner"],
        "fps": float(view.fps),
        "arrangements": arrangements,
        "switches": switches,
        "seams": seam_rows,
    }


def _snapshot_checkout(config: Mapping[str, Any], registry: Mapping[str, Any], *, fps: float | None = None):
    """Adapt an immutable flattened visualize snapshot back to Checkout's read model.

    The snapshot's occurrence proof, clip provenance, and asset namespace are
    retained. No timeline placement, source identity, or TimeRef is guessed.
    """
    from astrid.sdk.timeline.checkout import Checkout

    raw_app = config.get("app") if isinstance(config.get("app"), Mapping) else {}
    composition = raw_app.get("astrid_shot_composition")
    composition = composition if isinstance(composition, Mapping) else {}
    occurrences = composition.get("occurrences", [])
    if not isinstance(occurrences, list):
        raise AudioLaneError("adopted bed snapshot has no exact occurrence list")

    root_assets = registry.get("assets") if isinstance(registry.get("assets"), Mapping) else {}
    placements = []
    shots: dict[str, dict[str, Any]] = {}
    occurrence_sids: dict[str, str] = {}
    used_sids: set[str] = set()
    for index, occurrence in enumerate(occurrences):
        if not isinstance(occurrence, Mapping):
            raise AudioLaneError(f"snapshot occurrence {index} is malformed")
        oid, original_sid = occurrence.get("occurrence_id"), occurrence.get("shot_id")
        if not isinstance(oid, str) or not oid or not isinstance(original_sid, str) or not original_sid:
            raise AudioLaneError(f"snapshot occurrence {index} lacks exact identities")
        start_ms = occurrence.get("at_ms")
        if start_ms is None:
            placement = occurrence.get("placement") if isinstance(occurrence.get("placement"), Mapping) else {}
            start_ms = placement.get("start_ms")
        if start_ms is None and isinstance(occurrence.get("start"), (int, float)):
            start_ms = round(float(occurrence["start"]) * 1000)
        duration_ms = occurrence.get("duration_ms")
        if duration_ms is None and isinstance(occurrence.get("duration_seconds"), (int, float)):
            duration_ms = round(float(occurrence["duration_seconds"]) * 1000)
        if start_ms is None or duration_ms is None:
            raise AudioLaneError(f"{oid}: snapshot occurrence has no exact placement window")
        sid = original_sid if original_sid not in used_sids else f"{original_sid}@{oid}"
        if sid in used_sids:
            raise AudioLaneError(f"{oid}: duplicate occurrence-to-shot mapping")
        used_sids.add(sid)
        occurrence_sids[oid] = sid
        placements.append({
            "shot_id": sid,
            "occurrence_id": oid,
            "at_ms": start_ms,
            "duration_ms": duration_ms,
        })
        internal_assets = {
            str(key)[len(oid) + 1:]: copy.deepcopy(dict(value))
            for key, value in root_assets.items()
            if isinstance(key, str) and key.startswith(f"{oid}:") and isinstance(value, Mapping)
        }
        shots[sid] = {
            "payload": {"name": str(occurrence.get("name") or original_sid)},
            "internal_timeline": {
                "tracks": copy.deepcopy(list(config.get("tracks") or [])),
                "clips": [],
                "registry": {"assets": internal_assets},
            },
        }

    parent_config = copy.deepcopy(dict(config))
    parent_config["clips"] = []
    for raw in config.get("clips") or []:
        if not isinstance(raw, Mapping):
            continue
        app = raw.get("app") if isinstance(raw.get("app"), Mapping) else {}
        provenance = app.get("astrid_shot_composition") if isinstance(app.get("astrid_shot_composition"), Mapping) else {}
        oid = raw.get("shot_occurrence_id") or raw.get("occurrence_id") or provenance.get("occurrence_id")
        if not isinstance(oid, str) or oid not in occurrence_sids:
            parent_config["clips"].append(copy.deepcopy(dict(raw)))
            continue
        occurrence = next(row for row in occurrences if row.get("occurrence_id") == oid)
        start_ms = occurrence.get("at_ms")
        if start_ms is None:
            placement = occurrence.get("placement") if isinstance(occurrence.get("placement"), Mapping) else {}
            start_ms = placement.get("start_ms")
        if start_ms is None:
            start_ms = round(float(occurrence.get("start", 0)) * 1000)
        clip = copy.deepcopy(dict(raw))
        source_id = provenance.get("source_clip_id")
        if not isinstance(source_id, str) or not source_id:
            prefix = f"{oid}:"
            source_id = clip.get("id", "")
            if isinstance(source_id, str) and source_id.startswith(prefix):
                source_id = source_id[len(prefix):]
        if not isinstance(source_id, str) or not source_id:
            raise AudioLaneError(f"{oid}: flattened clip lacks source identity")
        clip["id"] = source_id
        clip["at"] = round(float(clip.get("at", 0)) - float(start_ms) / 1000, 6)
        raw_app = clip.get("app")
        if isinstance(raw_app, dict):
            raw_app.pop("astrid_shot_composition", None)
            if not raw_app:
                clip.pop("app", None)
        for key in ("shot_id", "shot_name", "shot_occurrence_id", "occurrence_id", "occurrence_ids"):
            clip.pop(key, None)
        for key in ("asset", "asset_id"):
            value = clip.get(key)
            prefix = f"{oid}:"
            if isinstance(value, str) and value.startswith(prefix):
                clip[key] = value[len(prefix):]
        shots[occurrence_sids[oid]]["internal_timeline"]["clips"].append(clip)

    parent_assets = {
        str(key): copy.deepcopy(dict(value))
        for key, value in root_assets.items()
        if isinstance(key, str) and not any(key.startswith(f"{oid}:") for oid in occurrence_sids)
        and isinstance(value, Mapping)
    }
    parent = {"config": parent_config, "clips": parent_config["clips"],
              "registry": {"assets": parent_assets}}
    bundle = {
        "project_id": config.get("project_id") or "<project>",
        "timeline_id": config.get("timeline_id") or "<timeline>",
        "parent": parent,
        "placements": placements,
        "shots": shots,
    }
    if fps is not None:
        canvas = parent_config.setdefault("theme_overrides", {}).setdefault("visual", {}).setdefault("canvas", {})
        canvas.setdefault("fps", fps)
    return Checkout(bundle)


def project_snapshot(snapshot: Mapping[str, Any], *, capabilities=None) -> dict[str, Any] | None:
    """Project the adopted bed from the immutable config/registry visualize input."""
    config = snapshot.get("config") if isinstance(snapshot.get("config"), Mapping) else None
    if config is None:
        return None
    app = config.get("app") if isinstance(config.get("app"), Mapping) else {}
    ns = app.get("astrid") if isinstance(app.get("astrid"), Mapping) else {}
    if ns.get("bed") is None:
        return None
    registry = snapshot.get("registry") if isinstance(snapshot.get("registry"), Mapping) else {}
    fps_raw = snapshot.get("fps")
    if fps_raw is None:
        canvas = (config.get("theme_overrides") or {}).get("visual", {}).get("canvas", {})
        fps_raw = canvas.get("fps") if isinstance(canvas, Mapping) else None
    try:
        view = _snapshot_checkout(config, registry, fps=float(fps_raw) if fps_raw is not None else None)
    except (KeyError, TypeError, ValueError, OverflowError) as exc:
        if isinstance(exc, AudioLaneError):
            raise
        raise AudioLaneError(f"adopted bed snapshot cannot be read: {exc}") from exc
    view.bundle["project_id"] = snapshot.get("project_id") or view.bundle.get("project_id")
    view.bundle["timeline_id"] = snapshot.get("timeline_id") or view.bundle.get("timeline_id")
    return project(view, capabilities=capabilities)


__all__ = ["ADOPTED_BED_CAPABILITY", "AudioLaneError", "TimelineCompatibilityError", "project", "project_snapshot"]
