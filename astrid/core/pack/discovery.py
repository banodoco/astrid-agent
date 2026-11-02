"""Shared pack discovery across executor, orchestrator, and element registries.

Each registry consumes the same manifest-ledger discovery walk. The walk is
read-only: it can see source-tree packs, explicit extra roots, and read-only
roots named by ``ASTRID_PACKS_PATH``. It never creates a project pack and
never consults a mutable user pack store.

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

from astrid.core.foundation.paths import REPO_ROOT
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
    unsafe_pack_paths,
)

DiscoverPacksFn = Callable[..., "tuple[PackDefinition, ...]"]

# Source-kind labels in discovery (and therefore priority) order.
SOURCE_KINDS: tuple[str, ...] = ("source", "managed", "extra", "env")
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


def _tree_fingerprint(
    root: Path, *, pack_root: bool = False, pack_collection: bool = True,
    source_root: bool = False, expected_pack_id: str | None = None,
    selected_roots: frozenset[Path] = frozenset(),
) -> str:
    """Fingerprint admitted declarations and narrowly defined admission inputs.

    Candidate manifests stay in the key so changes to admission, visibility,
    or alternate-manifest presence invalidate discovery. Only canonical
    resource handles of returned packs grant access to payload bytes. Declared
    path metadata observes repair/rejection transitions. The canonical whole-
    tree symlink rule contributes unsafe paths, never undeclared file bytes.
    """
    from astrid.core.pack import loader
    from astrid.core.pack.canonical import LEGACY_MANIFEST_NAMES

    digest = hashlib.sha256()

    def add(value: bytes) -> None:
        digest.update(len(value).to_bytes(8, "big"))
        digest.update(value)

    def visit(
        path: Path, *, content: bool = True, track_changes: bool = False, recurse: bool = True,
    ) -> None:
        add(os.fsencode(path.relative_to(root)))
        try:
            state = path.lstat()
            mode = state.st_mode
        except FileNotFoundError:
            add(b"missing")
            return
        add(str(mode).encode())
        if stat.S_ISLNK(mode):
            add(os.fsencode(os.readlink(path)))
        elif stat.S_ISDIR(mode):
            if not recurse:
                return
            for child in sorted(path.iterdir()):
                if content and child.is_dir() and (child.name.startswith(".") or child.name == "__pycache__"):
                    continue
                visit(child, content=content, track_changes=track_changes)
        elif stat.S_ISREG(mode) and content:
            with path.open("rb") as stream:
                add(hashlib.file_digest(stream, "sha256").digest())
        elif stat.S_ISREG(mode) and track_changes:
            # Full admission can reject descriptor contents. Until selected,
            # observe edits through filesystem metadata, never their bytes.
            add(str((state.st_ino, state.st_size, state.st_mtime_ns, state.st_ctime_ns)).encode())
        elif not stat.S_ISREG(mode):
            raise OSError(f"cannot fingerprint non-regular pack resource: {path}")

    def selected_pack(path: Path) -> CanonicalPackEntry | None:
        # Keep every scanner-recognized manifest name in the cache identity.
        # In particular, pack.yml appearing after a successful scan must
        # invalidate the cache so pack_manifest_path can enforce refusal.
        visit(path, content=False, recurse=False)
        manifest = path / "pack.yaml"
        visit(manifest, recurse=False)
        for legacy_name in LEGACY_MANIFEST_NAMES:
            # Canonical refusal depends on alternate presence, not its bytes.
            visit(path / legacy_name, content=False, recurse=False)
        try:
            manifest_path = loader.pack_manifest_path(path)
            if manifest_path is None:
                return None
            # Admission's whole-tree symlink rule is a safety input, not
            # authority to hash ordinary undeclared descendants.
            for unsafe in unsafe_pack_paths(path):
                visit(unsafe, content=False, recurse=False)
            entry = read_normalize_validate(
                manifest_path, source="local", resolve_resources=False,
                validate_resource_paths=False, expected_pack_id=expected_pack_id,
            )
            excluded = entry.definition.visibility == "hidden" or (
                source_root and entry.definition.schema_version != 3
            )
            for declared in entry.admission_input_paths():
                visit(declared, content=False, track_changes=not excluded, recurse=False)
        except Exception:  # Match scanner admission; its caller owns refusal/isolation.
            return None
        if excluded:
            return None
        return entry

    def payload(entry: CanonicalPackEntry) -> None:
        for handle in entry.resource_metadata():
            visit(handle.resolved, content=handle.file_kind == "file", recurse=False)

    def selected_collection(path: Path, *, source: bool) -> None:
        for child in sorted(path.iterdir()):
            if child.name.startswith(".") or child.name == "__pycache__":
                continue
            if child.is_symlink():
                # The strict source scanner examines directory symlinks. Keep
                # their lexical identity without following a target payload;
                # scanner refusal, rather than the cache, owns the outcome.
                if source:
                    visit(child, content=False)
                    # is_dir and manifest lookup are the only target state
                    # consulted by the scanner before refusing a pack link.
                    # Observe that metadata without traversing target content.
                    add(str(child.is_dir()).encode())
                    for name in ("pack.yaml", *sorted(LEGACY_MANIFEST_NAMES)):
                        visit(child / name, content=False, recurse=False)
                continue
            if not child.is_dir():
                continue
            entry = selected_pack(child)
            if entry is not None and entry.root in selected_roots:
                payload(entry)

    if pack_root:
        entry = selected_pack(root)
        if entry is not None and entry.root in selected_roots:
            payload(entry)
    elif pack_collection:
        add(os.fsencode(root.relative_to(root)))
        selected_collection(root, source=source_root)
    else:
        visit(root)
    return digest.hexdigest()


def _selection_key(project_root, extra_pack_roots, managed_inventory):
    return (
        "selected-roots", str(Path(project_root).resolve()), tuple(extra_pack_roots),
        os.environ.get(ASTRID_PACKS_PATH_ENV, ""), managed_inventory.identity,
    )


def _discovery_fingerprint(
    project_root, extra_pack_roots, managed_inventory, scan, *, selected_roots=None,
):
    from astrid.core.pack import loader

    # Preserve registry-specific scanner seams; their arbitrary inputs cannot
    # be fingerprinted by the normal filesystem discovery implementation.
    if scan is not loader.discover_packs:
        return None
    project_root = Path(project_root).resolve()
    if selected_roots is None:
        cache = _discovery_cache.get()
        selection = (cache or {}).get(
            _selection_key(project_root, extra_pack_roots, managed_inventory),
        )
        metadata = _discovery_fingerprint(
            project_root, extra_pack_roots, managed_inventory, scan,
            selected_roots=frozenset(),
        )
        if selection is None or selection[0] != metadata:
            # Previous selection grants no payload access after an observed
            # edit. Re-admit first, including descriptor content validation.
            return metadata
        selected_roots = selection[1]
    raw_env = os.environ.get(ASTRID_PACKS_PATH_ENV, "")
    # The project's former ``astrid/packs`` tree is not a discovery input.
    # Only the bundled source root and explicitly selected external roots
    # participate in admission and therefore in cache identity.
    roots = [(loader.packs_root(), True, False, True, None), (Path(__file__).with_name("schemas"), False, False, False, None)]
    roots.extend((source.pack_root, False, True, False, source.pack_id) for source in managed_inventory.sources)
    for raw_root in (*extra_pack_roots, *filter(None, raw_env.split(os.pathsep))):
        root = Path(raw_root).expanduser()
        resolved = root if root.is_absolute() else project_root / root
        roots.append((resolved, not (resolved / "pack.yaml").is_file(), (resolved / "pack.yaml").is_file(), False, None))
    try:
        trees = tuple(
            (
                str(root), str(root.resolve()),
                _tree_fingerprint(
                    root.resolve(), pack_root=is_pack, pack_collection=is_collection,
                    source_root=is_source, expected_pack_id=expected_pack_id,
                    selected_roots=selected_roots,
                ),
            )
            for root, is_collection, is_pack, is_source, expected_pack_id in dict.fromkeys(roots)
        )
    except (OSError, CanonicalPackValidationError):
        return None  # Unreadable/racing inputs use ordinary admission and its errors.
    return (str(project_root), tuple(extra_pack_roots), raw_env, managed_inventory.identity, trees)


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
    (source < managed < extra < env) and is distinct from the per-content
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
    """Discover admitted v2/v3 packs through managed/extra/env.

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
        metadata = read_normalize_validate(
            path, source=source, resolve_resources=False,
            expected_pack_id=source_record.pack_id if source_record is not None else None,
        )
        if metadata.definition.visibility == "hidden":
            return
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
                if child.name == "__pycache__":
                    continue
                if manifest.is_file():
                    add(manifest, source)
            except Exception as exc:  # noqa: BLE001 - external roots are fault-tolerant
                logging.getLogger(__name__).warning(
                    "skipping %s pack %s: manifest failed to load: %s", source.value, manifest, exc
                )

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

    Layers, in order: source-tree packs, validated managed roots, explicit
    extra pack roots, and ``ASTRID_PACKS_PATH`` roots. User personal packs are
    resolved through the explicit personal-pack contract, not a project path
    selected by discovery.

    *discover_packs_fn* overrides the source layer scanner; callers
    pass their own module-level ``discover_packs`` so the historical per-registry
    test seam (``mock.patch("astrid.core.<x>.registry.discover_packs")``) keeps
    working. Defaults to :func:`astrid.core.pack.discover_packs`.
    """
    scan = discover_packs_fn if discover_packs_fn is not None else discover_packs
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

    cache = _discovery_cache.get()
    fingerprint = None
    metadata_fingerprint = None
    if cache is not None and managed_inventory is not None:
        # active_source_inventory verifies revision, cleanliness, manifest and
        # tree digests even when an admitted inventory can be reused below.
        fingerprint = _discovery_fingerprint(project_root, extra_pack_roots, managed_inventory, scan)
        if fingerprint is not None and fingerprint in cache:
            return deepcopy(cache[fingerprint])
        metadata_fingerprint = _discovery_fingerprint(
            project_root, extra_pack_roots, managed_inventory, scan,
            selected_roots=frozenset(),
        )

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
        candidates: list[PackDefinition] = []
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
                pack = loader.load_pack_metadata(
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
            candidates.append(pack)
        for candidate in candidates:
            try:
                pack = load_pack_manifest(
                    candidate.manifest_path,
                    expected_pack_id=managed_by_root[resolved].pack_id
                    if source_kind == MANAGED_SOURCE_KIND and resolved in managed_by_root
                    else None,
                )
            except Exception as exc:  # noqa: BLE001 - external roots are fault-tolerant
                _LOGGER.warning(
                    "skipping %s pack %s: manifest failed to load: %s",
                    source_kind, candidate.manifest_path, exc,
                )
                continue
            if pack.visibility == "hidden":
                continue
            _add(
                pack,
                source_kind,
                source_record=managed_by_root.get(resolved)
                if source_kind == MANAGED_SOURCE_KIND
                else None,
            )

    source_packs = scan()
    for pack in source_packs:
        _add(pack, "source")

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

    result = tuple(discovered)
    if metadata_fingerprint is not None and metadata_fingerprint == _discovery_fingerprint(
        project_root, extra_pack_roots, managed_inventory, scan, selected_roots=frozenset(),
    ):
        selected_roots = frozenset(row.pack.root for row in result)
        selected_fingerprint = _discovery_fingerprint(
            project_root, extra_pack_roots, managed_inventory, scan, selected_roots=selected_roots,
        )
        if selected_fingerprint is not None and selected_fingerprint == _discovery_fingerprint(
            project_root, extra_pack_roots, managed_inventory, scan, selected_roots=selected_roots,
        ):
            # Only real admission owns this selection. Never infer payload
            # ownership from visibility or metadata preflight alone.
            cache[_selection_key(project_root, extra_pack_roots, managed_inventory)] = (
                metadata_fingerprint, selected_roots,
            )
            cache[selected_fingerprint] = deepcopy(result)
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
