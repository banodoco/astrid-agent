"""Shared read-only adopted-bed projection for sheet and visualize consumers."""
from __future__ import annotations

import copy
import json
from types import SimpleNamespace

import pytest

from astrid.core.timeline.authoring_bundle import AuthoringBundleError, _digest
from astrid.core.timeline.compatibility import ADOPTED_BED_CAPABILITY, TimelineCompatibilityError
from astrid.packs.rendering.actions.timeline_visualize.layers.base import LayerContext
from astrid.packs.rendering.actions.timeline_visualize.layers.builtin import render_sync
from astrid.packs.timeline import cli_show
from astrid.sdk.authoring_bundle import (
    open_authoring_bundle,
    project_audio_lane_for_authoring_bundle,
)
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


def _pinned_lane_bundle(tl):
    """Reopen the disposable fixture through the real immutable-closure adapter."""
    bundle = tl.bundle
    parent = copy.deepcopy(bundle["parent"])
    parent["occurrences"] = copy.deepcopy(bundle["placements"])
    shots, internals = [], []
    for sid, row in bundle["shots"].items():
        shot_revision = f"fixture-shot-{sid}"
        internal_revision = f"fixture-internal-{sid}"
        for placement in parent["occurrences"]:
            if placement["shot_id"] == sid:
                placement["shot_revision_id"] = shot_revision
        payload = copy.deepcopy(row["payload"])
        payload["internal_timeline_revision_id"] = internal_revision
        shots.append({
            "project_id": bundle["project_id"], "shot_id": sid, "revision_id": shot_revision,
            "content_digest": _digest(payload), "payload": payload,
            "internal_timeline_revision_id": internal_revision,
        })
        internal = copy.deepcopy(row["internal_timeline"])
        internals.append({
            "project_id": bundle["project_id"], "timeline_id": sid, "revision_id": internal_revision,
            "content_digest": _digest(internal), "payload": internal,
        })
    return open_authoring_bundle({
        "project_id": bundle["project_id"], "timeline_id": bundle["timeline_id"],
        "revision_id": "fixture-parent-r1", "content_digest": _digest(parent), "payload": parent,
    }, shot_revisions=shots, internal_timeline_revisions=internals)


def _bundle_bytes(bundle):
    return json.dumps(bundle, ensure_ascii=False, separators=(",", ":")).encode()


def test_pinned_authoring_lane_uses_exact_producer_without_side_effects(monkeypatch, tmp_path):
    from astrid.sdk.timeline import music_beds, music_lane, store
    from astrid.sdk.timeline.publish import _PublishMixin

    tl, owners = _adopted_lane_fixture()
    bundle = _pinned_lane_bundle(tl)
    before = _bundle_bytes(bundle)
    expected = project(tl)
    word_identities = [vars(word) for word in Checkout(copy.deepcopy(bundle)).words()]
    monkeypatch.chdir(tmp_path)

    def forbidden(*args, **kwargs):
        pytest.fail("projection attempted persistence, HEAD lookup, or materialization")

    monkeypatch.setattr(Checkout, "open", forbidden)
    monkeypatch.setattr(Checkout, "save", forbidden)
    monkeypatch.setattr(Checkout, "resolve", forbidden)
    monkeypatch.setattr(_PublishMixin, "publish", forbidden)
    monkeypatch.setattr(store, "fetch_bundle", forbidden)
    monkeypatch.setattr(music_beds, "project", forbidden)
    monkeypatch.setattr(music_beds, "dematerialize", forbidden)
    actual_project = music_lane.project
    calls = []
    capabilities = {ADOPTED_BED_CAPABILITY}

    def observed(view, *, capabilities):
        calls.append(capabilities)
        assert view.bundle is not bundle
        assert [vars(word) for word in view.words()] == word_identities
        assert view.bundle["source_mapping"] == bundle["source_mapping"]
        assert view.path is None and not view._journal and not view._pending_calls
        result = actual_project(view, capabilities=capabilities)
        # Even migration/read-helper changes in Checkout cannot reach the caller.
        view.bundle["parent"]["opaque_read_probe"] = True
        return result

    monkeypatch.setattr(music_lane, "project", observed)
    files_before = {str(path.relative_to(tmp_path)): path.read_bytes() if path.is_file() else None
                    for path in tmp_path.rglob("*")}
    lane = project_audio_lane_for_authoring_bundle(bundle, capabilities=capabilities)
    assert calls == [capabilities] and calls[0] is capabilities
    assert lane == expected
    assert [row["owner"] for row in lane["arrangements"]] == owners
    assert [[span["id"] for span in row["spans"]] for row in lane["arrangements"]] == [
        ["a-music", "a-music:b"], ["arrangement-two", "arrangement-two:b"],
    ]
    assert lane["switches"][0]["on"] == {
        "time_ref": '"SwitchWord" in s1 #1', "center_s": 6.4, "center_frame": 192,
    }
    assert lane["switches"][0]["window"] == {
        "start_s": 5.4, "end_s": 7.4, "start_frame": 162, "end_frame": 222,
    }
    assert lane["seams"] == [{
        "at_s": 2.0, "frame": 60, "action": "repeat", "delta_s": 1.0, "duration_s": 1.0,
    }]
    assert _bundle_bytes(bundle) == before
    assert {str(path.relative_to(tmp_path)): path.read_bytes() if path.is_file() else None
            for path in tmp_path.rglob("*")} == files_before


