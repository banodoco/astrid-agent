"""Stdlib-only resolution of the personal packs collection root.

Kept free of astrid.core imports so the cold-target helper archive can ship
astrid/packs/__init__.py byte-for-byte and still import it.
"""

from __future__ import annotations

import os
from pathlib import Path


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


__all__ = ["resolve_personal_packs_root"]
