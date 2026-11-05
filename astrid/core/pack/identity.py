"""Stable pack source digests and the on-disk PIN record."""

from __future__ import annotations

import hashlib
import json
import os
import re
import stat
import tempfile
import unicodedata
from collections.abc import Mapping
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit


_EXCLUDED_DIRS = {".git", ".build", ".generated", ".cache", ".pytest_cache", "__pycache__"}
_SHA256_RE = re.compile(r"[0-9a-f]{64}\Z")
_COMMIT_RE = re.compile(r"[0-9a-f]{40}\Z")
_PIN_FIELDS = {"version", "source", "commit", "digest_rule", "digest", "origin"}
_PIN_ORIGINS = {"default", "user", "folder", "personal"}


def _entry_path(relative: str) -> bytes:
    """Validate a POSIX relative path and return its UTF-8 representation."""
    if not relative or relative.startswith("/") or "\\" in relative:
        raise ValueError(f"unsafe pack path: {relative!r}")
    if "\x00" in relative or "\n" in relative or "\r" in relative:
        raise ValueError(f"unsafe pack path: {relative!r}")
    if unicodedata.normalize("NFC", relative) != relative:
        raise ValueError(f"pack path is not NFC-normalized: {relative!r}")
    parts = relative.split("/")
    if any(part in {"", ".", ".."} for part in parts):
        raise ValueError(f"unsafe pack path: {relative!r}")
    try:
        return relative.encode("utf-8")
    except UnicodeEncodeError as exc:
        raise ValueError(f"pack path is not valid UTF-8: {relative!r}") from exc


def pack_digest(root: str | os.PathLike[str]) -> str:
    """Return the pack-tree-v1 digest for a source tree.

    Links and special files are refused before exclusions are applied. Excluded
    real directories are pruned without visiting their children.
    """
    root_path = Path(root)
    root_stat = root_path.lstat()
    if not stat.S_ISDIR(root_stat.st_mode):
        raise ValueError("pack root must be a real directory")

    files: list[tuple[bytes, Path]] = []
    folded: set[str] = set()

    def walk(directory: Path, prefix: str = "") -> None:
        try:
            entries = sorted(os.scandir(directory), key=lambda item: item.name.encode("utf-8"))
        except (UnicodeEncodeError, OSError) as exc:
            raise ValueError(f"cannot enumerate pack directory: {directory}") from exc
        for entry in entries:
            relative = f"{prefix}/{entry.name}" if prefix else entry.name
            path_bytes = _entry_path(relative)
            folded_name = relative.casefold()
            if folded_name in folded:
                raise ValueError(f"case-fold path collision: {relative!r}")
            folded.add(folded_name)

            try:
                entry_stat = entry.stat(follow_symlinks=False)
            except OSError as exc:
                raise ValueError(f"cannot inspect pack entry: {relative!r}") from exc
            if stat.S_ISLNK(entry_stat.st_mode):
                raise ValueError(f"pack symlinks are not allowed: {relative!r}")
            if stat.S_ISDIR(entry_stat.st_mode):
                if entry.name not in _EXCLUDED_DIRS:
                    walk(Path(entry.path), relative)
                continue
            if not stat.S_ISREG(entry_stat.st_mode):
                raise ValueError(f"pack entry is not a regular file or directory: {relative!r}")
            if entry.name == ".git" or (prefix == "" and entry.name == "PIN"):
                continue
            if entry.name.endswith((".pyc", ".pyo")):
                continue
            files.append((path_bytes, Path(entry.path)))

    walk(root_path)
    files.sort(key=lambda item: item[0])
    digest_input = bytearray()
    for relative_bytes, path in files:
        try:
            content = path.read_bytes()
        except OSError as exc:
            raise ValueError(f"cannot read pack file: {path}") from exc
        file_hash = hashlib.sha256(content).hexdigest().encode("ascii")
        digest_input.extend(relative_bytes)
        digest_input.extend(b"\0")
        digest_input.extend(file_hash)
        digest_input.extend(b"\n")
    return hashlib.sha256(digest_input).hexdigest()


def _validate_pin(pin: Mapping[str, Any]) -> dict[str, Any]:
    if not isinstance(pin, Mapping) or set(pin) != _PIN_FIELDS:
        raise ValueError("PIN must contain exactly the version, source, commit, digest_rule, digest, and origin fields")
    value = dict(pin)
    if not isinstance(value["version"], int) or isinstance(value["version"], bool) or value["version"] != 1:
        raise ValueError("PIN version must be 1")
    if (
        not isinstance(value["source"], str)
        or not value["source"]
        or any(character in value["source"] for character in ("\x00", "\n", "\r"))
    ):
        raise ValueError("PIN source must be a non-empty single-line string")
    if value["digest_rule"] != "pack-tree-v1":
        raise ValueError("PIN digest_rule must be 'pack-tree-v1'")
    origin = value["origin"]
    if not isinstance(origin, str) or origin not in _PIN_ORIGINS:
        raise ValueError("PIN origin must be default, user, folder, or personal")
    commit, digest = value["commit"], value["digest"]
    if origin in {"folder", "personal"}:
        if commit is not None or digest is not None:
            raise ValueError("folder and personal PINs must be unpinned")
        source = urlsplit(value["source"])
        if (
            source.scheme != "file"
            or source.netloc.lower() not in {"", "localhost"}
            or not source.path.startswith("/")
            or source.query
            or source.fragment
        ):
            raise ValueError("folder and personal PIN sources must be local file URIs")
    else:
        if not isinstance(commit, str) or not _COMMIT_RE.fullmatch(commit):
            raise ValueError("pinned PIN commit must be a full lowercase Git SHA")
        if not isinstance(digest, str) or not _SHA256_RE.fullmatch(digest):
            raise ValueError("pinned PIN digest must be a full lowercase SHA256")
    return value


def read_pin(path: str | os.PathLike[str]) -> dict[str, Any]:
    """Read and validate a newline-terminated C4 PIN JSON object."""
    pin_path = Path(path)
    raw = pin_path.read_bytes()
    if not raw.endswith(b"\n"):
        raise ValueError("PIN must end with a newline")

    def unique_pairs(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for key, value in pairs:
            if key in result:
                raise ValueError(f"duplicate PIN field: {key}")
            result[key] = value
        return result

    try:
        value = json.loads(raw.decode("utf-8"), object_pairs_hook=unique_pairs)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError("PIN must be valid UTF-8 JSON") from exc
    return _validate_pin(value)


def write_pin(path: str | os.PathLike[str], pin: Mapping[str, Any]) -> None:
    """Validate and atomically write a newline-terminated C4 PIN."""
    value = _validate_pin(pin)
    destination = Path(path)
    destination.parent.mkdir(parents=True, exist_ok=True)
    try:
        current = destination.lstat()
    except FileNotFoundError:
        current = None
    if current is not None and not stat.S_ISREG(current.st_mode):
        raise ValueError("PIN destination must be a regular file")
    encoded = (json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False) + "\n").encode("utf-8")
    fd, temporary_name = tempfile.mkstemp(prefix=f".{destination.name}.", suffix=".tmp", dir=destination.parent)
    try:
        with os.fdopen(fd, "wb") as stream:
            stream.write(encoded)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary_name, destination)
    except BaseException:
        try:
            os.unlink(temporary_name)
        except FileNotFoundError:
            pass
        raise
