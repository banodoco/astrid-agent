"""Lightweight command lifetime for the lazily loaded pack inventory."""
import sys
from contextlib import contextmanager
from contextvars import ContextVar
from copy import deepcopy
from threading import Lock

# Command-local reuse; never retain an inventory across requests.
class _DiscoveryCache(dict):
    """A shared owner that can revoke caches held by copied contexts."""

    def __init__(self):
        super().__init__()
        self.active = True
        self._lock = Lock()
        self._roots = {}
        self._generation = 0

    def read(self, key):
        with self._lock:
            if self.active and key in self:
                return deepcopy(self[key])
            return _CACHE_MISS

    def publish(self, key, value):
        snapshot = deepcopy(value)
        with self._lock:
            if self.active:
                self[key] = snapshot

    def tree_fingerprint(self, root):
        from astrid.core.pack.discovery import _RootSnapshot, _tree_fingerprint

        # On Linux, ctime records content and metadata edits even when mtime is
        # restored. Directory ctime records membership changes. Checking the
        # previously enumerated paths avoids reopening and rewalking the tree.
        with self._lock:
            if not self.active or not sys.platform.startswith("linux"):
                return _tree_fingerprint(root)
            snapshot = self._roots.get(root)
            if snapshot is not None and snapshot.unchanged():
                return snapshot.identity
            try:
                snapshot = _RootSnapshot(root)
                self._generation += 1
                snapshot.identity = self._generation
                self._roots[root] = snapshot
                return snapshot.identity
            except OSError:
                self._roots.pop(root, None)
                return _tree_fingerprint(root)

    def revoke(self):
        with self._lock:
            self.active = False
            self._roots.clear()
            self.clear()


_CACHE_MISS = object()
_discovery_cache: ContextVar[_DiscoveryCache | None] = ContextVar("pack_discovery_cache", default=None)


def _active_discovery_cache() -> _DiscoveryCache | None:
    cache = _discovery_cache.get()
    return cache if cache is not None and cache.active else None


@contextmanager
def pack_discovery_scope(*, fresh=False):
    """Reuse admitted packs only while their complete discovery inputs match."""
    if not fresh and _active_discovery_cache() is not None:
        yield
        return
    cache = _DiscoveryCache()
    token = _discovery_cache.set(cache)
    try:
        yield
    finally:
        cache.revoke()
        _discovery_cache.reset(token)


# The console's dispatch into the gateway is one command, not a new public
# invocation. Consume the handoff once so gateway calls nested inside its
# implementation still receive their own command scope.
_gateway_handoff = ContextVar("pack_discovery_gateway_handoff", default=False)


@contextmanager
def gateway_handoff():
    token = _gateway_handoff.set(True)
    try:
        yield
    finally:
        _gateway_handoff.reset(token)


def consume_gateway_handoff():
    handoff = _gateway_handoff.get()
    if handoff:
        _gateway_handoff.set(False)
    return handoff
