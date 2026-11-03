"""Capability package namespace.

The workspace runtime owns the product database, schema, and migration stream.
This namespace contains executable capability packs and their source
manifests; it does not register, host, or apply a local schema.

Installed folder packs live at ``~/Astrid/packs`` by default. The single
``ASTRID_PACKS_PATH`` setting replaces that visible root. This module only
looks at immediate directory names: manifests are validated later, when a
pack is selected for discovery or execution.
"""

from __future__ import annotations

import logging
import os
import re
import stat
import sys
from importlib.abc import MetaPathFinder
from importlib.machinery import ModuleSpec, PathFinder
from pathlib import Path
from typing import Sequence

from astrid.core.pack.personal import resolve_personal_packs_root


__all__: list[str] = []

_LOG = logging.getLogger(__name__)
_PACK_ID = re.compile(r"[a-z][a-z0-9_]*\Z")
_PERSONAL_ID = "personal"
_CORE_ROOT = Path(__file__).resolve().parent


def _child_directory_names(root: Path) -> tuple[str, ...]:
    """Return sorted immediate real-directory names without reading contents."""
    try:
        with os.scandir(root) as entries:
            return tuple(sorted(
                entry.name
                for entry in entries
                if _PACK_ID.fullmatch(entry.name)
                and stat.S_ISDIR(entry.stat(follow_symlinks=False).st_mode)
            ))
    except (FileNotFoundError, NotADirectoryError, PermissionError, OSError):
        return ()


def _visible_packs_root() -> Path | None:
    """Expose an existing configurable folder-pack root without creating it."""
    resolved = resolve_personal_packs_root()
    if resolved is None or not resolved.is_dir():
        return None
    if resolved == _CORE_ROOT:
        return None
    return resolved


class _CorePackCollisionFinder(MetaPathFinder):
    """Keep same-name core packages authoritative over external folders."""

    def __init__(self, core_root: Path, core_names: Sequence[str]) -> None:
        self._core_root = core_root
        self._core_names = frozenset(core_names)

    def find_spec(self, fullname: str, path=None, target=None) -> ModuleSpec | None:
        if not fullname.startswith(__name__ + ".") or "." in fullname[len(__name__) + 1:]:
            return None
        pack_id = fullname.rsplit(".", 1)[-1]
        if pack_id == _PERSONAL_ID:
            raise ModuleNotFoundError(
                f"{fullname!r} is reserved for the personal pack collection, not a pack id"
            )
        if pack_id not in self._core_names:
            return None
        # PathFinder sees only the source-tree directory here. Returning its
        # regular or namespace spec prevents an external same-name package
        # from winning merely because it has an __init__.py.
        return PathFinder.find_spec(fullname, [str(self._core_root)], target)


_CORE_PACK_NAMES = _child_directory_names(_CORE_ROOT)
_collision_finder = _CorePackCollisionFinder(_CORE_ROOT, _CORE_PACK_NAMES)
if not any(isinstance(finder, _CorePackCollisionFinder) for finder in sys.meta_path):
    # Run immediately before the standard PathFinder while preserving other
    # finders (including instrumentation) already installed by the host.
    _path_finder_index = next(
        (index for index, finder in enumerate(sys.meta_path) if finder is PathFinder),
        len(sys.meta_path),
    )
    sys.meta_path.insert(_path_finder_index, _collision_finder)


_EXTERNAL_ROOT = _visible_packs_root()
if _EXTERNAL_ROOT is not None:
    _external_names = _child_directory_names(_EXTERNAL_ROOT)
    for _name in _external_names:
        if _name == _PERSONAL_ID:
            _LOG.warning("ignoring reserved personal pack id in visible root: %s", _EXTERNAL_ROOT / _name)
        elif _name in _CORE_PACK_NAMES:
            _LOG.warning("core pack %r wins folder-pack id collision at %s", _name, _EXTERNAL_ROOT / _name)
    if _EXTERNAL_ROOT not in (Path(item).resolve() for item in __path__):
        __path__.append(str(_EXTERNAL_ROOT))
