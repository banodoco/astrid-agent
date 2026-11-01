"""Chapters stored as shots, edited as one flat timeline (timeline_restructure)."""
from __future__ import annotations

import copy

import pytest

from astrid.sdk import timeline_intent as intent
from astrid.sdk.timeline._common import TimelineEditError
from astrid.sdk.timeline.commands import execute
from astrid.sdk.timeline.emitter import emit_document_commands
from astrid.sdk.timeline.rebase import replay
from astrid.sdk.timeline_checkout import Checkout
from astrid.sdk.timeline_restructure import (
    STRUCTURE_KEY,
    _stable_id,
    assert_equivalent,
    describe,
    differences,
    flatten,
    is_split,
    plan,
    split,
    walls_of,
)
from tests.editing_conformance import test_commit_runtime_source_copy
from tests.timeline.test_audio_import_runtime import realm, session_for  # noqa: F401

saved_source = test_commit_runtime_source_copy.saved_source

TRACKS = [{"id": "type", "kind": "visual"}, {"id": "sprite", "kind": "visual"}, {"id": "plate", "kind": "visual"},
          {"id": "vo", "kind": "audio"}, {"id": "music", "kind": "audio"}]
ASSETS = {"P": {"media_id": "m-p"}, "R": {"media_id": "m-r"}, "Q": {"media_id": "m-q"}, "U": {"media_id": "m-u"}}


def plate(cid, at, hold, cut):
    return {"id": cid, "clipType": "am-snap-plate", "track": "plate", "asset": "P", "at": at, "hold": hold, "params": {}, "app": {"astrid": {"cut": cut}}}


def flat_film():
    """ONE shot, 12 s, three chapters (walls 4 s and 8 s). A sprite crosses the first wall, a music bed spans the film."""
    clips = [
        plate("p1", 0.0, 4.0, "c1"), plate("p2", 4.0, 4.0, "c2"), plate("p3", 8.0, 4.0, "c3"),
        {"id": "cross", "clipType": "am-sprite", "track": "sprite", "asset": "R", "at": 3.5, "hold": 1.0, "params": {}},
        {"id": "inside", "clipType": "am-sprite", "track": "sprite", "asset": "R", "at": 5.0, "hold": 1.0, "params": {}},
        {"id": "late", "clipType": "am-type", "track": "type", "at": 9.0, "hold": 1.0, "params": {"text": "hi"}},
        {"id": "vo2", "clipType": "media", "track": "vo", "asset": "U", "at": 4.5, "from": 0.0, "to": 2.0,
         "app": {"astrid": {"segment": "s2", "text": "second line", "words": [[0.1, 0.4, "second"]]}}},
        {"id": "bed", "clipType": "media", "track": "music", "asset": "Q", "at": 0.0, "from": 0.0, "to": 12.0, "volume": 0.5},
    ]
    row = {"shot_id": "film", "occurrence_id": "occ-film", "placement": {"start_ms": 0}, "duration_ms": 12000,
           "source_offset": {"start": 0, "end": 0}, "speed": {"numerator": 1, "denominator": 1}, "track": "picture",
           "transform": {}, "gain": 1, "muted": False, "provenance": {}}
    internal = {"tracks": copy.deepcopy(TRACKS), "clips": clips, "registry": {"assets": copy.deepcopy(ASSETS)},
                "effects": [], "audio": [], "layout": {}, "assets": []}
    payload = {"name": "FILM", "metadata": {}, "items": [], "pools": [], "text_bindings": []}
    config = {"theme_overrides": {"visual": {"canvas": {"fps": 30, "width": 320, "height": 180}}},
              "tracks": [{"id": "picture", "kind": "visual"}]}
    bundle = {
        "schema_version": 1, "project_id": "p", "timeline_id": "t", "base_parent": {"revision_id": "rev-0", "content_digest": "sha256:0"},
        "parent": {"config": config, "clips": [], "registry": {}},
        "placements": [row], "base_placements": {"occ-film": copy.deepcopy(row)},
        "shots": {"film": {"shot_id": "film", "payload": payload, "internal_timeline": internal,
                           "base_payload": copy.deepcopy(payload), "base_internal_timeline": copy.deepcopy(internal)}},
        "source_mapping": {"placements": {"occ-film": copy.deepcopy(row)}, "shots": {"film": {"shot_id": "film", "revision_id": "r-film"}}},
    }
    bundle["base_parent_payload"] = copy.deepcopy(bundle["parent"])
    intent.set_chapters(bundle, [{"name": "01 ONE", "from": "c1"}, {"name": "02 TWO", "from": "c2"}, {"name": "03 THREE", "from": "c3"}])
    bundle["base_parent_payload"] = copy.deepcopy(bundle["parent"])
    return bundle


def published(stored):
    """What a re-read of the published form looks like: the current content is the new base."""
    out = copy.deepcopy(stored)
    out["base_parent_payload"] = copy.deepcopy(out["parent"])
    out["base_placements"] = {r["occurrence_id"]: copy.deepcopy(r) for r in out["placements"]}
    out["source_mapping"]["placements"] = copy.deepcopy(out["base_placements"])
    for shot in out["shots"].values():
        shot["base_payload"] = copy.deepcopy(shot["payload"])
        shot["base_internal_timeline"] = copy.deepcopy(shot["internal_timeline"])
    for sid in out["shots"]:
        out["source_mapping"]["shots"].setdefault(sid, {"shot_id": sid})
    return out


def clip_ids(shot):
    return {c["id"] for c in shot["internal_timeline"]["clips"]}


def four_chapter_film(*, repeated: bool = False):
    flat = flat_film()
    flat["shots"]["film"]["internal_timeline"]["clips"].append(plate("p4", 12.0, 4.0, "c4"))
    flat["placements"][0]["duration_ms"] = 16000
    next(c for c in flat["shots"]["film"]["internal_timeline"]["clips"] if c["id"] == "bed")["to"] = 16.0
    intent.set_chapters(flat, [{"name": "A", "from": "c1"}, {"name": "B", "from": "c2"},
                               {"name": "C", "from": "c3"}, {"name": "B" if repeated else "D", "from": "c4"}])
    return flat


def test_repeated_nonconsecutive_chapter_refused_by_restructure_and_publish_split():
    flat = four_chapter_film(repeated=True)
    error = 'chapter "B" appears twice (4.000 s at c2, 12.000 s at c4); rename or remove one label'
    with pytest.raises(TimelineEditError, match="chapter.*B.*appears twice") as exc:
        split(flat)
    assert str(exc.value) == error
    with pytest.raises(TimelineEditError, match="chapter.*B.*appears twice"):
        Checkout(flat).stored_document(restructure=True)


def test_consecutive_labels_with_the_same_name_remain_one_chapter():
    flat = flat_film()
    intent.set_chapters(flat, [{"name": "A", "from": "c1"}, {"name": "A", "from": "c2"},
                               {"name": "B", "from": "c3"}])
    assert [c.name for c in plan(flat).chapters] == ["A", "B"]


def test_split_and_flatten_keep_shot_only_fx_above_plate_with_full_track_properties():
    flat = flat_film()
    config = flat["parent"]["config"]
    config["tracks"] = [
        {"id": "picture", "kind": "visual"}, {"id": "sprite", "kind": "visual"},
        {"id": "plate", "kind": "visual"}, {"id": "vo", "kind": "audio"},
        {"id": "music", "kind": "audio"},
    ]
    source_tracks = flat["shots"]["film"]["internal_timeline"]["tracks"]
    source_tracks[:] = [
        {"id": "chrome", "kind": "visual", "opacity": 0.8},
        {"id": "type", "kind": "visual", "scale": 1.2},
        {"id": "fx", "kind": "visual", "blend": "screen", "opacity": 0.7},
        {"id": "sprite", "kind": "visual"}, {"id": "plate", "kind": "visual"},
        {"id": "vo", "kind": "audio"}, {"id": "music", "kind": "audio"},
        {"id": "sfx", "kind": "audio", "volume": 0.6},
    ]
    flat["shots"]["film"]["internal_timeline"]["clips"].append(
        {"id": "fx-clip", "clipType": "am-type", "track": "fx", "at": 0.0, "hold": 1.0, "params": {}}
    )

    stored = split(flat)
    expected = ["picture", "chrome", "type", "fx", "sprite", "plate", "vo", "music", "sfx"]
    assert [t["id"] for t in stored["parent"]["config"]["tracks"]] == expected
    fx = next(t for t in stored["parent"]["config"]["tracks"] if t["id"] == "fx")
    assert fx == {"id": "fx", "kind": "visual", "blend": "screen", "opacity": 0.7}
    flattened = flatten(published(stored))
    flat_shot = flattened["shots"][flattened["placements"][0]["shot_id"]]["internal_timeline"]
    assert [t["id"] for t in flat_shot["tracks"]] == expected
    assert next(t for t in flat_shot["tracks"] if t["id"] == "fx") == fx


def test_a_film_whose_shots_repeat_a_name_is_not_flattened():
    stored = published(split(four_chapter_film()))
    stored["shots"][stored["placements"][-1]["shot_id"]]["payload"]["name"] = "B"
    assert not is_split(stored)
    assert flatten(stored) is stored


def test_set_chapters_on_a_split_film_is_refused_but_dropping_is_allowed():
    stored = published(split(four_chapter_film()))
    with pytest.raises(TimelineEditError, match="rename the shot instead"):
        intent.set_chapters(stored, [{"name": "A", "from": "c1"}])
    with pytest.raises(TimelineEditError, match="rename the shot instead"):
        intent.set_chapters(Checkout(stored).bundle, [{"name": "A", "from": "c1"}])
    intent.set_chapters(stored, [])