def test_pinned_authoring_lane_no_bed_and_explicit_caller_capabilities():
    bundle = _pinned_lane_bundle(tiny())
    before = _bundle_bytes(bundle)
    assert project_audio_lane_for_authoring_bundle(bundle, capabilities=()) is None
    assert _bundle_bytes(bundle) == before
    with pytest.raises(AuthoringBundleError, match="explicit caller capability"):
        project_audio_lane_for_authoring_bundle(bundle, capabilities=None)
    with pytest.raises(TypeError, match="capabilities"):
        project_audio_lane_for_authoring_bundle(bundle)
    tl, _ = _adopted_lane_fixture()
    bundle = _pinned_lane_bundle(tl)
    before = _bundle_bytes(bundle)
    with pytest.raises(TimelineCompatibilityError, match="lacks adopted-music-bed"):
        project_audio_lane_for_authoring_bundle(bundle, capabilities=())
    assert _bundle_bytes(bundle) == before


def test_pinned_authoring_lane_retains_valid_zero_output_arrangement(monkeypatch):
    from astrid.sdk.timeline import music_beds
    from tests.sdk.test_music_beds import enrolled
    from tests.sdk.test_timeline_adopt import adopt_one

    tl = enrolled()
    tl.ripple_delete(0, 9)
    tl = adopt_one(tl)
    bundle = _pinned_lane_bundle(tl)
    before = _bundle_bytes(bundle)
    monkeypatch.setattr(music_beds, "project", lambda *a, **k: pytest.fail("materialized on read"))
    lane = project_audio_lane_for_authoring_bundle(bundle, capabilities={ADOPTED_BED_CAPABILITY})
    assert lane is not None
    assert len(lane["arrangements"]) == 1
    assert lane["arrangements"][0]["owner"] == OWNER
    assert lane["arrangements"][0]["spans"] == []
    assert _bundle_bytes(bundle) == before


@pytest.mark.parametrize("damage", ["clips", "start", "duration", "edited-parent", "edited-words", "shot-id"])
def test_pinned_authoring_lane_validates_closure_even_without_a_bed(damage):
    tl = tiny()
    if damage == "clips":
        tl._internal("A")["clips"] = {}
    elif damage == "start":
        tl.bundle["placements"][0].pop("placement")
    elif damage == "duration":
        tl.bundle["placements"][0].pop("duration_ms")
    bundle = _pinned_lane_bundle(tl)
    if damage == "edited-parent":
        bundle["parent"]["config"]["opaque"] = "uninspected edit"
    elif damage == "edited-words":
        bundle["shots"]["A"]["internal_timeline"]["clips"][2]["app"]["astrid"]["words"] = []
    elif damage == "shot-id":
        bundle["shots"]["A"]["shot_id"] = "other"
    before = _bundle_bytes(bundle)
    with pytest.raises(AuthoringBundleError):
        project_audio_lane_for_authoring_bundle(bundle, capabilities=())
    assert _bundle_bytes(bundle) == before


@pytest.mark.parametrize("damage", [
    "parent", "placements", "shots", "source_mapping", "base_parent", "base_parent_payload",
    "base_placements", "schema_version", "project_id", "timeline_id",
    "missing-placement", "missing-shot", "missing-shot-source", "missing-placement-source",
    "missing-source-revision", "missing-words", "missing-output", "missing-output-ownership",
    "ambiguous-word", "malformed-bed",
])
def test_pinned_authoring_lane_refuses_incomplete_or_malformed_closure(damage):
    from astrid.sdk.timeline.music_beds import MusicBedError
    from astrid.sdk.timeline.music_lane import AudioLaneError

    tl, _ = _adopted_lane_fixture()
    if damage in ("missing-words", "ambiguous-word"):
        words = next(c for c in tl._internal("A")["clips"] if c["id"] == "a-vo")["app"]["astrid"]["words"]
        words[:] = [word for word in words if word[2] != "SwitchWord"]
        if damage == "ambiguous-word":
            words.extend([[5.9, 6.1, "SwitchWord"], [6.2, 6.3, "SwitchWord"]])
            # An unqualified duplicated word must not choose an arbitrary match.
            tl.bundle["parent"]["config"]["app"]["astrid"]["bed"]["switches"][0]["on"] = '"SwitchWord" in s1'
    elif damage == "missing-output":
        clips = tl._internal("A")["clips"]
        clips[:] = [c for c in clips if c["id"] != "a-music:b"]
    elif damage == "missing-output-ownership":
        tl.bundle["parent"]["config"]["app"]["astrid"]["bed"].pop("outputs")
    elif damage == "malformed-bed":
        tl.bundle["parent"]["config"]["app"]["astrid"]["bed"] = None
    bundle = _pinned_lane_bundle(tl)
    if damage in bundle:
        bundle.pop(damage)
    elif damage == "missing-placement":
        bundle["placements"].pop()
    elif damage == "missing-shot":
        bundle["shots"].pop("B")
    elif damage == "missing-shot-source":
        bundle["source_mapping"]["shots"].pop("A")
    elif damage == "missing-placement-source":
        bundle["source_mapping"]["placements"].pop("occ-A")
    elif damage == "missing-source-revision":
        bundle["source_mapping"]["shots"]["A"].pop("internal_timeline_revision_id")
    before = _bundle_bytes(bundle)
    with pytest.raises((AuthoringBundleError, AudioLaneError, MusicBedError)):
        project_audio_lane_for_authoring_bundle(bundle, capabilities={ADOPTED_BED_CAPABILITY})
    assert _bundle_bytes(bundle) == before


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
