"""Shared first-owner policy for layered pack discovery."""
from __future__ import annotations

from typing import Any, Iterable


def first_pack_owner(items: Iterable[Any]) -> list[Any]:
    """Keep the first candidate for each id in established source priority order.

    Callers supply candidates in inventory order (core, project-local, managed,
    user extras, then environment roots). Ownership is based on that order, never
    on the presence of a user-writable PIN file.
    """
    winners: list[Any] = []
    seen: set[str] = set()
    for item in items:
        pack = getattr(item, "pack", item)
        pack_id = getattr(pack, "id", None)
        if not isinstance(pack_id, str) or pack_id in seen:
            continue
        seen.add(pack_id)
        winners.append(item)
    return winners


__all__ = ["first_pack_owner"]
