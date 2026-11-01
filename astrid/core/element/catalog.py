#!/usr/bin/env python3
"""Element catalog facade over the Astrid elements registry.

The registry is the authority for executable element definitions.  The small
descriptor projection below is deliberately data-only so editor hosts can
discover the same effects, animations, and transitions without importing
Python or re-implementing pack discovery.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
from functools import lru_cache
from pathlib import Path, PurePosixPath
from typing import Any

from astrid.core.element.registry import (
    ElementRegistry,
    clear_default_registry_cache,
    load_default_registry,
)
from astrid.core.element.schema import ElementKind
from astrid.core.foundation.paths import REPO_ROOT, WORKSPACE_ROOT

TOOLS_DIR = REPO_ROOT
EDITOR_CATALOG_VERSION = 1
EDITOR_CATALOG_KINDS = ("effects", "animations", "transitions")


def effects_root() -> Path:
    return element_root("effects")


def animations_root() -> Path:
    return element_root("animations")


def transitions_root() -> Path:
    return element_root("transitions")


def element_root(kind: ElementKind) -> Path:
    return WORKSPACE_ROOT / _validate_kind(kind)


def _validate_kind(kind: str) -> ElementKind:
    return _registry().element_kind_registry.normalize(kind)


def _registry(theme: str | Path | None = None, *, project_slug: str | None = None) -> ElementRegistry:
    # Timeline theme slugs are authoring metadata.  They cannot select
    # executable element definitions; only discovered Astrid packs do that.
    del theme
    return _cached_registry(
        project_slug,
        _path_cache_key(TOOLS_DIR),
    )


def _path_cache_key(path: str | Path | None) -> str | None:
    if path is None:
        return None
    return str(Path(path).resolve())


@lru_cache(maxsize=None)
def _cached_registry(
    project_slug: str | None,
    project_root_key: str | None,
) -> ElementRegistry:
    project_root = Path(project_root_key) if project_root_key is not None else TOOLS_DIR
    return load_default_registry(project_root=project_root)


def _clear_registry_cache() -> None:
    _cached_registry.cache_clear()
    clear_default_registry_cache()


def list_element_ids(
    kind: ElementKind,
    theme: str | Path | None = None,
    *,
    project_slug: str | None = None,
) -> list[str]:
    registry = _registry(theme, project_slug=project_slug)
    normalized_kind = registry.element_kind_registry.normalize(kind)
    return [element.id for element in registry.list(kind=normalized_kind)]


def list_element_descriptors(
    kind: ElementKind | None = None,
    *,
    project_slug: str | None = None,
    pack_id: str | None = None,
    include_shadowed: bool = False,
) -> tuple[dict[str, Any], ...]:
    """Return the stable editor-facing projection of the Astrid registry.

    This is intentionally narrower than :class:`ElementDefinition`: absolute
    filesystem paths and Python objects are not part of the host contract.
    ``scripts/gen_element_catalog.py`` serializes this projection into the
    Vite-importable TypeScript catalog consumed by Astrid App.
    """

    registry = _registry(project_slug=project_slug)
    normalized_kind = (
        registry.element_kind_registry.normalize(kind)
        if kind is not None
        else None
    )
    definitions = registry.list(kind=normalized_kind, pack_id=pack_id, include_shadowed=include_shadowed)
    if normalized_kind is None:
        definitions = tuple(
            definition
            for definition in definitions
            if definition.kind in EDITOR_CATALOG_KINDS
        )
    return tuple(_element_descriptor(definition) for definition in definitions)


def _element_descriptor(definition: Any, *, files=None) -> dict[str, Any]:
    metadata = definition.metadata
    pack_id = metadata.get("pack_id")
    if not isinstance(pack_id, str) or not pack_id:
        pack_id = (
            definition.source.split(":", 1)[1]
            if definition.source.startswith("pack:")
            else definition.source
        )
    label = metadata.get("label") or metadata.get("name") or definition.id
    if not isinstance(label, str):
        label = definition.id
    description = definition.description or definition.short_description or ""
    return {
        "id": definition.id,
        "kind": _singular_kind(definition.kind),
        "label": label,
        "description": description,
        "shortDescription": definition.short_description,
        "keywords": list(definition.keywords),
        "defaults": _json_value(definition.defaults),
        "schema": _json_value(definition.schema),
        "parameters": _parameter_descriptors(definition.schema, definition.defaults),
        "source": definition.source,
        "packId": pack_id,
        # This is a checkout-relative module path, not an absolute filesystem
        # path. Editor hosts can resolve it through their configured Astrid
        # source alias without making machine-local paths part of the catalog.
        "componentPath": _component_path(definition) if files is None else definition.component.as_posix(),
        "revision": _element_revision(definition, files=files),
        "runtime": _json_value(definition.runtime),
        "renderability": _renderability(definition, files=files),
    }


def _component_path(definition: Any) -> str:
    """Return the stable path used by source-backed editor hosts."""

    source_root = (REPO_ROOT / "astrid").resolve()
    try:
        return definition.component.resolve().relative_to(source_root).as_posix()
    except ValueError:
        # Registry extensions outside the checkout are still discoverable and
        # renderable by Astrid, but are not safe to import into a Vite host.
        return ""


def _singular_kind(kind: str) -> str:
    if kind.endswith("s"):
        return kind[:-1]
    return kind


def _json_value(value: Any) -> Any:
    """Copy JSON-shaped manifest data without leaking mutable registry state."""

    return json.loads(json.dumps(value, sort_keys=True))


def _parameter_descriptors(schema: Any, defaults: Any) -> list[dict[str, Any]]:
    """Project the JSON Schema subset understood by the editor inspector."""

    if not isinstance(schema, dict) or not isinstance(schema.get("properties"), dict):
        return []
    default_values = defaults if isinstance(defaults, dict) else {}
    parameters: list[dict[str, Any]] = []
    for name in sorted(schema["properties"]):
        property_schema = schema["properties"][name]
        if not isinstance(property_schema, dict):
            continue
        parameter_type = _parameter_type(property_schema)
        if parameter_type is None:
            continue
        parameter: dict[str, Any] = {
            "name": name,
            "label": _humanize(name),
            "description": str(property_schema.get("description") or ""),
            "type": parameter_type,
        }
        if name in default_values and isinstance(default_values[name], (bool, int, float, str)):
            parameter["default"] = default_values[name]
        if parameter_type == "number":
            for source, target in (("minimum", "min"), ("maximum", "max"), ("multipleOf", "step")):
                value = property_schema.get(source)
                if isinstance(value, (int, float)) and not isinstance(value, bool):
                    parameter[target] = value
        enum = property_schema.get("enum")
        if parameter_type == "select" and isinstance(enum, list):
            parameter["options"] = [
                {"label": _humanize(str(option)), "value": str(option)}
                for option in enum
                if isinstance(option, (str, int, float)) and not isinstance(option, bool)
            ]
        parameters.append(parameter)
    return parameters


def _parameter_type(schema: dict[str, Any]) -> str | None:
    schema_type = schema.get("type")
    if schema_type in ("number", "integer"):
        return "number"
    if schema_type == "boolean":
        return "boolean"
    if schema_type == "string":
        if isinstance(schema.get("enum"), list):
            return "select"
        pattern = schema.get("pattern")
        if isinstance(pattern, str) and "#" in pattern:
            return "color"
    return None


def _humanize(value: str) -> str:
    spaced = re.sub(r"(?<=[a-z0-9])(?=[A-Z])", " ", value)
    return spaced.replace("_", " ").replace("-", " ").strip().title()


def _element_revision(definition: Any, *, files=None) -> str:
    """Return a stable revision for the definition and its executable source."""

    digest = hashlib.sha256()
    payload = definition.to_dict()
    # Absolute source paths are machine-local and must not affect the public
    # revision. The component and asset contents are added below instead.
    payload.pop("root", None)
    payload.pop("component", None)
    payload.pop("assets", None)
    payload.pop("support_files", None)
    payload["asset_paths"] = {
        asset.name: asset.path.as_posix()
        for asset in sorted(definition.assets, key=lambda item: item.name)
    }
    digest.update(json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8"))
    digest.update(b"\0")
    if files is None:
        component = definition.component.read_bytes() if definition.component.is_file() else None
        helper_paths = set(definition.support_files)
        helper_paths.update(
            path.resolve()
            for path in definition.root.rglob("*")
            if path.suffix in {".ts", ".tsx", ".js", ".jsx", ".mjs", ".cjs", ".css"}
            and path.resolve() != definition.component
        )
        helpers = [(Path(os.path.relpath(path, definition.root)).as_posix(), path.read_bytes())
                   for path in sorted(helper_paths, key=lambda path: path.parts)]
        assets = {asset.name: (definition.root / asset.path).read_bytes()
                  for asset in definition.assets if (definition.root / asset.path).is_file()}
    else:
        component = files.get(definition.component.as_posix())
        helpers = [(path.relative_to(definition.root).as_posix(), value)
                   for name, value in sorted(files.items(), key=lambda item: PurePosixPath(item[0]))
                   if (path := PurePosixPath(name)).is_relative_to(definition.root)
                   and path.suffix in {".ts", ".tsx", ".js", ".jsx", ".mjs", ".cjs", ".css"}
                   and path != definition.component]
        assets = {asset.name: files[(definition.root / asset.path).as_posix()]
                  for asset in definition.assets}
    if component is not None:
        digest.update(component)
    for relative, value in helpers:
        digest.update(relative.encode("utf-8"))
        digest.update(b"\0")
        digest.update(value)
        digest.update(b"\0")
    for asset in sorted(definition.assets, key=lambda item: item.name):
        digest.update(asset.name.encode("utf-8"))
        digest.update(b"\0")
        if asset.name in assets:
            digest.update(assets[asset.name])
        digest.update(b"\0")
    return f"sha256:{digest.hexdigest()}"


def _renderability(definition: Any, *, files=None) -> dict[str, str]:
    adapter = definition.runtime.get("adapter") if isinstance(definition.runtime, dict) else None
    supported = adapter == "remotion" and (definition.component.is_file() if files is None
                                           else definition.component.as_posix() in files)
    status = "supported" if supported else "unknown"
    return {
        "preview": status,
        "browserExport": status,
        "workerExport": status,
    }


def _element(
    element_id: str,
    *,
    kind: ElementKind,
    theme: str | Path | None = None,
    project_slug: str | None = None,
):
    return _registry(theme, project_slug=project_slug).get(kind, element_id)


def read_element_schema(
    element_id: str,
    *,
    kind: ElementKind,
    theme: str | Path | None = None,
) -> dict[str, Any]:
    return dict(_element(element_id, kind=kind, theme=theme).schema)


def read_element_meta(
    element_id: str,
    *,
    kind: ElementKind,
    theme: str | Path | None = None,
) -> dict[str, Any]:
    return dict(_element(element_id, kind=kind, theme=theme).metadata)


def read_element_defaults(
    element_id: str,
    *,
    kind: ElementKind,
    theme: str | Path | None = None,
) -> dict[str, Any]:
    return dict(_element(element_id, kind=kind, theme=theme).defaults)


def list_effect_ids(theme: str | Path | None = None) -> list[str]:
    return list_element_ids("effects", theme=theme)


def read_effect_schema(effect_id: str, theme: str | Path | None = None) -> dict[str, Any]:
    return read_element_schema(effect_id, kind="effects", theme=theme)


def read_effect_meta(effect_id: str, theme: str | Path | None = None) -> dict[str, Any]:
    return read_element_meta(effect_id, kind="effects", theme=theme)


def read_effect_defaults(effect_id: str, theme: str | Path | None = None) -> dict[str, Any]:
    return read_element_defaults(effect_id, kind="effects", theme=theme)


def list_animation_ids(theme: str | Path | None = None) -> list[str]:
    return list_element_ids("animations", theme=theme)


def read_animation_schema(animation_id: str, theme: str | Path | None = None) -> dict[str, Any]:
    return read_element_schema(animation_id, kind="animations", theme=theme)


def read_animation_meta(animation_id: str, theme: str | Path | None = None) -> dict[str, Any]:
    return read_element_meta(animation_id, kind="animations", theme=theme)


def read_animation_defaults(animation_id: str, theme: str | Path | None = None) -> dict[str, Any]:
    return read_element_defaults(animation_id, kind="animations", theme=theme)


def list_transition_ids(theme: str | Path | None = None) -> list[str]:
    return list_element_ids("transitions", theme=theme)


def read_transition_schema(transition_id: str, theme: str | Path | None = None) -> dict[str, Any]:
    return read_element_schema(transition_id, kind="transitions", theme=theme)


def read_transition_meta(transition_id: str, theme: str | Path | None = None) -> dict[str, Any]:
    return read_element_meta(transition_id, kind="transitions", theme=theme)


def read_transition_defaults(transition_id: str, theme: str | Path | None = None) -> dict[str, Any]:
    return read_element_defaults(transition_id, kind="transitions", theme=theme)
