"""Capability package namespace.

The workspace runtime owns the product database, schema, and migration stream.
This namespace contains only executable capability packs and their source
manifests; it does not register, host, or apply a local schema.
"""

from __future__ import annotations

import os
from pathlib import Path

_PACKAGE_ROOT = Path(__file__).resolve().parent
_PERSONAL_ROOT = Path.home() / "Astrid" / "packs"
_EXTERNAL_ROOTS = [
    Path(item).expanduser()
    for item in os.environ.get("ASTRID_PACKS_PATH", "").split(os.pathsep)
    if item.strip()
]


def _resolved_roots() -> list[Path]:
    roots: list[Path] = []
    seen: set[Path] = set()
    for candidate in (_PERSONAL_ROOT, *_EXTERNAL_ROOTS):
        try:
            root = candidate.resolve(strict=True)
        except OSError:
            continue
        if not root.is_dir() or root == _PACKAGE_ROOT or root in seen:
            continue
        seen.add(root)
        roots.append(root)
    return roots


def _check_pack_name_clashes(roots: list[Path]) -> None:
    core_names = {
        child.name
        for child in _PACKAGE_ROOT.iterdir()
        if child.is_dir() and child.name != "__pycache__"
    }
    owners: dict[str, Path] = {}
    clashes: list[str] = []
    for root in roots:
        try:
            children = sorted(root.iterdir())
        except OSError:
            continue
        for child in children:
            if not child.is_dir() or child.name.startswith(".") or child.name == "__pycache__":
                continue
            if child.name in core_names:
                clashes.append(f"{child.name!r} at {child} clashes with a core pack id")
                continue
            previous = owners.get(child.name)
            if previous is not None:
                clashes.append(
                    f"{child.name!r} at {child} duplicates personal/external pack id at {previous}"
                )
            else:
                owners[child.name] = child
    if clashes:
        details = "; ".join(clashes)
        raise ImportError(f"refusing astrid.packs path extension: {details}")


_PACK_ROOTS = _resolved_roots()
_check_pack_name_clashes(_PACK_ROOTS)
__path__ = [str(_PACKAGE_ROOT), *(str(root) for root in _PACK_ROOTS)]

__all__: list[str] = []
