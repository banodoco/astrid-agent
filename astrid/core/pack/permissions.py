"""Normalizers for pack permissions, aliases, and extension blocks."""

from __future__ import annotations

from typing import Any

from astrid.core.pack._common import (
    PACK_ALIAS_KINDS,
    PACK_PERMISSION_IDS,
    TIMELINE_KIND_CATALOGS,
    PackValidationError,
    _normalize_json_object,
    _optional_string,
    _require_mapping,
    _require_string,
    qualified_id_pack_id,
)
from astrid.core.pack.definition import PackPermission


def _optional_pack_aliases(value: Any, *, path: str) -> tuple[dict[str, Any], ...]:
    if value is None:
        return ()
    if not isinstance(value, list):
        raise PackValidationError(f"{path} must be an array")

    normalized: list[dict[str, Any]] = []
    allowed_keys = {"alias", "canonical_id", "kind", "deprecated", "deprecation_message"}
    for index, raw_alias in enumerate(value):
        alias_path = f"{path}[{index}]"
        if not isinstance(raw_alias, dict):
            raise PackValidationError(f"{alias_path} must be an object")
        unknown_keys = sorted(set(raw_alias) - allowed_keys)
        if unknown_keys:
            raise PackValidationError(
                f"{alias_path} has unknown field(s): {', '.join(unknown_keys)}"
            )

        kind = _require_string(raw_alias, "kind", f"{alias_path}.kind")
        if kind not in PACK_ALIAS_KINDS:
            raise PackValidationError(
                f"{alias_path}.kind must be one of {list(PACK_ALIAS_KINDS)}"
            )

        alias = _require_string(raw_alias, "alias", f"{alias_path}.alias")
        qualified_id_pack_id(alias, path=f"{alias_path}.alias")
        canonical_id = _require_string(raw_alias, "canonical_id", f"{alias_path}.canonical_id")
        qualified_id_pack_id(canonical_id, path=f"{alias_path}.canonical_id")

        normalized_alias: dict[str, Any] = {
            "kind": kind,
            "alias": alias,
            "canonical_id": canonical_id,
        }
        if "deprecated" in raw_alias:
            deprecated = raw_alias["deprecated"]
            if not isinstance(deprecated, bool):
                raise PackValidationError(f"{alias_path}.deprecated must be a boolean")
            normalized_alias["deprecated"] = deprecated
        if "deprecation_message" in raw_alias:
            deprecation_message = raw_alias["deprecation_message"]
            if not isinstance(deprecation_message, str):
                raise PackValidationError(f"{alias_path}.deprecation_message must be a string")
            normalized_alias["deprecation_message"] = deprecation_message
        normalized.append(normalized_alias)

    return tuple(normalized)


def _normalize_pack_permissions(raw: Any, field: str = "permissions") -> tuple[PackPermission, ...]:
    if raw is None:
        return ()
    if not isinstance(raw, list):
        raise PackValidationError(f"{field} must be an array")

    normalized: list[PackPermission] = []
    allowed_keys = {"id", "reason", "access", "services"}
    for index, raw_permission in enumerate(raw):
        permission_path = f"{field}[{index}]"
        permission = _require_mapping(raw_permission, permission_path)
        unknown_keys = sorted(set(permission) - allowed_keys)
        if unknown_keys:
            raise PackValidationError(
                f"{permission_path} has unknown field(s): {', '.join(unknown_keys)}"
            )

        permission_id = _require_string(permission, "id", f"{permission_path}.id")
        if permission_id not in PACK_PERMISSION_IDS:
            raise PackValidationError(
                f"{permission_path}.id must be one of {list(PACK_PERMISSION_IDS)}"
            )

        normalized.append(
            PackPermission(
                id=permission_id,
                reason=_require_string(permission, "reason", f"{permission_path}.reason"),
                access=_optional_string(permission, "access", f"{permission_path}.access", default=""),
                services=_normalize_pack_permission_services(
                    permission.get("services"),
                    path=f"{permission_path}.services",
                ),
            )
        )

    return tuple(normalized)


def _normalize_pack_permission_services(value: Any, *, path: str) -> tuple[str, ...]:
    if value is None:
        return ()
    if not isinstance(value, list):
        raise PackValidationError(f"{path} must be an array")

    normalized: list[str] = []
    for index, raw_service in enumerate(value):
        service_path = f"{path}[{index}]"
        if not isinstance(raw_service, str) or not raw_service.strip():
            raise PackValidationError(f"{service_path} must be a non-empty string")
        normalized.append(raw_service.strip())
    return tuple(normalized)


