"""Registry and discovery helpers for Astrid executors."""

from __future__ import annotations

import json
import logging
from dataclasses import replace
from pathlib import Path
from types import MappingProxyType
from typing import TYPE_CHECKING, Any, Iterable

from astrid.core.foundation.paths import REPO_ROOT
from astrid.core.pack import (
    discover_packs,
    iter_executor_roots,
    validate_content_id_in_pack,
)
from astrid.core.pack.discovery import DiscoveredPack, discover_packs_ordered
from astrid.core.pack.manifest import (
    ManifestParseError,
)
from astrid.core.pack.resolver import PackResolver
from astrid.core.registry import CapabilityRegistry

if TYPE_CHECKING:
    from .banodoco_catalog import BanodocoCatalogConfig
from .actions import action_executor_definition
from .folder import load_folder_executors
from .schema import ExecutorDefinition, ExecutorValidationError, validate_executor_definition

BUILTIN_STEP_ORDER: tuple[str, ...] = (
    "transcribe",
    "scenes",
    "quality_zones",
    "shots",
    "triage",
    "scene_describe",
    "quote_scout",
    "pool_build",
    "pool_merge",
    "arrange",
    "cut",
    "refine",
    "render",
    "editor_review",
    "validate",
)

class ExecutorRegistryError(ExecutorValidationError):
    """Raised when a executor registry is inconsistent."""


_LOGGER = logging.getLogger(__name__)


class ExecutorRegistry(CapabilityRegistry[str, ExecutorDefinition]):
    """Small in-memory registry keyed by executor id.

    Inherits generic storage and conflict detection from
    :class:`CapabilityRegistry`.  Duplicate ids retain canonical discovery
    order; a project-local source pack cannot shadow an earlier source pack.
    """

    def __init__(
        self,
        executors: Iterable[ExecutorDefinition | dict[str, Any]] = (),
    ) -> None:
        super().__init__()
        for executor in executors:
            self.register(executor)

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def register(self, executor: ExecutorDefinition | dict[str, Any]) -> ExecutorDefinition:
        definition = validate_executor_definition(executor)
        self._register_impl(
            definition.id,
            definition,
            # Discovery order is the only source ordering.  In particular,
            # Local editable packs are ordinary source inputs, not a
            # precedence layer with elevated priority.
            priority_key=lambda d: int(d.metadata.get("priority", 30)),
        )
        return definition

    def get(self, executor_id: str) -> ExecutorDefinition:
        if executor_id not in self._entries:
            raise KeyError(f"unknown executor id {executor_id!r}")
        return self._resolve_entry(self._entries[executor_id])

    def _iter_all(self) -> Iterable[ExecutorDefinition]:
        """Yield every registered definition (including shadowed)."""
        for entry in self._entries.values():
            yield from self._iter_entries(entry)

    def list(self, kind: str | None = None) -> tuple[ExecutorDefinition, ...]:
        if kind is not None and kind not in {"built_in", "external"}:
            raise ExecutorRegistryError("kind must be one of ['built_in', 'external']")
        # Winners only (first entry per id after priority sort).
        executors = (self._resolve_entry(entry) for entry in self._entries.values())
        if kind is not None:
            executors = [executor for executor in executors if executor.kind == kind]
        return tuple(sorted(executors, key=lambda executor: executor.id))

    def validate_all(self) -> tuple[ExecutorDefinition, ...]:
        # Validate winners only — shadowed entries intentionally not validated.
        for executor in (self._resolve_entry(entry) for entry in self._entries.values()):
            validate_executor_definition(executor)
        self._validate_graph_references()
        return self.list()

    def to_dict(self, kind: str | None = None) -> dict[str, Any]:
        return {"executors": [executor.to_dict() for executor in self.list(kind=kind)]}

    def to_json(self, *, kind: str | None = None, indent: int | None = 2) -> str:
        return json.dumps(self.to_dict(kind=kind), indent=indent, sort_keys=True)

    def as_mapping(self) -> MappingProxyType[str, ExecutorDefinition]:
        # Winners only.
        return MappingProxyType(
            {eid: self._resolve_entry(entry) for eid, entry in self._entries.items()}
        )

    def _validate_graph_references(self) -> None:
        known_ids = set(self._entries)  # keys are strings, unchanged
        # Winners only.
        for executor in (self._resolve_entry(entry) for entry in self._entries.values()):
            for dependency in executor.graph.depends_on:
                resolved = dependency
                if resolved not in known_ids:
                    raise ExecutorRegistryError(f"executor {executor.id!r} depends on unknown executor {resolved!r}")
                if resolved == executor.id:
                    raise ExecutorRegistryError(f"executor {executor.id!r} cannot depend on itself")

