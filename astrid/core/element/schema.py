"""Schema dataclasses for render/custom elements."""

from __future__ import annotations

import json
import posixpath
import re
from dataclasses import asdict, dataclass, field
from pathlib import Path, PurePosixPath
from typing import Any

# Single source of truth for element kinds lives in astrid.core.pack to avoid a
# circular import (element.__init__ would import pack.py during schema load).
from astrid.core.contracts.capability_schema import (
    validate_capability_text as _validate_capability_text,
)
from astrid.core.contracts.schema import (
    OUTPUT_MODES,
    PORT_REQUIRED_TYPES,
    CapabilityHandle,
    Output,
    OutputMode,
    Port,
    PortType,
    Provenance,
    SafetyDeclaration,
)
from astrid.core.pack import (
    ELEMENT_KIND_REGISTRY,
    ElementKind,
    ElementKindRegistry,
)
from astrid.core.pack import (
    ELEMENT_KINDS as ELEMENT_KINDS,
)
from astrid.core.pack.manifest import ManifestParseError, load_manifest_mapping

from .metadata import raise_on_problems as _raise_metadata_problems

REQUIRED_ELEMENT_FILES = ("element.yaml",)
ELEMENT_MANIFEST_NAMES = ("element.yaml", "element.yml", "element.json")
_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]*$")


class ElementValidationError(ValueError):
    """Raised when an element definition is invalid."""


@dataclass(frozen=True)
class ElementDependencies:
    js_packages: tuple[str, ...] = ()
    python_requirements: tuple[str, ...] = ()


@dataclass(frozen=True)
class ElementAsset:
    name: str
    path: Path


