"""Validation of declared file ports at managed execution boundaries."""

from __future__ import annotations

import re
from typing import Any, Mapping


def managed_file_digest(value: Any, name: str) -> str:
    """Return a canonical digest; never interpret ordinary paths or nested JSON."""
    candidates = [value]
    if isinstance(value, Mapping):
        candidates = [value[key] for key in ("digest", "object_id") if value.get(key) is not None]
    digests = set()
    for candidate in candidates:
        if not isinstance(candidate, str) or not re.fullmatch(r"(?:sha256:)?[0-9a-f]{64}", candidate):
            break
        digests.add("sha256:" + candidate.removeprefix("sha256:"))
    else:
        if len(digests) == 1:
            return digests.pop()
    raise ValueError(
        f"file input {name!r} requires a managed Runtime object with a valid SHA-256 digest; "
        "caller-local paths are not available to executor workers. Import the file with "
        "client.media.import_file(project=..., path=..., idempotency_key=...) and pass "
        "its digest/object_id descriptor. Package declared dependencies inside JSON "
        "explicitly."
    )