def load_default_registry(
    banodoco_config: "BanodocoCatalogConfig | None" = None,
    *,
    project_root: str | Path = REPO_ROOT,
    extra_pack_roots: tuple[str, ...] = (),
    additional_executors: Iterable[ExecutorDefinition | dict[str, Any]] = (),
    discovered_packs: tuple[DiscoveredPack, ...] | None = None,
) -> ExecutorRegistry:
    """Compose admitted definitions before validating the complete graph.

    Legacy pack executors, optional catalog definitions and v3 actions from
    the same discovered inventory retain the established registration order.
    Explicit additional definitions follow those sources. Existing priority
    and winner rules apply; the complete graph is validated once.
    ``discovered_packs`` reuses an admitted per-call snapshot without rescanning.
    """
    packs = (
        tuple(item.pack for item in discovered_packs)
        if discovered_packs is not None
        else _discover_executor_packs(project_root=project_root, extra_pack_roots=extra_pack_roots)
    )
    registry = ExecutorRegistry()
    local_parent_ids: set[str] = set()
    for executor in _load_pack_executors_from_packs(packs):
        registry.register(executor)
    if banodoco_config is not None and banodoco_config.enabled:
        from .banodoco_catalog import load_banodoco_catalog_executors

        for executor in load_banodoco_catalog_executors(banodoco_config):
            registry.register(executor)
    for pack in packs:
        for local_id, action in getattr(pack, "actions", {}).items():
            from .actions import action_is_local_orchestrator
            if action_is_local_orchestrator(action):
                local_parent_ids.add(f"{pack.id}.{local_id}")
                if f"{pack.id}.{local_id}" in registry.as_mapping():
                    raise ExecutorRegistryError("local orchestrator collides with executor identity")
                continue
            registry.register(action_executor_definition(pack, local_id, action))
    for executor in additional_executors:
        registry.register(executor)
    if local_parent_ids.intersection(registry.as_mapping()):
        raise ExecutorRegistryError("local orchestrator collides with executor identity")
    registry.validate_all()
    return registry


def load_pack_executors(
    *,
    project_root: str | Path = REPO_ROOT,
    extra_pack_roots: tuple[str, ...] = (),
) -> tuple[ExecutorDefinition, ...]:
    packs = _discover_executor_packs(
        project_root=project_root,
        extra_pack_roots=extra_pack_roots,
    )
    return _load_pack_executors_from_packs(packs)


def _discover_executor_packs(
    *,
    project_root: str | Path,
    extra_pack_roots: tuple[str, ...],
) -> tuple[Any, ...]:
    return discover_packs_ordered(
        project_root=project_root,
        extra_pack_roots=extra_pack_roots,
        discover_packs_fn=discover_packs,
    )


def _load_pack_executors_from_packs(
    packs: Iterable[Any],
) -> tuple[ExecutorDefinition, ...]:
    executors: list[ExecutorDefinition] = []
    for pack in packs:
        # Per-pack fault tolerance: one broken content manifest (e.g. an
        # external pack whose executor.yaml fails schema
        # validation) must not abort the whole discovery/invoke. The pack is
        # skipped with a warning; pack-alignment failures
        # (``PackValidationError`` from ``validate_content_id_in_pack``)
        # still propagate — a misplaced id is a packaging contract breach,
        # not a bad manifest.
        try:
            for root in iter_executor_roots(pack):
                for executor in load_folder_executors(root):
                    validate_content_id_in_pack(executor.id, pack, content_type="executor")
                    executors.append(_attach_pack_metadata(executor, pack.id, pack_root=pack.root, content_root=root))
        except (ExecutorValidationError, ManifestParseError) as exc:
            _LOGGER.warning(
                "skipping pack %r: executor manifests failed validation: %s",
                getattr(pack, "id", pack),
                exc,
            )
            continue
    return tuple(executors)


def _attach_pack_metadata(
    executor: ExecutorDefinition,
    pack_id: str,
    *,
    pack_root: Path | None = None,
    content_root: Path | None = None,
) -> ExecutorDefinition:
    metadata = dict(executor.metadata)
    metadata["source"] = "pack"
    metadata["source_pack"] = pack_id
    if pack_root is not None:
        metadata["pack_root"] = str(pack_root)
    metadata["priority"] = 30
    if content_root is not None:
        metadata["content_root"] = str(content_root)
    return validate_executor_definition(replace(executor, metadata=metadata))


def resolve_executor_callable(executor: ExecutorDefinition):
    """Resolve an executor's manifest-declared Python runtime callable."""

    return PackResolver().resolve(executor.metadata, owner_id=executor.id)


def __getattr__(name: str):
    """Resolve the optional Banodoco catalog type only for explicit callers."""

    if name == "BanodocoCatalogConfig":
        from .banodoco_catalog import BanodocoCatalogConfig

        return BanodocoCatalogConfig
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


__all__ = [
    "BUILTIN_STEP_ORDER",
    "ExecutorRegistry",
    "ExecutorRegistryError",
    "BanodocoCatalogConfig",
    "load_pack_executors",
    "load_default_registry",
    "resolve_executor_callable",
]
