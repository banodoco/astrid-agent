"""Exact project-pack source selections carried by immutable render inputs."""

from __future__ import annotations

import copy
from dataclasses import replace
from pathlib import Path
from typing import Any, Mapping

from astrid.core.element import catalog, project_packs
from astrid.core.element.registry import ElementRegistry, load_default_registry, load_pack_elements

CARRIER = "astrid_project_element_snapshots"
PACK_ROOTS_ENV = "ASTRID_RENDER_PACK_ROOTS"
REGISTRY_ROOT_ENV = "ASTRID_RENDER_REGISTRY_ROOT"


def selections(config: Mapping[str, Any]) -> list[dict[str, Any]]:
    app = config.get("app")
    return app.get(CARRIER, []) if isinstance(app, Mapping) else []


def selection_key(reference: Mapping[str, str]) -> tuple[str, str, str, str]:
    return reference["packId"], reference["kind"], reference["id"], reference["revision"]


def render_key(selection: Mapping[str, Any]) -> str:
    ref = selection["reference"]
    scope = project_packs._json_digest([
        selection["realm"], selection["project"], ref["packId"], ref["kind"], ref["id"],
    ]).removeprefix("sha256:")
    return f"pinned_{scope}_{ref['revision'].removeprefix('sha256:')}"


def _is_builtin_reference(reference: Mapping[str, str]) -> bool:
    """Keep genuine installed pins on the built-in registry path.

    A ref carries no source-layer marker, so pack ID alone cannot distinguish
    an installed element from a project-pack admission that happens to reuse
    that ID. The complete catalog identity is the only safe built-in signal.
    """
    return any(
        all(descriptor.get(key) == reference[key]
            for key in ("packId", "kind", "id", "revision"))
        for descriptor in catalog.list_element_descriptors(include_shadowed=True)
    )


def _project_references(
    config: Mapping[str, Any], *, project_pack_owners: set[str] = frozenset()
) -> dict[tuple[str, str, str, str], dict[str, str]]:
    refs = {}
    for clip in config.get("clips", []):
        ref = clip.get("elementRef") if isinstance(clip, Mapping) else None
        if (isinstance(ref, Mapping)
                and all(isinstance(ref.get(key), str) and ref[key]
                        for key in ("packId", "kind", "id", "revision"))
                and not ref["revision"].startswith("draft-")):
            refs[selection_key(ref)] = dict(ref)
    if not refs:
        return refs
    selected = {selection_key(row["reference"]) for row in selections(config)}
    return {
        key: ref for key, ref in refs.items()
        if key in selected
        or ref["packId"] in project_pack_owners
        or not _is_builtin_reference(ref)
    }


def _project_pack_owners(
    config: Mapping[str, Any], *, client: Any, realm: str, project: str,
) -> set[str]:
    """Identify project owners before catalog identity can hide their refs."""
    owners = set()
    documents = getattr(client, "documents", None)
    for clip in config.get("clips", []):
        ref = clip.get("elementRef") if isinstance(clip, Mapping) else None
        if not (
            isinstance(ref, Mapping)
            and all(isinstance(ref.get(key), str) and ref[key]
                    for key in ("packId", "kind", "id", "revision"))
            and not ref["revision"].startswith("draft-")
        ):
            continue
        owner = ref["packId"]
        if not _is_builtin_reference(ref):
            owners.add(owner)
            continue
        if documents is None:
            continue
        _, row = project_packs._read_document(client, realm, project)
        if row is None or row["content"]["pack_id"] != owner:
            _, row = project_packs._read_document(client, realm, project, owner)
        if row is not None and row["content"]["pack_id"] == owner:
            owners.add(owner)
    return owners


def _clip_contexts(
    config: Mapping[str, Any], reference: Mapping[str, str]
) -> tuple[str, ...]:
    contexts = []
    for index, clip in enumerate(config.get("clips", [])):
        ref = clip.get("elementRef") if isinstance(clip, Mapping) else None
        if not isinstance(ref, Mapping):
            continue
        if not all(ref.get(key) == reference[key] for key in ("packId", "kind", "id", "revision")):
            continue
        context = f"$.clips[{index}]"
        clip_id = clip.get("id")
        if isinstance(clip_id, str) and clip_id:
            context += f" (id={clip_id!r})"
        contexts.append(context)
    return tuple(contexts)


def _snapshot_label(config: Mapping[str, Any], reference: Mapping[str, str]) -> str:
    label = (
        f"{reference['packId']}/{reference['kind']}/"
        f"{reference['id']}@{reference['revision']}"
    )
    contexts = _clip_contexts(config, reference)
    if contexts:
        label += f" for clip {'; '.join(contexts)}"
    return label


def require_project_snapshots(config: Mapping[str, Any]) -> None:
    selected = {selection_key(row["reference"]) for row in selections(config)}
    for key, ref in _project_references(config).items():
        if key not in selected:
            raise ValueError(f"Project element snapshot missing for {_snapshot_label(config, ref)}")