def test_split_fallback_id_does_not_overwrite_an_existing_shot():
    flat = flat_film()
    occupied = _stable_id("chapter-shot", flat["timeline_id"], "02 TWO")
    flat["placements"][0]["shot_id"] = occupied
    flat["shots"] = {occupied: flat["shots"].pop("film")}
    flat["shots"][occupied]["shot_id"] = occupied
    original = copy.deepcopy(flat["shots"][occupied])
    stored = split(flat)
    assert len({r["shot_id"] for r in stored["placements"]}) == 3
    assert occupied not in {r["shot_id"] for r in stored["placements"]}
    assert stored["shots"][occupied] == original
    assert_equivalent(flat, stored)


def test_split_stores_cuttable_crossers_in_shots_and_music_on_the_film():
    stored = split(flat_film())
    assert [(r["placement"]["start_ms"], r["duration_ms"]) for r in stored["placements"]] == [(0, 4000), (4000, 4000), (8000, 4000)]
    shots = [stored["shots"][r["shot_id"]] for r in stored["placements"]]
    assert [s["payload"]["name"] for s in shots] == ["01 ONE", "02 TWO", "03 THREE"]
    assert clip_ids(shots[0]) == {"p1", _stable_id("chapter-piece", "t", "cross", "01 ONE", 0, 0)}
    assert clip_ids(shots[1]) == {"p2", "inside", "vo2", _stable_id("chapter-piece", "t", "cross", "02 TWO", 4000, 1)}
    assert clip_ids(shots[2]) == {"p3", "late"}
    assert {c["id"] for c in stored["parent"]["clips"]} == {"bed"}  # music remains film-level
    assert "clips" not in stored["parent"]["config"]  # one list only (Runtime 409s on two differing lists)
    assert stored["parent"]["config"]["tracks"][0]["id"] == "picture"
    assert [t["id"] for t in stored["parent"]["config"]["tracks"]] == ["picture", "type", "sprite", "plate", "vo", "music"]
    inside = next(c for c in shots[1]["internal_timeline"]["clips"] if c["id"] == "inside")
    assert inside["at"] == pytest.approx(1.0)  # chapter-relative
    assert set(stored["parent"]["registry"]["assets"]) == {"Q"}  # only the music asset remains film-level
    assert set(shots[1]["internal_timeline"]["registry"]["assets"]) == {"P", "R", "U"}
    assert STRUCTURE_KEY not in stored


def test_the_renderer_gets_the_same_input_before_and_after():
    flat = flat_film()
    assert differences(flat, split(flat)) == []


def test_round_trip_flat_to_stored_to_flat_and_back_keeps_everything():
    flat = flat_film()
    stored = published(split(flat))
    assert is_split(stored)
    back = flatten(copy.deepcopy(stored))
    assert STRUCTURE_KEY in back and len(back["placements"]) == 1
    want = {c["id"]: c for c in flat["shots"]["film"]["internal_timeline"]["clips"]}
    got = {c["id"]: c for c in back["shots"][back["placements"][0]["shot_id"]]["internal_timeline"]["clips"]}
    assert set(got) == set(want)
    for cid in want:
        assert got[cid]["at"] == pytest.approx(want[cid]["at"]) and got[cid].get("hold") == want[cid].get("hold")
    assert back["parent"]["clips"] == [] and "tracks" not in back["parent"]["config"]
    again = split(back)  # re-publishing an untouched film reuses the shot and occurrence ids
    assert [r["shot_id"] for r in again["placements"]] == [r["shot_id"] for r in stored["placements"]]
    assert again["parent"] == stored["parent"] and again["shots"] == stored["shots"]


def test_a_checkout_of_the_stored_form_edits_one_flat_timeline_and_sees_film_level_clips():
    tl = Checkout(published(split(flat_film())))
    ids = {c.id for c in tl.clips()}
    assert {"cross", "bed", "inside", "p3"} <= ids  # parent clips are clips (no false EMPTY, no hidden bed)
    assert tl.clip("late").start == pytest.approx(9.0)  # film seconds, not chapter seconds
    assert len(tl._shot_ids()) == 1
    assert tl.stored_document()["parent"]["clips"]


def test_reflow_that_moves_a_wall_rehomes_a_clip_between_film_and_chapter():
    stored = published(split(flat_film()))
    tl = Checkout(stored)
    shot = tl._shot_ids()[0]
    clips = tl.bundle["shots"][shot]["internal_timeline"]["clips"]
    for c in clips:  # a ripple opening 1 s at 4 s: everything from the wall on moves, the first chapter grows
        if c["id"] not in ("cross", "bed") and c["at"] >= 4.0 - 1e-9:
            c["at"] += 1.0
    next(c for c in clips if c["id"] == "p1")["hold"] = 5.0
    tl.bundle["placements"][0]["duration_ms"] = 13000
    after = tl.stored_document()
    assert [r["placement"]["start_ms"] for r in after["placements"]] == [0, 5000, 9000]
    assert {c["id"] for c in after["parent"]["clips"]} == {"bed"}  # "cross" (3.5-4.5) no longer crosses a wall
    first = after["shots"][after["placements"][0]["shot_id"]]
    assert {"p1", "cross"} == clip_ids(first)
    assert [r["shot_id"] for r in after["placements"]] == [r["shot_id"] for r in stored["placements"]]


def test_three_way_merge_works_on_the_stored_form(restructure_runtime):
    from astrid.sdk.timeline.commit import bundle_from_document

    env = restructure_runtime
    Checkout.open(env.project, "t", client=env.client).publish(
        "Stored merge fixture", restructure=True, client=env.client)
    base = bundle_from_document(env.document())
    candidate = copy.deepcopy(base)
    next(c for shot in candidate["shots"].values() for c in shot["internal_timeline"]["clips"]
         if c["id"] == "late")["hold"] = 1.5
    next(c for c in candidate["parent"]["clips"] if c["id"] == "bed")["volume"] = 0.9
    head = copy.deepcopy(base)
    first = head["shots"][head["placements"][0]["shot_id"]]
    first["internal_timeline"]["clips"][0]["hold"] = 3.9  # someone else touched a different clip in another chapter
    # The retired store merger's witness now exercises the commit engine's
    # guarded emitter/replay: disjoint changes survive, overlapping ones refuse.
    pending = execute(base, emit_document_commands(base, candidate))
    conflicts = []
    try:
        merged = replay(pending, head).bundle
    except TimelineEditError as exc:
        conflicts.append(str(exc))
    assert conflicts == []
    by_id = {c["id"]: c for s in merged["shots"].values() for c in s["internal_timeline"]["clips"]}
    assert by_id["late"]["hold"] == 1.5 and by_id["p1"]["hold"] == 3.9
    assert next(c for c in merged["parent"]["clips"] if c["id"] == "bed")["volume"] == 0.9
    theirs = copy.deepcopy(base)
    next(c for c in theirs["parent"]["clips"] if c["id"] == "bed")["volume"] = 0.1
    with pytest.raises(TimelineEditError) as conflict:
        replay(pending, theirs)
    conflicts = [str(conflict.value.to_json())]
    assert any("bed" in line for line in conflicts)  # the same film-level clip changed on both sides refuses


def test_restructure_refuses_what_would_change_the_render_after_cutting():
    flat = flat_film()
    good = split(flat)
    assert_equivalent(flat, good)
    bad = copy.deepcopy(good)
    next(c for c in bad["parent"]["clips"] if c["id"] == "bed")["volume"] = 0.25
    with pytest.raises(TimelineEditError, match="refused"):
        assert_equivalent(flat, bad)
    short = copy.deepcopy(good)
    short["parent"]["config"]["tracks"] = [t for t in short["parent"]["config"]["tracks"] if t["id"] != "picture"]
    assert any("track order" in line for line in differences(flat, short))
    moved = copy.deepcopy(good)
    cross_piece = next(c for shot in moved["shots"].values() for c in shot["internal_timeline"]["clips"]
                       if c.get("app", {}).get("astrid", {}).get("timeline_restructure_piece", {}).get("id") == "cross")
    cross_piece["at"] += 0.1
    assert differences(flat, moved)


def test_renderer_equivalence_detects_group_order_inversion_and_piece_edits():
    flat = flat_film()
    good = split(flat)
    second = good["shots"][good["placements"][1]["shot_id"]]["internal_timeline"]
    second["tracks"] = list(reversed(second["tracks"]))
    assert any("track order" in line for line in differences(flat, good))

    good = split(flat)
    second = good["shots"][good["placements"][1]["shot_id"]]["internal_timeline"]
    next(track for track in second["tracks"] if track["id"] == "sprite")["opacity"] = 0.5
    assert differences(flat, good)

    good = split(flat)
    piece = next(c for shot in good["shots"].values() for c in shot["internal_timeline"]["clips"]
                 if c.get("app", {}).get("astrid", {}).get("timeline_restructure_piece", {}).get("id") == "cross"
                 and c["app"]["astrid"]["timeline_restructure_piece"]["index"] == 1)
    piece["asset"] = "changed-source"
    assert differences(flat, good)


