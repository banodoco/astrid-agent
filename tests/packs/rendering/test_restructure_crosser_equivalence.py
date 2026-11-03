"""Renderer-input regressions for chapter crossers with non-resettable clocks."""
from __future__ import annotations

import copy
import json
import math
import os
import subprocess
from collections.abc import Mapping
from pathlib import Path

import pytest

import astrid.sdk.timeline_restructure as restructure
from astrid.core.rendering.node_pin import resolve_pinned_node
from astrid.core.timeline.cuts import clip_duration
from astrid.core.timeline.shot_composition_projection import project_runtime_parent_composition
from astrid.sdk.timeline_restructure import _cut_clip, differences, plan, split
from tests.sdk.test_timeline_restructure import flat_film

ROOT = Path(__file__).resolve().parents[3]
FPS = 30


def _js_round(value):
    """Match the pinned renderer's Math.round, including positive half frames."""
    return math.floor(value + 0.5)


def _project(bundle):
    """Pass a restructure bundle through the same projection used by managed rendering."""
    occurrences, shot_revisions, internals = [], [], []
    for row in bundle["placements"]:
        shot_id = str(row["shot_id"])
        occurrence = copy.deepcopy(row)
        occurrence["shot_revision_id"] = f"r-{shot_id}"
        occurrences.append(occurrence)
        shot_revisions.append({
            "shot_id": shot_id,
            "revision_id": f"r-{shot_id}",
            "internal_timeline_revision_id": f"i-{shot_id}",
            "payload": {},
        })
        internals.append({
            "revision_id": f"i-{shot_id}",
            "payload": copy.deepcopy(bundle["shots"][shot_id].get("internal_timeline") or {}),
        })
    payload = copy.deepcopy(bundle["parent"])
    payload["occurrences"] = occurrences
    return project_runtime_parent_composition(
        {
            "project_id": str(bundle.get("project_id")),
            "timeline_id": str(bundle.get("timeline_id")),
            "revision_id": "renderer-input-proof",
            "payload": payload,
        },
        shot_revisions=shot_revisions,
        internal_timeline_revisions=internals,
    )


def _source_clips(projected, source_id):
    result = []
    for clip in projected.config.get("clips", []):
        app = clip.get("app", {})
        astrid = app.get("astrid", {}) if isinstance(app, Mapping) else {}
        provenance = app.get("astrid_shot_composition", {}) if isinstance(app, Mapping) else {}
        marker = astrid.get("timeline_restructure_piece", {}) if isinstance(astrid, Mapping) else {}
        if (provenance.get("source_clip_id") == source_id or marker.get("id") == source_id
                or clip.get("id") == source_id):
            result.append(clip)
    return result


def _active_source_clip(projected, source_id, frame):
    active = [
        clip for clip in _source_clips(projected, source_id)
        if _js_round(float(clip.get("at", 0)) * FPS) <= frame
        < _js_round(float(clip.get("at", 0)) * FPS)
        + max(1, _js_round(clip_duration(clip) * FPS))
    ]
    assert len(active) == 1, (source_id, frame, active)
    return active[0]


def _node_renderer_samples(before, after, source_id):
    """Use the pinned composition package's rounded trim for every active output frame."""
    first = _source_clips(before, source_id)
    assert len(first) == 1
    start = _js_round(float(first[0].get("at", 0)) * FPS)
    end = start + max(1, _js_round(clip_duration(first[0]) * FPS))
    frames = list(range(start, end))
    cases = []
    for frame in frames:
        cases.append({"side": "before", "frame": frame, "clip": _active_source_clip(before, source_id, frame)})
        cases.append({"side": "after", "frame": frame, "clip": _active_source_clip(after, source_id, frame)})
    node = resolve_pinned_node(ROOT / "remotion", override=os.environ.get("ASTRID_NODE_EXECUTABLE"))
    assert node.ok, node.problem
    script = r"""
import {readFileSync} from 'node:fs';
import {computeMediaTrim} from '@banodoco/timeline-composition/evaluation';
const {cases, fps} = JSON.parse(readFileSync(0, 'utf8'));
const samples = cases.map(({side, frame, clip}) => {
  const trim = computeMediaTrim(clip, fps);
  const clipStart = Math.round(clip.at * fps);
  const sourceFrame = trim.trimBefore + Math.round((frame - clipStart) * (clip.speed ?? 1));
  const continuousSourcePhase = trim.trimBefore + (frame - clipStart) * (clip.speed ?? 1);
  return {side, frame, sourceFrame, continuousSourcePhase};
});
process.stdout.write(JSON.stringify(samples));
"""
    result = subprocess.run(
        [str(node.path), "--input-type=module", "-e", script],
        input=json.dumps({"cases": cases, "fps": FPS}),
        cwd=ROOT / "remotion",
        text=True,
        capture_output=True,
        check=False,
        timeout=30,
    )
    assert result.returncode == 0, result.stderr
    samples = json.loads(result.stdout)
    return frames, samples


