"""Declared model rows for ``--align trust`` on an attached ComfyUI.

The host never carries a machine's model inventory to a child (Plan 32 T5:
a rented machine aligns its own disk). On an attached ComfyUI the host sets
``ASTRID_ALIGN=trust`` only when the task asked to trust; the child then uses
the workflow's own declared rows and marks the result unverified.
"""
from __future__ import annotations

from collections.abc import Mapping
from typing import Any

#: The child environment flag the host sets for ``--align trust`` (value ``trust``).
TRUST_ENV = "ASTRID_ALIGN"


def declared_rows(workflow: Any) -> tuple[dict[str, Any], ...]:
    """The workflow's own declared model rows, for ``--align trust``."""
    rows = []
    for asset in (getattr(workflow, "metadata", None) or {}).get("model_assets", []) or []:
        if not isinstance(asset, Mapping):
            continue
        name = asset.get("name", asset.get("filename"))
        digest, size = asset.get("sha256"), asset.get("size_bytes")
        if isinstance(name, str) and name and isinstance(digest, str) and type(size) is int:
            rows.append({"name": name, "subdir": str(asset.get("subdir", asset.get("directory", "")) or ""),
                         "size": size, "sha256": digest.removeprefix("sha256:")})
    return tuple(rows)
