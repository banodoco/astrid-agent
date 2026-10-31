"""Registry and discovery helpers for Astrid orchestrators."""

from __future__ import annotations

import json
import logging
import os
from dataclasses import replace
from pathlib import Path
from types import MappingProxyType
from typing import Any, Iterable

from astrid.core.execution.executor.registry import ExecutorRegistry
from astrid.core.execution.executor.registry import (
    load_default_registry as load_default_executor_registry,
)
from astrid.core.foundation.paths import REPO_ROOT
from astrid.core.pack import (
    discover_packs,
    iter_orchestrator_roots,
    validate_content_id_in_pack,
)
from astrid.core.pack.discovery import DiscoveredPack, discover_packs_ordered
from astrid.core.pack.manifest import (
    ManifestParseError,
)
from astrid.core.registry import CapabilityRegistry

from .folder import load_folder_orchestrators
from .schema import (
    OrchestratorDefinition,
    OrchestratorValidationError,
    validate_orchestrator_definition,
)

logger = logging.getLogger(__name__)


class OrchestratorRegistryError(OrchestratorValidationError):
    """Raised when an orchestrator registry is inconsistent."""


class OrchestratorRegistry(CapabilityRegistry[str, OrchestratorDefinition]):
    """Small in-memory registry keyed by orchestrator id.

    Inherits generic storage and conflict detection from
    :class:`CapabilityRegistry`.  Duplicate ids retain canonical discovery
    order; a project-local source pack cannot shadow an earlier source pack.
    """

    def __init__(
        self,
        orchestrators: Iterable[OrchestratorDefinition | dict[str, Any]] = (),
        *,
        executor_registry: ExecutorRegistry | None = None,
    ) -> None:
        super().__init__()
        self._executor_registry = executor_registry
        #: Nested map: orchestrator_id → child_capability_id → frozenset of output artifact_type values.
        self._child_output_types: dict[str, dict[str, frozenset[str]]] = {}
        for orchestrator in orchestrators:
            self.register(orchestrator)

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def register(self, orchestrator: OrchestratorDefinition | dict[str, Any]) -> OrchestratorDefinition:
        definition = validate_orchestrator_definition(orchestrator)
        self._register_impl(
            definition.id,
            definition,
            priority_key=lambda d: int(d.metadata.get("priority", 30)),
        )
        return definition

    def get(self, orchestrator_id: str) -> OrchestratorDefinition:
        if orchestrator_id not in self._entries:
            raise KeyError(f"unknown orchestrator id {orchestrator_id!r}")
        return self._resolve_entry(self._entries[orchestrator_id])

    def _iter_all(self) -> Iterable[OrchestratorDefinition]:
        """Yield every registered definition (including shadowed)."""
        for entry in self._entries.values():
            yield from self._iter_entries(entry)

    def list(self, kind: str | None = None) -> tuple[OrchestratorDefinition, ...]:
        if kind is not None and kind not in {"built_in", "external"}:
            raise OrchestratorRegistryError("kind must be one of ['built_in', 'external']")
        # Winners only (first entry per id after priority sort).
        orchestrators = (self._resolve_entry(entry) for entry in self._entries.values())
        if kind is not None:
            orchestrators = [orchestrator for orchestrator in orchestrators if orchestrator.kind == kind]
        return tuple(sorted(orchestrators, key=lambda orchestrator: orchestrator.id))

    def validate_all(
        self,
        *,
        executor_registry: ExecutorRegistry | None = None,
    ) -> tuple[OrchestratorDefinition, ...]:
        # Validate winners only — shadowed entries intentionally not validated.
        for orchestrator in (self._resolve_entry(entry) for entry in self._entries.values()):
            validate_orchestrator_definition(orchestrator)
        self._validate_child_executors(
            executor_registry=executor_registry,
        )
        self._validate_child_orchestrators()
        self._strict_child_type_check()
        return self.list()

    def to_dict(self, kind: str | None = None) -> dict[str, Any]:
        return {"orchestrators": [orchestrator.to_dict() for orchestrator in self.list(kind=kind)]}

    def to_json(self, *, kind: str | None = None, indent: int | None = 2) -> str:
        return json.dumps(self.to_dict(kind=kind), indent=indent, sort_keys=True)

    def as_mapping(self) -> MappingProxyType[str, OrchestratorDefinition]:
        # Winners only.
        return MappingProxyType(
            {oid: self._resolve_entry(entry) for oid, entry in self._entries.items()}
        )

    def child_output_artifact_types(self, orchestrator_id: str) -> dict[str, frozenset[str]]:
        """Return a map of ``{child_capability_id: {output_artifact_types}}`` for *orchestrator_id*.

        Each child capability (executor or orchestrator) that declares at
        least one ``artifact_type`` on its outputs is included in the
        result.  Returns an empty dict when the orchestrator has no
        annotated children or the id is unknown.
        """
        canonical_id = orchestrator_id
        return dict(self._child_output_types.get(canonical_id, {}))

    def _validate_child_executors(
        self,
        *,
        executor_registry: ExecutorRegistry | None,
    ) -> None:
        registry = executor_registry or self._executor_registry or load_default_executor_registry()
        known_executor_ids = set(registry.as_mapping())
        # Build executor mapping for artifact_type collection.
        executor_mapping = registry.as_mapping()
        # Winners only.
        for orchestrator in (self._resolve_entry(entry) for entry in self._entries.values()):
            for child_executor in orchestrator.child_executors:
                resolved = child_executor
                if resolved not in known_executor_ids:
                    raise OrchestratorRegistryError(
                        f"orchestrator {orchestrator.id!r} references unknown child executor {resolved!r}"
                    )
            # Collect child executor output artifact types.
            child_map: dict[str, frozenset[str]] = {}
            for child_executor in orchestrator.child_executors:
                resolved = child_executor
                if resolved in executor_mapping:
                    exec_def = executor_mapping[resolved]
                    output_types: set[str] = set()
                    for output in exec_def.outputs:
                        if output.artifact_type:
                            output_types.add(output.artifact_type)
                    if output_types:
                        child_map[resolved] = frozenset(output_types)
            if child_map:
                self._child_output_types[orchestrator.id] = child_map

    def _validate_child_orchestrators(
        self,
    ) -> None:
        known_orchestrator_ids = set(self._entries)  # keys are strings
        graph: dict[str, tuple[str, ...]] = {}
        # Winners only.
        for orchestrator in (self._resolve_entry(entry) for entry in self._entries.values()):
            children: list[str] = []
            for child_orchestrator in orchestrator.child_orchestrators:
                resolved = child_orchestrator
                if resolved not in known_orchestrator_ids:
                    raise OrchestratorRegistryError(
                        f"orchestrator {orchestrator.id!r} references unknown child orchestrator {resolved!r}"
                    )
                if resolved == orchestrator.id:
                    raise OrchestratorRegistryError(f"orchestrator {orchestrator.id!r} cannot reference itself")
                children.append(resolved)
            graph[orchestrator.id] = tuple(children)
            # Collect child orchestrator output artifact types.
            child_map = self._child_output_types.get(orchestrator.id, {})
            for child_orchestrator in orchestrator.child_orchestrators:
                resolved = child_orchestrator
                child_def = self._resolve_entry(self._entries[resolved])
                output_types: set[str] = set()
                for output in child_def.outputs:
                    if output.artifact_type:
                        output_types.add(output.artifact_type)
                if output_types:
                    child_map[resolved] = frozenset(output_types)
            if child_map:
                self._child_output_types[orchestrator.id] = child_map
        self._validate_no_cycles(graph)

    def _validate_no_cycles(self, graph: dict[str, tuple[str, ...]]) -> None:
        visiting: set[str] = set()
        visited: set[str] = set()
        stack: list[str] = []

        def visit(orchestrator_id: str) -> None:
            if orchestrator_id in visited:
                return
            if orchestrator_id in visiting:
                cycle = stack[stack.index(orchestrator_id) :] + [orchestrator_id]
                raise OrchestratorRegistryError(f"orchestrator cycle detected: {' -> '.join(cycle)}")
            visiting.add(orchestrator_id)
            stack.append(orchestrator_id)
            for child in graph.get(orchestrator_id, ()):
                visit(child)
            stack.pop()
            visiting.remove(orchestrator_id)
            visited.add(orchestrator_id)

        for orchestrator_id in sorted(graph):
            visit(orchestrator_id)

    def _strict_child_type_check(self) -> None:
        """Warn when ASTRID_STRICT_CHILD_TYPES is set and an orchestrator's
        input artifact_type is not produced by any child capability.

        This is a best-effort development aid — orchestrators often
        receive inputs from external sources (e.g. user-supplied files),
        so missing matches are warnings, not errors.
        """
        if not os.environ.get("ASTRID_STRICT_CHILD_TYPES"):
            return

        for orchestrator in (self._resolve_entry(entry) for entry in self._entries.values()):
            child_types = self._child_output_types.get(orchestrator.id, {})
            if not child_types:
                continue
            # Union of all child output artifact types.
            all_child_output_types: set[str] = set()
            for typeset in child_types.values():
                all_child_output_types.update(typeset)

            for port in orchestrator.inputs:
                if port.artifact_type and port.artifact_type not in all_child_output_types:
                    logger.warning(
                        "ASTRID_STRICT_CHILD_TYPES: orchestrator %r input %r has "
                        "artifact_type=%r not found in any child's output types. "
                        "Child output types: %s",
                        orchestrator.id,
                        port.name,
                        port.artifact_type,
                        sorted(all_child_output_types),
                    )

