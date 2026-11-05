"""Shared first-owner policy for layered pack discovery."""
from __future__ import annotations

from pathlib import Path
from typing import Any, Iterable

from astrid.core.pack._common import PackValidationError


def _same_pack_root(left: Any, right: Any) -> bool:
    left_root, right_root = getattr(left, "root", None), getattr(right, "root", None)
    if left_root is None or right_root is None:
        return False
    try:
        return Path(left_root).resolve() == Path(right_root).resolve()
    except OSError:
        return False


def first_pack_owner(items: Iterable[Any]) -> list[Any]:
    """Apply the shared core, personal, and duplicate-id admission policy.

    Core/source and canonical personal roots have priority over later copies.
    Duplicate non-core ids are rejected. Ownership follows source authority,
    never the presence of a user-writable PIN file.
    """
    winners: list[Any] = []
    seen: dict[str, tuple[Any, str]] = {}
    for item in items:
        pack = getattr(item, "pack", item)
        pack_id = getattr(pack, "id", None)
        if not isinstance(pack_id, str):
            continue
        source_kind = getattr(item, "source_kind", None) or "external"
        previous = seen.get(pack_id)
        if previous is not None and _same_pack_root(getattr(previous[0], "pack", previous[0]), pack):
            # One physical pack reached through two configured roots is one owner.
            continue
        if previous is not None:
            _, previous_kind = previous
            if ((previous_kind in {"source", "core"} or source_kind in {"source", "core"})
                    and previous_kind != source_kind):
                raise PackValidationError(
                    f"external pack id {pack_id!r} conflicts with a core pack"
                )
            if pack_id == "personal" and {previous_kind, source_kind} != {"personal"}:
                raise PackValidationError("the personal pack id is reserved for the canonical personal root")
            if previous_kind in {"source", "core", "personal", "local"}:
                if source_kind == previous_kind:
                    raise PackValidationError(f"duplicate pack id {pack_id!r}")
                continue
            if source_kind in {"source", "core", "personal", "local"}:
                winners = [winner for winner in winners
                           if getattr(getattr(winner, "pack", winner), "id", None) != pack_id]
            else:
                raise PackValidationError(f"duplicate pack id {pack_id!r}")
        elif pack_id == "personal" and source_kind != "personal":
            raise PackValidationError("the personal pack id is reserved for the canonical personal root")
        seen[pack_id] = (item, source_kind)
        winners.append(item)
    return winners


__all__ = ["first_pack_owner"]
