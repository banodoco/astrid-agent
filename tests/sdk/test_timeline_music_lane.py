"""Shared read-only adopted-bed projection for sheet and visualize consumers."""
from __future__ import annotations

import copy
import json
from types import SimpleNamespace

import pytest

from astrid.core.timeline.compatibility import ADOPTED_BED_CAPABILITY, TimelineCompatibilityError
from astrid.packs.rendering.actions.timeline_visualize.layers.base import LayerContext
from astrid.packs.rendering.actions.timeline_visualize.layers.builtin import render_sync
from astrid.packs.timeline import cli_show
from astrid.sdk.timeline.bed_adoption import adopt_music_bed
from astrid.sdk.timeline.checkout import Checkout
from astrid.sdk.timeline.music_beds import collection
from astrid.sdk.timeline.music_beds import project as materialize_beds
from astrid.sdk.timeline.music_lane import project, project_snapshot
from astrid.sdk.timeline_filmstrip import build_filmstrip_snapshot
from astrid.sdk.timeline_sheet import render_sheet
from tests.sdk.test_music_beds import tiny
from tests.sdk.test_timeline_adopt import OWNER


def _adopted_lane_fixture():
    tl = tiny()
    source = copy.deepcopy(tl.clip("a-music").data)
    source.update(id="arrangement-two", at=0.0)
    tl._internal("A")["clips"].append(source)
    owners = [OWNER, "occurrence:occ-A/clip:arrangement-two"]
    words = next(c for c in tl._internal("A")["clips"] if c["id"] == "a-vo")["app"]["astrid"]["words"]
    words.append([5.9, 6.1, "SwitchWord"])
    basis = [dict(at_s=2.0, delta_s=1.0)]
    for owner in owners:
        source_id = owner.split("/clip:", 1)[1]
        raw = copy.deepcopy(next(c for c in tl._internal("A")["clips"] if c["id"] == source_id))
        collection(tl, create=True)[owner] = {
            "owner": owner, "source": raw, "ripple_basis": copy.deepcopy(basis), "outputs": [source_id],
        }
    materialize_beds(tl)
    switch = {"id": "switch-one", "on": '"SwitchWord" in s1 #1', "duration": 2.0, "curve": "equal-power"}
    evidence = {owner: {"confidence": 1.0, "source_offset_s": 0.0} for owner in owners}
    return Checkout(adopt_music_bed(tl.document(), owners, switches=[switch], alignment=evidence)), owners


def _render_snapshot(tl):
    config = copy.deepcopy(tl.bundle["parent"]["config"])
    config["tracks"] = copy.deepcopy(tl._internal("A")["tracks"])
    config.setdefault("theme_overrides", {}).setdefault("visual", {}).setdefault("canvas", {})["fps"] = tl.fps
    clips = copy.deepcopy(config.get("clips") or [])
    occurrences = []
    assets = {}
    for placement in tl.bundle["placements"]:
        oid, sid = placement["occurrence_id"], placement["shot_id"]
        at_ms = placement.get("at_ms", placement.get("placement", {}).get("start_ms"))
        occurrences.append({
            "occurrence_id": oid, "shot_id": sid, "name": "s1",
            "at_ms": at_ms,
            "duration_ms": placement["duration_ms"],
        })
        for key, value in tl._registry(sid).items():
            assets[f"{oid}:{key}"] = copy.deepcopy(value)
        for raw in tl._internal(sid)["clips"]:
            clip = copy.deepcopy(raw)
            original_id = clip["id"]
            clip["id"] = f"{oid}:{original_id}"
            clip["at"] = round(float(clip.get("at") or 0) + at_ms / 1000, 6)
            if isinstance(clip.get("asset"), str):
                clip["asset"] = f"{oid}:{clip['asset']}"
            app = clip.setdefault("app", {})
            app["astrid_shot_composition"] = {
                "occurrence_id": oid, "source_clip_id": original_id,
            }
            clip.update(shot_id=sid, shot_occurrence_id=oid)
            clips.append(clip)
    config["clips"] = clips
    config.setdefault("app", {}).setdefault("astrid_shot_composition", {})["occurrences"] = occurrences
    return {
        "config": config, "registry": {"assets": assets}, "fps": tl.fps,
        "project_id": tl.bundle.get("project_id"), "timeline_id": tl.bundle.get("timeline_id"),
    }


