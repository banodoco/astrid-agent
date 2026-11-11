"""Declared model rows for ``--align trust`` on an attached ComfyUI.

The host never carries a machine's model inventory to a child (Plan 32 T5:
a rented machine aligns its own disk). On an attached ComfyUI the host sets
``ASTRID_ALIGN=trust`` only when the task asked to trust; the child then uses
the workflow's own declared rows and marks the result unverified.
"""
from __future__ import annotations

from collections.abc import Mapping
from contextvars import ContextVar
from typing import Any

#: The child environment flag the host sets for ``--align trust`` (value ``trust``).
TRUST_ENV = "ASTRID_ALIGN"

# A pod job runs in-process, rather than in a host-launched action child. Keep
# its validated alignment choice local to that execution context.
POD_ALIGN = ContextVar("astrid_pod_align", default=None)
POD_STOP = ContextVar("astrid_pod_stop", default=None)


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


def trust_declared_loader_choices(observed: dict[str, Any], workflow: Any) -> dict[str, Any]:
    """Overlay exact declared loader names for explicit trust, without changing live facts.

    Callers retain node, socket, type and ordinary widget validation. A declaration
    can extend an existing model menu, never create a missing node or socket.
    """
    from copy import deepcopy

    from vibecomfy.model_assets import _referenced_model_values

    trusted = deepcopy(observed)
    for reference in _referenced_model_values(workflow):
        row = trusted.get(reference['class_type'])
        groups = row.get('input') if isinstance(row, dict) else None
        if not isinstance(groups, dict):
            continue
        for section in ('required', 'optional'):
            fields = groups.get(section)
            spec = fields.get(reference['field']) if isinstance(fields, dict) else None
            if not isinstance(spec, (list, tuple)) or not spec or not isinstance(spec[0], (list, tuple)):
                continue
            choices = list(spec[0])
            if reference['value'] not in choices:
                choices.append(reference['value'])
            fields[reference['field']] = [choices, *spec[1:]]
    return trusted
