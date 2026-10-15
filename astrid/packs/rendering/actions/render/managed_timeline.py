"""Canonical kernel timeline resolution for the explicit managed render mode."""

from __future__ import annotations

import copy
import hashlib
import json
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any, Mapping

from astrid.core import timeline
from astrid.core.rendering.contracts import CompiledAudio
from astrid.core._shared.jsonio import write_json_atomic
from astrid.core.foundation.hash import validate_digest
from astrid.core.timeline.duration import render_clock
from astrid.sdk.pagination import paged_rows


class ManagedRenderValidationError(ValueError):
    """Actionable, JSON-safe pre-admission failure for a managed render."""

    def __init__(
        self,
        message: str,
        *,
        path: str,
        reason: str,
        recovery: str,
        validator: str | None = None,
        schema_path: str | None = None,
    ) -> None:
        super().__init__(message)
        details: dict[str, Any] = {
            "path": path,
            "reason": reason,
            "recovery": recovery,
        }
        if validator:
            details["validator"] = validator
        if schema_path:
            details["schema_path"] = schema_path
        self.details = details


def _render_compatible_projection(
    config: Mapping[str, Any], registry: Mapping[str, Any]
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Adapt public authoring-helper output to the existing render schema.

    Detached candidates accept exact Runtime media identities and convenient
    clip-local placement fields.  The renderer consumes the older shared
    timeline schema, where media is selected through ``clip.asset``, fit is a
    track property, and rectangles are ordinary clip geometry.  Project those
    equivalent spellings here, after the immutable composition is resolved but
    before managed-media admission and schema validation.  Publication remains
    byte-for-byte the candidate compiled by the authoring bundle.
    """

    normalized_config = copy.deepcopy(dict(config))
    normalized_registry = copy.deepcopy(dict(registry))
    raw_assets = normalized_registry.setdefault("assets", {})
    if not isinstance(raw_assets, dict):
        return normalized_config, normalized_registry

    def media_identity(value: Any) -> str | None:
        if not isinstance(value, str) or not value.strip():
            return None
        stripped = value.removeprefix("sha256:")
        if len(stripped) == 64 and all(character in "0123456789abcdef" for character in stripped):
            return "sha256:" + stripped
        return value

    assets_by_media: dict[str, list[str]] = {}
    for asset_key, raw_entry in raw_assets.items():
        if not isinstance(raw_entry, dict):
            continue
        # ``managed-local`` is the authoring/import description used by the
        # evaluation helpers.  Once Runtime has admitted the exact media id and
        # digest, it is an opaque imported source for renderer purposes.
        if raw_entry.get("origin") == "managed-local":
            raw_entry["origin"] = "opaque-foreign"
        identity = media_identity(raw_entry.get("object_id") or raw_entry.get("media_id"))
        if identity is not None:
            assets_by_media.setdefault(identity, []).append(str(asset_key))

    raw_tracks = normalized_config.get("tracks", [])
    tracks_by_id = {
        str(track.get("id")): track
        for track in raw_tracks
        if isinstance(track, dict) and isinstance(track.get("id"), str)
    } if isinstance(raw_tracks, list) else {}
    raw_clips = normalized_config.get("clips", [])
    if not isinstance(raw_clips, list):
        return normalized_config, normalized_registry

    for index, raw_clip in enumerate(raw_clips):
        if not isinstance(raw_clip, dict):
            continue
        compatibility: dict[str, Any] = {}
        direct_field = next(
            (field for field in ("media_id", "object_id") if field in raw_clip),
            None,
        )
        if direct_field is not None:
            direct_value = raw_clip.get(direct_field)
            identity = media_identity(direct_value)
            if identity is None:
                raise ManagedRenderValidationError(
                    f"canonical timeline clip {index} has an invalid {direct_field}",
                    path=f"$.clips[{index}].{direct_field}",
                    reason="direct media selector must be a non-empty Runtime media identity",
                    recovery="Use the exact media id returned by the project Runtime.",
                    validator="authoring_render_projection",
                )
            matches = sorted(assets_by_media.get(identity, ()))
            selected_asset = raw_clip.get("asset")
            if isinstance(selected_asset, str):
                entry = raw_assets.get(selected_asset)
                selected_identity = media_identity(
                    entry.get("object_id") or entry.get("media_id")
                ) if isinstance(entry, Mapping) else None
                if selected_identity != identity:
                    raise ManagedRenderValidationError(
                        f"canonical timeline clip {index} has conflicting asset and {direct_field}",
                        path=f"$.clips[{index}]",
                        reason="asset alias and direct media selector resolve to different identities",
                        recovery="Keep one exact Runtime media selection for the clip.",
                        validator="authoring_render_projection",
                    )
            elif matches:
                # Parent and occurrence-local registries may legitimately
                # contain aliases for the same digest. Prefer the alias in
                # this projected occurrence so its type/origin provenance is
                # retained instead of binding the clip to an unrelated global
                # parent alias merely because it sorts first.
                clip_id = raw_clip.get("id")
                namespace = (
                    str(clip_id).rsplit(":", 1)[0] + ":"
                    if isinstance(clip_id, str) and ":" in clip_id
                    else None
                )
                local_matches = (
                    [key for key in matches if key.startswith(namespace)]
                    if namespace is not None else []
                )
                selected_asset = local_matches[0] if local_matches else matches[0]
            else:
                digest = identity.removeprefix("sha256:")
                key_seed = hashlib.sha256(identity.encode("utf-8")).hexdigest()[:24]
                selected_asset = f"authoring-media-{key_seed}"
                suffix = 1
                while selected_asset in raw_assets:
                    suffix += 1
                    selected_asset = f"authoring-media-{key_seed}-{suffix}"
                entry = {"media_id": str(direct_value)}
                if identity.startswith("sha256:"):
                    entry["content_sha256"] = digest
                raw_assets[selected_asset] = entry
                assets_by_media.setdefault(identity, []).append(selected_asset)
            raw_clip["asset"] = selected_asset
            raw_clip.pop("media_id", None)
            raw_clip.pop("object_id", None)
            compatibility["media_selector"] = {
                "field": direct_field,
                "media_id": str(direct_value),
                "asset": selected_asset,
            }

        if "fit" in raw_clip:
            fit = raw_clip.pop("fit")
            track_id = raw_clip.get("track")
            track = tracks_by_id.get(str(track_id))
            if track is None:
                raise ManagedRenderValidationError(
                    f"canonical timeline clip {index} cannot project fit without its track",
                    path=f"$.clips[{index}].fit",
                    reason="clip-local fit references a missing track",
                    recovery="Attach the clip to an existing visual track, then retry.",
                    validator="authoring_render_projection",
                )
            existing_fit = track.get("fit")
            if existing_fit is not None and existing_fit != fit:
                raise ManagedRenderValidationError(
                    f"canonical timeline clip {index} conflicts with track fit {existing_fit!r}",
                    path=f"$.clips[{index}].fit",
                    reason="the existing renderer supports one fit policy per track",
                    recovery="Use a separate visual track for clips with a different fit policy.",
                    validator="authoring_render_projection",
                )
            track["fit"] = fit
            compatibility["fit"] = {"value": fit, "projected_to_track": str(track_id)}

        rect = raw_clip.pop("rect", None)
        if rect is not None:
            if not isinstance(rect, Mapping):
                raise ManagedRenderValidationError(
                    f"canonical timeline clip {index} has an invalid rect",
                    path=f"$.clips[{index}].rect",
                    reason="rect must be an object",
                    recovery="Provide x, y, width, and height geometry.",
                    validator="authoring_render_projection",
                )
            unknown = sorted(set(rect) - {"x", "y", "width", "height"})
            if unknown:
                raise ManagedRenderValidationError(
                    f"canonical timeline clip {index} rect has unsupported fields: {unknown!r}",
                    path=f"$.clips[{index}].rect",
                    reason="the existing renderer supports x, y, width, and height geometry",
                    recovery="Express the rectangle with x, y, width, and height only.",
                    validator="authoring_render_projection",
                )
            for field in ("x", "y", "width", "height"):
                if field in rect:
                    if field in raw_clip and raw_clip[field] != rect[field]:
                        raise ManagedRenderValidationError(
                            f"canonical timeline clip {index} has conflicting {field} geometry",
                            path=f"$.clips[{index}].rect.{field}",
                            reason="rect and canonical clip geometry disagree",
                            recovery="Keep one value for each geometry field.",
                            validator="authoring_render_projection",
                        )
                    raw_clip[field] = copy.deepcopy(rect[field])
            compatibility["rect"] = copy.deepcopy(dict(rect))

        if compatibility:
            app = raw_clip.setdefault("app", {})
            if not isinstance(app, dict):
                app = {}
                raw_clip["app"] = app
            app["astrid_authoring_render_projection"] = compatibility

    return normalized_config, normalized_registry


def _json_path(parts: Any) -> str:
    path = "$"
    for part in parts:
        if isinstance(part, int):
            path += f"[{part}]"
        elif isinstance(part, str) and part.isidentifier():
            path += f".{part}"
        else:
            path += f"[{json.dumps(str(part), ensure_ascii=True)}]"
    return path


def _schema_validation_error(
    snapshot: "ManagedRenderSnapshot", exc: Exception
) -> ManagedRenderValidationError:
    absolute_path = getattr(exc, "absolute_path", ())
    absolute_schema_path = getattr(exc, "absolute_schema_path", ())
    path = _json_path(absolute_path)
    reason = str(getattr(exc, "message", None) or exc).strip().splitlines()[0]
    validator = getattr(exc, "validator", None)
    recovery = f"Fix {path} to match the canonical timeline schema, then retry."
    if ".effects" in path or "'effects'" in reason:
        recovery = (
            "Use clip.effects only for fade timing (fade_in/fade_out, or a numeric "
            "fade map). Reference a reusable visual element with clipType:<effect-id> "
            "and params:{...}, then retry."
        )
    message = (
        f"canonical timeline {snapshot.timeline_slug!r} is not renderable at {path}: "
        f"{reason}. Recovery: {recovery}"
    )
    return ManagedRenderValidationError(
        message,
        path=path,
        reason=reason,
        recovery=recovery,
        validator=str(validator) if validator is not None else None,
        schema_path=_json_path(absolute_schema_path) if absolute_schema_path else None,
    )


def _validate_render_element_clip_types(
    snapshot: "ManagedRenderSnapshot", config: Mapping[str, Any]
) -> None:
    """Fail closed for clip types that the managed renderer would drop/fail on.

    The authoring schema deliberately keeps ``clipType`` open for compatibility.
    Canonical render admission is stricter: every non-built-in spelling is a
    reusable visual-element reference and must resolve in the active catalog.
    Opaque data inside ``params``/``generation`` is intentionally not scanned.
    """

    from astrid.core.element import catalog as element_catalog

    effect_ids = set(element_catalog.list_effect_ids())
    aliases = {"text"} if "text-card" in effect_ids else set()
    for effect_id in effect_ids:
        metadata = element_catalog.read_effect_meta(effect_id)
        raw_aliases = metadata.get("clipTypeAliases")
        if isinstance(raw_aliases, list):
            aliases.update(alias for alias in raw_aliases if isinstance(alias, str) and alias)
    builtins = {"media", "video", "image", "audio", "effect-layer"}
    known = builtins | effect_ids | aliases
    for index, clip in enumerate(config.get("clips", [])):
        if not isinstance(clip, Mapping):
            continue
        clip_type = clip.get("clipType", "media")
        if clip_type == "com.reigh.astrid.liveScene":
            from astrid.packs.rendering.shared.live_scenes.package import validate_package

            path = f"$.clips[{index}].app.liveScene"
            try:
                app = clip.get("app")
                validate_package(app.get("liveScene") if isinstance(app, Mapping) else None)
            except (ValueError, TypeError) as exc:
                raise ManagedRenderValidationError(
                    f"canonical timeline {snapshot.timeline_slug!r} is not renderable at {path}: {exc}",
                    path=path,
                    reason=str(exc),
                    recovery="Repair the prepared scene package integrity, then retry.",
                    validator="prepared_live_scene_package",
                ) from exc
            continue
        if not isinstance(clip_type, str) or clip_type in known:
            continue
        path = f"$.clips[{index}].clipType"
        available = ", ".join(sorted(effect_ids)) or "none installed"
        reason = f"unregistered reusable visual element id {clip_type!r}"
        recovery = (
            "Use a built-in clipType (media, video, image, audio, text, or "
            f"effect-layer) or an installed effect id. Available effect ids: {available}."
        )
        raise ManagedRenderValidationError(
            f"canonical timeline {snapshot.timeline_slug!r} is not renderable at "
            f"{path}: {reason}. Recovery: {recovery}",
            path=path,
            reason=reason,
            recovery=recovery,
            validator="registered_element_reference",
        )


def _normalize_pinned_element_references(config: Mapping[str, Any]) -> dict[str, Any]:
    """Project editor element refs onto the fields the Remotion backend reads.

    ``elementRef`` is the stable authoring identity. The existing generated
    Remotion composition still consumes ``clipType`` for effect clips and the
    canonical ``entrance``/``exit``/``continuous`` phase fields for animation
    references, so the managed render snapshot carries both representations
    without asking callers to know backend details.
    """

    normalized = dict(config)
    raw_clips = config.get("clips")
    if not isinstance(raw_clips, list):
        return normalized
    clips: list[Any] = []
    changed = False
    for raw_clip in raw_clips:
        if not isinstance(raw_clip, Mapping):
            clips.append(raw_clip)
            continue
        clip = dict(raw_clip)
        ref = clip.get("elementRef")
        if isinstance(ref, Mapping):
            element_id = ref.get("id")
            element_kind = ref.get("kind")
            if isinstance(element_id, str) and element_id and element_kind == "effect":
                if clip.get("clipType") == "effect-layer" or (
                    "packId" in ref and clip.get("clipType") != element_id
                ):
                    clip["clipType"] = element_id
                    changed = True
        clips.append(clip)
    if changed:
        normalized["clips"] = clips
    return normalized


def _validate_pinned_element_references(
    snapshot: "ManagedRenderSnapshot", config: Mapping[str, Any]
) -> None:
    """Check revision-pinned editor refs against Astrid's registry."""

    from astrid.core.element import catalog as element_catalog

    descriptors = {
        (str(descriptor.get("kind")), str(descriptor.get("id"))): descriptor
        for descriptor in element_catalog.list_element_descriptors()
    }
    owned_descriptors = {
        (descriptor["kind"], descriptor["id"], descriptor["packId"]): descriptor
        for descriptor in element_catalog.list_element_descriptors(include_shadowed=True)
    }
    for index, clip in enumerate(config.get("clips", [])):
        if not isinstance(clip, Mapping):
            continue
        ref = clip.get("elementRef")
        if not isinstance(ref, Mapping):
            continue
        ref_id = ref.get("id")
        ref_kind = ref.get("kind")
        ref_revision = ref.get("revision")
        ref_pack = ref.get("packId")
        path = f"$.clips[{index}].elementRef"
        if not all(isinstance(value, str) and value for value in (ref_id, ref_kind, ref_revision)) or (
            "packId" in ref and (not isinstance(ref_pack, str) or not ref_pack)
        ):
            raise ManagedRenderValidationError(
                f"canonical timeline {snapshot.timeline_slug!r} is not renderable at {path}: "
                "elementRef must contain id, kind, and revision",
                path=path,
                reason="malformed pinned element reference",
                recovery="Re-apply the element from the current Astrid catalog, then retry.",
                validator="pinned_element_reference",
            )
        if ref_revision.startswith("draft-"):
            raise ManagedRenderValidationError(
                f"canonical timeline {snapshot.timeline_slug!r} is not renderable at {path}: "
                f"draft element {ref_id!r} is preview-only",
                path=path,
                reason="draft element reference",
                recovery="Publish the element before exporting the timeline.",
                validator="pinned_element_reference",
            )
        descriptor = (
            owned_descriptors.get((ref_kind, ref_id, ref_pack))
            if "packId" in ref else descriptors.get((ref_kind, ref_id))
        )
        if descriptor is None:
            raise ManagedRenderValidationError(
                f"canonical timeline {snapshot.timeline_slug!r} is not renderable at {path}: "
                f"element {ref_kind}/{ref_id!r} is not registered",
                path=path,
                reason="unregistered pinned element",
                recovery="Re-apply the element from the current Astrid catalog, then retry.",
                validator="pinned_element_reference",
            )
        if descriptor.get("revision") != ref_revision:
            raise ManagedRenderValidationError(
                f"canonical timeline {snapshot.timeline_slug!r} is not renderable at {path}: "
                f"element {ref_id!r} is pinned to stale revision {ref_revision!r}",
                path=path,
                reason="stale pinned element revision",
                recovery="Refresh the catalog and re-apply the current element revision, then retry.",
                validator="pinned_element_reference",
            )


def _timeline_fps(config: Mapping[str, Any]) -> float:
    output = config.get("output")
    if isinstance(output, Mapping) and isinstance(output.get("fps"), (int, float)):
        return float(output["fps"])
    overrides = config.get("theme_overrides")
    if isinstance(overrides, Mapping):
        visual = overrides.get("visual")
        if isinstance(visual, Mapping):
            canvas = visual.get("canvas")
            if isinstance(canvas, Mapping) and isinstance(canvas.get("fps"), (int, float)):
                return float(canvas["fps"])
    return 30.0


def _validate_render_clock_binding(
    config: Mapping[str, Any], registry: Mapping[str, Any]
) -> None:
    """Validate the managed render-only tail against the canonical registry."""

    clock = render_clock(config, _timeline_fps(config))
    if clock is None:
        return
    tail = clock["tail"]
    assets = registry.get("assets", {})
    if not isinstance(assets, Mapping):
        raise ManagedRenderValidationError(
            "render clock tail requires a canonical asset registry",
            path="$.app.astrid_render_clock.tail.source_asset",
            reason="tail source binding cannot be resolved",
            recovery="Register the frozen tail source as a runtime-managed asset, then retry.",
            validator="managed_render_clock_binding",
        )
    asset_key = tail["source_asset"]
    entry = assets.get(asset_key)
    if not isinstance(entry, Mapping):
        raise ManagedRenderValidationError(
            f"render clock tail source asset {asset_key!r} is not in the canonical registry",
            path="$.app.astrid_render_clock.tail.source_asset",
            reason="tail source binding is not a registered asset",
            recovery="Register the frozen tail source in the same managed timeline snapshot, then retry.",
            validator="managed_render_clock_binding",
        )
    if not isinstance(entry.get("media_id"), str) or not entry["media_id"].strip():
        raise ManagedRenderValidationError(
            f"render clock tail source asset {asset_key!r} has no runtime media identity",
            path=f"$.assets[{json.dumps(str(asset_key))}].media_id",
            reason="tail source is not runtime-admitted",
            recovery="Import the frozen tail source into the project and refresh the canonical registry.",
            validator="managed_render_clock_binding",
        )
    if not isinstance(entry.get("content_sha256"), str) or not entry["content_sha256"].strip():
        raise ManagedRenderValidationError(
            f"render clock tail source asset {asset_key!r} has no content digest",
            path=f"$.assets[{json.dumps(str(asset_key))}].content_sha256",
            reason="tail source provenance is incomplete",
            recovery="Refresh the canonical registry with the runtime-admitted content digest, then retry.",
            validator="managed_render_clock_binding",
        )


def _canonical_bytes(value: Any) -> bytes:
    return json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    ).encode("utf-8")


