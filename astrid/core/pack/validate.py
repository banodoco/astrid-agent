"""Static pack validation module.

Uses yaml.safe_load for author-facing YAML, validates each manifest against its
JSON Schema (v1), rejects unknown schema_version values, and normalizes errors
into file-specific builder-facing messages.

Validation is static: checks declared content roots, docs, STAGE.md,
runtime entrypoint files, and component manifests exist on disk without
importing run.py.

Layout contract validation lives in ``astrid.core.pack.validate_layout``.
First-party packs root validation lives in ``astrid.core.pack.validate_first_party``.
"""

from __future__ import annotations

import json as _json
import logging
import re as _re
from pathlib import Path
from typing import Any, Optional
from urllib.parse import unquote, urlsplit

import jsonschema
import yaml
from referencing import Registry, Resource

from astrid.core.pack import (
    PACK_ALIAS_KINDS,
    PackDefinition,
    PackValidationError,
    _normalize_pack_permissions,
    _optional_pack_aliases,
    _optional_pack_extensions,
    element_kind_registry_for_pack,
    find_component_manifest,
    iter_element_roots,
    iter_executor_roots,
    iter_orchestrator_roots,
    pack_manifest_path,
    pack_rendering_manifest_paths,
    pack_taxonomy_from_manifest,
    validate_content_id_in_pack,
    validate_element_pack_id,
)
from astrid.core.pack.alias_resolver import AliasResolutionError, AliasResolver
from astrid.core.pack.manifest import (
    ManifestParseError,
    load_manifest_mapping,
    load_manifest_payload,
    reconcile_runtime_module,
)
from astrid.core.pack.validate_first_party import (
    PackRootValidation,
    PackValidationReport,
    _parse_skill_frontmatter,
    is_first_party_packs_root_candidate,
    validate_first_party_packs_root,
    validate_first_party_packs_root_report,
    validate_pack_roots,
)
from astrid.core.pack.validate_layout import (
    CANONICAL_PACK_LAYOUT_RULES,
    CanonicalLayoutRule,
    LayoutExceptionClass,
    LayoutExceptionLifecycle,
    LayoutValidationIssue,
    PackLayoutException,
    parse_layout_exceptions,
)

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Known schema versions and their schema files
# ---------------------------------------------------------------------------

_SCHEMAS_ROOT = Path(__file__).resolve().parent / "schemas"
_RENDERING_SCHEMAS_ROOT = _SCHEMAS_ROOT.parent.parent / "rendering" / "schemas"
_REPO_ROOT = Path(__file__).resolve().parents[3]
_RENDERING_MANIFEST_KINDS = ("renderer", "planner", "finalizer")

_PACK_TAXONOMY_ENUMS: dict[str, tuple[str, ...]] = {
    "origin": ("unknown", "builtin", "external"),
    "install_tier": ("default", "core", "optional"),
    "pack_type": ("capability", "adapter"),
    "domain": (
        "general",
        "development",
        "editorial",
        "generation",
        "infrastructure",
        "integration",
        "media",
        "system",
        "timeline",
    ),
    "stability": ("stable", "experimental", "deprecated"),
    "support": ("project", "core", "community"),
}

KNOWN_SCHEMA_VERSIONS: dict[int, dict[str, Path]] = {
    1: {
        "pack": _SCHEMAS_ROOT / "v1" / "pack.json",
        "executor": _SCHEMAS_ROOT / "v1" / "executor.json",
        "orchestrator": _SCHEMAS_ROOT / "v1" / "orchestrator.json",
        "element": _SCHEMAS_ROOT / "v1" / "element.json",
        "renderer": _RENDERING_SCHEMAS_ROOT / "v1" / "renderer-manifest.json",
        "planner": _RENDERING_SCHEMAS_ROOT / "v1" / "planner-manifest.json",
        "finalizer": _RENDERING_SCHEMAS_ROOT / "v1" / "finalizer-manifest.json",
    },
    2: {
        "pack": _SCHEMAS_ROOT / "v2" / "pack.json",
    },
    3: {
        "pack": _SCHEMAS_ROOT / "v3" / "pack.json",
    },
}

KNOWN_VERSIONS_STR = ", ".join(str(v) for v in sorted(KNOWN_SCHEMA_VERSIONS))
V1_TRUST_BLOCK: dict[str, Any] = {
    "sandbox": "none",
    "runs_with_user_process_permissions": True,
    "permission_enforcement": "disclosure_only",
}


def _check_schema_version(version_value: Any, manifest_relpath: str) -> int:
    """Validate that schema_version is a known integer."""
    if not isinstance(version_value, int) and not (
        isinstance(version_value, float) and version_value == int(version_value)
    ):
        raise ValidationError(
            f"{manifest_relpath}: schema_version must be an integer, got "
            f"{type(version_value).__name__}"
        )
    version = int(version_value)
    if version not in KNOWN_SCHEMA_VERSIONS:
        raise ValidationError(
            f"{manifest_relpath}: unknown schema_version {version} (known: {KNOWN_VERSIONS_STR})"
        )
    return version


def _normalize_jsonschema_error(
    error: jsonschema.ValidationError,
    manifest_relpath: str,
    raw_data: dict[str, Any],
) -> str:
    """Convert a jsonschema ValidationError into a file-specific message."""
    # Build the field path from the error's absolute path
    path_parts: list[str] = list(error.absolute_path)
    field = ".".join(str(p) for p in path_parts) if path_parts else "<root>"

    prefix = f"{manifest_relpath}"

    # Special-case schema_version since we handle it separately upstream,
    # but jsonschema may still report it for missing/wrong-type.
    if path_parts == ["schema_version"]:
        if "schema_version" not in raw_data:
            return f"{prefix}: missing required field schema_version"
        return f"{prefix}: schema_version must be a known integer ({KNOWN_VERSIONS_STR})"

    message = error.message
    # Clean up verbose jsonschema messages
    if message and len(message) > 200:
        message = message[:200] + "..."

    if error.validator == "required":
        # error.validator_value is the full required array from the schema.
        # error.message names the actually missing property.
        # Extract the missing field name from the message.
        msg = error.message
        # Typical message: "'name' is a required property"
        m = _re.match(r"'([^']+)' is a required property", msg)
        if m:
            missing_field = m.group(1)
            if field == "<root>":
                return f"{prefix}: missing required field {missing_field}"
            return f"{prefix}: missing required field {field}.{missing_field}"
        # Fallback
        return f"{prefix}: missing required field(s) — {msg}"

    if error.validator == "additionalProperties":
        return f"{prefix}: unknown field(s) in {field}"

    if error.validator == "enum":
        allowed = error.validator_value
        actual = raw_data
        for p in path_parts:
            if isinstance(actual, dict):
                actual = actual.get(p)
            else:
                break
        return f"{prefix}: {field} must be one of {allowed}, got {actual!r}"

    if error.validator == "type":
        expected = error.validator_value
        actual_val = raw_data
        for p in path_parts:
            if isinstance(actual_val, dict):
                actual_val = actual_val.get(p)
            else:
                break
        actual_type = type(actual_val).__name__
        expected_str = expected if isinstance(expected, str) else ", ".join(expected)
        return f"{prefix}: {field} must be {expected_str}, got {actual_type}"

    if error.validator == "pattern":
        actual_val = raw_data
        for p in path_parts:
            if isinstance(actual_val, dict):
                actual_val = actual_val.get(p)
            else:
                break
        return f"{prefix}: {field} value {actual_val!r} does not match required pattern"

    return f"{prefix}: {field} — {message}"


