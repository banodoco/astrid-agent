"""Dependency-group installation with one durable directory publication.

The collection directory is the commit record, not a list of pack presence.
A private complete copy is validated/built before an atomic directory exchange.
The exchanged directory is retained for artifact custody; this module never
retires another attempt's publication or deletes an admitted generation.
"""
from __future__ import annotations

import hashlib
import json
import os
import shutil
import stat
import sys
import tempfile
from collections.abc import Callable, Mapping
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal
from uuid import uuid4

from astrid import pack_api, sdk
from astrid.core.pack.canonical import validate_canonical_pack
from astrid.core.pack.identity import pack_digest, read_pin
from astrid.core.pack.install import (
    _ID,
    _SHA,
    PackInstallError,
    _copy_folder,
    _directory,
    _fetch_tree,
    _git_source,
    _publish,
    _relative,
    _sync_tree,
    _tree,
    LockedPythonEnvironment,
    install_pack,
)
from astrid.pack_build import build_pack_assets
from astrid.pack_imports import ImportContract, check_pack_imports
from astrid.pack_validation import PackFacts, PinFacts, validate_pack

_STATE = ".group.json"
_BUNDLED_CORE_ROOT = Path(__file__).resolve().parents[2] / "packs"
Refresh = Callable[..., Mapping[str, Any]]
_HELD: ContextVar[dict[str, bool]] = ContextVar("pack_group_locks", default={})


@dataclass(frozen=True)
class GroupSnapshot:
    transaction_id: str | None
    revision: int
    paths: Mapping[str, Path]
    removals: tuple[str, ...]
    admissions: Mapping[str, Any]
    refresh_pending: bool


@dataclass(frozen=True)
class GroupResult:
    transaction_id: str
    revision: int
    changed_ids: tuple[str, ...]
    paths: Mapping[str, Path]
    refresh_pending: bool
    # The generation this call retired (now restorable by rollback). ``None``
    # when the call published nothing (``changed_ids == ()``, an equal-byte
    # update): nothing was retired, and the live rollback source is not this
    # call's to dispose of (LB-TXN r1 finding 3).
    retired_root: Path | None


class GroupCommitError(RuntimeError):
    """Publication occurred but its durability could not yet be confirmed.

    Recovery must sync the current collection before notifying. Never roll
    back individual packs or claim that the old group is still installed.
    """
    def __init__(self, transaction_id: str, revision: int, late_write_root: Path | None = None):
        message = "Group publication needs recovery; sync the collection and retry refresh."
        if late_write_root is not None:
            # LB-TXN r2 R2-1: a reversed exchange failed; the generation that
            # held a concurrent write is the retired one, not the live one.
            message += (f" A write made to the collection during this transaction may exist only in {late_write_root};"
                        " copy it back before discarding that generation.")
        super().__init__(message)
        self.transaction_id = transaction_id
        self.revision = revision
        self.late_write_root = late_write_root


def _json(value: Any) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":"),
                      ensure_ascii=False, allow_nan=False).encode("utf-8")

