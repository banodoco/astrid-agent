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
import sys
from pathlib import Path, PurePath

RUNTIME_DIR_NAME = ".vibecomfy-runtime"
_WINDOWS_PARENT = "vr"


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
    try:
        shutil.rmtree(vibecomfy_runtime_root(destination))
    except FileNotFoundError:
        pass


__all__ = ["RUNTIME_DIR_NAME", "remove_windows_runtime_root", "vibecomfy_runtime_root"]