def _digest(value: Any) -> str:
    return hashlib.sha256(_canonical_bytes(value)).hexdigest()


def _runtime_media_admissions(client: Any, project_ref: str) -> dict[str, str]:
    """Read the project-scoped runtime media identity map.

    A timeline's ``media_id`` and digest are authored input, not proof of
    ownership.  Only the generated runtime client's project-scoped media read
    can authorize turning that identity into an attempt-local materialization.
    Keep the
    result deliberately small so it can be used as an admission snapshot and
    never requires a child renderer to reopen runtime storage.
    """

    try:
        rows = paged_rows(client.media.list, project_ref)
    except Exception:  # noqa: BLE001 - an unavailable authority fails closed
        return {}
    if rows is None:
        return {}
    admitted: dict[str, str] = {}
    for row in rows:
        if not isinstance(row, Mapping):
            continue
        scope = row.get("project_ref") or row.get("project_slug")
        if scope is not None and str(scope) != project_ref:
            continue
        media_id = row.get("media_id") or row.get("id") or row.get("object_id")
        if not isinstance(media_id, str) or not media_id.strip():
            continue
        raw_digest = (
            row.get("content_hash")
            or row.get("content_sha256")
            or row.get("sha256")
            or row.get("digest")
            or row.get("object_id")
        )
        if not isinstance(raw_digest, str):
            continue
        try:
            digest = validate_digest(raw_digest.removeprefix("sha256:"))
        except (TypeError, ValueError):
            continue
        previous = admitted.get(media_id)
        if previous is not None and previous != digest:
            # A runtime response that is internally contradictory cannot
            # authorize either value.
            admitted.pop(media_id, None)
            continue
        admitted[media_id] = digest
    return admitted


