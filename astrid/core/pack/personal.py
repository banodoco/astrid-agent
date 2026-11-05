"""Resolve and explicitly initialize the user's reserved personal pack."""

from __future__ import annotations

import os
from contextlib import contextmanager
from pathlib import Path

from astrid.pack_api import VERSION as PACK_API_VERSION

ASTRID_PACKS_PATH_ENV = "ASTRID_PACKS_PATH"
PERSONAL_PACK_ID = "personal"
_MANIFEST = (
    "schema_version: 3\n"
    "id: personal\n"
    "name: Personal\n"
    "version: 0.1.0\n"
    f'pack_api: "{PACK_API_VERSION[0]}.{PACK_API_VERSION[1]}"\n'
    "actions: {}\n"
).encode("utf-8")


def resolve_personal_packs_root(root: str | os.PathLike[str] | None = None) -> Path | None:
    """Return the canonical collection root without creating or modifying it.

    An explicit root takes precedence. Otherwise the canonical collection is
    ``~/Astrid/packs``. ``ASTRID_PACKS_PATH`` names additional collections and
    never changes the identity of the personal pack.
    """
    if root is None:
        candidate = Path.home() / "Astrid" / "packs"
    else:
        candidate = Path(root).expanduser()
    if not candidate.is_absolute():
        return None
    try:
        return candidate.resolve(strict=False)
    except (OSError, RuntimeError):
        return None


@contextmanager
def personal_pack_custody(root: str | os.PathLike[str] | None = None):
    """Hold exclusive collection custody while writing personal-pack bytes.

    The personal pack lives in the transaction-managed collection. A group
    install/update/rollback copies that collection and exchanges it later, so
    every personal-pack writer takes the same exclusive group lock and never
    races a transaction (LB-TXN r1 finding 1). Yields the resolved collection
    root. Hold it only around local writes.
    """
    packs_root = resolve_personal_packs_root(root)
    if packs_root is None:
        raise ValueError("no absolute personal packs root is configured")
    from astrid.core.pack.transaction import collection_write_custody

    with collection_write_custody(packs_root):
        yield packs_root


def ensure_personal_pack(root: str | os.PathLike[str] | None = None) -> Path:
    """Create the reserved personal pack if absent and return its directory.

    ``root`` is the packs collection root, not the personal directory. Existing
    files and manifests are left byte-for-byte unchanged. Creation happens
    under exclusive collection custody (see ``personal_pack_custody``).
    """
    with personal_pack_custody(root) as packs_root:
        return _ensure_personal_pack(packs_root)


def _ensure_personal_pack(packs_root: Path) -> Path:
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


__all__ = ["ensure_personal_pack", "personal_pack_custody", "resolve_personal_packs_root"]
