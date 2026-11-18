"""Markers written when Astrid authors a new timeline track."""
from __future__ import annotations

from collections.abc import MutableMapping
from typing import Any


SCALE_APPLIES_TO_POSITIONED_CLIPS = "scaleAppliesToPositionedClips"
CLIP_PLACEMENT_VERSION = "clipPlacementVersion"
CLIP_PLACEMENT_VERSION_CURRENT = 1


def label_track_with_id(track: MutableMapping[str, Any]) -> MutableMapping[str, Any]:
    """Give a track without a label its id as label.

    The timeline schema requires a label on every track and render refuses one
    without. This is the single rule for every path that introduces a track.
    """
    track_id = track.get("id")
    if not track.get("label") and isinstance(track_id, str) and track_id:
        track["label"] = track_id
    return track


def stamp_new_track_appearance(track: MutableMapping[str, Any]) -> MutableMapping[str, Any]:
    """Stamp placement migrations on a newly authored track, preserving its app bag.

    This is creation metadata only. Callers must not use it to normalize an
    existing imported or projected track.
    """
    track_id = track.get("id", "<new>")
    app = track.get("app")
    if app is None:
        app = {}
        track["app"] = app
    elif not isinstance(app, MutableMapping):
        raise ValueError(f"track {track_id!r} property app must be an object")

    if CLIP_PLACEMENT_VERSION in app:
        version = app[CLIP_PLACEMENT_VERSION]
        if (isinstance(version, bool) or not isinstance(version, int)
                or version != CLIP_PLACEMENT_VERSION_CURRENT):
            raise ValueError(
                f"track {track_id!r} property app.{CLIP_PLACEMENT_VERSION} "
                f"has unsupported value {version!r}; expected "
                f"{CLIP_PLACEMENT_VERSION_CURRENT}"
            )

    if SCALE_APPLIES_TO_POSITIONED_CLIPS in app:
        value = app[SCALE_APPLIES_TO_POSITIONED_CLIPS]
        if not isinstance(value, bool):
            raise ValueError(
                f"track {track_id!r} property app.{SCALE_APPLIES_TO_POSITIONED_CLIPS} "
                "must be a boolean"
            )

    # New tracks already use the current whole-track scale semantics, so they
    # must not be inverse-scaled as if their positioned clips were legacy.
    app[SCALE_APPLIES_TO_POSITIONED_CLIPS] = True
    app[CLIP_PLACEMENT_VERSION] = CLIP_PLACEMENT_VERSION_CURRENT
    return track
