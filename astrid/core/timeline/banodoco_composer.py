from __future__ import annotations

import copy
import json
import sys
import uuid
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any, cast

from .banodoco_schema import (
    Arrangement,
    AssetRegistry,
    ClipClassifiedKind,
    PipelineMetadata,
    Pool,
    ThemeOverrides,
    TimelineClip,
    TimelineConfig,
    TimelineOutput,
    TrackDefinition,
    validate_arrangement,
    validate_metadata,
    validate_pool,
    validate_registry,
    validate_timeline,
)
from .reader_projection import reader_fps_view


def _write_json(path: Path, payload: Any) -> None:
    Path(path).write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")


def _swap_from_load(clip: dict[str, Any]) -> dict[str, Any]:
    if "from" in clip:
        clip["from_"] = clip.pop("from")
    return clip


def _swap_from_dump(clip: dict[str, Any]) -> dict[str, Any]:
    out = dict(clip)
    if "from_" in out:
        out["from"] = out.pop("from_")
    return out


def _round_at_for_dump(clip: dict[str, Any]) -> dict[str, Any]:
    if "at" in clip and isinstance(clip["at"], float):
        clip["at"] = round(float(clip["at"]), 3)
    return clip

def _asset_entry_for_clip(
    clip: Mapping[str, Any],
    registry: AssetRegistry | Mapping[str, Any] | None,
) -> Mapping[str, Any] | None:
    asset_key = clip.get("asset")
    if not isinstance(asset_key, str) or not isinstance(registry, Mapping):
        return None
    assets = registry.get("assets")
    if not isinstance(assets, Mapping):
        return None
    entry = assets.get(asset_key)
    return entry if isinstance(entry, Mapping) else None


def _asset_kind_from_entry(entry: Mapping[str, Any] | None) -> ClipClassifiedKind | None:
    if not isinstance(entry, Mapping):
        return None
    raw_type = entry.get("type")
    if isinstance(raw_type, str):
        normalized = raw_type.lower()
        if normalized in {"video", "image", "audio"}:
            return ClipClassifiedKind(normalized)
        if normalized.startswith("video/"):
            return ClipClassifiedKind.VIDEO
        if normalized.startswith("image/"):
            return ClipClassifiedKind.IMAGE
        if normalized.startswith("audio/"):
            return ClipClassifiedKind.AUDIO
    for key in ("mime", "mimeType", "contentType"):
        mime = entry.get(key)
        if not isinstance(mime, str):
            continue
        normalized = mime.lower()
        if normalized.startswith("video/"):
            return ClipClassifiedKind.VIDEO
        if normalized.startswith("image/"):
            return ClipClassifiedKind.IMAGE
        if normalized.startswith("audio/"):
            return ClipClassifiedKind.AUDIO
    return None


def _classify_clip(
    clip: Mapping[str, Any],
    registry: AssetRegistry | Mapping[str, Any] | None = None,
    *,
    theme: str | None = None,
) -> ClipClassifiedKind:
    clip_type = clip.get("clipType", "media")
    if clip_type == "text":
        return ClipClassifiedKind.TEXT
    if clip_type == "media":
        return _asset_kind_from_entry(_asset_entry_for_clip(clip, registry)) or ClipClassifiedKind.OPAQUE
    if clip_type in {"hold"}:
        return _asset_kind_from_entry(_asset_entry_for_clip(clip, registry)) or ClipClassifiedKind.VIDEO
    if clip_type == "effect-layer":
        return ClipClassifiedKind.EFFECT
    from astrid.core.timeline.validators._type_resolve import is_visual_clip_element
    if isinstance(clip_type, str) and is_visual_clip_element(clip_type, theme):
        return ClipClassifiedKind.EFFECT
    return ClipClassifiedKind.OPAQUE


@dataclass(frozen=True)
class TimelineClipView:
    data: Mapping[str, Any]
    registry: AssetRegistry | Mapping[str, Any] | None = None
    theme: str | None = None

    @property
    def classified_kind(self) -> ClipClassifiedKind:
        return _classify_clip(self.data, self.registry, theme=self.theme)


