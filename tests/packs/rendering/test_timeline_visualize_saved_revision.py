"""Visualizer checks on saved-revision configs (no video render).

* A video (mpeg4) clip keeps its ``from`` trim key through materialization, so the
  retained capture config and the materialized timeline file must compare equal.
* An occurrence-only timeline (blank shots placed in the parent) must review its
  placed extent, not the zero-clip extent.
"""
from __future__ import annotations

import copy
import json
from pathlib import Path

from astrid.core.timeline import Timeline
from astrid.core.timeline.shot_composition_projection import project_runtime_parent_composition
from astrid.packs.rendering.actions.timeline_visualize.composed_frame import (
    materialized_timeline_matches,
    visualized_duration_frames,
)

FPS = 24
OUTPUT = {"file": "output.mp4", "fps": FPS, "resolution": "640x360", "background": None, "background_scale": None}
TRACKS = [{"id": "V1", "kind": "visual", "label": "Visual 1", "role": "output"}]


def _video_config():
    return {
        "app": {},
        "clips": [{"id": "footage", "asset": "media-clip", "at": 2.0, "clipType": "media", "hold": 4.0,
                   "from": 0, "to": 4.0, "speed": 1, "track": "V1"}],
        "output": copy.deepcopy(OUTPUT),
        "tracks": copy.deepcopy(TRACKS),
    }


def test_materialized_video_timeline_matches_its_retained_capture_config(tmp_path: Path):
    config = _video_config()
    path = tmp_path / "timeline.json"
    Timeline.from_config(config).dump(path)

    assert json.loads(path.read_text())["clips"][0]["from"] == 0
    assert materialized_timeline_matches(path, config)


def _occurrence_only_parent(count=20, seconds=2):
    internal = {"revision_id": "int-blank", "payload": {
        "tracks": copy.deepcopy(TRACKS), "clips": [], "registry": {"assets": {}}, "output": copy.deepcopy(OUTPUT)}}
    shots = [{"shot_id": f"shot-{i:02d}", "revision_id": "srev-1", "internal_timeline_revision_id": "int-blank"}
             for i in range(count)]
    occurrences = [{"occurrence_id": f"occ-{i:02d}", "shot_id": f"shot-{i:02d}", "shot_revision_id": "srev-1",
                    "track": "V1", "placement": {"start_ms": 100_000 + i * seconds * 1000},
                    "duration_ms": seconds * 1000} for i in range(count)]
    parent = {"project_id": "p", "timeline_id": "film", "revision_id": "par-1", "payload": {
        "config": {"tracks": copy.deepcopy(TRACKS), "clips": [], "output": copy.deepcopy(OUTPUT)},
        "registry": {"assets": {}}, "clips": [], "occurrences": occurrences}}
    return parent, shots, [internal]


def test_occurrence_only_saved_revision_is_reviewed_over_its_placed_extent():
    parent, shots, internals = _occurrence_only_parent()
    projected = project_runtime_parent_composition(parent, shot_revisions=shots, internal_timeline_revisions=internals)

    assert projected.config["clips"] == []
    last_end = max(row["at"] + row["hold"] for row in projected.occurrences)
    assert last_end == 140.0
    assert visualized_duration_frames(projected.config, FPS) >= round(last_end * FPS)