def test_renderer_equivalence_detects_missing_piece_and_changed_source_offset():
    flat = flat_film()
    clips = flat["shots"]["film"]["internal_timeline"]["clips"]
    clips.append({"id": "media-cross", "clipType": "media", "track": "plate", "asset": "P",
                  "at": 3.5, "from": 5.0, "to": 9.0, "hold": 4.0, "speed": 2.0})
    good = split(flat)
    second_shot = good["shots"][good["placements"][1]["shot_id"]]["internal_timeline"]
    second_piece = next(c for c in second_shot["clips"]
                        if c.get("app", {}).get("astrid", {}).get("timeline_restructure_piece", {}).get("id") == "media-cross")
    second_piece["from"] += 0.25
    assert differences(flat, good)

    good = split(flat)
    second_shot = good["shots"][good["placements"][1]["shot_id"]]["internal_timeline"]
    second_shot["clips"] = [c for c in second_shot["clips"]
                            if c.get("app", {}).get("astrid", {}).get("timeline_restructure_piece", {}).get("id") != "media-cross"]
    assert differences(flat, good)

    good = split(flat)
    second_shot = good["shots"][good["placements"][1]["shot_id"]]["internal_timeline"]
    second_piece = next(c for c in second_shot["clips"]
                        if c.get("app", {}).get("astrid", {}).get("timeline_restructure_piece", {}).get("id") == "media-cross")
    second_piece["at"] += 0.1
    assert differences(flat, good)


def test_stills_text_and_unsupported_elements_route_without_losing_names():
    flat = flat_film()
    clips = flat["shots"]["film"]["internal_timeline"]["clips"]
    clips.extend([
        {"id": "still-cross", "clipType": "image", "track": "plate", "asset": "P", "at": 3.5,
         "from": 5.0, "to": 6.0, "hold": 1.0, "params": {"fit": "cover"}},
        {"id": "text-cross", "clipType": "text", "track": "type", "at": 3.5,
         "hold": 1.0, "params": {"text": "hello"}},
        {"id": "footage-cross", "name": "Footage", "clipType": "am-footage", "track": "sprite",
         "at": 3.5, "hold": 1.0, "params": {"in": 4, "zoom": 2}},
        {"id": "type-cross", "name": "Title", "clipType": "am-type", "track": "type",
         "at": 3.5, "hold": 1.0, "params": {"text": "legacy", "keyframes": [{"frame": 1}] }},
    ])
    stored = split(flat)
    chapter_clips = [c for shot in stored["shots"].values() for c in shot["internal_timeline"]["clips"]]
    sources = {c.get("app", {}).get("astrid", {}).get("timeline_restructure_piece", {}).get("id")
               for c in chapter_clips}
    assert {"still-cross", "text-cross"} <= sources
    still_pieces = [c for c in chapter_clips
                    if c.get("app", {}).get("astrid", {}).get("timeline_restructure_piece", {}).get("id") == "still-cross"]
    assert [(c["from"], c["to"]) for c in still_pieces] == [(5.0, 5.5), (5.0, 5.5)]
    assert {c["id"] for c in stored["parent"]["clips"]} == {"bed", "footage-cross", "type-cross"}
    report = "\n".join(describe(flat))
    assert "footage-cross (Footage) [am-footage]" in report
    assert "type-cross (Title) [am-type]" in report
    assert differences(flat, stored)  # the specified -over fallback changes active layer order in this fixture


def test_walls_come_from_the_chapter_labels():
    assert walls_of(flat_film()) == [("01 ONE", 0, 4000), ("02 TWO", 4000, 4000), ("03 THREE", 8000, 4000)]
    flat = flat_film()
    intent.set_chapters(flat, [{"name": "only", "from": "c1"}])
    assert plan(flat) is None and split(flat)["placements"] == flat["placements"]  # not a chapter film: unchanged


def test_frame_aligned_clip_stays_in_chapter_when_cut_start_has_float_error(monkeypatch):
    flat = flat_film()
    clips = flat["shots"]["film"]["internal_timeline"]["clips"]
    clips.append({"id": "c17-38-am-footage", "clipType": "am-sprite", "track": "sprite",
                  "asset": "R", "at": 60.833, "hold": 0.06700000000001, "params": {}})
    clips.append({"id": "first-clip-at-rounded-wall", "clipType": "am-sprite", "track": "sprite",
                  "asset": "R", "at": 60.899998, "hold": 1.0, "params": {}})
    flat["placements"][0]["duration_ms"] = 70000
    monkeypatch.setattr(Checkout, "_cut_groups", lambda self: [
        {"id": "c1", "start": 0.0}, {"id": "c2", "start": 60.899998}, {"id": "c3", "start": 65.0},
    ])

    planned = plan(flat)

    assert planned.chapters[1].start_ms == 60900
    assert "c17-38-am-footage" in {c["id"] for c in planned.chapters[0].clips}
    assert "c17-38-am-footage" not in {c["id"] for c in planned.film_clips}
    assert "first-clip-at-rounded-wall" in {c["id"] for c in planned.chapters[1].clips}
    assert "first-clip-at-rounded-wall" not in {c["id"] for c in planned.film_clips}


def test_narration_binds_one_script_per_chapter_shot():
    tl = Checkout(published(split(flat_film())))
    narration = tl.narration()
    second = tl.bundle[STRUCTURE_KEY]["placements"][1]["shot_id"]
    assert narration == {second: "second line\n"}


def test_shift_parent_moves_a_mirrored_config_clips_list_too():
    tl = Checkout({"project_id": "p", "timeline_id": "t", "placements": [], "shots": {},
                   "parent": {"config": {"clips": [{"id": "x", "at": 5.0}]}, "clips": [{"id": "x", "at": 5.0}]}})
    tl._shift_parent(2.0, 1.5)
    assert tl.bundle["parent"]["clips"][0]["at"] == 6.5 and tl.bundle["parent"]["config"]["clips"][0]["at"] == 6.5


def test_splitting_is_opt_in_per_film():
    """A one-shot film with chapter labels stays one shot unless it is restructured; once split, it stays split."""
    tl = Checkout(flat_film())
    assert len(tl.stored_document()["placements"]) == 1  # an ordinary publish never restructures a film
    split_doc = tl.stored_document(restructure=True)
    assert len(split_doc["placements"]) == 3  # `timelines restructure` opts this film in
    again = Checkout(published(split_doc))  # read back flat for editing
    assert len(again._shot_ids()) == 1
    assert len(again.stored_document()["placements"]) == 3  # later publishes keep its chapters