class ValidationError(ValueError):
    """Raised when pack validation fails."""


class PackLayoutContractError(ValidationError):
    """Aggregate layout-contract failure with per-path detail."""

    def __init__(self, issues: list[LayoutValidationIssue]):
        self.issues = tuple(issues)
        count = len(issues)
        noun = "issue" if count == 1 else "issues"
        super().__init__(f"pack layout contract failed ({count} {noun})")

    def lines(self) -> list[str]:
        lines = [str(self)]
        for issue in self.issues:
            lines.append(f"{issue.path}: {issue.message}")
        return lines


class PackValidator:
    """Validates an external pack directory statically."""

    def __init__(self, pack_root: Path):
        self.pack_root = pack_root.resolve()
        self.errors: list[str] = []
        self.warnings: list[str] = []
        self._pack_data: Optional[dict[str, Any]] = None
        self._layout_issues: list[LayoutValidationIssue] = []
        self._layout_exceptions: list[PackLayoutException] = []
        self._schema_cache: dict[tuple, tuple[dict[str, Any], Registry]] = {}

    def validate(self) -> list[str]:
        """Run all validations. Returns list of error strings (empty = valid)."""
        self.errors = []
        self.warnings = []
        self._layout_issues = []
        self._layout_exceptions = []
        self._capability_locations: dict[str, str] = {}
        self._pack_capability_locations: dict[str, dict[str, str]] = {
            kind: {} for kind in PACK_ALIAS_KINDS
        }
        self._alias_targets: list[tuple[str, str, str]] = []
        self._pack_alias_resolvers: dict[str, AliasResolver] = {
            kind: AliasResolver() for kind in PACK_ALIAS_KINDS
        }
        self._pack_alias_targets: list[tuple[str, str, str, str]] = []

        if (self.pack_root / ".no-pack").exists():
            return self.errors

        pack_yaml = pack_manifest_path(self.pack_root)
        if pack_yaml is None:
            self.errors.append(
                f"{self._rel(self.pack_root)}: pack manifest not found "
                f"(pack.yaml, pack.yml, or pack.json)"
            )
            return self.errors

        # Parse pack.yaml
        pack_data = self._load_yaml(pack_yaml)
        if pack_data is None:
            return self.errors  # parse error already recorded
        self._pack_data = pack_data

        # Check schema_version and validate against JSON Schema
        version = self._validate_manifest(pack_data, "pack", self._rel(pack_yaml))
        if version is None:
            return self.errors  # schema_version error already recorded
        if version == 2:
            from astrid.core.pack.canonical import (
                CanonicalPackValidationError,
                validate_canonical_pack,
            )

            try:
                validate_canonical_pack(self.pack_root)
            except CanonicalPackValidationError as exc:
                self.errors.append(str(exc))
                return self.errors

        if version == 3:
            # Canonical v3 admission is the shared source of truth for the
            # closed manifest, role paths, containment, declared resources,
            # symlink rejection, and rendering descriptor identity.  Keep the
            # author-facing validator as a thin diagnostic wrapper around that
            # admission path; it never imports or executes pack code.
            from astrid.core.pack.canonical import (
                CanonicalPackValidationError,
                validate_canonical_pack,
            )

            try:
                validate_canonical_pack(self.pack_root)
            except CanonicalPackValidationError as exc:
                self.errors.append(str(exc))
                return self.errors

            self._validate_v3_documentation()
            self._validate_v3_contract_schemas()
            self._validate_layout_contract(version=version)
            self._flush_layout_issues()
            return self.errors

        self._validate_pack_taxonomy()

        # Validate content roots exist
        content = pack_data.get("content", {})
        if isinstance(content, dict):
            self._validate_content_roots(content)

        # Validate docs exist
        docs = pack_data.get("docs", {})
        if isinstance(docs, dict):
            self._validate_docs(docs)

        # Validate component manifests
        self._validate_components(content)
        self._validate_pack_aliases()
        self._validate_alias_targets()
        self._validate_layout_contract()
        self._flush_layout_issues()

        return self.errors

    def validate_component_manifest(
        self,
        manifest_path: str | Path,
        manifest_kind: str,
    ) -> dict[str, Any] | None:
        """Load and schema-validate one component manifest.

        This uses the same parsing and JSON Schema path as full pack validation,
        without requiring callers to validate a whole pack tree.
        """
        path = Path(manifest_path)
        data = self._load_yaml(path)
        if data is None:
            return None
        self._validate_manifest(data, manifest_kind, self._rel(path))
        return data

    def _load_yaml(
        self,
        path: Path,
        *,
        manifest_kind: str = "pack",
    ) -> Optional[dict[str, Any]]:
        """Load a YAML file with safe_load. Returns None on error."""
        rel = self._rel(path)
        try:
            data = load_manifest_mapping(path, manifest_kind=manifest_kind)
        except ManifestParseError as e:
            self.errors.append(f"{rel}: {e}")
            return None

        return data

    def _validate_manifest(
        self,
        data: dict[str, Any],
        manifest_kind: str,
        relpath: str,
    ) -> Optional[int]:
        """Validate a manifest dict against its JSON Schema.

        Returns the schema_version on success, None on failure.
        """
        # Pack and component manifests are schema-versioned. If a component
        # omits schema_version, validate against v1 so the schema reports the
        # same missing-field error direct JSON Schema validation would report.
        if "schema_version" not in data:
            if manifest_kind == "pack":
                self.errors.append(f"{relpath}: missing required field schema_version")
                return None
            version = 1
        else:
            try:
                version = _check_schema_version(data["schema_version"], relpath)
            except ValidationError as e:
                self.errors.append(str(e))
                return None
        if manifest_kind == "pack" and version not in {2, 3}:
            self.errors.append(
                f"{relpath}: pack schema_version must be exactly integer 2 or 3"
            )
            return None

        # Load and validate against JSON Schema
        schema_path = KNOWN_SCHEMA_VERSIONS[version].get(manifest_kind)
        if schema_path is None:
            self.errors.append(f"{relpath}: no schema for {manifest_kind} in version {version}")
            return None

        try:
            schema, registry = self._load_schema(schema_path, manifest_kind, version)
        except Exception as e:  # noqa: BLE001 - validator reports malformed third-party schemas
            self.errors.append(f"{relpath}: cannot load schema {schema_path} — {e}")
            return None

        validator_cls = jsonschema.validators.validator_for(schema)
        validator_cls.check_schema(schema)
        validator = validator_cls(schema, registry=registry)
        raw_errors = list(validator.iter_errors(data))
        raw_errors = self._filter_dynamic_element_kind_errors(
            raw_errors,
            data=data,
            manifest_kind=manifest_kind,
        )

        if raw_errors:
            # Take the first few errors to avoid overwhelming output
            for err in raw_errors[:5]:
                self.errors.append(_normalize_jsonschema_error(err, relpath, data))
            if len(raw_errors) > 5:
                self.errors.append(
                    f"{relpath}: ... and {len(raw_errors) - 5} more validation errors"
                )
            return None

        return version

    def _filter_dynamic_element_kind_errors(
        self,
        errors: list[jsonschema.ValidationError],
        *,
        data: dict[str, Any],
        manifest_kind: str,
    ) -> list[jsonschema.ValidationError]:
        if manifest_kind != "element" or self._pack_data is None:
            return errors
        kind_value = data.get("kind")
        if not isinstance(kind_value, str):
            return errors
        try:
            pack = self._pack_definition_for_discovery({})
            element_kind_registry_for_pack(pack).normalize(kind_value)
        except Exception:  # noqa: BLE001 - unknown pack element kinds are validation results
            return errors
        return [
            error
            for error in errors
            if not (error.validator == "enum" and list(error.absolute_path) == ["kind"])
        ]

    def _load_schema(
        self, schema_path: Path, manifest_kind: str, version: int
    ) -> tuple[dict[str, Any], Registry]:
        """Load a JSON Schema file and build a referencing.Registry.

        Returns (schema_dict, registry) for use with jsonschema validators.
        Cached per (manifest_kind, version).
        """
        schema_key = (manifest_kind, version)
        if schema_key in self._schema_cache:
            return self._schema_cache[schema_key]

        # Load the _defs.json first
        defs_path = schema_path.parent / "_defs.json"
        registry = Registry()
        if defs_path.is_file():
            with open(defs_path, "r", encoding="utf-8") as f:
                defs_schema = json_loads(f.read())
            registry = registry.with_resource("_defs.json", Resource.from_contents(defs_schema))

        # Load the schema
        with open(schema_path, "r", encoding="utf-8") as f:
            schema = json_loads(f.read())

        # Also register the schema itself if it has an $id
        schema_id = schema.get("$id")
        if schema_id:
            registry = registry.with_resource(schema_id, Resource.from_contents(schema))

        self._schema_cache[schema_key] = (schema, registry)

        return schema, registry

    def _validate_content_roots(self, content: dict[str, Any]) -> None:
        """Verify that declared content root directories exist.

        Missing ``executors``, ``orchestrators``, or ``elements`` directories
        are hard errors. Missing ``schemas`` or ``examples`` directories are
        warnings.
        """
        supported = {"executors", "orchestrators", "elements", "schemas", "examples", "docs"}
        for key in sorted(set(content) - supported):
            self.warnings.append(f"pack.yaml: unsupported content root {key!r}")
        _hard_roots = {"executors", "orchestrators", "elements"}
        for key in ("executors", "orchestrators", "elements", "schemas", "examples"):
            if key not in content:
                continue
            root_rel = content[key]
            if not isinstance(root_rel, str) or not root_rel.strip():
                continue
            root_path = self.pack_root / root_rel
            if not root_path.is_dir():
                if key in _hard_roots:
                    self.errors.append(
                        f"{self._rel(root_path)}/: declared content root {key!r} does not exist"
                    )
                else:
                    self.warnings.append(
                        f"{self._rel(root_path)}/: declared content root does not exist"
                    )

    def _validate_pack_taxonomy(self) -> None:
        if self._pack_data is None:
            return
        for field, allowed in _PACK_TAXONOMY_ENUMS.items():
            value = self._pack_data.get(field)
            if value is None or value == "":
                continue
            if not isinstance(value, str):
                continue
            if value not in allowed:
                self.errors.append(
                    f"pack.yaml: {field} must be one of {list(allowed)}, got {value!r}"
                )

    def _validate_docs(self, docs: dict[str, Any]) -> None:
        """Verify that declared doc files exist."""
        for doc_key, doc_rel in docs.items():
            if not isinstance(doc_rel, str) or not doc_rel.strip():
                continue
            doc_path = self.pack_root / doc_rel
            if not doc_path.is_file():
                self.warnings.append(f"{self._rel(doc_path)}: declared docs file not found")

    def _validate_components(self, content: dict[str, Any]) -> None:
        """Validate all component manifests declared via content roots."""
        if self._pack_data is None:
            return
        self._validate_discovered_components(content)

    def _pack_definition_for_discovery(self, content: dict[str, Any]) -> PackDefinition:
        data = self._pack_data or {}
        status = str(data.get("status") or "active")
        taxonomy = pack_taxonomy_from_manifest(data, status=status)
        return PackDefinition(
            id=str(data.get("id") or self.pack_root.name),
            name=str(data.get("name") or data.get("id") or self.pack_root.name),
            version=str(data.get("version") or ""),
            root=self.pack_root,
            manifest_path=self.pack_root / "pack.yaml",
            schema_version=data.get("schema_version"),
            description=str(data.get("description") or ""),
            status=status,
            visibility=str(data.get("visibility") or "visible"),
            content=dict(content),
            metadata=dict(data.get("metadata", {}))
            if isinstance(data.get("metadata", {}), dict)
            else {},
            agent=dict(data.get("agent", {})) if isinstance(data.get("agent", {}), dict) else {},
            aliases=_optional_pack_aliases(data.get("aliases"), path="pack.aliases"),
            permissions=_normalize_pack_permissions(data.get("permissions")),
            extensions=_optional_pack_extensions(data.get("extensions"), path="pack.extensions"),
            **taxonomy,
        )

    def _validate_discovered_components(self, content: dict[str, Any]) -> None:
        try:
            pack = self._pack_definition_for_discovery(content)
        except PackValidationError as exc:
            self.errors.append(f"pack.yaml: {exc}")
            return
        for comp_dir in iter_executor_roots(pack):
            manifest_path = find_component_manifest(comp_dir, "executor")
            if manifest_path is not None:
                self._validate_component_manifest_file(pack, comp_dir, manifest_path, "executor")
        for comp_dir in iter_orchestrator_roots(pack):
            manifest_path = find_component_manifest(comp_dir, "orchestrator")
            if manifest_path is not None:
                self._validate_component_manifest_file(
                    pack, comp_dir, manifest_path, "orchestrator"
                )
        for kind, elem_dir in iter_element_roots(pack):
            manifest_path = find_component_manifest(elem_dir, "element")
            if manifest_path is not None:
                self._validate_element_manifest_file(pack, kind, manifest_path)
        try:
            rendering_manifest_paths = pack_rendering_manifest_paths(pack)
        except PackValidationError as exc:
            self.errors.append(f"pack.yaml: {exc}")
            return
        for manifest_kind, manifest_paths in zip(
            _RENDERING_MANIFEST_KINDS,
            rendering_manifest_paths,
        ):
            for manifest_path in manifest_paths:
                self._validate_rendering_manifest_file(
                    pack,
                    manifest_path,
                    manifest_kind,
                )

    def _validate_rendering_manifest_file(
        self,
        pack: PackDefinition,
        manifest_path: Path,
        manifest_kind: str,
    ) -> None:
        data = self._load_yaml(manifest_path, manifest_kind=manifest_kind)
        if data is None:
            return

        rel = self._rel(manifest_path)
        version = self._validate_manifest(data, manifest_kind, rel)
        if version is None:
            return
        capability_id = data.get("id")
        if not isinstance(capability_id, str):
            return
        self._register_capability_id(capability_id, rel)
        self._pack_capability_locations[manifest_kind][capability_id] = rel
        try:
            validate_content_id_in_pack(
                capability_id,
                pack,
                content_type=manifest_kind,
            )
        except ValueError as exc:
            self.errors.append(f"{rel}: {exc}")

    def _validate_component_manifest_file(
        self,
        pack: PackDefinition,
        component_dir: Path,
        manifest_path: Path,
        manifest_kind: str,
    ) -> None:
        data = self._load_yaml(manifest_path)
        if data is None:
            return

        rel = self._rel(manifest_path)
        version = self._validate_manifest(data, manifest_kind, rel)
        if version is None:
            return
        component_id = data.get("id")
        if isinstance(component_id, str):
            self._register_capability_id(component_id, rel)
            if manifest_kind in self._pack_capability_locations:
                self._pack_capability_locations[manifest_kind][component_id] = rel
            self._register_aliases(data, rel)
            try:
                validate_content_id_in_pack(
                    component_id,
                    pack,
                    content_type=manifest_kind,
                )
            except ValueError as exc:
                self.errors.append(f"{rel}: {exc}")

        self._validate_runtime_entrypoints(component_dir, data, manifest_kind, rel)
        self._validate_runtime_definition(data, manifest_kind, rel)

        docs = data.get("docs", {})
        stage = docs.get("stage", "STAGE.md") if isinstance(docs, dict) else "STAGE.md"
        stage_path = component_dir / stage
        if not stage_path.is_file():
            self.warnings.append(f"{self._rel(stage_path)}: STAGE.md not found")

    def _validate_runtime_definition(
        self, data: dict[str, Any], manifest_kind: str, rel: str
    ) -> None:
        """Run the pack-tier runtime-reconciliation check after the JSON-Schema pass.

        The JSON Schema is permissive about shapes the runtime parser rejects
        (e.g. a manifest declaring its runtime module twice with conflicting
        values). That structural check lives in the pack tier
        (``reconcile_runtime_module``); the executor / orchestrator schemas call
        the same helper at registry-load time, so running it here keeps
        ``packs validate`` parity without importing those upper tiers.
        """
        if manifest_kind == "executor":
            if isinstance(data, dict) and isinstance(data.get("executors"), list):
                items = data["executors"]
            else:
                items = [data]
            for item in items:
                self._reconcile_runtime_module(item, "executor", rel)
        elif manifest_kind == "orchestrator":
            self._reconcile_runtime_module(data, "orchestrator", rel)

    def _reconcile_runtime_module(self, data: Any, component: str, rel: str) -> None:
        """Apply the pack-tier runtime-module reconciliation check to one manifest."""
        if not isinstance(data, dict):
            return
        metadata = data.get("metadata", {})
        if not isinstance(metadata, dict):
            return
        try:
            reconcile_runtime_module(data.get("runtime"), metadata, ValidationError, component)
        except ValidationError as exc:
            self.errors.append(f"{rel}: {exc}")

    def _validate_element_manifest_file(
        self,
        pack: PackDefinition,
        kind: str,
        manifest_path: Path,
    ) -> None:
        data = self._load_yaml(manifest_path)
        if data is None:
            return

        rel = self._rel(manifest_path)
        version = self._validate_manifest(data, "element", rel)
        if version is None:
            return
        element_id = data.get("id")
        if isinstance(element_id, str):
            self._register_capability_id(f"{kind}/{element_id}", rel)
            self._register_aliases(data, rel)
        try:
            validate_element_pack_id(
                data.get("pack_id"),
                pack,
                element_root=manifest_path.parent,
            )
        except ValueError as exc:
            self.errors.append(f"{rel}: {exc}")

    def _validate_runtime_entrypoints(
        self,
        component_dir: Path,
        data: dict[str, Any],
        manifest_kind: str,
        rel: str,
    ) -> None:
        if manifest_kind == "executor":
            self._check_runtime_entrypoint(component_dir, data.get("entrypoint"), "entrypoint")
            runtime = data.get("runtime", {})
            if isinstance(runtime, dict):
                self._check_runtime_entrypoint(
                    component_dir, runtime.get("entrypoint"), "runtime entrypoint"
                )
                self._check_command_entrypoint(component_dir, runtime.get("command"))
            self._check_command_entrypoint(component_dir, data.get("command"))
            self._check_metadata_runtime_file(component_dir, data)
            return

        if manifest_kind != "orchestrator":
            return

        runtime = data.get("runtime", {})
        if not isinstance(runtime, dict):
            return
        kind = runtime.get("kind")
        if kind == "python":
            self._check_python_module(component_dir, runtime.get("module"), rel, "runtime.module")
        elif kind == "command":
            self._check_command_entrypoint(component_dir, runtime.get("command"))

    def _check_runtime_entrypoint(self, component_dir: Path, value: Any, label: str) -> None:
        if not isinstance(value, str) or not value.strip():
            return
        if "{" in value or "}" in value:
            return
        entrypoint_path = component_dir / value
        if not entrypoint_path.is_file():
            self.errors.append(f"{self._rel(entrypoint_path)}: {label} file not found")

    def _check_metadata_runtime_file(self, component_dir: Path, data: dict[str, Any]) -> None:
        metadata = data.get("metadata", {})
        if not isinstance(metadata, dict):
            return
        self._check_runtime_entrypoint(
            component_dir, metadata.get("runtime_file"), "metadata.runtime_file"
        )
        self._check_python_module(
            component_dir,
            metadata.get("runtime_module"),
            self._rel(component_dir),
            "metadata.runtime_module",
        )

    def _check_command_entrypoint(self, component_dir: Path, command: Any) -> None:
        if isinstance(command, dict):
            argv = command.get("argv")
        else:
            argv = command
        if not isinstance(argv, list):
            return
        parts = [part for part in argv if isinstance(part, str)]
        for index, part in enumerate(parts):
            if part == "-m" and index + 1 < len(parts):
                self._check_python_module(
                    component_dir,
                    parts[index + 1],
                    self._rel(component_dir),
                    "command.argv module",
                )
                return
        for part in parts:
            if not self._looks_like_local_entrypoint(part):
                continue
            self._check_runtime_entrypoint(component_dir, part, "command.argv entrypoint")
            return

    def _looks_like_local_entrypoint(self, value: str) -> bool:
        if not value or value.startswith("-") or "{" in value or "}" in value:
            return False
        return value.endswith(".py") or "/" in value or "\\" in value

    def _check_python_module(
        self,
        component_dir: Path,
        module: Any,
        rel: str,
        label: str,
    ) -> None:
        if not isinstance(module, str) or not module.strip():
            return
        if "{" in module or "}" in module:
            return
        module_path = self._module_path(component_dir, module)
        if module_path is None:
            return
        if not module_path.is_file():
            self.errors.append(
                f"{rel}: {label} file not found for module {module!r}: {self._rel(module_path)}"
            )

    def _module_path(self, component_dir: Path, module: str) -> Path | None:
        parts = module.split(".")
        pack_id = self._pack_data.get("id") if self._pack_data is not None else None
        if (
            len(parts) >= 6
            and parts[0:2] == ["astrid", "packs"]
            and parts[2] == pack_id
            and parts[3] in {"executors", "orchestrators"}
            and parts[4] == component_dir.name
        ):
            return component_dir / Path(*parts[5:]).with_suffix(".py")
        if (
            len(parts) >= 4
            and parts[0] == pack_id
            and parts[1] in {"executors", "orchestrators"}
            and parts[2] == component_dir.name
        ):
            return self.pack_root / Path(*parts[1:]).with_suffix(".py")
        if (
            len(parts) >= 5
            and parts[0:2] == ["astrid", "packs"]
            and parts[2] == pack_id
            and parts[3] == component_dir.name
        ):
            return component_dir / Path(*parts[4:]).with_suffix(".py")
        if len(parts) >= 5 and parts[0:2] == ["astrid", "packs"]:
            pack_root = _REPO_ROOT / "astrid" / "packs" / parts[2]
            component_name = parts[3]
            tail = Path(*parts[4:]).with_suffix(".py")
            for kind_root in ("executors", "orchestrators"):
                candidate = pack_root / kind_root / component_name / tail
                if candidate.is_file():
                    return candidate
        if module.startswith("astrid."):
            return _REPO_ROOT / Path(*module.split(".")).with_suffix(".py")
        if "." not in module:
            return component_dir / f"{module}.py"
        return component_dir / Path(*module.split(".")).with_suffix(".py")

    def _register_capability_id(self, capability_id: str, relpath: str) -> None:
        existing = self._capability_locations.get(capability_id)
        if existing is not None:
            self.errors.append(
                f"{relpath}: duplicate capability id {capability_id!r}; already declared in {existing}"
            )
            return
        self._capability_locations[capability_id] = relpath

    def _register_aliases(self, data: dict[str, Any], relpath: str) -> None:
        metadata = data.get("metadata", {})
        if not isinstance(metadata, dict):
            return
        aliases = metadata.get("aliases", [])
        if not isinstance(aliases, list):
            self.errors.append(f"{relpath}: metadata.aliases must be an array")
            return
        for index, alias in enumerate(aliases):
            if isinstance(alias, str):
                self._alias_targets.append((relpath, f"metadata.aliases[{index}]", alias))
            elif isinstance(alias, dict):
                target = alias.get("canonical_id") or alias.get("target") or alias.get("id")
                if isinstance(target, str):
                    self._alias_targets.append((relpath, f"metadata.aliases[{index}]", target))
                else:
                    self.errors.append(
                        f"{relpath}: metadata.aliases[{index}] must declare canonical_id"
                    )
            else:
                self.errors.append(
                    f"{relpath}: metadata.aliases[{index}] must be a string or object"
                )

    def _validate_pack_aliases(self) -> None:
        if self._pack_data is None:
            return
        aliases = self._pack_data.get("aliases")
        if aliases is None:
            return
        try:
            normalized_aliases = _optional_pack_aliases(aliases, path="pack.aliases")
        except ValueError as exc:
            self.errors.append(f"pack.yaml: {exc}")
            return

        for index, alias in enumerate(normalized_aliases):
            kind = str(alias["kind"])
            alias_id = str(alias["alias"])
            canonical_id = str(alias["canonical_id"])
            resolver = self._pack_alias_resolvers[kind]
            if resolver.is_alias(alias_id):
                self.errors.append(
                    f"pack.yaml: pack.aliases[{index}] duplicates existing {kind} alias {alias_id!r}"
                )
                continue
            try:
                resolver.register_alias(
                    alias_id,
                    canonical_id,
                    deprecated=bool(alias.get("deprecated", False)),
                    deprecation_message=str(alias.get("deprecation_message", "")),
                )
            except AliasResolutionError as exc:
                self.errors.append(f"pack.yaml: pack.aliases[{index}] {exc}")
                continue
            self._pack_alias_targets.append(
                ("pack.yaml", f"pack.aliases[{index}]", kind, canonical_id)
            )

        for relpath, alias_path, kind, target in self._pack_alias_targets:
            resolved_target = self._pack_alias_resolvers[kind].resolve(target)
            pack_id = resolved_target.split(".", 1)[0]
            if pack_id != self._pack_id():
                continue
            if resolved_target not in self._pack_capability_locations[kind]:
                self.errors.append(
                    f"{relpath}: {alias_path} points to unknown {kind} id {resolved_target!r}"
                )

    def _validate_alias_targets(self) -> None:
        for relpath, alias_path, target in self._alias_targets:
            if target not in self._capability_locations:
                self.errors.append(
                    f"{relpath}: {alias_path} points to unknown capability id {target!r}"
                )

    # -----------------------------------------------------------------------
    # Canonical v3 author checks
    # -----------------------------------------------------------------------

    def _validate_v3_documentation(self) -> None:
        """Validate the authored metadata of the one declared pack skill."""
        if self._pack_data is None:
            return
        documentation = self._pack_data.get("documentation")
        if not isinstance(documentation, dict) or documentation.get("kind") != "skill":
            return

        path_value = documentation.get("path")
        if not isinstance(path_value, str) or not path_value.strip():
            return
        skill_path = self.pack_root / path_value
        rel = self._rel(skill_path)
        try:
            text = skill_path.read_text(encoding="utf-8")
        except OSError as exc:
            # Canonical admission normally reports this first. Keep this
            # branch useful for callers that invoke the helper directly.
            self.errors.append(f"{rel}: cannot read authored skill: {exc}")
            return

        frontmatter, parse_error = self._parse_skill_frontmatter(text)
        if parse_error is not None:
            self.errors.append(f"{rel}: {parse_error}")
            return
        for field in ("name", "description"):
            value = frontmatter.get(field)
            if not isinstance(value, str) or not value.strip():
                self.errors.append(
                    f"{rel}: frontmatter {field} must be a non-empty string"
                )

    @staticmethod
    def _parse_skill_frontmatter(text: str) -> tuple[dict[str, Any], str | None]:
        """Parse the small YAML frontmatter contract used by skill bundles."""
        return _parse_skill_frontmatter(text)

    def _validate_v3_contract_schemas(self) -> None:
        """Check inline and local JSON Schema contracts without resolving imports."""
        if self._pack_data is None:
            return

        for key, action in self._mapping_items("actions"):
            for slot in ("inputs", "outputs"):
                value = action.get(slot)
                self._validate_contract_schema_value(
                    value,
                    location=f"actions.{key}.{slot}",
                )

        for key, document in self._mapping_items("documents"):
            self._validate_contract_schema_value(
                document.get("schema"),
                location=f"documents.{key}.schema",
            )

    def _mapping_items(self, section: str) -> list[tuple[str, dict[str, Any]]]:
        if self._pack_data is None:
            return []
        value = self._pack_data.get(section, {})
        if not isinstance(value, dict):
            return []
        return [
            (str(key), item)
            for key, item in value.items()
            if isinstance(item, dict)
        ]

    def _validate_contract_schema_value(self, value: Any, *, location: str) -> None:
        if isinstance(value, str):
            path = self.pack_root / value
            try:
                payload = load_manifest_payload(path, manifest_kind="JSON Schema")
            except ManifestParseError as exc:
                self.errors.append(f"{location}: {exc}")
                return
            if not isinstance(payload, (dict, bool)):
                self.errors.append(
                    f"{self._rel(path)}: JSON Schema must be an object or boolean"
                )
                return
            self._check_json_schema(
                payload,
                location=f"{location} ({self._rel(path)})",
                source_path=path,
                seen_paths=set(),
            )
            return

        if isinstance(value, dict):
            self._check_json_schema(
                value,
                location=location,
                source_path=None,
                seen_paths=set(),
            )
        # Booleans are valid JSON Schema values. Port arrays are validated by
        # the v3 manifest schema and are intentionally not interpreted here.

    def _check_json_schema(
        self,
        schema: dict[str, Any] | bool,
        *,
        location: str,
        source_path: Path | None,
        seen_paths: set[Path],
    ) -> None:
        try:
            validator_cls = jsonschema.validators.validator_for(schema)
            validator_cls.check_schema(schema)
        except jsonschema.exceptions.SchemaError as exc:
            message = getattr(exc, "message", str(exc))
            self.errors.append(f"{location}: invalid JSON Schema: {message}")
            return

        self._check_schema_references(
            schema,
            location=location,
            source_path=source_path,
            seen_paths=seen_paths,
            root_schema=schema,
        )

    def _check_schema_references(
        self,
        schema: Any,
        *,
        location: str,
        source_path: Path | None,
        seen_paths: set[Path],
        root_schema: dict[str, Any] | bool,
    ) -> None:
        if isinstance(schema, dict):
            ref = schema.get("$ref")
            if ref is not None:
                if not isinstance(ref, str) or not ref:
                    self.errors.append(f"{location}: $ref must be a non-empty string")
                else:
                    self._check_schema_reference(
                        ref,
                        location=f"{location}.$ref",
                        source_path=source_path,
                        root_schema=root_schema,
                        seen_paths=seen_paths,
                    )
            for key, child in schema.items():
                if key != "$ref":
                    self._check_schema_references(
                        child,
                        location=f"{location}.{key}",
                        source_path=source_path,
                        seen_paths=seen_paths,
                        root_schema=root_schema,
                    )
        elif isinstance(schema, list):
            for index, child in enumerate(schema):
                self._check_schema_references(
                    child,
                    location=f"{location}[{index}]",
                    source_path=source_path,
                    seen_paths=seen_paths,
                    root_schema=root_schema,
                )

    def _check_schema_reference(
        self,
        reference: str,
        *,
        location: str,
        source_path: Path | None,
        root_schema: dict[str, Any] | bool,
        seen_paths: set[Path],
    ) -> None:
        parsed = urlsplit(reference)
        if parsed.scheme or parsed.netloc:
            self.errors.append(
                f"{location}: external JSON Schema references are not resolved by static validation"
            )
            return

        fragment = parsed.fragment
        if not parsed.path:
            if fragment and not self._schema_pointer_exists(root_schema, fragment):
                self.errors.append(f"{location}: local JSON Schema reference {reference!r} not found")
            return

        if source_path is None:
            self.errors.append(
                f"{location}: inline JSON Schema cannot resolve local file reference {reference!r}"
            )
            return

        candidate = (source_path.parent / unquote(parsed.path)).resolve(strict=False)
        try:
            relative = candidate.relative_to(self.pack_root)
        except ValueError:
            self.errors.append(f"{location}: JSON Schema reference escapes the pack root")
            return
        if not candidate.is_file():
            self.errors.append(
                f"{location}: referenced JSON Schema file not found: {relative.as_posix()}"
            )
            return

        try:
            payload = load_manifest_payload(candidate, manifest_kind="JSON Schema")
        except ManifestParseError as exc:
            self.errors.append(f"{location}: {exc}")
            return
        if not isinstance(payload, (dict, bool)):
            self.errors.append(
                f"{relative.as_posix()}: referenced JSON Schema must be an object or boolean"
            )
            return
        if fragment and not self._schema_pointer_exists(payload, fragment):
            self.errors.append(
                f"{location}: JSON Schema reference {reference!r} not found in {relative.as_posix()}"
            )
            return
        if candidate in seen_paths:
            return
        seen_paths.add(candidate)
        self._check_json_schema(
            payload,
            location=f"{location} ({relative.as_posix()})",
            source_path=candidate,
            seen_paths=seen_paths,
        )

    @staticmethod
    def _schema_pointer_exists(document: Any, fragment: str) -> bool:
        if not fragment:
            return True
        if not fragment.startswith("/"):
            return False
        current = document
        for token in fragment[1:].split("/"):
            token = token.replace("~1", "/").replace("~0", "~")
            if isinstance(current, dict) and token in current:
                current = current[token]
            elif isinstance(current, list) and token.isdigit() and int(token) < len(current):
                current = current[int(token)]
            else:
                return False
        return True

    # -----------------------------------------------------------------------
    # Layout contract validation (delegates to validate_layout module)
    # -----------------------------------------------------------------------

    def _validate_layout_contract(self, *, version: int | None = None) -> None:
        """Validate the pack directory layout against the canonical contract."""
        if self._pack_data is None:
            return
        self._layout_exceptions, issues = parse_layout_exceptions(self._pack_data)
        self._layout_issues.extend(issues)
        if version == 3:
            from astrid.core.pack.validate_layout import validate_pack_layout

            self._layout_issues.extend(
                validate_pack_layout(self.pack_root, self._pack_data)
            )

    def _flush_layout_issues(self) -> None:
        """Surface any collected layout validation issues as errors."""
        if not self._layout_issues:
            return
        aggregate = PackLayoutContractError(self._layout_issues)
        self.errors.extend(aggregate.lines())
        self._layout_issues = []

    def _pack_id(self) -> str:
        if self._pack_data is None:
            return self.pack_root.name
        value = self._pack_data.get("id")
        if isinstance(value, str) and value.strip():
            return value
        return self.pack_root.name

    def _rel(self, path: Path) -> str:
        """Return a path relative to the pack root for error messages."""
        try:
            return str(path.relative_to(self.pack_root))
        except ValueError:
            return str(path)


