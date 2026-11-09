"""The built-in non-video default slots, read from the pack data that replaced the old table."""
from __future__ import annotations

from astrid.core.generation import defaults


def default_entries() -> dict[tuple[str, str], tuple[str, ...]]:
    """``{(kind, mode): entry ids by priority}`` for the image and audio defaults."""
    slots: dict[tuple[str, str], list] = {}
    for candidate in defaults.pack_candidates():
        if candidate.kind != "video":
            slots.setdefault((candidate.kind, candidate.mode), []).append(candidate)
    return {slot: tuple(c.entry for c in sorted(rows, key=lambda c: -c.priority)) for slot, rows in slots.items()}
