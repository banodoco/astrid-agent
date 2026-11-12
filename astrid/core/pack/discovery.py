"""Shared pack discovery across executor, orchestrator, and element registries.

Each registry consumes the same manifest-ledger discovery walk. The walk is
read-only: it can see source-tree packs, the bundled project ``local`` pack,
managed sources, the per-user ``personal`` collection, explicit extra roots,
and read-only roots named by
``ASTRID_PACKS_PATH``. It never creates a project pack and never consults an
    mutable user pack store.

Fault tolerance: the ``managed`` / ``extra`` / ``env`` layers are
external by definition. A pack whose manifest fails to load is skipped
individually with a logged warning so one broken external pack cannot hide its
valid neighbors. The source-tree scan stays strict: first-party packs must
always load.
"""

from __future__ import annotations

import hashlib
import logging
import os
import stat
from contextlib import contextmanager
from contextvars import ContextVar
from copy import deepcopy
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

from astrid.core.foundation.paths import PACKAGE_ROOT, REPO_ROOT
from astrid.core.pack import (
    PackDefinition,
    PackValidationError,
    discover_packs,
    iter_element_roots,
    iter_executor_roots,
    iter_orchestrator_roots,
    loader,
)
from astrid.core.pack.canonical import (
    CanonicalPackEntry,
    CanonicalPackValidationError,
    ExternalPackSource,
    read_normalize_validate,
)
from astrid.core.pack.loader import discover_packs as _default_discover_packs

DiscoverPacksFn = Callable[..., "tuple[PackDefinition, ...]"]

# Source-kind labels in discovery (and therefore priority) order.
SOURCE_KINDS: tuple[str, ...] = ("source", ExternalPackSource.LOCAL.value, "managed", "extra", "env")
# The project ``local`` pack id. Only ``<project_root>/astrid/packs/local`` is
# admitted under it, as the ``local`` layer; a folder or pack with this id in any
# other root is never loaded. Editable user effects live in ``personal``.
LOCAL_PACK_ID = ExternalPackSource.LOCAL.value
MANAGED_SOURCE_KIND = "managed"
ASTRID_PACKS_PATH_ENV = "ASTRID_PACKS_PATH"

# Opt-in, request-local reuse for generation input resolution. Other discovery
# callers continue to scan normally. Never retain an inventory across requests.
_discovery_cache: ContextVar[dict | None] = ContextVar("pack_discovery_cache", default=None)


@contextmanager
def pack_discovery_scope():
    """Reuse admitted packs only while their complete discovery inputs match."""
    token = _discovery_cache.set({})
    try:
        yield
    finally:
        _discovery_cache.reset(token)


def _tree_fingerprint(root: Path) -> str:
    """Read contents, names and file kinds, including untracked resources.

    A stat-only key misses same-size edits with restored timestamps. Tracking
    the entire tree also covers added manifests, alternate manifests, deleted
    resources and new symlinks that canonical admission must reject.
    """
    digest = hashlib.sha256()

    def add(value: bytes) -> None:
        digest.update(len(value).to_bytes(8, "big"))
        digest.update(value)

    def visit(path: Path) -> None:
        add(os.fsencode(path.relative_to(root)))
        try:
            mode = path.lstat().st_mode
        except FileNotFoundError:
            add(b"missing")
            return
        add(str(mode).encode())
        if stat.S_ISLNK(mode):
            add(os.fsencode(os.readlink(path)))
        elif stat.S_ISDIR(mode):
            for child in sorted(path.iterdir()):
                visit(child)
        elif stat.S_ISREG(mode):
            with path.open("rb") as stream:
                add(hashlib.file_digest(stream, "sha256").digest())
        else:
            raise OSError(f"cannot fingerprint non-regular pack resource: {path}")

    visit(root)
    return digest.hexdigest()