def _runtime_snapshot_registry(
    registry: Mapping[str, Any], *, project_ref: str, client: Any
) -> dict[str, Any]:
    """Validate runtime-admitted media identities without replacing locators.

    The runtime snapshot is the authority and carries the stable ``media_id``
    and content digest. The generic host materializes bytes into the fenced
    attempt; this function never derives or writes a filesystem locator.
    """

    rebased = json.loads(json.dumps(dict(registry), ensure_ascii=False))
    raw_assets = rebased.get("assets", rebased)
    if not isinstance(raw_assets, dict):
        return rebased
    admitted = _runtime_media_admissions(client, project_ref)
    seen_media: dict[str, str] = {}
    # Detect contradictory authored aliases before checking runtime presence so
    # malformed canonical documents are reported deterministically even when
    # the runtime has no matching object.
    for entry in raw_assets.values():
        if not isinstance(entry, dict):
            continue
        media_id = entry.get("object_id") or entry.get("media_id")
        digest_value = (
            entry.get("content_sha256") or entry.get("sha256") or entry.get("hash")
        )
        if not isinstance(media_id, str) or not media_id.strip() or digest_value in (None, ""):
            continue
        try:
            digest = validate_digest(str(digest_value).removeprefix("sha256:"))
        except (TypeError, ValueError):
            continue
        previous = seen_media.get(media_id)
        if previous is not None and previous != digest:
            raise ManagedRenderValidationError(
                f"canonical registry contains conflicting entries for media_id {media_id!r}",
                path="$.assets",
                reason="one media_id claims multiple content digests",
                recovery="Keep one runtime-admitted digest for each media_id and retry.",
                validator="managed_media_identity",
            )
        seen_media[media_id] = digest
    seen_media.clear()
    for key, entry in raw_assets.items():
        if not isinstance(entry, dict):
            continue
        forbidden = [field for field in ("url", "file", "path", "source_path", "locator", "realm") if field in entry]
        if forbidden:
            raise ManagedRenderValidationError(
                f"canonical registry asset {key!r} contains retired media locator field(s): {', '.join(forbidden)}",
                path=f"$.assets[{json.dumps(str(key))}]",
                reason="live rendering accepts runtime-managed object ids and digests only",
                recovery="Import the bytes into the runtime and reference its object_id/digest.",
                validator="managed_media_locator",
            )
        media_id = entry.get("object_id") or entry.get("media_id")
        digest_value = (
            entry.get("content_sha256") or entry.get("sha256") or entry.get("hash")
        )
        if not isinstance(media_id, str) or not media_id.strip():
            raise ManagedRenderValidationError(
                f"canonical registry asset {key!r} is missing object_id",
                path=f"$.assets[{json.dumps(str(key))}].object_id",
                reason="path and URL media references are retired",
                recovery="Import the bytes into the runtime and reference its object_id.",
                validator="managed_media_identity",
            )
        admitted_digest = admitted.get(media_id)
        if admitted_digest is None:
            raise ManagedRenderValidationError(
                f"canonical registry asset {key!r} references media_id {media_id!r} "
                "that is not admitted by the selected project runtime",
                path=f"$.assets[{json.dumps(str(key))}].media_id",
                reason="media identity is missing from the project-scoped runtime read",
                recovery="Import the media into this project, refresh the timeline, and retry.",
                validator="managed_media_runtime_admission",
            )
        if digest_value in (None, ""):
            digest = admitted_digest
        else:
            try:
                digest = validate_digest(str(digest_value).removeprefix("sha256:"))
            except (TypeError, ValueError) as exc:
                raise ManagedRenderValidationError(
                    f"canonical registry asset {key!r} has an invalid content_sha256; "
                    "runtime media snapshots require a lowercase 64-hex digest",
                    path=f"$.assets[{json.dumps(str(key))}].content_sha256",
                    reason="invalid managed media digest",
                    recovery="Refresh the timeline from the runtime media snapshot and retry.",
                    validator="managed_media_digest",
                ) from exc
            if digest != admitted_digest:
                raise ManagedRenderValidationError(
                    f"canonical registry asset {key!r} claims digest {digest!r} for "
                    f"runtime media_id {media_id!r}, but the runtime admitted {admitted_digest!r}",
                    path=f"$.assets[{json.dumps(str(key))}].content_sha256",
                    reason="authored media identity does not match project runtime admission",
                    recovery="Refresh the timeline from the runtime media snapshot and retry.",
                    validator="managed_media_runtime_admission",
                )
        previous_digest = seen_media.get(media_id)
        if previous_digest is not None and previous_digest != digest:
            raise ManagedRenderValidationError(
                f"canonical registry contains conflicting entries for media_id {media_id!r}",
                path="$.assets",
                reason="one media_id claims multiple content digests",
                recovery="Keep one runtime-admitted digest for each media_id and retry.",
                validator="managed_media_identity",
            )
        seen_media[media_id] = digest
        # Keep the runtime identity in the child snapshot. The generic host,
        # not the timeline resolver, materializes bytes into its attempt.
        entry["media_id"] = media_id
        entry.pop("object_id", None)
        entry["content_sha256"] = digest
    return rebased


