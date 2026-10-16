"""Lazy SDK-facing adapter for the canonical timeline authoring bundle.

The bundle contract and compiler remain owned by ``astrid.core.timeline``;
this module only keeps the public SDK surface transport-friendly and avoids a
second representation or persistence path.
"""

import copy
import math
from collections.abc import Iterable, Mapping
from typing import Any

from astrid.core.timeline.authoring_bundle import (
    AUTHORING_BUNDLE_SCHEMA_VERSION,
    AuthoringBundleError,
    approve_authoring_candidate,
    authoring_media_inventory,
    compile_authoring_candidate,
    diff_authoring_candidate,
    format_authoring_inspection,
    inspect_authoring_candidate,
    open_authoring_bundle,
    preview_authoring_candidate,
    promote_approved_authoring_candidate,
    publish_authoring_candidate,
    validate_authoring_candidate,
)
from astrid.core.timeline.authoring_diff_format import format_authoring_revision_diff

from .authoring_media import import_authoring_media, plan_authoring_media
from .authoring_remote import AuthoringRemoteError, TargetBoundAuthoringBundle
from .authoring_render_preview import render_authoring_candidate_preview


def project_audio_lane_for_runtime_inspection(
    closure: Mapping[str, Any], *, capabilities: Iterable[str],
) -> dict[str, Any] | None:
    """Trusted Runtime composition over its verified, detached revision closure.

    Open only the supplied immutable revisions. The complete authoring bundle
    and caller-advertised capabilities pass directly to the canonical adapter.
    """
    bundle = open_authoring_bundle(
        closure["parent_revision"],
        shot_revisions=closure["shot_revisions"],
        internal_timeline_revisions=closure["internal_timeline_revisions"],
    )
    return project_audio_lane_for_authoring_bundle(bundle, capabilities=capabilities)


