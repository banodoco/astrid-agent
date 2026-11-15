"""Short pytest parametrize ids for oversized values.

pytest builds a node id from the repr of any parameter that has no explicit id, so a
million-character value becomes a million-character id, and the failure report prints that
id as a header line. Pass ``ids=bounded_id`` to ``pytest.mark.parametrize``: values whose text
is at most ``LIMIT`` characters keep their default id (the callable returns ``None``), longer
ones get a short stable name made of the type, the length and a digest prefix.
"""
from __future__ import annotations

import hashlib

LIMIT = 200


def bounded_id(value):
    if isinstance(value, (str, bytes, bytearray)) and len(value) > LIMIT:
        data = value.encode("utf-8", "surrogatepass") if isinstance(value, str) else bytes(value)
        return f"{type(value).__name__}-{len(value)}-{hashlib.sha256(data).hexdigest()[:8]}"
    return None