@dataclass(frozen=True, slots=True)
class ManagedRenderSnapshot:
    project_id: str
    project_slug: str
    timeline_id: str
    timeline_ulid: str
    timeline_slug: str
    config_version: int
    head_event_id: str
    head_hash: str
    config: Mapping[str, Any]
    registry: Mapping[str, Any]
    config_hash: str
    registry_hash: str
    materialized_registry_hash: str
    expansion: Mapping[str, Any] | None = None
    composition_graph: Mapping[str, Any] | None = None
    compiled_audio: CompiledAudio | None = None
    authoring_preview: Mapping[str, Any] | None = None

    def compile_audio(self) -> "ManagedRenderSnapshot":
        """Finalize derived audio after all occurrence/timing projection, before admission."""
        from astrid.core.rendering.compiled_audio import compile_snapshot_audio
        return replace(self, compiled_audio=compile_snapshot_audio(self.config, self.authority()))

    def authority(self) -> dict[str, Any]:
        result = {
            "authority": "kernel",
            "project_id": self.project_id,
            "project_slug": self.project_slug,
            "timeline_id": self.timeline_id,
            "timeline_ulid": self.timeline_ulid,
            "timeline_slug": self.timeline_slug,
            "config_version": self.config_version,
            "head_event_id": self.head_event_id,
            "head_hash": self.head_hash,
            "config_hash": self.config_hash,
            "registry_hash": self.registry_hash,
            "materialized_registry_hash": self.materialized_registry_hash,
        }
        if self.compiled_audio is not None:
            from astrid.core.rendering.compiled_audio import digest
            result["compiled_audio_digest"] = digest(self.compiled_audio)
        if self.authoring_preview is None:
            # Canonical snapshots resolve head_event_id from the verified exact
            # parent revision. Keep that pin explicit in the admitted envelope.
            result["parent_revision_id"] = self.head_event_id
        if self.expansion is not None:
            result["expansion"] = dict(self.expansion)
        if self.authoring_preview is not None:
            result["render_mode"] = "authoring_candidate_preview"
            result["authoring_preview"] = dict(self.authoring_preview)
        clock = render_clock(self.config, _timeline_fps(self.config))
        if clock is not None:
            result["render_clock"] = dict(clock)
        admissions: dict[str, str] = {}
        assets = self.registry.get("assets", {})
        if isinstance(assets, Mapping):
            for entry in assets.values():
                if not isinstance(entry, Mapping):
                    continue
                media_id = entry.get("object_id") or entry.get("media_id")
                digest = entry.get("content_sha256")
                if (
                    isinstance(media_id, str)
                    and media_id.strip()
                    and isinstance(digest, str)
                ):
                    admissions[media_id] = digest
        if admissions:
            # This is the immutable parent-to-child handoff.  A child may use
            # it only as a runtime-admitted allowlist; registry media_id and
            # digest fields alone are never ownership proof.
            result["managed_media_admissions"] = admissions
        return result


