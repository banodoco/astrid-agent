"""Stable content identity and local PIN helpers for source packs."""

from __future__ import annotations

import hashlib
import os
import re
import tempfile
import unicodedata
from pathlib import Path


_PIN_NAME = "PIN"
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_EXCLUDED_DIRS = frozenset(
    {
        ".build",
        ".cache",
        ".git",
        ".mypy_cache",
        ".nox",
        ".pytest_cache",
        ".ruff_cache",
        ".tox",
        "__pycache__",
        "cache",
        "caches",
    }
)
_CHUNK_SIZE = 1024 * 1024


def pack_digest(pack_root: str | os.PathLike[str]) -> str:
    """Return a SHA-256 digest of a pack's source files.

    Relative POSIX paths and raw file bytes are hashed in lexical path order.
    File metadata, the ``PIN`` file, build output, and common cache trees are
    not part of the identity. Symlinks are rejected so the digest cannot
    depend on external files or platform-specific link behavior.
    """
    root = Path(pack_root)
    if not root.is_dir():
        raise ValueError(f"pack root is not a directory: {root}")

    entries: list[tuple[str, Path]] = []
    for current, dirnames, filenames in os.walk(root, topdown=True, followlinks=False):
        current_path = Path(current)
        kept_dirs: list[str] = []
        for name in dirnames:
            child = current_path / name
            if name in _EXCLUDED_DIRS:
                continue
            if child.is_symlink():
                raise ValueError(f"pack contains a symlink: {child}")
            kept_dirs.append(name)
        dirnames[:] = kept_dirs

        for name in filenames:
            if name == _PIN_NAME:
                continue
            path = current_path / name
            relative = unicodedata.normalize("NFC", path.relative_to(root).as_posix())
            if "\\" in relative:
                raise ValueError(f"pack path is not portable across operating systems: {relative!r}")
            if any(part in _EXCLUDED_DIRS for part in Path(relative).parts):
                continue
            if path.is_symlink():
                raise ValueError(f"pack contains a symlink: {path}")
            if path.is_file():
                entries.append((relative, path))

    paths = [relative for relative, _ in entries]
    if len(paths) != len(set(paths)):
        raise ValueError("pack contains paths that collide after Unicode normalization")

    digest = hashlib.sha256()
    digest.update(b"astrid-pack-digest-v1\0")
    for relative, path in sorted(entries, key=lambda item: item[0]):
        encoded_path = relative.encode("utf-8")
        digest.update(len(encoded_path).to_bytes(8, "big"))
        digest.update(encoded_path)
        size = path.stat().st_size
        digest.update(size.to_bytes(8, "big"))
        with path.open("rb") as source:
            for chunk in iter(lambda: source.read(_CHUNK_SIZE), b""):
                digest.update(chunk)
    return digest.hexdigest()


def read_pin(pack_root: str | os.PathLike[str]) -> str | None:
    """Read a pack's lowercase SHA-256 PIN, or return ``None`` if absent."""
    pin_path = Path(pack_root) / _PIN_NAME
    try:
        value = pin_path.read_text(encoding="ascii").strip()
    except FileNotFoundError:
        return None
    if not _SHA256_RE.fullmatch(value):
        raise ValueError(f"invalid pack PIN at {pin_path}")
    return value


def write_pin(pack_root: str | os.PathLike[str], digest: str) -> None:
    """Atomically write a validated lowercase SHA-256 digest to ``PIN``."""
    if not isinstance(digest, str) or not _SHA256_RE.fullmatch(digest):
        raise ValueError("pack PIN must be a lowercase 64-character SHA-256 digest")

    root = Path(pack_root)
    if not root.is_dir():
        raise ValueError(f"pack root is not a directory: {root}")
    pin_path = root / _PIN_NAME
    fd, temporary_name = tempfile.mkstemp(prefix=".PIN.", suffix=".tmp", dir=root)
    temporary_path = Path(temporary_name)
    try:
        with os.fdopen(fd, "w", encoding="ascii", newline="\n") as stream:
            stream.write(digest + "\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary_path, pin_path)
    finally:
        try:
            temporary_path.unlink()
        except FileNotFoundError:
            pass