def _optional_pack_extensions(value: Any, *, path: str) -> dict[str, Any]:
    if value is None:
        return {}
    data = _require_mapping(value, path)
    allowed_keys = {
        "generation",
        "elements",
        "timeline",
        "rendering",
        "editor",
        "schemas",
        "artifact_types",
    }
    unknown_keys = sorted(set(data) - allowed_keys)
    if unknown_keys:
        raise PackValidationError(f"{path} has unknown field(s): {', '.join(unknown_keys)}")

    normalized: dict[str, Any] = {}
    if "generation" in data:
        normalized["generation"] = _normalize_generation_extensions(
            data["generation"],
            path=f"{path}.generation",
        )
    if "elements" in data:
        normalized["elements"] = _normalize_element_extensions(
            data["elements"],
            path=f"{path}.elements",
        )
    if "timeline" in data:
        normalized["timeline"] = _normalize_timeline_extensions(
            data["timeline"],
            path=f"{path}.timeline",
        )
    if "rendering" in data:
        normalized["rendering"] = _normalize_rendering_extensions(
            data["rendering"],
            path=f"{path}.rendering",
        )
    if "editor" in data:
        normalized["editor"] = _normalize_editor_extensions(
            data["editor"],
            path=f"{path}.editor",
        )
    if "schemas" in data:
        normalized["schemas"] = _normalize_json_object(
            data["schemas"],
            path=f"{path}.schemas",
        )
    if "artifact_types" in data:
        normalized["artifact_types"] = _normalize_artifact_types(
            data["artifact_types"],
            path=f"{path}.artifact_types",
        )
    return normalized


def _normalize_rendering_extensions(value: Any, *, path: str) -> dict[str, Any]:
    data = _require_mapping(value, path)
    allowed_keys = {"renderers", "planners", "finalizers"}
    unknown_keys = sorted(set(data) - allowed_keys)
    if unknown_keys:
        raise PackValidationError(f"{path} has unknown field(s): {', '.join(unknown_keys)}")

    normalized: dict[str, Any] = {}
    for key in ("renderers", "planners", "finalizers"):
        if key in data:
            normalized[key] = _normalize_rendering_manifest_paths(
                data[key],
                path=f"{path}.{key}",
            )
    return normalized

def _normalize_editor_extensions(value: Any, *, path: str) -> dict[str, Any]:
    data = _require_mapping(value, path)
    allowed_keys = {"entries"}
    unknown_keys = sorted(set(data) - allowed_keys)
    if unknown_keys:
        raise PackValidationError(f"{path} has unknown field(s): {', '.join(unknown_keys)}")
    if "entries" not in data:
        raise PackValidationError(f"{path} must declare entries")
    return {
        "entries": _normalize_rendering_manifest_paths(
            data["entries"],
            path=f"{path}.entries",
        )
    }


def _normalize_rendering_manifest_paths(value: Any, *, path: str) -> list[str]:
    if not isinstance(value, list):
        raise PackValidationError(f"{path} must be an array")

    normalized: list[str] = []
    for index, raw_path in enumerate(value):
        if not isinstance(raw_path, str):
            raise PackValidationError(f"{path}[{index}] must be a string")
        normalized.append(raw_path)
    return normalized


def _normalize_generation_extensions(value: Any, *, path: str) -> dict[str, Any]:
    data = _require_mapping(value, path)
    allowed_keys = {"backends", "features", "modes"}
    unknown_keys = sorted(set(data) - allowed_keys)
    if unknown_keys:
        raise PackValidationError(f"{path} has unknown field(s): {', '.join(unknown_keys)}")

    normalized: dict[str, Any] = {}
    if "backends" in data:
        normalized["backends"] = _normalize_generation_backends(
            data["backends"],
            path=f"{path}.backends",
        )
    if "features" in data:
        normalized["features"] = _normalize_named_extension_list(
            data["features"],
            path=f"{path}.features",
        )
    if "modes" in data:
        normalized["modes"] = _normalize_named_extension_list(
            data["modes"],
            path=f"{path}.modes",
        )
    return normalized