@pytest.fixture
def restructure_runtime(tmp_path, monkeypatch):
    """Build the source with ordinary commands; every film write uses the real provider."""
    from types import SimpleNamespace

    from tests.helpers.runtime import use_runtime

    runtime = use_runtime()
    from pathlib import Path

    import runtime_protocol

    assert Path(runtime_protocol.__file__).resolve().parent.parent == runtime.resolve()
    monkeypatch.syspath_prepend(str(runtime / 'tests'))
    from runtime_protocol.errors import NotFoundError
    from runtime_protocol.timeline_commit_provider import load_timeline_commit_provider
    from test_generation_publish_v1_settlement import _effect, _fixture, _output, _settle

    from astrid.sdk.contracts import DomainResult, ErrorObject
    from astrid.sdk.remote import RemoteTimelines
    from astrid.sdk.timeline.commit import bundle_from_document
    from astrid.sdk.timeline.emitter import emit_document_commands
    from astrid.sdk.workspace_client import WorkspaceClient

    names = ('P', 'R', 'Q', 'U')
    def effect(project):
        return _effect(project, groups=[{'group_key': 'main', 'selectors': [
            {'selector': name, 'ordinal': i, 'variant_key': name, 'output_port': 'image'}
            for i, name in enumerate(names)]}])
    service, project, task, attempt, admitted = _fixture(tmp_path, effect_factory=effect)
    project = project['id']
    _settle(service, attempt, [_output(name.encode(), output_port='image', group_key='main',
        variant_key=name, ordinal=i, filename=name + '.png', media_type='image/png')
        for i, name in enumerate(names)], effect=admitted)
    pins = {row['variant_key']: service.get_project_media_selection(project, {
        'association_id': row['association_id'], 'object_id': row['object_id'],
        'version': row['lifecycle']['version']}, authorize=lambda: None)
        for row in service.managed_outputs(task['task']['id'])}
    service.register_timeline_commit_provider(load_timeline_commit_provider('astrid'))
    service.create_timeline(project, 't', idempotency_key='create')
    service.list_timeline_commits(project, 't', authorize=lambda: None)

    def document():
        head = service.get_project_timeline(project, 't')
        return service._timeline_commits()[0].revision_closure(project, 't', head['head_revision_id'])

    calls, replies = [], []
    def send(p, t, body, *, idempotency_key):
        assert (p, t) == (project, 't')
        calls.append((copy.deepcopy(body), idempotency_key))
        result = service.commit_timeline(project, t, body, idempotency_key=idempotency_key,
            publisher='editor', invocation_path=f'/v1/projects/{project}/timelines/{t}/commit',
            authorize=lambda: None)
        replies.append(copy.deepcopy(result))
        return result

    def list_timelines(p, cursor=None, limit=50):
        page = service.list_timelines(p, cursor=cursor, limit=limit)
        return page['items'], page.get('next_cursor')

    generated = SimpleNamespace(
        commit_timeline=send,
        list_timelines=list_timelines,
        inspect_timeline=lambda p, t, options=None: service.inspect_timeline(p, t, options),
        list_timeline_commits=lambda p, t, cursor=None, limit=200:
            service.list_timeline_commits(p, t, cursor=cursor, limit=limit, authorize=lambda: None),
        get_project_parent_composition_revision=lambda p, t, revision:
            service.get_project_parent_composition_revision(p, t, revision),
        get_project_shot_revision=lambda p, shot, revision:
            service.get_project_shot_revision(p, shot, revision),
        get_project_timeline_revision=lambda p, timeline, revision:
            service.get_project_timeline_revision(p, timeline, revision),
    )
    transport = WorkspaceClient.__new__(WorkspaceClient)
    transport._generated = generated
    transport._last_handshake = {'realm_id': service.realm['id'], 'actor_id': 'editor'}
    client = SimpleNamespace(_remote=SimpleNamespace(_transport=transport), timelines=RemoteTimelines(transport))
    client.timelines._resolve_timeline = RemoteTimelines._resolve_timeline.__get__(client.timelines)
    client.timelines.open_composition = RemoteTimelines.open_composition.__get__(client.timelines)
    client.timelines.open_bundle = RemoteTimelines.open_bundle.__get__(client.timelines)
    client.projects = SimpleNamespace(show=lambda p: DomainResult.success({'project_id': project}))
    client.media = SimpleNamespace(import_bytes=lambda **kw: DomainResult.success(service.ingest(
        project, kw['data'], original_name=kw['filename'], media_type=kw['media_type'],
        idempotency_key=kw['idempotency_key'])['data']))
    document_calls = []
    def show(identity, **kw):
        document_calls.append('show')
        try:
            return DomainResult.success(service.get_document(project, identity))
        except NotFoundError:
            return DomainResult.failure(ErrorObject(code='not_found', message='document not found', details={}))
    def create(**kw):
        document_calls.append('create')
        return DomainResult.success(service.create_document(project, {
            key: kw[key] for key in ('document_id', 'kind', 'content')}))
    client.documents = SimpleNamespace(show=show, create=create)

    def commit(commands, key):
        head = service.get_project_timeline(project, 't')
        body = {'api_version': 1, 'project_id': project, 'timeline_id': 't',
            'base_revision_id': head['head_revision_id'], 'base_publication_id': head['head_publication_id'],
            'commands': commands, 'label': key, 'merge': 'fields', 'reads': [],
            'steps': [{'id': 'setup', 'label': key, 'intent': '', 'tags': [],
                'command_ordinals': list(range(len(commands))),
                'patch': [change for call in commands if call['op'] == 'doc.patch' for change in call['args']['changes']]}]}
        return send(project, 't', body, idempotency_key=key)
    try:
        from astrid.sdk.timeline.capture import capture_track_create
        from astrid.sdk.timeline.native_occurrence_create import capture_occurrence_create
        from astrid.sdk.timeline.native_occurrence_media_commands import (
            capture_occurrence_add_media,
        )

        base = bundle_from_document(document())
        commit([capture_track_create(base, 'parent', {'id': 'picture', 'kind': 'visual'})], 'track')
        base = bundle_from_document(document())
        commit([capture_occurrence_create(base, 'occ-film', 'film', name='FILM',
            track='picture', on=0, duration=12)], 'shot')
        base = bundle_from_document(document())
        commit([capture_track_create(base, 'occurrence:occ-film', {'id': 'plate', 'kind': 'visual'})], 'child-track')
        for name, pin in pins.items():
            base = bundle_from_document(document())
            verified = service.get_project_verified_media(project, pin, authorize=lambda: None)
            commit([capture_occurrence_add_media(base, 'occurrence:occ-film', {
                'id': 'seed-' + name, 'clipType': 'image', 'track': 'plate',
                'asset': pin['asset'], 'at': 0, 'hold': 1}, pin, verified_media=[verified])], 'source-' + name)
        base = bundle_from_document(document())
        candidate = copy.deepcopy(base)
        film = flat_film()
        for name, asset in film['shots']['film']['internal_timeline']['registry']['assets'].items():
            asset['media_id'] = pins[name]['object_id']
        candidate['parent'] = film['parent']
        identity = candidate['placements'][0]['shot_id']
        candidate['shots'][identity]['payload'].update(film['shots']['film']['payload'])
        candidate['shots'][identity]['internal_timeline'].update(film['shots']['film']['internal_timeline'])
        # Opaque authored fields exercise explicit null and absence preservation.
        candidate['parent']['opaque'] = {'null': None, 'ordered': [None, False, 0]}
        commit(emit_document_commands(base, candidate), 'source-content')
        calls.clear()
        replies.clear()
        yield SimpleNamespace(client=client, project=project, document=document, calls=calls,
            replies=replies, generated=generated, send=send, service=service, document_calls=document_calls)
    finally:
        service.close()


def test_cli_restructure_commits_the_complete_equivalent_bundle_and_followups(restructure_runtime):
    from astrid.core.timeline.authoring_bundle import compile_authoring_candidate
    from astrid.packs.timeline import cli
    from astrid.sdk.timeline.commit import bundle_from_document

    env = restructure_runtime
    original = copy.deepcopy(env.document())
    original_head = env.client.timelines.history(env.project, 't', limit=1).data['head']
    before = Checkout(bundle_from_document(original))
    expected = compile_authoring_candidate(before.stored_document(restructure=True)).publication
    parsed = cli.build_parser(env.client).parse_args(['restructure', 't', '--project', env.project])
    assert parsed.handler(parsed) == 0
    assert len(env.calls) == len(env.replies) == 2
    cut = env.replies[0]['data']['canonical_document']
    assert cut['parent_revision']['payload'] == expected['parent_composition']
    assert cut['shot_revisions'] == expected['shot_revisions']
    assert cut['internal_timeline_revisions'] == expected['internal_timeline_revisions']
    assert cut['dependency_manifest'] == {**expected['dependency_manifest'], 'documents': []}
    rows = cut['parent_revision']['payload']['occurrences']
    assert len(rows) == len({row['shot_id'] for row in rows}) == len({row['occurrence_id'] for row in rows}) == 3
    assert all(row['occurrence_id'] != 'occ-film' for row in rows)
    assert [shot['payload']['name'] for row in rows for shot in cut['shot_revisions']
            if shot['shot_id'] == row['shot_id']] == ['01 ONE', '02 TWO', '03 THREE']
    assert cut['parent_revision']['payload']['opaque'] == {'null': None, 'ordered': [None, False, 0]}
    assert 'missing' not in cut['parent_revision']['payload']['opaque']
    assert_equivalent(before.bundle, bundle_from_document(cut))
    body = env.calls[0][0]
    assert body['api_version'] == 1 and body['merge'] == 'atomic'
    assert body['base_revision_id'] == original['parent_revision']['revision_id']
    assert body['base_publication_id'] == original_head['publication_id']
    assert body['commands'][-1]['op'] == 'timeline.restructure'
    assert env.calls[1][0]['commands'][0]['op'] == 'text-binding.set'
    assert env.document_calls == ['show', 'create']
    assert env.document()['parent_revision']['revision_id'] == env.replies[1]['data']['accepted_revision_id']


def test_restructure_retries_the_original_commit_after_lost_ack(restructure_runtime):
    from astrid.sdk.workspace_client import WorkspaceClientError

    env = restructure_runtime
    original = Checkout.open(env.project, 't', client=env.client)
    lost = False
    def send(*args, **kwargs):
        nonlocal lost
        result = env.send(*args, **kwargs)
        if not lost:
            lost = True
            raise WorkspaceClientError('lost ACK')
        return result
    env.generated.commit_timeline = send
    with pytest.raises(WorkspaceClientError):
        original.publish('Restructure', restructure=True, client=env.client, idempotency_key='retry-structure')
    reopened = Checkout.open(env.project, 't', client=env.client)
    result = reopened.publish('Restructure', restructure=True, client=env.client, idempotency_key='retry-structure')
    assert env.calls[0] == env.calls[1]
    assert env.replies[0] == env.replies[1]
    assert len(env.calls) == 3
    assert result['new_head'] == env.document()['parent_revision']['revision_id']
    assert result['narration_pinned'] and result['transcripts'][0]['status'] == 'created'
    assert len(env.client.timelines.history(env.project, 't', limit=200).data['items']) == 11


def test_restructure_reconciles_narration_without_republishing_the_cut(restructure_runtime):
    from astrid.sdk.workspace_client import WorkspaceClientError

    env = restructure_runtime
    original = Checkout.open(env.project, 't', client=env.client)
    lost = False
    def send(*args, **kwargs):
        nonlocal lost
        result = env.send(*args, **kwargs)
        if len(env.calls) == 2 and not lost:
            lost = True
            raise WorkspaceClientError('lost narration ACK')
        return result
    env.generated.commit_timeline = send
    first = original.publish('Restructure', restructure=True, client=env.client, idempotency_key='narration-retry')
    assert first['new_head'] == env.replies[0]['data']['accepted_revision_id'] and first['narration_error']
    reopened = Checkout.open(env.project, 't', client=env.client)
    result = reopened.publish('Restructure', restructure=True, client=env.client, idempotency_key='narration-retry')
    assert env.calls[1] == env.calls[2]
    assert env.replies[1] == env.replies[2]
    assert len(env.calls) == 3
    assert result['receipt'] == first['receipt']
    assert result['new_head'] == env.document()['parent_revision']['revision_id']
    assert result['narration_pinned'] and result['transcripts'][0]['status'] == 'unchanged'
    assert env.document_calls.count('create') == 1


