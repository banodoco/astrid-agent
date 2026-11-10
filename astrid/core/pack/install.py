"""Verified, non-executing single-pack installation.

CLI consent belongs to the caller. Dependency-group build/epoch retirement and
journaling belong to M1-16; a sequence of these calls is not a group transaction.
The publication point is one OS rename (exchange for an existing directory).
Unsupported atomic exchange fails closed; there is no move-old-then-move-new gap.
"""

from __future__ import annotations

import ctypes
import errno
import json
import logging
import os
import re
import shlex
import shutil
import stat
import subprocess
import sys
import tempfile
from collections.abc import Callable, Mapping
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal
from urllib.parse import urlsplit

from astrid import pack_api, sdk
from astrid.core.pack.canonical import validate_canonical_pack
from astrid.core.pack.identity import _entry_path, pack_digest, read_pin, write_pin
from astrid.core.pack.personal import resolve_personal_packs_root
from astrid.installer.git_lookup import _find_executable, _git_executable, _path_entries
from astrid.pack_imports import ImportContract, check_pack_imports
from astrid.pack_validation import CompatibilityResult, PackFacts, PinFacts, _declared_requirement_names, validate_pack

_ID = re.compile(r"[a-z][a-z0-9_]*\Z")
_SHA = re.compile(r"[0-9a-f]{40}\Z")
_DIGEST = re.compile(r"[0-9a-f]{64}\Z")
# Transport metadata and unchecked generated output never enter an install.
_OMIT = {".git", ".build", ".generated", ".cache", ".pytest_cache", "__pycache__"}
_LOG = logging.getLogger(__name__)


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


# Executed by the declared interpreter with site initialization disabled. Read
# exact declared metadata files; importing distributions would enumerate paths
# and could execute package/site startup code before admission.
_PYTHON_INVENTORY = """
import json, sys
from email.parser import BytesParser

packages = {}
for name, expected, path in json.load(sys.stdin):
    try:
        with open(path, 'rb') as stream:
            metadata = BytesParser().parse(stream, headersonly=True)
    except OSError:
        continue
    names = metadata.get_all('Name', [])
    versions = metadata.get_all('Version', [])
    if (len(names) == len(versions) == 1
            and names[0].lower().replace('_', '-').replace('.', '-') == name
            and versions[0] == expected):
        packages[name] = versions[0]
print(json.dumps(packages))
"""


@dataclass(frozen=True)
class LockedPythonEnvironment:
    """Host-declared interpreter and locked distribution metadata index.

    ``inventory`` maps normalized names to (locked version, absolute METADATA
    path). The host owns this declaration, never the pack or ambient sys.path.
    Only entries selected after canonical manifest parsing are read. The
    interpreter reads their installed metadata without loading site or packages.
    """

    interpreter: Path
    inventory: Mapping[str, tuple[str, Path]]
    timeout: float = 30

    def selected_packages(self, requirements: list[str]) -> dict[str, str]:
        entries = []
        for name in requirements:
            if name in self.inventory:
                version, metadata = self.inventory[name]
                if not version or not Path(metadata).is_absolute():
                    raise PackInstallError("python_inventory_failed", "Locked Python metadata must have a version and absolute path.")
                entries.append((name, version, str(metadata)))
        if not entries:
            return {}
        if not Path(self.interpreter).is_absolute():
            raise PackInstallError("python_inventory_failed", "The locked Python interpreter must have an absolute path.")
        try:
            process = subprocess.run(
                [str(self.interpreter), "-I", "-S", "-B", "-c", _PYTHON_INVENTORY],
                input=json.dumps(entries), text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                cwd=Path(self.interpreter).parent,
                env={"PATH": os.defpath, "LC_ALL": "C"}, timeout=self.timeout, check=True,
            )
            packages = json.loads(process.stdout)
            expected = {name: version for name, version, _ in entries}
            if not isinstance(packages, dict) or any(
                name not in expected or version != expected[name] for name, version in packages.items()
            ):
                raise ValueError("Unexpected locked Python inventory response")
            return packages
        except (OSError, ValueError, subprocess.SubprocessError) as exc:
            raise PackInstallError("python_inventory_failed", "The declared locked Python inventory could not be read.") from exc


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


