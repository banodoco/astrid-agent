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
    install_pack,
)
from astrid.pack_build import build_pack_assets
from astrid.pack_imports import ImportContract, check_pack_imports
from astrid.pack_validation import PackFacts, PinFacts, validate_pack

_STATE = ".group.json"
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
    retired_root: Path


class GroupCommitError(RuntimeError):
    """Publication occurred but its durability could not yet be confirmed.

    Recovery must sync the current collection before notifying. Never roll
    back individual packs or claim that the old group is still installed.
    """
    def __init__(self, transaction_id: str, revision: int):
        super().__init__("Group publication needs recovery; sync the collection and retry refresh.")
        self.transaction_id = transaction_id
        self.revision = revision


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
        try:
            yield
        finally:
            _HELD.reset(token)
    finally:
        os.close(fd)


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


def _verify_artifacts(request: Mapping[str, Any], result: Mapping[str, Any]) -> dict[str, Any] | None:
    try:
        return _verify_artifact_bytes(request, result)
    except PackInstallError:
        raise
    except (OSError, ValueError, TypeError, KeyError) as exc:
        raise PackInstallError("incomplete_artifacts", "Artifact file or manifest is missing/invalid; rebuild the private stage.") from exc


def _verify_artifact_bytes(request: Mapping[str, Any], result: Mapping[str, Any]) -> dict[str, Any] | None:
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
    # Generated output is only admitted from the enumerated source-addressed set.
    physical = {relative for relative, _ in _tree(root / ".build")}
    expected = {item["disk"][len(".build/"):] for item in files}
    if physical != expected:
        refuse("incomplete_artifacts")
    return {"packId": request["pack_id"], "sourceDigest": source_digest, "digest": artifact["digest"],
            "artifactManifestDigest": hashlib.sha256(_json(artifact)).hexdigest(), "artifact_manifest": artifact}


def _refresh(root: Path, state: dict[str, Any], refresh: Refresh | None) -> bool:
    pending = state["refresh"]
    if pending is None:
        return False
    if refresh is None:
        return True
    try:
        ack = refresh(transaction_id=pending["transaction_id"], committed_revision=pending["revision"],
                      changed_ids=tuple(pending["changed_ids"]))
        if (not isinstance(ack, Mapping) or type(ack.get("version")) is not int or ack.get("version") != 1 or ack.get("accepted") is not True
                or type(ack.get("revision")) is not int or ack["revision"] != pending["revision"]
                or not isinstance(ack.get("reason"), str)):
            return True
        state["refresh"] = None
        _write_state(root, state)
        return False
    except Exception:  # noqa: BLE001 - arbitrary host notification failure is retryable after commit
        # Notification/acknowledgment failure cannot turn a committed install
        # into a rollback. The committed outbox remains retryable and idempotent.
        return True


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
        return _refresh(root, state, refresh)


