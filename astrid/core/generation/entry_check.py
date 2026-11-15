"""Declaration-only admission for a pod's selected local generation entry."""
from __future__ import annotations

from typing import Any


def local_entry_error(rows: Any, entry: str | None, mode: str | None) -> str | None:
    """Preserve the pod guard's refusals; readiness belongs to managed alignment."""
    if not isinstance(rows, list) or any(not isinstance(row, dict) for row in rows):
        return 'the pod runtime did not list entries as JSON'
    if entry is None:
        return None
    rows = [row for row in rows if row.get('id') == entry]
    if not rows:
        return f"entry {entry!r} is not listed by the pod's runtime"
    rows = [row for row in rows if row.get('backend') == 'local'
            and (mode is None or row.get('mode') == mode)]
    if not rows:
        return f"entry {entry!r} has no local declaration for the requested mode"
    if any(not isinstance(row.get('mode'), str) or not row['mode']
           or type(row.get('ready')) is not bool for row in rows):
        return 'the pod runtime listed an invalid entry declaration'
    if mode is None and len({row['mode'] for row in rows}) != 1:
        return 'the pod entry requires an explicit mode'
    if any(row.get('availability') != 'available' for row in rows):
        return f"entry {entry!r} is unavailable on the pod's runtime"
    return None


def selected_entry_error(entry: str, mode: str | None) -> str | None:
    if entry == 'workflow':
        return None
    from .listing import catalog_rows

    # No targets, inventory, readiness probes or full catalogue projection.
    rows = [{**row, 'ready': False} for row in catalog_rows(entry_id=entry)]
    return local_entry_error(rows, entry, mode)