# Windows processes need these system variables to start and load DLLs; they
# carry no Git configuration, so passing them through does not widen trust.
_WINDOWS_SYSTEM_VARIABLES = ("SYSTEMROOT", "SYSTEMDRIVE", "WINDIR", "COMSPEC", "PATHEXT")


def _askpass_program(work: Path, windows: bool) -> str:
    """A no-op credential helper: it prints nothing and refuses, so Git never prompts.

    The leading dot keeps it from colliding with any pack id, which never starts with a dot.
    """
    if windows:
        path = work / ".askpass.cmd"
        path.write_text("@echo off\r\nexit /b 1\r\n", encoding="ascii")
    else:
        path = work / ".askpass"
        path.write_text("#!/bin/sh\nexit 1\n", encoding="ascii")
        path.chmod(0o700)
    return str(path)


def _git_missing_reason(operation: str, windows: bool) -> str:
    hint = " Install Git for Windows, then try again." if windows else ""
    return f"Git source operation failed ({operation}; git not found).{hint}"


def _shell_word(path: str, windows: bool) -> str:
    # GIT_SSH_COMMAND is run through a shell; quote the resolved absolute path.
    return f'"{path}"' if windows else shlex.quote(path)


def _git_environment(work: Path, askpass: str, *, windows: bool | None = None,
                     source: Mapping[str, str] | None = None,
                     cwd: str | os.PathLike[str] | None = None) -> dict[str, str]:
    """Build the isolated Git environment; ``windows`` defaults to the running OS."""
    windows = (os.name == "nt") if windows is None else windows
    source = os.environ if source is None else source
    cwd = os.getcwd() if cwd is None else os.fspath(cwd)
    # Windows environment names are case-insensitive (``Path`` vs ``PATH``).
    lookup = {key.upper() if windows else key: value for key, value in source.items()}
    raw_path = lookup.get("PATH", os.defpath)
    # The git child gets the same filtered PATH the resolver uses, so a shell
    # lookup inside git (for ssh) can never reach the working directory.
    path_value = (";" if windows else os.pathsep).join(_path_entries(raw_path, windows=windows, cwd=cwd))
    ssh = _find_executable(raw_path, ("ssh.exe",) if windows else ("ssh",), windows=windows, cwd=cwd)
    ssh_program = _shell_word(ssh, windows) if ssh else "ssh"
    env = {
        "PATH": path_value,
        "HOME": str(work), "LC_ALL": "C",
        "GIT_CONFIG_NOSYSTEM": "1", "GIT_CONFIG_GLOBAL": os.devnull,
        "GIT_TERMINAL_PROMPT": "0", "GIT_ASKPASS": askpass,
        "GIT_NO_REPLACE_OBJECTS": "1",
        "GIT_SSH_COMMAND": f"{ssh_program} -F {os.devnull} -oBatchMode=yes",
    }
    if windows:
        env["USERPROFILE"] = str(work)
        env["TEMP"] = env["TMP"] = str(work)
        for name in _WINDOWS_SYSTEM_VARIABLES:
            if name in lookup:
                env[name] = lookup[name]
    return env


def _git(work: Path, *args: str, input_bytes: bytes | None = None) -> bytes:
    # No inherited Git config, hooks, helpers, filters, alternate object stores,
    # replacement objects, credential prompts or pack-controlled environment.
    operation = args[0] if args else "git"
    windows = os.name == "nt"
    cwd = os.getcwd()
    env = _git_environment(work, _askpass_program(work, windows), windows=windows, cwd=cwd)
    git = _git_executable(os.environ.get("PATH", os.defpath), windows=windows, cwd=cwd,
                          recorded=os.environ.get("ASTRID_GIT_EXECUTABLE"))
    if git is None:
        raise PackInstallError("git_failed", _git_missing_reason(operation, windows))
    command = [
        git, "-c", "core.hooksPath=" + os.devnull,
        "-c", "protocol.allow=never", "-c", "protocol.file.allow=always",
        "-c", "protocol.https.allow=always", "-c", "protocol.ssh.allow=always",
        "--git-dir=" + str(work / "objects.git"), *args,
    ]
    try:
        process = subprocess.run(command, input=input_bytes, stdout=subprocess.PIPE,
                                 stderr=subprocess.PIPE, env=env, timeout=120, check=False)
    except (OSError, subprocess.TimeoutExpired) as exc:
        # Diagnostic only: the operation and the error class. Never the URL or stderr.
        raise PackInstallError(
            "git_failed", f"Git source operation failed ({operation}; {type(exc).__name__}).") from exc
    if process.returncode:
        # Never include arbitrary source URLs, credential-helper output or stderr.
        raise PackInstallError(
            "git_failed", f"Git source operation failed ({operation}; exit {process.returncode}).")
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


