"""Shared pack discovery across executor, orchestrator, and element registries.

Each registry consumes the same manifest-ledger discovery walk. The walk is
read-only: it can see source-tree packs, an already-materialized project
``local`` pack, explicit extra roots, and read-only roots named by
``ASTRID_PACKS_PATH``. It never creates a project pack and never consults an
    mutable user pack store.

Fault tolerance: the ``managed`` / ``extra`` / ``env`` layers are
external by definition. A pack whose manifest fails to load is skipped
individually with a logged warning so one broken external pack cannot hide its
valid neighbors. The source-tree scan stays strict: first-party packs must
always load.
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

from astrid.core.foundation.paths import REPO_ROOT
from astrid.core.pack import (
    PackDefinition,
    PackValidationError,
    discover_packs,
    iter_element_roots,
    iter_executor_roots,
    iter_orchestrator_roots,
)
from astrid.core.pack.canonical import (
    CanonicalPackEntry,
    CanonicalPackValidationError,
    ExternalPackSource,
    read_normalize_validate,
)

DiscoverPacksFn = Callable[..., "tuple[PackDefinition, ...]"]

# Source-kind labels in discovery (and therefore priority) order.
SOURCE_KINDS: tuple[str, ...] = ("source", "local", "managed", "extra", "env")
MANAGED_SOURCE_KIND = "managed"
ASTRID_PACKS_PATH_ENV = "ASTRID_PACKS_PATH"


@dataclass(frozen=True)
class DiscoveredPack:
    """A pack located by discovery plus where it came from.

    ``priority_index`` is the position of this pack in the ordered discovery
    list; lower indices were discovered first. It encodes layer precedence
    (source < local < managed < extra < env) and is distinct from the per-content
    ``metadata["priority"]`` value that registries use to pick winners.
    """

    pack: PackDefinition
    source_kind: str
    priority_index: int
    source_revision: str | None = None
    source_manifest_sha256: str | None = None
    source_tree_sha256: str | None = None
    source_inventory_identity: str | None = None

    @property
    def id(self) -> str:
        return self.pack.id

    @property
    def pack_dir(self) -> Path:
        return self.pack.root

    def executor_roots(self) -> tuple[Path, ...]:
        return iter_executor_roots(self.pack)

    def orchestrator_roots(self) -> tuple[Path, ...]:
        return iter_orchestrator_roots(self.pack)

    def element_roots(self, *, kind: str | None = None):
        return iter_element_roots(self.pack, kind=kind)

    @property
    def documentation_path(self) -> Path | None:
        """The singular authored documentation file declared by this pack."""
        raw_path = self.pack.documentation.get("path")
        if not raw_path:
            return None
        root = self.pack_dir.resolve()
        relative = Path(raw_path)
        resolved = (root / relative).resolve()
        if relative.is_absolute() or not resolved.is_relative_to(root):
            raise PackValidationError("pack.documentation.path must stay within the pack root")
        return resolved

    def skill_roots(self) -> tuple[Path, ...]:
        """Only the declared pack skill directory; actions never export skills.

        Manifestless ``_core`` remains a separate shell owned by skill discovery.
        """
        if self.pack.documentation.get("kind") != "skill":
            return ()
        path = self.documentation_path
        return (path.parent,) if path is not None else ()


@dataclass(frozen=True)
class CanonicalDiscoveredPack:
    """An admitted v2/v3 pack from a read-only external discovery layer."""

    entry: CanonicalPackEntry
    source_kind: str
    priority_index: int
    source_revision: str | None = None
    source_manifest_sha256: str | None = None
    source_tree_sha256: str | None = None
    source_inventory_identity: str | None = None

    @property
    def id(self) -> str:
        return self.entry.id

    @property
    def pack_dir(self) -> Path:
        return self.entry.root


def discover_canonical_pack_metadata(
    *,
    project_root: str | Path = REPO_ROOT,
    extra_pack_roots: tuple[str, ...] = (),
) -> tuple[CanonicalDiscoveredPack, ...]:
    """Discover admitted v2/v3 packs through local/managed/extra/env.

    Source packs belong to the bundled canonical catalog. External failures
    are isolated per pack, matching :func:`discover_pack_metadata`.
    """
    project_root = Path(project_root).expanduser().resolve()
    discovered: list[CanonicalDiscoveredPack] = []
    seen: set[tuple[str, str]] = set()
    scanned: set[Path] = set()

    try:
        from astrid.core.pack.source_setup import active_source_inventory

        managed_inventory = active_source_inventory()
    except ImportError:
        managed_inventory = None
    def add(path: Path, source: ExternalPackSource, *, source_record: Any | None = None) -> None:
        entry = read_normalize_validate(
            path,
            source=source,
            expected_pack_id=source_record.pack_id if source_record is not None else None,
        )
        if entry.definition.visibility == "hidden":
            return
        key = (source.value, entry.id)
        if key in seen:
            raise CanonicalPackValidationError(
                f"duplicate canonical pack ID {entry.id!r} in {source.value}"
            )
        seen.add(key)
        discovered.append(
            CanonicalDiscoveredPack(
                entry,
                source.value,
                len(discovered),
                source_revision=source_record.revision if source_record is not None else None,
                source_manifest_sha256=source_record.manifest_sha256 if source_record is not None else None,
                source_tree_sha256=source_record.tree_sha256 if source_record is not None else None,
                source_inventory_identity=managed_inventory.identity
                if source_record is not None and managed_inventory is not None
                else None,
            )
        )

    def scan(raw_root: str | Path, source: ExternalPackSource) -> None:
        root = Path(raw_root).expanduser()
        if not root.is_absolute():
            root = project_root / root
        root = root.resolve()
        if root in scanned or not root.is_dir():
            return
        scanned.add(root)
        try:
            children = (
                (root,) if (root / "pack.yaml").is_file()
                else sorted(root.iterdir(), key=lambda p: p.name)
            )
        except OSError as exc:
            logging.getLogger(__name__).warning(
                "skipping unreadable %s root %s: %s", source.value, root, exc
            )
            return
        for child in children:
            manifest = child / "pack.yaml"
            try:
                if child.is_symlink() or not child.is_dir() or child.name.startswith("."):
                    continue
                if child.name in {"local", "__pycache__"}:
                    continue
                if manifest.is_file():
                    add(manifest, source)
            except Exception as exc:  # noqa: BLE001 - external roots are fault-tolerant
                logging.getLogger(__name__).warning(
                    "skipping %s pack %s: manifest failed to load: %s", source.value, manifest, exc
                )

    local = project_root / "astrid" / "packs" / "local" / "pack.yaml"
    if local.is_file():
        add(local, ExternalPackSource.LOCAL)
    if managed_inventory is not None:
        for source in managed_inventory.sources:
            root = source.pack_root.resolve()
            if root in scanned:
                continue
            scanned.add(root)
            try:
                add(root / "pack.yaml", ExternalPackSource.MANAGED, source_record=source)
            except Exception as exc:  # noqa: BLE001 - external roots are fault-tolerant
                logging.getLogger(__name__).warning(
                    "skipping managed pack %s: manifest failed to load: %s", root, exc
                )
    for root in extra_pack_roots:
        scan(root, ExternalPackSource.EXTRA)
    for root in os.environ.get(ASTRID_PACKS_PATH_ENV, "").split(os.pathsep):
        if root:
            scan(root, ExternalPackSource.ENV)
    return tuple(discovered)


def discover_canonical_packs_ordered(**kwargs: Any) -> tuple[CanonicalPackEntry, ...]:
    return tuple(item.entry for item in discover_canonical_pack_metadata(**kwargs))


def discover_pack_metadata(
    *,
    project_root: str | Path = REPO_ROOT,
    extra_pack_roots: tuple[str, ...] = (),
    discover_packs_fn: DiscoverPacksFn | None = None,
) -> tuple[DiscoveredPack, ...]:
    """Return discovered packs in layered priority order.

    Layers, in order: source-tree packs (excluding ``local``), an existing
    project-scoped ``local`` pack, validated managed roots, explicit extra pack
    roots (excluding ``local``), and ``ASTRID_PACKS_PATH`` roots (excluding
    ``local``).

    *discover_packs_fn* overrides the source/local/extra layer scanner; callers
    pass their own module-level ``discover_packs`` so the historical per-registry
    test seam (``mock.patch("astrid.core.<x>.registry.discover_packs")``) keeps
    working. Defaults to :func:`astrid.core.pack.discover_packs`.
    """
    scan = discover_packs_fn if discover_packs_fn is not None else discover_packs
    project_pack_root = (Path(project_root) / "astrid" / "packs").resolve()
    # Discovery is observational. Do not materialize a project-local pack just
    # because a registry was loaded; only an explicitly-authored manifest is
    # eligible for the local layer.
    local_pack_root = project_pack_root / "local"
    if not any(
        (local_pack_root / name).is_file() for name in ("pack.yaml", "pack.yml", "pack.json")
    ):
        local_pack_root = None

    discovered: list[DiscoveredPack] = []
    scanned_external_roots: set[Path] = set()

    def _add(pack: PackDefinition, source_kind: str, *, source_record: Any | None = None) -> None:
        metadata: dict[str, Any] = {}
        if source_record is not None:
            metadata = {
                "source_revision": source_record.revision,
                "source_manifest_sha256": source_record.manifest_sha256,
                "source_tree_sha256": source_record.tree_sha256,
                "source_inventory_identity": managed_inventory.identity,
            }
        discovered.append(
            DiscoveredPack(
                pack=pack,
                source_kind=source_kind,
                priority_index=len(discovered),
                **metadata,
            )
        )

    _LOGGER = logging.getLogger(__name__)

    try:
        from astrid.core.pack.source_setup import active_source_inventory

        managed_inventory = active_source_inventory()
    except ImportError:
        managed_inventory = None

    managed_by_root = {
        source.pack_root.resolve(): source
        for source in (managed_inventory.sources if managed_inventory is not None else ())
    }

    def _resolve_pack_root(raw_root: str | Path) -> Path:
        candidate = Path(raw_root).expanduser()
        if not candidate.is_absolute():
            candidate = Path(project_root) / candidate
        return candidate.resolve()

    def _scan_external_root(raw_root: str | Path, source_kind: str) -> None:
        """Scan one external root, isolating failures per pack manifest.

        A readable root contributes every loadable pack; one bad manifest
        skips only its own pack (with a warning), so valid neighbors in the
        same root survive. Only an unreadable root is skipped wholesale.
        """
        from astrid.core.pack import load_pack_manifest, pack_manifest_path

        resolved = _resolve_pack_root(raw_root)
        # An SDK caller may pass a root explicitly while the same canonical
        # root is also present in ASTRID_PACKS_PATH.  Scan it once, retaining
        # the higher-priority explicit ``extra`` provenance instead of
        # reporting the same pack twice (and potentially registering duplicate
        # renderer/element candidates).
        if resolved in scanned_external_roots:
            return
        scanned_external_roots.add(resolved)
        if not resolved.is_dir():
            return
        try:
            if (resolved / "pack.yaml").is_file():
                children = (resolved,)
            else:
                children = tuple(sorted(resolved.iterdir(), key=lambda path: path.name))
        except OSError as exc:
            # An unreadable external root (e.g. chmod 000) is skipped
            # wholesale with a warning — one dead root must not abort
            # discovery and hide every valid neighbor.
            _LOGGER.warning(
                "skipping unreadable %s root %s: %s",
                source_kind,
                resolved,
                exc,
            )
            return
        seen: dict[str, Path] = {}
        for child in children:
            try:
                if not child.is_dir() or child.name.startswith(".") or child.name == "__pycache__":
                    continue
                manifest_path = pack_manifest_path(child)
            except (OSError, PackValidationError) as exc:
                # Per-child pre-scan failures (e.g. an unreadable pack
                # directory) skip only that child, matching the
                # per-manifest isolation below.
                _LOGGER.warning(
                    "skipping %s pack %s: manifest lookup failed: %s",
                    source_kind,
                    child,
                    exc,
                )
                continue
            if manifest_path is None:
                continue
            try:
                pack = load_pack_manifest(
                    manifest_path,
                    expected_pack_id=managed_by_root[resolved].pack_id
                    if source_kind == MANAGED_SOURCE_KIND and resolved in managed_by_root
                    else None,
                )
            except Exception as exc:  # noqa: BLE001 - external roots are fault-tolerant
                _LOGGER.warning(
                    "skipping %s pack %s: manifest failed to load: %s",
                    source_kind,
                    manifest_path,
                    exc,
                )
                continue
            if pack.id == "local":
                continue
            if pack.visibility == "hidden":
                continue
            if pack.id in seen:
                _LOGGER.warning(
                    "skipping duplicate pack id %r in %s root %s (%s and %s)",
                    pack.id,
                    source_kind,
                    resolved,
                    seen[pack.id],
                    manifest_path,
                )
                continue
            seen[pack.id] = manifest_path
            _add(
                pack,
                source_kind,
                source_record=managed_by_root.get(resolved)
                if source_kind == MANAGED_SOURCE_KIND
                else None,
            )

    for pack in scan():
        if pack.id == "local":
            continue
        _add(pack, "source")

    if local_pack_root is not None and project_pack_root.is_dir():
        for pack in scan(project_pack_root):
            if pack.id == "local" and pack.root.resolve() == local_pack_root.resolve():
                _add(pack, "local")

    if managed_inventory is not None:
        for source in managed_inventory.sources:
            _scan_external_root(source.pack_root, MANAGED_SOURCE_KIND)

    raw_env_roots = os.environ.get(ASTRID_PACKS_PATH_ENV, "")
    if extra_pack_roots or raw_env_roots:
        for extra_root in extra_pack_roots:
            _scan_external_root(extra_root, "extra")
        for env_root in raw_env_roots.split(os.pathsep):
            if env_root == "":
                continue
            _scan_external_root(env_root, "env")

    return tuple(discovered)


def discover_packs_ordered(
    *,
    project_root: str | Path = REPO_ROOT,
    extra_pack_roots: tuple[str, ...] = (),
    discover_packs_fn: DiscoverPacksFn | None = None,
) -> tuple[Any, ...]:
    """Convenience wrapper returning just the ``PackDefinition`` objects.

    Drop-in replacement for the per-registry ``_discover_*_packs`` helpers,
    preserving their exact ordering.
    """
    return tuple(
        dp.pack
        for dp in discover_pack_metadata(
            project_root=project_root,
            extra_pack_roots=extra_pack_roots,
            discover_packs_fn=discover_packs_fn,
        )
    )


__all__ = [
    "ASTRID_PACKS_PATH_ENV",
    "MANAGED_SOURCE_KIND",
    "SOURCE_KINDS",
    "DiscoveredPack",
    "CanonicalDiscoveredPack",
    "discover_pack_metadata",
    "discover_canonical_pack_metadata",
    "discover_canonical_packs_ordered",
    "discover_packs_ordered",
]
