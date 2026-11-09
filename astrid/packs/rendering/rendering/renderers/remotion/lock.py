"""Cross-process ownership for Remotion's shared generated state.

The lock is deliberately non-recursive.  Callers that already own it must
route nested writer work through the explicit held-lock path instead of
acquiring it again.
"""

from __future__ import annotations

from astrid.pack_api import checkout_root

CHECKOUT_ROOT = checkout_root()

import os
import re
import secrets
import time
from contextlib import contextmanager
from contextvars import ContextVar
from pathlib import Path
from typing import Any, Iterator

from filelock import Timeout
from runtime_protocol import platform


REMOTION_LOCK_PATH = CHECKOUT_ROOT / "remotion" / ".astrid-registry.lock"
REMOTION_LOCK_TOKEN_ENV = "ASTRID_REMOTION_LOCK_TOKEN"
REMOTION_LOCK_PATH_ENV = "ASTRID_REMOTION_LOCK_PATH"
REMOTION_LOCK_RECEIPT_PREFIX = "ASTRID_REMOTION_LOCK_RECEIPT:"

_LOCAL_LOCK_PATH: ContextVar[str | None] = ContextVar(
    "remotion_render_lock_path",
    default=None,
)


class _FcntlLock:
    """Small ``filelock``-compatible fallback for the Remotion render lock."""

    def __init__(self, path: Path) -> None:
        self.path = Path(path)
        self._handle: Any | None = None

    def acquire(self, timeout: float | None = None) -> _FcntlLock:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._handle = self.path.open("a+b")
        deadline = None if timeout is None or timeout < 0 else time.monotonic() + timeout
        while True:
            try:
                platform.lock_file(self._handle.fileno(), blocking=False)
                return self
            except BlockingIOError as exc:
                if timeout == 0 or (deadline is not None and time.monotonic() >= deadline):
                    self._handle.close()
                    self._handle = None
                    raise Timeout(str(self.path)) from exc
                time.sleep(0.05)

    def release(self) -> None:
        if self._handle is None:
            return
        platform.unlock_file(self._handle.fileno())
        self._handle.close()
        self._handle = None


def _lock_for(path: Path) -> Any:
    """Keep a stable lock inode; Runtime supplies the native locking API."""
    return _FcntlLock(path)


def _resolved_lock_path() -> Path:
    return Path(REMOTION_LOCK_PATH).resolve(strict=False)


def _inherited_lock_path() -> str | None:
    """Return the canonical inherited lock path for a child with its launch token."""

    token = os.environ.get(REMOTION_LOCK_TOKEN_ENV, "")
    raw_path = os.environ.get(REMOTION_LOCK_PATH_ENV)
    if not re.fullmatch(r"[0-9a-f]{64}", token) or not raw_path:
        return None
    inherited_path = str(Path(raw_path).resolve(strict=False))
    if inherited_path != str(_resolved_lock_path()):
        return None
    return inherited_path


def remotion_render_lock_held() -> bool:
    """Whether this context, or its direct parent process, owns the lock."""

    return _LOCAL_LOCK_PATH.get() is not None or _inherited_lock_path() is not None


def remotion_render_lock_child_env() -> dict[str, str]:
    """Return a fresh launch token and path for one child of the held lock."""

    lock_path = _LOCAL_LOCK_PATH.get() or _inherited_lock_path()
    if lock_path is None:
        raise RuntimeError("cannot delegate Remotion writer work without owning the lock")
    return {
        REMOTION_LOCK_TOKEN_ENV: secrets.token_hex(32),
        REMOTION_LOCK_PATH_ENV: lock_path,
    }


@contextmanager
def remotion_render_lock() -> Iterator[None]:
    """Hold the one non-recursive Remotion registry/render file lock."""

    if remotion_render_lock_held():
        raise RuntimeError("Remotion render lock is non-recursive")

    lock_path = _resolved_lock_path()
    lock = _lock_for(lock_path)
    lock.acquire()
    token = _LOCAL_LOCK_PATH.set(str(lock_path))
    try:
        yield
    finally:
        try:
            lock.release()
        finally:
            _LOCAL_LOCK_PATH.reset(token)


__all__ = [
    "REMOTION_LOCK_PATH_ENV",
    "REMOTION_LOCK_PATH",
    "REMOTION_LOCK_RECEIPT_PREFIX",
    "REMOTION_LOCK_TOKEN_ENV",
    "remotion_render_lock",
    "remotion_render_lock_child_env",
    "remotion_render_lock_held",
]