def _normalize_generation_backends(value: Any, *, path: str) -> list[dict[str, Any]]:
    if not isinstance(value, list):
        raise PackValidationError(f"{path} must be an array")

    normalized: list[dict[str, Any]] = []
    allowed_keys = {"id", "label", "module", "class", "init_kwargs"}
    for index, raw_backend in enumerate(value):
        backend_path = f"{path}[{index}]"
        backend = _require_mapping(raw_backend, backend_path)
        unknown_keys = sorted(set(backend) - allowed_keys)
        if unknown_keys:
            raise PackValidationError(
                f"{backend_path} has unknown field(s): {', '.join(unknown_keys)}"
            )
        normalized_backend = {
            "id": _require_string(backend, "id", f"{backend_path}.id"),
            "module": _require_string(backend, "module", f"{backend_path}.module"),
            "class": _require_string(backend, "class", f"{backend_path}.class"),
        }
        if "label" in backend:
            normalized_backend["label"] = _optional_string(
                backend,
                "label",
                f"{backend_path}.label",
                default="",
            )
        if "init_kwargs" in backend:
            normalized_backend["init_kwargs"] = _normalize_json_object(
                backend["init_kwargs"],
                path=f"{backend_path}.init_kwargs",
            )
        normalized.append(normalized_backend)
    return normalized


def _normalize_named_extension_list(value: Any, *, path: str) -> list[dict[str, str]]:
    if not isinstance(value, list):
        raise PackValidationError(f"{path} must be an array")

    normalized: list[dict[str, str]] = []
    allowed_keys = {"id", "label", "description"}
    for index, raw_item in enumerate(value):
        item_path = f"{path}[{index}]"
        if isinstance(raw_item, str):
            if not raw_item.strip():
                raise PackValidationError(f"{item_path} must be a non-empty string")
            normalized.append({"id": raw_item})
            continue
        item = _require_mapping(raw_item, item_path)
        unknown_keys = sorted(set(item) - allowed_keys)
        if unknown_keys:
            raise PackValidationError(
                f"{item_path} has unknown field(s): {', '.join(unknown_keys)}"
            )
        normalized_item = {
            "id": _require_string(item, "id", f"{item_path}.id"),
        }
        if "label" in item:
            normalized_item["label"] = _optional_string(
                item,
                "label",
                f"{item_path}.label",
                default="",
            )
        if "description" in item:
            normalized_item["description"] = _optional_string(
                item,
                "description",
                f"{item_path}.description",
                default="",
            )
        normalized.append(normalized_item)
    return normalized


def _normalize_element_extensions(value: Any, *, path: str) -> dict[str, Any]:
    data = _require_mapping(value, path)
    allowed_keys = {"kinds"}
    unknown_keys = sorted(set(data) - allowed_keys)
    if unknown_keys:
        raise PackValidationError(f"{path} has unknown field(s): {', '.join(unknown_keys)}")

    normalized: dict[str, Any] = {}
    if "kinds" in data:
        normalized["kinds"] = _normalize_element_kinds(
            data["kinds"],
            path=f"{path}.kinds",
        )
    return normalized


def _normalize_timeline_extensions(value: Any, *, path: str) -> dict[str, Any]:
    data = _require_mapping(value, path)
    allowed_keys = {"kinds"}
    unknown_keys = sorted(set(data) - allowed_keys)
    if unknown_keys:
        raise PackValidationError(f"{path} has unknown field(s): {', '.join(unknown_keys)}")

    normalized: dict[str, Any] = {}
    if "kinds" in data:
        normalized["kinds"] = _normalize_timeline_kinds(
            data["kinds"],
            path=f"{path}.kinds",
        )
    return normalized


def _normalize_artifact_types(value: Any, *, path: str) -> dict[str, Any]:
    """Normalize ``extensions.artifact_types`` to ``{types: [...]}``."""
    data = _require_mapping(value, path)
    allowed_keys = {"types"}
    unknown_keys = sorted(set(data) - allowed_keys)
    if unknown_keys:
        raise PackValidationError(f"{path} has unknown field(s): {', '.join(unknown_keys)}")

    normalized: dict[str, Any] = {}
    if "types" in data:
        normalized["types"] = _normalize_artifact_type_items(
            data["types"],
            path=f"{path}.types",
        )
    return normalized


