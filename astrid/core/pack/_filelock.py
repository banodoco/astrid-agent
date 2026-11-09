"""Portable advisory file locking for pack custody (fcntl on POSIX, msvcrt on Windows)."""

from __future__ import annotations

import os
import sys
import time

try:  # POSIX
    import fcntl as _fcntl
except ImportError:  # pragma: no cover - exercised via monkeypatch in tests
    _fcntl = None

try:  # Windows
    import msvcrt as _msvcrt
except ImportError:
    _msvcrt = None


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