@pytest.mark.parametrize('damage', ['foreign', 'before', 'order', 'identity', 'media', 'occupied', 'stale'])
def test_restructure_provider_refuses_changed_inputs_without_writes(restructure_runtime, damage):
    from runtime_protocol.errors import RuntimeErrorBase

    from astrid.sdk.timeline.commands import execute
    from astrid.sdk.timeline.commit import bundle_from_document

    env = restructure_runtime
    base = bundle_from_document(env.document())
    command = execute(base, [{'schema_version': 1, 'op': 'timeline.restructure',
        'version': '1', 'select': 'parent', 'args': {}}]).commands[0]
    head = env.client.timelines.history(env.project, 't', limit=1).data['head']
    body = {'api_version': 1, 'project_id': env.project, 'timeline_id': 't',
        'base_revision_id': head['revision_id'], 'base_publication_id': head['publication_id'],
        'commands': [command], 'label': 'Restructure', 'merge': 'atomic',
        'reads': [{'kind': 'scope', 'ref': 'timeline', 'path': []}],
        'steps': [{'id': 'cut', 'label': 'Restructure', 'intent': '', 'tags': [],
            'command_ordinals': [0], 'patch': []}]}
    if damage == 'foreign':
        body['project_id'] = 'foreign'
    elif damage == 'before':
        command['reads'][0]['state']['value']['content_digest'] = 'sha256:' + '0' * 64
    elif damage == 'order':
        command['created'].reverse()
    elif damage == 'identity':
        command['created'][0] = 'shot:forged'
    elif damage == 'media':
        command['args']['media'] = [{'media_id': 'sha256:' + '0' * 64}]
    elif damage == 'stale':
        from astrid.sdk.timeline import open as open_timeline

        timeline = open_timeline(env.project, 't', client=env.client)
        timeline.bundle['parent']['opaque']['concurrent'] = True
        timeline.commit('Concurrent edit', idempotency_key='concurrent')
    else:
        from astrid.sdk.timeline.capture import capture_track_create
        from astrid.sdk.timeline.native_occurrence_create import capture_occurrence_create

        env.service.create_timeline(env.project, 'occupier', idempotency_key='occupier')
        def occupy(calls, key):
            current = env.service.get_project_timeline(env.project, 'occupier')
            return env.service.commit_timeline(env.project, 'occupier', {
                'api_version': 1, 'project_id': env.project, 'timeline_id': 'occupier',
                'base_revision_id': current['head_revision_id'],
                'base_publication_id': current['head_publication_id'], 'commands': calls},
                idempotency_key=key, publisher='editor',
                invocation_path=f'/v1/projects/{env.project}/timelines/occupier/commit', authorize=lambda: None)
        current = env.service.get_project_timeline(env.project, 'occupier')
        empty = bundle_from_document(env.service._timeline_commits()[0].revision_closure(
            env.project, 'occupier', current['head_revision_id']))
        tracked = occupy([capture_track_create(empty, 'parent', {'id': 'picture', 'kind': 'visual'})], 'occupied-track')
        empty = bundle_from_document(tracked['data']['canonical_document'])
        identity = command['created'][0].removeprefix('shot:')
        occupy([capture_occurrence_create(empty, 'reserved', identity, name='Occupied',
            track='picture', on=0, duration=1)], 'occupied-shot')
    original, stored = copy.deepcopy(env.document()), tuple(env.service.store.conn.iterdump())
    history = env.client.timelines.history(env.project, 't', limit=200).data
    with pytest.raises(RuntimeErrorBase):
        env.send(env.project, 't', body, idempotency_key='bad-' + damage)
    assert env.document() == original
    assert env.client.timelines.history(env.project, 't', limit=200).data == history
    assert tuple(env.service.store.conn.iterdump()) == stored


def test_restructure_reuses_an_existing_chapter_root(restructure_runtime):
    from astrid.sdk.timeline import open as open_timeline

    env = restructure_runtime
    timeline = open_timeline(env.project, 't', client=env.client)
    shot = timeline.bundle['shots'][timeline.bundle['placements'][0]['shot_id']]
    shot['payload']['name'] = '02 TWO'
    timeline.commit('Name source chapter', idempotency_key='chapter-name')
    original = env.document()
    old_row = original['parent_revision']['payload']['occurrences'][0]
    checkout = Checkout.open(env.project, 't', client=env.client)
    checkout.publish('Restructure', restructure=True, client=env.client, idempotency_key='reuse-root')
    rows = env.document()['parent_revision']['payload']['occurrences']
    assert rows[1]['shot_id'] == old_row['shot_id']
    assert rows[1]['occurrence_id'] == old_row['occurrence_id']
    assert len({row['shot_id'] for row in rows}) == 3


def test_restructure_retries_narration_before_a_flight_exists(restructure_runtime):
    from astrid.sdk.timeline._common import TimelineEditError

    env = restructure_runtime
    ingest = env.client.media.import_bytes
    def unavailable(**kw):
        raise TimelineEditError('ingest unavailable')
    env.client.media.import_bytes = unavailable
    first = Checkout.open(env.project, 't', client=env.client).publish(
        'Restructure', restructure=True, client=env.client)
    assert first['narration_error'] and len(env.calls) == 1
    env.client.media.import_bytes = ingest
    result = Checkout.open(env.project, 't', client=env.client).publish(
        'Restructure', restructure=True, client=env.client)
    assert result['receipt'] == first['receipt'] and result['narration_pinned']
    assert len(env.calls) == 2 and env.calls[1][0]['commands'][0]['op'] == 'text-binding.set'


def test_restructure_order_carrier_only_permutes_unchanged_rows():
    from astrid.sdk.timeline._command_state import CommandError, State, present

    bundle = flat_film()
    state = State(bundle)
    original = copy.deepcopy(bundle['placements'])
    with pytest.raises(CommandError, match='preserve every authored row'):
        state.set('parent', ['occurrences'], present([]))
    changed = copy.deepcopy(original)
    changed[0]['shot_id'] = 'foreign'
    with pytest.raises(CommandError, match='preserve every authored row'):
        state.set('parent', ['occurrences'], present(changed))
    changed = copy.deepcopy(original)
    changed[0]['muted'] = 0
    with pytest.raises(CommandError, match='preserve every authored row'):
        state.set('parent', ['occurrences'], present(changed))
    assert bundle['placements'] == original


def test_restructure_provider_preview_has_no_writes(restructure_runtime):
    from astrid.sdk.timeline.commands import execute
    from astrid.sdk.timeline.commit import bundle_from_document

    env = restructure_runtime
    base = bundle_from_document(env.document())
    command = execute(base, [{'schema_version': 1, 'op': 'timeline.restructure',
        'version': '1', 'select': 'parent', 'args': {}}]).commands[0]
    head = env.client.timelines.history(env.project, 't', limit=1).data['head']
    body = {'api_version': 1, 'project_id': env.project, 'timeline_id': 't',
        'base_revision_id': head['revision_id'], 'base_publication_id': head['publication_id'],
        'commands': [command], 'label': 'Preview restructure', 'merge': 'atomic',
        'reads': [{'kind': 'scope', 'ref': 'timeline', 'path': []}],
        'steps': [{'id': 'cut', 'label': 'Preview restructure', 'intent': 'Preserve the film',
            'tags': ['restructure'], 'command_ordinals': [0], 'patch': [], 'reads': []}],
        'source': {'via': 'python'}, 'rationale': 'Preview the chapter cut', 'group_id': 'chapter-cut',
        'dry_run': True}
    original, stored = copy.deepcopy(env.document()), tuple(env.service.store.conn.iterdump())
    result = env.send(env.project, 't', body, idempotency_key='preview')
    assert result['receipt'] is None
    assert result['data']['status'] == 'preview' and result['data']['would_status'] == 'accepted'
    assert_equivalent(base, bundle_from_document(result['data']['canonical_document']))
    assert env.document() == original
    assert tuple(env.service.store.conn.iterdump()) == stored


def test_flatten_merges_track_copies_that_differ_only_by_non_conflicting_app_keys():
    """An App save annotated the film-level copy of a track; the chapter shots' copies lack the flag."""
    stored = published(split(flat_film()))
    parent_tracks = stored["parent"]["config"]["tracks"]
    shot_ids = {t["id"] for shot in stored["shots"].values() for t in shot["internal_timeline"]["tracks"]}
    shared = next(t for t in parent_tracks if t["id"] in shot_ids)
    shared["app"] = {"scaleAppliesToPositionedClips": True}
    flat = flatten(stored)
    merged = next(t for t in flat["shots"][next(iter(flat["shots"]))]["internal_timeline"]["tracks"] if t["id"] == shared["id"])
    assert merged["app"] == {"scaleAppliesToPositionedClips": True}
    for shot in stored["shots"].values():
        for t in shot["internal_timeline"]["tracks"]:
            if t["id"] == shared["id"]:
                t["app"] = {"scaleAppliesToPositionedClips": False}
    flat = flatten(stored)  # a conflicting shot copy no longer raises: the film's definition wins
    held = next(t for t in flat["shots"][next(iter(flat["shots"]))]["internal_timeline"]["tracks"] if t["id"] == shared["id"])
    assert held["app"] == {"scaleAppliesToPositionedClips": True}


def test_flatten_takes_the_films_track_definition_when_a_shot_copy_has_another_label():
    stored = published(split(flat_film()))
    film_track = next(t for t in stored["parent"]["config"]["tracks"]
                      if any(t["id"] == x["id"] for shot in stored["shots"].values() for x in shot["internal_timeline"]["tracks"]))
    film_track["label"] = "Film label"
    for shot in stored["shots"].values():
        for t in shot["internal_timeline"]["tracks"]:
            if t["id"] == film_track["id"]:
                t["label"] = "Shot label"
    flat = flatten(stored)
    held = next(t for t in flat["shots"][next(iter(flat["shots"]))]["internal_timeline"]["tracks"] if t["id"] == film_track["id"])
    assert held["label"] == "Film label"


def shared_asset_film(digest: str):
    """A chapter film whose shots carry handle and source on asset R; the film-level copy of R lacks them."""
    stored = published(split(flat_film()))
    stored["parent"]["registry"].setdefault("assets", {})["R"] = {"media_id": "m-r"}
    for shot in stored["shots"].values():
        held = shot["internal_timeline"]["registry"]["assets"]
        if "R" in held:
            held["R"].update(handle=digest, source=digest)
    return stored


