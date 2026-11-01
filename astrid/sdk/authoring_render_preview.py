"""Admit a frozen authoring candidate through the ordinary managed renderer."""

from __future__ import annotations

import copy
from collections.abc import Mapping
from dataclasses import replace
from typing import Any

from astrid.core.timeline.authoring_bundle import (
    AuthoringBundleError,
    compile_authoring_candidate,
    preview_authoring_candidate,
)
from astrid.core.timeline.authoring_bundle import (
    _digest as authoring_digest,
)
from astrid.core.timeline.shot_composition_projection import project_runtime_parent_composition
from astrid.packs.rendering.actions.render.managed_timeline import (
    ManagedRenderSnapshot,
    _exact_mapping,
    _exact_revision_reader,
    _render_compatible_projection,
    _runtime_snapshot_registry,
)
from astrid.packs.rendering.actions.render.managed_timeline import (
    _digest as render_digest,
)


def candidate_projection(preview: Mapping[str, Any], *, client: Any, project_id: str) -> dict[str, Any]:
    """Project a frozen candidate (``preview_authoring_candidate``) without publishing it.

    Unchanged children are read by immutable identity from Runtime; changed children come
    from the deterministic candidate compilation. Returns ``{projected, shots, internals,
    publication, marker}``. Used by visualize to look at a working copy."""
    if preview.get("kind") != "authoring-candidate-preview":
        raise AuthoringBundleError("authoring_preview.kind is invalid")
    frozen = preview.get("candidate")
    if not isinstance(frozen, Mapping):
        raise AuthoringBundleError("authoring_preview.candidate is missing")
    compilation = compile_authoring_candidate(frozen)
    publication = compilation.publication
    if (
        preview.get("candidate_digest") != compilation.candidate_digest
        or preview.get("publication_digest") != authoring_digest(publication)
    ):
        raise AuthoringBundleError("authoring_preview does not match its frozen candidate")
    reader = _exact_revision_reader(client)
    changed_shots = {(row["shot_id"], row["revision_id"]): row for row in publication["shot_revisions"]}
    changed_internals = {(row["timeline_id"], row["revision_id"]): row for row in publication["internal_timeline_revisions"]}
    shots = []
    for pin in publication["dependency_manifest"]["shots"]:
        key = (pin["shot_id"], pin["revision_id"])
        row = changed_shots.get(key) or _exact_mapping(
            reader.get_project_shot_revision(project_id, *key), label=f"shot revision {key[0]}/{key[1]}")
        shots.append(copy.deepcopy(dict(row)))
    internals = []
    for pin in publication["dependency_manifest"]["internal_timelines"]:
        key = (pin["timeline_id"], pin["revision_id"])
        row = changed_internals.get(key) or _exact_mapping(
            reader.get_project_timeline_revision(project_id, *key), label=f"internal timeline revision {key[0]}/{key[1]}")
        internals.append(copy.deepcopy(dict(row)))
    parent = {
        "project_id": project_id,
        "timeline_id": publication["timeline_id"],
        "revision_id": publication["parent_revision_id"],
        "content_digest": publication["content_digest"],
        "payload": copy.deepcopy(publication["parent_composition"]),
    }
    projected = project_runtime_parent_composition(parent, shot_revisions=shots, internal_timeline_revisions=internals)
    marker = {
        "label": "Unpublished working copy",
        "base_parent": copy.deepcopy(dict(frozen.get("base_parent") or {})),
        "candidate_digest": compilation.candidate_digest,
        "publication_digest": preview["publication_digest"],
        "candidate_parent_revision_id": publication["parent_revision_id"],
    }
    return {"projected": projected, "shots": shots, "internals": internals, "publication": publication, "marker": marker}