def _fetch_tree(work: Path, source: str, revision: str, subpath: str, stage: Path,
                provenance: dict[str, str] | None = None) -> str:
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
    if provenance is not None:
        provenance["tree"] = _git(work, "rev-parse", "--verify", "--end-of-options",
                                  commit + ("^{tree}" if subpath == "." else ":" + subpath)).decode("ascii").strip()
    return commit


@contextmanager
def _install_lock(root: Path):
    # Stable collection custody also serializes whole-group publication.
    # Keep the original per-directory lock and all of its trust checks.
    from astrid.core.pack.transaction import group_read_lock

    with group_read_lock(root, exclusive=True):
        from astrid.core.pack import _filelock

        if not _filelock.locking_supported():
            raise PackInstallError("atomic_swap_unsupported", "Atomic pack installs require a supported filesystem.")
        descriptor = os.open(root / ".install.lock", os.O_RDWR | os.O_CREAT | _filelock.nofollow_flag(root / ".install.lock"), 0o600)
        try:
            if not stat.S_ISREG(os.fstat(descriptor).st_mode) or os.fstat(descriptor).st_nlink != 1:
                raise PackInstallError("unsafe_path", "Install lock must be a regular file with one link.")
            _filelock.lock_fd(descriptor)
            yield
        finally:
            os.close(descriptor)


def _unsupported_exchange(stage: Path, destination: Path) -> None:
    raise PackInstallError("atomic_swap_unsupported", "This platform cannot atomically exchange pack directories.")


def _check_exchange(result: int) -> None:
    if result:
        error = ctypes.get_errno()
        code = "atomic_swap_unsupported" if error in {errno.ENOSYS, errno.EINVAL, errno.ENOTSUP} else "swap_failed"
        raise PackInstallError(code, "Atomic pack publication failed; the previous install is unchanged.")


@dataclass(frozen=True)
class PublicationPolicy:
    """Trusted host directory exchange, selected once before installation.

    An exchange must atomically swap both paths or raise without changing them.
    It must leave the old destination at the stage path for installer cleanup.
    """

    exchange: Callable[[Path, Path], None]

    @classmethod
    def for_platform(cls, platform: str) -> PublicationPolicy:
        """Bind the native operation at the composition boundary, failing closed."""
        if platform != "darwin" and not platform.startswith("linux"):
            return cls(_unsupported_exchange)
        libc = ctypes.CDLL(None, use_errno=True)
        if platform.startswith("linux") and hasattr(libc, "renameat2"):
            rename = libc.renameat2
            rename.argtypes = [ctypes.c_int, ctypes.c_char_p, ctypes.c_int, ctypes.c_char_p, ctypes.c_uint]
            rename.restype = ctypes.c_int

            def exchange(stage: Path, destination: Path) -> None:
                _check_exchange(rename(-100, os.fsencode(stage), -100, os.fsencode(destination), 2))

            return cls(exchange)
        if platform == "darwin" and hasattr(libc, "renamex_np"):
            rename = libc.renamex_np
            rename.argtypes = [ctypes.c_char_p, ctypes.c_char_p, ctypes.c_uint]
            rename.restype = ctypes.c_int

            def exchange(stage: Path, destination: Path) -> None:
                _check_exchange(rename(os.fsencode(stage), os.fsencode(destination), 2))

            return cls(exchange)
        return cls(_unsupported_exchange)


# Existing callers share the policy constructed for this host. Callers with
# another publication contract can supply their own immutable policy explicitly.
_PUBLICATION = PublicationPolicy.for_platform(sys.platform)


def _publish(stage: Path, destination: Path, *, publication: PublicationPolicy = _PUBLICATION) -> None:
    if not destination.exists():
        os.replace(stage, destination)
        return
    publication.exchange(stage, destination)