def install_group(*, source: str | Path, packs: Mapping[str, str], packs_root: str | Path,
                  python_packages: Mapping[str, str], revision: str | None = None,
                  origin: Literal["user", "default", "folder"] | None = None,
                  expected_digests: Mapping[str, str] | None = None,
                  selected: Mapping[str, PackFacts] | None = None,
                  release_pins: Mapping[str, PinFacts] | None = None,
                  import_contract: ImportContract | None = None,
                  builds: Mapping[str, Mapping[str, Any]] | None = None,
                  toolchain: Mapping[str, Any] | None = None, remove: tuple[str, ...] = (),
                  intent: Literal["explicit", "defaults"] = "explicit", refresh: Refresh | None = None,
                  boundary: Callable[[str], None] | None = None) -> GroupResult:
    """Validate -> build -> publish a same-source, same-commit dependency group.

    Only explicitly named ids are updated. Defaults honor durable removal
    tombstones and cannot take over user/folder packs. Dependencies are facts
    from this candidate collection plus explicitly selected host/core facts;
    installation never fetches an undeclared dependency or pack update.
    ``boundary`` is a trusted host observability hook, never pack code.
    All mutation callers must use this orchestration rather than composing
    multiple calls to the single-pack primitive against a live collection.
    """
    root = _root(packs_root)
    packs = dict(packs)
    remove = tuple(remove)
    if (intent not in {"explicit", "defaults"} or (intent == "defaults" and origin != "default")
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
    if origin == "default" and (not isinstance(revision, str) or not _SHA.fullmatch(revision)
                                 or any(i not in expected_digests for i in packs)):
        raise PackInstallError("invalid_pin", "Default groups require a full commit SHA and each expected digest.")
    release_pins = {i: dict(v) for i, v in (release_pins or {}).items()}
    host_selected = {i: dict(v) for i, v in (selected or {}).items()}
    packages = dict(python_packages)
    tx = uuid4().hex
    mark = boundary or (lambda phase: None)
    with group_read_lock(root, exclusive=True):
        root.mkdir(exist_ok=True)
        old = _state(root)
        tombstones = set(old["removals"]) | set(remove)
        if intent == "defaults":
            packs = {i: p for i, p in packs.items() if i not in tombstones}
        else:
            tombstones.difference_update(packs)
        retired = _directory(root.parent / ".astrid-retired", create=True)
        candidate = retired / tx
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
            for pack_id in remove:
                target = candidate / pack_id
                if target.exists():
                    pin = read_pin(target / "PIN") if (target / "PIN").exists() else {}
                    if pin.get("origin") == "personal":
                        raise PackInstallError("reserved_id", "Personal packs cannot be removed.")
                    shutil.rmtree(target)
            admissions = {i: a for i, a in old["admissions"].items() if i not in remove and i not in packs}
            facts = dict(host_selected)
            for pack_id, path in _paths(candidate).items():
                if pack_id not in packs:
                    manifest, item = _facts(path)
                    facts[pack_id] = item
            commit = revision
            source_text = _git_source(str(source)) if revision is not None and packs else None
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
            contract = import_contract or ImportContract(
                facade_exports=frozenset(pack_api.__all__), sdk_names=frozenset(sdk.__all__),
                core_pack_apis={i: f"astrid.packs.{i}.api" for i, f in facts.items() if f.get("origin") == "core"})
            for pack_id, subpath in sorted(packs.items()):
                install_pack(source=source, pack_id=pack_id, revision=commit, subpath=subpath, origin=origin,
                             expected_digest=expected_digests.get(pack_id), packs_root=candidate,
                             selected=facts, release_pins=release_pins, import_contract=contract, python_packages=packages)
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
            build_records = []
            for pack_id in sorted(packs):
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
                build_records.append((request, result))
                if admission is not None:
                    admissions[pack_id] = admission
                mark("built:" + pack_id)
            mark("built")
            _tree(candidate)
            for pack_id, (digest, pin) in identities.items():
                if pack_digest(candidate / pack_id) != digest or read_pin(candidate / pack_id / "PIN") != pin:
                    raise PackInstallError("pin_mismatch", "Build changed staged source/PIN; the installed group is unchanged.")
            # Recheck every output after the whole build phase: a later build
            # cannot alter an earlier pack's admitted bytes unnoticed.
            for request, result in build_records:
                _verify_artifacts(request, result)
            # A newer inventory revision subsumes an unacknowledged one. Carry
            # its changed ids forward so a delta consumer cannot miss a commit.
            pending_ids = old["refresh"]["changed_ids"] if old["refresh"] else ()
            changed_ids = tuple(sorted(set(packs) | set(remove) | set(pending_ids)))
            state = {"version": 1, "revision": old["revision"] + 1, "transaction_id": tx,
                     "removals": sorted(tombstones), "admissions": admissions,
                     "refresh": {"transaction_id": tx, "revision": old["revision"] + 1,
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
            for request, result in build_records:
                _verify_artifacts(request, result)
            if _state(candidate) != state:
                raise PackInstallError("invalid_state", "Prepared group record changed before publication; retry the transaction.")
            _publish(candidate, root)
            published = True  # Sole terminal-transition owner for this attempt.
            mark("swapped")
            try:
                _sync_directory(retired)
                _sync_directory(root.parent)
            except OSError as exc:
                raise GroupCommitError(tx, state["revision"]) from exc
            mark("durable")
            mark("before_refresh")
            pending = _refresh(root, state, refresh)
            mark("refreshed")
            return GroupResult(tx, state["revision"], changed_ids,
                               _paths(root), pending, candidate)
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
            if not published:
                shutil.rmtree(candidate, ignore_errors=True)
            shutil.rmtree(work, ignore_errors=True)
            try:
                (retired / ("." + tx + ".transaction.lock")).unlink(missing_ok=True)
            except OSError:
                pass  # Housekeeping cannot mask a publication/durability result.


__all__ = ["install_group", "read_group", "read_retired_group", "retry_refresh", "group_read_lock",
           "GroupResult", "GroupSnapshot", "GroupCommitError"]
