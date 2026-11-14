"""Managed model storage hashing observes cancellation between read chunks."""
from __future__ import annotations

import hashlib
import os
from concurrent.futures import CancelledError

import pytest

from tests.core.generation.test_managed_preparation import fetch_volume as fetch_volume  # noqa: F401


def test_storage_preflight_stops_reading_when_cancelled_mid_hash(fetch_volume, monkeypatch):
    c = fetch_volume
    payload = b'x' * (3 * 1024 * 1024)
    c.have.write_bytes(payload)
    c.rows[0].update(size=len(payload), sha256=hashlib.sha256(payload).hexdigest())
    identity = (c.have.stat().st_dev, c.have.stat().st_ino)
    reads = []
    cancelled = False
    original_read = os.read

    def cancel_after_first_chunk(fd, size):
        nonlocal cancelled
        block = original_read(fd, size)
        info = os.fstat(fd)
        if (info.st_dev, info.st_ino) == identity:
            reads.append(len(block))
            cancelled = True
        return block

    c.prep.cancelled = lambda: cancelled
    monkeypatch.setattr(os, 'read', cancel_after_first_chunk)
    with pytest.raises(CancelledError, match='managed preparation cancelled'):
        c.fetch()
    assert reads == [1024 * 1024]
    assert sum(reads) < len(payload)
    assert c.native == []