def load_default_registry(
    *,
    executor_registry: ExecutorRegistry | None = None,
    banodoco_config: Any | None = None,
    project_root: str | Path = REPO_ROOT,
    extra_pack_roots: tuple[str, ...] = (),
    discovered_packs: tuple[DiscoveredPack, ...] | None = None,
) -> OrchestratorRegistry:
    """Assemble and validate, optionally reusing an admitted per-call snapshot."""
    active_executor_registry = executor_registry
    if active_executor_registry is None:
        active_executor_registry = load_default_executor_registry(
            banodoco_config=banodoco_config,
            project_root=project_root,
            extra_pack_roots=extra_pack_roots,
            **({"discovered_packs": discovered_packs} if discovered_packs is not None else {}),
        )
    packs = (
        tuple(item.pack for item in discovered_packs)
        if discovered_packs is not None
        else _discover_orchestrator_packs(project_root=project_root, extra_pack_roots=extra_pack_roots)
    )
    registry = OrchestratorRegistry(
        executor_registry=active_executor_registry,
    )
    for orchestrator in _load_pack_orchestrators_from_packs(packs):
        if orchestrator.metadata.get("local_orchestrator") is True and orchestrator.id in active_executor_registry.as_mapping():
            raise OrchestratorRegistryError("local orchestrator collides with executor identity")
        registry.register(orchestrator)
    registry.validate_all(executor_registry=active_executor_registry)
    return registry