def validate_pack(pack_root: str | Path) -> tuple[list[str], list[str]]:
    """Validate an external pack directory.

    Args:
        pack_root: Path to the pack root directory.

    Returns:
        A tuple of (errors, warnings). Empty errors list means valid.
    """
    validator = PackValidator(Path(pack_root))
    errors = validator.validate()
    return errors, validator.warnings


def json_loads(text: str) -> Any:
    """Load JSON, wrapping decode errors for consistent messaging."""
    return _json.loads(text)


def _check_semantic_secrets(data: dict[str, Any]) -> list[str]:
    warnings: list[str] = []
    secrets_raw = data.get("secrets")
    if not isinstance(secrets_raw, list):
        return warnings
    for idx, item in enumerate(secrets_raw):
        if not isinstance(item, dict):
            warnings.append(f"secrets[{idx}]: not a mapping, skipping")
            continue
        name = item.get("name")
        if not isinstance(name, str) or not name.strip():
            warnings.append(f"secrets[{idx}]: empty or missing secret name")
            continue
        if not item.get("required", False):
            description = item.get("description")
            if not isinstance(description, str) or not description.strip():
                warnings.append(f"secret '{name.strip()}': optional secret has no description")
    return warnings


def _check_semantic_deps(data: dict[str, Any]) -> list[str]:
    warnings: list[str] = []
    deps = data.get("dependencies")
    if not isinstance(deps, dict):
        return warnings
    python_deps = deps.get("python")
    if isinstance(python_deps, list):
        for idx, dep in enumerate(python_deps):
            if not isinstance(dep, str) or not dep.strip():
                warnings.append(f"dependencies.python[{idx}]: empty entry")
            elif not _re.match(
                r"^[A-Za-z0-9_.-]+(\s*[><=!~]+\s*[A-Za-z0-9_.*-]+)*(\s*;\s*.*)?$", dep.strip()
            ):
                warnings.append(
                    f"dependencies.python[{idx}]: '{dep}' does not look like a pip requirement"
                )
    npm_deps = deps.get("npm")
    if isinstance(npm_deps, list):
        for idx, dep in enumerate(npm_deps):
            if not isinstance(dep, str) or not dep.strip():
                warnings.append(f"dependencies.npm[{idx}]: empty entry")
            elif not _re.match(
                r"^@?[A-Za-z0-9_.-]+(/[A-Za-z0-9_.-]+)?(@[A-Za-z0-9_.-]+)?$", dep.strip()
            ):
                warnings.append(
                    f"dependencies.npm[{idx}]: '{dep}' does not look like an npm package specifier"
                )
    system_deps = deps.get("system")
    if isinstance(system_deps, list):
        for idx, dep in enumerate(system_deps):
            if not isinstance(dep, str) or not dep.strip():
                warnings.append(f"dependencies.system[{idx}]: empty entry")
            elif not _re.match(r"^[A-Za-z0-9_.-]+$", dep.strip()):
                warnings.append(
                    f"dependencies.system[{idx}]: '{dep}' does not look like a system command name"
                )
    return warnings