def test_sheet_visualize_and_snapshot_share_the_same_uncapped_lane(monkeypatch, capsys):
    tl, owners = _adopted_lane_fixture()
    before = copy.deepcopy(tl.bundle)
    lane = project(tl)
    assert tl.bundle == before
    assert lane["schema"] == "astrid.audio-lane.v1"
    assert lane["capability"] == ADOPTED_BED_CAPABILITY
    assert [row["owner"] for row in lane["arrangements"]] == owners
    assert lane["switches"][0]["on"] == {
        "time_ref": '"SwitchWord" in s1 #1', "center_s": 6.4, "center_frame": 192,
    }
    assert lane["switches"][0]["window"]["start_s"] == 5.4
    assert lane["switches"][0]["curve"] == "equal-power"
    assert lane["seams"] == [{
        "at_s": 2.0, "frame": 60, "action": "repeat", "delta_s": 1.0, "duration_s": 1.0,
    }]
    ids = [[span["id"] for span in row["spans"]] for row in lane["arrangements"]]
    assert all(span["read_only"] and span["generated"] for row in lane["arrangements"] for span in row["spans"])
    assert all(span["edit_source"]["operation"]["select"] == row["owner"]
               for row in lane["arrangements"] for span in row["spans"])
    assert lane["arrangements"][0]["spans"][0]["source"]["start_s"] == 0.0
    assert lane["arrangements"][0]["gain"]["linear"] == pytest.approx(1.0)

    sheet = render_sheet(tl, audio_lane=lane)
    for row_ids in ids:
        for generated_id in row_ids:
            assert f"~{generated_id}" in sheet
    for arrangement in lane["arrangements"]:
        for span in arrangement["spans"]:
            source = span["source"]
            assert f"source {source['start_s']:.2f}–{source['end_s']:.2f}s" in sheet
            gain = arrangement["gain"]
            db = "mute" if gain["db"] is None else f"{gain['db']:+.2f} dB"
            assert f"gain {gain['linear']:g} ({db})" in sheet
    assert 'on "SwitchWord" in s1 #1' in sheet
    assert "overlap 5.40–7.40s" in sheet
    assert "seam 2.00s/f60 · repeat 1.00s" in sheet

    context = LayerContext(
        cut={"start": 0.0, "end": 8.0, "index": 1}, cuts=[], window=(0.0, 8.0), fps=tl.fps,
        elements=[], all_elements=[], words=[], beats={}, sfx=[], events=[], frames={},
    )
    context.shared["audio_lane"] = lane
    visual = render_sync(context)
    joined = "\n".join(visual.findings)
    for row_ids in ids:
        for generated_id in row_ids:
            assert f"~{generated_id}" in joined
    for arrangement in lane["arrangements"]:
        gain = arrangement["gain"]
        db = "mute" if gain["db"] is None else f"{gain['db']:+.1f}dB"
        for span in arrangement["spans"]:
            source = span["source"]
            assert (f"source {source['start_s']}–{source['end_s']}s gain "
                    f"{gain['linear']} ({db})") in joined
    assert 'on "SwitchWord" in s1 #1' in joined
    assert "overlap 5.400–7.400s (equal-power)" in joined
    assert "seam 2.000s/f60 repeat 1.0s" in joined

    snapshot_lane = project_snapshot(_render_snapshot(tl))
    assert snapshot_lane == lane

    monkeypatch.setattr(cli_show, "show_checkout", lambda _parsed, _opener: (tl, "TEST"))
    args = SimpleNamespace(
        project="project", ref="timeline", range=None, at=None, detail=False, json=True,
        published=False, revision_id=None,
    )
    assert cli_show.print_sheet(args, None) == 0
    machine = json.loads(capsys.readouterr().out)
    assert machine["data"]["presentation"]["audio_lane"] == lane
    assert len(machine["data"]["presentation"]["audio_lane"]["arrangements"]) == 2

    with pytest.raises(TimelineCompatibilityError, match="lacks adopted-music-bed"):
        project(tl, capabilities=())


def test_filmstrip_builder_projects_the_same_adopted_audio_lane():
    tl, _owners = _adopted_lane_fixture()
    timeline_snapshot = _render_snapshot(tl)
    original_snapshot = copy.deepcopy(timeline_snapshot)
    expected = project(tl)
    envelope = {
        "authority_context": {
            "timeline_id": timeline_snapshot["timeline_id"],
            "timeline_slug": "fixture",
            "expansion": {"occurrences": [], "shots": []},
        },
        "inputs": {
            "profile": {"fps_rational": [30, 1]},
            "timeline_snapshot": timeline_snapshot,
        },
    }

    result = build_filmstrip_snapshot(
        envelope,
        client=None,
        project=timeline_snapshot["project_id"],
        run_id="fixture-run",
        video_digest="sha256:" + "d" * 64,
    )

    lane = result["audio_lane"]
    assert lane == expected
    assert [[span["id"] for span in row["spans"]] for row in lane["arrangements"]] == [
        [span["id"] for span in row["spans"]] for row in expected["arrangements"]
    ]
    assert lane["switches"][0]["on"] == {
        "time_ref": '"SwitchWord" in s1 #1', "center_s": 6.4, "center_frame": 192,
    }
    assert lane["switches"][0]["window"] == {
        "start_s": 5.4, "end_s": 7.4, "start_frame": 162, "end_frame": 222,
    }
    assert lane["arrangements"][0]["spans"][0]["source"] == {
        "start_s": 0.0, "end_s": 2.125,
    }
    assert lane["arrangements"][0]["gain"]["linear"] == pytest.approx(1.0)
    assert lane["seams"] == [{
        "at_s": 2.0, "frame": 60, "action": "repeat", "delta_s": 1.0, "duration_s": 1.0,
    }]
    assert timeline_snapshot == original_snapshot
