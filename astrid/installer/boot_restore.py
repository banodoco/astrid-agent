"""Installer-owned validation and rollback restore for boot manifests."""

from __future__ import annotations

import json
import os
import tempfile
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from runtime_protocol import platform

BOOT_MANIFEST_FILENAME = "boot-manifest.json"

_PRE_RESTAMP_SUFFIX = ".pre-restamp"

_SECRET_WORDS = ("secret", "token", "password", "credential", "api_key", "private_key")

_MANIFEST_FIELDS = frozenset(
    {
        "schema_version",
        "profile_order",
        "profile_digests",
        "registry_digest",
        "conformance_digest",
        "fixture_digests",
    }
)


class BootManifestError(RuntimeError):
    """Base error for invalid or unsafe boot-manifest state."""


class BootManifestCorrupt(BootManifestError):
    """A stamped manifest cannot be trusted."""


def assert_secret_free(manifest: Mapping[str, Any]) -> None:
    """Reject unknown fields and recursively reject credential-shaped data."""
    unknown = set(manifest) - _MANIFEST_FIELDS
    if unknown:
        raise BootManifestError(f"boot manifest carries unknown fields: {sorted(unknown)!r}")

    def walk(value: Any, path: str = "manifest") -> None:
        if isinstance(value, Mapping):
            for key, item in value.items():
                lowered = str(key).lower()
                if any(word in lowered for word in _SECRET_WORDS):
                    raise BootManifestError(f"boot manifest is not secret-free at {path}.{key}")
                walk(item, f"{path}.{key}")
        elif isinstance(value, (list, tuple)):
            for index, item in enumerate(value):
                walk(item, f"{path}[{index}]")

    walk(manifest)
    if isinstance(manifest.get("schema_version"), bool) or not isinstance(manifest.get("schema_version"), int):
        raise BootManifestError("boot manifest schema_version must be an integer")
    for field in ("registry_digest", "conformance_digest"):
        value = manifest.get(field)
        if (
            not isinstance(value, str)
            or len(value) != 64
            or any(char not in "0123456789abcdef" for char in value)
        ):
            raise BootManifestError(
                f"boot manifest field {field!r} must be a SHA-256 digest"
            )
    for field in ("profile_digests", "fixture_digests"):
        values = manifest.get(field)
        if not isinstance(values, Mapping):
            raise BootManifestError(f"boot manifest field {field!r} must be an object")
        for key, value in values.items():
            if (
                not isinstance(key, str)
                or not isinstance(value, str)
                or len(value) != 64
                or any(char not in "0123456789abcdef" for char in value)
            ):
                raise BootManifestError(
                    f"boot manifest field {field!r} contains an invalid digest"
                )


def _reject_lexical_symlinks(path: Path, *, label: str) -> None:
    """Reject symlinks in a lexical path before any resolution occurs."""
    cursor = path
    while True:
        if cursor.is_symlink():
            raise BootManifestError(
                f"{label} contains a symlink component: {cursor}"
            )
        parent = cursor.parent
        if parent == cursor:
            break
        cursor = parent


def validate_support_root(support_root: str | Path) -> Path:
    """Require an explicit, existing non-symlink support directory."""
    root = Path(support_root).expanduser()
    if not root.is_absolute() or root.is_symlink() or not root.is_dir():
        raise BootManifestError(
            "boot manifest support root must be an existing absolute "
            f"non-symlink directory: {root}"
        )
    return root.resolve()


def validate_manifest_path(
    manifest_path: str | Path,
    support_root: str | Path,
    *,
    require_existing: bool = True,
) -> Path:
    """Validate a manifest location contained by the explicit support root."""
    raw_root = Path(support_root).expanduser()
    _reject_lexical_symlinks(raw_root, label="boot manifest support root")
    root = validate_support_root(raw_root)
    path = Path(manifest_path).expanduser()
    if path.name != BOOT_MANIFEST_FILENAME:
        raise BootManifestError(
            f"boot manifest must be named {BOOT_MANIFEST_FILENAME}: {path}"
        )
    if (
        not path.is_absolute()
        or path.is_symlink()
        or (require_existing and not path.is_file())
        or (not require_existing and path.exists() and not path.is_file())
    ):
        raise BootManifestError(
            "boot manifest must be an existing absolute regular non-symlink "
            f"file: {path}"
        )
    lexical_root = Path(os.path.abspath(raw_root))
    lexical_path = Path(os.path.abspath(path))
    try:
        lexical_path.relative_to(lexical_root)
    except ValueError as exc:
        raise BootManifestError(
            f"boot manifest must be contained by explicit support root: {lexical_path}"
        ) from exc
    _reject_lexical_symlinks(lexical_path, label="boot manifest path")
    resolved = path.resolve()
    try:
        resolved.relative_to(root)
    except ValueError as exc:
        raise BootManifestError(
            f"boot manifest must be contained by explicit support root: {resolved}"
        ) from exc
    return resolved


def _load_stored(path: Path) -> dict[str, Any]:
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise BootManifestCorrupt(f"stamped boot manifest unreadable at {path}: {exc}") from None
    if not isinstance(raw, dict):
        raise BootManifestCorrupt(f"stamped boot manifest at {path} must be an object")
    try:
        assert_secret_free(raw)
        if set(raw) != _MANIFEST_FIELDS:
            raise BootManifestError("boot manifest is missing required fields")
        order = raw["profile_order"]
        if (
            not isinstance(order, list)
            or any(not isinstance(item, str) or not item for item in order)
            or len(order) != len(set(order))
        ):
            raise BootManifestError("boot manifest profile_order must be a list of unique profile IDs")
    except BootManifestError as exc:
        raise BootManifestCorrupt(str(exc)) from exc
    return raw


def restore_pre_restamp_boot_manifest(
    support_root: str | Path, *, remove_if_missing: bool = True,
) -> None:
    """Restore retained provenance after stopping the candidate, before rollback.

    Keep the backup for interrupted rollback replay. Without one, an older
    release can stamp its own fresh identity, even for a same-schema update.
    A full workspace restore may already have restored the old stamp.
    """
    root = Path(support_root)
    if not root.exists():
        return
    path = validate_manifest_path(root / "astrid-host" / BOOT_MANIFEST_FILENAME,
                                  root, require_existing=False)
    backup = path.with_name(path.name + _PRE_RESTAMP_SUFFIX)
    _reject_lexical_symlinks(backup, label="pre-restamp backup")
    if not backup.exists():
        if remove_if_missing:
            path.unlink(missing_ok=True)
        return
    _load_stored(backup)
    handle = tempfile.NamedTemporaryFile("wb", dir=path.parent, prefix=".boot-manifest-", suffix=".tmp", delete=False)
    try:
        handle.write(backup.read_bytes())
        handle.flush()
        os.fsync(handle.fileno())
        handle.close()
        platform.replace_path(handle.name, path)
    finally:
        handle.close()
        Path(handle.name).unlink(missing_ok=True)
