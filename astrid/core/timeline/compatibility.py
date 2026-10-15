"""The single Astrid timeline writer compatibility declaration.

``parent.config.app.astrid.v`` is an integer format version, not a list of
features. Missing declarations are legacy version 0; version 1 is the existing
namespaced intent format, and version 2 carries authored music sources.
Readers preserve unknown data, but writers must
understand both their candidate and the head they are replacing.
"""
from __future__ import annotations

from collections.abc import Mapping
from typing import Any

SUPPORTED_VERSIONS = frozenset({0, 1, 2})
# Explicit capabilities are independent of the integer storage format.
ADOPTED_BED_CAPABILITY = "adopted-music-bed"
WRITER_CAPABILITIES = frozenset()  # D enables this after authored-owner integration.


class TimelineCompatibilityError(ValueError):
    """A writer cannot safely replace this feature-bearing timeline."""


def require_writable_parent(parent: Any, *, source: str = "timeline", capabilities=None) -> None:
    """Refuse malformed or unknown declarations without modifying opaque data."""
    config = parent.get("config") if isinstance(parent, Mapping) else None
    app = config.get("app") if isinstance(config, Mapping) else None
    ns = app.get("astrid") if isinstance(app, Mapping) else None
    capabilities = WRITER_CAPABILITIES if capabilities is None else frozenset(capabilities)
    if isinstance(ns, Mapping) and "bed" in ns:
        if ns.get("v") != 2:
            raise TimelineCompatibilityError(f"{source}: bed requires app.astrid.v=2")
        if ADOPTED_BED_CAPABILITY not in capabilities:
            raise TimelineCompatibilityError(f"{source}: writer lacks {ADOPTED_BED_CAPABILITY} capability; use an arrangement-aware writer")
    if isinstance(ns, Mapping) and "music_beds" in ns and ns.get("v", 0) != 2:
        raise TimelineCompatibilityError(f"{source}: music_beds requires app.astrid.v=2")
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


def require_writable_candidate(candidate: Mapping[str, Any], *, capabilities=None) -> None:
    """Check the pinned head too: removing ``v`` must not bypass compatibility."""
    require_writable_parent(candidate.get("base_parent_payload"), source="pinned head", capabilities=capabilities)
    require_writable_parent(candidate.get("parent"), source="candidate", capabilities=capabilities)
    # A legacy payload does not declare a writer capable of preserving a newer
    # head. Refuse downgrade/removal as well as versions unknown to this writer.
    def version(parent: Any) -> int:
        config = parent.get("config") if isinstance(parent, Mapping) else None
        app = config.get("app") if isinstance(config, Mapping) else None
        ns = app.get("astrid") if isinstance(app, Mapping) else None
        return ns.get("v", 0) if isinstance(ns, Mapping) else 0

    base_version = version(candidate.get("base_parent_payload"))
    candidate_version = version(candidate.get("parent"))
    if candidate_version < base_version:
        raise TimelineCompatibilityError(
            f"candidate: app.astrid.v={candidate_version} is below pinned head "
            f"app.astrid.v={base_version}; nothing was committed"
        )