def candidate_preview_snapshot(
    preview: Mapping[str, Any], *, snapshot: ManagedRenderSnapshot, client: Any
) -> ManagedRenderSnapshot:
    """Replace a pinned base snapshot with a validated, unpublished projection.

    Reused children are read by immutable identity from Runtime. Changed
    children come only from the deterministic candidate compilation. No
    publication method is called.
    """
    if preview.get("kind") != "authoring-candidate-preview":
        raise AuthoringBundleError("authoring_preview.kind is invalid")
    frozen = preview.get("candidate")
    if not isinstance(frozen, Mapping):
        raise AuthoringBundleError("authoring_preview.candidate is missing")
    compilation = compile_authoring_candidate(frozen)
    publication = compilation.publication
    if (
        preview.get("candidate_digest") != compilation.candidate_digest
        or preview.get("publication_digest") != authoring_digest(publication)
        or preview.get("publication") != publication
    ):
        raise AuthoringBundleError("authoring_preview does not match its frozen candidate")
    base_head = frozen.get("base_parent")
    if not isinstance(base_head, Mapping) or preview.get("head") != base_head:
        raise AuthoringBundleError("authoring_preview base head is inconsistent")
    if (
        publication["project_id"] != snapshot.project_id
        or publication["timeline_id"] != snapshot.timeline_id
    ):
        raise AuthoringBundleError("authoring_preview targets a different project or timeline")
    if (
        base_head.get("revision_id") != snapshot.head_event_id
        or base_head.get("content_digest") != "sha256:" + snapshot.head_hash
    ):
        raise AuthoringBundleError("authoring_preview base parent is no longer the exact render head")

    reader = _exact_revision_reader(client)
    changed_shots = {
        (row["shot_id"], row["revision_id"]): row
        for row in publication["shot_revisions"]
    }
    changed_internals = {
        (row["timeline_id"], row["revision_id"]): row
        for row in publication["internal_timeline_revisions"]
    }
    shots = []
    for pin in publication["dependency_manifest"]["shots"]:
        key = (pin["shot_id"], pin["revision_id"])
        row = changed_shots.get(key)
        if row is None:
            row = _exact_mapping(
                reader.get_project_shot_revision(snapshot.project_id, *key),
                label=f"shot revision {key[0]}/{key[1]}",
            )
        if (row.get("shot_id"), row.get("revision_id"), row.get("content_digest")) != (
            *key, pin["content_digest"]
        ):
            raise AuthoringBundleError(f"authoring_preview shot pin {key!r} is inconsistent")
        shots.append(copy.deepcopy(dict(row)))
    internals = []
    for pin in publication["dependency_manifest"]["internal_timelines"]:
        key = (pin["timeline_id"], pin["revision_id"])
        row = changed_internals.get(key)
        if row is None:
            row = _exact_mapping(
                reader.get_project_timeline_revision(snapshot.project_id, *key),
                label=f"internal timeline revision {key[0]}/{key[1]}",
            )
        if (row.get("timeline_id"), row.get("revision_id"), row.get("content_digest")) != (
            *key, pin["content_digest"]
        ):
            raise AuthoringBundleError(f"authoring_preview internal pin {key!r} is inconsistent")
        internals.append(copy.deepcopy(dict(row)))

    parent = {
        "project_id": snapshot.project_id,
        "timeline_id": snapshot.timeline_id,
        "revision_id": publication["parent_revision_id"],
        "content_digest": publication["content_digest"],
        "payload": copy.deepcopy(publication["parent_composition"]),
    }
    projected = project_runtime_parent_composition(
        parent, shot_revisions=shots, internal_timeline_revisions=internals
    )
    raw_registry = projected.registry
    render_config, render_registry = _render_compatible_projection(
        projected.config, raw_registry
    )
    registry = _runtime_snapshot_registry(
        render_registry, project_ref=snapshot.project_slug, client=client
    )
    shot_rows = []
    for shot in shots:
        payload = shot.get("payload") if isinstance(shot.get("payload"), Mapping) else {}
        metadata = payload.get("metadata") if isinstance(payload.get("metadata"), Mapping) else {}
        shot_rows.append({
            "shot_id": shot["shot_id"],
            "revision_id": shot["revision_id"],
            "name": str(metadata.get("name") or metadata.get("title") or shot["shot_id"]),
            "text_bindings": list(payload.get("text_bindings") or [])
            if isinstance(payload.get("text_bindings"), list) else [],
        })
    marker = {
        "label": "Unpublished candidate preview",
        "base_parent": copy.deepcopy(dict(base_head)),
        "candidate_digest": compilation.candidate_digest,
        "publication_digest": preview["publication_digest"],
        "candidate_parent_revision_id": publication["parent_revision_id"],
    }
    return replace(
        snapshot,
        config=render_config,
        registry=registry,
        config_hash=render_digest(render_config),
        registry_hash=render_digest(raw_registry),
        materialized_registry_hash=render_digest(registry),
        composition_graph=projected.graph,
        expansion={
            "canonical": True,
            "parent_revision_id": publication["parent_revision_id"],
            "children": [
                {"timeline_id": row["timeline_id"], "revision_id": row["revision_id"],
                 "config_version": 1, "config_hash": row["content_digest"]}
                for row in internals
            ],
            "shots": shot_rows,
            "occurrences": [dict(row) for row in projected.occurrences],
            "outputs": [dict(row) for row in projected.outputs],
            "graph": projected.graph,
        },
        authoring_preview=marker,
    )


def render_authoring_candidate_preview(
    candidate: Mapping[str, Any], client: Any, *, project: str, timeline_ref: str,
    wait: bool = True, **render_inputs: Any,
) -> Any:
    """Render a candidate or prior frozen preview with a managed run/receipt."""
    reserved = {"timeline_ref", "authoring_preview", "timeline_snapshot", "timeline_authority"}
    if reserved & render_inputs.keys():
        raise AuthoringBundleError("render inputs cannot override authoring preview authority")
    preview = (
        copy.deepcopy(dict(candidate))
        if candidate.get("kind") == "authoring-candidate-preview"
        else preview_authoring_candidate(candidate)
    )
    return client.invoke_result(
        "rendering.render", kind="executor", project=project, wait=wait,
        inputs={"review": True, **render_inputs, "timeline_ref": timeline_ref,
                "authoring_preview": preview},
    )


__all__ = ["candidate_preview_snapshot", "candidate_projection", "render_authoring_candidate_preview"]
