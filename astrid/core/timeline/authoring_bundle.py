"""Lossless authoring bundles and deterministic candidate compilation.

This module is the narrow bridge between an immutable parent-composition
closure and ordinary, editable Python objects.  It deliberately does not
introduce another timeline language: parent, shot, and internal-timeline
payloads remain the Runtime payloads that were read from the pinned closure.

The adapter gives every placement an independent authoring ``shot_id``.  A
legacy shared child is copied in memory, with globally-scoped shot-item IDs
remapped deterministically; opening never writes.  The compiler materializes
only changed/copied children and delegates the resulting publication to the
Runtime's existing immutable-child + parent-CAS transaction.
"""

from __future__ import annotations

import copy
import hashlib
import json
import re
from collections import Counter
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any, Protocol, runtime_checkable

from .parent_registry import ParentRegistryError, effective_parent_registry
from .compatibility import require_writable_candidate

AUTHORING_BUNDLE_SCHEMA_VERSION = 1
_DIGEST_RE = re.compile(r"^sha256:[0-9a-f]{64}$")
_BARE_DIGEST_RE = re.compile(r"^[0-9a-f]{64}$")
_MEDIA_SELECTOR_KEYS = ("asset", "asset_id", "media_id", "object_id")


class AuthoringBundleError(ValueError):
    """The pinned closure or complete candidate violates the bundle contract."""


class UnsupportedAuthoringEditError(AuthoringBundleError):
    """The candidate changed a field not admitted by the current compiler."""


@runtime_checkable
class AuthoringCandidateWriter(Protocol):
    """The existing Runtime publication port consumed by candidate commits."""

    def publish_parent_composition(
        self,
        project_id: str,
        timeline_id: str,
        publication: Mapping[str, Any],
        *,
        idempotency_key: str,
    ) -> Any: ...


def authoring_contract() -> dict[str, Any]:
    """Return the small, versioned contract used by downstream editors.

    This is descriptive capability metadata for the existing Runtime payloads,
    not a serialized operation vocabulary.  Direct edits and helpers both
    modify the same fields named here and use :func:`compile_authoring_candidate`.
    """

    return {
        "schema_version": AUTHORING_BUNDLE_SCHEMA_VERSION,
        "authored": {
            "parent": "parent payload except its derived occurrences closure",
            "placements": "occurrence timing, track, transform, gain, mute, provenance, and opaque fields",
            "shots": "complete shot payload and complete internal timeline payload",
        },
        "derived": [
            "base parent head and digest",
            "source-to-authoring identity mapping",
            "shot/internal/parent revision identities and content digests",
            "occurrence shot_revision_id pins",
            "dependency manifest",
        ],
        "selected_media_authority": (
            "internal timeline clip asset/media selector resolved through that timeline's registry; "
            "when a mirrored shot item changes, its media_id must resolve to the same immutable object"
        ),
        "capabilities": {
            "parent": {
                "editable": ["config", "clips", "registry", "layout", "extension_fields"],
                "derived": ["occurrences"],
            },
            "placement": {
                "editable": [
                    "order", "start_ms", "duration_ms", "track", "transform",
                    "gain", "muted", "provenance", "extension_fields",
                ],
                "guarded": ["source_offset", "speed"],
            },
            "shot_payload": {
                "editable": [
                    "items", "media_selection", "metadata", "pools", "bindings",
                    "audio", "generation_inputs", "extension_fields",
                ],
                "invariants": ["unique_item_ids", "canonical_media_ids"],
            },
            "internal_timeline": {
                "editable": [
                    "tracks", "clips", "timing", "geometry", "effects", "audio",
                    "layout", "registry_alternatives", "extension_fields",
                ],
                "invariants": ["unique_clip_ids", "selected_asset_resolves"],
            },
        },
        "timing": {
            "intervals": "half-open [start,end)",
            "placement_domain": "container-local milliseconds",
            "source_domain": "media-source offset",
            "compiler_policy": "ordinary authored field edits are lossless; interval helpers must declare quantization, ripple scope, and parent-duration policy",
            "later_edits_require": "explicit quantization, ripple scope, and parent-duration policy",
        },
        "admission": [
            "deterministic independent-shot materialization",
            "selected immutable media replacement",
            "shot item and internal clip/track structure edits",
            "placement timing, ordering, track, supported geometry, audio, effects, layout, and opaque edits",
            "parent authored payload edits except the derived occurrences field",
            "renderer-backed frozen candidate preview through managed rendering",
        ],
        "unsupported_until_extended": [
            "new media import or transcoding (use the existing media service first)",
            "renderer-specific capability negotiation",
            "placement source-offset/playback-rate edits and scale-only transforms (use internal clip trims/geometry)",
            "implicit ripple, quantization, or cross-timeline coordinate conversion",
        ],
    }


@dataclass(frozen=True)
class CandidateCompilation:
    """One deterministic Runtime publication plus its authoring identities."""

    candidate_digest: str
    publication: Mapping[str, Any]
    identity_mapping: Mapping[str, Any]
    changed_identities: tuple[Mapping[str, Any], ...]
    reused_identities: tuple[Mapping[str, Any], ...]

    def as_dict(self) -> dict[str, Any]:
        return {
            "candidate_digest": self.candidate_digest,
            "publication": copy.deepcopy(dict(self.publication)),
            "identity_mapping": copy.deepcopy(dict(self.identity_mapping)),
            "changed_identities": copy.deepcopy(list(self.changed_identities)),
            "reused_identities": copy.deepcopy(list(self.reused_identities)),
        }


def _copy(value: Any) -> Any:
    return copy.deepcopy(value)


def _canonical_json(value: Any) -> str:
    try:
        return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    except (TypeError, ValueError) as exc:
        raise AuthoringBundleError(f"authoring value is not canonical JSON: {exc}") from exc


def _digest(value: Any) -> str:
    return "sha256:" + hashlib.sha256(_canonical_json(value).encode("utf-8")).hexdigest()


def _identity(kind: str, *parts: Any) -> str:
    encoded = "\0".join([kind, *(_canonical_json(part) for part in parts)]).encode("utf-8")
    return f"{kind}-" + hashlib.sha256(encoded).hexdigest()


