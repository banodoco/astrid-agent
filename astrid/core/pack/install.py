"""Verified, non-executing single-pack installation.

CLI consent belongs to the caller. Dependency-group build/epoch retirement and
journaling belong to M1-16; a sequence of these calls is not a group transaction.
The publication point is one OS rename (exchange for an existing directory).
Unsupported atomic exchange fails closed; there is no move-old-then-move-new gap.
"""

from __future__ import annotations

import ctypes
import errno
import os
import re
import shutil
import stat
import subprocess
import sys
import tempfile
from collections.abc import Callable, Mapping
from contextlib import contextmanager
from dataclasses import dataclass
from importlib import metadata
from pathlib import Path
from typing import Any, Literal
from urllib.parse import urlsplit

from astrid import pack_api, sdk
from astrid.core.pack.canonical import validate_canonical_pack
from astrid.core.pack.identity import _entry_path, pack_digest, read_pin, write_pin
from astrid.core.pack.personal import resolve_personal_packs_root
from astrid.pack_imports import ImportContract, check_pack_imports
from astrid.pack_validation import CompatibilityResult, PackFacts, PinFacts, validate_pack

_ID = re.compile(r"[a-z][a-z0-9_]*\Z")
_SHA = re.compile(r"[0-9a-f]{40}\Z")
_DIGEST = re.compile(r"[0-9a-f]{64}\Z")
# Transport metadata and unchecked generated output never enter an install.
_OMIT = {".git", ".build", ".generated", ".cache", ".pytest_cache", "__pycache__"}


class PackInstallError(ValueError):
    """A refusal before publication; the previous install is unchanged."""

    def __init__(self, code: str, reason: str, *, compatibility: CompatibilityResult | None = None):
        super().__init__(reason)
        self.code = code
        self.compatibility = compatibility


@dataclass(frozen=True)
class InstallResult:
    pack_id: str
    path: Path
    commit: str | None
    observed_digest: str
    origin: str
    changed: bool


def _directory(path: Path, *, create: bool = False) -> Path:
    """Check components before resolving, so symlinks cannot disappear in resolve()."""
    path = path.expanduser().absolute()
    if ".." in path.parts:
        raise PackInstallError("unsafe_path", "Parent traversal is not allowed.")
    current = Path(path.anchor)
    for part in path.parts[1:]:
        current /= part
        if create:
            try:
                current.mkdir()
            except FileExistsError:
                pass
        try:
            mode = current.lstat().st_mode
        except OSError as exc:
            raise PackInstallError("unsafe_path", "Directory is missing or inaccessible.") from exc
        if not stat.S_ISDIR(mode):
            raise PackInstallError("unsafe_path", "Directory paths must not contain symlinks or special files.")
    return path


def _relative(value: str, *, root_ok: bool = False) -> str:
    if value == "." and root_ok:
        return value
    if re.match(r"^[A-Za-z]:", value):
        raise PackInstallError("unsafe_path", "Drive-qualified pack paths are forbidden.")
    try:
        _entry_path(value)
    except ValueError as exc:
        raise PackInstallError("unsafe_path", "Pack paths must be confined relative paths.") from exc
    if any(part.casefold() == ".git" for part in value.split("/")):
        raise PackInstallError("unsafe_path", "Git metadata is not pack payload.")
    return value


def _tree(root: Path) -> list[tuple[str, Path]]:
    """Audit ALL entries, including excluded directories, before copying or publishing."""
    _directory(root)
    result: list[tuple[str, Path]] = []
    folded: dict[str, str] = {}

    def walk(directory: Path) -> None:
        for child in sorted(directory.iterdir()):
            relative = child.relative_to(root).as_posix()
            _entry_path(relative)
            if relative.casefold() in folded:
                raise PackInstallError("unsafe_path", "Case-folding pack paths collide.")
            folded[relative.casefold()] = relative
            mode = child.lstat().st_mode
            if stat.S_ISDIR(mode):
                # A local checkout's .git is not payload, and is never entered.
                if child.name != ".git":
                    walk(child)
            elif stat.S_ISREG(mode):
                result.append((relative, child))
            else:
                raise PackInstallError("unsafe_path", "Pack symlinks and special files are forbidden.")

    walk(root)
    return result