def validate_managed_render_snapshot(snapshot: ManagedRenderSnapshot) -> None:
    """Validate deterministic render requirements before kernel admission.

    Canonical timeline documents intentionally remain permissive authoring
    drafts.  Rendering is a stricter transition: once a managed ref is pinned,
    reject a malformed timeline/registry before materializing execution inputs
    or creating a run.  Backend runtime readiness remains the renderer's job.
    """

    config = _normalize_pinned_element_references(snapshot.config)
    output = config.get("output")
    if isinstance(output, Mapping):
        required_output_fields = ("resolution", "fps", "file")
        missing = [field for field in required_output_fields if field not in output]
        if missing:
            missing_text = ", ".join(missing)
            raise ValueError(
                f"canonical timeline {snapshot.timeline_slug!r} is not renderable: "
                f"config.output is incomplete; missing required field(s): {missing_text}. "
                "Either omit config.output or provide resolution, fps, and file, then retry"
            )
    try:
        timeline.validate_timeline(config)
    except Exception as exc:
        raise _schema_validation_error(snapshot, exc) from exc
    _validate_pinned_element_references(snapshot, config)
    _validate_render_element_clip_types(snapshot, config)

    registry = dict(snapshot.registry)
    try:
        timeline.validate_registry(registry)
    except Exception as exc:
        raise ValueError(
            f"canonical timeline {snapshot.timeline_slug!r} asset registry is not renderable: {exc}"
        ) from exc

    assets = registry.get("assets")
    registered = assets if isinstance(assets, Mapping) else {}
    missing_assets = sorted(
        {
            str(clip.get("asset"))
            for clip in config.get("clips", [])
            if isinstance(clip, Mapping)
            and isinstance(clip.get("asset"), str)
            and clip.get("asset") not in registered
        }
    )
    if missing_assets:
        raise ValueError(
            f"canonical timeline {snapshot.timeline_slug!r} is not renderable: "
            "clips reference missing registry asset id(s): " + ", ".join(missing_assets)
        )
    try:
        _validate_render_clock_binding(config, registry)
    except ValueError as exc:
        if isinstance(exc, ManagedRenderValidationError):
            raise
        raise ManagedRenderValidationError(
            f"canonical timeline {snapshot.timeline_slug!r} has an invalid render clock: {exc}",
            path="$.app.astrid_render_clock",
            reason=str(exc),
            recovery="Provide a valid authored/render clock and runtime-admitted tail source, then retry.",
            validator="managed_render_clock",
        ) from exc