def _normalize_artifact_type_items(value: Any, *, path: str) -> list[dict[str, Any]]:
    if not isinstance(value, list):
        raise PackValidationError(f"{path} must be an array")

    normalized: list[dict[str, Any]] = []
    allowed_keys = {"id", "aliases", "description"}
    for index, raw_item in enumerate(value):
        item_path = f"{path}[{index}]"
        item = _require_mapping(raw_item, item_path)
        unknown_keys = sorted(set(item) - allowed_keys)
        if unknown_keys:
            raise PackValidationError(
                f"{item_path} has unknown field(s): {', '.join(unknown_keys)}"
            )
        normalized_item: dict[str, Any] = {
            "id": _require_string(item, "id", f"{item_path}.id"),
        }
        if "aliases" in item:
            aliases = item["aliases"]
            if not isinstance(aliases, list):
                raise PackValidationError(f"{item_path}.aliases must be an array")
            normalized_aliases: list[str] = []
            for alias_index, raw_alias in enumerate(aliases):
                alias_path = f"{item_path}.aliases[{alias_index}]"
                if not isinstance(raw_alias, str) or not raw_alias.strip():
                    raise PackValidationError(f"{alias_path} must be a non-empty string")
                normalized_aliases.append(raw_alias.strip())
            normalized_item["aliases"] = normalized_aliases
        if "description" in item:
            normalized_item["description"] = _optional_string(
                item,
                "description",
                f"{item_path}.description",
                default="",
            )
        normalized.append(normalized_item)
    return normalized


def _normalize_element_kinds(value: Any, *, path: str) -> list[dict[str, str]]:
    if not isinstance(value, list):
        raise PackValidationError(f"{path} must be an array")

    normalized: list[dict[str, str]] = []
    allowed_keys = {"id", "singular", "plural", "label", "description"}
    for index, raw_kind in enumerate(value):
        kind_path = f"{path}[{index}]"
        kind = _require_mapping(raw_kind, kind_path)
        unknown_keys = sorted(set(kind) - allowed_keys)
        if unknown_keys:
            raise PackValidationError(
                f"{kind_path} has unknown field(s): {', '.join(unknown_keys)}"
            )
        normalized_kind = {
            "id": _require_string(kind, "id", f"{kind_path}.id"),
        }
        for key in ("singular", "plural", "label", "description"):
            if key in kind:
                normalized_kind[key] = _optional_string(
                    kind,
                    key,
                    f"{kind_path}.{key}",
                    default="",
                )
        normalized.append(normalized_kind)
    return normalized


def _normalize_timeline_kinds(value: Any, *, path: str) -> list[dict[str, Any]]:
    if not isinstance(value, list):
        raise PackValidationError(f"{path} must be an array")

    normalized: list[dict[str, Any]] = []
    allowed_keys = {"catalog", "id", "aliases", "default"}
    for index, raw_kind in enumerate(value):
        kind_path = f"{path}[{index}]"
        kind = _require_mapping(raw_kind, kind_path)
        unknown_keys = sorted(set(kind) - allowed_keys)
        if unknown_keys:
            raise PackValidationError(
                f"{kind_path} has unknown field(s): {', '.join(unknown_keys)}"
            )
        catalog = _require_string(kind, "catalog", f"{kind_path}.catalog")
        if catalog not in TIMELINE_KIND_CATALOGS:
            raise PackValidationError(
                f"{kind_path}.catalog must be one of {list(TIMELINE_KIND_CATALOGS)}"
            )
        normalized_kind: dict[str, Any] = {
            "catalog": catalog,
            "id": _require_string(kind, "id", f"{kind_path}.id"),
        }
        if "aliases" in kind:
            aliases = kind["aliases"]
            if not isinstance(aliases, list):
                raise PackValidationError(f"{kind_path}.aliases must be an array")
            normalized_aliases: list[str] = []
            for alias_index, raw_alias in enumerate(aliases):
                alias_path = f"{kind_path}.aliases[{alias_index}]"
                if not isinstance(raw_alias, str) or not raw_alias.strip():
                    raise PackValidationError(f"{alias_path} must be a non-empty string")
                normalized_aliases.append(raw_alias.strip())
            normalized_kind["aliases"] = normalized_aliases
        if "default" in kind:
            default = kind["default"]
            if not isinstance(default, bool):
                raise PackValidationError(f"{kind_path}.default must be a boolean")
            normalized_kind["default"] = default
        normalized.append(normalized_kind)
    return normalized
