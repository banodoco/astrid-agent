"""Pack discovery and validation helpers.

This module is a thin re-export facade. The implementation lives in cohesive
sibling submodules so that ``astrid.core.pack`` stays the stable import path:

* :mod:`astrid.core.pack._common` — leaf: errors, constants, scalar/JSON helpers,
  id validation.
* :mod:`astrid.core.pack.definition` — :class:`PackDefinition` / :class:`PackPermission`.
* :mod:`astrid.core.pack.registry` — element/timeline kind registry + descriptors.
* :mod:`astrid.core.pack.permissions` — permission/alias/extension normalizers.
* :mod:`astrid.core.pack.walkers` — content-root filesystem iterators.
* :mod:`astrid.core.pack.loader` — manifest loading/parsing, discovery, packs root.

The package re-exports the canonical public pack APIs from these focused
modules; retired parser compatibility helpers are intentionally not exposed.
"""

from __future__ import annotations

from astrid.core.pack.identity import pack_digest, read_pin, write_pin

from astrid.core.pack._common import (
    _PACK_ID_RE,
    ELEMENT_KINDS,
    ELEMENT_MANIFEST_NAMES,
    EXECUTOR_MANIFEST_NAMES,
    ORCHESTRATOR_MANIFEST_NAMES,
    PACK_ALIAS_KINDS,
    PACK_MANIFEST_NAMES,
    PACK_PERMISSION_IDS,
    TIMELINE_KIND_CATALOGS,
    ElementKind,
    PackAliasKind,
    PackValidationError,
    _normalize_json_object,
    _normalize_json_value,
    _optional_string,
    _require_mapping,
    _require_string,
    _validate_pack_id,
    find_component_manifest,
    qualified_id_pack_id,
    validate_content_id_in_pack,
    validate_element_pack_id,
)
from astrid.core.pack.canonical import (
    BundledCatalog,
    CanonicalPackEntry,
    CanonicalPackValidationError,
    catalog_from_root,
    read_normalize_validate,
    validate_canonical_pack,
)
from astrid.core.pack.definition import (
    PackDefinition,
    PackPermission,
)
from astrid.core.pack.loader import (
    DEFAULT_PACKS_ROOT,
    _default_stability_for_status,
    _load_manifest_payload,
    discover_packs,
    load_pack_manifest,
    pack_manifest_path,
    pack_taxonomy_from_manifest,
    packs_root,
)
from astrid.core.pack.permissions import (
    _normalize_editor_extensions,
    _normalize_element_extensions,
    _normalize_element_kinds,
    _normalize_generation_backends,
    _normalize_generation_extensions,
    _normalize_named_extension_list,
    _normalize_pack_permission_services,
    _normalize_pack_permissions,
    _normalize_rendering_extensions,
    _normalize_rendering_manifest_paths,
    _normalize_timeline_extensions,
    _normalize_timeline_kinds,
    _optional_pack_aliases,
    _optional_pack_extensions,
)
from astrid.core.pack.registry import (
    _BUILTIN_KIND_IDS_BY_CATALOG,
    ELEMENT_KIND_REGISTRY,
    ElementKindDescriptor,
    ElementKindRegistry,
    _builtin_element_kind_descriptors,
    _builtin_kind_descriptors,
    _extension_kind_descriptors,
    _pack_kind_registry_error,
    artifact_type_registry_for_pack,
    element_kind_registry_for_pack,
    pack_artifact_type_descriptors,
    pack_editor_entry_paths,
    pack_element_kind_descriptors,
    pack_kind_descriptors,
    pack_rendering_manifest_paths,
    pack_timeline_kind_descriptors,
)
from astrid.core.pack.walkers import (
    _content_roots,
    _declared_content_root,
    _direct_content_roots,
    _iter_element_kind_dirs,
    _vendored_subdirs,
    iter_element_roots,
    iter_executor_roots,
    iter_orchestrator_roots,
)

__all__ = [
    "ElementKindDescriptor",
    "ElementKindRegistry",
    "ELEMENT_KIND_REGISTRY",
    "PackDefinition",
    "PackValidationError",
    "pack_digest",
    "read_pin",
    "write_pin",
    "discover_packs",
    "element_kind_registry_for_pack",
    "iter_element_roots",
    "iter_executor_roots",
    "iter_orchestrator_roots",
    "load_pack_manifest",
    "pack_editor_entry_paths",
    "pack_taxonomy_from_manifest",
    "pack_manifest_path",
    "pack_rendering_manifest_paths",
    "packs_root",
    "qualified_id_pack_id",
    "validate_content_id_in_pack",
    "validate_element_pack_id",
    "BundledCatalog",
    "CanonicalPackEntry",
    "CanonicalPackValidationError",
    "catalog_from_root",
    "read_normalize_validate",
    "validate_canonical_pack",
]