@dataclass(frozen=True)
class ElementDefinition:
    id: str
    kind: ElementKind
    root: Path
    source: str
    editable: bool
    priority: int
    component: Path
    schema: dict[str, Any]
    defaults: dict[str, Any]
    metadata: dict[str, Any] = field(default_factory=dict)
    dependencies: ElementDependencies = field(default_factory=ElementDependencies)
    description: str = ""
    short_description: str = ""
    keywords: tuple[str, ...] = ()
    inputs: tuple[Port, ...] = ()
    outputs: tuple[Output, ...] = ()
    runtime: dict[str, Any] = field(default_factory=dict)
    assets: tuple[ElementAsset, ...] = ()
    support_files: tuple[Path, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data["root"] = str(self.root)
        data["component"] = str(self.component)
        if self.support_files:
            data["support_files"] = [str(path) for path in self.support_files]
        else:
            data.pop("support_files", None)
        if self.assets:
            data["assets"] = {asset.name: asset.path.as_posix() for asset in self.assets}
        else:
            data.pop("assets", None)
        return data

    def to_json(self, *, indent: int | None = 2) -> str:
        return json.dumps(self.to_dict(), indent=indent, sort_keys=True)


def to_capability_handle(definition: ElementDefinition) -> CapabilityHandle:
    """Adapt an ``ElementDefinition`` into a ``CapabilityHandle``.

    Field mapping:

    * ``canonical_id`` — ``"<kind>/<id>"`` slash-separator display string
      (e.g. ``"effects/blur"``)
    * ``local_id`` — ``definition.id``
    * ``pack_id`` — from ``metadata.pack_id`` when available, else ``""``
    * ``kind`` — ``definition.kind`` (e.g. ``"effects"``, ``"animations"``)
    * ``name`` — ``metadata.name`` or ``metadata.label`` or ``definition.id``
    * ``version`` — ``metadata.version`` (default ``""``)
    * ``provenance.source`` — ``definition.source`` preserved as-is
      (for example ``"pack:builtin"`` or ``"pack:local"``)
    * ``safety.network`` — ``False`` (elements have no network isolation)
    """
    metadata = definition.metadata

    name = str(metadata.get("name") or metadata.get("label") or definition.id)
    version = str(metadata.get("version") or "")
    pack_id = str(metadata.get("pack_id") or "")

    canonical_id = f"{definition.kind}/{definition.id}"

    return CapabilityHandle(
        canonical_id=canonical_id,
        local_id=definition.id,
        pack_id=pack_id,
        kind=definition.kind,
        name=name,
        version=version,
        provenance=Provenance(
            source=definition.source,
        ),
        safety=SafetyDeclaration(network=False),
        description=definition.description,
        short_description=definition.short_description,
        keywords=definition.keywords,
        inputs=definition.inputs,
        outputs=definition.outputs,
    )


def load_element_definition(
    root: str | Path,
    *,
    kind: str,
    source: str,
    editable: bool,
    priority: int,
    manifest_path: str | Path | None = None,
    element_kind_registry: ElementKindRegistry | None = None,
) -> ElementDefinition:
    element_root = Path(root)
    folder_kind = _normalize_kind(kind, element_kind_registry=element_kind_registry)
    if manifest_path is None:
        manifest_path = _element_manifest_path(element_root)
    else:
        manifest_path = Path(manifest_path).resolve()
        if manifest_path.parent != element_root.resolve() or not manifest_path.is_file():
            raise ElementValidationError(f"element manifest must be a regular file in {element_root}")
    if manifest_path is None:
        raise ElementValidationError(f"missing element manifest in {element_root}")
    payload = _read_manifest(manifest_path)
    definition = element_definition_from_payload(
        payload, root=element_root.resolve(), kind=folder_kind, source=source,
        editable=editable, priority=priority, manifest_path=manifest_path,
        element_kind_registry=element_kind_registry,
    )
    return definition


def element_definition_from_payload(
    payload, *, root, kind, source, editable, priority, manifest_path,
    files=None, element_kind_registry=None,
):
    """Parse the same declaration from a bounded immutable resource snapshot.

    ``files`` contains pack-relative resource names; when supplied this path
    performs no filesystem reads. Admission remains responsible for the tree.
    """
    element_root = root
    folder_kind = _normalize_kind(kind, element_kind_registry=element_kind_registry)
    element_id = str(payload.get("id") or element_root.name)
    declared_kind = payload.get("kind")
    if declared_kind is not None:
        normalized = _normalize_kind(
            str(declared_kind),
            element_kind_registry=element_kind_registry,
        )
        if normalized != folder_kind:
            raise ElementValidationError(
                f"element {element_id!r} declared kind {declared_kind!r} does not match folder kind {folder_kind!r}"
            )
    metadata_section = payload.get("metadata", {})
    if not isinstance(metadata_section, dict):
        raise ElementValidationError(f"{manifest_path}: metadata must be an object")
    metadata = dict(metadata_section)
    metadata.setdefault("id", element_id)
    pack_id = payload.get("pack_id")
    if pack_id is not None:
        metadata["pack_id"] = pack_id
    schema_section = payload.get("schema", {})
    defaults_section = payload.get("defaults", {})
    if not isinstance(schema_section, dict):
        raise ElementValidationError(f"{manifest_path}: schema must be an object")
    if not isinstance(defaults_section, dict):
        raise ElementValidationError(f"{manifest_path}: defaults must be an object")
    dependencies = _parse_dependencies(payload.get("dependencies", {}), path=f"{manifest_path}.dependencies")
    component = element_root / "component.tsx"
    if files is None:
        component = component.resolve()
    # component.tsx is optional when runtime.adapter is declared in the manifest
    _has_runtime_adapter = isinstance(payload.get("runtime"), dict) and bool(
        payload["runtime"].get("adapter")
    )
    if not _has_runtime_adapter and not (
            component.is_file() if files is None else component.as_posix() in files):
        raise ElementValidationError(f"element {element_id!r} missing component.tsx")
    description = _optional_capability_string(payload, "description", manifest_path)
    short_description = _optional_capability_string(payload, "short_description", manifest_path)
    keywords = _optional_capability_string_list(payload, "keywords", manifest_path)
    element_inputs = tuple(
        _parse_element_port(item, f"{manifest_path}.inputs[{index}]")
        for index, item in enumerate(payload.get("inputs") or ())
    )
    element_outputs = tuple(
        _parse_element_output(item, f"{manifest_path}.outputs[{index}]")
        for index, item in enumerate(payload.get("outputs") or ())
    )
    runtime = _parse_runtime(payload.get("runtime"), path=f"{manifest_path}.runtime")
    assets = _parse_assets(payload.get("assets"), element_root=element_root, path=f"{manifest_path}.assets", files=files)
    definition = ElementDefinition(
        id=element_id,
        kind=folder_kind,
        root=element_root,
        source=source,
        editable=editable,
        priority=priority,
        component=component,
        schema=dict(schema_section),
        defaults=dict(defaults_section),
        metadata=metadata,
        dependencies=dependencies,
        description=description,
        short_description=short_description,
        keywords=keywords,
        inputs=element_inputs,
        outputs=element_outputs,
        runtime=runtime,
        assets=assets,
    )
    return validate_element_definition(
        definition, element_kind_registry=element_kind_registry, files=files,
    )


def validate_element_definition(
    raw: ElementDefinition | dict[str, Any],
    *,
    element_kind_registry: ElementKindRegistry | None = None,
    files=None,
) -> ElementDefinition:
    if isinstance(raw, ElementDefinition):
        definition = raw
    else:
        definition = _parse_definition(raw)
    _validate_id(definition.id, "element.id")
    _validate_kind(
        definition.kind,
        element_kind_registry=element_kind_registry,
    )
    if files is None and not definition.root.is_dir():
        raise ElementValidationError(f"element root is not a directory: {definition.root}")
    # component.tsx is optional when runtime.adapter is declared
    if not definition.runtime.get("adapter") and not (
            (definition.root / "component.tsx").is_file() if files is None
            else (definition.root / "component.tsx").as_posix() in files):
        raise ElementValidationError(f"element {definition.id!r} missing component.tsx")
    if files is None:
        has_manifest = _element_manifest_path(definition.root) is not None
    else:
        has_manifest = any((definition.root / name).as_posix() in files
                           for name in ELEMENT_MANIFEST_NAMES)
    if not has_manifest:
        raise ElementValidationError(f"element {definition.id!r} missing element.yaml")
    if definition.metadata.get("id") not in (None, definition.id):
        raise ElementValidationError(f"element {definition.id!r} metadata.id does not match")
    if not isinstance(definition.schema, dict):
        raise ElementValidationError("element.schema must be an object")
    if not isinstance(definition.defaults, dict):
        raise ElementValidationError("element.defaults must be an object")
    if not isinstance(definition.metadata, dict):
        raise ElementValidationError("element.metadata must be an object")
    if not isinstance(definition.runtime, dict):
        raise ElementValidationError("element.runtime must be an object")
    _validate_assets(definition.assets, element_root=definition.root, path="element.assets", files=files)
    _validate_runtime_adapter(definition.runtime, f"{definition.kind}/{definition.id}")
    _validate_capability_text(
        definition.description,
        definition.short_description,
        definition.keywords,
        manifest_id=f"{definition.kind}/{definition.id}",
        error_cls=ElementValidationError,
    )
    _raise_metadata_problems(definition, error_cls=ElementValidationError)
    return definition


def _parse_definition(raw: dict[str, Any]) -> ElementDefinition:
    return ElementDefinition(
        id=str(raw["id"]),
        kind=_validate_kind(_normalize_kind(str(raw["kind"]))),
        root=Path(raw["root"]),
        source=str(raw["source"]),
        editable=bool(raw["editable"]),
        priority=int(raw["priority"]),
        component=Path(raw["component"]),
        schema=dict(raw.get("schema", {})),
        defaults=dict(raw.get("defaults", {})),
        metadata=dict(raw.get("metadata", {})),
        dependencies=_parse_dependencies(raw.get("dependencies", {}), path="element.dependencies"),
        description=str(raw.get("description", "") or ""),
        short_description=str(raw.get("short_description", "") or ""),
        keywords=tuple(raw.get("keywords", ()) or ()),
        inputs=tuple(raw.get("inputs", ()) or ()),
        outputs=tuple(raw.get("outputs", ()) or ()),
        runtime=_parse_runtime(raw.get("runtime"), path="element.runtime"),
        assets=_parse_assets(raw.get("assets"), element_root=Path(raw["root"]), path="element.assets"),
        support_files=tuple(Path(path) for path in raw.get("support_files", ())),
    )


def _element_manifest_path(root: Path) -> Path | None:
    for name in ELEMENT_MANIFEST_NAMES:
        candidate = root / name
        if candidate.is_file():
            return candidate
    return None


def _read_manifest(path: Path) -> dict[str, Any]:
    try:
        return load_manifest_mapping(path, manifest_kind="element")
    except ManifestParseError as exc:
        raise ElementValidationError(f"{path}: {exc}") from exc


def _parse_dependencies(raw: Any, *, path: str) -> ElementDependencies:
    if raw is None:
        return ElementDependencies()
    if not isinstance(raw, dict):
        raise ElementValidationError(f"{path} must be an object")
    return ElementDependencies(
        js_packages=tuple(_string_list(raw.get("js_packages", ()), path=f"{path}.js_packages")),
        python_requirements=tuple(_string_list(raw.get("python_requirements", ()), path=f"{path}.python_requirements")),
    )


def _parse_assets(raw: Any, *, element_root: Path, path: str, files=None) -> tuple[ElementAsset, ...]:
    if raw is None:
        return ()
    if not isinstance(raw, dict):
        raise ElementValidationError(f"{path} must be an object")
    parsed: list[ElementAsset] = []
    for name, value in sorted(raw.items()):
        asset_path = f"{path}.{name}"
        if not isinstance(name, str) or not name.strip():
            raise ElementValidationError(f"{path} keys must be non-empty strings")
        _validate_id(name, asset_path)
        if not isinstance(value, str) or not value.strip():
            raise ElementValidationError(f"{asset_path} must be a non-empty relative file path")
        parsed.append(
            ElementAsset(
                name=name,
                path=_normalize_asset_path(value, element_root=element_root, path=asset_path, files=files),
            )
        )
    return tuple(parsed)


def _validate_assets(assets: Any, *, element_root: Path, path: str, files=None) -> None:
    if not isinstance(assets, tuple):
        raise ElementValidationError(f"{path} must be an immutable tuple")
    names: set[str] = set()
    for index, asset in enumerate(assets):
        item_path = f"{path}[{index}]"
        if not isinstance(asset, ElementAsset):
            raise ElementValidationError(f"{item_path} must be an ElementAsset")
        _validate_id(asset.name, f"{item_path}.name")
        if asset.name in names:
            raise ElementValidationError(f"{item_path}.name duplicates asset {asset.name!r}")
        names.add(asset.name)
        _normalize_asset_path(asset.path, element_root=element_root, path=f"{item_path}.path", files=files)


def _normalize_asset_path(value: str | Path, *, element_root: Path, path: str, files=None) -> Path:
    if files is not None:
        portable_candidate = PurePosixPath(value)
        normalized = PurePosixPath(posixpath.normpath(portable_candidate.as_posix()))
        if portable_candidate.is_absolute() or normalized.parts[:1] == ('..',):
            raise ElementValidationError(f"{path} must stay inside the element root")
        if (element_root / normalized).as_posix() not in files:
            raise ElementValidationError(f"{path} file does not exist: {normalized}")
        return Path(normalized)
    candidate = Path(value)
    if candidate.is_absolute():
        raise ElementValidationError(f"{path} must be relative to the element root")
    if not candidate.parts or any(part == "" for part in candidate.parts):
        raise ElementValidationError(f"{path} must be a non-empty relative file path")
    root = element_root.resolve()
    resolved = (root / candidate).resolve()
    try:
        normalized_path = resolved.relative_to(root)
    except ValueError as exc:
        raise ElementValidationError(f"{path} must stay inside the element root") from exc
    if not resolved.is_file():
        raise ElementValidationError(f"{path} file does not exist: {normalized_path.as_posix()}")
    return normalized_path


def _string_list(raw: Any, *, path: str) -> list[str]:
    if raw in (None, ()):
        return []
    if not isinstance(raw, list) or not all(isinstance(item, str) and item.strip() for item in raw):
        raise ElementValidationError(f"{path} must be a list of non-empty strings")
    return list(raw)


def _normalize_kind(
    kind: str,
    *,
    element_kind_registry: ElementKindRegistry | None = None,
) -> ElementKind:
    registry = element_kind_registry or ELEMENT_KIND_REGISTRY
    try:
        return registry.normalize(kind, error_cls=ElementValidationError)
    except ElementValidationError as exc:
        raise ElementValidationError(f"{exc} (or singular variants)") from exc


def _validate_kind(
    kind: str,
    *,
    element_kind_registry: ElementKindRegistry | None = None,
) -> ElementKind:
    registry = element_kind_registry or ELEMENT_KIND_REGISTRY
    return registry.normalize(kind, error_cls=ElementValidationError)


def _optional_capability_string(payload: dict[str, Any], key: str, manifest_path: Path) -> str:
    value = payload.get(key)
    if value is None:
        return ""
    if not isinstance(value, str):
        raise ElementValidationError(f"{manifest_path}: {key} must be a string")
    return value


def _optional_capability_string_list(
    payload: dict[str, Any], key: str, manifest_path: Path
) -> tuple[str, ...]:
    raw = payload.get(key)
    if raw is None:
        return ()
    if not isinstance(raw, list) or not all(isinstance(item, str) for item in raw):
        raise ElementValidationError(f"{manifest_path}: {key} must be a list of strings")
    return tuple(raw)


# ---------------------------------------------------------------------------
# Inline port/output parsers for element manifest I/O
# ---------------------------------------------------------------------------


def _require_mapping(raw: Any, path: str) -> dict[str, Any]:
    if not isinstance(raw, dict):
        raise ElementValidationError(f"{path} must be an object")
    return raw


def _require_string(data: dict[str, Any], key: str, path: str) -> str:
    if key not in data:
        raise ElementValidationError(f"missing required field {path}")
    value = data[key]
    if not isinstance(value, str) or not value.strip():
        raise ElementValidationError(f"{path} must be a non-empty string")
    return value


def _optional_string(data: dict[str, Any], key: str, path: str, *, default: str = "") -> str:
    if key not in data or data[key] is None or data[key] == "":
        return default
    value = data[key]
    if not isinstance(value, str):
        raise ElementValidationError(f"{path} must be a string")
    return value


def _optional_nullable_string(data: dict[str, Any], key: str, path: str) -> str | None:
    if key not in data or data[key] is None:
        return None
    value = data[key]
    if not isinstance(value, str):
        raise ElementValidationError(f"{path} must be a string or null")
    return value


def _optional_bool(data: dict[str, Any], key: str, path: str, *, default: bool) -> bool:
    if key not in data or data[key] is None:
        return default
    value = data[key]
    if not isinstance(value, bool):
        raise ElementValidationError(f"{path} must be a boolean")
    return value


def _parse_element_port(raw: Any, path: str) -> Port:
    data = _require_mapping(raw, path)
    name = _require_string(data, "name", f"{path}.name")
    # Elements may declare custom port types (e.g. "clip") beyond the
    # executor-oriented PORT_REQUIRED_TYPES literal; accept any string.
    raw_type = data.get("type")
    port_type: Any = raw_type if isinstance(raw_type, str) and raw_type.strip() else "path"
    return Port(
        name=name,
        type=port_type,
        required=_optional_bool(data, "required", f"{path}.required", default=True),
        description=_optional_string(data, "description", f"{path}.description"),
        default=data.get("default"),
        placeholder=_optional_nullable_string(data, "placeholder", f"{path}.placeholder"),
        artifact_type=_optional_nullable_string(data, "artifact_type", f"{path}.artifact_type"),
    )


def _parse_element_output(raw: Any, path: str) -> Output:
    data = _require_mapping(raw, path)
    name = _require_string(data, "name", f"{path}.name")
    # Elements may declare custom port types (e.g. "clip") beyond the
    # executor-oriented PORT_REQUIRED_TYPES literal; accept any string.
    raw_type = data.get("type")
    port_type: Any = raw_type if isinstance(raw_type, str) and raw_type.strip() else "path"
    raw_mode = data.get("mode", "create_or_replace")
    port_mode: Any = raw_mode if isinstance(raw_mode, str) and raw_mode.strip() else "create_or_replace"
    return Output(
        name=name,
        type=port_type,
        mode=port_mode,
        description=_optional_string(data, "description", f"{path}.description"),
        placeholder=_optional_nullable_string(data, "placeholder", f"{path}.placeholder"),
        path_template=_optional_nullable_string(data, "path_template", f"{path}.path_template"),
        extension=_optional_nullable_string(data, "extension", f"{path}.extension"),
        artifact_type=_optional_nullable_string(data, "artifact_type", f"{path}.artifact_type"),
    )


def _parse_runtime(raw: Any, *, path: str) -> dict[str, Any]:
    """Parse and validate the optional ``runtime`` mapping.

    Returns an empty dict when *raw* is ``None`` or absent.
    When *raw* is a dict, validates that ``adapter`` (if present) is a
    non-empty string.
    """
    if raw is None:
        return {}
    if not isinstance(raw, dict):
        raise ElementValidationError(f"{path} must be an object")
    _validate_runtime_adapter(raw, path)
    return dict(raw)


def _validate_runtime_adapter(runtime: dict[str, Any], path: str) -> None:
    """Validate that ``runtime.adapter``, when present, is a non-empty string."""
    adapter = runtime.get("adapter")
    if adapter is not None:
        if not isinstance(adapter, str) or not adapter.strip():
            raise ElementValidationError(f"{path}.adapter must be a non-empty string")


def _validate_id(value: str, path: str) -> None:
    if not _ID_RE.match(value) or "/" in value or "\\" in value or value in {".", ".."}:
        raise ElementValidationError(f"{path} must be a safe non-empty identifier")