def _sync_directory(path: Path) -> None:
    fd = os.open(path, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def _root(value: str | Path) -> Path:
    root = Path(value).expanduser()
    if not root.is_absolute() or root == root.parent:
        raise PackInstallError("unsafe_path", "Supply an absolute collection directory below its parent.")
    _directory(root.parent, create=True)
    if root.exists() or root.is_symlink():
        _directory(root)
    return root


_SHIPPED_PACKS_ROOT = Path(__file__).resolve().parents[2] / "packs"


@dataclass
class _GroupCustody:
    """The descriptor and context owned by one actual lock acquisition."""

    fd: int
    path: Path
    key: str
    exclusive: bool
    token: Any
    attached: bool = True

    def consent(self, callback, preview, *, roots: tuple[Path, ...]) -> bool:
        import fcntl

        if not self.exclusive or not self.attached or _HELD.get().get(self.key) is not True:
            raise PackInstallError("transaction_busy", "Consent requires this transaction's own exclusive custody.")
        if set(_HELD.get()) != {self.key}:
            raise PackInstallError("transaction_busy", "Release caller-owned collection custody before requesting consent.")
        snapshots = tuple(_consent_tree_snapshot(root) for root in roots)
        identity = os.fstat(self.fd)
        # Remove only our context entry and unlock only our descriptor. A
        # caller-owned outer acquisition is rejected before reaching here.
        _HELD.reset(self.token)
        self.attached = False
        fcntl.flock(self.fd, fcntl.LOCK_UN)
        try:
            accepted = callback(preview)
        finally:
            # A callback must not leave its own custody context open. In that
            # case do not release it or deadlock trying to acquire against it.
            if self.key in _HELD.get():
                raise PackInstallError("transaction_busy", "Release callback-owned collection custody before returning consent.")
            self._verify_descriptor(identity)
            fcntl.flock(self.fd, fcntl.LOCK_EX)
            self.token = _HELD.set({**_HELD.get(), self.key: True})
            self.attached = True
            self._verify_descriptor(identity)
        if accepted is True:
            try:
                current = tuple(_consent_tree_snapshot(root) for root in roots)
            except (OSError, PackInstallError) as exc:
                raise PackInstallError("collection_changed", "Collection or staged bytes changed during consent; retry.") from exc
            if current != snapshots:
                raise PackInstallError("collection_changed", "Collection or staged bytes changed during consent; retry.")
        return accepted is True

    def _verify_descriptor(self, identity) -> None:
        try:
            _directory(self.path.parent)
            current = self.path.lstat()
            held = os.fstat(self.fd)
        except OSError as exc:
            raise PackInstallError("unsafe_path", "Collection custody changed during consent; retry.") from exc
        expected = (identity.st_dev, identity.st_ino)
        if (not stat.S_ISREG(current.st_mode) or current.st_nlink != 1
                or (current.st_dev, current.st_ino) != expected
                or (held.st_dev, held.st_ino) != expected or held.st_nlink != 1):
            raise PackInstallError("unsafe_path", "Collection custody changed during consent; retry.")


def _consent_tree_snapshot(root: Path) -> tuple[Any, ...]:
    """Include the commit record, target packs, personal bytes and empty dirs."""
    files = _tree(root)
    root_info = root.lstat()
    entries = []
    for path in sorted(root.rglob("*")):
        info = path.lstat()
        if not (stat.S_ISREG(info.st_mode) or stat.S_ISDIR(info.st_mode)):
            raise PackInstallError("unsafe_path", "Consent snapshots cannot contain links or special files.")
        entries.append((path.relative_to(root).as_posix(), info.st_mode,
                        hashlib.sha256(path.read_bytes()).hexdigest() if stat.S_ISREG(info.st_mode) else None))
    # Audit through the same safe-tree gate before and after reading all bytes.
    if files != _tree(root):
        raise PackInstallError("collection_changed", "Collection changed while preparing consent; retry.")
    return (root_info.st_dev, root_info.st_ino, root_info.st_mode, tuple(entries))


@contextmanager
def group_read_lock(root: str | Path, *, exclusive: bool = False):
    """Hold collection custody across a read or publication.

    The lock lives outside the exchanged directory. Read-only discovery never
    creates it. Transaction writers create it before creating a collection.
    All group mutations use this stable lock, including refresh acknowledgments.
    """
    try:
        import fcntl
    except ImportError as exc:
        raise PackInstallError("atomic_swap_unsupported", "Group custody requires a supported POSIX filesystem.") from exc
    path = Path(root)
    lock = path.parent / ("." + path.name + ".transaction.lock")
    if not exclusive and lock.parent.resolve() == _SHIPPED_PACKS_ROOT:
        # The shipped core packs are never a transaction collection; a read of
        # them takes (and even probes) no collection lock (PK-4R).
        yield
        return
    key = str(lock.absolute())
    held = _HELD.get()
    if key in held:
        if exclusive:
            raise PackInstallError("transaction_busy", "Release collection custody before starting another mutation.")
        yield
        return
    flags = os.O_NOFOLLOW | (os.O_RDWR | os.O_CREAT if exclusive else os.O_RDONLY)
    try:
        fd = os.open(lock, flags, 0o600)
    except FileNotFoundError:
        if exclusive:
            raise
        yield
        return
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
            raise PackInstallError("unsafe_path", "Group lock must be a regular file with one link.")
        fcntl.flock(fd, fcntl.LOCK_EX if exclusive else fcntl.LOCK_SH)
        token = _HELD.set({**held, key: exclusive})
        custody = _GroupCustody(fd, lock, key, exclusive, token)
        try:
            yield custody
        finally:
            if custody.attached:
                _HELD.reset(custody.token)
    finally:
        os.close(fd)


@contextmanager
def collection_write_custody(root: str | Path):
    """Exclusive collection custody for a direct (non-transaction) writer.

    Group transactions copy the whole collection, build, then exchange it, so a
    writer that edits collection bytes (for example the personal pack) must
    serialize with them on the same stable lock. The collection parent is
    created safely first (no symlinked or special components). Re-entry by a
    holder of exclusive custody is allowed; hold this only around local file
    writes, never across network, consent or build work.
    """
    root = _root(root)
    key = str((root.parent / ("." + root.name + ".transaction.lock")).absolute())
    if _HELD.get().get(key) is True:
        yield root
        return
    with group_read_lock(root, exclusive=True):
        yield root


def _state(root: Path) -> dict[str, Any]:
    path = root / _STATE
    if not path.exists() and not path.is_symlink():
        return {"version": 1, "revision": 0, "transaction_id": None,
                "removals": [], "admissions": {}, "refresh": None}
    if not stat.S_ISREG(path.lstat().st_mode):
        raise PackInstallError("unsafe_path", "Group state must be a regular file.")
    def unique(pairs):
        result = {}
        for key, item in pairs:
            if key in result:
                raise PackInstallError("invalid_state", "Group state contains duplicate fields; restore the committed record.")
            result[key] = item
        return result

    try:
        value = json.loads(path.read_text(encoding="utf-8"), object_pairs_hook=unique)
    except (OSError, ValueError) as exc:
        raise PackInstallError("invalid_state", "Group state cannot be read; restore a complete committed collection.") from exc
    fields = {"version", "revision", "transaction_id", "removals", "admissions", "refresh"}
    if (not isinstance(value, dict) or set(value) != fields or type(value["version"]) is not int or value["version"] != 1
            or type(value["revision"]) is not int or value["revision"] < 1
            or not isinstance(value["transaction_id"], str)
            or not isinstance(value["removals"], list)
            or any(not isinstance(i, str) or not _ID.fullmatch(i) for i in value["removals"])
            or value["removals"] != sorted(set(value["removals"]))
            or not isinstance(value["admissions"], dict)):
        raise PackInstallError("invalid_state", "Group state is malformed; restore a complete committed collection.")
    refresh = value["refresh"]
    if refresh is not None and (
            not isinstance(refresh, dict) or set(refresh) != {"transaction_id", "revision", "changed_ids"}
            or refresh["transaction_id"] != value["transaction_id"] or refresh["revision"] != value["revision"]
            or not isinstance(refresh["changed_ids"], list)
            or any(not isinstance(i, str) or not _ID.fullmatch(i) for i in refresh["changed_ids"])
            or refresh["changed_ids"] != sorted(set(refresh["changed_ids"]))):
        raise PackInstallError("invalid_state", "Pending refresh does not identify the committed group.")
    if len(value["transaction_id"]) != 32 or any(c not in "0123456789abcdef" for c in value["transaction_id"]):
        raise PackInstallError("invalid_state", "Group transaction id must identify a private retained generation.")
    return value


def _write_state(root: Path, state: Mapping[str, Any]) -> None:
    fd, name = tempfile.mkstemp(prefix=".group-state-", dir=root)
    try:
        with os.fdopen(fd, "wb") as stream:
            stream.write(_json(state) + b"\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(name, root / _STATE)
        _sync_directory(root)
    finally:
        Path(name).unlink(missing_ok=True)


def _paths(root: Path) -> dict[str, Path]:
    if not root.exists():
        return {}
    return {p.name: p for p in sorted(root.iterdir())
            if _ID.fullmatch(p.name) and p.is_dir() and not p.is_symlink()
            and (p / "pack.yaml").is_file()}


def read_group(packs_root: str | Path) -> GroupSnapshot:
    """Read one coherent inventory; hold group_read_lock for subsequent file IO."""
    root = Path(packs_root)
    with group_read_lock(root):
        state = _state(root)
        return GroupSnapshot(state["transaction_id"], state["revision"], _paths(root),
                             tuple(state["removals"]), state["admissions"], state["refresh"] is not None)


def read_retired_group(*, packs_root: str | Path, transaction_id: str) -> GroupSnapshot:
    """Read the old generation retained by this committing attempt.

    Artifact custodians bind old B/M admissions to these stable physical paths.
    This module does not delete them: disposal and lease draining have a separate
    owner. A failed unpublished attempt has no admitted retirement record.
    """
    if (not isinstance(transaction_id, str) or len(transaction_id) != 32
            or any(c not in "0123456789abcdef" for c in transaction_id)):
        raise PackInstallError("invalid_request", "Use the full committing transaction id to locate its retired group.")
    root = Path(packs_root)
    with group_read_lock(root):
        retained = _directory(root.parent / ".astrid-retired" / transaction_id)
        state = _state(retained)
        if state["revision"] == 0 or state["transaction_id"] == transaction_id or not (root / _STATE).exists():
            raise PackInstallError("uncommitted_generation", "This attempt has no retired committed generation.")
        return GroupSnapshot(state["transaction_id"], state["revision"], _paths(retained),
                             tuple(state["removals"]), state["admissions"], state["refresh"] is not None)


def _facts(path: Path, *, destination: Path | None = None) -> tuple[dict[str, Any], PackFacts]:
    try:
        manifest = validate_canonical_pack(path, expected_pack_id=path.name).definition.to_dict()
    except (OSError, ValueError) as exc:
        raise PackInstallError("invalid_manifest", f"Selected pack {path.name} needs a valid canonical v3 pack.yaml; correct it and retry.") from exc
    try:
        pin = read_pin(path / "PIN") if (path / "PIN").exists() else {}
    except (OSError, ValueError) as exc:
        raise PackInstallError("pin_mismatch", f"Selected pack {path.name} has an invalid PIN; restore its provenance before retrying.") from exc
    return manifest, {
        "id": path.name, "canonical_root": str(destination or path), "schema_version": 3,
        "pack_api": manifest.get("pack_api"), "dependencies": manifest.get("dependencies", {}),
        "origin": pin.get("origin", "folder"), "source": pin.get("source", path.as_uri()),
        "commit": pin.get("commit"), "installed_digest": pin.get("digest"),
        "observed_digest": pack_digest(path),
    }


def _compose_resulting_facts(facts: Mapping[str, Mapping[str, Any]], paths: Mapping[str, Path], *,
                             selected_roots: tuple[Path, ...] = (), core_root: Path | None = None,
                             candidate_root: Path | None = None, candidate_ids: frozenset[str] = frozenset(),
                             python_packages: Mapping[str, str] | None = None,
                             release_pins: Mapping[str, PinFacts] | None = None
                             ) -> tuple[dict[str, dict[str, Any]], dict[str, str]]:
    """Complete dependency facts for every resulting pack under transaction custody.

    ``facts`` already contains every candidate in the resulting collection. We
    read only missing dependency IDs from explicitly configured external roots
    and the bundled core root, recursively following those selected manifests.
    """
    core_root = (core_root or _BUNDLED_CORE_ROOT).expanduser().resolve()
    roots = list(dict.fromkeys((*[Path(root).expanduser().resolve() for root in selected_roots], core_root)))
    result = {key: dict(value) for key, value in facts.items()}
    def valid_id(value: object) -> bool:
        return isinstance(value, str) and bool(_ID.fullmatch(value))

    def selected_identity(path: Path) -> tuple[int, int, int, int]:
        root_info = path.parent.stat(follow_symlinks=False)
        pack_info = path.stat(follow_symlinks=False)
        return root_info.st_dev, root_info.st_ino, pack_info.st_dev, pack_info.st_ino

    for key, item in result.items():
        if not valid_id(key) or not isinstance(item, Mapping) or item.get("id", key) != key:
            raise PackInstallError("invalid_manifest", "Selected dependency facts must use canonical pack IDs.")
        declared = item.get("dependencies", {})
        if not isinstance(declared, Mapping):
            raise PackInstallError("invalid_manifest", f"Pack {key} has malformed dependency facts.")
        deps = declared.get("packs", ())
        if not isinstance(deps, (list, tuple)) or any(not valid_id(dep) for dep in deps):
            raise PackInstallError("invalid_manifest", f"Pack {key} has a noncanonical dependency ID.")
    # Derive the selected collection portion from the held candidate paths so
    # a caller cannot accidentally omit a retained pack from policy validation.
    for pack_id, path in paths.items():
        if pack_id != "personal" and pack_id not in result:
            _manifest, result[pack_id] = _facts(path)
            declared = result[pack_id].get("dependencies", {})
            deps = declared.get("packs", ()) if isinstance(declared, Mapping) else ()
            if not isinstance(declared, Mapping) or not isinstance(deps, (list, tuple)) or any(not valid_id(dep) for dep in deps):
                raise PackInstallError("invalid_manifest", f"Pack {pack_id} has a noncanonical dependency ID.")
    pending: list[str] = []
    for item in result.values():
        dependencies = item.get("dependencies", {})
        declared = dependencies.get("packs", ()) if isinstance(dependencies, Mapping) else ()
        if isinstance(declared, (list, tuple)):
            pending.extend(dep for dep in declared if isinstance(dep, str) and dep)
    external_facts: dict[str, tuple[Path, dict[str, Any], dict[str, Any]]] = {}
    # Scan each explicitly selected root as a complete inventory. This makes
    # collisions independent of lookup order and binds all selected facts to
    # their physical roots before dependency resolution.
    root_ids: dict[str, Path] = {}
    for selected_root in roots:
        if not selected_root.is_dir():
            continue
        for candidate in sorted(selected_root.iterdir()):
            if (not valid_id(candidate.name) or candidate.is_symlink() or not candidate.is_dir()
                    or (candidate / "pack.yaml").is_symlink() or not (candidate / "pack.yaml").is_file()):
                continue
            if not candidate.resolve().is_relative_to(selected_root):
                raise PackInstallError("unsafe_path", f"Selected candidate {candidate.name} escapes its configured root.")
            selected_fact = result.get(candidate.name)
            if (selected_fact is not None and selected_fact.get("canonical_root")
                    and Path(str(selected_fact["canonical_root"])).resolve() != candidate.resolve()):
                raise PackInstallError("duplicate_id", f"Selected inventories contain duplicate pack ID {candidate.name}.")
            previous = root_ids.get(candidate.name)
            if previous is not None and previous.resolve() != candidate.resolve():
                raise PackInstallError("duplicate_id", f"Selected roots contain duplicate pack ID {candidate.name}.")
            root_ids[candidate.name] = candidate
            selected_fact = result.get(candidate.name)
            if selected_fact is not None and selected_fact.get("canonical_root") == str(candidate.resolve()):
                current_manifest, current_fact = _facts(candidate)
                if candidate.resolve().is_relative_to(core_root):
                    current_fact["origin"] = "core"
                if (current_fact != {key: selected_fact.get(key) for key in current_fact}
                        or selected_fact.get("_selection_identity") != selected_identity(candidate)):
                    raise PackInstallError("selected_snapshot_changed", f"Selected dependency {candidate.name} changed during this transaction; retry.")
                if not candidate.resolve().is_relative_to(core_root):
                    external_facts[candidate.name] = (candidate, current_manifest, current_fact)

    for pack_id, item in result.items():
        if pack_id in paths:
            continue
        if pack_id in candidate_ids:
            expected_path = (candidate_root / pack_id).resolve() if candidate_root is not None else None
            if not isinstance(item.get("canonical_root"), str) or Path(item["canonical_root"]).resolve() != expected_path:
                raise PackInstallError("invalid_manifest", f"Staged facts for {pack_id} do not match the transaction candidate root.")
            continue
        candidate = root_ids.get(pack_id)
        canonical_root = item.get("canonical_root")
        if (candidate is None or not isinstance(canonical_root, str)
                or Path(canonical_root).resolve() != candidate.resolve()):
            raise PackInstallError("missing_dep", f"Selected facts for {pack_id} have no admitted root in this composition.")

    visited: set[str] = set(result)
    while pending:
        pack_id = pending.pop()
        if pack_id in visited:
            continue
        visited.add(pack_id)
        path = root_ids.get(pack_id)
        if path is None:
            continue
        _manifest, item = _facts(path)
        resolved = path.resolve()
        if resolved.is_relative_to(core_root):
            item["origin"] = "core"
        item["_selection_identity"] = selected_identity(path)
        dependencies = item.get("dependencies", {})
        declared = dependencies.get("packs", ()) if isinstance(dependencies, Mapping) else ()
        if not isinstance(dependencies, Mapping) or not isinstance(declared, (list, tuple)) or any(not valid_id(dep) for dep in declared):
            raise PackInstallError("invalid_manifest", f"Pack {pack_id} has a noncanonical dependency ID.")
        result[pack_id] = item
        if not resolved.is_relative_to(core_root):
            external_facts[pack_id] = (path, _manifest, item)
        dependencies = item.get("dependencies", {})
        declared = dependencies.get("packs", ()) if isinstance(dependencies, Mapping) else ()
        if isinstance(declared, (list, tuple)):
            pending.extend(dep for dep in declared if isinstance(dep, str) and dep and dep not in visited)

    # Core public API names are a separate import-policy fact. Their presence
    # does not depend on the selected manifest's origin label.
    admitted_core_ids = set()
    for path in core_root.iterdir():
        if (not valid_id(path.name) or not path.is_dir() or path.is_symlink()
                or (path / "api.py").is_symlink() or not (path / "api.py").is_file()
                or (path / "pack.yaml").is_symlink() or not (path / "pack.yaml").is_file()):
            continue
        try:
            validate_canonical_pack(path, expected_pack_id=path.name)
        except (OSError, ValueError):
            continue
        admitted_core_ids.add(path.name)
    core_apis = {path.name: f"astrid.packs.{path.name}.api" for path in core_root.iterdir()
                 if path.name in admitted_core_ids and path.is_dir() and not path.is_symlink()
                 and (path / "api.py").is_file()}

    # Admit each selected external dependency through the same compatibility
    # policy as host inventory, with the caller's frozen Python and pin facts.
    if external_facts:
        package_snapshot = dict(python_packages or {})
        pin_snapshot = dict(release_pins or {})
        contract = ImportContract(facade_exports=frozenset(pack_api.__all__),
                                  facade_version=pack_api.VERSION, sdk_names=frozenset(sdk.__all__),
                                  core_pack_apis=core_apis)
        for pack_id, (path, manifest, item) in external_facts.items():
            if item.get("installed_digest") and item.get("installed_digest") != item.get("observed_digest"):
                raise PackInstallError("pin_mismatch", f"Selected dependency {pack_id} no longer matches its installed PIN.")
            checked = validate_pack(mode="inventory", manifest=manifest, candidate=item, selected=result,
                                    core_api=pack_api.VERSION, release_pins=pin_snapshot,
                                    python_packages=package_snapshot,
                                    import_violations=check_pack_imports(path, contract=contract, manifest=manifest))
            if not checked["ok"]:
                raise PackInstallError(checked["code"], checked["reason"], compatibility=checked)
    return result, core_apis


def _verify_external_root_digests(facts: Mapping[str, Mapping[str, Any]],
                                  paths: Mapping[str, Path]) -> None:
    """Verify external roots still match the facts admitted for publication."""
    for pack_id, item in facts.items():
        if (pack_id in paths or item.get("origin") == "core"
                or "_selection_identity" not in item):
            continue
        canonical_root = item.get("canonical_root")
        expected_digest = item.get("observed_digest")
        if not isinstance(canonical_root, str) or not isinstance(expected_digest, str):
            raise PackInstallError("selected_snapshot_changed",
                                   f"Selected dependency {pack_id} has no admitted external-root digest.")
        try:
            current_digest = pack_digest(Path(canonical_root))
        except (OSError, ValueError) as exc:
            raise PackInstallError("selected_snapshot_changed",
                                   f"Selected dependency {pack_id} changed during this transaction; retry.") from exc
        if current_digest != expected_digest:
            raise PackInstallError("selected_snapshot_changed",
                                   f"Selected dependency {pack_id} changed during this transaction; retry.")


def _retained_collection_bytes(root: Path, changed: frozenset[str]) -> dict[str, tuple[str, str]]:
    """Digest every live collection entry a transaction carries over unchanged.

    That is everything except the ids the transaction itself installs, removes
    or restores, the commit record and the single-pack lock: retained packs,
    the personal pack and any other user bytes in the collection.
    """
    result: dict[str, tuple[str, str]] = {}
    if not root.is_dir():
        return result
    for top in sorted(root.iterdir()):
        if top.name in changed or top.name in {_STATE, ".install.lock"}:
            continue
        nested = sorted(top.rglob("*")) if top.is_dir() and not top.is_symlink() else []
        for path in (top, *nested):
            key = path.relative_to(root).as_posix()
            if path.is_symlink():
                result[key] = ("link", os.readlink(path))
            elif path.is_dir():
                result[key] = ("dir", "")
            elif path.is_file():
                result[key] = ("file", hashlib.sha256(path.read_bytes()).hexdigest())
            else:
                result[key] = ("special", "")
    return result


def _verify_retained_collection(live: Path, changed: frozenset[str],
                                copied: Mapping[str, tuple[str, str]]) -> None:
    """Refuse when live bytes the candidate copied changed after the copy (LB-TXN r1 #1)."""
    try:
        current = _retained_collection_bytes(live, changed)
    except OSError as exc:
        raise PackInstallError("collection_changed",
                               "The pack collection changed during this transaction; nothing was committed. Retry.") from exc
    if current != copied:
        changed_paths = sorted(set(current) ^ set(copied) | {k for k in set(current) & set(copied) if current[k] != copied[k]})
        raise PackInstallError(
            "collection_changed",
            "The pack collection changed during this transaction (" + ", ".join(changed_paths[:5])
            + ("" if len(changed_paths) <= 5 else ", ...") + "); nothing was committed. Retry.")


def _publish_and_verify_external_roots(candidate: Path, root: Path, facts: Mapping[str, Mapping[str, Any]],
                                       paths: Mapping[str, Path], retired: Path, transaction_id: str,
                                       revision: int,
                                       carried_ids: frozenset[str] | None = None,
                                       carried: Mapping[str, tuple[str, str]] | None = None,
                                       preserve: list[Path] | None = None) -> None:
    """Publish, then restore the prior generation if an external root raced the exchange.

    With ``carried`` (the retained bytes the candidate copied), the generation
    that was live until now (``candidate`` names it after the exchange) is
    re-checked: a write that landed in it between the last pre-swap check and
    the exchange would otherwise be retired with it. Any refusal exchanges the
    prior generation back. The generation discarded by that swap-back is
    checked too (LB-TXN r2 R2-1): if its retained bytes differ from what this
    transaction published, a write landed in it after the exchange, so it is
    kept at ``<retired>/refused-<tx>`` and named in the error, not deleted.
    If it cannot be moved it stays at ``candidate`` and is appended to
    ``preserve`` so the caller's cleanup leaves it in place.
    """
    _publish(candidate, root)
    try:
        _verify_external_root_digests(facts, paths)
        if carried is not None:
            _verify_retained_collection(candidate, carried_ids or frozenset(), carried)
    except PackInstallError as refused:
        try:
            # candidate now names the prior complete generation. Exchanging it
            # back restores collection bytes and admission records atomically.
            _publish(candidate, root)
        except (OSError, PackInstallError) as exc:
            # The new generation stays live; the prior one (holding any write
            # that triggered this swap-back) is still at candidate.
            raise GroupCommitError(transaction_id, revision, late_write_root=candidate) from exc
        kept = _keep_discarded_generation_if_written(candidate, retired, transaction_id, carried_ids, carried)
        if kept == candidate and preserve is not None:
            preserve.append(candidate)
        try:
            _sync_directory(retired)
            _sync_directory(root.parent)
        except OSError as exc:
            raise GroupCommitError(transaction_id, revision) from exc
        if kept is not None:
            raise PackInstallError(
                refused.code,
                f"{refused} The generation this transaction briefly published also changed after publication;"
                f" it was kept at {kept} (not deleted) so that write can be recovered.") from refused
        raise


def _keep_discarded_generation_if_written(candidate: Path, retired: Path, transaction_id: str,
                                          carried_ids: frozenset[str] | None,
                                          carried: Mapping[str, tuple[str, str]] | None) -> Path | None:
    """Move the swapped-out new generation aside when it no longer matches what was published.

    Only the retained entries are compared (the ids this transaction itself
    installs, removes or restores are its own bytes, as in the pre-swap check).
    An unreadable generation is kept, never deleted unchecked.
    """
    if carried is None:
        return None
    try:
        if _retained_collection_bytes(candidate, carried_ids or frozenset()) == carried:
            return None
    except OSError:
        pass
    kept = retired / ("refused-" + transaction_id)
    try:
        os.rename(candidate, kept)
    except OSError:
        return candidate  # Could not move it; it stays (and is named) at candidate.
    return kept


def _artifact_metadata(request: Mapping[str, Any], artifact: Mapping[str, Any],
                       committed_revision: int) -> dict[str, bytes]:
    """Encode the frozen browser records owned by this group publication."""
    manifest = _json(artifact)
    record = {"version": 1, "packId": request["pack_id"], "sourceDigest": request["source_digest"],
              "digest": artifact["digest"], "artifactManifestDigest": hashlib.sha256(manifest).hexdigest(),
              "committedRevision": committed_revision, "transactionId": request["transaction_id"]}
    return {f'{request["source_digest"]}.artifact-manifest.json': manifest,
            "admission.json": _json(record)}


def _verify_artifacts(request: Mapping[str, Any], result: Mapping[str, Any], *,
                      committed_revision: int | None = None) -> dict[str, Any] | None:
    try:
        return _verify_artifact_bytes(request, result, committed_revision=committed_revision)
    except PackInstallError:
        raise
    except (OSError, ValueError, TypeError, KeyError) as exc:
        raise PackInstallError("incomplete_artifacts", "Artifact file or manifest is missing/invalid; rebuild the private stage.") from exc


def _verify_artifact_bytes(request: Mapping[str, Any], result: Mapping[str, Any], *,
                           committed_revision: int | None = None) -> dict[str, Any] | None:
    """Independently verify the frozen builder output before admission."""
    def refuse(code: str = "incomplete_artifacts") -> None:
        raise PackInstallError(code, "Build output failed admission; rebuild with the release-pinned toolchain.")

    if (not isinstance(result, Mapping) or type(result.get("version")) is not int or result.get("version") != 1
            or result.get("pack_id") != request["pack_id"] or type(result.get("ok")) is not bool):
        refuse()
    if not result["ok"]:
        code = result.get("code")
        allowed = {"invalid_request", "tool_mismatch", "compile_failed", "unsafe_import", "unsafe_output",
                   "hash_mismatch", "incomplete_artifacts", "io_failed"}
        refuse(code if code in allowed else "incomplete_artifacts")
    if set(result) != {"version", "ok", "pack_id", "source_digest", "artifact_manifest"}:
        refuse()
    if result.get("source_digest") != request["source_digest"]:
        refuse("hash_mismatch")
    artifact = result.get("artifact_manifest")
    if request["entry"] is None and request["extension_manifest"] is None:
        if artifact is not None:
            refuse()
        build_root = Path(request["stage_root"]) / ".build"
        if (build_root.exists() or build_root.is_symlink()) and _tree(build_root):
            refuse()
        return None
    if not isinstance(artifact, dict) or set(artifact) != {
            "version", "packId", "sourceDigest", "digest", "graphIndexDigest", "toolchainDigest",
            "entry", "extensionManifest", "sharedModules", "files"}:
        refuse()
    if (type(artifact["version"]) is not int or artifact["version"] != 1 or artifact["packId"] != request["pack_id"]
            or artifact["sourceDigest"] != request["source_digest"] or artifact["entry"] != "index.js"
            or artifact["toolchainDigest"] != request["toolchain"].get("lock_sha256")
            or artifact["sharedModules"] != request["toolchain"].get("shared_modules")
            or artifact["extensionManifest"] != request["extension_manifest"]):
        refuse("hash_mismatch")
    files = artifact["files"]
    if not isinstance(files, list) or not files:
        refuse()
    root = Path(request["stage_root"])
    source_digest = request["source_digest"]
    seen: set[str] = set()
    graph = []
    entry_bytes = None
    mimes = {".js": "text/javascript", ".css": "text/css", ".png": "image/png", ".jpg": "image/jpeg",
             ".jpeg": "image/jpeg", ".svg": "image/svg+xml", ".webp": "image/webp", ".gif": "image/gif",
             ".woff": "font/woff", ".woff2": "font/woff2"}
    names = []
    for item in files:
        if not isinstance(item, dict) or set(item) != {"path", "disk", "sha256", "size", "mime"}:
            refuse()
        name = item["path"]
        if not isinstance(name, str):
            refuse("unsafe_output")
        _relative(name)
        if any(c in name for c in "?#") or name.casefold() in seen:
            refuse("unsafe_output")
        seen.add(name.casefold())
        names.append(name)
        disk = f".build/{source_digest}.js" if name == "index.js" else f".build/{source_digest}/{name}"
        expected_mime = mimes.get(Path(name).suffix.lower())
        if item["disk"] != disk or expected_mime is None or item["mime"] != expected_mime:
            refuse("unsafe_output")
        path = root / disk
        # Audit every component: resolve() alone could hide an internal symlink.
        _directory(path.parent)
        if not stat.S_ISREG(path.lstat().st_mode):
            refuse("unsafe_output")
        data = path.read_bytes()
        if (type(item["size"]) is not int or item["size"] != len(data)
                or item["sha256"] != hashlib.sha256(data).hexdigest()):
            refuse("hash_mismatch")
        if name == "index.js":
            entry_bytes = data
            if item["sha256"] != artifact["digest"]:
                refuse("hash_mismatch")
        else:
            graph.append({"logical_path": name, "sha256": item["sha256"], "size": len(data), "mime": item["mime"]})
    if names != sorted(names) or entry_bytes is None:
        refuse()
    graph_digest = hashlib.sha256(_json(sorted(graph, key=lambda item: item["logical_path"]))).hexdigest()
    if (graph_digest != artifact["graphIndexDigest"]
            or not entry_bytes.startswith(f"/* astrid-graph-index-sha256:{graph_digest} */\n".encode())):
        refuse("hash_mismatch")
    # Builder output must be exactly the enumerated source-addressed set. Only
    # the transaction may add metadata, checked byte-for-byte once staged.
    physical = {relative for relative, _ in _tree(root / ".build")}
    expected = {item["disk"][len(".build/"):] for item in files}
    if committed_revision is not None:
        for name, encoded in _artifact_metadata(request, artifact, committed_revision).items():
            if (root / ".build" / name).read_bytes() != encoded:
                raise PackInstallError("hash_mismatch", f"Staged browser metadata .build/{name} changed; retry the group build.")
            expected.add(name)
    if physical != expected:
        refuse("incomplete_artifacts")
    return {"packId": request["pack_id"], "sourceDigest": source_digest, "digest": artifact["digest"],
            "artifactManifestDigest": hashlib.sha256(_json(artifact)).hexdigest(), "artifact_manifest": artifact}


def _retained_build_records(root: Path, admissions: Mapping[str, Any], group_revision: int):
    """Recheck retained bytes against their original committed admission facts.

    This is a verification projection, never a new builder call or tool policy.
    Retained source edits remain allowed by inventory validation; their browser
    artifacts must still match the previously admitted source/build identity.
    """
    records = []
    for pack_id, admission in sorted(admissions.items()):
        try:
            if not _ID.fullmatch(pack_id) or not (root / pack_id / "pack.yaml").is_file():
                raise ValueError("invalid retained pack identity")
            _tree(root / pack_id / ".build")
            artifact = admission["artifact_manifest"]
            # The existing durable group summary binds the original admission
            # epoch; the per-pack record cannot choose a different history.
            revision, tx = admission["committedRevision"], admission["transactionId"]
            source_digest = admission["sourceDigest"]
            if (type(revision) is not int or not 1 <= revision <= group_revision
                    or not isinstance(tx, str) or len(tx) != 32 or any(c not in "0123456789abcdef" for c in tx)
                    or not isinstance(source_digest, str) or len(source_digest) != 64
                    or any(c not in "0123456789abcdef" for c in source_digest)):
                raise ValueError("invalid retained admission identity")
            # .build is intentionally excluded from pack_digest, so bind the
            # held browser admission back to the source tree it originally
            # named before trusting its artifact bytes.
            if pack_digest(root / pack_id) != source_digest:
                raise ValueError("retained source digest differs from its admission")
            request = {"version": 1, "pack_id": pack_id, "transaction_id": tx,
                       "stage_root": str(root / pack_id), "source_digest": source_digest,
                       "entry": artifact["entry"], "extension_manifest": artifact["extensionManifest"],
                       "toolchain": {"lock_sha256": artifact["toolchainDigest"],
                                     "shared_modules": artifact["sharedModules"]}}
            result = {"version": 1, "ok": True, "pack_id": pack_id,
                      "source_digest": source_digest, "artifact_manifest": artifact}
            verified = _verify_artifacts(request, result, committed_revision=revision)
            if {**verified, "committedRevision": revision, "transactionId": tx} != admission:
                raise ValueError("retained admission summary differs from verified files")
            records.append((request, result, revision))
        except (OSError, ValueError, TypeError, KeyError, PackInstallError) as exc:
            raise PackInstallError("incomplete_artifacts", f"Retained browser admission for {pack_id} is invalid; explicitly rebuild that pack before updating the group.") from exc
    return records


def _refresh(root: Path, pending: Mapping[str, Any] | None, refresh: Refresh | None) -> bool:
    """Notify the host about one committed outbox entry; never under custody.

    The caller must NOT hold this collection's exclusive custody: the host's
    acknowledgment path reads the committed group (often from another thread
    or process), and a shared reader must be able to take the lock. The
    acknowledgment is recorded under a short exclusive section and only when
    the committed outbox still names this transaction and revision; a newer
    commit supersedes it and stays pending.
    """
    if pending is None:
        return False
    if refresh is None:
        return True
    try:
        ack = refresh(transaction_id=pending["transaction_id"], committed_revision=pending["revision"],
                      changed_ids=tuple(pending["changed_ids"]))
    except Exception:  # noqa: BLE001 - arbitrary host notification failure is retryable after commit
        # Notification/acknowledgment failure cannot turn a committed install
        # into a rollback. The committed outbox remains retryable and idempotent.
        return True
    if (not isinstance(ack, Mapping) or type(ack.get("version")) is not int or ack.get("version") != 1 or ack.get("accepted") is not True
            or type(ack.get("revision")) is not int or ack["revision"] != pending["revision"]
            or not isinstance(ack.get("reason"), str)):
        return True
    with group_read_lock(root, exclusive=True):
        current = _state(root)
        if current["refresh"] is None:
            return False
        if current["refresh"] != dict(pending):
            # A later commit carried these ids forward; its own outbox is pending.
            return True
        current["refresh"] = None
        _write_state(root, current)
        return False


def retry_refresh(*, packs_root: str | Path, refresh: Refresh) -> bool:
    """Recover durability then retry the latest committed notification.

    Return True when notification is still pending. Superseding revisions
    describe the entire current inventory; a delayed ack cannot clear a newer one.
    """
    root = _root(packs_root)
    with group_read_lock(root, exclusive=True):
        state = _state(root)
        if state["revision"]:
            _sync_tree(root)
            retired = root.parent / ".astrid-retired"
            if retired.exists():
                _sync_directory(retired)
            _sync_directory(root.parent)
        pending = dict(state["refresh"]) if state["refresh"] is not None else None
    # Custody is released: the host may read the committed group while it acknowledges.
    return _refresh(root, pending, refresh)


def _running_interpreter_environment(packages: Mapping[str, str]) -> LockedPythonEnvironment:
    """Declare the caller's interpreter snapshot as a locked metadata index.

    Callers of the group transaction pass the selected interpreter's package
    snapshot (name -> version). The single-pack installer admits requirements
    only from a LockedPythonEnvironment (park g2: names/versions alone carry no
    provenance), so each snapshot entry is bound to the running interpreter's
    installed METADATA file; the installer then re-reads that file in an
    isolated interpreter. An entry with no matching installed distribution is
    simply absent and the requirement fails closed.
    """
    from importlib import metadata as importlib_metadata

    inventory: dict[str, tuple[str, Path]] = {}
    for name, version in packages.items():
        try:
            dist = importlib_metadata.distribution(name)
        except importlib_metadata.PackageNotFoundError:
            continue
        info = getattr(dist, "_path", None)
        if dist.version != version or info is None or not (Path(info) / "METADATA").is_file():
            continue
        inventory[str(name)] = (str(version), (Path(info) / "METADATA").resolve())
    return LockedPythonEnvironment(interpreter=Path(sys.executable).resolve(), inventory=inventory)


_TRANSACTION_IDENTITY_FIELDS = frozenset({"transactionId", "committedRevision"})


def _same_tree_bytes(left: Path, right: Path) -> bool:
    """Byte equality of two pack trees (paths, file bytes, symlink targets).

    The build admission record ``.build/admission.json`` names the transaction
    that admitted it; it is compared with ``transactionId`` and
    ``committedRevision`` ignored, so identical sources and build outputs are
    equal across transactions (LB-TXN r1 finding 2). Every other byte,
    including the artifact manifest, is compared exactly.
    """
    if not left.is_dir() or not right.is_dir():
        return False

    def entries(root: Path) -> dict[str, tuple[str, bytes | str | None]]:
        result: dict[str, tuple[str, bytes | str | None]] = {}
        for path in root.rglob("*"):
            key = path.relative_to(root).as_posix()
            if path.is_symlink():
                result[key] = ("link", os.readlink(path))
            elif path.is_dir():
                result[key] = ("dir", None)
            else:
                data = path.read_bytes()
                if key == ".build/admission.json":
                    try:
                        record = json.loads(data)
                    except ValueError:
                        record = None
                    if isinstance(record, dict):
                        result[key] = ("admission", _json({name: value for name, value in record.items()
                                                           if name not in _TRANSACTION_IDENTITY_FIELDS}))
                        continue
                result[key] = ("file", data)
        return result

    return entries(left) == entries(right)


def install_group(*, source: str | Path, packs: Mapping[str, str], packs_root: str | Path,
                  python_packages: Mapping[str, str], revision: str | None = None,
                  origin: Literal["user", "default", "folder"] | None = None,
                  expected_digests: Mapping[str, str] | None = None,
                  selected: Mapping[str, PackFacts] | None = None,
                  selected_roots: tuple[str | Path, ...] = (),
                  core_root: str | Path | None = None,
                  release_pins: Mapping[str, PinFacts] | None = None,
                  import_contract: ImportContract | None = None,
                  builds: Mapping[str, Mapping[str, Any]] | None = None,
                  toolchain: Mapping[str, Any] | None = None, remove: tuple[str, ...] = (),
                  intent: Literal["explicit", "defaults", "update", "rollback"] = "explicit",
                  reconcile_defaults: bool = False,
                  refresh: Refresh | None = None,
                  consent: Callable[[Mapping[str, Any]], bool] | None = None,
                  boundary: Callable[[str], None] | None = None) -> GroupResult:
    """Validate -> build -> publish a same-source, same-commit dependency group.

    Only explicitly named ids are updated. Defaults, updates, and rollbacks
    honor durable removal tombstones; defaults also cannot take over
    user/folder packs. Only an explicit install clears a removal. Dependencies
    are facts from this candidate collection plus explicitly selected host/core facts;
    installation never fetches an undeclared dependency or pack update.
    ``boundary`` is a trusted host observability hook, never pack code.
    All mutation callers must use this orchestration rather than composing
    multiple calls to the single-pack primitive against a live collection.
    """
    root = _root(packs_root)
    packs = dict(packs)
    remove = tuple(remove)
    if (intent not in {"explicit", "defaults", "update", "rollback"}
            or (intent == "defaults" and origin != "default")
            or any(not isinstance(i, str) or not _ID.fullmatch(i) for i in (*packs, *remove))
            or set(packs) & set(remove) or set(remove) & {"personal", "core", "astrid"}):
        raise PackInstallError("invalid_request", "Use disjoint valid install/removal ids and an explicit default origin.")
    for subpath in packs.values():
        _relative(subpath, root_ok=True)
    origin = origin or ("folder" if revision is None else "user")
    # Freeze caller-owned inputs before any external operation.
    builds = json.loads(_json(dict(builds or {})))
    toolchain = json.loads(_json(dict(toolchain or {})))
    expected_digests = dict(expected_digests or {})
    # A default group needs a full commit SHA (ca370d21), even when empty; only a
    # release-manifest reconciliation to an empty default list has no commit (68ccd90e).
    if origin == "default" and (packs or not reconcile_defaults) and (
            not isinstance(revision, str) or not _SHA.fullmatch(revision)
            or any(i not in expected_digests for i in packs)):
        raise PackInstallError("invalid_pin", "Default groups require a full commit SHA and each expected digest.")
    release_pins = {i: dict(v) for i, v in (release_pins or {}).items()}
    host_selected = {i: dict(v) for i, v in (selected or {}).items()}
    packages = dict(python_packages)
    tx = uuid4().hex
    mark = boundary or (lambda phase: None)
    with group_read_lock(root, exclusive=True) as custody:
        root.mkdir(exist_ok=True)
        old = _state(root)
        committed_revision = old["revision"] + 1
        tombstones = set(old["removals"]) | (set(remove) if not reconcile_defaults else set())
        if intent in {"defaults", "update", "rollback"}:
            packs = {i: p for i, p in packs.items() if i not in tombstones}
        if intent == "explicit":
            tombstones.difference_update(packs)
        retired = _directory(root.parent / ".astrid-retired", create=True)
        candidate = retired / tx
        preserve: list[Path] = []  # LB-TXN r2 R2-1: a discarded generation that must stay
        # Custody is acquired before any source fetch, copy, or build side effect.
        candidate.mkdir(mode=0o700)
        candidate_identity = (candidate.stat().st_dev, candidate.stat().st_ino)
        published = False
        work = Path(tempfile.mkdtemp(prefix=".group-work-", dir=root.parent))
        try:
            mark("preparing")
            _tree(root)
            # A collection copy preserves user edits, PINs and generated outputs.
            # No hardlinks: a staged build must never mutate the live collection.
            shutil.copytree(root, candidate, dirs_exist_ok=True)
            candidate.chmod(0o700)
            (candidate / ".install.lock").unlink(missing_ok=True)
            # The primitive only writes unpublished candidates; a live group
            # commit record forbids standalone mutations that bypass admissions.
            (candidate / _STATE).unlink(missing_ok=True)
            if reconcile_defaults:
                if origin != "default":
                    raise PackInstallError("invalid_request", "Only release defaults may reconcile the default list.")
                desired_ids = set(packs)
                for existing_id, existing_path in _paths(candidate).items():
                    if existing_id in desired_ids:
                        continue
                    pin_path = existing_path / "PIN"
                    if pin_path.is_file() and read_pin(pin_path).get("origin") == "default":
                        remove = tuple(sorted(set(remove) | {existing_id}))
            for pack_id in remove:
                target = candidate / pack_id
                if target.exists():
                    pin = read_pin(target / "PIN") if (target / "PIN").exists() else {}
                    if pin.get("origin") == "personal":
                        raise PackInstallError("reserved_id", "Personal packs cannot be removed.")
                    shutil.rmtree(target)
            # Everything the candidate carries over unchanged, as copied. Live
            # writes after this point (fetch, consent, build) must not be lost.
            carried_ids = frozenset(packs) | frozenset(remove)
            carried = _retained_collection_bytes(candidate, carried_ids)
            admissions = {i: a for i, a in old["admissions"].items() if i not in remove and i not in packs}
            build_records = _retained_build_records(candidate, admissions, old["revision"])
            facts: dict[str, dict[str, Any]] = dict(host_selected)
            for pack_id, path in _paths(candidate).items():
                if pack_id not in packs:
                    manifest, item = _facts(path)
                    facts[pack_id] = item
            commit = revision
            source_text = _git_source(str(source)) if revision is not None and packs else None
            if intent == "update":
                for pack_id, subpath in packs.items():
                    installed_pin_path = root / pack_id / "PIN"
                    if not installed_pin_path.is_file():
                        continue
                    installed_pin = read_pin(installed_pin_path)
                    installed_source = installed_pin.get("source", "")
                    requested_source = source_text
                    if revision is None:
                        # Folder PINs identify the selected directory, including its subpath.
                        requested_source = _directory(Path(source) / subpath).as_uri()
                    else:
                        installed_source = installed_source.split("#", 1)[0]
                    if (installed_pin.get("origin") != origin
                            or installed_source != requested_source):
                        raise PackInstallError("pin_mismatch", f"Pack {pack_id} has a conflicting source PIN; update from its installed source.")
            for index, (pack_id, subpath) in enumerate(sorted(packs.items())):
                prepared = work / str(index)
                prepared.mkdir()
                payload = prepared / pack_id
                payload.mkdir()
                if revision is None:
                    folder = _directory(Path(source) / subpath)
                    if candidate.is_relative_to(folder) or root.is_relative_to(folder):
                        raise PackInstallError("unsafe_path", "Source folder cannot contain the collection or staging directory.")
                    _copy_folder(folder, payload)
                else:
                    observed = _fetch_tree(prepared, source_text, commit, subpath, payload)
                    if index and observed != commit:
                        raise PackInstallError("pin_mismatch", "Dependency group must use one source commit.")
                    commit = observed
                manifest, item = _facts(payload, destination=candidate / pack_id)
                # These are proposed immutable facts. install_pack proves them
                # against source/PIN/release pins before any build or publication.
                item.update(origin=origin, commit=commit, installed_digest=item["observed_digest"] if revision else None)
                facts[pack_id] = item
            # Candidate manifests are now verified/staged, and all retained
            # candidates are present. Compose the single policy snapshot while
            # exclusive collection custody is still held.
            selected_roots = tuple(Path(value).expanduser().resolve() for value in selected_roots
                                    if Path(value).expanduser().resolve() != root.resolve())
            core_root = Path(core_root or _BUNDLED_CORE_ROOT).expanduser().resolve()
            facts, core_apis = _compose_resulting_facts(facts, _paths(candidate), selected_roots=selected_roots,
                                                       core_root=core_root, candidate_root=candidate,
                                                       candidate_ids=frozenset(packs), python_packages=packages,
                                                       release_pins=release_pins)
            contract = ImportContract(
                facade_exports=(import_contract.facade_exports if import_contract else frozenset(pack_api.__all__)),
                facade_version=(import_contract.facade_version if import_contract else pack_api.VERSION),
                sdk_names=(import_contract.sdk_names if import_contract else frozenset(sdk.__all__)),
                core_pack_apis=core_apis)
            for pack_id, subpath in sorted(packs.items()):
                install_pack(source=source, pack_id=pack_id, revision=commit, subpath=subpath, origin=origin,
                             expected_digest=expected_digests.get(pack_id), packs_root=candidate,
                             selected=facts, release_pins=release_pins, import_contract=contract,
                             python_environment=_running_interpreter_environment(packages))
            # Validate the resulting closure, including retained dependants after
            # removals. Inventory mode allows intentional edits to installed packs.
            for pack_id, path in _paths(candidate).items():
                if pack_id == "personal":
                    continue
                manifest, item = _facts(path)
                checked = validate_pack(mode="inventory", manifest=manifest, candidate=item, selected=facts,
                                        core_api=pack_api.VERSION, release_pins=release_pins, python_packages=packages,
                                        import_violations=check_pack_imports(path, contract=contract, manifest=manifest))
                if not checked["ok"]:
                    raise PackInstallError(checked["code"], checked["reason"], compatibility=checked)
            mark("validated")
            identities = {i: (pack_digest(candidate / i), read_pin(candidate / i / "PIN")) for i in packs}
            # Consent covers the exact staged source and declarations before
            # any builder can run. Build outputs are validated separately and
            # remain bound to these source identities through publication.
            if consent is not None:
                changes = {
                    "intent": intent,
                    "source": source_text,
                    "revision": commit,
                    "packs": {
                        pack_id: {
                            "before": read_pin(root / pack_id / "PIN") if (root / pack_id / "PIN").is_file() else None,
                            "after": dict(identities[pack_id][1]),
                            "before_digest": pack_digest(root / pack_id) if (root / pack_id).is_dir() else None,
                            "after_digest": identities[pack_id][0],
                            "staged_root": str(candidate / pack_id),
                        }
                        for pack_id in sorted(packs)
                    },
                    "removals": sorted(tombstones),
                    # Every pack this staged change deletes: explicit removals and
                    # omitted defaults found by reconciliation (LB-TXN r1 finding 4).
                    "removed": sorted(remove),
                }
                if not custody.consent(consent, changes, roots=(root, candidate)):
                    raise PackInstallError("user_cancelled", "The pack update was not committed because consent was not given.")
                if revision is None:
                    for pack_id, subpath in packs.items():
                        if pack_digest(_directory(Path(source) / subpath)) != identities[pack_id][0]:
                            raise PackInstallError("pin_mismatch", "Local source changed after consent; retry the group transaction.")
            for pack_id in sorted(packs):
                # install_pack may retain an identical source/PIN. Every named
                # build still needs fresh outputs and this transaction's epoch;
                # never feed old metadata or another toolchain's files to it.
                generated = candidate / pack_id / ".build"
                if generated.exists() or generated.is_symlink():
                    _tree(generated)
                    shutil.rmtree(generated)
                spec = builds.get(pack_id, {})
                if not isinstance(spec, dict) or set(spec) - {"entry", "extension_manifest"}:
                    raise PackInstallError("invalid_request", "Use the frozen entry/extension_manifest build inputs.")
                request = {"version": 1, "transaction_id": tx, "pack_id": pack_id,
                           "stage_root": str(candidate / pack_id), "source_digest": identities[pack_id][0],
                           "entry": spec.get("entry"), "extension_manifest": spec.get("extension_manifest"),
                           "toolchain": toolchain}
                # Even an ordinary Python pack traverses the real M4 seam.
                result = build_pack_assets(request)
                admission = _verify_artifacts(request, result)
                build_records.append((request, result, committed_revision))
                if admission is not None:
                    # Both records remain private and are synced/published with
                    # their verified output files in the one directory exchange.
                    for name, encoded in _artifact_metadata(request, admission["artifact_manifest"],
                                                            committed_revision).items():
                        with (candidate / pack_id / ".build" / name).open("xb") as stream:
                            stream.write(encoded)
                    admissions[pack_id] = {**admission, "committedRevision": committed_revision, "transactionId": tx}
                mark("built:" + pack_id)
            mark("built")
            _tree(candidate)
            for pack_id, (digest, pin) in identities.items():
                if pack_digest(candidate / pack_id) != digest or read_pin(candidate / pack_id / "PIN") != pin:
                    raise PackInstallError("pin_mismatch", "Build changed staged source/PIN; the installed group is unchanged.")
            # Recheck every output after the whole build phase: a later build
            # cannot alter an earlier pack's admitted bytes unnoticed.
            for request, result, admitted_revision in build_records:
                _verify_artifacts(request, result, committed_revision=admitted_revision)
            if (intent == "update" and packs and not remove and old["refresh"] is None
                    and old["transaction_id"]
                    and all(_same_tree_bytes(candidate / pack_id, root / pack_id) for pack_id in packs)):
                # Equal-byte update (same source, PIN and build outputs): publish
                # nothing. The live generation, its revision and receipts stay;
                # the unpublished candidate is discarded by the finally below.
                mark("unchanged")
                return GroupResult(old["transaction_id"], old["revision"], (), _paths(root), False, None)
            # A newer inventory revision subsumes an unacknowledged one. Carry
            # its changed ids forward so a delta consumer cannot miss a commit.
            pending_ids = old["refresh"]["changed_ids"] if old["refresh"] else ()
            changed_ids = tuple(sorted(set(packs) | set(remove) | set(pending_ids)))
            state = {"version": 1, "revision": committed_revision, "transaction_id": tx,
                     "removals": sorted(tombstones), "admissions": admissions,
                     "refresh": {"transaction_id": tx, "revision": committed_revision,
                                 "changed_ids": list(changed_ids)}}
            _write_state(candidate, state)
            _sync_tree(candidate)
            _sync_directory(retired)
            mark("prepared")
            mark("before_swap")
            # Host quiescence/observability hooks cannot invalidate the sealed
            # candidate. Refuse a changed source, admission, or commit record.
            for pack_id, (digest, pin) in identities.items():
                if pack_digest(candidate / pack_id) != digest or read_pin(candidate / pack_id / "PIN") != pin:
                    raise PackInstallError("pin_mismatch", "Staged source/PIN changed after preparation; retry the group build.")
            if consent is not None and revision is None:
                for pack_id, subpath in packs.items():
                    source_pack = _directory(Path(source) / subpath)
                    if pack_digest(source_pack) != identities[pack_id][0]:
                        raise PackInstallError("pin_mismatch", "Local source changed after consent; retry the group transaction.")
            for request, result, admitted_revision in build_records:
                _verify_artifacts(request, result, committed_revision=admitted_revision)
            if _state(candidate) != state:
                raise PackInstallError("invalid_state", "Prepared group record changed before publication; retry the transaction.")
            verified_facts, verified_apis = _compose_resulting_facts(
                facts, _paths(candidate), selected_roots=selected_roots, core_root=core_root,
                candidate_root=candidate, candidate_ids=frozenset(packs),
                python_packages=packages, release_pins=release_pins)
            if verified_facts != facts or verified_apis != core_apis:
                raise PackInstallError("selected_snapshot_changed", "A selected dependency changed during this transaction; retry.")
            _verify_retained_collection(root, carried_ids, carried)
            _publish_and_verify_external_roots(
                candidate, root, facts, _paths(candidate), retired, tx, state["revision"],
                carried_ids=carried_ids, carried=carried, preserve=preserve)
            published = True  # Sole terminal-transition owner for this attempt.
            mark("swapped")
            try:
                _sync_directory(retired)
                _sync_directory(root.parent)
            except OSError as exc:
                raise GroupCommitError(tx, state["revision"]) from exc
            mark("durable")
            committed_paths = _paths(root)
        finally:
            # On success candidate now owns the complete previous generation.
            # Only an unpublished candidate belongs to this failed attempt.
            if not published:
                # A Python signal can arrive after the kernel exchange returns
                # but before the assignment above. Custody follows the held
                # directory identity, so cleanup still preserves the old epoch.
                try:
                    current = root.stat()
                    published = (current.st_dev, current.st_ino) == candidate_identity
                except OSError:
                    pass
            if not published and candidate not in preserve:
                shutil.rmtree(candidate, ignore_errors=True)
            shutil.rmtree(work, ignore_errors=True)
            try:
                (retired / ("." + tx + ".transaction.lock")).unlink(missing_ok=True)
            except OSError:
                pass  # Housekeeping cannot mask a publication/durability result.
    # Exclusive custody is released before the host is notified (see _refresh).
    mark("before_refresh")
    pending = _refresh(root, state["refresh"], refresh)
    mark("refreshed")
    return GroupResult(tx, state["revision"], changed_ids, committed_paths, pending, candidate)


def rollback_group(*, packs_root: str | Path, pack_ids: tuple[str, ...] | list[str] | None = None,
                   transaction_id: str | None = None,
                   expected_pins: Mapping[str, Mapping[str, Any]] | None = None,
                   selected: Mapping[str, PackFacts] | None = None,
                   selected_roots: tuple[str | Path, ...] = (),
                   core_root: str | Path | None = None,
                   release_pins: Mapping[str, PinFacts] | None = None,
                   python_packages: Mapping[str, str] | None = None,
                   refresh: Refresh | None = None,
                   consent: Callable[[Mapping[str, Any]], bool] | None = None,
                   boundary: Callable[[str], None] | None = None) -> GroupResult:
    """Restore selected packs from the generation retired by the active commit.

    The rollback itself is a complete atomic collection publication. Current
    tombstones win over the retired generation, and packs outside ``pack_ids``
    are copied from the live collection unchanged. Restored pack directories
    are copied as complete installed state, including build-hook admission and
    artifact-manifest files under ``.build``.
    """
    root = _root(packs_root)
    expected_pins = {key: dict(value) for key, value in (expected_pins or {}).items()}
    host_selected = {key: dict(value) for key, value in (selected or {}).items()}
    pins = {key: dict(value) for key, value in (release_pins or {}).items()}
    packages = dict(python_packages or {})
    mark = boundary or (lambda phase: None)
    with group_read_lock(root, exclusive=True) as custody:
        state = _state(root)
        active_tx = state["transaction_id"]
        if not active_tx or (transaction_id is not None and transaction_id != active_tx):
            raise PackInstallError("rollback_conflict", "Rollback must name the currently active committed generation.")
        prior = _directory(root.parent / ".astrid-retired" / active_tx)
        prior_state = _state(prior)
        if prior_state["revision"] < 1 or prior_state["transaction_id"] == active_tx:
            raise PackInstallError("uncommitted_generation", "The active generation has no prior committed generation to restore.")
        current_paths = _paths(root)
        prior_paths = _paths(prior)
        if pack_ids is None:
            default_ids = set()
            for pack_id, path in {**prior_paths, **current_paths}.items():
                try:
                    if read_pin(path / "PIN").get("origin") == "default":
                        default_ids.add(pack_id)
                except (OSError, ValueError):
                    continue
            ids = tuple(sorted(default_ids))
        else:
            ids = tuple(sorted(set(pack_ids)))
        if not ids or any(not isinstance(i, str) or not _ID.fullmatch(i) or i == "personal" for i in ids):
            raise PackInstallError("invalid_request", "Select one or more valid pack IDs to roll back.")
        # Verify the retired generation's held admission files and every
        # artifact byte before copying any restored pack or constructing a
        # new admission epoch. Historical source pins are preserved, subject
        # to any currently applicable authoritative release pin below.
        prior_build_records = _retained_build_records(prior, prior_state["admissions"], prior_state["revision"])
        prior_facts = {}
        for pack_id, path in prior_paths.items():
            if pack_id == "personal":
                continue
            prior_manifest, prior_item = _facts(path)
            prior_facts[pack_id] = (prior_manifest, prior_item, read_pin(path / "PIN") if (path / "PIN").is_file() else {})
        tombstones = set(state["removals"])
        before = {}
        for pack_id in ids:
            current_pin = read_pin(current_paths[pack_id] / "PIN") if pack_id in current_paths and (current_paths[pack_id] / "PIN").is_file() else None
            prior_pin = read_pin(prior_paths[pack_id] / "PIN") if pack_id in prior_paths and (prior_paths[pack_id] / "PIN").is_file() else None
            if pack_id in expected_pins and current_pin != expected_pins[pack_id]:
                raise PackInstallError("rollback_conflict", f"Pack {pack_id} no longer has the expected committed PIN.")
            if current_pin and current_pin.get("origin") == "personal":
                raise PackInstallError("reserved_id", "Personal packs cannot be replaced by a rollback.")
            if current_pin and prior_pin and (current_pin.get("source") != prior_pin.get("source")
                                               or current_pin.get("origin") != prior_pin.get("origin")):
                raise PackInstallError("rollback_conflict", f"Pack {pack_id} has conflicting source pins; refusing rollback.")
            if pack_id in prior_facts and pack_id in pins:
                historical_pin = prior_facts[pack_id][2]
                applicable = pins[pack_id]
                if (historical_pin.get("commit") != applicable.get("commit")
                        or historical_pin.get("digest") != applicable.get("digest")):
                    raise PackInstallError("pin_mismatch", f"Historical pack {pack_id} does not match the applicable release pin; refusing rollback.")
            before[pack_id] = {"current": current_pin, "restore": prior_pin,
                               "current_digest": pack_digest(current_paths[pack_id]) if pack_id in current_paths else None,
                               "restore_digest": pack_digest(prior_paths[pack_id]) if pack_id in prior_paths else None}
        preview = {"intent": "rollback", "packs": before, "removals": sorted(tombstones)}
        if consent is not None and not custody.consent(consent, preview, roots=(root, prior)):
            raise PackInstallError("user_cancelled", "The pack rollback was not committed because consent was not given.")

        retired = _directory(root.parent / ".astrid-retired", create=True)
        tx = uuid4().hex
        candidate = retired / tx
        preserve: list[Path] = []  # LB-TXN r2 R2-1: a discarded generation that must stay
        candidate.mkdir(mode=0o700)
        identity = (candidate.stat().st_dev, candidate.stat().st_ino)
        published = False
        try:
            mark("preparing")
            _tree(root)
            shutil.copytree(root, candidate, dirs_exist_ok=True)
            candidate.chmod(0o700)
            (candidate / ".install.lock").unlink(missing_ok=True)
            (candidate / _STATE).unlink(missing_ok=True)
            carried_ids = frozenset(ids)
            carried = _retained_collection_bytes(candidate, carried_ids)
            admissions = dict(state["admissions"])
            for pack_id in ids:
                target = candidate / pack_id
                if target.exists():
                    shutil.rmtree(target)
                admissions.pop(pack_id, None)
                if pack_id not in tombstones and pack_id in prior_paths:
                    # Preserve the complete installed tree. In particular,
                    # M1-16 build-hook admission/artifact records belong to the
                    # generation and must follow it through rollback.
                    shutil.copytree(prior_paths[pack_id], target)
                    prior_manifest, prior_item, prior_pin_fact = prior_facts[pack_id]
                    if (pack_digest(target) != prior_item["observed_digest"]
                            or (read_pin(target / "PIN") if (target / "PIN").is_file() else {}) != prior_pin_fact
                            or validate_canonical_pack(target, expected_pack_id=pack_id).definition.to_dict() != prior_manifest):
                        raise PackInstallError("pin_mismatch", f"Historical source/PIN facts for {pack_id} changed during rollback.")
                    if pack_id in prior_state["admissions"]:
                        prior_admission = prior_state["admissions"][pack_id]
                        artifact = prior_admission.get("artifact_manifest")
                        if isinstance(artifact, dict):
                            next(record for record in prior_build_records if record[0]["pack_id"] == pack_id)
                            # Keep the exact canonical manifest bytes that were
                            # verified in the retired generation. Only the
                            # transaction/revision record receives a new epoch.
                            rollback_request = {"pack_id": pack_id, "source_digest": prior_admission["sourceDigest"],
                                                "transaction_id": tx}
                            committed_revision = state["revision"] + 1
                            manifest_bytes = _json(artifact)
                            manifest_path = target / ".build" / f'{rollback_request["source_digest"]}.artifact-manifest.json'
                            if manifest_path.read_bytes() != manifest_bytes:
                                raise PackInstallError("incomplete_artifacts", f"Retained artifact manifest for {pack_id} changed during rollback.")
                            metadata = _artifact_metadata(rollback_request, artifact, committed_revision)
                            (target / ".build" / "admission.json").write_bytes(metadata["admission.json"])
                            admissions[pack_id] = {"packId": pack_id, "sourceDigest": rollback_request["source_digest"],
                                "digest": artifact["digest"],
                                "artifactManifestDigest": hashlib.sha256(manifest_bytes).hexdigest(),
                                "artifact_manifest": artifact, "committedRevision": committed_revision, "transactionId": tx}
                        else:
                            admissions[pack_id] = prior_admission
            facts = dict(host_selected)
            for pack_id, path in _paths(candidate).items():
                if pack_id == "personal":
                    continue
                manifest, item = _facts(path)
                facts[pack_id] = item
            selected_roots = tuple(Path(value).expanduser().resolve() for value in selected_roots
                                    if Path(value).expanduser().resolve() != root.resolve())
            core_root = Path(core_root or _BUNDLED_CORE_ROOT).expanduser().resolve()
            facts, core_apis = _compose_resulting_facts(facts, _paths(candidate), selected_roots=selected_roots,
                                                       core_root=core_root, python_packages=packages,
                                                       release_pins=pins)
            contract = ImportContract(
                facade_exports=frozenset(pack_api.__all__), sdk_names=frozenset(sdk.__all__),
                core_pack_apis=core_apis)
            for pack_id, path in _paths(candidate).items():
                if pack_id == "personal":
                    continue
                manifest, item = _facts(path)
                checked = validate_pack(mode="inventory", manifest=manifest, candidate=item, selected=facts,
                                        core_api=pack_api.VERSION, release_pins=pins, python_packages=packages,
                                        import_violations=check_pack_imports(path, contract=contract, manifest=manifest))
                if not checked["ok"]:
                    raise PackInstallError(checked["code"], checked["reason"], compatibility=checked)
            # Capture and verify the complete candidate source/PIN snapshot,
            # then verify every retained admission against exact bytes before
            # writing state or exposing the directory.
            candidate_facts = {}
            for pack_id, path in _paths(candidate).items():
                if pack_id == "personal":
                    continue
                candidate_facts[pack_id] = (pack_digest(path), read_pin(path / "PIN") if (path / "PIN").is_file() else {})
            _retained_build_records(candidate, admissions, state["revision"] + 1)
            mark("prepared")
            changed_ids = tuple(sorted(set(ids) | (set(state["refresh"]["changed_ids"]) if state["refresh"] else set())))
            new_state = {"version": 1, "revision": state["revision"] + 1, "transaction_id": tx,
                         "removals": sorted(tombstones), "admissions": admissions,
                         "refresh": {"transaction_id": tx, "revision": state["revision"] + 1,
                                     "changed_ids": list(changed_ids)}}
            _write_state(candidate, new_state)
            _sync_tree(candidate)
            _sync_directory(retired)
            mark("before_swap")
            for pack_id, (digest, pin) in candidate_facts.items():
                path = candidate / pack_id
                if pack_digest(path) != digest or (read_pin(path / "PIN") if (path / "PIN").is_file() else {}) != pin:
                    raise PackInstallError("pin_mismatch", "Rollback source/PIN changed after preparation; retry the rollback.")
            for pack_id in ids:
                if pack_id in prior_facts and pack_id not in tombstones and pack_id in prior_paths:
                    _manifest, item, pin = prior_facts[pack_id]
                    path = candidate / pack_id
                    if pack_digest(path) != item["observed_digest"] or (read_pin(path / "PIN") if (path / "PIN").is_file() else {}) != pin:
                        raise PackInstallError("pin_mismatch", f"Historical source/PIN facts for {pack_id} changed before rollback publication.")
            _retained_build_records(candidate, admissions, new_state["revision"])
            if _state(candidate) != new_state:
                raise PackInstallError("invalid_state", "Prepared rollback record changed before publication; retry the transaction.")
            verified_facts, verified_apis = _compose_resulting_facts(
                facts, _paths(candidate), selected_roots=selected_roots, core_root=core_root,
                python_packages=packages, release_pins=pins)
            if verified_facts != facts or verified_apis != core_apis:
                raise PackInstallError("selected_snapshot_changed", "A selected dependency changed during rollback; retry.")
            _verify_retained_collection(root, carried_ids, carried)
            _publish_and_verify_external_roots(
                candidate, root, facts, _paths(candidate), retired, tx, new_state["revision"],
                carried_ids=carried_ids, carried=carried, preserve=preserve)
            published = True
            _sync_directory(retired)
            _sync_directory(root.parent)
            committed_paths = _paths(root)
        finally:
            if not published:
                try:
                    current = root.stat()
                    published = (current.st_dev, current.st_ino) == identity
                except OSError:
                    pass
            if not published and candidate not in preserve:
                shutil.rmtree(candidate, ignore_errors=True)
            try:
                (retired / ("." + tx + ".transaction.lock")).unlink(missing_ok=True)
            except OSError:
                pass
    # Exclusive custody is released before the host is notified (see _refresh).
    pending = _refresh(root, new_state["refresh"], refresh)
    return GroupResult(tx, new_state["revision"], changed_ids, committed_paths, pending, candidate)


__all__ = ["install_group", "rollback_group", "read_group", "read_retired_group", "retry_refresh", "group_read_lock",
           "collection_write_custody",
           "GroupResult", "GroupSnapshot", "GroupCommitError"]
