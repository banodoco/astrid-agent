"""Test-only SHA-256 stream accounting; never enable in latency measurements.

Covers hashlib.sha256, hashlib.new and hashlib.file_digest (including a cached
constructor passed to file_digest). Matches completed streams by digest AND byte
count, not by the size/equality of individual update chunks. Native hashing and
constructor aliases invoked directly before installation remain unobserved.
This helper does not assert cross-process or native-flush coverage.
"""
from __future__ import annotations
import hashlib
import itertools
import threading
from dataclasses import dataclass
from typing import Mapping

@dataclass(frozen=True)
class HashObservation:
    completed_streams: int
    bytes_hashed: int
    coverage_gaps: tuple[str, ...]

class OutputHashObserver:
    def __init__(self, targets: Mapping[str, int]):
        self.targets = dict(targets)
        if any(len(d) != 64 or any(c not in "0123456789abcdef" for c in d)
               or isinstance(n, bool) or not isinstance(n, int) or n < 0
               for d, n in self.targets.items()):
            raise ValueError("targets require lowercase SHA-256 and nonnegative byte sizes")
        self._sha256, self._new = hashlib.sha256, hashlib.new
        self._file_digest = hashlib.file_digest
        self._empty = self._sha256().hexdigest()
        self._lock = threading.RLock()
        self._ids = itertools.count()
        self._hashers = []
        self._matched = {}
        self._gaps = set()

    def _token(self, size):
        return (next(self._ids), size)

    def _wrap(self, value, initial_size=0, tokens=(), known=True):
        if isinstance(value, _CountedHash):
            if value.observer is not self:
                raise ValueError("overlapping hash observers are unsupported")
            return value
        wrapped = _CountedHash(self, value, initial_size, tokens, known)
        self._hashers.append(wrapped)
        return wrapped

    def sha256(self, data=b"", *args, **kwargs):
        value = self._sha256(data, *args, **kwargs)
        size = memoryview(data).nbytes
        with self._lock:
            return self._wrap(value, size, (self._token(size),) if size else ())

    def new(self, name, data=b"", **kwargs):
        value = self._new(name, data, **kwargs)
        if value.name.lower().replace("-", "") != "sha256":
            return value
        size = memoryview(data).nbytes
        with self._lock:
            return self._wrap(value, size, (self._token(size),) if size else ())

    def file_digest(self, fileobj, digest, /, **kwargs):
        def factory():
            value = self.new(digest) if isinstance(digest, str) else digest()
            if isinstance(value, _CountedHash):
                return self._wrap(value)
            if getattr(value, "name", "").lower().replace("-", "") != "sha256":
                return value
            with self._lock:
                known = value.hexdigest() == self._empty
                if not known:
                    self._gaps.add("preseeded file_digest callable: initial bytes unknown")
                return self._wrap(value, known=known)
        return self._file_digest(fileobj, factory, **kwargs)

    def install(self, monkeypatch):
        monkeypatch.setattr(hashlib, "sha256", self.sha256)
        monkeypatch.setattr(hashlib, "new", self.new)
        monkeypatch.setattr(hashlib, "file_digest", self.file_digest)
        return self

    def _record(self, value):
        if not value.known:
            return
        digest = value.inner.hexdigest()
        if self.targets.get(digest) == value.size:
            # Copies share consumed-prefix tokens: repeated digest calls and
            # copying a finished hasher do not pretend bytes were hashed again.
            self._matched[(digest, value.tokens)] = value.tokens

    def snapshot(self):
        with self._lock:
            for value in self._hashers:
                self._record(value)
            consumed = {token for tokens in self._matched.values() for token in tokens}
            return HashObservation(len(self._matched), sum(size for _, size in consumed),
                                   tuple(sorted(self._gaps)))

class _CountedHash:
    def __init__(self, observer, inner, size, tokens, known):
        self.observer, self.inner, self.size = observer, inner, size
        self.tokens, self.known = tuple(tokens), known

    def update(self, block):
        with self.observer._lock:
            result = self.inner.update(block)
            size = memoryview(block).nbytes
            self.size += size
            if size:
                self.tokens += (self.observer._token(size),)
            return result

    def digest(self):
        with self.observer._lock:
            result = self.inner.digest()
            self.observer._record(self)
            return result

    def hexdigest(self):
        with self.observer._lock:
            result = self.inner.hexdigest()
            self.observer._record(self)
            return result

    def copy(self):
        with self.observer._lock:
            return self.observer._wrap(self.inner.copy(), self.size, self.tokens, self.known)

    def __getattr__(self, name):
        return getattr(self.inner, name)
