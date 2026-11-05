from __future__ import annotations

import json
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any

from astrid.pack_api import load_arrangement, load_metadata, load_pool, load_timeline

from .text_match import segments_in_range


class ZoneKind(str, Enum):
    AUDIO_DEAD = "audio_dead"
    VIDEO_DEAD = "video_dead"


class FindingSeverity(str, Enum):
    AUTO_FIX = "auto_fix"
    FLAG = "flag"


@dataclass(slots=True)
class QualityZone:
    kind: ZoneKind
    start: float
    end: float


@dataclass(slots=True)
class ReviewerFinding:
    clip_order: int
    code: str
    severity: FindingSeverity
    message: str
    proposed_patch: dict[str, Any] | None = None
    reviewer: str | None = None
    clip_uuid: str | None = None


@dataclass(slots=True)
class ReviewerResult:
    reviewer: str
    findings: list[ReviewerFinding] = field(default_factory=list)


@dataclass(slots=True)
class EnrichedClip:
    order: int
    asset_key: str
    clip: dict[str, Any]
    pool_entry: dict[str, Any] | None
    audio_pool_entry: dict[str, Any] | None
    visual_pool_entry: dict[str, Any] | None
    timeline_clips: dict[str, dict[str, Any]]
    audio_timeline_clip: dict[str, Any] | None
    primary_visual_timeline_clip: dict[str, Any] | None
    overlay_timeline_clip: dict[str, Any] | None
    text_timeline_clip: dict[str, Any] | None
    transcript_segments: list[dict[str, Any]]
    scenes: list[dict[str, Any]]
    zones: list[QualityZone]

    @property
    def uuid(self) -> str:
        return str(self.clip["uuid"])


@dataclass(slots=True)
class EnrichedArrangement:
    run_dir: Path
    arrangement: dict[str, Any]
    arrangement_path: Path
    timeline: dict[str, Any] | None
    timeline_path: Path
    metadata: dict[str, Any]
    metadata_path: Path
    pool: dict[str, Any]
    pool_path: Path
    pool_by_id: dict[str, dict[str, Any]]
    clips: list[EnrichedClip]
    clips_by_order: dict[int, EnrichedClip]
    transcript_by_asset: dict[str, list[dict[str, Any]]]
    scenes_by_asset: dict[str, list[dict[str, Any]]]
    zones_by_asset: dict[str, list[QualityZone]]


def expected_text_for_clip(clip: EnrichedClip) -> str:
    audio_source = clip.clip.get("audio_source")
    trim_range = audio_source.get("trim_sub_range") if isinstance(audio_source, dict) else None
    if isinstance(trim_range, list) and len(trim_range) == 2:
        try:
            trim_start, trim_end = map(float, trim_range)
        except (TypeError, ValueError):
            scoped = None
        else:
            scoped = _joined_text(segments_in_range(clip.transcript_segments, trim_start, trim_end))
        if scoped:
            return scoped.strip()
    entry = clip.audio_pool_entry or {}
    return str(entry.get("text", "")).strip()