def project_audio_lane_for_authoring_bundle(
    authoring_bundle: Mapping[str, Any], *, capabilities: Iterable[str],
) -> dict[str, Any] | None:
    """Project the full lane from the caller's already-pinned inspection closure.

    Accept only a complete, unedited ``open_authoring_bundle`` result. Runtime
    owns authentication and the enclosing revision/digest binding; this adapter
    has no client, persistence port, or mutable-head lookup. Missing caller
    capabilities must never select the producer's default writer capabilities.
    """
    from astrid.sdk.timeline.checkout import Checkout
    from astrid.sdk.timeline.music_lane import project

    if not isinstance(capabilities, Iterable) or isinstance(capabilities, (str, bytes)):
        raise AuthoringBundleError("audio lane requires an explicit caller capability set")

    def mapping(value, path):
        if not isinstance(value, Mapping):
            raise AuthoringBundleError(f"{path} must be a complete object")
        return value

    def identity(value, path):
        if not isinstance(value, str) or not value:
            raise AuthoringBundleError(f"{path} requires its pinned identity")
        return value

    root = mapping(authoring_bundle, "authoring_bundle")
    if type(root.get("schema_version")) is not int or root["schema_version"] != AUTHORING_BUNDLE_SCHEMA_VERSION:
        raise AuthoringBundleError("audio lane requires an open_authoring_bundle result")
    for key in ("project_id", "timeline_id"):
        identity(root.get(key), key)
    base = mapping(root.get("base_parent"), "base_parent")
    for key in ("revision_id", "content_digest"):
        identity(base.get(key), f"base_parent.{key}")
    parent = mapping(root.get("parent"), "parent")
    if parent != mapping(root.get("base_parent_payload"), "base_parent_payload"):
        raise AuthoringBundleError("audio lane requires the unedited pinned parent")
    config = mapping(parent.get("config", {}), "parent.config")
    app = mapping(config.get("app", {}), "parent.config.app")
    ns = mapping(app.get("astrid", {}), "parent.config.app.astrid")
    if "bed" in ns and not isinstance(ns["bed"], Mapping):
        raise AuthoringBundleError("app.astrid.bed must be an adopted bed object")
    if "clips" in parent and not isinstance(parent["clips"], list):
        raise AuthoringBundleError("parent.clips must be a complete list")

    placements = root.get("placements")
    if not isinstance(placements, list):
        raise AuthoringBundleError("placements must be the complete pinned list")
    shots = mapping(root.get("shots"), "shots")
    sources = mapping(root.get("source_mapping"), "source_mapping")
    source_placements = mapping(sources.get("placements"), "source_mapping.placements")
    source_shots = mapping(sources.get("shots"), "source_mapping.shots")
    pinned_placements = mapping(root.get("base_placements"), "base_placements")
    seen = set()
    placed_shots = set()
    for row in placements:
        row = mapping(row, "placement")
        oid = identity(row.get("occurrence_id"), "placement.occurrence_id")
        sid = identity(row.get("shot_id"), "placement.shot_id")
        placement = mapping(row.get("placement", {}), f"{oid}.placement")
        for key, value in (("start_ms", row.get("at_ms", placement.get("start_ms"))),
                           ("duration_ms", row.get("duration_ms"))):
            if type(value) not in (int, float) or not math.isfinite(value) or value < 0:
                raise AuthoringBundleError(f"{oid}: missing or malformed pinned {key}")
        if oid in seen or row != source_placements.get(oid) or row != pinned_placements.get(oid):
            raise AuthoringBundleError(f"{oid}: placement differs from the pinned source mapping")
        if sid in placed_shots:
            raise AuthoringBundleError(f"{oid}: missing independent authoring shot identity")
        seen.add(oid)
        placed_shots.add(sid)
    if seen != set(source_placements) or seen != set(pinned_placements):
        raise AuthoringBundleError("incomplete pinned placement closure")
    if placed_shots != set(shots) or placed_shots != set(source_shots):
        raise AuthoringBundleError("incomplete pinned shot/source closure")
    for sid, raw in shots.items():
        shot = mapping(raw, f"shots[{sid}]")
        if shot.get("shot_id") != sid:
            raise AuthoringBundleError(f"{sid}: shot identity differs from its pinned key")
        source = mapping(source_shots[sid], f"source_mapping.shots[{sid}]")
        for key in ("shot_id", "revision_id", "content_digest", "internal_timeline_id",
                    "internal_timeline_revision_id", "internal_timeline_content_digest"):
            identity(source.get(key), f"source_mapping.shots[{sid}].{key}")
        mapping(source.get("item_id_map"), f"source_mapping.shots[{sid}].item_id_map")
        for key, base_key in (("payload", "base_payload"),
                              ("internal_timeline", "base_internal_timeline")):
            value = mapping(shot.get(key), f"shots[{sid}].{key}")
            if value != mapping(shot.get(base_key), f"shots[{sid}].{base_key}"):
                raise AuthoringBundleError(f"{sid}: {key} differs from the pinned source")
        internal = shot["internal_timeline"]
        for key in ("clips", "tracks"):
            if key in internal and not isinstance(internal[key], list):
                raise AuthoringBundleError(f"{sid}: internal {key} must be a complete list")

    # Checkout migration and producer read helpers may populate containers.
    # Detach before either can see the caller's authoritative inspection data.
    return project(Checkout(copy.deepcopy(dict(root))), capabilities=capabilities)


__all__ = [
    "authoring_media_inventory",
    "AuthoringRemoteError",
    "approve_authoring_candidate",
    "compile_authoring_candidate",
    "diff_authoring_candidate",
    "format_authoring_inspection",
    "format_authoring_revision_diff",
    "plan_authoring_media",
    "import_authoring_media",
    "inspect_authoring_candidate",
    "open_authoring_bundle",
    "project_audio_lane_for_authoring_bundle",
    "project_audio_lane_for_runtime_inspection",
    "preview_authoring_candidate",
    "render_authoring_candidate_preview",
    "publish_authoring_candidate",
    "promote_approved_authoring_candidate",
    "validate_authoring_candidate",
    "TargetBoundAuthoringBundle",
]
