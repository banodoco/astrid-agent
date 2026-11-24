"""Freshness identity of the installer's private, noneditable Astrid wheel tree.

A cache input against accidental edits and installer/updater mistakes, not
integrity against the install's own user. The manifest has an unkeyed SHA-256
in a writable version folder; that user can replace it and its digest. Code
outside the stamp (including *.pth, sitecustomize, dist-info/entry_points.txt,
bin/astrid and dependencies such as PyYAML) is not covered. A privileged user
or concurrent malicious mutation is not defeated by this check.

Own/project pack roots and mutable runtime data never receive a stamp.
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import stat
import tempfile
from pathlib import Path

from .core import Version, absolute, linked, write_json

STAMP_NAME = "install-stamp.json"
_FORMAT = "astrid-install-stamp/v2"
_WRITE_BITS = stat.S_IWUSR | stat.S_IWGRP | stat.S_IWOTH
_REPARSE_POINT = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0)


class _StampRefusal(ValueError):
    pass


class _Modified(_StampRefusal):
    def __init__(self, path: Path, folder: Path):
        super().__init__(f"shipped_file_modified:{path.relative_to(folder).as_posix()}")


def _digest(path: Path) -> str:
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def _stat_token(info: os.stat_result) -> list[int]:
    return [info.st_dev, info.st_ino, info.st_mode, info.st_nlink, info.st_size,
            info.st_mtime_ns, info.st_ctime_ns]


def _is_link(info: os.stat_result) -> bool:
    return bool(stat.S_ISLNK(info.st_mode)
                or getattr(info, "st_file_attributes", 0) & _REPARSE_POINT)


def _check_owner(info: os.stat_result, path: Path, uid: int | None) -> None:
    if uid is not None and info.st_uid != uid:
        raise _StampRefusal(f"install_owner_mismatch:{path}")


def _effective_uid() -> int | None:
    if os.name == "nt":
        return None
    uid = os.geteuid()
    if os.getuid() != uid:
        raise _StampRefusal("install_uid_mismatch")
    return uid


def _check_ancestors(path: Path) -> None:
    for entry in (path, *path.parents):
        if _is_link(entry.lstat()):
            raise _StampRefusal(f"linked_ancestor:{entry}")


def _regular_record(path: Path, uid: int | None) -> dict:
    """Read metadata only, refusing special files and naming malformed JSON."""
    info = path.lstat()
    if _is_link(info):
        raise _StampRefusal(f"linked_record:{path.name}")
    if not stat.S_ISREG(info.st_mode):
        raise _StampRefusal("stamp_manifest_mismatch")
    _check_owner(info, path, uid)
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        raise _StampRefusal(f"invalid_stamp_json:{path.name}") from exc
    if not isinstance(value, dict):
        raise _StampRefusal("stamp_manifest_mismatch")
    return value


def _stamp(payload: dict) -> str:
    data = json.dumps(payload, sort_keys=True, separators=(",", ":"),
                      ensure_ascii=True, allow_nan=False)
    return hashlib.sha256(data.encode("utf-8")).hexdigest()


def _private_readonly_file(path: Path, mode: int, links: int) -> None:
    # uv can hard-link its cache into a new POSIX venv. Do not chmod shared
    # inodes: copy only the shared shipped leaves, keeping the old mode.
    if links > 1:
        fd, name = tempfile.mkstemp(prefix=".astrid-stamp-", dir=path.parent)
        temporary = Path(name)
        try:
            with os.fdopen(fd, "wb") as target, path.open("rb") as source:
                shutil.copyfileobj(source, target)
            os.chmod(temporary, stat.S_IMODE(mode))
            os.replace(temporary, path)
        finally:
            temporary.unlink(missing_ok=True)
    os.chmod(path, stat.S_IMODE(mode) & ~_WRITE_BITS)


def _inventory(
    folder: Path, product_root: Path, *, seal: bool, hash_files: bool, uid: int | None,
) -> tuple[dict[str, str], list[str], dict[str, list[int]]]:
    """Walk the installed packages, including all precompiled bytecode.

    At seal time hash every file and record metadata after detaching links
    and removing write bits. On the cheap path, read no shipped file bytes.
    """
    files: dict[str, str] = {}
    directories: list[str] = []
    tokens: dict[str, list[int]] = {}

    def facts(path: Path, info: os.stat_result | None = None) -> os.stat_result:
        try:
            info = path.lstat() if info is None else info
            if _is_link(info):
                raise _Modified(path, folder)
            _check_owner(info, path, uid)
            return info
        except OSError as exc:
            raise _Modified(path, folder) from exc

    def visit(path: Path, observed: os.stat_result | None = None) -> None:
        info = facts(path, observed)
        relative = path.relative_to(folder).as_posix()
        if stat.S_ISDIR(info.st_mode):
            directories.append(relative)
            if not seal and info.st_mode & _WRITE_BITS:
                raise _Modified(path, folder)
            try:
                with os.scandir(path) as entries:
                    for entry in sorted(entries, key=lambda child: child.name):
                        # On Windows DirEntry.stat may omit dev, ino and nlink.
                        child_info = (None if os.name == "nt"
                                      else entry.stat(follow_symlinks=False))
                        visit(Path(entry.path), child_info)
            except OSError as exc:
                raise _Modified(path, folder) from exc
            if seal:
                os.chmod(path, stat.S_IMODE(info.st_mode) & ~_WRITE_BITS)
            after = facts(path)
            if not seal and _stat_token(info) != _stat_token(after):
                raise _Modified(path, folder)
            tokens[relative] = _stat_token(after)
        elif stat.S_ISREG(info.st_mode):
            if seal:
                _private_readonly_file(path, info.st_mode, info.st_nlink)
                info = facts(path)
            if info.st_mode & _WRITE_BITS or info.st_nlink != 1:
                raise _Modified(path, folder)
            files[relative] = ""
            if hash_files:
                try:
                    files[relative] = _digest(path)
                except OSError as exc:
                    raise _Modified(path, folder) from exc
                after = facts(path)
                if _stat_token(info) != _stat_token(after):
                    raise _Modified(path, folder)
            tokens[relative] = _stat_token(info)
        else:
            raise _Modified(path, folder)

    for path in (folder / "manifest.json", product_root / "runtime.lock.json",
                 product_root / "astrid"):
        visit(path)
    client = product_root / "astrid_runtime_client"
    if client.exists() or linked(client):
        visit(client)
    return files, sorted(directories), tokens


def write_install_stamp(version: Version, product_root: Path) -> str:
    """Seal a newly staged candidate once, after bytecode compilation."""
    folder = absolute(version.root)
    product_root = absolute(product_root)
    if not product_root.is_relative_to(folder / "py"):
        raise ValueError("The shipped tree is outside the staged virtual environment.")
    target = folder / STAMP_NAME
    if target.exists() or linked(target):
        raise ValueError("A staged install stamp must never be overwritten.")
    files, directories, tokens = _inventory(
        folder, product_root, seal=True, hash_files=True, uid=_effective_uid(),
    )
    payload = {"format": _FORMAT, "version": folder.name,
               "identity": version.document()["identity"],
               "product_root": product_root.relative_to(folder).as_posix(),
               "files": files, "directories": directories, "stats": tokens}
    stamp = _stamp(payload)
    write_json(target, {**payload, "stamp": stamp})
    os.chmod(target, stat.S_IMODE(target.stat().st_mode) & ~_WRITE_BITS)
    return stamp


def _version_folder(package_root: Path) -> Path | None:
    for parent in package_root.parents:
        if parent.name == "py" and parent.parent.parent.name == "versions":
            return parent.parent
    return None


def _valid_identity(value) -> bool:
    return (isinstance(value, dict) and type(value.get("release")) is int
            and value["release"] >= 0 and "managed_by" in value
            and value["managed_by"] in (None, "pinokio"))


def _version_record(folder: Path, uid: int | None) -> dict:
    try:
        record = _regular_record(folder / "version.json", uid)
    except FileNotFoundError as exc:
        raise _StampRefusal("missing_version_record") from exc
    for key in ("root", "python", "profile", "env", "identity"):
        if key not in record:
            raise _StampRefusal(f"missing_version_key:{key}")
    identity = record["identity"]
    if isinstance(identity, dict):
        for key in ("release", "managed_by"):
            if key not in identity:
                raise _StampRefusal(f"missing_version_key:identity.{key}")
    return record


def read_install_stamp(
    package_root: Path | None = None, *, verify: bool = False,
) -> tuple[str | None, str | None]:
    """Observe a stamp without changing the installation.

    The POSIX default checks metadata and membership, including change time;
    any stat difference is a cache miss. Windows Python 3.11 reports creation
    time rather than change time, so it also hashes the shipped bytes.

    Explicit verify hashes every shipped file, checks readonly modes and
    membership, and may attest unchanged bytes despite metadata churn. It
    never updates the recorded tokens or re-enables a default cache hit.
    """
    try:
        raw = package_root if package_root is not None else Path(__file__).absolute().parents[1]
        package = Path(os.path.abspath(Path(raw).expanduser()))
        folder = _version_folder(package)
        if package.name != "astrid" or folder is None:
            return None, "not_managed_install"
        _check_ancestors(package)
        uid = _effective_uid()
        _check_owner(folder.lstat(), folder, uid)
        product_root = package.parent
        marker_path = folder.parent.parent / "install.json"
        if not marker_path.exists() and not linked(marker_path):
            return None, "not_managed_install"
        marker = _regular_record(marker_path, uid)
        if (marker.get("owner") != "haus.astrid.runtime"
                or marker.get("managed_by") not in (None, "pinokio")):
            return None, "not_managed_install"
        target = folder / STAMP_NAME
        if not target.exists() and not linked(target):
            return None, "missing_stamp_manifest"
        target_info = target.lstat()
        if (_is_link(target_info) or not stat.S_ISREG(target_info.st_mode)
                or target_info.st_mode & _WRITE_BITS or target_info.st_nlink != 1):
            return None, "stamp_manifest_mismatch"
        _check_owner(target_info, target, uid)
        record = _version_record(folder, uid)
        profile = _regular_record(folder / "profile.json", uid)
        if (record["root"] != str(folder)
                or not isinstance(record["env"], dict)
                or record["env"].get("ASTRID_HOST_EXECUTION_PROFILE") != "installed"
                or profile.get("source_checkout") != str(product_root)):
            return None, "not_managed_install"
        manifest = _regular_record(target, uid)
        stamp = manifest.pop("stamp", None)
        if (manifest.get("format") != _FORMAT or manifest.get("version") != folder.name
                or manifest.get("product_root") != product_root.relative_to(folder).as_posix()
                or set(manifest) != {"format", "version", "identity", "product_root",
                                     "files", "directories", "stats"}
                or not isinstance(manifest["files"], dict)
                or not isinstance(manifest["directories"], list)
                or not isinstance(manifest["stats"], dict)
                or not isinstance(stamp, str) or stamp != _stamp(manifest)):
            return None, "stamp_manifest_mismatch"
        # Manager adoption is mutable installer state, not shipped content.
        # Its original value remains in the sealed record, but valid manager
        # changes do not change the version/release or the stamp.
        identity = manifest["identity"]
        if (not _valid_identity(identity) or not _valid_identity(record["identity"])
                or record["identity"]["release"] != identity["release"]):
            return None, "stamp_manifest_mismatch"
        expected = manifest["files"]
        expected_dirs = set(manifest["directories"])
        expected_tokens = manifest["stats"]
        if expected_tokens.keys() != expected.keys() | expected_dirs:
            return None, "stamp_manifest_mismatch"
        full = verify or os.name == "nt"
        files, directories, tokens = _inventory(
            folder, product_root, seal=False, hash_files=full, uid=uid,
        )
        # Report changed leaves before parent directory metadata. A removed
        # or injected file must name that file, not its containing directory.
        for relative in sorted(files.keys() | expected.keys()):
            if (relative not in files or relative not in expected
                    or (full and files[relative] != expected[relative])
                    or (not verify and tokens.get(relative) != expected_tokens.get(relative))):
                return None, f"shipped_file_modified:{relative}"
        for relative in sorted(set(directories) | expected_dirs):
            if (relative not in tokens or relative not in expected_dirs
                    or relative in files
                    or (not verify and tokens[relative] != expected_tokens[relative])):
                return None, f"shipped_file_modified:{relative}"
        return stamp, None
    except _StampRefusal as exc:
        return None, str(exc)
    except (OSError, ValueError, TypeError, KeyError):
        return None, "stamp_manifest_mismatch"