def test_flatten_merges_asset_copies_that_differ_only_by_fields_the_film_copy_lacks():
    """An App save dropped handle and source from the film-level copy of an asset; the chapter shots kept them."""
    digest = "sha256:116b6dde"
    stored = shared_asset_film(digest)
    assert stored["parent"]["registry"]["assets"]["R"] == {"media_id": "m-r"}
    flat = flatten(stored)
    merged = flat["shots"][next(iter(flat["shots"]))]["internal_timeline"]["registry"]["assets"]["R"]
    assert merged == {"media_id": "m-r", "handle": digest, "source": digest}
    assert list(merged) == ["media_id", "handle", "source"]  # the held entry first, then the new fields


def test_flatten_still_refuses_an_asset_copy_that_names_other_media():
    stored = shared_asset_film("sha256:116b6dde")
    stored["parent"]["registry"]["assets"]["R"]["media_id"] = "m-other"
    with pytest.raises(TimelineEditError, match="conflicting asset key 'R'"):
        flatten(stored)


def test_flatten_then_split_leaves_every_copy_of_an_asset_equal_to_the_union():
    digest = "sha256:116b6dde"
    union = {"media_id": "m-r", "handle": digest, "source": digest}
    again = split(flatten(shared_asset_film(digest)))
    assert again["parent"]["registry"]["assets"]["R"] == union
    copies = [shot["internal_timeline"]["registry"]["assets"]["R"] for shot in again["shots"].values()
              if "R" in shot["internal_timeline"]["registry"]["assets"]]
    assert copies and all(copy_ == union for copy_ in copies)


def test_duplicate_of_a_chapter_film_keeps_one_distinct_shot_per_chapter(saved_source):
    """A chapter film reads flat but its authenticated copy stores one shot per chapter."""
    from astrid.sdk import timeline_duplicate as td
    from astrid.sdk.timeline.commit import bundle_from_document
    from astrid.sdk.timeline.native_timeline_duplicate import physical_content

    stored = published(split(flat_film()))
    assert len(stored["placements"]) == 3
    client, project, document, bodies = saved_source(stored)
    src = Checkout(flatten(bundle_from_document(document('t'))))
    assert len(src.bundle["shots"]) == 1 and STRUCTURE_KEY in src.bundle
    out = td._duplicate(client, project, "t", slug="dup", name=None, revision_id=None)
    doc = physical_content(bundle_from_document(document('dup')))
    shot_ids = [r["shot_id"] for r in doc["placements"]]
    assert out["shots"] == 3 and len(set(shot_ids)) == 3 and set(shot_ids) == set(doc["shots"])
    assert all(s.startswith("dup-") for s in shot_ids)
    assert [doc["shots"][s]["payload"]["name"] for s in shot_ids] == ["01 ONE", "02 TWO", "03 THREE"]
    assert len({r["occurrence_id"] for r in doc["placements"]}) == 3
    assert [call['op'] for call in bodies[0]['commands']] == ['timeline.duplicate']


def test_duplicate_of_a_chapter_film_keeps_one_distinct_shot_per_chapter_r4_original(saved_source):
    """A chapter film is read flat; duplicating must copy its stored form (a shot per chapter), not the one flat
    shot, or the copy is stored as a single shot and every chapter reads back with the same shot id."""
    from astrid.sdk import timeline_duplicate as td
    from astrid.sdk.timeline.commit import bundle_from_document
    from astrid.sdk.timeline.native_timeline_duplicate import physical_content

    stored = published(split(flat_film()))
    assert len(stored["placements"]) == 3
    client, project, document, _ = saved_source(stored)
    src = Checkout(flatten(bundle_from_document(document('t'))))
    assert len(src.bundle["shots"]) == 1 and STRUCTURE_KEY in src.bundle
    out = td._duplicate(client, project, "t", slug="dup", name=None, revision_id=None)
    doc = physical_content(bundle_from_document(document('dup')))
    shot_ids = [r["shot_id"] for r in doc["placements"]]
    assert out["shots"] == 3 and len(set(shot_ids)) == 3 and set(shot_ids) == set(doc["shots"])
    assert all(s.startswith("dup-") for s in shot_ids)
    assert [doc["shots"][s]["payload"]["name"] for s in shot_ids] == ["01 ONE", "02 TWO", "03 THREE"]
    assert len({r["occurrence_id"] for r in doc["placements"]}) == 3


def test_duplicate_guard_refuses_a_copy_that_collapses_shots_and_keeps_a_repeated_shot_repeated():
    """The copy is proven equal to its source: a shot placed twice stays ONE shot placed twice, and a copy that maps
    distinct shots onto one id (or changes a placement or a shot's clips) is refused."""
    from astrid.sdk.timeline_duplicate import assert_copy_equals_source, copy_content

    stored = published(split(flat_film()))
    stored["shots"] = {r["shot_id"]: stored["shots"][r["shot_id"]] for r in stored["placements"]}
    ids = [r["shot_id"] for r in stored["placements"]]
    again = copy.deepcopy(stored["placements"][1])  # the chapter placed a second time (as the owner's film does)
    again["occurrence_id"] = "occ-again"
    again["placement"] = {"start_ms": 12000}
    stored["placements"].append(again)

    out, _ = copy_content(stored, copy.deepcopy(flat_film()), new_slug="dup")
    new_ids = [r["shot_id"] for r in out["placements"]]
    assert len(new_ids) == 4 and len(set(new_ids)) == 3 and new_ids[1] == new_ids[3]
    assert len(out["shots"]) == 3 and len({r["occurrence_id"] for r in out["placements"]}) == 4
    shot_map = dict(zip(ids, new_ids[:3]))
    assert_copy_equals_source(stored, out, shot_map)

    collapsed = copy.deepcopy(out)
    for row in collapsed["placements"]:
        row["shot_id"] = new_ids[0]
    with pytest.raises(TimelineEditError, match="copied onto"):
        assert_copy_equals_source(stored, collapsed, shot_map)
    with pytest.raises(TimelineEditError, match="onto one shot id"):
        assert_copy_equals_source(stored, out, {ids[0]: "x", ids[1]: "x", ids[2]: "y"})
    with pytest.raises(TimelineEditError, match="placements"):
        assert_copy_equals_source(stored, {**out, "placements": out["placements"][:3]}, shot_map)
    edited = copy.deepcopy(out)
    edited["shots"][new_ids[2]]["internal_timeline"]["clips"].pop()
    with pytest.raises(TimelineEditError, match="clips differ"):
        assert_copy_equals_source(stored, edited, shot_map)


def test_pin_narration_adopts_a_binding_the_document_lost():
    """A shot whose binding exists in the runtime but not in the document (a lost pin) must not wedge publish."""
    import hashlib
    from types import SimpleNamespace

    tl = Checkout(flat_film())
    text = "hello there\n"
    digest = "sha256:" + hashlib.sha256(text.encode()).hexdigest()
    existing = {"binding_id": "b1", "shot_id": "film", "kind": "voiceover_script", "content_hash": digest, "head": 1, "slot": None}
    err = SimpleNamespace(code="conflict", details={"actual_head": 1})
    tl.narration_changes = lambda: {"film": text}
    client = SimpleNamespace(shots=SimpleNamespace(
        set_text_binding=lambda *a, **k: SimpleNamespace(ok=False, error=err),
        list_text_bindings=lambda *a, **k: SimpleNamespace(ok=True, data=([existing], None))))
    assert tl.pin_narration(client, idempotency_key="k") == ["film"]
    assert tl.bundle["shots"]["film"]["payload"]["text_bindings"] == [existing]


def test_pin_narration_does_not_adopt_a_different_text_and_reports_the_conflict():
    from types import SimpleNamespace

    tl = Checkout(flat_film())
    other = {"binding_id": "b1", "shot_id": "film", "kind": "voiceover_script", "content_hash": "sha256:other", "head": 1, "slot": None}
    err = SimpleNamespace(code="conflict", details={"actual_head": 1})
    tl.narration_changes = lambda: {"film": "hello there\n"}
    sets = []
    client = SimpleNamespace(shots=SimpleNamespace(
        set_text_binding=lambda *a, **k: sets.append(k) or SimpleNamespace(ok=False, error=err),
        list_text_bindings=lambda *a, **k: SimpleNamespace(ok=True, data=([other], None))))
    with pytest.raises(TimelineEditError, match="could not register"):
        tl.pin_narration(client, idempotency_key="k")
    assert len(sets) == 1  # never retried at the runtime's head


def test_duplicate_refuses_when_the_wrong_form_of_the_source_is_copied(monkeypatch):
    """Revert experiment as a test: if _duplicate copied the flat shot (ignoring structure_base), the copy stores one
    shot while the source stores three, and the duplicate is refused before anything is published."""
    from types import SimpleNamespace

    from astrid.sdk import timeline_duplicate as td

    src = Checkout(flatten(copy.deepcopy(published(split(flat_film())))))
    empty = flat_film()
    empty["timeline_id"] = "dup"
    dst = Checkout(empty)
    for tl in (src, dst):
        tl._check_core = lambda: SimpleNamespace(valid=True, blocking=[], problems=[])
    opened = {"t": src, "dup": dst}
    monkeypatch.setattr(td.Checkout, "open", classmethod(lambda cls, p, t, client=None, revision_id=None: opened[t]))
    real = td.copy_content
    monkeypatch.setattr(td, "copy_content", lambda content, target, **kw: real(src.bundle, target, **kw))  # the original bug
    published_docs = []
    monkeypatch.setattr(td.Checkout, "publish", lambda self, *a, **k: published_docs.append(1) or {})
    archived = []
    client = SimpleNamespace(timelines=SimpleNamespace(
        create_empty=lambda **kw: SimpleNamespace(ok=True, data={"timeline_id": "dup"}),
        archive=lambda *a: archived.append(a) or SimpleNamespace(ok=True)))
    with pytest.raises(TimelineEditError, match="distinct shots"):
        td._duplicate(client, "p", "t", slug="dup", name=None, revision_id=None)
    assert not published_docs and archived


