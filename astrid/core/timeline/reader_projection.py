"""What a timeline reader (render, visualize) sees, and the check that it can open it.

Authoring accepts four clip media spellings (``asset``, ``asset_id``,
``media_id``, ``object_id``).  Readers accept one: ``clip.asset``, a key of the
asset registry.  ``translate_media_selectors`` is the single bridge between the
two; the shared parent-composition projection applies it, so render, visualize
and Runtime capture admission all receive the same ``asset``-only clips.

``check_reader_projection`` runs the readers' own strict schema checks on such
a projection.  The commit path uses it so that a commit is accepted only when
every reader can open the result.
"""

from __future__ import annotations

import copy
import hashlib
import math
from collections.abc import Mapping
from typing import Any

READER_PROJECTION_APP_KEY = "astrid_authoring_render_projection"
_HEX = frozenset("0123456789abcdef")
# Recorded in R13 SDK and authoring round trips. Similar-looking structural
# fields stay in place for the strict reader schema to check.
_LEGACY_EXTENSION_KEYS = {
    "config": frozenset({"opaque", "x_f2_opaque", "opaque_parent_config", "org.example/data"}),
    "clip": frozenset({"opaque", "org.example/data"}),
    "track": frozenset({"opaque"}),
    "theme_overrides": frozenset({"opaque_theme"}),
}


class MediaSelectorError(ValueError):
    """A clip media selector that no reader can resolve to one registry asset."""

    def __init__(self, message: str, *, path: str, reason: str, recovery: str) -> None:
        super().__init__(message)
        self.path = path
        self.reason = reason
        self.recovery = recovery


class ReaderProjectionError(ValueError):
    """The projection of a timeline is refused by the readers' strict checks."""

    code = "unreadable_timeline"

    def __init__(self, message: str, *, part: str, reason: str) -> None:
        super().__init__(message)
        self.part = part
        self.reason = reason


def media_identity(value: Any) -> str | None:
    """Return a canonical Runtime media identity, or ``None`` when absent."""
    if not isinstance(value, str) or not value.strip():
        return None
    stripped = value.removeprefix("sha256:")
    if len(stripped) == 64 and all(character in _HEX for character in stripped):
        return "sha256:" + stripped
    return value


def _entry_identity(entry: Any) -> str | None:
    if not isinstance(entry, Mapping):
        return None
    return media_identity(entry.get("object_id") or entry.get("media_id"))


