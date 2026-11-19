"""Scope a whole-candidate rule to what a change introduces or touches.

Several validation rules judge the complete candidate. A long-lived timeline can
already fail one of them on an entity that a later change never touches, and that
timeline must stay editable and duplicable. Each such rule reports its findings
as ``{entity key: signature}``: the key names the entity, the signature is
everything the finding depends on (so any edit to the entity or to its inputs
changes it). A finding is *pre-existing* when the baseline has the same key with
the same signature; every other finding is *introduced* and still blocks.
"""
from __future__ import annotations

from collections.abc import Hashable, Mapping
from typing import Any, TypeVar

K = TypeVar("K", bound=Hashable)

PREEXISTING = "existing, not blocking: "


def split_findings(
    found: Mapping[K, Any], baseline: Mapping[K, Any] | None,
) -> tuple[list[K], list[K]]:
    """Return ``(introduced, preexisting)`` keys of ``found``, in order."""
    introduced: list[K] = []
    preexisting: list[K] = []
    for key, signature in found.items():
        if baseline is not None and key in baseline and baseline[key] == signature:
            preexisting.append(key)
        else:
            introduced.append(key)
    return introduced, preexisting


def warning(message: str) -> str:
    return PREEXISTING + message
