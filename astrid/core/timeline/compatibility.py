"""The single Astrid timeline writer compatibility declaration.

``parent.config.app.astrid.v`` is an integer format version, not a list of
features. Missing declarations are legacy version 0; version 1 is the existing
namespaced intent format. Readers preserve unknown data, but writers must
understand both their candidate and the head they are replacing.
"""
from __future__ import annotations

from collections.abc import Mapping
from typing import Any

SUPPORTED_VERSIONS = frozenset({0, 1})


class TimelineCompatibilityError(ValueError):
    """A writer cannot safely replace this feature-bearing timeline."""


def require_writable_parent(parent: Any, *, source: str = "timeline") -> None:
    """Refuse malformed or unknown declarations without modifying opaque data."""
    config = parent.get("config") if isinstance(parent, Mapping) else None
    app = config.get("app") if isinstance(config, Mapping) else None
    ns = app.get("astrid") if isinstance(app, Mapping) else None
    if not isinstance(ns, Mapping) or "v" not in ns:
        return
    version = ns["v"]
    if type(version) is not int:
        raise TimelineCompatibilityError(
            f"{source}: app.astrid.v must be an integer; got {version!r}"
        )
    if version not in SUPPORTED_VERSIONS:
        raise TimelineCompatibilityError(
            f"{source}: unknown Astrid feature version app.astrid.v={version}; "
            f"this writer supports {sorted(SUPPORTED_VERSIONS)}; nothing was committed"
        )


def require_writable_candidate(candidate: Mapping[str, Any]) -> None:
    """Check the pinned head too: removing ``v`` must not bypass compatibility."""
    require_writable_parent(candidate.get("base_parent_payload"), source="pinned head")
    require_writable_parent(candidate.get("parent"), source="candidate")