def _omitted(relative: str) -> bool:
    parts = relative.split("/")
    return any(part in _OMIT for part in parts) or parts[-1].endswith((".pyc", ".pyo"))


def _copy_folder(source: Path, stage: Path) -> None:
    for relative, path in _tree(source):
        if not _omitted(relative):
            target = stage / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(path, target, follow_symlinks=False)
            target.chmod(0o755 if path.stat().st_mode & 0o111 else 0o644)


def _git(work: Path, *args: str, input_bytes: bytes | None = None) -> bytes:
    # No inherited Git config, hooks, helpers, filters, alternate object stores,
    # replacement objects, credential prompts or pack-controlled environment.
    env = {
        "PATH": os.defpath, "HOME": str(work), "LC_ALL": "C",
        "GIT_CONFIG_NOSYSTEM": "1", "GIT_CONFIG_GLOBAL": os.devnull,
        "GIT_TERMINAL_PROMPT": "0", "GIT_ASKPASS": "/bin/false",
        "GIT_NO_REPLACE_OBJECTS": "1",
        "GIT_SSH_COMMAND": "ssh -F /dev/null -oBatchMode=yes",
    }
    command = [
        "git", "-c", "core.hooksPath=" + os.devnull,
        "-c", "protocol.allow=never", "-c", "protocol.file.allow=always",
        "-c", "protocol.https.allow=always", "-c", "protocol.ssh.allow=always",
        "--git-dir=" + str(work / "objects.git"), *args,
    ]
    try:
        process = subprocess.run(command, input=input_bytes, stdout=subprocess.PIPE,
                                 stderr=subprocess.PIPE, env=env, timeout=120, check=False)
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise PackInstallError("git_failed", "Git source operation failed.") from exc
    if process.returncode:
        # Never include arbitrary source URLs, credential-helper output or stderr.
        raise PackInstallError("git_failed", "Git source operation failed.")
    return process.stdout


def _git_source(source: str) -> str:
    if not source or any(c in source for c in "\x00\r\n") or source.startswith("-"):
        raise PackInstallError("invalid_source", "Invalid Git source.")
    if "://" in source:
        url = urlsplit(source)
        if (url.scheme not in {"https", "ssh", "file"} or url.password or url.query or url.fragment
                or (url.scheme == "https" and url.username)):
            raise PackInstallError("invalid_source", "Unsupported Git source or embedded credentials.")
        if url.scheme == "file":
            if url.netloc not in {"", "localhost"}:
                raise PackInstallError("invalid_source", "File sources must be local.")
            from urllib.parse import unquote
            return _directory(Path(unquote(url.path))).as_uri()
        return source
    if re.fullmatch(r"[A-Za-z0-9_.-]+@[A-Za-z0-9.-]+:[A-Za-z0-9_./-]+", source):
        return source
    return _directory(Path(source)).as_uri()


