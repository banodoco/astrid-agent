"""Portable advisory file locking for pack custody (fcntl on POSIX, msvcrt on Windows)."""

from __future__ import annotations

import errno
import os
import sys
import time
from importlib import import_module
from pathlib import Path
from types import ModuleType

_fcntl: ModuleType | None
try:  # POSIX
    _fcntl = import_module("fcntl")
except ImportError:  # pragma: no cover - exercised via monkeypatch in tests
    _fcntl = None

_msvcrt: ModuleType | None
try:  # Windows
    _msvcrt = import_module("msvcrt")
except ImportError:
    _msvcrt = None


def nofollow_flag(path: str | os.PathLike[str]) -> int:
    """Return the open flag that refuses a symlink at ``path``.

    POSIX has O_NOFOLLOW. Where it is missing (Windows), os.open would follow
    a link silently, so the path is first checked with the installer's link
    test (symlinks, junctions and reparse points) and refused with the same
    ELOOP the POSIX flag raises.
    """
    flag = getattr(os, "O_NOFOLLOW", 0)
    if flag:
        return flag
    from astrid.installer.core import linked

    if linked(Path(path)):
        raise OSError(errno.ELOOP, os.strerror(errno.ELOOP), os.fspath(path))
    return 0


def locking_supported() -> bool:
    return _fcntl is not None or (sys.platform == "win32" and _msvcrt is not None)


def lock_fd(fd: int, *, exclusive: bool = True) -> None:
    """Block until an advisory lock is held on ``fd``.

    Windows has no shared byte-range lock in msvcrt, so shared requests take
    the exclusive lock (stricter, never weaker).
    """
    if _fcntl is not None:
        _fcntl.flock(fd, _fcntl.LOCK_EX if exclusive else _fcntl.LOCK_SH)
        return
    if _msvcrt is None:
        raise OSError("no file locking available on this platform")
    while True:
        os.lseek(fd, 0, os.SEEK_SET)
        try:
            _msvcrt.locking(fd, _msvcrt.LK_LOCK, 1)
            return
        except OSError:
            time.sleep(0.05)


def unlock_fd(fd: int) -> None:
    if _fcntl is not None:
        _fcntl.flock(fd, _fcntl.LOCK_UN)
        return
    if _msvcrt is None:
        raise OSError("no file locking available on this platform")
    os.lseek(fd, 0, os.SEEK_SET)
    _msvcrt.locking(fd, _msvcrt.LK_UNLCK, 1)