# ---- chapters are shots ------------------------------------------------------------------

def test_a_split_films_chapters_are_its_shots():
    stored = published(split(flat_film()))
    assert [c["name"] for c in intent.chapters(published(stored))] == ["01 ONE", "02 TWO", "03 THREE"]
    assert [c["from"] for c in intent.chapters(stored)] == ["c1", "c2", "c3"]
    assert intent.film_chapter_labels(stored) == []  # split dropped the label list
    # a stale label list on the stored form (the 04 / 05 / 04 case) is ignored: the shots decide
    stale = copy.deepcopy(stored)
    intent._film_w(stale)["chapters"] = [{"name": "01 ONE", "from": "c1"}, {"name": "02 TWO", "from": "c2"},
                                         {"name": "01 ONE", "from": "c3"}]
    assert [c["name"] for c in intent.chapters(stale)] == ["01 ONE", "02 TWO", "03 THREE"]
    flat = flatten(stale)
    assert [c["name"] for c in intent.chapters(flat)] == ["01 ONE", "02 TWO", "03 THREE"]
    assert [c["name"] for c in Checkout(stale).bundle and intent.chapters(Checkout(stale).bundle)] == ["01 ONE", "02 TWO", "03 THREE"]


def test_a_one_shot_film_still_uses_its_labels():
    flat = flat_film()
    assert [c["name"] for c in intent.chapters(flat)] == ["01 ONE", "02 TWO", "03 THREE"]


def test_restructure_drops_the_label_list_after_cutting():
    flat = flat_film()
    assert intent.film_chapter_labels(flat)
    stored = split(flat)
    assert len(stored["placements"]) == 3
    assert intent.film_chapter_labels(stored) == []
    assert [c["name"] for c in intent.chapters(published(stored))] == ["01 ONE", "02 TWO", "03 THREE"]


def test_split_routes_new_clips_by_placement_and_cuts_wall_crossers_into_shots():
    stored = published(split(flat_film()))
    tl = Checkout(stored)
    doc = tl.bundle
    clips = doc["shots"][doc["placements"][0]["shot_id"]]["internal_timeline"]["clips"]
    clips.append({"id": "added", "clipType": "am-type", "track": "type", "at": 5.2, "hold": 1.0, "params": {"text": "x"}})
    clips.append({"id": "wide", "clipType": "am-sprite", "track": "sprite", "asset": "R", "at": 7.5, "hold": 1.0, "params": {}})
    out = split(doc)
    held = {out["shots"][r["shot_id"]]["payload"]["name"]: {c["id"] for c in out["shots"][r["shot_id"]]["internal_timeline"]["clips"]}
            for r in out["placements"]}
    assert "added" in held["02 TWO"]
    wide_pieces = [c for shot in out["shots"].values() for c in shot["internal_timeline"]["clips"]
                   if c.get("app", {}).get("astrid", {}).get("timeline_restructure_piece", {}).get("id") == "wide"]
    assert len(wide_pieces) == 2
    assert [c["app"]["astrid"]["timeline_restructure_piece"]["index"] for c in wide_pieces] == [0, 1]
    assert wide_pieces[0]["id"] == _stable_id("chapter-piece", "t", "wide", "02 TWO", 4000, 0)
    assert "wide" not in {c["id"] for c in out["parent"]["clips"]}
    assert intent.film_chapter_labels(out) == []


def test_same_asset_follows_the_runtime_identity_fixture():
    import json
    from pathlib import Path

    from astrid.sdk.timeline_restructure import same_asset

    cases = json.loads((Path(__file__).resolve().parents[1] / "fixtures" / "asset-identity.json").read_text())["cases"]
    for case in cases:
        assert same_asset(case["a"], case["b"]) is case["same"], case["name"]
    assert same_asset({"media_id": "m", "duration": 3}, {"media_id": "m", "duration": 4})


def test_duplicate_commit_keeps_a_repeated_shot_shared(saved_source):
    from astrid.sdk.timeline.commit import bundle_from_document
    from astrid.sdk.timeline.native_timeline_duplicate import physical_content
    from astrid.sdk.timeline_duplicate import duplicate_timeline

    source = flat_film()
    intent.set_chapters(source, [])
    internal = source["shots"]["film"]["internal_timeline"]
    internal["clips"] = [next(clip for clip in internal["clips"] if clip["id"] == "late")]
    internal["registry"] = {"assets": {}}
    again = copy.deepcopy(source["placements"][0])
    again.update(occurrence_id="occ-again", placement={"start_ms": 12000})
    source["placements"].append(again)
    client, project, document, bodies = saved_source(source)
    before = document('t')
    duplicate_timeline(project, 't', slug='copy', client=client)
    result = document('copy')
    # Raw canonical edges, then the authenticated mapping; working aliases differ.
    rows = result['parent_revision']['payload']['occurrences']
    assert len(rows) == 2 and rows[0]["shot_id"] == rows[1]["shot_id"]
    assert len({row["occurrence_id"] for row in rows}) == 2
    assert len({row['shot_revision_id'] for row in rows}) == 1
    assert len(result['shot_revisions']) == len(result['internal_timeline_revisions']) == 1
    reopened = bundle_from_document(result)
    assert len({row['shot_id'] for row in reopened['placements']}) == 2
    assert {link['shot_id'] for link in reopened['source_mapping']['shots'].values()} == {rows[0]['shot_id']}
    assert physical_content(reopened)['placements'] == [{k: v for k, v in row.items()
        if k not in ('shot_revision_id', 'revision_id')} for row in rows]
    assert document('t') == before
    assert [call['op'] for call in bodies[0]['commands']] == ['timeline.duplicate']


def test_friendly_duplicate_reports_setup_and_copy_receipt_with_complete_fresh_maps(saved_source, tmp_path):
    from astrid.sdk.timeline import open as open_timeline
    from astrid.sdk.timeline.commit import bundle_from_document

    source = published(split(flat_film()))
    again = copy.deepcopy(source['placements'][0])
    again.update(occurrence_id='again', placement={'start_ms': 12000})
    source['placements'].append(again)
    client, project, document, bodies = saved_source(source)
    original = document('t')
    create_calls = []
    create_empty = client.timelines.create_empty
    client.timelines.create_empty = lambda **kw: create_calls.append(copy.deepcopy(kw)) or create_empty(**kw)

    result = open_timeline(project, 't', client=client, outbox_path=tmp_path / 'source-outbox.json').duplicate(
        as_='friendly-copy')

    copied = document('friendly-copy')
    rows = copied['parent_revision']['payload']['occurrences']
    source_rows = original['parent_revision']['payload']['occurrences']
    assert result['destination_setup']['timeline_id'] == 'friendly-copy'
    assert result['destination_setup']['idempotency_key'] != result['copy_receipt']['idempotency_key']
    if result['destination_setup']['receipt']:
        assert result['copy_receipt']['receipt_id'] != result['destination_setup']['receipt']['receipt_id']
    assert result['copy_receipt']['command_kind'] == 'timeline.commit'
    assert len(create_calls) == 1
    assert len(bodies) == 1 and [command['op'] for command in bodies[0]['commands']] == ['timeline.duplicate']
    assert len(rows) == len(source_rows) == 4
    assert len({row['shot_id'] for row in rows}) == len({row['shot_id'] for row in source_rows}) == 3
    assert {row['shot_id'] for row in rows}.isdisjoint({row['shot_id'] for row in source_rows})
    assert len({row['occurrence_id'] for row in rows}) == 4
    assert {row['occurrence_id'] for row in rows}.isdisjoint({row['occurrence_id'] for row in source_rows})
    assert rows[0]['shot_id'] == rows[-1]['shot_id']
    assert len({row['shot_revision_id'] for row in rows}) == 3
    assert len(copied['shot_revisions']) == len(copied['internal_timeline_revisions']) == 3
    copied_bundle = bundle_from_document(copied)
    source_bundle = bundle_from_document(original)
    from astrid.sdk.timeline.native_timeline_duplicate import physical_content

    source_content = physical_content(source_bundle)
    copied_content = physical_content(copied_bundle)
    shot_map = {source_row['shot_id']: copy_row['shot_id']
                       for source_row, copy_row in zip(source_content['placements'], copied_content['placements'])}
    from astrid.sdk.timeline_duplicate import assert_copy_equals_source

    assert_copy_equals_source(source_content, copied_content, shot_map)
    for source_shot, copy_shot in shot_map.items():
        assert copied_content['shots'][copy_shot]['internal_timeline']['clips'] == source_content['shots'][source_shot]['internal_timeline']['clips']
    assert document('t') == original


