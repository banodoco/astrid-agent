"""Resolve and explicitly initialize the user's reserved personal pack."""

from __future__ import annotations

import os
from pathlib import Path


ASTRID_PACKS_PATH_ENV = "ASTRID_PACKS_PATH"
PERSONAL_PACK_ID = "personal"
_MANIFEST = (
    "schema_version: 3\n"
    "id: personal\n"
    "name: Personal\n"
    "version: 0.1.0\n"
    'pack_api: "1.0"\n'
    "actions: {}\n"
).encode("utf-8")


def resolve_personal_packs_root(root: str | os.PathLike[str] | None = None) -> Path | None:
    """Return the canonical collection root without creating or modifying it.

    An explicit root takes precedence. Otherwise ``ASTRID_PACKS_PATH`` replaces
    the default ``~/Astrid/packs`` root; an empty override disables it.
    """
    if root is None:
        configured = os.environ.get(ASTRID_PACKS_PATH_ENV)
        if configured is None:
            candidate = Path.home() / "Astrid" / "packs"
        elif not configured.strip():
            return None
        else:
            candidate = Path(configured).expanduser()
    else:
        candidate = Path(root).expanduser()
    if not candidate.is_absolute():
        return None
    try:
        return candidate.resolve(strict=False)
    except (OSError, RuntimeError):
        return None


def ensure_personal_pack(root: str | os.PathLike[str] | None = None) -> Path:
    """Create the reserved personal pack if absent and return its directory.

    ``root`` is the packs collection root, not the personal directory. Existing
    files and manifests are left byte-for-byte unchanged.
    """
    packs_root = resolve_personal_packs_root(root)
    if packs_root is None:
        raise ValueError("no absolute personal packs root is configured")
    personal = packs_root / PERSONAL_PACK_ID
    personal.mkdir(parents=True, exist_ok=True)
    if personal.is_symlink() or not personal.is_dir():
        raise NotADirectoryError(f"reserved personal pack path is not a directory: {personal}")
    manifest = personal / "pack.yaml"
    try:
        descriptor = os.open(manifest, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except FileExistsError:
        return personal
    with os.fdopen(descriptor, "wb") as stream:
        stream.write(_MANIFEST)
    return personal


__all__ = ["ensure_personal_pack", "resolve_personal_packs_root"]