def _discovery_fingerprint(
    project_root, extra_pack_roots, managed_inventory, scan, *, include_hidden=False,
    personal_packs_root=None,
):
    from astrid.core.pack import loader

    # Preserve registry-specific scanner seams; their arbitrary inputs cannot
    # be fingerprinted by the normal filesystem discovery implementation.
    if scan is not loader.discover_packs:
        return None
    project_root = Path(project_root).resolve()
    raw_env = os.environ.get(ASTRID_PACKS_PATH_ENV, "")
    roots = [loader.packs_root(), Path.home() / "Astrid" / "packs", project_root / "astrid" / "packs",
             Path(__file__).with_name("schemas")]
    if personal_packs_root is not None:
        roots.append(Path(personal_packs_root).expanduser())
    roots.extend(source.pack_root for source in managed_inventory.sources)
    for raw_root in (*extra_pack_roots, *filter(None, raw_env.split(os.pathsep))):
        root = Path(raw_root).expanduser()
        roots.append(root if root.is_absolute() else project_root / root)
    try:
        trees = tuple((str(root), str(root.resolve()), _tree_fingerprint(root.resolve()))
                      for root in dict.fromkeys(roots))
    except OSError:
        return None  # Unreadable/racing inputs use ordinary admission and its errors.
    return (str(project_root), tuple(extra_pack_roots), raw_env, managed_inventory.identity,
            bool(include_hidden), str(personal_packs_root) if personal_packs_root is not None else None, trees)