def test_friendly_duplicate_adopts_imported_media_on_a_clean_timeline(realm, tmp_path):
    from pathlib import Path
    from types import SimpleNamespace

    from astrid.sdk.contracts import DomainResult
    from astrid.sdk.timeline.commit import bundle_from_document
    from astrid.sdk.timeline.commit_session import Timeline

    service, writer, candidate = realm
    project = candidate['project_id']
    source_id = 'imported-source'
    service.create_timeline(project, source_id, idempotency_key='create-imported-source')
    session = session_for(service, writer, candidate, tmp_path / 'fixture-outbox.json')

    def head_for(project_id, timeline_id):
        return writer.list_timeline_commits(project_id, timeline_id)['head']

    def open_composition(project_id, timeline_id):
        head = head_for(project_id, timeline_id)
        return DomainResult.success({'native_inspection': {'project_id': project_id, 'timeline_id': timeline_id},
                                     'summary': {'head_revision_id': head['revision_id']}})

    def open_bundle(project_id, timeline_id, *, revision_id=None, publication_id=None):
        head = head_for(project_id, timeline_id)
        document = service._timeline_commits()[0].revision_closure(
            project_id, timeline_id, revision_id or head['revision_id'])
        bundle = bundle_from_document(document)
        bundle['base_parent']['publication_id'] = publication_id or head['publication_id']
        return DomainResult.success({'bundle': bundle})

    session.client.timelines.open_composition = open_composition
    session.client.timelines.open_bundle = open_bundle

    def create_empty(**kw):
        service.create_timeline(project, kw['timeline_id'], idempotency_key=kw['idempotency_key'])
        return SimpleNamespace(ok=True, data={'timeline_id': kw['timeline_id']}, receipt=None)

    session.client.timelines.create_empty = create_empty
    source = Timeline.open(project, source_id, client=session.client,
                           outbox_path=tmp_path / 'imported-source-outbox.json')
    image = Path(__file__).resolve().parents[1] / 'fixtures' / 'storyboard-assets' / 'open.png'
    source.commit(lambda timeline: timeline.add(image, at=0, hold=1, id='opening'), label='Import PNG')

    duplicate = source.duplicate(as_='variant-a')
    copied = Timeline.open(project, 'variant-a', client=session.client,
                           outbox_path=tmp_path / 'variant-outbox.json')

    assert duplicate['timeline_id'] == 'variant-a'
    clip = next(clip for clip in copied.clips() if clip.id == 'opening')
    assert clip.asset in copied._registry(clip.shot_id)
    from astrid.core.timeline.parent_registry import effective_parent_registry

    source_assets = effective_parent_registry(source.bundle['parent'])['assets']
    assert copied._registry(clip.shot_id)[clip.asset]['media_id'] == source_assets[clip.asset]['media_id']


def test_duplicate_adopts_parent_asset_aliases_used_by_child_clips():
    from astrid.sdk.timeline_duplicate import copy_content

    source = flat_film()
    asset_id = 'media-imported-png'
    media_id = 'sha256:' + 'a' * 64
    source['parent']['registry'] = {'assets': {asset_id: {'media_id': media_id, 'type': 'image'}}}
    source['shots']['film']['internal_timeline']['clips'].append({
        'id': 'imported-png', 'clipType': 'image', 'track': 'plate', 'asset': asset_id,
        'at': 0, 'from': 0, 'to': 1, 'hold': 1, 'params': {},
    })

    duplicate, _notes = copy_content(source, source, new_slug='copy')
    copied_shot = next(iter(duplicate['shots'].values()))

    assert asset_id not in source['shots']['film']['internal_timeline']['registry']['assets']
    assert copied_shot['internal_timeline']['registry']['assets'][asset_id] == {
        'media_id': media_id, 'type': 'image'}


def test_retime_gives_each_occurrence_of_a_repeated_cut_its_own_timing(saved_source):
    source = published(split(flat_film()))
    first = source['placements'][0]
    again = copy.deepcopy(first)
    again.update(occurrence_id='again', placement={'start_ms': 12000})
    source['placements'].append(again)
    client, project, document, _ = saved_source(source)
    tl = Checkout.open(project, 't', client=client)

    tl.retime()

    rows = {row['occurrence_id']: row for row in tl.bundle['placements']}
    assert rows['again']['placement']['start_ms'] == 12000 and rows['again']['duration_ms'] == 4000
    assert all(row['duration_ms'] == 4000 for row in rows.values())
    assert all(clip.duration > 0 for clip in tl.clips() if not clip.is_audio)

    def timing(occurrence):
        shot = rows[occurrence]['shot_id']
        return sorted((clip.element, clip.start, clip.end) for clip in tl.clips() if clip.shot_id == shot)

    assert timing(first['occurrence_id']) == [('am-snap-plate', 0.0, 4.0), ('am-sprite', 3.5, 4.0)]
    assert timing('again') == [('am-snap-plate', 12.0, 16.0), ('am-sprite', 15.5, 16.0)]


def test_friendly_duplicate_retry_after_lost_ack_reuses_destination_and_copy(saved_source, tmp_path):
    from astrid.sdk.timeline import open as open_timeline
    from astrid.sdk.workspace_client import WorkspaceClientError

    client, project, document, _ = saved_source(published(split(flat_film())))
    original = document('t')
    generated = client._remote._transport._generated
    send = generated.commit_timeline
    create_calls, sent = [], []
    create_empty = client.timelines.create_empty
    client.timelines.create_empty = lambda **kw: create_calls.append(copy.deepcopy(kw)) or create_empty(**kw)
    lost = False

    def lose_ack(p, t, body, *, idempotency_key):
        nonlocal lost
        sent.append((copy.deepcopy(body), idempotency_key))
        result = send(p, t, body, idempotency_key=idempotency_key)
        if not lost:
            lost = True
            raise WorkspaceClientError('lost duplicate acknowledgement')
        return result

    generated.commit_timeline = lose_ack
    source_timeline = open_timeline(project, 't', client=client, outbox_path=tmp_path / 'source-outbox.json')
    with pytest.raises(TimelineEditError, match='outcome is unknown'):
        source_timeline.duplicate(as_='retry-copy')
    result = source_timeline.duplicate(as_='retry-copy')

    assert result['destination_setup']['timeline_id'] == 'retry-copy'
    assert len(create_calls) == 2 and create_calls[0]['idempotency_key'] == create_calls[1]['idempotency_key']
    assert len(sent) == 2 and sent[0] == sent[1]
    history = client.timelines.history(project, 'retry-copy', limit=200).data['items']
    assert len([item for item in history if item['intent'] != 'bootstrap']) == 1
    assert result['copy_receipt']['receipt_id'] == next(
        item['publication_id'] for item in history if item['intent'] != 'bootstrap'
    )
    assert document('t') == original


# Parent coverage retained during the Release-1 + Timeline merge.
def test_split_stores_chapters_as_shots_and_music_and_crossers_on_the_film():
    stored = split(flat_film())
    assert [(r["placement"]["start_ms"], r["duration_ms"]) for r in stored["placements"]] == [(0, 4000), (4000, 4000), (8000, 4000)]
    shots = [stored["shots"][r["shot_id"]] for r in stored["placements"]]
    assert [s["payload"]["name"] for s in shots] == ["01 ONE", "02 TWO", "03 THREE"]
    assert clip_ids(shots[0]) == {"p1"} and clip_ids(shots[1]) == {"p2", "inside", "vo2"} and clip_ids(shots[2]) == {"p3", "late"}
    assert {c["id"] for c in stored["parent"]["clips"]} == {"cross", "bed"}  # crosser and music
    assert "clips" not in stored["parent"]["config"]  # one list only (Runtime 409s on two differing lists)
    assert stored["parent"]["config"]["tracks"][0]["id"] == "picture"
    assert [t["id"] for t in stored["parent"]["config"]["tracks"]] == ["picture", "type", "sprite", "plate", "vo", "music"]
    inside = next(c for c in shots[1]["internal_timeline"]["clips"] if c["id"] == "inside")
    assert inside["at"] == pytest.approx(1.0)  # chapter-relative
    assert set(stored["parent"]["registry"]["assets"]) == {"R", "Q"}  # film clips' assets (+ none unused)
    assert set(shots[1]["internal_timeline"]["registry"]["assets"]) == {"P", "R", "U"}
    assert STRUCTURE_KEY not in stored

def test_restructure_refuses_what_would_change_the_render():
    flat = flat_film()
    good = split(flat)
    assert_equivalent(flat, good)
    bad = copy.deepcopy(good)
    next(c for c in bad["parent"]["clips"] if c["id"] == "bed")["volume"] = 0.25
    with pytest.raises(TimelineEditError, match="refused"):
        assert_equivalent(flat, bad)
    short = copy.deepcopy(good)
    short["parent"]["config"]["tracks"] = [t for t in short["parent"]["config"]["tracks"] if t["id"] != "picture"]
    assert any("track order" in line for line in differences(flat, short))
    moved = copy.deepcopy(good)
    next(c for c in moved["parent"]["clips"] if c["id"] == "cross")["at"] = 3.6
    assert differences(flat, moved)

def test_split_routes_a_new_clip_by_placement_and_a_wall_crosser_to_the_film():
    stored = published(split(flat_film()))
    tl = Checkout(stored)
    doc = tl.bundle
    clips = doc["shots"][doc["placements"][0]["shot_id"]]["internal_timeline"]["clips"]
    clips.append({"id": "added", "clipType": "am-type", "track": "type", "at": 5.2, "hold": 1.0, "params": {"text": "x"}})
    clips.append({"id": "wide", "clipType": "am-sprite", "track": "sprite", "asset": "R", "at": 7.5, "hold": 1.0, "params": {}})
    out = split(doc)
    held = {out["shots"][r["shot_id"]]["payload"]["name"]: {c["id"] for c in out["shots"][r["shot_id"]]["internal_timeline"]["clips"]}
            for r in out["placements"]}
    assert "added" in held["02 TWO"]
    assert "wide" not in set().union(*held.values())
    assert "wide" in {c["id"] for c in out["parent"]["clips"]}
    assert intent.film_chapter_labels(out) == []