def _crosser(extra):
    bundle = flat_film()
    source = {
        "id": "clock-crosser",
        "clipType": "media",
        "track": "sprite",
        "asset": "R",
        "at": 3.5,
        "hold": 1.0,
        **copy.deepcopy(extra),
    }
    bundle["shots"]["film"]["internal_timeline"]["clips"].append(source)
    return bundle, source


@pytest.mark.parametrize("phase", ["entrance", "exit"])
def test_animation_crossers_keep_the_original_renderer_clock(phase):
    flat, source = _crosser({phase: {"type": "fade", "duration": 0.5}})
    before = _project(flat)
    after = _project(split(flat))
    frames = list(range(_js_round(source["at"] * FPS),
                        _js_round((source["at"] + source["hold"]) * FPS)))

    actual_clocks = []
    for frame in frames:
        original = _active_source_clip(before, source["id"], frame)
        restructured = _active_source_clip(after, source["id"], frame)
        # This is the Sequence-local frame passed to ClipAnimationPhase by the renderer.
        original_clock = frame - _js_round(float(original["at"]) * FPS)
        split_clock = frame - _js_round(float(restructured["at"]) * FPS)
        actual_clocks.append((frame, original_clock, split_clock))

    original = _source_clips(before, source["id"])
    restructured = _source_clips(after, source["id"])
    assert frames
    assert len(original) == len(restructured) == 1
    assert original[0][phase] == restructured[0][phase]
    assert clip_duration(original[0]) == clip_duration(restructured[0])
    assert any(original_clock >= 0 and split_clock >= 0
               for _, original_clock, split_clock in actual_clocks)
    assert all(original == split_clock for _, original, split_clock in actual_clocks), actual_clocks


@pytest.mark.parametrize("trim,at", [((0.0, 1.0), 3.5), ((0.25, 1.75), 3.5), ((0.0, 1.0), 3.55)])
def test_fractional_media_trim_has_the_same_pinned_renderer_source_sample_each_frame(trim, at):
    flat, source = _crosser({"at": at, "from": trim[0], "to": trim[1], "speed": 1.5})
    before = _project(flat)
    after = _project(split(flat))
    frames, samples = _node_renderer_samples(before, after, source["id"])
    before_samples = [row["sourceFrame"] for row in samples if row["side"] == "before"]
    after_samples = [row["sourceFrame"] for row in samples if row["side"] == "after"]
    before_phases = [row["continuousSourcePhase"] for row in samples if row["side"] == "before"]
    after_phases = [row["continuousSourcePhase"] for row in samples if row["side"] == "after"]

    assert len(before_samples) == len(after_samples) == len(frames)
    assert before_samples == after_samples, list(zip(frames, before_samples, after_samples))
    assert len(before_phases) == len(after_phases) == len(frames)
    assert before_phases == after_phases, list(zip(frames, before_phases, after_phases))


def test_unsafe_clock_crossers_are_preserved_on_the_over_row_after_projection():
    for extra in (
        {"entrance": {"type": "fade", "duration": 0.5}},
        {"exit": {"type": "fade", "duration": 0.5}},
        {"from": 0.0, "to": 1.0, "speed": 1.5},
    ):
        flat, source = _crosser(extra)
        candidate = split(flat)
        projected = _project(candidate)
        clip = _source_clips(projected, source["id"])
        assert len(clip) == 1
        assert clip[0].get("at") == pytest.approx(source["at"])
        track = next(row for row in projected.config["tracks"] if row["id"] == clip[0]["track"])
        assert track["id"] == "picture-over"
        astrid = clip[0].get("app", {}).get("astrid", {})
        assert astrid.get("timeline_restructure_over") == {"track": "sprite", "row": "picture"}
        assert not astrid.get("timeline_restructure_piece")


def test_differences_rejects_a_broken_old_piece_group(monkeypatch):
    flat, source = _crosser({"from": 0.0, "to": 1.0, "speed": 1.5})
    with monkeypatch.context() as patcher:
        patcher.setattr(restructure, "_cut_safety_reason", lambda *args: None)
        old_split = split(flat)
    raw_cut = _cut_clip(source, 4.0, 4.5, plan(flat).chapters[1], piece_id="unsafe-second-piece")
    assert raw_cut["at"] == 0.0 and raw_cut["from"] == pytest.approx(0.75)

    pieces = [
        clip for shot in old_split["shots"].values()
        for clip in shot["internal_timeline"]["clips"]
        if clip.get("app", {}).get("astrid", {}).get("timeline_restructure_piece", {}).get("id") == source["id"]
    ]
    assert len(pieces) == 2
    # Removing one marker prevents the comparator from rejoining a malformed group.
    del pieces[1]["app"]["astrid"]["timeline_restructure_piece"]
    assert differences(flat, old_split)