def load(run_dir: Path) -> EnrichedArrangement:
    run_dir = Path(run_dir)
    arrangement_path = run_dir / "arrangement.json"
    timeline_path = run_dir / "hype.timeline.json"
    metadata_path = run_dir / "hype.metadata.json"
    pool_path = run_dir.parent.parent / "pool.json"

    arrangement = load_arrangement(arrangement_path, assign_missing_uuids=True)
    timeline_data = load_timeline(timeline_path) if timeline_path.exists() else None
    metadata = load_metadata(metadata_path)
    pool = load_pool(pool_path)
    pool_by_id = {
        str(entry["id"]): dict(entry)
        for entry in pool.get("entries", [])
        if isinstance(entry, dict) and "id" in entry
    }
    sources = metadata.get("sources", {}) if isinstance(metadata, dict) else {}
    transcript_by_asset = {
        str(asset): _load_transcript(source)
        for asset, source in sources.items()
        if isinstance(source, dict)
    }
    scenes_by_asset = {
        str(asset): _load_scenes(source)
        for asset, source in sources.items()
        if isinstance(source, dict)
    }
    zones_by_asset = {
        str(asset): _load_quality_zones(source)
        for asset, source in sources.items()
        if isinstance(source, dict)
    }
    timeline_by_order = _timeline_by_order(timeline_data)
    clips: list[EnrichedClip] = []
    clips_by_order: dict[int, EnrichedClip] = {}
    for raw_clip in arrangement.get("clips", []):
        if not isinstance(raw_clip, dict):
            continue
        clip = _build_enriched_clip(
            raw_clip,
            pool_by_id,
            timeline_by_order.get(int(raw_clip["order"]), {}),
            transcript_by_asset,
            scenes_by_asset,
            zones_by_asset,
        )
        clips.append(clip)
        clips_by_order[clip.order] = clip
    return EnrichedArrangement(
        run_dir=run_dir,
        arrangement=arrangement,
        arrangement_path=arrangement_path,
        timeline=timeline_data,
        timeline_path=timeline_path,
        metadata=metadata,
        metadata_path=metadata_path,
        pool=pool,
        pool_path=pool_path,
        pool_by_id=pool_by_id,
        clips=clips,
        clips_by_order=clips_by_order,
        transcript_by_asset=transcript_by_asset,
        scenes_by_asset=scenes_by_asset,
        zones_by_asset=zones_by_asset,
    )


def apply_auto_fixes(enriched: EnrichedArrangement, findings: list[ReviewerFinding]) -> None:
    for finding in findings:
        if finding.severity is not FindingSeverity.AUTO_FIX or not finding.proposed_patch:
            continue
        clip = enriched.clips_by_order.get(int(finding.clip_order))
        if clip is None:
            continue
        for key, value in finding.proposed_patch.items():
            if key == "trim_after":
                _set_path(clip.clip, ["audio_source", "trim_sub_range"], value)
            elif key == "audio_pool_id":
                _set_path(clip.clip, ["audio_source", "pool_id"], value)
            elif key == "visual_pool_id":
                _set_path(clip.clip, ["visual_source", "pool_id"], value)
            else:
                _set_path(clip.clip, key.split("."), value)
        _refresh_clip(clip, enriched)


def _build_enriched_clip(
    clip: dict[str, Any],
    pool_by_id: dict[str, dict[str, Any]],
    timeline_clips: dict[str, dict[str, Any]],
    transcript_by_asset: dict[str, list[dict[str, Any]]],
    scenes_by_asset: dict[str, list[dict[str, Any]]],
    zones_by_asset: dict[str, list[QualityZone]],
) -> EnrichedClip:
    order = int(clip["order"])
    audio_pool_entry = _pool_entry(pool_by_id, clip.get("audio_source"))
    visual_pool_entry = _pool_entry(pool_by_id, clip.get("visual_source"))
    asset_key = _asset_key(audio_pool_entry, visual_pool_entry)
    return EnrichedClip(
        order=order,
        asset_key=asset_key,
        clip=clip,
        pool_entry=audio_pool_entry or visual_pool_entry,
        audio_pool_entry=audio_pool_entry,
        visual_pool_entry=visual_pool_entry,
        timeline_clips=timeline_clips,
        audio_timeline_clip=_first_clip(timeline_clips, "clip_a_"),
        primary_visual_timeline_clip=_first_clip(timeline_clips, "clip_v1_"),
        overlay_timeline_clip=_first_clip(timeline_clips, "clip_v2_"),
        text_timeline_clip=_first_clip(timeline_clips, "clip_t_"),
        transcript_segments=list(transcript_by_asset.get(asset_key, [])),
        scenes=list(scenes_by_asset.get(asset_key, [])),
        zones=list(zones_by_asset.get(asset_key, [])),
    )