def _fetch_tree(work: Path, source: str, revision: str, subpath: str, stage: Path) -> str:
    if not revision or revision.startswith("-") or not re.fullmatch(r"[A-Za-z0-9_./-]+", revision):
        raise PackInstallError("invalid_revision", "Use a branch, tag, HEAD or full commit SHA.")
    _git(work, "init", "--bare", "--template=")
    # Fetch advertised refs, never fetch an arbitrary requested object id. A
    # server may allow fetching dangling SHAs; that does not establish trust.
    _git(work, "fetch", "--no-tags", "--no-recurse-submodules", "--", source,
         "+refs/*:refs/declared/*")
    if revision == "HEAD":
        try:
            _git(work, "fetch", "--no-tags", "--no-recurse-submodules", "--", source,
                 "+HEAD:refs/fetched-head")
        except PackInstallError as exc:
            raise PackInstallError("unreachable_revision", "The declared source has no resolvable HEAD.") from exc
    if _SHA.fullmatch(revision):
        names = [revision]
    elif revision == "HEAD":
        names = ["refs/fetched-head"]
    elif revision.startswith("refs/"):
        names = ["refs/declared/" + revision[5:]]
    else:
        names = ["refs/declared/heads/" + revision, "refs/declared/tags/" + revision]
    commits = set()
    for name in names:
        try:
            commits.add(_git(work, "rev-parse", "--verify", "--end-of-options",
                             name + "^{commit}").decode("ascii").strip())
        except PackInstallError:
            pass
    if len(commits) != 1:
        raise PackInstallError("unreachable_revision", "Revision is unavailable or ambiguous in the declared source.")
    commit = commits.pop()
    reachable = _git(work, "for-each-ref", "--format=%(refname)",
                     "--contains=" + commit, "refs/declared/")
    if not reachable.strip():
        raise PackInstallError("unreachable_revision", "Commit is not reachable from a declared source ref.")
    treeish = commit if subpath == "." else commit + ":" + subpath
    records = _git(work, "ls-tree", "-rz", "--full-tree", treeish).split(b"\0")
    entries: list[tuple[str, str, bytes]] = []
    folded: dict[str, str] = {}
    for record in records:
        if not record:
            continue
        header, name = record.split(b"\t", 1)
        mode, kind, oid = header.split()
        try:
            relative = _relative(name.decode("utf-8"))
        except UnicodeError as exc:
            raise PackInstallError("unsafe_path", "Git paths must be UTF-8.") from exc
        # Check prefixes too: Foo/a and foo/b collide on some filesystems.
        for index in range(1, len(relative.split("/")) + 1):
            prefix = "/".join(relative.split("/")[:index])
            previous = folded.setdefault(prefix.casefold(), prefix)
            if previous != prefix:
                raise PackInstallError("unsafe_path", "Case-folding Git paths collide.")
        if mode not in {b"100644", b"100755"} or kind != b"blob":
            raise PackInstallError("unsafe_path", "Git symlinks, submodules or colliding paths are forbidden.")
        entries.append((relative, mode.decode("ascii"), oid))
    for relative, mode, oid in entries:
        if _omitted(relative):
            continue
        target = stage / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        with target.open("xb") as stream:
            stream.write(_git(work, "cat-file", "blob", oid.decode("ascii")))
        target.chmod(0o755 if mode == "100755" else 0o644)
    return commit