def _exclude_source_tracks(payload: dict[str, Any]) -> None:
    """Drop Source-role tracks and the clips on them from a render payload.

    A track with ``role: "source"`` is reference material (the App keeps it
    for the editor, never for export). ``role`` defaults to ``output``, so
    documents without it render unchanged. The stored document keeps the
    Source track; only the render payload excludes it.
    """
    tracks = payload.get("tracks")
    if not isinstance(tracks, list):
        return
    source_ids = {
        track.get("id")
        for track in tracks
        if isinstance(track, dict) and track.get("role") == "source"
    }
    if not source_ids:
        return
    payload["tracks"] = [
        track for track in tracks if not (isinstance(track, dict) and track.get("id") in source_ids)
    ]
    clips = payload.get("clips")
    if isinstance(clips, list):
        payload["clips"] = [
            clip for clip in clips if not (isinstance(clip, dict) and clip.get("track") in source_ids)
        ]


class TimelineRenderView:
    def __init__(self, timeline: "Timeline", *, default_theme: str) -> None:
        self._timeline = timeline
        self._default_theme = default_theme

    @property
    def theme(self) -> str:
        return self._timeline.theme or self._default_theme

    @property
    def clips(self) -> list[TimelineClip]:
        return self._timeline.clips

    @property
    def tracks(self) -> list[TrackDefinition]:
        return self._timeline.tracks

    def to_json_data(self) -> dict[str, Any]:
        payload = self._timeline.to_json_data()
        _exclude_source_tracks(payload)
        payload.setdefault("theme", self._default_theme)
        return payload


class Timeline:
    """Persisted timeline JSON with top-level passthrough fields preserved."""

    def __init__(self, config: Mapping[str, Any], *, validate: bool = True) -> None:
        data = copy.deepcopy(dict(config))
        if validate:
            validate_timeline(reader_fps_view(data))
        self._data = data

    @classmethod
    def from_json_data(cls, payload: Mapping[str, Any], *, validate: bool = True) -> "Timeline":
        data = copy.deepcopy(dict(payload))
        clips = data.get("clips")
        if isinstance(clips, list):
            data["clips"] = [
                _swap_from_load(dict(clip)) if isinstance(clip, dict) else clip
                for clip in clips
            ]
        return cls(data, validate=validate)

    @classmethod
    def load(cls, path: Path, *, validate: bool = True) -> "Timeline":
        data = json.loads(Path(path).read_text(encoding="utf-8"))
        if not isinstance(data, dict):
            raise ValueError("Timeline must be a JSON object")
        return cls.from_json_data(data, validate=validate)

    @classmethod
    def from_config(cls, config: Mapping[str, Any], *, validate: bool = True) -> "Timeline":
        return cls(config, validate=validate)

    @property
    def theme(self) -> str | None:
        theme = self._data.get("theme")
        return theme if isinstance(theme, str) else None

    @property
    def clips(self) -> list[TimelineClip]:
        # setdefault returns the dict's `Any` value type because TimelineConfig
        # is a `total=False` TypedDict; the cast names the validated shape.
        return cast(list[TimelineClip], self._data.setdefault("clips", []))

    @property
    def tracks(self) -> list[TrackDefinition]:
        return cast(list[TrackDefinition], self._data.setdefault("tracks", []))

    @property
    def theme_overrides(self) -> ThemeOverrides | None:
        value = self._data.get("theme_overrides")
        return cast(ThemeOverrides, value) if isinstance(value, dict) else None

    @property
    def generation_defaults(self) -> dict[str, Any] | None:
        value = self._data.get("generation_defaults")
        return value if isinstance(value, dict) else None

    @property
    def pinnedShotGroups(self) -> list[dict[str, Any]] | None:
        value = self._data.get("pinnedShotGroups")
        return value if isinstance(value, list) else None

    @property
    def output(self) -> TimelineOutput | None:
        value = self._data.get("output")
        return cast(TimelineOutput, value) if isinstance(value, dict) else None

    def classified_clips(
        self,
        registry: AssetRegistry | Mapping[str, Any] | None = None,
    ) -> list[TimelineClipView]:
        return [
            TimelineClipView(clip, registry=registry, theme=self.theme)
            for clip in self.clips
            if isinstance(clip, Mapping)
        ]

    def for_render(self, default_theme: str = "banodoco-default") -> TimelineRenderView:
        if not isinstance(default_theme, str) or not default_theme:
            raise ValueError("default_theme must be a non-empty slug")
        return TimelineRenderView(self, default_theme=default_theme)

    def to_config(self) -> TimelineConfig:
        return cast(TimelineConfig, copy.deepcopy(self._data))

    def to_json_data(self) -> dict[str, Any]:
        payload = copy.deepcopy(self._data)
        clips = payload.get("clips")
        if isinstance(clips, list):
            payload["clips"] = [
                _round_at_for_dump(_swap_from_dump(dict(clip))) if isinstance(clip, dict) else clip
                for clip in clips
            ]
        validate_timeline(reader_fps_view(payload))
        return payload

    def dump(self, path: Path) -> None:
        _write_json(path, self.to_json_data())

