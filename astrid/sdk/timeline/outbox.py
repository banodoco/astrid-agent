"""Atomic storage for unsent commands and one exact in-flight request."""
from __future__ import annotations

import json
import os
import tempfile
from contextlib import contextmanager
from pathlib import Path

from runtime_protocol import platform


def request_bytes(value):
    # Match the generated commit_timeline serializer, including insertion order.
    return json.dumps(value, separators=(",", ":"), allow_nan=False).encode()


class Outbox:
    def __init__(self, path: Path):
        self.path = Path(path)

    @contextmanager
    def locked(self):
        missing, directory = [], self.path.parent
        while not directory.exists():
            missing.append(directory)
            directory = directory.parent
        self.path.parent.mkdir(parents=True, exist_ok=True)
        for directory in reversed(missing):
            descriptor = os.open(directory.parent, os.O_RDONLY | os.O_DIRECTORY)
            try:
                os.fsync(descriptor)
            finally:
                os.close(descriptor)
        with self.path.with_suffix(self.path.suffix + ".lock").open("a") as lock:
            platform.lock_file(lock.fileno())
            try:
                yield
            finally:
                platform.unlock_file(lock.fileno())

    def read(self):
        return json.loads(self.path.read_bytes()) if self.path.exists() else None

    def write(self, state):
        descriptor, name = tempfile.mkstemp(prefix=".outbox-", dir=self.path.parent)
        try:
            with os.fdopen(descriptor, "wb") as stream:
                stream.write(request_bytes(state))
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(name, self.path)
            directory = os.open(self.path.parent, os.O_RDONLY | os.O_DIRECTORY)
            try:
                os.fsync(directory)
            finally:
                os.close(directory)
        finally:
            if os.path.exists(name):
                os.unlink(name)