def reuse_pack_projection(projection: Callable[[], Any]) -> Any:
    """Reuse a projection of default pack discovery inside the current scope.

    The projection must depend only on the default discovery roots. Explicit
    discovery objects and projections with other inputs must not use this seam.
    Managed custody is verified on every lookup, just as for pack metadata.
    """
    cache = _discovery_cache.get()
    if cache is None:
        return projection()
    from astrid.core.pack.source_setup import active_source_inventory

    def fingerprint():
        return _discovery_fingerprint(REPO_ROOT, (), active_source_inventory(), discover_packs)

    before = fingerprint()
    key = (projection, before)
    if before is not None and key in cache:
        return deepcopy(cache[key])
    result = projection()
    if before is not None and before == fingerprint():
        cache[key] = deepcopy(result)
    return result


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
        from astrid.core.pack.transaction import group_read_lock

        root = Path(raw_root).expanduser()
        if not root.is_absolute():
            root = project_root / root
        with group_read_lock(root.resolve()):
            scan_locked(root, source)

    def scan_locked(raw_root: str | Path, source: ExternalPackSource) -> None:
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
                # ``local`` is admitted only from the project tree (below).
                if child.name in {LOCAL_PACK_ID, "__pycache__"}:
                    continue
                if manifest.is_file():
                    add(manifest, source)
            except Exception as exc:  # noqa: BLE001 - external roots are fault-tolerant
                logging.getLogger(__name__).warning(
                    "skipping %s pack %s: manifest failed to load: %s", source.value, manifest, exc
                )

    local = project_root / "astrid" / "packs" / LOCAL_PACK_ID / "pack.yaml"
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
    include_hidden: bool = False,
    personal_packs_root: str | Path | None = None,
) -> tuple[DiscoveredPack, ...]:
    """Return discovered packs in layered priority order.

    Layers, in order: source-tree packs (excluding ``local``), the project's
    ``astrid/packs/local`` pack when it has an authored manifest, validated
    managed roots, the personal collection, explicit extra pack roots, and
    ``ASTRID_PACKS_PATH`` roots. ``local`` is never admitted from any other root.

    *discover_packs_fn* overrides the source/local/extra layer scanner; callers
    pass their own module-level ``discover_packs`` so the historical per-registry
    test seam (``mock.patch("astrid.core.<x>.registry.discover_packs")``) keeps
    working. Defaults to :func:`astrid.core.pack.discover_packs`.
    """
    scan = discover_packs_fn if discover_packs_fn is not None else discover_packs
    # Bind the personal collection once at the runtime discovery boundary.
    # Every layer classification, compatibility pass, loader scan, and cache
    # identity below must use this exact root.
    if personal_packs_root is None:
        from astrid.core.pack.personal import resolve_personal_packs_root

        personal_packs_root = resolve_personal_packs_root()
    selected_personal_root = (
        Path(personal_packs_root).expanduser().resolve(strict=False)
        if personal_packs_root is not None else None
    )

    discovered: list[DiscoveredPack] = []
    scanned_external_roots: set[Path] = set()
    pack_names: dict[str, tuple[Path, str | None]] = {}

    def _register_pack_name(path: Path, pack_id: str | None = None) -> tuple[Path, str | None] | None:
        """Record names before filtering, retaining ids for collision policy."""
        previous = pack_names.get(path.name)
        if previous is None:
            pack_names[path.name] = (path, pack_id)
        elif previous[0].resolve() == path.resolve():
            if pack_id is not None:
                pack_names[path.name] = (path, pack_id)
            return None
        elif pack_id is not None and previous[1] is not None and pack_id != previous[1]:
            raise PackValidationError(
                f"duplicate pack directory name {path.name!r} across {previous[0]} and {path}"
            )
        return previous

    def _add(pack: PackDefinition, source_kind: str, *, source_record: Any | None = None) -> None:
        if pack.id == "personal":
            if selected_personal_root is not None and pack.root.resolve() == (selected_personal_root / "personal").resolve():
                source_kind = "personal"
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

    # The managed source state can identify its roots without opening their
    # manifests. Reserve all such directory names (and the regular discovery
    # roots) before active_source_inventory verifies any selected checkout.
    try:
        from astrid.core.pack.source_setup import active_sources

        provisional_managed_sources = active_sources(verify=False)
    except ImportError:
        provisional_managed_sources = ()
    # Explicit extras are caller ordered and can precede bundled roots. The
    # admission result is identical regardless of which selected root is seen
    # first, because no manifest is consulted during this pass.
    provisional_roots: list[str | Path] = [*extra_pack_roots, loader.packs_root()]
    provisional_roots.extend(source.pack_root for source in provisional_managed_sources)
    if scan is _default_discover_packs:
        provisional_roots.append(Path.home() / "Astrid" / "packs")
    if selected_personal_root is not None:
        provisional_roots.append(selected_personal_root)
    provisional_roots.extend(root for root in os.environ.get(ASTRID_PACKS_PATH_ENV, "").split(os.pathsep) if root)
    provisional_paths: dict[str, Path] = {}
    provisional_scanned: set[Path] = set()
    canonical_core = Path(loader.packs_root()).expanduser().resolve()
    for raw_root in provisional_roots:
        root = Path(raw_root).expanduser()
        if not root.is_absolute():
            root = Path(project_root) / root
        root = root.resolve()
        if root in provisional_scanned or not root.is_dir():
            continue
        provisional_scanned.add(root)
        try:
            children = (root,) if (root / "pack.yaml").is_file() else tuple(root.iterdir())
        except OSError as exc:
            _LOGGER.warning("skipping unreadable ownership root %s: %s", root, exc)
            continue
        for child in children:
            if not child.is_dir() or child.name == "__pycache__":
                continue
            previous = provisional_paths.get(child.name)
            if previous is None or previous.resolve() == child.resolve():
                provisional_paths[child.name] = child
                continue
            if previous.resolve().parent == canonical_core or child.resolve().parent == canonical_core:
                raise PackValidationError(
                    f"duplicate pack id {child.name!r}: pack {child.name!r} conflicts with a core pack "
                    f"across {previous} and {child}"
                )
            if child.name == "personal" and selected_personal_root is not None:
                canonical_personal = (selected_personal_root / "personal").resolve()
                if previous.resolve() != canonical_personal or child.resolve() != canonical_personal:
                    raise PackValidationError(
                        "pack id 'personal' is reserved for the canonical personal root"
                    )
            raise PackValidationError(
                f"duplicate pack id/directory name {child.name!r} across {previous} and {child}"
            )

    try:
        from astrid.core.pack.source_setup import active_source_inventory

        managed_inventory = active_source_inventory()
    except ImportError:
        managed_inventory = None

    cache = _discovery_cache.get()
    fingerprint = None
    if cache is not None and managed_inventory is not None:
        # active_source_inventory verifies revision, cleanliness, manifest and
        # tree digests even when an admitted inventory can be reused below.
        fingerprint = _discovery_fingerprint(
            project_root, extra_pack_roots, managed_inventory, scan,
            include_hidden=include_hidden, personal_packs_root=selected_personal_root,
        )
        if fingerprint is not None and fingerprint in cache:
            return deepcopy(cache[fingerprint])

    managed_by_root = {
        source.pack_root.resolve(): source
        for source in (managed_inventory.sources if managed_inventory is not None else ())
    }

    def _resolve_pack_root(raw_root: str | Path) -> Path:
        candidate = Path(raw_root).expanduser()
        if not candidate.is_absolute():
            candidate = Path(project_root) / candidate
        return candidate.resolve()

    def _admit_root_names(raw_root: str | Path, *, label: str) -> None:
        """Reserve every directory basename before any manifest is opened."""
        root = _resolve_pack_root(raw_root)
        if not root.is_dir():
            return
        try:
            children = (root,) if (root / "pack.yaml").is_file() else tuple(root.iterdir())
        except OSError as exc:
            _LOGGER.warning("skipping unreadable %s root %s: %s", label, root, exc)
            return
        for child in children:
            if not child.is_dir() or child.name == "__pycache__":
                continue
            previous = _register_pack_name(child)
            if previous is not None:
                previous_root = previous[0].resolve().parent
                current_root = child.resolve().parent
                if previous_root != current_root:
                    if (previous_root == _resolve_pack_root(loader.packs_root())
                            or current_root == _resolve_pack_root(loader.packs_root())):
                        raise PackValidationError(
                            f"duplicate pack id {child.name!r}: pack {child.name!r} conflicts with a core pack "
                            f"across {previous[0]} and {child}"
                        )
                    raise PackValidationError(
                        f"duplicate pack id/directory name {child.name!r} across {previous[0]} and {child}"
                    )

    # Name-only ownership admission spans every selected root. This deliberately
    # includes hidden, malformed, and manifestless directories; the loaders below
    # may filter visibility or isolate a bad manifest only after this pass.
    core_root_for_names = loader.packs_root()
    name_roots: list[tuple[str | Path, str]] = [(core_root_for_names, "bundled")]
    if managed_inventory is not None:
        name_roots.extend((source.pack_root, "managed") for source in managed_inventory.sources)
    if scan is _default_discover_packs:
        name_roots.append((Path.home() / "Astrid" / "packs", "extra"))
    if selected_personal_root is not None:
        name_roots.append((selected_personal_root, "extra"))
    name_roots.extend((root, "extra") for root in extra_pack_roots)
    name_roots.extend((root, "env") for root in os.environ.get(ASTRID_PACKS_PATH_ENV, "").split(os.pathsep) if root)
    admitted_name_roots: set[Path] = set()
    for raw_root, label in name_roots:
        canonical = _resolve_pack_root(raw_root)
        if canonical in admitted_name_roots:
            continue
        admitted_name_roots.add(canonical)
        _admit_root_names(canonical, label=label)

    def _scan_external_root(raw_root: str | Path, source_kind: str) -> None:
        from astrid.core.pack.transaction import group_read_lock

        resolved = _resolve_pack_root(raw_root)
        with group_read_lock(resolved):
            _scan_external_root_locked(resolved, source_kind)

    def _scan_external_root_locked(raw_root: str | Path, source_kind: str) -> None:
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
        for child in children:
            if not child.is_dir() or child.name == "__pycache__":
                continue
            previous_name = _register_pack_name(child)
            if child.name.startswith("."):
                continue
            try:
                manifest_path = pack_manifest_path(child)
            except (OSError, PackValidationError) as exc:
                if previous_name is not None:
                    raise PackValidationError(
                        f"duplicate pack directory name {child.name!r} across {previous_name[0]} and {child}"
                    ) from exc
                if loader._fail_closed(child.name, loader.REQUIRED_PACK_IDS):
                    raise PackValidationError(f"{child}: {exc}") from exc
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
                if previous_name is not None:
                    raise PackValidationError(
                        f"duplicate pack directory name {child.name!r} across {previous_name[0]} and {child}"
                    )
                continue
            try:
                pack = load_pack_manifest(
                    manifest_path,
                    expected_pack_id=managed_by_root[resolved].pack_id
                    if source_kind == MANAGED_SOURCE_KIND and resolved in managed_by_root
                    else None,
                )
            except Exception as exc:  # noqa: BLE001 - external roots are fault-tolerant
                if previous_name is not None:
                    raise PackValidationError(
                        f"duplicate pack directory name {child.name!r} across {previous_name[0]} and {child}"
                    ) from exc
                if loader._fail_closed(child.name, loader.REQUIRED_PACK_IDS):
                    raise PackValidationError(f"{child}: {exc}") from exc
                _LOGGER.warning(
                    "skipping %s pack %s: manifest failed to load: %s",
                    source_kind,
                    manifest_path,
                    " ".join(str(exc).split()),
                )
                continue
            if pack.id == LOCAL_PACK_ID:
                continue  # admitted only as the project ``local`` layer
            if previous_name is not None and previous_name[1] is None:
                raise PackValidationError(
                    f"duplicate pack directory name {child.name!r} across {previous_name[0]} and {child}"
                )
            if previous_name is not None and previous_name[1] != pack.id:
                raise PackValidationError(
                    f"duplicate pack directory name {child.name!r} across {previous_name[0]} and {child}"
                )
            _register_pack_name(child, pack.id)
            _add(
                pack,
                source_kind,
                source_record=managed_by_root.get(resolved)
                if source_kind == MANAGED_SOURCE_KIND
                else None,
            )

    # Collect every layer before compatibility checks, so dependencies may
    # resolve across roots. Preserve the injectable scanner seam for callers.
    source_packs = (loader.scan_packs(loader.packs_root(), strict=False, include_hidden=include_hidden,
                                      _validate=False, personal_packs_root=selected_personal_root).packs
                    if scan is _default_discover_packs else scan())
    # Hivemind is a runtime-owned core pack rather than an authorable pack
    # under ``astrid/packs``. Admit it through the same strict manifest loader
    # as source packs, and place it first so user/managed copies cannot shadow
    # the bundled implementation in registries that honor discovery order.
    core_hivemind_root = PACKAGE_ROOT / "core" / "hivemind"
    core_hivemind_manifest = core_hivemind_root / "pack.yaml"
    if core_hivemind_manifest.is_file():
        core_hivemind = loader.load_pack_manifest(
            core_hivemind_manifest,
            expected_pack_id="hivemind",
        )
        _add(core_hivemind, "source")
    for pack in source_packs:
        if pack.id in {LOCAL_PACK_ID, "hivemind"}:
            continue
        _register_pack_name(Path(pack.root), pack.id)
        _add(pack, "source")

    # Discovery is observational: only an explicitly-authored manifest at the
    # project's ``astrid/packs/local`` is eligible for the local layer.
    project_pack_root = (Path(project_root) / "astrid" / "packs").resolve()
    local_pack_root = project_pack_root / LOCAL_PACK_ID
    if project_pack_root.is_dir() and any(
        (local_pack_root / name).is_file() for name in ("pack.yaml", "pack.yml", "pack.json")
    ):
        # Only the built-in scanner guarantees identical strict admission for
        # these two calls when its actual default root is the project root.
        # Injected scanners can assign different authority to each argument.
        local_packs = (
            source_packs
            if scan is _default_discover_packs and loader.packs_root().resolve() == project_pack_root
            else scan(project_pack_root)
        )
        for pack in local_packs:
            if pack.id == LOCAL_PACK_ID and Path(pack.root).resolve() == local_pack_root.resolve():
                _register_pack_name(Path(pack.root), pack.id)
                _add(pack, ExternalPackSource.LOCAL.value)

    if managed_inventory is not None:
        for source in managed_inventory.sources:
            _scan_external_root(source.pack_root, MANAGED_SOURCE_KIND)

    if scan is _default_discover_packs:
        _scan_external_root(Path.home() / "Astrid" / "packs", "extra")
    if selected_personal_root is not None:
        _scan_external_root(selected_personal_root, "extra")
    raw_env_roots = os.environ.get(ASTRID_PACKS_PATH_ENV, "")
    if extra_pack_roots or raw_env_roots:
        for extra_root in extra_pack_roots:
            _scan_external_root(extra_root, "extra")
        for env_root in raw_env_roots.split(os.pathsep):
            if env_root == "":
                continue
            _scan_external_root(env_root, "env")

    from astrid.core.pack.ownership import first_pack_owner
    discovered = first_pack_owner(discovered)
    if not include_hidden:
        discovered = [item for item in discovered if item.pack.visibility != "hidden"]

    if scan is _default_discover_packs:
        records = {item.pack.id: managed_by_root[item.pack.root.resolve()]
                   for item in discovered if item.pack.root.resolve() in managed_by_root}
        admitted, _ = loader._isolate_incompatible_packs(
            [item.pack for item in discovered], source_records=records,
            personal_packs_root=selected_personal_root)
        admitted_roots = {pack.root.resolve() for pack in admitted}
        unique_roots: set[Path] = set()
        discovered = [item for item in discovered
                      if item.pack.root.resolve() in admitted_roots
                      and not (item.pack.root.resolve() in unique_roots
                               or unique_roots.add(item.pack.root.resolve()))]
    result = tuple(discovered)
    if fingerprint is not None and fingerprint == _discovery_fingerprint(
        project_root, extra_pack_roots, managed_inventory, scan,
        include_hidden=include_hidden, personal_packs_root=selected_personal_root,
    ):
        # Do not publish a snapshot if its inputs changed during admission.
        cache[fingerprint] = deepcopy(result)
    return result


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
    "pack_discovery_scope",
    "reuse_pack_projection",
    "discover_canonical_pack_metadata",
    "discover_canonical_packs_ordered",
    "discover_packs_ordered",
]
