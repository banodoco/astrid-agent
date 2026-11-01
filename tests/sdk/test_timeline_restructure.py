"""Chapters stored as shots, edited as one flat timeline (timeline_restructure)."""
from __future__ import annotations

import copy

import pytest

from astrid.sdk import timeline_intent as intent
from astrid.sdk.timeline._common import TimelineEditError
from astrid.sdk.timeline.store import three_way
from astrid.sdk.timeline_checkout import Checkout
from astrid.sdk.timeline_restructure import STRUCTURE_KEY, _stable_id, assert_equivalent, differences, flatten, is_split, plan, split, walls_of

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


def test_three_way_merge_works_on_the_stored_form():
    base = published(split(flat_film()))
    mine = Checkout(copy.deepcopy(base))
    mine.clip("late").data["hold"] = 1.5
    mine.clip("bed").data["volume"] = 0.9
    candidate = mine.stored_document()
    head = copy.deepcopy(base)
    first = head["shots"][head["placements"][0]["shot_id"]]
    first["internal_timeline"]["clips"][0]["hold"] = 3.9  # someone else touched a different clip in another chapter
    merged, conflicts = three_way(base, head, candidate)
    assert conflicts == []
    by_id = {c["id"]: c for s in merged["shots"].values() for c in s["internal_timeline"]["clips"]}
    assert by_id["late"]["hold"] == 1.5 and by_id["p1"]["hold"] == 3.9
    assert next(c for c in merged["parent"]["clips"] if c["id"] == "bed")["volume"] == 0.9
    theirs = copy.deepcopy(base)
    next(c for c in theirs["parent"]["clips"] if c["id"] == "bed")["volume"] = 0.1
    _, conflicts = three_way(base, theirs, candidate)
    assert any("bed" in line for line in conflicts)  # the same film-level clip changed on both sides refuses


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


def test_duplicate_of_a_chapter_film_keeps_one_distinct_shot_per_chapter(monkeypatch):
    """A chapter film is read flat; duplicating must copy its stored form (a shot per chapter), not the one flat
    shot, or the copy is stored as a single shot and every chapter reads back with the same shot id."""
    from types import SimpleNamespace

    from astrid.sdk import timeline_duplicate as td

    stored = published(split(flat_film()))
    assert len(stored["placements"]) == 3
    src = Checkout(flatten(copy.deepcopy(stored)))
    assert len(src.bundle["shots"]) == 1 and STRUCTURE_KEY in src.bundle
    empty = flat_film()
    empty["timeline_id"] = "dup"
    dst = Checkout(empty)
    for tl in (src, dst):  # the toy film has no real media identities; validity is not what this test is about
        tl.check = lambda: SimpleNamespace(valid=True, blocking=[], problems=[])
    opened = {"t": src, "dup": dst}
    monkeypatch.setattr(td.Checkout, "open", classmethod(lambda cls, p, t, client=None, revision_id=None: opened[t]))
    stored_docs = []
    monkeypatch.setattr(td.Checkout, "publish", lambda self, *a, **k: stored_docs.append(self.stored_document()) or {})
    client = SimpleNamespace(timelines=SimpleNamespace(
        create_empty=lambda **kw: SimpleNamespace(ok=True, data={"timeline_id": "dup"}),
        archive=lambda *a: SimpleNamespace(ok=True)))
    out = td._duplicate(client, "p", "t", slug="dup", name=None, revision_id=None)
    doc = stored_docs[0]
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
        tl.check = lambda: SimpleNamespace(valid=True, blocking=[], problems=[])
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


def test_same_asset_follows_the_runtime_identity_fixture():
    import json
    from pathlib import Path
    from astrid.sdk.timeline_restructure import same_asset

    cases = json.loads((Path(__file__).resolve().parents[1] / "fixtures" / "asset-identity.json").read_text())["cases"]
    for case in cases:
        assert same_asset(case["a"], case["b"]) is case["same"], case["name"]
    assert same_asset({"media_id": "m", "duration": 3}, {"media_id": "m", "duration": 4})
