"""Content identity of the installer's private, noneditable Astrid wheel tree.

This is a cache input, not a signature or an admission rule. Never grant a
stamp to mutable/project/own pack roots. No cache or Runtime state is written
by the reader.
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import stat
import tempfile
from pathlib import Path

from .core import Version, absolute, install_identity, linked, read_json, write_json

STAMP_NAME = "install-stamp.json"
_FORMAT = "astrid-install-stamp/v1"
_WRITE_BITS = stat.S_IWUSR | stat.S_IWGRP | stat.S_IWOTH


class _Modified(ValueError):
    def __init__(self, path: Path, folder: Path):
        super().__init__(f"shipped_file_modified:{path.relative_to(folder).as_posix()}")


def _digest(path: Path) -> str:
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def _regular_record(path: Path) -> dict:
    """Do not open linked or special installer records."""
    path = absolute(path)
    if not stat.S_ISREG(path.lstat().st_mode):
        raise ValueError("An install stamp record is not a regular file.")
    return read_json(path)


def _stamp(payload: dict) -> str:
    data = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
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


def _inventory(folder: Path, product_root: Path, *, seal: bool) -> tuple[dict[str, str], list[str]]:
    """Enumerate exactly the shipped trees; mutable runtime roots stay outside.

    Generated CPython bytecode is not shipped. Only ordinary .pyc leaves in
    __pycache__ may be ignored; a link or another kind of resource is a refusal.
    This also permits Python's normal bytecode writes on Windows, whose
    directory read-only attribute is advisory.
    """
    files: dict[str, str] = {}
    directories: list[str] = []

    def facts(path: Path):
        try:
            info = path.lstat()
            if linked(path):
                raise _Modified(path, folder)
            if os.name != "nt" and info.st_uid != os.geteuid():
                raise _Modified(path, folder)
            return info
        except OSError as exc:
            raise _Modified(path, folder) from exc

    def visit(path: Path) -> None:
        info = facts(path)
        relative = path.relative_to(folder).as_posix()
        if stat.S_ISDIR(info.st_mode):
            if path.name == "__pycache__":
                for child in sorted(path.iterdir()):
                    child_info = facts(child)
                    if child.suffix != ".pyc" or not stat.S_ISREG(child_info.st_mode):
                        raise _Modified(child, folder)
                return
            directories.append(relative)
            if not seal and info.st_mode & _WRITE_BITS:
                raise _Modified(path, folder)
            for child in sorted(path.iterdir()):
                visit(child)
            if seal:
                os.chmod(path, stat.S_IMODE(info.st_mode) & ~_WRITE_BITS)
        elif stat.S_ISREG(info.st_mode):
            if seal:
                _private_readonly_file(path, info.st_mode, info.st_nlink)
            elif info.st_mode & _WRITE_BITS or info.st_nlink != 1:
                raise _Modified(path, folder)
            before = facts(path)
            try:
                files[relative] = _digest(path)
            except OSError as exc:
                raise _Modified(path, folder) from exc
            after = facts(path)
            if (before.st_dev, before.st_ino, before.st_mode, before.st_size,
                    before.st_mtime_ns, before.st_ctime_ns) != (
                    after.st_dev, after.st_ino, after.st_mode, after.st_size,
                    after.st_mtime_ns, after.st_ctime_ns):
                raise _Modified(path, folder)
        else:
            raise _Modified(path, folder)

    # The wheel's own Astrid package includes core pack manifests, models.yaml,
    # shipped packs and Python modules. The API client is a second wheel
    # package. Site-packages itself also holds unrelated dependency packages;
    # never stamp them, the source checkout, or user/project pack collections.
    for path in (folder / "manifest.json", product_root / "runtime.lock.json",
                 product_root / "astrid"):
        visit(path)
    client = product_root / "astrid_runtime_client"
    if client.exists() or linked(client):
        visit(client)
    return files, sorted(directories)


def write_install_stamp(version: Version, product_root: Path) -> str:
    """Seal a newly staged candidate, once, after its final shipped-file write."""
    folder = absolute(version.root)
    product_root = absolute(product_root)
    if not product_root.is_relative_to(folder / "py"):
        raise ValueError("The shipped tree is outside the staged virtual environment.")
    target = folder / STAMP_NAME
    if target.exists() or linked(target):
        raise ValueError("A staged install stamp must never be overwritten.")
    files, directories = _inventory(folder, product_root, seal=True)
    payload = {"format": _FORMAT, "version": folder.name,
               "identity": version.document()["identity"],
               "product_root": product_root.relative_to(folder).as_posix(),
               "files": files, "directories": directories}
    stamp = _stamp(payload)
    write_json(target, {**payload, "stamp": stamp})
    os.chmod(target, stat.S_IMODE(target.stat().st_mode) & ~_WRITE_BITS)
    return stamp


def _version_folder(package_root: Path) -> Path | None:
    for parent in package_root.parents:
        if parent.name == "py" and parent.parent.parent.name == "versions":
            return parent.parent
    return None


def read_install_stamp(package_root: Path | None = None) -> tuple[str | None, str | None]:
    """Verify the entire shipped file set, modes and bytes without repairing it."""
    try:
        package = absolute(package_root if package_root is not None else Path(__file__).absolute().parents[1])
        folder = _version_folder(package)
        if package.name != "astrid" or folder is None:
            return None, "not_managed_install"
        product_root = package.parent
        marker_path = folder.parent.parent / "install.json"
        if not marker_path.exists() or _regular_record(marker_path).get("owner") != "haus.astrid.runtime":
            return None, "not_managed_install"
        target = folder / STAMP_NAME
        if not target.exists():
            return None, "missing_stamp_manifest"
        target_info = target.lstat()
        if (linked(target) or not stat.S_ISREG(target_info.st_mode)
                or target_info.st_mode & _WRITE_BITS or target_info.st_nlink != 1):
            return None, "stamp_manifest_mismatch"
        record = _regular_record(folder / "version.json")
        profile = _regular_record(folder / "profile.json")
        if (record.get("root") != str(folder)
                or not isinstance(record.get("env"), dict)
                or record["env"].get("ASTRID_HOST_EXECUTION_PROFILE") != "installed"
                or profile.get("source_checkout") != str(product_root)):
            return None, "not_managed_install"
        manifest = _regular_record(target)
        stamp = manifest.pop("stamp", None)
        if (manifest.get("format") != _FORMAT or manifest.get("version") != folder.name
                or manifest.get("product_root") != product_root.relative_to(folder).as_posix()
                or set(manifest) != {"format", "version", "identity", "product_root", "files", "directories"}
                or not isinstance(manifest.get("files"), dict)
                or not isinstance(manifest.get("directories"), list)
                or not isinstance(stamp, str) or stamp != _stamp(manifest)):
            return None, "stamp_manifest_mismatch"
        # Check bytes before deriving identity, so edits to manifest.json are
        # reported with the same precise path as any other shipped-file edit.
        files, directories = _inventory(folder, product_root, seal=False)
        expected = manifest["files"]
        for relative in sorted(files.keys() | expected.keys()):
            if files.get(relative) != expected.get(relative):
                return None, f"shipped_file_modified:{relative}"
        if directories != manifest["directories"]:
            changed = sorted(set(directories) ^ set(manifest["directories"]))
            return None, f"shipped_file_modified:{changed[0]}" if changed else "stamp_manifest_mismatch"
        if (manifest["identity"] != record.get("identity")
                or manifest["identity"] != install_identity(folder)):
            return None, "stamp_manifest_mismatch"
        return stamp, None
    except _Modified as exc:
        return None, str(exc)
    except (OSError, ValueError, TypeError, KeyError):
        return None, "stamp_manifest_mismatch"