def merge_generation(
    theme_generation: dict[str, Any] | None,
    per_clip: dict[str, Any] | None,
) -> dict[str, Any]:
    """Merge per-clip generation atop the resolved theme.generation block.

    Per-clip keys win on conflict. Lists (references, assets) are replaced wholesale,
    not merged. Returns an empty dict if both inputs are empty/None.
    """
    merged: dict[str, Any] = {}
    if isinstance(theme_generation, dict):
        merged.update(theme_generation)
    if isinstance(per_clip, dict):
        merged.update(per_clip)
    return merged


def load_timeline(path: Path) -> TimelineConfig:
    return Timeline.load(path).to_config()

def save_timeline(config: TimelineConfig, path: Path) -> None:
    Timeline.from_config(config).dump(path)

def load_registry(path: Path) -> AssetRegistry:
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    validate_registry(data)
    return cast(AssetRegistry, data)

def save_registry(registry: AssetRegistry, path: Path) -> None:
    validate_registry(registry)
    _write_json(path, registry)

def load_metadata(path: Path) -> PipelineMetadata:
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    validate_metadata(data)
    return cast(PipelineMetadata, data)

def save_metadata(meta: PipelineMetadata, path: Path) -> None:
    validate_metadata(meta)
    _write_json(path, meta)

def load_pool(path: Path) -> Pool:
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    validate_pool(data)
    return cast(Pool, data)

def save_pool(pool: Pool, path: Path) -> None:
    validate_pool(pool)
    _write_json(path, pool)

def _assign_missing_arrangement_uuids(arrangement: Any) -> bool:
    if not isinstance(arrangement, dict):
        return False
    clips = arrangement.get("clips")
    if not isinstance(clips, list):
        return False
    used = {
        clip.get("uuid")
        for clip in clips
        if isinstance(clip, dict) and isinstance(clip.get("uuid"), str)
    }
    assigned = False
    for clip in clips:
        if not isinstance(clip, dict) or "uuid" in clip:
            continue
        value = uuid.uuid4().hex[:8]
        while value in used:
            value = uuid.uuid4().hex[:8]
        used.add(value)
        clip["uuid"] = value
        assigned = True
        order = clip.get("order")
        print(f"timeline.load_arrangement: migrated clip order={order} uuid={value}", file=sys.stderr)
    return assigned

def load_arrangement(
    path: Path,
    pool_ids: set[str] | None = None,
    *,
    assign_missing_uuids: bool = False,
) -> Arrangement:
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    migrated = False
    if assign_missing_uuids:
        migrated = _assign_missing_arrangement_uuids(data)
    validate_arrangement(data, pool_ids)
    if migrated:
        _write_json(Path(path), data)
    return cast(Arrangement, data)

def save_arrangement(arrangement: Arrangement, path: Path, pool_ids: set[str] | None = None) -> None:
    validate_arrangement(arrangement, pool_ids)
    _write_json(path, arrangement)
