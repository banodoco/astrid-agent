"""Small executable models of R0 owner lifetime and retained-worker boundaries.

These are test helpers, not an R0 runtime. See README.md for source and limits.
"""
from __future__ import annotations

import contextlib
import ctypes
import os
from pathlib import Path
import shutil
import subprocess
import sys


class OwnerBusy(RuntimeError):
    """A second owner could not obtain the exclusive, nonblocking lock."""


class OwnerLock:
    """Hold the R0-style file lock until close, independent of marker contents."""

    def __init__(self, path: Path):
        self.path = Path(path)
        self._file = None

    def acquire(self):
        if self._file is not None:
            raise RuntimeError("lock already held")
        if self.path.is_symlink():
            raise ValueError("owner lock must not be a symlink")
        f = open(self.path, "a+b")
        try:
            _lock(f.fileno())
        except (OSError, BlockingIOError) as exc:
            f.close()
            raise OwnerBusy(str(self.path)) from exc
        self._file = f
        return self

    def close(self):
        if self._file is not None:
            f, self._file = self._file, None
            try:
                _unlock(f.fileno())
            finally:
                f.close()

    def __enter__(self):
        return self.acquire()

    def __exit__(self, *_):
        self.close()


def _lock(fd):
    if sys.platform != "win32":
        import fcntl
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        return
    import msvcrt
    from ctypes import wintypes
    class OVERLAPPED(ctypes.Structure):
        _fields_ = [("Internal", ctypes.c_size_t), ("InternalHigh", ctypes.c_size_t),
                    ("Offset", wintypes.DWORD), ("OffsetHigh", wintypes.DWORD),
                    ("hEvent", wintypes.HANDLE)]
    overlap = OVERLAPPED()
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.LockFileEx.argtypes = [wintypes.HANDLE, wintypes.DWORD, wintypes.DWORD,
                                  wintypes.DWORD, wintypes.DWORD, ctypes.POINTER(OVERLAPPED)]
    kernel.LockFileEx.restype = wintypes.BOOL
    if not kernel.LockFileEx(msvcrt.get_osfhandle(fd), 3, 0, 1, 0, ctypes.byref(overlap)):
        raise OSError(ctypes.get_last_error(), "LockFileEx failed")


def _unlock(fd):
    if sys.platform != "win32":
        import fcntl
        fcntl.flock(fd, fcntl.LOCK_UN)
        return
    import msvcrt
    from ctypes import wintypes
    class OVERLAPPED(ctypes.Structure):
        _fields_ = [("Internal", ctypes.c_size_t), ("InternalHigh", ctypes.c_size_t),
                    ("Offset", wintypes.DWORD), ("OffsetHigh", wintypes.DWORD),
                    ("hEvent", wintypes.HANDLE)]
    overlap = OVERLAPPED()
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.UnlockFileEx.argtypes = [wintypes.HANDLE, wintypes.DWORD, wintypes.DWORD,
                                    wintypes.DWORD, ctypes.POINTER(OVERLAPPED)]
    kernel.UnlockFileEx.restype = wintypes.BOOL
    if not kernel.UnlockFileEx(msvcrt.get_osfhandle(fd), 0, 1, 0, ctypes.byref(overlap)):
        raise OSError(ctypes.get_last_error(), "UnlockFileEx failed")


def purge_after_unlock(root: Path, *, after_unlock=lambda: None):
    """Demonstrate R0's close-then-rmtree ordering; callback exposes the race.

    Caller sets up the root and models successful tombstone/confirmation checks.
    Never use on a real realm. The callback can acquire a new owner between close
    and removal. On Windows removal may fail while the new handle is open.
    """
    root = Path(root)
    with OwnerLock(root / "owner.lock"):
        pass
    after_unlock()
    shutil.rmtree(root)


def start_session_worker():
    """Spawn a disposable process in a new POSIX session, as R0 workers do.

    The caller owns cleanup. On Windows this uses a new process group only; it
    does not emulate R0's Job object or inherited handles.
    """
    kwargs = {"stdin": subprocess.DEVNULL, "stdout": subprocess.DEVNULL,
              "stderr": subprocess.DEVNULL}
    if os.name == "nt":
        kwargs["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP
    else:
        kwargs["start_new_session"] = True
    return subprocess.Popen([sys.executable, "-c", "import time; time.sleep(300)"], **kwargs)


@contextlib.contextmanager
def session_worker():
    worker = start_session_worker()
    try:
        yield worker
    finally:
        worker.terminate()
        try:
            worker.wait(timeout=5)
        except subprocess.TimeoutExpired:
            worker.kill()
            worker.wait(timeout=5)
