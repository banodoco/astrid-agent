"""Tracks created by the SDK carry the label the timeline schema requires.

The visualizer validates a saved revision's tracks against the timeline schema
(``'label' is a required property`` otherwise), so a track created with
``t.track(...)`` must be complete before it is saved.
"""
from __future__ import annotations

import copy

from astrid.core.timeline import Timeline
from astrid.sdk.timeline.lookup import _LookupMixin

OUTPUT = {"file": "output.mp4", "fps": 24, "resolution": "640x360", "background": None, "background_scale": None}


class _Doc(_LookupMixin):
    """An in-memory parent document: no runtime, so track rows are appended directly."""

    def __init__(self, tracks):
        self.bundle = {"parent": {"config": {"tracks": copy.deepcopy(list(tracks)), "clips": [], "output": copy.deepcopy(OUTPUT)}}}

    def saved_revision_config(self):
        return copy.deepcopy(self.bundle["parent"]["config"])


def test_sdk_created_tracks_carry_label_and_save_as_a_valid_revision():
    doc = _Doc([{"id": "V1", "kind": "visual", "label": "Visual 1", "role": "output"}])

    doc.track("Titles", kind="visual")
    doc.track("Shots", kind="visual", above="V1")

    rows = {row["id"]: row for row in doc.saved_revision_config()["tracks"]}
    assert rows["Titles"]["label"] == "Titles"
    assert rows["Shots"]["label"] == "Shots"
    # The saved revision must load through the same schema gate the visualizer uses.
    Timeline.from_config(doc.saved_revision_config())
