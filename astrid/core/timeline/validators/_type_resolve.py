"""Type-resolution helper: bare clip_type string → canonical artifact_type id.

Implements the SD2 scan order: effects → animations → transitions.
Returns None for unresolved or unannotated elements (SD3 branch b/c).
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from astrid.core.contracts.artifact_types import ArtifactTypeRegistry
    from astrid.core.element.registry import ElementRegistry

_ELEMENT_KIND_SCAN_ORDER: tuple[str, ...] = ("effects", "animations", "transitions")


def resolve_clip_to_artifact_type(
    clip_type: str,
    theme: str | None,
    element_registry: "ElementRegistry",
    artifact_type_registry: "ArtifactTypeRegistry",
) -> str | None:
    """Resolve a bare clip_type string to its canonical artifact_type id.

    Scans element kinds effects → animations → transitions in order (SD2).
    For each kind, calls ``list_element_ids(kind)`` from the
    existing catalog, then fetches the ``ElementDefinition`` from
    *element_registry* and reads the first annotated output's
    ``artifact_type``.

    Returns ``None`` when the clip_type is not registered as any element kind
    or when the matched element has no annotated outputs (SD3 opaque
    fallthrough).
    """
    return _resolve_clip_to_artifact_type(
        clip_type,
        theme,
        element_registry,
        artifact_type_registry,
    )


def _resolve_clip_to_artifact_type(
    clip_type: str,
    theme: str | None,
    element_registry: "ElementRegistry",
    artifact_type_registry: "ArtifactTypeRegistry",
    *,
    membership_registry: "ElementRegistry | None" = None,
) -> str | None:
    """Resolve a clip using an optional already-admitted membership registry.

    Direct callers retain the catalog membership guard.  The default
    ``is_visual_clip_element`` path passes the default registry it has already
    loaded as both the type source and membership snapshot; that registry is
    the same authoritative source the catalog would assemble again.
    """
    from astrid.core.element import catalog as _catalog
    from astrid.core.element.schema import to_capability_handle

    for kind in _ELEMENT_KIND_SCAN_ORDER:
        try:
            if membership_registry is None:
                ids = _catalog.list_element_ids(kind)
            else:
                ids = [definition.id for definition in membership_registry.list(kind=kind)]
        except Exception:  # noqa: BLE001 - an optional pack must not break opaque fallthrough
            continue
        if clip_type not in ids:
            continue
        try:
            definition = element_registry.get(kind, clip_type)
        except (KeyError, Exception):
            continue
        definitions = getattr(element_registry, "_entries", {}).get((kind, clip_type), [definition])
        for candidate in definitions:
            handle = to_capability_handle(candidate)
            for output in handle.outputs:
                if output.artifact_type is not None:
                    return output.artifact_type
        if kind == "effects":
            return "clip/visual"
        return None  # element found but has no annotated output — opaque
    return None  # not found in any kind


def is_visual_clip_element(
    clip_type: str,
    theme: str | None,
    element_registry: "ElementRegistry | None" = None,
    artifact_type_registry: "ArtifactTypeRegistry | None" = None,
) -> bool:
    """Return True iff *clip_type* resolves to ``clip/visual``.

    Product validation uses the canonical default registries when callers do
    not provide explicit registries; this keeps registry setup in the one
    type-resolution implementation rather than a compatibility shim.
    """
    use_loaded_registry_for_membership = element_registry is None
    if element_registry is None:
        from astrid.core.element.registry import load_default_registry

        element_registry = load_default_registry()
    if artifact_type_registry is None:
        from astrid.core.contracts.artifact_types import ARTIFACT_TYPE_REGISTRY

        artifact_type_registry = ARTIFACT_TYPE_REGISTRY
    if use_loaded_registry_for_membership:
        resolved = _resolve_clip_to_artifact_type(
            clip_type,
            theme,
            element_registry,
            artifact_type_registry,
            membership_registry=element_registry,
        )
    else:
        resolved = resolve_clip_to_artifact_type(
            clip_type, theme, element_registry, artifact_type_registry
        )
    return resolved == "clip/visual"