def _sync_tree(root: Path) -> None:
    for _, path in _tree(root):
        # Windows' _commit/FlushFileBuffers requires a writable handle.
        with path.open("r+b" if os.name == "nt" else "rb") as stream:
            os.fsync(stream.fileno())
    # Windows' CRT cannot open a directory for fsync (and has no O_DIRECTORY).
    # File flushes above remain mandatory; POSIX directory errors must surface.
    if os.name == "nt":
        return
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
                 python_environment: LockedPythonEnvironment | None = None,
                 before_swap: Callable[[Path], None] | None = None,
                 publication: PublicationPolicy = _PUBLICATION) -> InstallResult:
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
    New directories publish with os.replace, including on Windows. Replacing
    an existing directory requires atomic exchange (Linux/macOS); other hosts
    fail closed rather than moving the old directory out of the way first.
    ``publication`` supplies a trusted host exchange policy; the default is
    constructed once for the host when this module is loaded.
    ``python_environment`` declares the locked interpreter and metadata index.
    This production caller constructs selected package facts from that index
    only after parsing the staged manifest, then passes them to the validator.
    Inventory keys use normalized names (lowercase, dots/underscores replaced
    by hyphens). Only declared requirement names are read. Missing inventory
    fails closed. There is no ambient fallback.
    """
    if not isinstance(pack_id, str) or not _ID.fullmatch(pack_id):
        raise PackInstallError("invalid_id", "Pack id must be a safe Python identifier.")
    bundled_root = Path(__file__).resolve().parents[2] / "packs"
    if pack_id in {"personal", "core", "astrid"} or (bundled_root / pack_id).is_dir():
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
    # The collection this call publishes into (and whose parent holds the
    # group lock file) is ``root``; create it with its parents safely before
    # the lock is opened with O_CREAT (LB-TXN r1 finding 11).
    _directory(root, create=True)
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
    phase = "lock"
    try:
        with _install_lock(root):
            phase = "inspect_destination"
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
            phase = "create_stage"
            work = Path(tempfile.mkdtemp(prefix=".install-", dir=root))
            try:
                stage = work / pack_id
                stage.mkdir()
                if revision is None:
                    phase = "copy_source"
                    _copy_folder(folder, stage)
                    commit = None
                else:
                    phase = "fetch_source"
                    commit = _fetch_tree(work, source_text.split("#", 1)[0], revision, subpath, stage)
                phase = "verify_source"
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
                phase = "validate"
                manifest = validate_canonical_pack(stage, expected_pack_id=pack_id).definition.to_dict()
                requirements = _declared_requirement_names(manifest) or []
                if python_environment is not None:
                    packages = python_environment.selected_packages(requirements)
                else:
                    packages = {}
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
                phase = "write_pin"
                write_pin(stage / "PIN", pin)
                if before_swap is not None:
                    phase = "before_swap"
                    before_swap(stage)
                phase = "verify_stage"
                _tree(stage)
                if pack_digest(stage) != digest or read_pin(stage / "PIN") != pin:
                    raise PackInstallError("pin_mismatch", "Staged source changed before publication.")
                result = InstallResult(pack_id, destination, commit, digest, origin, True)
                # A host build may change excluded artifacts while source/PIN
                # stay identical (for example, a new pinned toolchain). Such a
                # request must publish its verified outputs, not discard them.
                phase = "compare_destination"
                if before_swap is None and destination.exists() and (destination / "PIN").exists():
                    if read_pin(destination / "PIN") == pin and pack_digest(destination) == digest:
                        return InstallResult(pack_id, destination, commit, digest, origin, False)
                phase = "sync_tree"
                _sync_tree(stage)
                phase = "publish"
                _publish(stage, destination, publication=publication)
                # Publication succeeded. Housekeeping cannot turn a committed
                # install into a reported failure (or roll it back piecemeal).
                return result
            finally:
                shutil.rmtree(work, ignore_errors=True)
    except PackInstallError:
        raise
    except (OSError, ValueError) as exc:
        # Exception messages/filenames may contain credentials or private paths.
        # Keep the receipt useful without logging those or a chained traceback.
        number = exc.errno if isinstance(exc, OSError) else None
        number = number if isinstance(number, int) else None
        diagnostic = f"stage={phase}, error={type(exc).__name__}, errno={number}"
        _LOG.warning("Pack install failed (%s).", diagnostic)
        raise PackInstallError(
            "install_failed",
            f"Pack staging or validation failed ({diagnostic}); the previous install is unchanged.",
        ) from None


__all__ = ["install_pack", "InstallResult", "PackInstallError", "PublicationPolicy", "LockedPythonEnvironment"]