def _exact_revision_reader(client: Any) -> Any:
    """Return the generated Runtime reader behind an Astrid product client."""

    candidates = [client]
    timelines = getattr(client, "timelines", None)
    candidates.append(getattr(timelines, "_client", None))
    remote = getattr(client, "_remote", None)
    candidates.append(getattr(remote, "_transport", None))
    required = (
        "get_project_parent_composition_revision",
        "get_project_shot_revision",
        "get_project_timeline_revision",
    )
    for candidate in candidates:
        if candidate is not None and all(callable(getattr(candidate, name, None)) for name in required):
            return candidate
    raise ValueError(
        "canonical timeline has an immutable parent head, but the bound Runtime client "
        "cannot read exact parent/shot/internal revisions"
    )


def _exact_mapping(value: Any, *, label: str) -> Mapping[str, Any]:
    """Unwrap either a generated read or a product-domain read result."""

    if hasattr(value, "ok"):
        if not value.ok or not isinstance(value.data, Mapping):
            raise ValueError(f"Runtime could not resolve exact {label}")
        value = value.data
    if not isinstance(value, Mapping):
        raise ValueError(f"Runtime exact {label} response is not an object")
    return value


def _read_exact_parent_head(
    *,
    client: Any,
    project_id: str,
    timeline_id: str,
    parent_revision_id: str,
) -> Mapping[str, Any]:
    """Read one immutable parent head without resolving its child closure."""

    reader = _exact_revision_reader(client)
    parent = _exact_mapping(
        reader.get_project_parent_composition_revision(
            project_id, timeline_id, parent_revision_id
        ),
        label="parent composition revision",
    )
    if parent.get("revision_id") != parent_revision_id:
        raise ValueError("Runtime returned a different parent composition revision")
    if str(parent.get("project_id")) != project_id:
        raise ValueError("Runtime returned a parent composition for a different project")
    if str(parent.get("timeline_id")) != timeline_id:
        raise ValueError("Runtime returned a parent composition for a different timeline")
    content_digest = parent.get("content_digest")
    if not isinstance(content_digest, str) or not content_digest.startswith("sha256:"):
        raise ValueError("canonical parent head has no content digest")
    return parent


