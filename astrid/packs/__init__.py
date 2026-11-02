"""Capability package namespace.

Folder packs live in ``~/Astrid/packs`` and in the additional roots listed
in ``ASTRID_PACKS_PATH``. Importing this module never creates those roots or
reads pack manifests.
"""

from __future__ import annotations

import os
import sys
from importlib.abc import MetaPathFinder
from importlib.machinery import ModuleSpec, PathFinder
from pathlib import Path

from astrid.core.pack.personal import resolve_personal_packs_root

__all__: list[str] = []

_CORE_ROOT = Path(__file__).resolve().parent
_PERSONAL_ROOT = resolve_personal_packs_root()


def _roots() -> tuple[Path, ...]:
    candidates = [
        Path(item).expanduser()
        for item in os.environ.get("ASTRID_PACKS_PATH", "").split(os.pathsep)
        if item.strip()
    ]
    roots: list[Path] = []
    for candidate in (_PERSONAL_ROOT, *candidates):
        if candidate is None:
            continue
        try:
            root = candidate.resolve()
        except (OSError, RuntimeError) as exc:
            raise ImportError(f"cannot resolve pack root {candidate}: {exc}") from exc
        if root != _CORE_ROOT and root not in roots:
            roots.append(root)
    return tuple(roots)


def _child_directory_names(root: Path) -> tuple[str, ...]:
    try:
        with os.scandir(root) as entries:
            return tuple(sorted(
                entry.name for entry in entries
                if not entry.name.startswith(".")
                and entry.name != "__pycache__"
                and entry.is_dir()
            ))
    except (FileNotFoundError, NotADirectoryError):
        return ()
    except OSError as exc:
        raise ImportError(f"cannot scan pack root {root}: {exc}") from exc


def _check_collisions(roots: tuple[Path, ...]) -> None:
    owners = {name: _CORE_ROOT / name for name in _child_directory_names(_CORE_ROOT)}
    for root in roots:
        for name in _child_directory_names(root):
            path = root / name
            if name == "personal" and root != _PERSONAL_ROOT:
                raise ImportError(
                    f"refusing astrid.packs: reserved personal directory at {path}; "
                    f"canonical root is {_PERSONAL_ROOT}"
                )
            if name in owners:
                raise ImportError(
                    f"refusing astrid.packs: pack id {name!r} at {path} "
                    f"collides with {owners[name]}"
                )
            owners[name] = path


_PACK_ROOTS = _roots()
_check_collisions(_PACK_ROOTS)
for _root in _PACK_ROOTS:
    if _root not in (Path(item).resolve() for item in __path__):
        __path__.append(str(_root))


class _PackCollisionFinder(MetaPathFinder):
    """Recheck newly created packs before resolving any top-level pack."""

    def find_spec(self, fullname: str, path=None, target=None) -> ModuleSpec | None:
        if not fullname.startswith(__name__ + ".") or "." in fullname[len(__name__) + 1:]:
            return None
        _check_collisions(_PACK_ROOTS)
        name = fullname.rsplit(".", 1)[-1]
        if name in _child_directory_names(_CORE_ROOT):
            return PathFinder.find_spec(fullname, [str(_CORE_ROOT)], target)
        return None


if not any(
    type(finder).__name__ == "_PackCollisionFinder" and type(finder).__module__ == __name__
    for finder in sys.meta_path
):
    _index = next(
        (index for index, finder in enumerate(sys.meta_path) if finder is PathFinder),
        len(sys.meta_path),
    )
    sys.meta_path.insert(_index, _PackCollisionFinder())