def extract_trust_summary(pack_root: str | Path) -> dict[str, Any]:
    """Extract a lightweight trust summary for pack install dry-runs."""
    root = Path(pack_root).resolve()
    manifest_path = pack_manifest_path(root)
    if manifest_path is None:
        raise ValidationError(f"No pack manifest found in {root}")

    try:
        data = load_manifest_mapping(manifest_path, manifest_kind="pack")
    except ManifestParseError as exc:
        raise ValidationError(f"Failed to parse {manifest_path}: {exc}") from exc

    pack_id = data.get("id", root.name)
    name = data.get("name", pack_id)
    version = data.get("version", "0.0.0")
    schema_version = data.get("schema_version", "unknown")

    content = data.get("content", {}) if isinstance(data.get("content"), dict) else {}
    component_counts: dict[str, int] = {}
    for key in ("executors", "orchestrators", "elements"):
        comp_root_rel = content.get(key)
        if isinstance(comp_root_rel, str) and comp_root_rel.strip():
            comp_dir = root / comp_root_rel
            component_counts[key] = (
                sum(
                    1
                    for child in comp_dir.iterdir()
                    if child.is_dir() and not child.name.startswith(".")
                )
                if comp_dir.is_dir()
                else 0
            )
        else:
            component_counts[key] = 0

    agent = data.get("agent", {}) if isinstance(data.get("agent"), dict) else {}
    normal_entrypoints = (
        [str(ep) for ep in agent.get("normal_entrypoints", []) if ep]
        if isinstance(agent.get("normal_entrypoints"), list)
        else []
    )
    display_entrypoints = normal_entrypoints

    secrets_raw = data.get("secrets")
    secrets_list: list[str] = []
    if isinstance(secrets_raw, list):
        for item in secrets_raw:
            if isinstance(item, dict) and item.get("name"):
                label = str(item["name"])
                if item.get("required"):
                    label += " (required)"
                description = item.get("description")
                if description:
                    label += f": {description}"
                secrets_list.append(label)
    elif isinstance(secrets_raw, dict) and isinstance(secrets_raw.get("required"), list):
        secrets_list = [str(secret) for secret in secrets_raw["required"] if secret]

    deps_raw = data.get("dependencies", {}) if isinstance(data.get("dependencies"), dict) else {}
    dependencies: list[str] = []
    dependencies_struct: dict[str, list[str]] = {}
    for ecosystem in ("python", "npm", "system"):
        values = deps_raw.get(ecosystem) if isinstance(deps_raw, dict) else None
        if isinstance(values, list):
            clean = [str(value) for value in values if value]
            dependencies_struct[ecosystem] = clean
            dependencies.extend(f"{ecosystem}:{value}" for value in clean)
    if isinstance(deps_raw.get("packs"), list):
        for value in deps_raw["packs"]:
            if value and str(value) not in dependencies:
                dependencies.append(str(value))

    docs = data.get("docs", {}) if isinstance(data.get("docs"), dict) else {}
    doc_info = {
        key: str(docs.get(key)) if docs.get(key) else None for key in ("readme", "agents", "stage")
    }

    warnings: list[str] = []
    for key, comp_root_rel in content.items():
        if isinstance(comp_root_rel, str) and not (root / comp_root_rel).exists():
            warnings.append(f"Declared content root does not exist: {comp_root_rel}")
    warnings.extend(_check_semantic_secrets(data))
    warnings.extend(_check_semantic_deps(data))

    keywords = (
        [str(value) for value in data.get("keywords", []) if value]
        if isinstance(data.get("keywords"), list)
        else []
    )
    capabilities = (
        [str(value) for value in data.get("capabilities", []) if value]
        if isinstance(data.get("capabilities"), list)
        else []
    )
    required_context = (
        [str(value) for value in agent.get("required_context", []) if value]
        if isinstance(agent.get("required_context"), list)
        else []
    )
    permissions = _normalize_pack_permissions(data.get("permissions"))
    normalized_permissions = [permission.to_dict() for permission in permissions]
    permission_ids = [permission.id for permission in permissions]

    return {
        "pack_id": pack_id,
        "name": name,
        "version": version,
        "schema_version": schema_version,
        "source_path": str(root),
        "component_counts": component_counts,
        "entrypoints": display_entrypoints,
        "normal_entrypoints": normal_entrypoints,
        "declared_secrets": secrets_list,
        "dependencies": dependencies,
        "dependencies_struct": dependencies_struct,
        "docs": doc_info,
        "warnings": warnings,
        "do_not_use_for": str(agent.get("do_not_use_for")) if agent.get("do_not_use_for") else None,
        "required_context": required_context,
        "keywords": keywords,
        "capabilities": capabilities,
        "permissions": normalized_permissions,
        "permission_ids": permission_ids,
        "trust": dict(V1_TRUST_BLOCK),
        "astrid_version": data.get("astrid_version"),
    }


__all__ = [
    "CANONICAL_PACK_LAYOUT_RULES",
    "CanonicalLayoutRule",
    "KNOWN_SCHEMA_VERSIONS",
    "KNOWN_VERSIONS_STR",
    "LayoutExceptionClass",
    "LayoutExceptionLifecycle",
    "LayoutValidationIssue",
    "PackLayoutContractError",
    "PackLayoutException",
    "PackValidator",
    "V1_TRUST_BLOCK",
    "ValidationError",
    "extract_trust_summary",
    "PackRootValidation",
    "PackValidationReport",
    "is_first_party_packs_root_candidate",
    "json_loads",
    "validate_first_party_packs_root",
    "validate_first_party_packs_root_report",
    "validate_pack",
    "validate_pack_roots",
    # Re-exported — used by tests that mock through astrid.core.pack.validate.
    "iter_executor_roots",
    "iter_orchestrator_roots",
    "iter_element_roots",
]