def translate_media_selectors(
    config: Mapping[str, Any], registry: Mapping[str, Any], *, strict: bool = True,
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Return copies in which every clip selects its media through ``clip.asset``.

    ``media_id``/``object_id`` resolve to a registry asset with the same
    identity (preferring an alias in the clip's own occurrence namespace), or
    to a new ``authoring-media-*`` registry entry.  ``asset_id`` is the
    authoring spelling of a registry key and becomes ``asset``.

    A selector that cannot be resolved (an empty identity, or an ``asset`` that
    names different media) raises ``MediaSelectorError`` when ``strict``; when
    not strict the clip is left as written, so the readers' strict check
    reports it instead of a guess being made here.
    """
    normalized_config = copy.deepcopy(dict(config))
    normalized_registry = copy.deepcopy(dict(registry))
    raw_assets = normalized_registry.get("assets")
    if raw_assets is None:
        raw_assets = normalized_registry["assets"] = {}
    raw_clips = normalized_config.get("clips")
    if not isinstance(raw_assets, dict) or not isinstance(raw_clips, list):
        return normalized_config, normalized_registry

    assets_by_media: dict[str, list[str]] = {}
    for asset_key, raw_entry in raw_assets.items():
        identity = _entry_identity(raw_entry)
        if identity is not None:
            assets_by_media.setdefault(identity, []).append(str(asset_key))

    def refuse(message: str, *, path: str, reason: str, recovery: str) -> None:
        if strict:
            raise MediaSelectorError(message, path=path, reason=reason, recovery=recovery)

    for index, raw_clip in enumerate(raw_clips):
        if not isinstance(raw_clip, dict):
            continue
        alias = raw_clip.get("asset_id")
        if "asset_id" in raw_clip and isinstance(alias, str) and alias in raw_assets:
            selected = raw_clip.get("asset")
            if selected is None or selected == alias:
                raw_clip["asset"] = alias
                del raw_clip["asset_id"]
            else:
                refuse(
                    f"canonical timeline clip {index} has conflicting asset and asset_id",
                    path=f"$.clips[{index}]",
                    reason="asset and asset_id name different registry assets",
                    recovery="Keep one registry asset for the clip.",
                )
                continue
        direct_field = next((field for field in ("media_id", "object_id") if field in raw_clip), None)
        if direct_field is None:
            continue
        direct_value = raw_clip.get(direct_field)
        identity = media_identity(direct_value)
        if identity is None:
            refuse(
                f"canonical timeline clip {index} has an invalid {direct_field}",
                path=f"$.clips[{index}].{direct_field}",
                reason="direct media selector must be a non-empty Runtime media identity",
                recovery="Use the exact media id returned by the project Runtime.",
            )
            continue
        selected_asset = raw_clip.get("asset")
        if isinstance(selected_asset, str):
            if _entry_identity(raw_assets.get(selected_asset)) != identity:
                refuse(
                    f"canonical timeline clip {index} has conflicting asset and {direct_field}",
                    path=f"$.clips[{index}]",
                    reason="asset alias and direct media selector resolve to different identities",
                    recovery="Keep one exact Runtime media selection for the clip.",
                )
                continue
        else:
            matches = sorted(assets_by_media.get(identity, ()))
            if matches:
                # Parent and occurrence-local registries may both alias the same
                # digest. Prefer the alias in this clip's occurrence so its
                # type/origin provenance is kept.
                clip_id = raw_clip.get("id")
                namespace = (str(clip_id).rsplit(":", 1)[0] + ":"
                             if isinstance(clip_id, str) and ":" in clip_id else None)
                local = [key for key in matches if namespace is not None and key.startswith(namespace)]
                selected_asset = local[0] if local else matches[0]
            else:
                seed = hashlib.sha256(identity.encode("utf-8")).hexdigest()[:24]
                selected_asset = f"authoring-media-{seed}"
                suffix = 1
                while selected_asset in raw_assets:
                    suffix += 1
                    selected_asset = f"authoring-media-{seed}-{suffix}"
                entry: dict[str, Any] = {"media_id": str(direct_value)}
                if identity.startswith("sha256:"):
                    entry["content_sha256"] = identity.removeprefix("sha256:")
                raw_assets[selected_asset] = entry
                assets_by_media.setdefault(identity, []).append(selected_asset)
        raw_clip["asset"] = selected_asset
        raw_clip.pop("media_id", None)
        raw_clip.pop("object_id", None)
        app = raw_clip.get("app")
        if not isinstance(app, dict):
            app = raw_clip["app"] = {}
        annotation = app.get(READER_PROJECTION_APP_KEY)
        if not isinstance(annotation, dict):
            annotation = app[READER_PROJECTION_APP_KEY] = {}
        annotation["media_selector"] = {
            "field": direct_field, "media_id": str(direct_value), "asset": selected_asset,
        }
    return normalized_config, normalized_registry


def _normalize_legacy_transitions(config: dict[str, Any]) -> None:
    from .legacy_elements import normalize_legacy_transition_id

    clips = config.get("clips")
    if isinstance(clips, list):
        for clip in clips:
            if not isinstance(clip, dict):
                continue
            transition = clip.get("transition")
            if isinstance(transition, str):
                clip["transition"] = normalize_legacy_transition_id(transition)
            elif isinstance(transition, dict):
                # The validator selects id over type, so normalize that same field.
                field = "id" if "id" in transition else "type"
                value = transition.get(field)
                if isinstance(value, str):
                    transition[field] = normalize_legacy_transition_id(value)


def normalize_legacy_reader_fields(
    config: dict[str, Any], registry: dict[str, Any], *, preserve_transition_ids: bool = False,
) -> None:
    """Adapt detached reader copies and leave authored fields for exact comparison."""
    if not preserve_transition_ids:
        _normalize_legacy_transitions(config)

    tracks = config.get("tracks")
    if isinstance(tracks, list):
        for track in tracks:
            track_id = track.get("id") if isinstance(track, dict) else None
            if isinstance(track_id, str) and track_id:
                track.setdefault("label", track_id)

    if "meta" in registry:
        from .validators.meta import validate_meta

        validate_meta(registry["meta"], path="registry.meta")
        del registry["meta"]


def _project_extensions(
    owner: dict[str, Any], section: str, *, app_owner: dict[str, Any],
) -> None:
    extensions = {key: owner[key] for key in _LEGACY_EXTENSION_KEYS[section] if key in owner}
    if section == "clip" and owner.get("clipType") == "text":
        # R13 could retain these null placeholders on a text clip. A
        # non-null sourceURL would still be an unknown media selector.
        extensions.update({key: None for key in ("nullable", "sourceURL")
                           if key in owner and owner[key] is None})
    if not extensions:
        return

    from astrid.core.receipts.canonical import CanonicalizationError, canonical_json

    try:
        canonical_json(extensions)
    except CanonicalizationError as exc:
        reason = f"{section} extensions must contain only bounded finite JSON"
        raise ReaderProjectionError(reason, part="timeline", reason=reason) from exc

    app = app_owner.setdefault("app", {})
    annotation = app.setdefault(READER_PROJECTION_APP_KEY, {}) if isinstance(app, dict) else None
    slots = annotation.setdefault("extensions", {}) if isinstance(annotation, dict) else None
    if not isinstance(slots, dict) or section in slots:
        reason = f"{section} extensions collide with the reader projection annotation"
        raise ReaderProjectionError(reason, part="timeline", reason=reason)
    slots[section] = extensions
    for key in extensions:
        del owner[key]


def _project_rational_fps(container: Any, path: str) -> None:
    value = container.get("fps") if isinstance(container, dict) else None
    # The legacy canvas also tolerates opaque objects with no FPS components.
    components = {"numerator", "denominator"}
    if not isinstance(value, dict) or components.isdisjoint(value):
        return

    reason = f"{path}.fps must be a finite positive ratio of positive integers"
    numerator = value.get("numerator")
    denominator = value.get("denominator")
    if (value.keys() != components
            or type(numerator) is not int or numerator <= 0
            or type(denominator) is not int or denominator <= 0):
        raise ReaderProjectionError(reason, part="timeline", reason=reason)
    try:
        fps = numerator / denominator
    except OverflowError as exc:
        raise ReaderProjectionError(reason, part="timeline", reason=reason) from exc
    if not math.isfinite(fps) or fps <= 0:
        raise ReaderProjectionError(reason, part="timeline", reason=reason)
    container["fps"] = fps


def project_reader_fields(config: dict[str, Any]) -> None:
    """Adapt a reader-owned copy of the compiled timeline for the scalar schema."""
    _project_rational_fps(config.get("output"), "output")
    _project_extensions(config, "config", app_owner=config)
    for collection, section in (("tracks", "track"), ("clips", "clip")):
        rows = config.get(collection)
        if isinstance(rows, list):
            for row in rows:
                if isinstance(row, dict):
                    _project_extensions(row, section, app_owner=row)

    overrides = config.get("theme_overrides")
    if isinstance(overrides, dict):
        _project_extensions(overrides, "theme_overrides", app_owner=config)
        visual = overrides.get("visual")
        if isinstance(visual, dict):
            _project_rational_fps(visual.get("canvas"), "theme_overrides.visual.canvas")


def reader_view(config: Mapping[str, Any], registry: Mapping[str, Any]) -> tuple[dict[str, Any], dict[str, Any]]:
    """Return what render reads from a projection, without importing the renderer.

    Media selectors, legacy fields, editor registry metadata, and ``rect``
    placement are translated for the strict reader check.
    """
    from .banodoco_schema import EDITOR_ASSET_METADATA_KEYS

    view_config, view_registry = translate_media_selectors(config, registry, strict=False)
    project_reader_fields(view_config)
    _normalize_legacy_transitions(view_config)
    assets = view_registry.get("assets")
    if isinstance(assets, dict):
        for entry in assets.values():
            if isinstance(entry, dict):
                for key in EDITOR_ASSET_METADATA_KEYS & set(entry):
                    del entry[key]
                entry.pop("media_type", None)
                if entry.get("origin") == "managed-local":
                    entry["origin"] = "opaque-foreign"
    for clip in view_config.get("clips") or []:
        rect = clip.get("rect") if isinstance(clip, dict) else None
        if (isinstance(rect, Mapping) and set(rect) <= {"x", "y", "width", "height"}
                and all(clip.get(key, rect[key]) == rect[key] for key in rect)):
            del clip["rect"]
            clip.update(copy.deepcopy(dict(rect)))
    return view_config, view_registry


def check_reader_projection(config: Mapping[str, Any], registry: Mapping[str, Any]) -> None:
    """Raise ``ReaderProjectionError`` unless render and visualize can open this projection.

    These are the readers' own strict checks: the shared timeline schema,
    the asset registry schema, and that every ``clip.asset`` is registered.
    """
    from . import validate_registry, validate_timeline

    try:
        validate_timeline(copy.deepcopy(dict(config)))
    except Exception as exc:  # noqa: BLE001 - every schema failure is one refusal
        reason = str(exc).strip().splitlines()[0] if str(exc).strip() else type(exc).__name__
        raise ReaderProjectionError(
            f"render and visualize cannot open the result: {reason}",
            part="timeline", reason=reason,
        ) from exc
    try:
        validate_registry(copy.deepcopy(dict(registry)))
    except Exception as exc:  # noqa: BLE001
        reason = str(exc).strip().splitlines()[0] if str(exc).strip() else type(exc).__name__
        raise ReaderProjectionError(
            f"render and visualize cannot open the result's asset registry: {reason}",
            part="registry", reason=reason,
        ) from exc
    assets = registry.get("assets") if isinstance(registry, Mapping) else None
    registered = assets if isinstance(assets, Mapping) else {}
    for index, clip in enumerate(config.get("clips") or []):
        if isinstance(clip, Mapping) and isinstance(clip.get("asset"), str) and clip["asset"] not in registered:
            reason = f"clips[{index}] (clip {clip.get('id', '?')!r}) uses asset {clip['asset']!r}, which is not in the registry"
            raise ReaderProjectionError(
                f"render and visualize cannot open the result: {reason}",
                part="registry", reason=reason,
            )


__all__ = [
    "MediaSelectorError",
    "READER_PROJECTION_APP_KEY",
    "ReaderProjectionError",
    "check_reader_projection",
    "media_identity",
    "project_reader_fields",
    "reader_view",
    "translate_media_selectors",
]