def _mapping(value: Any, path: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise AuthoringBundleError(f"{path} must be an object")
    return value


def _sequence(value: Any, path: str) -> Sequence[Any]:
    if isinstance(value, (str, bytes)) or not isinstance(value, Sequence):
        raise AuthoringBundleError(f"{path} must be a list")
    return value


def _string(value: Any, path: str) -> str:
    if not isinstance(value, str) or not value:
        raise AuthoringBundleError(f"{path} must be a non-empty string")
    return value


def _verify_digest(payload: Any, supplied: Any, path: str) -> str:
    supplied = _string(supplied, path)
    actual = _digest(payload)
    if supplied != actual:
        raise AuthoringBundleError(
            f"{path} does not match the pinned payload: expected {actual}, found {supplied}"
        )
    return actual


def _records_by_identity(
    records: Sequence[Mapping[str, Any]] | Mapping[str, Mapping[str, Any]],
    *,
    identity: str,
    path: str,
) -> dict[str, Mapping[str, Any]]:
    values = records.values() if isinstance(records, Mapping) else _sequence(records, path)
    result: dict[str, Mapping[str, Any]] = {}
    for index, raw in enumerate(values):
        row = _mapping(raw, f"{path}[{index}]")
        key = _string(row.get(identity), f"{path}[{index}].{identity}")
        if key in result:
            raise AuthoringBundleError(f"{path} contains duplicate {identity} {key!r}")
        result[key] = row
    return result


def _authoring_shot_id(
    project_id: str,
    timeline_id: str,
    occurrence_id: str,
    source_shot_id: str,
    *,
    shared: bool,
) -> str:
    if not shared:
        return source_shot_id
    return _identity("shot-placement", project_id, timeline_id, occurrence_id, source_shot_id)


def _remap_known_item_references(
    value: Any, item_ids: Mapping[str, str], *, key: str | None = None
) -> Any:
    """Remap known item-reference fields without rewriting opaque string data."""

    if isinstance(value, Mapping):
        result: dict[str, Any] = {}
        for child_key, child in value.items():
            if not isinstance(child_key, str):
                result[child_key] = _copy(child)
                continue
            if isinstance(child, str) and (
                child_key in {"item_id", "source_item_id", "selected_item_id", "parent_item_id"}
                or child_key.endswith("_item_id")
            ):
                result[child_key] = item_ids.get(child, child)
            elif isinstance(child, list) and child_key.endswith("_item_ids"):
                result[child_key] = [
                    item_ids.get(item, item) if isinstance(item, str) else _copy(item)
                    for item in child
                ]
            else:
                result[child_key] = _remap_known_item_references(child, item_ids, key=child_key)
        return result
    if isinstance(value, list):
        return [_remap_known_item_references(item, item_ids, key=key) for item in value]
    return _copy(value)


def _copy_shot_for_placement(
    payload: Mapping[str, Any],
    *,
    project_id: str,
    timeline_id: str,
    occurrence_id: str,
    authoring_shot_id: str,
    source_shot_id: str,
) -> tuple[dict[str, Any], dict[str, str]]:
    result = _copy(dict(payload))
    if authoring_shot_id == source_shot_id:
        return result, {}
    items = result.get("items", [])
    if not isinstance(items, list):
        raise AuthoringBundleError("shot revision payload.items must be a list")
    remap: dict[str, str] = {}
    for index, raw_item in enumerate(items):
        item = _mapping(raw_item, f"shot payload.items[{index}]")
        source_item_id = item.get("item_id", item.get("id"))
        source_item_id = _string(source_item_id, f"shot payload.items[{index}].item_id")
        if source_item_id in remap:
            raise AuthoringBundleError(
                f"shot payload contains duplicate item identity {source_item_id!r}"
            )
        remap[source_item_id] = _identity(
            "shot-item-placement",
            project_id,
            timeline_id,
            occurrence_id,
            authoring_shot_id,
            source_item_id,
        )
    result = _remap_known_item_references(result, remap)
    return result, remap


def open_authoring_bundle(
    parent_revision: Mapping[str, Any],
    *,
    shot_revisions: Sequence[Mapping[str, Any]] | Mapping[str, Mapping[str, Any]],
    internal_timeline_revisions: Sequence[Mapping[str, Any]] | Mapping[str, Mapping[str, Any]],
) -> dict[str, Any]:
    """Open one exact immutable closure as a detached, lossless working copy.

    ``parent_revision`` and both child collections are the exact-head Runtime
    read results.  No mutable child head is consulted and this function has no
    persistence port, so opening cannot mutate storage.
    """

    parent = _mapping(parent_revision, "parent_revision")
    project_id = _string(parent.get("project_id"), "parent_revision.project_id")
    timeline_id = _string(parent.get("timeline_id"), "parent_revision.timeline_id")
    revision_id = _string(parent.get("revision_id"), "parent_revision.revision_id")
    payload = _mapping(parent.get("payload"), "parent_revision.payload")
    parent_digest = _verify_digest(
        payload, parent.get("content_digest"), "parent_revision.content_digest"
    )
    occurrences = _sequence(payload.get("occurrences"), "parent_revision.payload.occurrences")
    shot_index = _records_by_identity(shot_revisions, identity="revision_id", path="shot_revisions")
    internal_index = _records_by_identity(
        internal_timeline_revisions,
        identity="revision_id",
        path="internal_timeline_revisions",
    )

    source_counts = Counter(
        _string(
            _mapping(row, f"occurrences[{index}]").get("shot_id"), f"occurrences[{index}].shot_id"
        )
        for index, row in enumerate(occurrences)
    )
    candidate_parent = _copy(dict(payload))
    candidate_parent.pop("occurrences", None)
    placements: list[dict[str, Any]] = []
    base_placements: dict[str, dict[str, Any]] = {}
    shots: dict[str, dict[str, Any]] = {}
    shot_sources: dict[str, dict[str, Any]] = {}
    seen_occurrences: set[str] = set()
    seen_authoring_shots: set[str] = set()
    seen_items: set[str] = set()

    for ordinal, raw_occurrence in enumerate(occurrences):
        occurrence = _copy(dict(_mapping(raw_occurrence, f"occurrences[{ordinal}]")))
        occurrence_id = _string(
            occurrence.get("occurrence_id"), f"occurrences[{ordinal}].occurrence_id"
        )
        if occurrence_id in seen_occurrences:
            raise AuthoringBundleError(f"duplicate occurrence identity {occurrence_id!r}")
        seen_occurrences.add(occurrence_id)
        source_shot_id = _string(occurrence.get("shot_id"), f"occurrences[{ordinal}].shot_id")
        source_revision_id = _string(
            occurrence.get("shot_revision_id"), f"occurrences[{ordinal}].shot_revision_id"
        )
        shot = shot_index.get(source_revision_id)
        if (
            shot is None
            or shot.get("shot_id") != source_shot_id
            or shot.get("project_id") != project_id
        ):
            raise AuthoringBundleError(
                f"occurrence {occurrence_id!r} does not resolve to its pinned shot revision"
            )
        shot_payload = _mapping(
            shot.get("payload"), f"shot_revisions[{source_revision_id}].payload"
        )
        shot_digest = _verify_digest(
            shot_payload,
            shot.get("content_digest"),
            f"shot_revisions[{source_revision_id}].content_digest",
        )
        internal_revision_id = _string(
            shot.get(
                "internal_timeline_revision_id", shot_payload.get("internal_timeline_revision_id")
            ),
            f"shot_revisions[{source_revision_id}].internal_timeline_revision_id",
        )
        internal = internal_index.get(internal_revision_id)
        if internal is None or internal.get("project_id") != project_id:
            raise AuthoringBundleError(
                f"shot revision {source_revision_id!r} is missing its pinned internal timeline"
            )
        internal_payload = _mapping(
            internal.get("payload"), f"internal_timeline_revisions[{internal_revision_id}].payload"
        )
        internal_digest = _verify_digest(
            internal_payload,
            internal.get("content_digest"),
            f"internal_timeline_revisions[{internal_revision_id}].content_digest",
        )
        internal_timeline_id = _string(
            internal.get("timeline_id"),
            f"internal_timeline_revisions[{internal_revision_id}].timeline_id",
        )
        authoring_shot_id = _authoring_shot_id(
            project_id,
            timeline_id,
            occurrence_id,
            source_shot_id,
            shared=source_counts[source_shot_id] > 1,
        )
        if authoring_shot_id in seen_authoring_shots:
            raise AuthoringBundleError(f"independent shot identity collision {authoring_shot_id!r}")
        seen_authoring_shots.add(authoring_shot_id)
        copied_shot, item_id_map = _copy_shot_for_placement(
            shot_payload,
            project_id=project_id,
            timeline_id=timeline_id,
            occurrence_id=occurrence_id,
            authoring_shot_id=authoring_shot_id,
            source_shot_id=source_shot_id,
        )
        for item in copied_shot.get("items", []):
            item_id = _string(item.get("item_id", item.get("id")), "shot payload item identity")
            if item_id in seen_items:
                raise AuthoringBundleError(f"authoring shot item identity collision {item_id!r}")
            seen_items.add(item_id)
        copied_internal = _copy(dict(internal_payload))
        source = {
            "shot_id": source_shot_id,
            "revision_id": source_revision_id,
            "content_digest": shot_digest,
            "internal_timeline_id": internal_timeline_id,
            "internal_timeline_revision_id": internal_revision_id,
            "internal_timeline_content_digest": internal_digest,
            "item_id_map": item_id_map,
        }
        shots[authoring_shot_id] = {
            "shot_id": authoring_shot_id,
            "base_payload": _copy(copied_shot),
            "payload": _copy(copied_shot),
            "base_internal_timeline": _copy(copied_internal),
            "internal_timeline": _copy(copied_internal),
        }
        shot_sources[authoring_shot_id] = source
        occurrence.pop("shot_revision_id", None)
        occurrence.pop("revision_id", None)
        occurrence["shot_id"] = authoring_shot_id
        placements.append(occurrence)
        base_placements[occurrence_id] = _copy(occurrence)

    return {
        "schema_version": AUTHORING_BUNDLE_SCHEMA_VERSION,
        "project_id": project_id,
        "timeline_id": timeline_id,
        "base_parent": {"revision_id": revision_id, "content_digest": parent_digest},
        "base_parent_payload": _copy(candidate_parent),
        "base_placements": _copy(base_placements),
        "parent": _copy(candidate_parent),
        "placements": placements,
        "shots": shots,
        "source_mapping": {
            "placements": base_placements,
            "shots": shot_sources,
        },
    }


def _changed_media_selector(base: Mapping[str, Any], candidate: Mapping[str, Any]) -> bool:
    return any(base.get(key) != candidate.get(key) for key in _MEDIA_SELECTOR_KEYS)


def _media_identity(value: Any, path: str) -> str:
    if not isinstance(value, str) or not (
        _DIGEST_RE.fullmatch(value) or _BARE_DIGEST_RE.fullmatch(value)
    ):
        raise AuthoringBundleError(f"{path} must be a SHA-256 media identity")
    # Runtime accepts both historical bare object IDs and canonical wire IDs.
    # Normalize only comparison/dependency identities; authored bytes remain
    # untouched in the publication payload.
    return value if value.startswith("sha256:") else "sha256:" + value


def _clip_selected_media(clip: Mapping[str, Any], internal: Mapping[str, Any], path: str) -> str:
    selector_key = next((key for key in _MEDIA_SELECTOR_KEYS if key in clip), None)
    if selector_key is None:
        raise AuthoringBundleError(f"{path} has no explicit media selector")
    selector = clip[selector_key]
    if selector_key in {"media_id", "object_id"}:
        return _media_identity(selector, f"{path}.{selector_key}")
    registry = internal.get("registry", {})
    assets = registry.get("assets", {}) if isinstance(registry, Mapping) else {}
    entry = assets.get(selector) if isinstance(assets, Mapping) else None
    if not isinstance(entry, Mapping):
        raise AuthoringBundleError(
            f"{path}.{selector_key} references missing registry asset {selector!r}"
        )
    for key in ("media_id", "object_id", "digest", "content_digest"):
        if key in entry and isinstance(entry[key], str):
            try:
                return _media_identity(entry[key], f"{path}.{selector_key}.{key}")
            except AuthoringBundleError:
                continue
    raise AuthoringBundleError(
        f"registry asset {selector!r} has no canonical selected media identity"
    )


def _asset_authority(value: Any) -> dict[str, Mapping[str, Any]]:
    """Collect closed source asset entries without changing authored payloads."""

    result: dict[str, Mapping[str, Any]] = {}

    def walk(node: Any) -> None:
        if not isinstance(node, Mapping):
            if isinstance(node, list):
                for child in node:
                    walk(child)
            return
        registry = node.get("registry")
        if isinstance(registry, Mapping) and isinstance(registry.get("assets"), Mapping):
            for key, entry in registry["assets"].items():
                if isinstance(key, str) and isinstance(entry, Mapping):
                    result.setdefault(key, entry)
        for child in node.values():
            walk(child)

    walk(value)
    return result


def _effective_parent_assets(payload: Mapping[str, Any]) -> dict[str, Mapping[str, Any]]:
    """Resolve the one parent registry namespace used by validation/rendering."""
    try:
        registry = effective_parent_registry(payload)
    except ParentRegistryError as exc:
        raise AuthoringBundleError(f"candidate.{exc}") from exc
    selected = registry.get("assets", {})
    return {
        str(key): value
        for key, value in (selected.items() if isinstance(selected, Mapping) else ())
        if isinstance(key, str) and isinstance(value, Mapping)
    }


def _with_source_asset_authority(
    internal: Mapping[str, Any], source_assets: Mapping[str, Mapping[str, Any]] | None
) -> Mapping[str, Any]:
    """Return a validation-only registry view with source entries as fallback."""

    if not source_assets:
        return internal
    local_registry = internal.get("registry", {})
    local_assets = local_registry.get("assets", {}) if isinstance(local_registry, Mapping) else {}
    merged_assets = dict(source_assets)
    if isinstance(local_assets, Mapping):
        merged_assets.update(local_assets)
    merged_registry = dict(local_registry) if isinstance(local_registry, Mapping) else {}
    merged_registry["assets"] = merged_assets
    result = dict(internal)
    result["registry"] = merged_registry
    return result


def _assert_shot_edit_supported(
    base: Mapping[str, Any], candidate: Mapping[str, Any], *, path: str
) -> set[str]:
    """Validate a complete shot payload while retaining arbitrary extensions.

    Shot payloads are Runtime-owned JSON, so the adapter must not maintain a
    second closed schema.  We do enforce the identities which the Runtime
    uses to resolve media and references, while allowing ordinary metadata,
    item insertion/removal, and opaque fields to round-trip unchanged.
    """

    del base  # The candidate is checked as a complete authored value below.
    candidate_items = candidate.get("items", [])
    if not isinstance(candidate_items, list):
        raise AuthoringBundleError(f"{path}.items must be a list")
    seen_items: set[str] = set()
    changed: set[str] = set()
    for index, raw_candidate in enumerate(candidate_items):
        item = dict(_mapping(raw_candidate, f"{path}.items[{index}]"))
        item_id = _string(
            item.get("item_id", item.get("id")), f"{path}.items[{index}].item_id"
        )
        if item_id in seen_items:
            raise AuthoringBundleError(f"{path}.items contains duplicate item identity {item_id!r}")
        seen_items.add(item_id)
        if "media_id" in item:
            changed.add(_media_identity(item["media_id"], f"{path}.items[{index}].media_id"))
    return changed


def _assert_internal_edit_supported(
    base: Mapping[str, Any], candidate: Mapping[str, Any], *, path: str,
    source_assets: Mapping[str, Mapping[str, Any]] | None = None,
) -> set[str]:
    """Admit authored timeline edits while validating media selectors.

    Tracks, clips, effects, audio, layout, and extension fields are all
    authored Runtime payload.  Structural changes are therefore safe to carry
    through this lossless adapter; the checks here only reject malformed or
    dangling identity/media references before the existing Runtime boundary.
    """

    del base
    candidate_for_media = _with_source_asset_authority(candidate, source_assets)
    candidate_clips = candidate.get("clips", [])
    if not isinstance(candidate_clips, list):
        raise AuthoringBundleError(f"{path}.clips must be a list")
    seen_clip_ids: set[str] = set()
    changed_media: set[str] = set()
    selected_asset_keys: set[str] = set()
    for index, raw_candidate in enumerate(candidate_clips):
        candidate_clip = dict(_mapping(raw_candidate, f"{path}.clips[{index}]"))
        clip_id = _string(candidate_clip.get("id"), f"{path}.clips[{index}].id")
        if clip_id in seen_clip_ids:
            raise AuthoringBundleError(f"{path}.clips contains duplicate id {clip_id!r}")
        seen_clip_ids.add(clip_id)
        selector_key = next((key for key in _MEDIA_SELECTOR_KEYS if key in candidate_clip), None)
        if selector_key is None:
            continue  # Text/effect/placeholder clips may intentionally have no media.
        if selector_key in {"asset", "asset_id"}:
            selected_asset_keys.add(str(candidate_clip[selector_key]))
        changed_media.add(
            _clip_selected_media(candidate_clip, candidate_for_media, f"{path}.clips[{index}]")
        )

    registry = _mapping(candidate_for_media.get("registry", {}), f"{path}.registry")
    candidate_assets = _mapping(registry.get("assets", {}), f"{path}.registry.assets")
    # Unreferenced alternatives may be removed, but a candidate may not leave
    # a selected asset key dangling.  Existing immutable media remains in the
    # Runtime store and is never deleted by a working-copy edit.
    missing = selected_asset_keys - set(candidate_assets)
    if missing:
        raise AuthoringBundleError(
            f"{path}.registry.assets: selected asset(s) are missing: {sorted(missing)!r}"
        )
    return changed_media


def _shot_media_set(payload: Mapping[str, Any], path: str) -> set[str]:
    result: set[str] = set()
    items = payload.get("items", [])
    if not isinstance(items, list):
        return result
    for index, raw_item in enumerate(items):
        item = _mapping(raw_item, f"{path}.items[{index}]")
        if "media_id" in item:
            result.add(_media_identity(item["media_id"], f"{path}.items[{index}].media_id"))
    return result


def _shot_media_mutations(
    base: Mapping[str, Any], candidate: Mapping[str, Any], path: str
) -> set[str]:
    """Return media changed on an existing mirrored shot item.

    Newly added items are allowed to carry alternatives or metadata mirrors;
    changing an existing item to a new selected object must still agree with
    the internal timeline's newly selected media.
    """

    def index(payload: Mapping[str, Any], payload_path: str) -> dict[str, str]:
        items = payload.get("items", [])
        if not isinstance(items, list):
            return {}
        result: dict[str, str] = {}
        for item_index, raw_item in enumerate(items):
            item = _mapping(raw_item, f"{payload_path}.items[{item_index}]")
            item_id = _string(
                item.get("item_id", item.get("id")),
                f"{payload_path}.items[{item_index}].item_id",
            )
            if "media_id" in item:
                result[item_id] = _media_identity(
                    item["media_id"], f"{payload_path}.items[{item_index}].media_id"
                )
        return result

    before = index(base, f"{path}.base")
    after = index(candidate, path)
    return {
        media_id
        for item_id, media_id in after.items()
        if item_id in before and before[item_id] != media_id
    }


def _is_default_speed(value: Any) -> bool:
    if value in (None, 1, 1.0):
        return True
    return isinstance(value, Mapping) and value.get("numerator") == 1 and value.get("denominator") == 1


def _is_default_source_offset(value: Any) -> bool:
    return value in (None, 0, 0.0, {}) or (
        isinstance(value, Mapping)
        and value.get("start", 0) == 0
        and value.get("end", 0) == 0
    )


def _validate_placement_capabilities(
    placement: Mapping[str, Any], *, path: str, fields: set[str] | None = None
) -> None:
    """Reject placement semantics the shared projection cannot execute.

    For an existing placement, ``fields`` contains only values the author
    changed, so inherited legacy metadata is preserved verbatim. A placement
    with no pinned baseline is checked in full.
    """

    if fields is None or "source_offset" in fields:
        if not _is_default_source_offset(placement.get("source_offset")):
            raise UnsupportedAuthoringEditError(f"{path}.source_offset requires an internal clip source trim")
    if fields is None or "speed" in fields:
        if not _is_default_speed(placement.get("speed")):
            raise UnsupportedAuthoringEditError(f"{path}.speed requires an explicit clip playback-rate edit")
    transform = placement.get("transform") if fields is None or "transform" in fields else None
    if transform is None:
        return
    if not isinstance(transform, Mapping):
        return
    allowed = {"x", "y", "width", "height", "cropTop", "cropBottom", "cropLeft", "cropRight", "opacity", "scale"}
    unsupported = {
        key for key in transform
        if key not in allowed and not str(key).startswith("opaque_")
    }
    if unsupported:
        raise UnsupportedAuthoringEditError(f"{path}.transform has unsupported fields: {sorted(unsupported)!r}")
    if transform.get("scale", 1) != 1:
        raise UnsupportedAuthoringEditError(f"{path}.transform.scale requires explicit clip geometry")


def _internal_media_set(
    payload: Mapping[str, Any], path: str,
    source_assets: Mapping[str, Mapping[str, Any]] | None = None,
) -> set[str]:
    result: set[str] = set()
    clips = payload.get("clips", [])
    if not isinstance(clips, list):
        return result
    payload_for_media = _with_source_asset_authority(payload, source_assets)
    for index, raw_clip in enumerate(clips):
        clip = _mapping(raw_clip, f"{path}.clips[{index}]")
        if any(key in clip for key in _MEDIA_SELECTOR_KEYS):
            result.add(_clip_selected_media(clip, payload_for_media, f"{path}.clips[{index}]"))
    return result


def _collect_media(value: Any) -> set[str]:
    result: set[str] = set()
    if isinstance(value, Mapping):
        for key, child in value.items():
            if (
                key in {"media_id", "object_id"}
                and isinstance(child, str)
                and (_DIGEST_RE.fullmatch(child) or _BARE_DIGEST_RE.fullmatch(child))
            ):
                result.add(child if child.startswith("sha256:") else "sha256:" + child)
            result.update(_collect_media(child))
    elif isinstance(value, list):
        for child in value:
            result.update(_collect_media(child))
    return result


def compile_authoring_candidate(candidate: Mapping[str, Any]) -> CandidateCompilation:
    """Compile a complete working copy into one deterministic CAS publication.

    The candidate is complete: supported structural, timing, geometry,
    media, effects/audio/layout, opaque, and parent authored fields are
    compiled together.  Every untouched field is compared with the pinned
    base and copied verbatim; there is still only one publication path.
    """

    root = _mapping(candidate, "candidate")
    require_writable_candidate(root)
    if root.get("schema_version") != AUTHORING_BUNDLE_SCHEMA_VERSION:
        raise AuthoringBundleError(
            f"candidate.schema_version must be {AUTHORING_BUNDLE_SCHEMA_VERSION}"
        )
    project_id = _string(root.get("project_id"), "candidate.project_id")
    timeline_id = _string(root.get("timeline_id"), "candidate.timeline_id")
    base_parent = _mapping(root.get("base_parent"), "candidate.base_parent")
    expected_head = _string(base_parent.get("revision_id"), "candidate.base_parent.revision_id")
    _string(base_parent.get("content_digest"), "candidate.base_parent.content_digest")
    _mapping(root.get("base_parent_payload"), "candidate.base_parent_payload")
    parent_payload = _mapping(root.get("parent"), "candidate.parent")
    if "occurrences" in parent_payload:
        raise UnsupportedAuthoringEditError(
            "candidate.parent.occurrences is derived; edit candidate.placements instead"
        )
    placements = _sequence(root.get("placements"), "candidate.placements")
    shots = _mapping(root.get("shots"), "candidate.shots")
    source_mapping = _mapping(root.get("source_mapping"), "candidate.source_mapping")
    source_placements = _mapping(
        source_mapping.get("placements"), "candidate.source_mapping.placements"
    )
    base_placements = _mapping(root.get("base_placements"), "candidate.base_placements")
    source_shots = _mapping(source_mapping.get("shots"), "candidate.source_mapping.shots")
    # A complete candidate may add/remove/reorder placements and independent
    # authoring shots.  Every authored shot still needs an explicit source
    # mapping so the compiler can distinguish a copied child from an existing
    # immutable revision; helpers populate this mapping for new identities.
    if not set(source_shots).issuperset(shots):
        raise AuthoringBundleError(
            "candidate.source_mapping.shots must include every candidate shot identity"
        )

    # Exact closures occasionally store a clip's selected registry key in the
    # parent/shot authority while the child timeline payload omits its local
    # registry.  Keep that source authority validation-only and never merge it
    # into the authored publication bytes.
    # Only the pinned parent is a shared fallback authority.  Do not union
    # sibling shot registries: that would let one shot's local key validate a
    # different shot, and it would resurrect a selected asset deliberately
    # removed from its owning candidate registry.
    source_assets = _effective_parent_assets(parent_payload)

    candidate_digest = _digest(root)
    compiled_shots: dict[str, dict[str, Any]] = {}
    internal_revisions: list[dict[str, Any]] = []
    shot_revisions: list[dict[str, Any]] = []
    changed: list[dict[str, Any]] = []
    reused: list[dict[str, Any]] = []
    identity_mapping: dict[str, Any] = {"placements": {}, "shots": {}}

    for authoring_shot_id in sorted(shots):
        shot = _mapping(shots[authoring_shot_id], f"candidate.shots[{authoring_shot_id!r}]")
        if shot.get("shot_id") != authoring_shot_id:
            raise AuthoringBundleError(
                f"candidate.shots[{authoring_shot_id!r}].shot_id must match its key"
            )
        source = _mapping(
            source_shots[authoring_shot_id],
            f"candidate.source_mapping.shots[{authoring_shot_id!r}]",
        )
        source_shot_id = _string(source.get("shot_id"), "source shot_id")
        base_shot = _mapping(
            shot.get("base_payload"), f"candidate.shots[{authoring_shot_id!r}].base_payload"
        )
        candidate_shot = _mapping(
            shot.get("payload"), f"candidate.shots[{authoring_shot_id!r}].payload"
        )
        base_internal = _mapping(
            shot.get("base_internal_timeline"),
            f"candidate.shots[{authoring_shot_id!r}].base_internal_timeline",
        )
        candidate_internal = _mapping(
            shot.get("internal_timeline"),
            f"candidate.shots[{authoring_shot_id!r}].internal_timeline",
        )
        shot_media = _assert_shot_edit_supported(
            base_shot, candidate_shot, path=f"shots[{authoring_shot_id}].payload"
        )
        shot_media_mutations = _shot_media_mutations(
            base_shot,
            candidate_shot,
            f"shots[{authoring_shot_id}].payload",
        )
        internal_media = _assert_internal_edit_supported(
            base_internal,
            candidate_internal,
            path=f"shots[{authoring_shot_id}].internal_timeline",
            source_assets=source_assets,
        )
        base_shot_media = _shot_media_set(base_shot, f"shots[{authoring_shot_id}].base_payload")
        base_internal_media = _internal_media_set(
            base_internal,
            f"shots[{authoring_shot_id}].base_internal_timeline",
            source_assets=source_assets,
        )
        shot_additions = shot_media - base_shot_media
        internal_additions = internal_media - base_internal_media
        # Shot items are a mirrored/metadata view and may legitimately carry
        # pre-existing media (alternatives, shared backgrounds, or provenance)
        # that the selected internal clips do not render.  A newly introduced
        # shot-item media and a newly introduced internal selection must agree;
        # unchanged extras are not a competing selection.
        if (
            shot_media
            and internal_media
            and shot_additions
            and internal_additions
            and shot_additions != internal_additions
        ):
            raise AuthoringBundleError(
                f"shot {authoring_shot_id!r} has competing selected media between shot items and internal clips"
            )
        if shot_media_mutations and not shot_media_mutations.issubset(internal_additions):
            raise AuthoringBundleError(
                f"shot {authoring_shot_id!r} has competing selected media between shot items and internal clips"
            )
        needs_materialization = (
            authoring_shot_id != source_shot_id
            or candidate_shot != base_shot
            or candidate_internal != base_internal
        )
        if needs_materialization:
            internal_payload = _copy(dict(candidate_internal))
            internal_revision_id = _identity(
                "authoring-timeline-revision",
                project_id,
                timeline_id,
                authoring_shot_id,
                internal_payload,
            )
            internal_timeline_id = (
                timeline_id
                if authoring_shot_id != source_shot_id
                else _string(source.get("internal_timeline_id"), "source internal_timeline_id")
            )
            internal_digest = _digest(internal_payload)
            shot_payload = _copy(dict(candidate_shot))
            shot_payload["internal_timeline_revision_id"] = internal_revision_id
            shot_digest = _digest(shot_payload)
            shot_revision_id = _identity(
                "authoring-shot-revision", project_id, authoring_shot_id, shot_payload
            )
            internal_revisions.append(
                {
                    "timeline_id": internal_timeline_id,
                    "revision_id": internal_revision_id,
                    "payload": internal_payload,
                    "content_digest": internal_digest,
                }
            )
            shot_revisions.append(
                {
                    "shot_id": authoring_shot_id,
                    "revision_id": shot_revision_id,
                    "internal_timeline_revision_id": internal_revision_id,
                    "payload": shot_payload,
                    "content_digest": shot_digest,
                }
            )
            changed.append(
                {
                    "shot_id": authoring_shot_id,
                    "shot_revision_id": shot_revision_id,
                    "internal_timeline_revision_id": internal_revision_id,
                }
            )
        else:
            shot_revision_id = _string(source.get("revision_id"), "source revision_id")
            shot_digest = _string(source.get("content_digest"), "source content_digest")
            internal_revision_id = _string(
                source.get("internal_timeline_revision_id"), "source internal_timeline_revision_id"
            )
            internal_digest = _string(
                source.get("internal_timeline_content_digest"), "source internal timeline digest"
            )
            internal_timeline_id = _string(
                source.get("internal_timeline_id"), "source internal timeline_id"
            )
            reused.append(
                {
                    "shot_id": authoring_shot_id,
                    "shot_revision_id": shot_revision_id,
                    "internal_timeline_revision_id": internal_revision_id,
                }
            )
        compiled_shots[authoring_shot_id] = {
            "shot_id": authoring_shot_id,
            "revision_id": shot_revision_id,
            "internal_timeline_revision_id": internal_revision_id,
            "internal_timeline_id": internal_timeline_id,
            "content_digest": shot_digest,
            "internal_timeline_content_digest": internal_digest,
        }
        identity_mapping["shots"][authoring_shot_id] = {
            "source_shot_id": source_shot_id,
            "source_revision_id": source.get("revision_id"),
            **_copy(compiled_shots[authoring_shot_id]),
        }

    compiled_occurrences: list[dict[str, Any]] = []
    seen_occurrences: set[str] = set()
    for ordinal, raw_placement in enumerate(placements):
        placement = _copy(dict(_mapping(raw_placement, f"candidate.placements[{ordinal}]")))
        occurrence_id = _string(
            placement.get("occurrence_id"), f"candidate.placements[{ordinal}].occurrence_id"
        )
        if occurrence_id in seen_occurrences:
            raise AuthoringBundleError(f"duplicate occurrence identity {occurrence_id!r}")
        seen_occurrences.add(occurrence_id)
        base_placement = source_placements.get(occurrence_id)
        if base_placement is not None and not isinstance(base_placement, Mapping):
            raise AuthoringBundleError(
                f"candidate.source_mapping.placements[{occurrence_id!r}] must be an object"
            )
        # New placements are valid when their complete occurrence object is
        # supplied. Existing placement identity is immutable. Source offsets
        # are a media-domain operation owned by the internal clip helpers; do
        # not publish a changed placement offset that the shared projection
        # would silently ignore.
        pinned_placement = base_placements.get(occurrence_id)
        if pinned_placement is not None and not isinstance(pinned_placement, Mapping):
            raise AuthoringBundleError(f"candidate.base_placements[{occurrence_id!r}] must be an object")
        if pinned_placement is not None:
            changed_projection_fields = any(
                placement.get(field) != pinned_placement.get(field)
                for field in ("source_offset", "speed", "transform")
            )
            if changed_projection_fields:
                changed_fields = {
                    field
                    for field in ("source_offset", "speed", "transform")
                    if placement.get(field) != pinned_placement.get(field)
                }
                _validate_placement_capabilities(
                    placement,
                    path=f"candidate.placements[{ordinal}]",
                    fields=changed_fields,
                )
        else:
            _validate_placement_capabilities(placement, path=f"candidate.placements[{ordinal}]")
        # Timing, geometry, track, speed, audio, provenance, and extension
        # fields are carried through the canonical projection.
        authoring_shot_id = _string(
            placement.get("shot_id"), f"candidate.placements[{ordinal}].shot_id"
        )
        compiled = compiled_shots.get(authoring_shot_id)
        if compiled is None:
            raise AuthoringBundleError(
                f"candidate placement {occurrence_id!r} references missing shot"
            )
        placement["shot_id"] = authoring_shot_id
        placement["shot_revision_id"] = compiled["revision_id"]
        placement.pop("revision_id", None)
        compiled_occurrences.append(placement)
        identity_mapping["placements"][occurrence_id] = {
            "occurrence_id": occurrence_id,
            "shot_id": authoring_shot_id,
            "shot_revision_id": compiled["revision_id"],
        }

    referenced_shot_ids = {row["shot_id"] for row in compiled_occurrences}
    final_parent = _copy(dict(parent_payload))
    final_parent["occurrences"] = compiled_occurrences
    parent_digest = _digest(final_parent)
    parent_revision_id = _identity(
        "authoring-parent-revision", project_id, timeline_id, expected_head, final_parent
    )
    media = sorted(
        _collect_media(final_parent)
        | {
            digest
            for shot_id, row in shots.items()
            if shot_id in referenced_shot_ids
            for digest in _collect_media(row.get("payload"))
        }
        | {
            digest
            for shot_id, row in shots.items()
            if shot_id in referenced_shot_ids
            for digest in _collect_media(row.get("internal_timeline"))
        }
    )
    shot_manifest = [
        {
            "shot_id": value["shot_id"],
            "revision_id": value["revision_id"],
            "internal_timeline_revision_id": value["internal_timeline_revision_id"],
            "content_digest": value["content_digest"],
        }
        for value in sorted(
            (value for value in compiled_shots.values() if value["shot_id"] in referenced_shot_ids),
            key=lambda row: (row["shot_id"], row["revision_id"])
        )
    ]
    internal_manifest = [
        {
            "timeline_id": value["internal_timeline_id"],
            "revision_id": value["internal_timeline_revision_id"],
            "content_digest": value["internal_timeline_content_digest"],
        }
        for value in sorted(
            (value for value in compiled_shots.values() if value["shot_id"] in referenced_shot_ids),
            key=lambda row: (row["internal_timeline_id"], row["internal_timeline_revision_id"]),
        )
    ]
    # A shared immutable internal revision can appear under more than one
    # occurrence.  The Runtime manifest is a set-like closure, so dedupe it.
    internal_manifest = list(
        {(row["timeline_id"], row["revision_id"]): row for row in internal_manifest}.values()
    )
    # Do not register orphaned children when a working copy removed their
    # placements.  The publication closure is exactly what the parent now
    # references; immutable source rows remain untouched in Runtime storage.
    referenced_changed = {
        row["shot_id"] for row in changed if row["shot_id"] in referenced_shot_ids
    }
    referenced_internal_ids = {
        row["internal_timeline_revision_id"]
        for row in compiled_shots.values()
        if row["shot_id"] in referenced_shot_ids
    }
    publication = {
        "project_id": project_id,
        "timeline_id": timeline_id,
        "expected_head": expected_head,
        "parent_revision_id": parent_revision_id,
        "content_digest": parent_digest,
        "parent_composition": final_parent,
        "internal_timeline_revisions": sorted(
            (
                row
                for row in internal_revisions
                if row["revision_id"] in referenced_internal_ids
            ),
            key=lambda row: (row["timeline_id"], row["revision_id"]),
        ),
        "shot_revisions": sorted(
            (row for row in shot_revisions if row["shot_id"] in referenced_changed),
            key=lambda row: (row["shot_id"], row["revision_id"]),
        ),
        "dependency_manifest": {
            "shots": shot_manifest,
            "internal_timelines": internal_manifest,
            "media": [{"media_id": item, "content_digest": item} for item in media],
        },
    }
    return CandidateCompilation(
        candidate_digest=candidate_digest,
        publication=publication,
        identity_mapping=identity_mapping,
        changed_identities=tuple(changed),
        reused_identities=tuple(reused),
    )


def validate_authoring_candidate(candidate: Mapping[str, Any]) -> dict[str, Any]:
    """Validate a detached candidate without publishing it.

    Compilation is the authoritative validation path.  This summary deliberately
    omits the large publication payload while retaining the frozen digest and
    complete changed/reused identity lists for UI diagnostics.
    """

    compilation = compile_authoring_candidate(candidate)
    return {
        "valid": True,
        "candidate_digest": compilation.candidate_digest,
        "changed_identities": _copy(list(compilation.changed_identities)),
        "reused_identities": _copy(list(compilation.reused_identities)),
        "publication_digest": _digest(compilation.publication),
    }


def _diff_values(before: Any, after: Any, path: str, result: list[dict[str, Any]]) -> None:
    if isinstance(before, Mapping) and isinstance(after, Mapping):
        for key in sorted(set(before) | set(after), key=str):
            child_path = f"{path}.{key}" if path else str(key)
            if key not in before:
                result.append({"path": child_path, "kind": "added", "after": _copy(after[key])})
            elif key not in after:
                result.append({"path": child_path, "kind": "removed", "before": _copy(before[key])})
            else:
                _diff_values(before[key], after[key], child_path, result)
        return
    if isinstance(before, list) and isinstance(after, list):
        for index in range(max(len(before), len(after))):
            child_path = f"{path}[{index}]"
            if index >= len(before):
                result.append({"path": child_path, "kind": "added", "after": _copy(after[index])})
            elif index >= len(after):
                result.append({"path": child_path, "kind": "removed", "before": _copy(before[index])})
            else:
                _diff_values(before[index], after[index], child_path, result)
        return
    if before != after:
        result.append({"path": path, "kind": "changed", "before": _copy(before), "after": _copy(after)})


def diff_authoring_candidate(candidate: Mapping[str, Any]) -> dict[str, Any]:
    """Return a deterministic authored-field diff against the pinned bundle."""

    root = _mapping(candidate, "candidate")
    base = {
        "parent": root.get("base_parent_payload"),
        "placements": list(_mapping(
            _mapping(root.get("source_mapping"), "candidate.source_mapping").get(
                "placements", {}
            ), "candidate.source_mapping.placements"
        ).values()),
        "shots": {
            key: {
                "payload": value.get("base_payload"),
                "internal_timeline": value.get("base_internal_timeline"),
            }
            for key, value in _mapping(root.get("shots"), "candidate.shots").items()
            if isinstance(value, Mapping)
        },
    }
    current = {
        "parent": root.get("parent"),
        "placements": root.get("placements"),
        "shots": {
            key: {
                "payload": value.get("payload"),
                "internal_timeline": value.get("internal_timeline"),
            }
            for key, value in _mapping(root.get("shots"), "candidate.shots").items()
            if isinstance(value, Mapping)
        },
    }
    changes: list[dict[str, Any]] = []
    _diff_values(base, current, "", changes)
    return {
        "base_parent": _copy(_mapping(root.get("base_parent"), "candidate.base_parent")),
        "candidate_digest": _digest(root),
        "changed": changes,
        "change_count": len(changes),
    }


def authoring_media_inventory(candidate: Mapping[str, Any]) -> dict[str, Any]:
    """Inventory selected media and registry alternatives in one candidate.

    The inventory is read-only and intentionally reports unresolved selectors
    instead of guessing a latest object or silently dropping a missing asset.
    It can therefore be used by a source viewer before validation/commit.
    """

    root = _mapping(candidate, "candidate")
    project_id = _string(root.get("project_id"), "candidate.project_id")
    candidate_digest = _digest(root)
    entries: dict[str, dict[str, Any]] = {}
    unresolved: list[dict[str, Any]] = []

    def add(media_id: str, path: str, *, selected: bool, role: str) -> None:
        row = entries.setdefault(
            media_id,
            {"media_id": media_id, "selected": False, "usage": [], "metadata": []},
        )
        row["selected"] = bool(row["selected"] or selected)
        usage = {"path": path, "role": role, "selected": selected}
        if usage not in row["usage"]:
            row["usage"].append(usage)

    def walk(value: Any, path: str, registry: Mapping[str, Any] | None = None) -> None:
        if isinstance(value, Mapping):
            for key, child in value.items():
                child_path = f"{path}.{key}" if path else str(key)
                if key == "registry":
                    continue
                if key in {"media_id", "object_id"} and isinstance(child, str):
                    if _DIGEST_RE.fullmatch(child) or _BARE_DIGEST_RE.fullmatch(child):
                        add(_media_identity(child, child_path), child_path, selected=True, role="direct")
                elif key in {"asset", "asset_id"} and isinstance(child, str):
                    if _DIGEST_RE.fullmatch(child) or _BARE_DIGEST_RE.fullmatch(child):
                        add(_media_identity(child, child_path), child_path, selected=True, role="selected")
                    elif registry is not None:
                        try:
                            add(
                                _clip_selected_media({key: child}, {"registry": registry}, child_path),
                                child_path,
                                selected=True,
                                role="selected",
                            )
                        except AuthoringBundleError as exc:
                            unresolved.append({"path": child_path, "selector": child, "error": str(exc)})
                else:
                    walk(child, child_path, registry)

    parent = root.get("parent", {})
    parent_registry = parent.get("registry", {}) if isinstance(parent, Mapping) else {}
    walk(parent, "parent", parent_registry if isinstance(parent_registry, Mapping) else None)
    for index, placement in enumerate(_sequence(root.get("placements", []), "candidate.placements")):
        walk(placement, f"placements[{index}]")
    for shot_id, shot in _mapping(root.get("shots"), "candidate.shots").items():
        if not isinstance(shot, Mapping):
            continue
        payload = shot.get("payload", {})
        walk(payload, f"shots[{shot_id!r}].payload")
        internal = shot.get("internal_timeline", {})
        registry = internal.get("registry", {}) if isinstance(internal, Mapping) else {}
        walk(internal, f"shots[{shot_id!r}].internal_timeline", registry if isinstance(registry, Mapping) else None)
        assets = registry.get("assets", {}) if isinstance(registry, Mapping) else {}
        if isinstance(assets, Mapping):
            for asset_key, metadata in assets.items():
                if not isinstance(metadata, Mapping):
                    continue
                for field in ("media_id", "object_id", "digest", "content_digest"):
                    media_id = metadata.get(field)
                    if not isinstance(media_id, str) or not (
                        _DIGEST_RE.fullmatch(media_id) or _BARE_DIGEST_RE.fullmatch(media_id)
                    ):
                        continue
                    media_id = _media_identity(
                        media_id,
                        f"shots[{shot_id!r}].internal_timeline.registry.assets.{asset_key}.{field}",
                    )
                    row = entries.setdefault(
                        media_id,
                        {"media_id": media_id, "selected": False, "usage": [], "metadata": []},
                    )
                    row["metadata"].append(
                        {"path": f"shots[{shot_id!r}].internal_timeline.registry.assets.{asset_key}", "asset": asset_key, "value": _copy(dict(metadata))}
                    )
                    break
    for row in entries.values():
        row["usage"].sort(key=lambda item: (item["path"], item["role"]))
        row["metadata"].sort(key=lambda item: item["path"])
        # The Runtime media service already owns source opening.  Return a
        # stable, non-mutating handle for it instead of guessing a filesystem
        # path or creating a second media authority.
        row["source_ref"] = {
            "kind": "runtime-media-object",
            "project_id": project_id,
            "object_id": row["media_id"].removeprefix("sha256:"),
            "digest": row["media_id"],
        }
    return {
        "candidate_digest": candidate_digest,
        "base_parent": _copy(_mapping(root.get("base_parent"), "candidate.base_parent")),
        "composed_ref": {
            "kind": "authoring-candidate",
            "candidate_digest": candidate_digest,
            "base_parent": _copy(_mapping(root.get("base_parent"), "candidate.base_parent")),
        },
        "media": [entries[key] for key in sorted(entries)],
        "unresolved": sorted(unresolved, key=lambda item: item["path"]),
    }


def inspect_authoring_candidate(
    candidate: Mapping[str, Any], *, offset: int = 0, limit: int = 20
) -> dict[str, Any]:
    """Return a bounded, compact inspection of one exact-head candidate.

    This is a projection of the existing bundle, not another timeline format.
    Compilation first proves the candidate has a coherent pinned closure; the
    response then exposes the base head, selected media, placement structure,
    unresolved selectors, and explicit pagination omissions without copying
    opaque payloads into an agent-facing response.
    """

    if isinstance(offset, bool) or not isinstance(offset, int) or offset < 0:
        raise AuthoringBundleError("inspection offset must be a non-negative integer")
    if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 100:
        raise AuthoringBundleError("inspection limit must be an integer from 1 to 100")

    root = _mapping(candidate, "candidate")
    compilation = compile_authoring_candidate(candidate)
    inventory = authoring_media_inventory(candidate)
    placements = _sequence(root.get("placements"), "candidate.placements")
    page = placements[offset : offset + limit]
    changed_shots = {
        str(row.get("shot_id"))
        for row in compilation.changed_identities
        if isinstance(row, Mapping) and row.get("shot_id") is not None
    }

    def placement_summary(row: Mapping[str, Any]) -> dict[str, Any]:
        result: dict[str, Any] = {
            "occurrence_id": row.get("occurrence_id"),
            "shot_id": row.get("shot_id"),
        }
        for field in (
            "track",
            "start_ms",
            "duration_ms",
            "source_offset",
            "speed",
            "transform",
            "gain",
            "muted",
        ):
            if field in row:
                result[field] = _copy(row[field])
        return result

    shots = _mapping(root.get("shots"), "candidate.shots")
    shot_rows = []
    for shot_id, shot in sorted(shots.items(), key=lambda item: str(item[0])):
        if not isinstance(shot, Mapping):
            continue
        source = _mapping(
            _mapping(root.get("source_mapping"), "candidate.source_mapping").get(
                "shots", {}
            ).get(shot_id, {}),
            f"candidate.source_mapping.shots[{shot_id!r}]",
        )
        shot_rows.append(
            {
                "shot_id": str(shot_id),
                "source_shot_id": source.get("shot_id"),
                "revision_id": source.get("revision_id"),
                "changed": str(shot_id) in changed_shots,
            }
        )

    media = [
        {
            "media_id": row["media_id"],
            "selected": bool(row.get("selected")),
            "usage_count": len(row.get("usage", [])),
            "metadata_count": len(row.get("metadata", [])),
            "source_ref": _copy(row.get("source_ref")),
        }
        for row in inventory["media"]
    ]
    omitted = max(0, len(placements) - len(page))
    return {
        "kind": "authoring-candidate-inspection",
        "schema_version": AUTHORING_BUNDLE_SCHEMA_VERSION,
        "project_id": root.get("project_id"),
        "timeline_id": root.get("timeline_id"),
        "head": _copy(_mapping(root.get("base_parent"), "candidate.base_parent")),
        "candidate_digest": compilation.candidate_digest,
        "publication_digest": _digest(compilation.publication),
        "composed_ref": _copy(inventory["composed_ref"]),
        "counts": {
            "placements": len(placements),
            "shots": len(shot_rows),
            "changed_shots": len(changed_shots),
            "media": len(media),
            "unresolved": len(inventory["unresolved"]),
        },
        "page": {
            "offset": offset,
            "limit": limit,
            "returned": len(page),
            "total": len(placements),
            "next_offset": offset + len(page) if offset + len(page) < len(placements) else None,
        },
        "placements": [placement_summary(row) for row in page],
        "shots": shot_rows,
        "media": media,
        "unresolved": _copy(inventory["unresolved"]),
        "omissions": {"placements": omitted} if omitted else {},
    }


def format_authoring_inspection(inspection: Mapping[str, Any]) -> str:
    """Render the bounded inspection as stable human-readable text."""

    value = _mapping(inspection, "inspection")
    head = _mapping(value.get("head"), "inspection.head")
    page = _mapping(value.get("page"), "inspection.page")
    lines = [
        f"{value.get('kind', 'authoring-candidate-inspection')} "
        f"{value.get('project_id')}/{value.get('timeline_id')}",
        f"head={head.get('revision_id')} digest={head.get('content_digest')}",
        f"candidate={value.get('candidate_digest')} placements="
        f"{page.get('offset')}..{page.get('offset', 0) + page.get('returned', 0)}"
        f"/{page.get('total')}",
    ]
    for row in value.get("placements", []):
        if not isinstance(row, Mapping):
            continue
        lines.append(
            f"placement {row.get('occurrence_id')} shot={row.get('shot_id')}"
            f" track={row.get('track', '<none>')}"
            f" start={row.get('start_ms', '<none>')}"
            f" duration={row.get('duration_ms', '<none>')}"
        )
    omissions = _mapping(value.get("omissions", {}), "inspection.omissions")
    if omissions:
        lines.append("omitted=" + ",".join(f"{key}:{omissions[key]}" for key in sorted(omissions)))
    unresolved = value.get("unresolved", [])
    if unresolved:
        lines.append(f"unresolved={len(unresolved)}")
    return "\n".join(lines)


def preview_authoring_candidate(candidate: Mapping[str, Any]) -> dict[str, Any]:
    """Freeze an exact candidate artifact for managed preview render admission."""

    compilation = compile_authoring_candidate(candidate)
    return {
        "kind": "authoring-candidate-preview",
        "candidate_digest": compilation.candidate_digest,
        "publication_digest": _digest(compilation.publication),
        "head": _copy(_mapping(candidate, "candidate").get("base_parent", {})),
        "label": "Unpublished candidate preview",
        "candidate": _copy(dict(candidate)),
        "publication": _copy(dict(compilation.publication)),
        "identity_mapping": _copy(dict(compilation.identity_mapping)),
    }


def approve_authoring_candidate(
    candidate: Mapping[str, Any],
    *,
    approver: str,
    approval_id: str,
) -> dict[str, Any]:
    """Create an approval bound to one frozen candidate and exact base head.

    Approval is deliberately a detached artifact.  It does not mutate Runtime
    state and cannot be reused for a later candidate revision, even when the
    later revision targets the same shot or timeline.
    """

    if not isinstance(approver, str) or not approver.strip():
        raise AuthoringBundleError("approver must be a non-empty string")
    if not isinstance(approval_id, str) or not approval_id.strip():
        raise AuthoringBundleError("approval_id must be a non-empty string")
    root = _mapping(candidate, "candidate")
    compilation = compile_authoring_candidate(candidate)
    base_parent = _mapping(root.get("base_parent"), "candidate.base_parent")
    return {
        "kind": "authoring-candidate-approval",
        "schema_version": AUTHORING_BUNDLE_SCHEMA_VERSION,
        "decision": "approved",
        "approval_id": approval_id.strip(),
        "approver": approver.strip(),
        "project_id": root["project_id"],
        "timeline_id": root["timeline_id"],
        "candidate_digest": compilation.candidate_digest,
        "publication_digest": _digest(compilation.publication),
        "base_parent": _copy(dict(base_parent)),
    }


def promote_approved_authoring_candidate(
    candidate: Mapping[str, Any],
    approval: Mapping[str, Any],
    writer: AuthoringCandidateWriter,
    *,
    idempotency_key: str,
) -> dict[str, Any]:
    """Promote exactly the candidate represented by an approval artifact."""

    if not isinstance(idempotency_key, str) or not idempotency_key:
        raise AuthoringBundleError("idempotency_key must be a non-empty string")
    approval_value = _mapping(approval, "approval")
    if approval_value.get("kind") != "authoring-candidate-approval":
        raise AuthoringBundleError("approval.kind is invalid")
    if approval_value.get("schema_version") != AUTHORING_BUNDLE_SCHEMA_VERSION:
        raise AuthoringBundleError("approval.schema_version is invalid")
    if approval_value.get("decision") != "approved":
        raise AuthoringBundleError("candidate is not approved")
    compilation = compile_authoring_candidate(candidate)
    root = _mapping(candidate, "candidate")
    base_parent = _mapping(root.get("base_parent"), "candidate.base_parent")
    expected = {
        "project_id": root["project_id"],
        "timeline_id": root["timeline_id"],
        "candidate_digest": compilation.candidate_digest,
        "publication_digest": _digest(compilation.publication),
        "base_parent": dict(base_parent),
    }
    for field, value in expected.items():
        if approval_value.get(field) != value:
            raise AuthoringBundleError(
                f"approval is stale or does not match candidate: {field}"
            )
    publication = compilation.publication
    result = writer.publish_parent_composition(
        publication["project_id"],
        publication["timeline_id"],
        _copy(dict(publication)),
        idempotency_key=idempotency_key,
    )
    return {
        "approval": _copy(dict(approval_value)),
        "candidate_digest": compilation.candidate_digest,
        "identity_mapping": _copy(dict(compilation.identity_mapping)),
        "changed_identities": _copy(list(compilation.changed_identities)),
        "publication": result,
    }


def publish_authoring_candidate(
    candidate: Mapping[str, Any],
    writer: AuthoringCandidateWriter,
    *,
    idempotency_key: str,
    media_imports: Sequence[Mapping[str, Any]] | None = None,
) -> dict[str, Any]:
    """Compile and publish through the existing atomic Runtime boundary.

    Optional ``media_imports`` are receipts from the existing catalog import
    helper, used only for feedback. Without them, catalog counts stay unknown.
    """

    if not isinstance(idempotency_key, str) or not idempotency_key:
        raise AuthoringBundleError("idempotency_key must be a non-empty string")
    compilation = compile_authoring_candidate(candidate)
    from .authoring_feedback import authoring_change_summary, publication_feedback

    summary = authoring_change_summary(candidate, media_imports=media_imports)
    publication = compilation.publication
    result = writer.publish_parent_composition(
        publication["project_id"],
        publication["timeline_id"],
        _copy(dict(publication)),
        idempotency_key=idempotency_key,
    )
    return {
        "candidate_digest": compilation.candidate_digest,
        "identity_mapping": _copy(dict(compilation.identity_mapping)),
        "changed_identities": _copy(list(compilation.changed_identities)),
        "publication": result,
        **publication_feedback(
            summary, result,
            project_id=publication["project_id"], timeline_id=publication["timeline_id"],
        ),
    }


__all__ = [
    "AUTHORING_BUNDLE_SCHEMA_VERSION",
    "AuthoringBundleError",
    "AuthoringCandidateWriter",
    "CandidateCompilation",
    "UnsupportedAuthoringEditError",
    "authoring_contract",
    "authoring_media_inventory",
    "approve_authoring_candidate",
    "compile_authoring_candidate",
    "diff_authoring_candidate",
    "format_authoring_inspection",
    "inspect_authoring_candidate",
    "open_authoring_bundle",
    "publish_authoring_candidate",
    "promote_approved_authoring_candidate",
    "preview_authoring_candidate",
    "validate_authoring_candidate",
]