def _refresh_clip(clip: EnrichedClip, enriched: EnrichedArrangement) -> None:
    clip.audio_pool_entry = _pool_entry(enriched.pool_by_id, clip.clip.get("audio_source"))
    clip.visual_pool_entry = _pool_entry(enriched.pool_by_id, clip.clip.get("visual_source"))
    clip.pool_entry = clip.audio_pool_entry or clip.visual_pool_entry
    clip.asset_key = _asset_key(clip.audio_pool_entry, clip.visual_pool_entry)
    clip.transcript_segments = list(enriched.transcript_by_asset.get(clip.asset_key, []))
    clip.scenes = list(enriched.scenes_by_asset.get(clip.asset_key, []))
    clip.zones = list(enriched.zones_by_asset.get(clip.asset_key, []))


def _pool_entry(pool_by_id: dict[str, dict[str, Any]], source: Any) -> dict[str, Any] | None:
    if not isinstance(source, dict) or "pool_id" not in source:
        return None
    entry = pool_by_id.get(str(source["pool_id"]))
    return dict(entry) if entry else None


def _asset_key(*entries: dict[str, Any] | None) -> str:
    for entry in entries:
        if entry is not None:
            return str(entry.get("asset") or "main")
    return "main"


def _joined_text(segments: list[dict[str, Any]]) -> str | None:
    joined = " ".join(str(segment.get("text", "")).strip() for segment in segments if str(segment.get("text", "")).strip()).strip()
    return joined or None


def _timeline_by_order(timeline_data: dict[str, Any] | None) -> dict[int, dict[str, dict[str, Any]]]:
    grouped: dict[int, dict[str, dict[str, Any]]] = {}
    if not timeline_data:
        return grouped
    for clip in timeline_data.get("clips", []):
        if not isinstance(clip, dict) or "id" not in clip:
            continue
        prefix, _, suffix = str(clip["id"]).rpartition("_")
        if not suffix.isdigit():
            continue
        grouped.setdefault(int(suffix), {})[str(clip["id"])] = dict(clip)
    return grouped


def _first_clip(timeline_clips: dict[str, dict[str, Any]], prefix: str) -> dict[str, Any] | None:
    for clip_id, clip in timeline_clips.items():
        if clip_id.startswith(prefix):
            return clip
    return None


def _load_transcript(source: dict[str, Any]) -> list[dict[str, Any]]:
    payload = _load_ref(source.get("transcript_ref"))
    if isinstance(payload, dict) and isinstance(payload.get("segments"), list):
        return [dict(segment) for segment in payload["segments"] if isinstance(segment, dict)]
    return []


def _load_scenes(source: dict[str, Any]) -> list[dict[str, Any]]:
    payload = _load_ref(source.get("scenes_ref"))
    if isinstance(payload, list):
        return [dict(scene) for scene in payload if isinstance(scene, dict)]
    if isinstance(payload, dict) and isinstance(payload.get("scenes"), list):
        return [dict(scene) for scene in payload["scenes"] if isinstance(scene, dict)]
    return []


def _load_quality_zones(source: dict[str, Any]) -> list[QualityZone]:
    payload = _load_ref(source.get("quality_zones_ref"))
    if not isinstance(payload, dict) or not isinstance(payload.get("zones"), list):
        return []
    zones: list[QualityZone] = []
    for zone in payload["zones"]:
        if not isinstance(zone, dict):
            continue
        try:
            zones.append(
                QualityZone(
                    kind=ZoneKind(str(zone["kind"])),
                    start=float(zone["start"]),
                    end=float(zone["end"]),
                )
            )
        except (KeyError, TypeError, ValueError):
            continue
    return zones


def _load_ref(ref: Any) -> Any:
    if not ref:
        return None
    path = Path(str(ref))
    if not path.exists():
        return None
    return json.loads(path.read_text(encoding="utf-8"))


def _set_path(target: dict[str, Any], path: list[str], value: Any) -> None:
    cursor = target
    for key in path[:-1]:
        child = cursor.get(key)
        if not isinstance(child, dict):
            child = {}
            cursor[key] = child
        cursor = child
    cursor[path[-1]] = value