def _project_exact_parent_head(
    *,
    client: Any,
    project_id: str,
    timeline_id: str,
    parent_revision_id: str,
) -> tuple[Mapping[str, Any], Any, dict[str, Any]]:
    """Read and project the closure pinned by one immutable parent head."""

    parent = _read_exact_parent_head(
        client=client,
        project_id=project_id,
        timeline_id=timeline_id,
        parent_revision_id=parent_revision_id,
    )
    reader = _exact_revision_reader(client)
    payload = parent.get("payload")
    occurrences = payload.get("occurrences") if isinstance(payload, Mapping) else None
    if not isinstance(occurrences, list):
        raise ValueError("exact parent composition occurrences are not a list")

    shots: list[Mapping[str, Any]] = []
    internals: list[Mapping[str, Any]] = []
    seen_shots: set[tuple[str, str]] = set()
    seen_internals: set[str] = set()
    for index, occurrence in enumerate(occurrences):
        if not isinstance(occurrence, Mapping):
            raise ValueError(f"exact parent occurrence {index} is not an object")
        shot_id = occurrence.get("shot_id")
        shot_revision_id = occurrence.get("shot_revision_id", occurrence.get("revision_id"))
        if not isinstance(shot_id, str) or not isinstance(shot_revision_id, str):
            raise ValueError(f"exact parent occurrence {index} does not pin a shot revision")
        shot_key = (shot_id, shot_revision_id)
        if shot_key in seen_shots:
            continue
        seen_shots.add(shot_key)
        shot = _exact_mapping(
            reader.get_project_shot_revision(project_id, shot_id, shot_revision_id),
            label=f"shot revision {shot_id}/{shot_revision_id}",
        )
        if shot.get("shot_id") != shot_id or shot.get("revision_id") != shot_revision_id:
            raise ValueError("Runtime returned a different pinned shot revision")
        shots.append(shot)
        internal_revision_id = shot.get("internal_timeline_revision_id")
        if not isinstance(internal_revision_id, str) or not internal_revision_id:
            raise ValueError(f"shot revision {shot_revision_id!r} has no internal timeline pin")
        if internal_revision_id in seen_internals:
            continue
        seen_internals.add(internal_revision_id)
        internal = _exact_mapping(
            reader.get_project_timeline_revision(
                project_id, timeline_id, internal_revision_id
            ),
            label=f"internal timeline revision {internal_revision_id}",
        )
        if internal.get("revision_id") != internal_revision_id:
            raise ValueError("Runtime returned a different pinned internal timeline revision")
        internals.append(internal)

    from astrid.core.timeline.shot_composition_projection import (
        project_runtime_parent_composition,
    )

    try:
        projected = project_runtime_parent_composition(
            parent,
            shot_revisions=shots,
            internal_timeline_revisions=internals,
        )
    except ValueError as exc:
        raise ValueError(f"canonical parent revision closure is not renderable: {exc}") from exc

    shot_rows = []
    for shot in shots:
        shot_payload = shot.get("payload") if isinstance(shot.get("payload"), Mapping) else {}
        metadata = shot_payload.get("metadata") if isinstance(shot_payload.get("metadata"), Mapping) else {}
        shot_rows.append({
            "shot_id": shot["shot_id"],
            "revision_id": shot["revision_id"],
            # Authoring shots store their human name on the payload itself
            # (``add_authoring_shot(name=...)``); metadata is the legacy spot.
            "name": str(
                metadata.get("name") or metadata.get("title") or shot_payload.get("name") or shot["shot_id"]
            ),
            "text_bindings": list(shot_payload.get("text_bindings") or [])
            if isinstance(shot_payload.get("text_bindings"), list)
            else [],
        })
    expansion = {
        "canonical": True,
        "parent_revision_id": parent_revision_id,
        "children": [
            {
                "timeline_id": row.get("timeline_id"),
                "revision_id": row.get("revision_id"),
                "config_version": 1,
                "config_hash": row.get("content_digest"),
            }
            for row in internals
        ],
        "shots": shot_rows,
        "occurrences": [dict(row) for row in projected.occurrences],
        "outputs": [dict(row) for row in projected.outputs],
        "graph": projected.graph,
    }
    return parent, projected, expansion