def resolve_project_elements(config: Mapping[str, Any], *, project: str, client: Any) -> dict[str, Any]:
    """Fetch each exact admission once; never use authoring files or the catalog as source.

    The mutable document locates a retained admission, selected by the complete
    element ref. The installed catalog only identifies genuine built-in refs;
    scoped verified-document/object ports authenticate project-pack source,
    which then travels with this render's frozen inputs.
    """
    result = copy.deepcopy(dict(config))
    app = result.get("app")
    if isinstance(app, dict):
        app.pop(CARRIER, None)  # Authored data cannot nominate executable inputs.
    document_service = getattr(client, "documents", None)
    realm = document_service._identity()["realm_id"] if document_service is not None else None
    refs = _project_references(
        result,
        project_pack_owners=_project_pack_owners(
            result, client=client, realm=realm, project=project,
        ),
    )
    if not refs:
        return result
    if realm is None:
        raise ValueError("Project element snapshot missing for rendered project reference")

    reader = getattr(document_service, "_client", client)
    verified_documents = {}
    objects = {}
    selected = []
    for ref in refs.values():
        label = _snapshot_label(result, ref)
        owner = ref["packId"]
        if owner not in verified_documents:
            _, row = project_packs._read_document(client, realm, project)
            if row is None or row["content"]["pack_id"] != owner:
                _, row = project_packs._read_document(client, realm, project, owner)
            if row is None or row["content"]["pack_id"] != owner:
                raise ValueError(f"Project element snapshot missing for {label}")
            pin = {"document_id": row["document_id"], "version": row["version"],
                   "content_digest": project_packs._json_digest(row["content"])}
            try:
                verified_documents[owner] = reader.get_project_verified_document(project, pin)
            except Exception as exc:
                raise ValueError(f"Project element snapshot missing for {label}: {exc}") from exc
        verified = verified_documents[owner]
        admissions = verified["document"]["content"]["admissions"]
        matches = [row for row in admissions if all(row.get(key) == ref[key]
                   for key in ("id", "kind", "revision"))]
        if len(matches) != 1 or not matches[0].get("snapshot_object"):
            raise ValueError(f"Project element snapshot missing for {label}; re-admit pack")
        object_id = matches[0]["snapshot_object"]["object_id"]
        if (owner, object_id) not in objects:
            try:
                objects[owner, object_id] = reader.get_project_verified_document_object(
                    project, verified["pin"], object_id)
            except Exception as exc:
                raise ValueError(f"Project element snapshot missing for {label}: {exc}") from exc
        snapshot = objects[owner, object_id]
        try:
            descriptor = project_packs.admitted_element_snapshot_descriptor(
                realm, project, ref, verified, snapshot)
        except ValueError as exc:
            raise ValueError(f"Project element snapshot digest mismatch or invalid source for {label}: {exc}") from exc
        selected.append({"realm": realm, "project": project, "reference": ref,
                         "verified": verified, "snapshot": snapshot, "descriptor": descriptor})
    result.setdefault("app", {})[CARRIER] = selected
    return result


def stage_project_elements(
    config: Mapping[str, Any], root: Path, *, registry: ElementRegistry | None = None,
) -> tuple[ElementRegistry, list[dict[str, str]]]:
    """Recheck frozen bytes and stage separate, unmodified pack source trees."""
    require_project_snapshots(config)
    registry = registry if registry is not None else load_default_registry()
    roots = []
    for selected in selections(config):
        ref = selected["reference"]
        label = _snapshot_label(config, ref)
        try:
            descriptor = project_packs.admitted_element_snapshot_descriptor(
                selected["realm"], selected["project"], ref,
                selected["verified"], selected["snapshot"])
        except ValueError as exc:
            raise ValueError(
                f"Project element snapshot digest mismatch or invalid source for {label}: {exc}"
            ) from exc
        snapshot = selected["snapshot"]
        files, _ = project_packs._snapshot_object_resources(
            {key: snapshot[key] for key in ("object_id", "digest", "size", "media_type")},
            project_packs._decode_base64(snapshot["bytes_base64"]))
        key = render_key(selected)
        pack_root = root / key / ref["packId"]
        pack_root.mkdir(parents=True, exist_ok=True, mode=0o700)
        for name, data in files.items():
            path = pack_root / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(data)
        # Discover each closure independently: two revisions deliberately have
        # the same authored pack ID, which ordinary discovery deduplicates.
        elements = load_pack_elements(project_root=root, extra_pack_roots=(str(pack_root),))
        exact = [item for item in elements if item.id == ref["id"]
                 and item.kind == ref["kind"] + "s" and item.root.is_relative_to(pack_root)]
        if len(exact) != 1:
            raise ValueError(f"Staged project element snapshot is unavailable for {label}: {ref}")
        element = exact[0]
        metadata = {**element.metadata, "render_revision": descriptor["revision"],
                    "render_key": key, "render_import_scope": f"pack-{key}-elements"}
        registry.register(replace(element, metadata=metadata))
        roots.append({"root": str(pack_root), "scope": f"pack-{key}-elements"})
    return registry, roots
