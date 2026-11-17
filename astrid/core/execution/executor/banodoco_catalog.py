"""Banodoco website catalog client for opt-in external agent executors."""

from __future__ import annotations

import json
import os
import urllib.error
import urllib.request
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any

from astrid.core.env_vars import ASTRID_RUNTIME_CATALOG_URL
from astrid.core.util.http import os_trust_ssl_context

from .catalog_source import GitExecutorSource, fetch_git_executor_manifest
from .schema import ExecutorDefinition, ExecutorValidationError, validate_executor_definition


class BanodocoCatalogError(ExecutorValidationError):
    """Raised when the Banodoco website catalog cannot be used."""


@dataclass(frozen=True)
class BanodocoCatalogConfig:
    enabled: bool = False
    catalog_url: str | None = None
    include_defaults: bool = True
    include_mandatory: bool = True
    cache_dir: Path | None = None
    refresh: bool = False
    timeout_seconds: float = 15.0

    @classmethod
    def from_env(cls) -> "BanodocoCatalogConfig":
        enabled_var = "ASTRID_RUNTIME_AGENT_EXECUTORS"
        cache_var = "ASTRID_RUNTIME_EXECUTOR_CACHE"
        return cls(
            enabled=_env_bool(enabled_var, default=False),
            catalog_url=os.environ.get(ASTRID_RUNTIME_CATALOG_URL),
            include_defaults=_env_bool("ASTRID_RUNTIME_DEFAULT_EXECUTORS", default=True),
            include_mandatory=_env_bool("ASTRID_RUNTIME_MANDATORY_EXECUTORS", default=True),
            cache_dir=Path(os.environ[cache_var]).expanduser()
            if os.environ.get(cache_var)
            else None,
            refresh=_env_bool("ASTRID_RUNTIME_REFRESH", default=False),
        )


def load_banodoco_catalog_executors(config: BanodocoCatalogConfig) -> tuple[ExecutorDefinition, ...]:
    if not config.enabled:
        return ()
    if not config.catalog_url:
        raise BanodocoCatalogError("ASTRID_RUNTIME_CATALOG_URL is required when Banodoco agent executors are enabled")

    payload = _fetch_catalog_payload(config)
    executors: list[ExecutorDefinition] = []
    for row in _catalog_executors(payload):
        catalog = _require_mapping(row.get("catalog", {}), "executor.catalog")
        is_default = bool(catalog.get("default"))
        is_mandatory = bool(catalog.get("mandatory"))
        if (is_mandatory and config.include_mandatory) or (is_default and config.include_defaults):
            executors.append(_load_catalog_executor(row, config))
    return tuple(executors)


def _fetch_catalog_payload(config: BanodocoCatalogConfig) -> dict[str, Any]:
    request = urllib.request.Request(config.catalog_url or "", headers={"Accept": "application/json"})
    try:
        with urllib.request.urlopen(request, timeout=config.timeout_seconds, context=os_trust_ssl_context()) as response:
            raw = response.read().decode("utf-8")
    except urllib.error.URLError as exc:
        raise BanodocoCatalogError(f"failed to fetch Banodoco agent executor catalog: {exc}") from exc
    try:
        data = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise BanodocoCatalogError(f"Banodoco agent executor catalog returned invalid JSON: {exc.msg}") from exc
    return _require_mapping(data, "catalog")


def _catalog_executors(payload: dict[str, Any]) -> list[dict[str, Any]]:
    return _catalog_entries(payload, key="executors", label="executor")


def _catalog_entries(payload: dict[str, Any], *, key: str, label: str) -> list[dict[str, Any]]:
    items = payload.get(key, [])
    if not isinstance(items, list):
        raise BanodocoCatalogError(f"Banodoco catalog field {key!r} must be a list")
    result: list[dict[str, Any]] = []
    for index, item in enumerate(items):
        result.append(_require_mapping(item, f"{key}[{index}]"))
    return result


def _load_catalog_executor(row: dict[str, Any], config: BanodocoCatalogConfig) -> ExecutorDefinition:
    expected_id = _require_string(row.get("expected_manifest_id"), "executor.expected_manifest_id")
    targets = row.get("install_targets", [])
    if not isinstance(targets, list) or not targets:
        raise BanodocoCatalogError(f"Banodoco executor {expected_id!r} has no install targets")

    target = _require_mapping(targets[0], "executor.install_targets[0]")
    source_type = _require_string(target.get("source_type"), "install_target.source_type")
    if source_type != "git":
        raise BanodocoCatalogError(f"Banodoco executor {expected_id!r} install target must be git for Astrid v1")

    manifest = fetch_git_executor_manifest(
        GitExecutorSource(
            repo_url=_require_string(target.get("repo_url"), "install_target.repo_url"),
            manifest_path=_require_string(target.get("manifest_path"), "install_target.manifest_path"),
            expected_executor_id=_require_string(target.get("expected_executor_id"), "install_target.expected_executor_id"),
            commit_sha=_optional_string(_ref_value(target, "commit_sha"), "install_target.ref.commit_sha"),
            tag=_optional_string(_ref_value(target, "tag"), "install_target.ref.tag"),
            branch=_optional_string(_ref_value(target, "branch"), "install_target.ref.branch"),
            source_ref=_optional_string(_ref_value(target, "source_ref"), "install_target.ref.source_ref"),
            install_subdir=_optional_string(target.get("install_subdir"), "install_target.install_subdir"),
        ),
        cache_dir=config.cache_dir,
        refresh=config.refresh,
    )
    executor = validate_executor_definition(manifest)
    if executor.id != expected_id:
        raise BanodocoCatalogError(
            f"Banodoco catalog identity mismatch: expected {expected_id!r}, fetched manifest {executor.id!r}"
        )
    metadata = dict(executor.metadata)
    metadata.update({"source": "banodoco_catalog", "banodoco_catalog_id": row.get("id"), "banodoco_slug": row.get("slug")})
    return validate_executor_definition(replace(executor, metadata=metadata))


def _ref_value(target: dict[str, Any], name: str) -> Any:
    ref = target.get("ref")
    if isinstance(ref, dict) and name in ref:
        return ref[name]
    return target.get(name)


def _require_mapping(value: Any, path: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise BanodocoCatalogError(f"{path} must be an object")
    return value


def _require_string(value: Any, path: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise BanodocoCatalogError(f"{path} must be a non-empty string")
    return value


def _optional_string(value: Any, path: str) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str) or not value.strip():
        raise BanodocoCatalogError(f"{path} must be a non-empty string")
    return value


def _env_bool(name: str, *, default: bool) -> bool:
    raw = os.environ.get(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


__all__ = [
    "BanodocoCatalogConfig",
    "BanodocoCatalogError",
    "load_banodoco_catalog_executors",
]