def resolve_managed_render_snapshot(
    *,
    project_ref: str,
    timeline_ref: str,
    expected_version: int | None = None,
    client: Any,
    candidate_preview: bool = False,
) -> ManagedRenderSnapshot:
    """Resolve one active timeline through the generated SDK read surface.

    ``client`` is the already-bound runtime client for this attempt. No
    renderer path opens a competing database owner or accepts local storage
    configuration.
    """
    project_result = client.projects.show(project_ref)
    if not project_result.ok or not project_result.data:
        raise ValueError(f"project not found: {project_ref!r}")
    project = project_result.data

    # Resolve identity from the active canonical listing, then read exactly one
    # immutable current parent head through native inspection.  The legacy
    # mutable timeline document is deliberately unavailable to renderers.
    rows = paged_rows(client.timelines.list, project_ref)
    listed_timeline = None
    if rows is not None:
        for row in rows:
            if not isinstance(row, Mapping):
                continue
            if str(timeline_ref) in {
                str(row.get("timeline_id", "")),
                str(row.get("timeline_ulid", "")),
                str(row.get("slug", "")),
            }:
                listed_timeline = row
                break
    if not isinstance(listed_timeline, Mapping):
        raise ValueError(
            f"timeline {timeline_ref!r} was not found in project {project_ref!r}"
        )
    if listed_timeline.get("archived_at") or listed_timeline.get("archived") is True:
        raise ValueError(
            f"timeline {timeline_ref!r} is archived; unarchive it before rendering"
        )
    timeline_id = listed_timeline.get("timeline_id") or listed_timeline.get("id")
    if not isinstance(timeline_id, str) or not timeline_id:
        raise ValueError("canonical timeline listing omitted timeline_id")
    version_value = listed_timeline.get("version")
    if isinstance(version_value, bool) or not isinstance(version_value, (int, float, str)):
        raise ValueError("canonical timeline listing has no version")
    try:
        version = int(version_value)
    except (TypeError, ValueError) as exc:
        raise ValueError("canonical timeline listing has an invalid version") from exc
    if version < 1:
        raise ValueError("canonical timeline version must be positive")
    if expected_version is not None and expected_version != version:
        raise ValueError(
            f"stale timeline version: expected {expected_version}, current version is {version}; "
            "show the timeline and retry with the current version"
        )

    inspected = client.timelines.inspect(
        project_ref, timeline_id, limit=100, detail=False, neighbors=0
    )
    if not inspected.ok or not isinstance(inspected.data, Mapping):
        raise ValueError("canonical timeline inspection is unavailable")
    inspection = inspected.data
    if inspection.get("representation") != "canonical_head" or inspection.get("is_current_head") is not True:
        raise ValueError(
            "Runtime did not return the canonical current timeline head; "
            "legacy timeline documents are not renderable"
        )
    parent_head = inspection.get("revision_id") or inspection.get("head_revision_id")
    if not isinstance(parent_head, str) or not parent_head:
        raise ValueError("canonical timeline inspection has no current head revision")
    inspected_timeline_id = inspection.get("timeline_id")
    if inspected_timeline_id is not None and str(inspected_timeline_id) != timeline_id:
        raise ValueError("Runtime inspected a different timeline identity")

    timeline_slug = str(listed_timeline.get("slug") or timeline_ref)
    project_id = str(project.get("id") or project["project_id"])
    if candidate_preview:
        # Candidate previews replace this base with their own frozen closure.
        # Resolve only the exact parent identity here: superseded source
        # children may be unavailable or intentionally unrenderable, and must
        # not be fetched before the candidate's changed/reused pins are read.
        exact_parent = _read_exact_parent_head(
            client=client,
            project_id=project_id,
            timeline_id=timeline_id,
            parent_revision_id=parent_head,
        )
        parent_digest = str(exact_parent["content_digest"])
        identity_config: dict[str, Any] = {"tracks": [], "clips": []}
        identity_registry: dict[str, Any] = {"assets": {}}
        return ManagedRenderSnapshot(
            project_id=project_id,
            project_slug=str(project["slug"]),
            timeline_id=timeline_id,
            timeline_ulid=str(listed_timeline.get("timeline_ulid") or timeline_id),
            timeline_slug=timeline_slug,
            config_version=version,
            head_event_id=parent_head,
            head_hash=parent_digest.removeprefix("sha256:"),
            config=identity_config,
            registry=identity_registry,
            config_hash=_digest(identity_config),
            registry_hash=_digest(identity_registry),
            materialized_registry_hash=_digest(identity_registry),
        )

    exact_parent, projected, expansion = _project_exact_parent_head(
        client=client,
        project_id=project_id,
        timeline_id=timeline_id,
        parent_revision_id=parent_head,
    )
    config = projected.config
    stored_registry = projected.registry
    composition_graph = projected.graph
    parent_digest = str(exact_parent["content_digest"])

    # The SDK's read model is the authority. Keep runtime-admitted media
    # identities in the snapshot; the generic host supplies bytes to the child
    # attempt without any local database or filesystem lookup.
    raw_registry = stored_registry
    config, stored_registry = _render_compatible_projection(config, stored_registry)
    config = _normalize_pinned_element_references(config)
    registry = _runtime_snapshot_registry(
        stored_registry,
        project_ref=project_ref,
        client=client,
    )
    config_hash = _digest(config)
    registry_hash = _digest(raw_registry)
    head_event_id = str(exact_parent["revision_id"])
    head_hash = str(exact_parent["content_digest"]).removeprefix("sha256:")
    return ManagedRenderSnapshot(
        project_id=project_id,
        project_slug=str(project["slug"]),
        timeline_id=timeline_id,
        timeline_ulid=str(listed_timeline.get("timeline_ulid") or timeline_id),
        timeline_slug=timeline_slug,
        config_version=version,
        head_event_id=head_event_id,
        head_hash=head_hash,
        config=config,
        registry=registry,
        config_hash=config_hash,
        registry_hash=registry_hash,
        materialized_registry_hash=_digest(registry),
        expansion=expansion,
        composition_graph=composition_graph,
    )


def materialize_managed_render_snapshot(
    attempt_root: Path,
    snapshot: ManagedRenderSnapshot,
) -> tuple[Path, Path, dict[str, Any]]:
    """Write deterministic renderer inputs beneath one assigned attempt.

    This helper is intentionally a pure attempt-artifact writer.  It never
    derives a path from a project slug or a project root; the generic host
    supplies the already-created attempt directory.
    """

    authority = snapshot.authority()
    snapshot_key = _digest(authority)
    destination = Path(attempt_root).resolve() / "managed-render" / snapshot_key
    destination.mkdir(parents=True, exist_ok=True)
    timeline_path = destination / "timeline.json"
    registry_path = destination / "assets.json"
    authority_path = destination / "authority.json"
    write_json_atomic(timeline_path, dict(snapshot.config))
    write_json_atomic(registry_path, dict(snapshot.registry))
    write_json_atomic(authority_path, authority)
    if snapshot.compiled_audio is not None:
        write_json_atomic(destination / "compiled-audio.json", dict(snapshot.compiled_audio))
    return timeline_path, registry_path, authority


__all__ = [
    "ManagedRenderValidationError",
    "ManagedRenderSnapshot",
    "materialize_managed_render_snapshot",
    "resolve_managed_render_snapshot",
    "validate_managed_render_snapshot",
]