def load_pack_orchestrators(
    *,
    project_root: str | Path = REPO_ROOT,
    extra_pack_roots: tuple[str, ...] = (),
) -> tuple[OrchestratorDefinition, ...]:
    packs = _discover_orchestrator_packs(
        project_root=project_root,
        extra_pack_roots=extra_pack_roots,
    )
    return _load_pack_orchestrators_from_packs(packs)


def _discover_orchestrator_packs(
    *,
    project_root: str | Path,
    extra_pack_roots: tuple[str, ...],
) -> tuple[Any, ...]:
    return discover_packs_ordered(
        project_root=project_root,
        extra_pack_roots=extra_pack_roots,
        discover_packs_fn=discover_packs,
    )


def _load_pack_orchestrators_from_packs(
    packs: Iterable[Any],
) -> tuple[OrchestratorDefinition, ...]:
    packs = tuple(packs)
    orchestrators: list[OrchestratorDefinition] = []
    for pack in packs:
        # Per-pack fault tolerance: one broken content manifest (e.g. an
        # external pack whose orchestrator.yaml fails schema
        # validation) must not abort the whole discovery/invoke. The pack is
        # skipped with a warning; pack-alignment failures
        # (``PackValidationError`` from ``validate_content_id_in_pack``)
        # still propagate — a misplaced id is a packaging contract breach,
        # not a bad manifest.
        try:
            for root in iter_orchestrator_roots(pack):
                for orchestrator in load_folder_orchestrators(root):
                    validate_content_id_in_pack(orchestrator.id, pack, content_type="orchestrator")
                    orchestrators.append(_attach_pack_metadata(orchestrator, pack.id, content_root=root))
        except (OrchestratorValidationError, ManifestParseError) as exc:
            logger.warning(
                "skipping pack %r: orchestrator manifests failed validation: %s",
                getattr(pack, "id", pack),
                exc,
            )
            continue
    from astrid.core.execution.executor.actions import action_is_local_orchestrator, action_orchestrator_definition
    for pack in packs:
        for local_id, action in getattr(pack, "actions", {}).items():
            if not action_is_local_orchestrator(action):
                continue
            definition = action_orchestrator_definition(pack, local_id, action)
            if any(item.id == definition.id for item in orchestrators):
                raise OrchestratorRegistryError("local orchestrator collides with orchestrator identity")
            orchestrators.append(definition)
    return tuple(orchestrators)


def _attach_pack_metadata(
    orchestrator: OrchestratorDefinition,
    pack_id: str,
    *,
    content_root: Path | None = None,
) -> OrchestratorDefinition:
    metadata = dict(orchestrator.metadata)
    metadata["source"] = "pack"
    metadata["source_pack"] = pack_id
    metadata["priority"] = 30
    if content_root is not None:
        metadata["content_root"] = str(content_root)
    return validate_orchestrator_definition(replace(orchestrator, metadata=metadata))


__all__ = [
    "OrchestratorRegistry",
    "OrchestratorRegistryError",
    "load_pack_orchestrators",
    "load_default_registry",
]
