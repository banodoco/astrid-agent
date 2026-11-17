"""Where one attempt's VibeComfy runtime folder lives.

VibeComfy writes ``out/runs/<run>/transactions/<64-hex digest>/...`` under the
``runtime_root`` Astrid hands it. Inside a Windows attempt root that is deeper
than the 248/260-character limit (long paths are off by default), so on Windows
only the folder moves to a short per-attempt directory under the Runtime data
root. Every writer, reader and cleaner derives it from ``destination`` here, so
they cannot disagree. Elsewhere the location is unchanged.
"""

from __future__ import annotations

import hashlib
import shutil
import stat
import sys
from pathlib import Path, PurePath
from typing import overload

RUNTIME_DIR_NAME = ".vibecomfy-runtime"
_WINDOWS_PARENT = "vr"


@overload
def vibecomfy_runtime_root(destination: Path | str, *, platform_name: str | None = None) -> Path: ...
@overload
def vibecomfy_runtime_root(destination: PurePath, *, platform_name: str | None = None) -> PurePath: ...
def vibecomfy_runtime_root(destination: PurePath | str, *, platform_name: str | None = None) -> PurePath:
    """``destination/.vibecomfy-runtime``; on Windows ``<data root>/vr/<16 hex of the attempt name>``."""
    if (sys.platform if platform_name is None else platform_name) != "win32":
        return Path(destination) / RUNTIME_DIR_NAME
    path = Path(destination) if isinstance(destination, str) else destination
    if isinstance(path, Path):
        path = path.resolve()
    attempt = path.parent
    # <data root>/attempts/<base>/<attempt>/outputs puts the data root above "attempts".
    anchor = next((parent.parent for parent in attempt.parents if parent.name == "attempts"), attempt.parent)
    return anchor / _WINDOWS_PARENT / hashlib.sha256(attempt.name.encode("utf-8")).hexdigest()[:16]


def remove_windows_runtime_root(destination: Path) -> None:
    """Delete the short Windows runtime folder of one attempt; nothing to do elsewhere."""
    if sys.platform != "win32":
        return
    root = Path(vibecomfy_runtime_root(destination))
    try:
        # A replaced short-root parent must not redirect recursive deletion.
        # Junctions are reparse points on Windows, not necessarily symlinks.
        for path in (root.parent, root):
            metadata = path.lstat()
            if (stat.S_ISLNK(metadata.st_mode)
                    or getattr(metadata, "st_file_attributes", 0) & 0x400):
                raise OSError(f"refusing linked VibeComfy runtime folder: {path}")
        shutil.rmtree(root)
    except FileNotFoundError:
        pass
    try:
        root.lstat()
    except FileNotFoundError:
        return
    raise OSError(f"VibeComfy runtime cleanup was not verified: {root}")


__all__ = ["RUNTIME_DIR_NAME", "remove_windows_runtime_root", "vibecomfy_runtime_root"]