@contextmanager
def _install_lock(root: Path):
    # Stable collection custody also serializes whole-group publication.
    # Keep the original per-directory lock and all of its trust checks.
    from astrid.core.pack.transaction import group_read_lock

    with group_read_lock(root, exclusive=True):
        try:
            import fcntl
        except ImportError as exc:
            raise PackInstallError("atomic_swap_unsupported", "Atomic pack installs require a supported POSIX filesystem.") from exc
        descriptor = os.open(root / ".install.lock", os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
        try:
            if not stat.S_ISREG(os.fstat(descriptor).st_mode) or os.fstat(descriptor).st_nlink != 1:
                raise PackInstallError("unsafe_path", "Install lock must be a regular file with one link.")
            fcntl.flock(descriptor, fcntl.LOCK_EX)
            yield
        finally:
            os.close(descriptor)


def _publish(stage: Path, destination: Path) -> None:
    if not destination.exists():
        os.rename(stage, destination)
        return
    libc = ctypes.CDLL(None, use_errno=True)
    if sys.platform.startswith("linux") and hasattr(libc, "renameat2"):
        rename = libc.renameat2
        rename.argtypes = [ctypes.c_int, ctypes.c_char_p, ctypes.c_int, ctypes.c_char_p, ctypes.c_uint]
        rename.restype = ctypes.c_int
        result = rename(-100, os.fsencode(stage), -100, os.fsencode(destination), 2)
    elif sys.platform == "darwin" and hasattr(libc, "renamex_np"):
        rename = libc.renamex_np
        rename.argtypes = [ctypes.c_char_p, ctypes.c_char_p, ctypes.c_uint]
        rename.restype = ctypes.c_int
        result = rename(os.fsencode(stage), os.fsencode(destination), 2)
    else:
        raise PackInstallError("atomic_swap_unsupported", "This platform cannot atomically exchange pack directories.")
    if result:
        error = ctypes.get_errno()
        code = "atomic_swap_unsupported" if error in {errno.ENOSYS, errno.EINVAL, errno.ENOTSUP} else "swap_failed"
        raise PackInstallError(code, "Atomic pack publication failed; the previous install is unchanged.")


def _sync_tree(root: Path) -> None:
    for _, path in _tree(root):
        with path.open("rb") as stream:
            os.fsync(stream.fileno())
    for directory in [*sorted((p for p in root.rglob("*") if p.is_dir()), reverse=True), root]:
        descriptor = os.open(directory, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)


def install_pack(*, source: str | Path, pack_id: str, revision: str | None = None,
                 subpath: str = ".", expected_digest: str | None = None,
                 origin: Literal["user", "default", "folder"] | None = None,
                 packs_root: str | Path | None = None,
                 selected: Mapping[str, PackFacts] | None = None,
                 release_pins: Mapping[str, PinFacts] | None = None,
                 import_contract: ImportContract | None = None,
                 python_packages: Mapping[str, str] | None = None,
                 before_swap: Callable[[Path], None] | None = None) -> InstallResult:
    """Stage, verify and atomically install a pack without executing its code.

    ``revision=None`` copies a local folder as UNPINNED; otherwise source is a
    Git URL/local repository and revision is a ref or full SHA. ``subpath``
    selects a pack in a monorepo. Source PINs, expected digests and release pins
    are verified, never silently replaced. Selected facts describe dependency
    closure (and may include this pack at its destination for an update).

    ``before_swap`` is a trusted host hook for build/quiescence, never a pack
    entry point. Source/PIN are rechecked afterwards. It may raise to abort.
    Callers must retire executing workers before publication (C1). Group
    journaling, browser-output verification and inventory refresh are M1-16.
    Crash residues are hidden under .install-* and are never auto-published.
    Atomic replacement is supported on Linux/macOS; others fail closed.
    """
    if not isinstance(pack_id, str) or not _ID.fullmatch(pack_id):
        raise PackInstallError("invalid_id", "Pack id must be a safe Python identifier.")
    bundled_root = Path(__file__).resolve().parents[2] / "packs"
    bundled_ids = {p.name for p in bundled_root.iterdir() if p.is_dir()}
    if pack_id in {"personal", "core", "astrid"} | bundled_ids:
        raise PackInstallError("reserved_id", "The personal and core pack identities cannot be installed over.")
    _relative(subpath, root_ok=True)
    if expected_digest is not None and not _DIGEST.fullmatch(expected_digest):
        raise PackInstallError("invalid_digest", "Expected digest must be a full lowercase SHA256.")
    origin = origin or ("folder" if revision is None else "user")
    if origin not in ({"folder"} if revision is None else {"user", "default"}):
        raise PackInstallError("invalid_origin", "Pack origin does not match its source kind.")
    if origin == "default" and (not _SHA.fullmatch(revision or "") or expected_digest is None):
        raise PackInstallError("invalid_pin", "Default packs require a full commit SHA and expected digest.")
    raw_root = packs_root if packs_root is not None else os.environ.get("ASTRID_PACKS_PATH", str(Path.home() / "Astrid/packs"))
    root = resolve_personal_packs_root(packs_root)
    if root is None or not Path(raw_root).expanduser().is_absolute():
        raise PackInstallError("unsafe_path", "No absolute packs root is configured.")
    _directory(Path(raw_root), create=True)
    destination = root / pack_id
    source_text = _git_source(str(source)) if revision is not None else _directory(Path(source)).as_uri()
    if revision is None:
        folder = _directory(Path(source) / subpath)
        if root.is_relative_to(folder):
            raise PackInstallError("unsafe_path", "A source folder cannot contain the install staging root.")
        source_text = folder.as_uri()
    elif subpath != ".":
        source_text += "#" + subpath
    selected = dict(selected or {})
    release_pins = dict(release_pins or {})
    contract = import_contract or ImportContract(
        facade_exports=frozenset(pack_api.__all__), sdk_names=frozenset(sdk.__all__),
        core_pack_apis={key: f"astrid.packs.{key}.api" for key, facts in selected.items() if facts.get("origin") == "core"},
    )
    packages = dict(python_packages) if python_packages is not None else {
        dist.metadata["Name"]: dist.version for dist in metadata.distributions() if dist.metadata["Name"]
    }
    try:
        with _install_lock(root):
            if (root / ".group.json").exists() or (root / ".group.json").is_symlink():
                raise PackInstallError("group_transaction_required", "Use install_group to update a committed dependency group.")
            if destination.is_symlink():
                raise PackInstallError("unsafe_path", "Install destination is a symlink.")
            if destination.exists():
                _tree(destination)
                existing_pin = read_pin(destination / "PIN") if (destination / "PIN").exists() else None
                if existing_pin and existing_pin["origin"] == "personal":
                    raise PackInstallError("reserved_id", "A personal pack can never be replaced.")
                if origin == "default" and (existing_pin is None or existing_pin["origin"] != "default"):
                    raise PackInstallError("default_pin_conflict", "A release default cannot overwrite a user-owned pack.")
            work = Path(tempfile.mkdtemp(prefix=".install-", dir=root))
            try:
                stage = work / pack_id
                stage.mkdir()
                if revision is None:
                    _copy_folder(folder, stage)
                    commit = None
                else:
                    commit = _fetch_tree(work, source_text.split("#", 1)[0], revision, subpath, stage)
                _tree(stage)
                digest = pack_digest(stage)
                pin: dict[str, Any] = {
                    "version": 1, "source": source_text, "commit": commit,
                    "digest_rule": "pack-tree-v1", "digest": digest if commit else None, "origin": origin,
                }
                if (stage / "PIN").exists() and read_pin(stage / "PIN") != pin:
                    raise PackInstallError("pin_mismatch", "Source PIN disagrees with the requested source or observed bytes.")
                if expected_digest is not None and digest != expected_digest:
                    raise PackInstallError("pin_mismatch", "Observed pack digest does not match the expected digest.")
                # ONE strict parser/schema and ONE pure compatibility checker.
                manifest = validate_canonical_pack(stage, expected_pack_id=pack_id).definition.to_dict()
                candidate: PackFacts = {
                    "id": pack_id, "canonical_root": str(destination), "schema_version": 3,
                    "origin": origin, "source": source_text, "commit": commit,
                    "installed_digest": digest if commit else None, "observed_digest": digest,
                    "pack_api": manifest.get("pack_api"), "dependencies": manifest.get("dependencies", {}),
                }
                compatibility = validate_pack(
                    mode="install", manifest=manifest, candidate=candidate, selected=selected,
                    core_api=pack_api.VERSION, release_pins=release_pins, python_packages=packages,
                    import_violations=check_pack_imports(stage, contract=contract, manifest=manifest),
                )
                if not compatibility["ok"]:
                    raise PackInstallError(compatibility["code"], compatibility["reason"], compatibility=compatibility)
                write_pin(stage / "PIN", pin)
                if before_swap is not None:
                    before_swap(stage)
                _tree(stage)
                if pack_digest(stage) != digest or read_pin(stage / "PIN") != pin:
                    raise PackInstallError("pin_mismatch", "Staged source changed before publication.")
                result = InstallResult(pack_id, destination, commit, digest, origin, True)
                # A host build may change excluded artifacts while source/PIN
                # stay identical (for example, a new pinned toolchain). Such a
                # request must publish its verified outputs, not discard them.
                if before_swap is None and destination.exists() and (destination / "PIN").exists():
                    if read_pin(destination / "PIN") == pin and pack_digest(destination) == digest:
                        return InstallResult(pack_id, destination, commit, digest, origin, False)
                _sync_tree(stage)
                _publish(stage, destination)
                # Publication succeeded. Housekeeping cannot turn a committed
                # install into a reported failure (or roll it back piecemeal).
                return result
            finally:
                shutil.rmtree(work, ignore_errors=True)
    except PackInstallError:
        raise
    except (OSError, ValueError) as exc:
        raise PackInstallError("install_failed", "Pack staging or validation failed; the previous install is unchanged.") from exc


__all__ = ["install_pack", "InstallResult", "PackInstallError"]
