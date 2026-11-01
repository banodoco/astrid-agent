"""The timeline editing API: timeline seconds everywhere, anchors that keep intent, ripple that keeps sync."""
from __future__ import annotations

import copy
from pathlib import Path
import json

import pytest

from astrid.sdk import timeline_intent as intent
from astrid.sdk.timeline_checkout import Checkout, TimelineEditError, describe_changes, three_way

FPS = 30


def _shot(clips, tracks=None):
    return {
        "payload": {"name": "SHOT"},
        "internal_timeline": {
            "tracks": tracks or [
                {"id": "type", "kind": "visual"}, {"id": "sprite", "kind": "visual"}, {"id": "plate", "kind": "visual"},
                {"id": "vo", "kind": "audio"}, {"id": "music", "kind": "audio"},
            ],
            "clips": clips,
            "registry": {"assets": {"P": {"media_id": "m-p"}, "R": {"media_id": "m-r"}, "Q": {"media_id": "m-q"}}},
        },
    }


def bundle():
    """Two shots: A (0–4 s) and B (4–8 s). The VO says "it went viral" in A and "live now" in B."""
    a = [
        {"id": "a-plate", "clipType": "am-snap-plate", "track": "plate", "asset": "P", "at": 0.0, "hold": 4.0, "params": {}},
        {"id": "a-rocket", "clipType": "am-sprite", "track": "sprite", "asset": "R", "at": 1.0, "hold": 1.0, "params": {"x": 10}},
        {"id": "a-vo", "clipType": "media", "track": "vo", "asset": "P", "at": 0.5, "from": 0.0, "to": 2.0,
         "app": {"segment": "s1", "words": [[0.1, 0.3, "it"], [0.4, 0.6, "went"], [0.8, 1.2, "viral"]]}},
        {"id": "a-music", "clipType": "media", "track": "music", "asset": "Q", "at": 0.0, "from": 0.0, "to": 4.0},
    ]
    b = [
        {"id": "b-plate", "clipType": "am-snap-plate", "track": "plate", "asset": "Q", "at": 0.0, "hold": 4.0, "params": {}},
        {"id": "b-type", "clipType": "am-type", "track": "type", "at": 2.0, "hold": 1.0, "params": {"text": "Live."}},
        {"id": "b-vo", "clipType": "media", "track": "vo", "asset": "P", "at": 1.9, "from": 0.0, "to": 1.0,
         "app": {"segment": "s2", "words": [[0.1, 0.4, "Live"], [0.5, 0.8, "now"]]}},
    ]
    return {
        "project_id": "p", "timeline_id": "t",
        "base_parent": {"revision_id": "rev-0"},
        "parent": {"config": {"theme_overrides": {"visual": {"canvas": {"fps": FPS, "width": 1920, "height": 1080}}}}, "clips": []},
        "placements": [
            {"shot_id": "A", "occurrence_id": "occ-A", "placement": {"start_ms": 0}, "duration_ms": 4000},
            {"shot_id": "B", "occurrence_id": "occ-B", "placement": {"start_ms": 4000}, "duration_ms": 4000},
        ],
        "shots": {"A": _shot(a), "B": _shot(b)},
    }


def test_words_are_in_timeline_seconds_with_stable_ids():
    tl = Checkout(bundle())
    viral = tl.word("viral")
    assert (viral.id, viral.start, viral.end) == ("s1:2", pytest.approx(1.3), pytest.approx(1.7))
    live = tl.word("Live")
    assert live.start == pytest.approx(4.0 + 1.9 + 0.1)  # shot B starts at 4 s
    assert tl.word("s2:1").text == "now"
    assert tl.word("went viral").id == "s1:1"  # phrases match their first word


def test_first_draft_pins_the_fetched_bundle_for_edit_diffs(tmp_path, monkeypatch):
    from astrid.sdk.timeline import store

    monkeypatch.setattr(Checkout, "SAY_SAVED", False)
    monkeypatch.setattr(store, "resolve_ids", lambda *args, **kwargs: ("p", "t", "rev-0"))
    monkeypatch.setattr(store, "current_draft", lambda *args, **kwargs: "main")
    monkeypatch.setattr(store, "draft_path", lambda *args, **kwargs: tmp_path / "main.json")
    monkeypatch.setattr(store, "fetch_bundle", lambda *args, **kwargs: bundle())
    monkeypatch.setattr(store, "set_current_draft", lambda *args, **kwargs: None)

    tl = Checkout.draft("p", "t")
    tl.clip("R").nudge(0.5)

    fetched = bundle()
    intent.migrate(fetched)
    assert tl.bundle["base_parent_payload"] == fetched["parent"]
    assert tl.bundle["base_placements"] == {row["occurrence_id"]: row for row in bundle()["placements"]}
    assert all("base_internal_timeline" in shot for shot in tl.bundle["shots"].values())
    assert [(change["clip_id"], change["fields"]) for change in tl.edits()["changes"]] == [
        ("a-rocket", ["start", "end"])
    ]


def test_ambiguous_and_missing_words_say_what_to_do():
    data = bundle()
    data["shots"]["B"]["internal_timeline"]["clips"][2]["app"]["words"].append([0.9, 1.0, "viral"])
    tl = Checkout(data)
    with pytest.raises(TimelineEditError, match="spoken 2 times"):
        tl.word("viral")
    assert tl.word("viral", n=2).segment == "s2"
    assert tl.word("viral", after=3).segment == "s2"
    with pytest.raises(TimelineEditError, match="did you mean"):
        tl.word("virl")


def test_clip_lookup_by_asset_id_prefix_text_and_near():
    tl = Checkout(bundle())
    assert tl.clip("R").id == "a-rocket"
    assert tl.clip("a-rock").id == "a-rocket"
    assert tl.clip("Live.").id == "b-type"
    with pytest.raises(TimelineEditError, match="matches 3 clips"):
        tl.clip("P")  # a-plate and a-vo share the asset key
    assert tl.clip("P", near=0.1).id == "a-plate"


def test_assets_list_registry_media_and_every_clip_address():
    data = bundle()
    data["shots"]["A"]["internal_timeline"]["registry"]["assets"]["R"] = {
        "media_id": "sha-old", "content_sha256": "sha-old", "type": "image/png", "size_bytes": 12,
        "source": "run:old/sprites#0",
    }
    data["shots"]["B"]["internal_timeline"]["registry"]["assets"]["R"] = copy.deepcopy(
        data["shots"]["A"]["internal_timeline"]["registry"]["assets"]["R"]
    )
    data["shots"]["B"]["internal_timeline"]["clips"].append(
        {"id": "b-tool", "clipType": "am-sprite", "track": "sprite", "asset": "R", "at": 0.5, "hold": 0.5, "params": {}}
    )
    row = Checkout(data).assets("R")[0]
    assert row["media"]["content_sha256"] == "sha-old"
    assert row["media"]["source"] == "run:old/sprites#0"
    assert row["clips"] == ["a-rocket", "b-tool"]


def test_replace_asset_repoints_all_clips_and_undo_restores_registry(monkeypatch, tmp_path):
    from astrid.sdk.timeline import edits
    from astrid.sdk.timeline import store

    data = bundle()
    old = {"media_id": "sha-old", "content_sha256": "sha-old", "type": "image/png"}
    data["shots"]["A"]["internal_timeline"]["registry"]["assets"]["R"] = copy.deepcopy(old)
    data["shots"]["B"]["internal_timeline"]["registry"]["assets"]["R"] = copy.deepcopy(old)
    data["shots"]["B"]["internal_timeline"]["clips"].append(
        {"id": "b-tool", "clipType": "am-sprite", "track": "sprite", "asset": "R", "at": 0.5, "hold": 0.5, "params": {}}
    )
    monkeypatch.setattr(edits._store, "resolve_handle_entry", lambda *_: {
        "key": "new-art", "media_id": "sha-new", "content_sha256": "sha-new", "type": "image/webp", "size_bytes": 99,
    })
    root = tmp_path / "data"
    monkeypatch.setenv("ASTRID_RUNTIME_DATA_ROOT", str(root))
    tl = Checkout(data)
    path = store.drafts_root() / "p" / "t" / "main.json"
    tl.save(path, quiet=True)
    with tl.step("replace asset R"):
        changed = tl.replace_asset("R", "run:new/sprites#0")
    tl.save(quiet=True)
    assert changed == ["a-rocket", "b-tool"]
    assert {clip.asset for clip in tl.clips() if clip.asset == "R"} == {"R"}
    assert tl._registry("A")["R"]["media_id"] == "sha-new"
    tl.undo()
    assert tl._registry("A")["R"] == old
    assert tl._registry("B")["R"] == old


def test_replace_asset_refuses_cross_kind_media_without_changing_anything(monkeypatch):
    from astrid.sdk.timeline import edits

    data = bundle()
    data["shots"]["A"]["internal_timeline"]["registry"]["assets"]["R"].update(type="image/png")
    tl = Checkout(data)
    before = copy.deepcopy(tl.bundle)
    monkeypatch.setattr(edits._store, "resolve_handle_entry", lambda *_: {
        "key": "new-audio", "media_id": "sha-audio", "content_sha256": "sha-audio", "type": "audio/wav",
    })
    with pytest.raises(TimelineEditError, match="image; replacement media is audio"):
        tl.replace_asset("R", "run:new/audio#0")
    assert tl.bundle == before


def test_enter_at_a_word_moves_in_timeline_seconds_and_stores_the_moment():
    tl = Checkout(bundle())
    rocket = tl.clip("R").enter_at("viral")
    assert rocket.start == pytest.approx(1.3)
    assert rocket.data["at"] == pytest.approx(1.3)  # shot A starts at 0
    assert rocket.duration == pytest.approx(1.0)
    assert rocket.anchor == '"viral"'  # stored as a moment; the seconds are only the cache
    plate = tl.clip("b-type").enter_at(5.0)  # a plain time: shot-relative at = 1.0 in shot B
    assert plate.data["at"] == pytest.approx(1.0)
    assert plate.anchor is None


def test_nudge_frames_keeps_the_anchor_offset():
    tl = Checkout(bundle())
    rocket = tl.clip("R").enter_at("viral").nudge(frames=3)
    assert rocket.start == pytest.approx(1.4)
    assert rocket.anchor == '"viral" +3f'  # a nudge becomes an offset, never a fixed number
    rocket.extend(0.5)
    assert rocket.duration == pytest.approx(1.5)


def test_ripple_delete_across_a_shot_boundary_keeps_audio_in_sync():
    tl = Checkout(bundle())
    before = {c.id: c.start for c in tl.clips()}
    removed = tl.ripple_delete(3.5, 4.5)  # 0.5 s of shot A and 0.5 s of shot B
    assert removed == pytest.approx(1.0)
    assert tl.duration == pytest.approx(7.0)
    assert tl.clip("b-vo").start == pytest.approx(before["b-vo"] - 1.0)
    assert tl.word("Live").start == pytest.approx(6.0 - 1.0)
    assert tl.clip("b-type").start == pytest.approx(before["b-type"] - 1.0)
    music = [c for c in tl.clips() if c.track == "music"]
    # the default is keep_end (it replaced the old split at the cut, which gave 3.5 s): the bed is cut at the removal
    # and fades out over it, a 0.25 s crossfade centred on the cut, since its tail was in the removed span
    assert [round(m.duration, 3) for m in music] == [3.625]


def test_close_gap_before_a_word_ripples_everything_after():
    tl = Checkout(bundle())
    gap_start = tl.word("viral").end  # 1.7 s; "Live" starts at 6.0 s
    later = tl.clip("b-type").start
    removed = tl.close_gap(before="Live")
    assert removed == pytest.approx(6.0 - gap_start)
    assert tl.word("Live").start == pytest.approx(gap_start)
    assert tl.clip("b-type").start == pytest.approx(later - removed)


def test_voice_replace_moves_anchored_clips_with_their_words():
    tl = Checkout(bundle())
    tl.clip("b-type").enter_at("now")
    old_now = tl.word("now").start
    moved = tl.voice("s1").replace("Q", words=[[0.1, 0.5, "it"], [0.6, 1.0, "went"], [1.4, 2.4, "viral"]])
    assert tl.word("viral").start == pytest.approx(0.5 + 1.4)
    assert tl.word("now").start > old_now  # the longer take rippled the next line later
    assert tl.clip("b-type").start == pytest.approx(tl.word("now").start, abs=1 / FPS)  # its anchor followed (on a frame)
    assert any(cid == "b-type" for cid, _a, _b in moved)



def test_a_take_that_says_a_word_again_keeps_each_moment_on_the_word_it_meant():
    data = bundle()
    data["shots"]["A"]["internal_timeline"]["clips"][2]["app"]["words"] = [[0.1, 0.3, "and"], [0.4, 0.6, "share"], [0.8, 1.2, "it"]]
    tl = Checkout(data)
    tl.clip("a-rocket").on('"and" in s1 #1')  # "and share"
    tl.clip("b-type").on('"share"')  # the only "share": stays as it is
    tl.voice("s1").replace("Q", words=[[0.1, 0.5, "front-end"], [0.6, 0.7, "and"], [0.8, 1.2, "back-end,"],
                                       [1.3, 1.4, "and"], [1.5, 1.8, "share"], [1.9, 2.1, "it"]])
    assert intent.on(tl.clip("a-rocket").data) == '"and" in s1 #2'
    assert tl.clip("a-rocket").start == pytest.approx(0.5 + 1.3, abs=1 / FPS)
    assert intent.on(tl.clip("b-type").data) == '"share"'
    assert any("a-rocket" in note and "#2" in note for note in tl.notes)


def test_setting_a_param_to_null_takes_it_off_so_the_element_default_applies():
    tl = Checkout(bundle())
    tl.clip("b-type").set(italicWord="Live")
    tl.clip("b-type").set(italicWord=None)  # `--set italicWord=null`: a stored null would fail am-type's schema at render
    assert "italicWord" not in tl.clip("b-type").params

def test_formulas_resolve_like_a_spreadsheet():
    data = bundle()
    data["parent"]["config"]["slots"] = {"HAND": {"hand_mark": {"x": 1200, "y": 600}}}
    data["shots"]["A"]["internal_timeline"]["clips"][1]["app"] = {"formulas": {
        "params.x": {"mark": "HAND", "axis": "x", "offset": -10},
        "params.words": {"words_of": "s1"},
        "params.keyframes[1].frame": {"word": "s1:2", "text": "viral", "as": "clip_frame"},
    }}
    tl = Checkout(data)
    tl.resolve()
    rocket = tl.clip("R")
    assert rocket.params["x"] == 190
    # only the words that overlap the clip (1.0–2.0 s): "went" (0.9–1.1) and "viral" (1.3–1.7), clip-relative
    assert rocket.params["words"] == [[pytest.approx(-0.1), pytest.approx(0.1)], [pytest.approx(0.3), pytest.approx(0.7)]]
    assert rocket.params["keyframes"][1]["frame"] == 9  # viral at 1.3 s, the clip starts at 1.0 s


def test_save_load_round_trip_shows_both_clocks_and_honours_timeline_edits(tmp_path):
    tl = Checkout(bundle())
    path = tl.save(tmp_path / "t.json")
    raw = json.loads(path.read_text())
    clip = raw["shots"]["B"]["internal_timeline"]["clips"][1]
    assert clip["_timeline"] == {"start": 6.0, "end": 7.0} and clip["at"] == 2.0
    assert raw["_index"]["words"][0] == ["s1:0", "it", 0.6, 0.8]
    clip["_timeline"]["start"] = 6.5  # edit in timeline seconds
    path.write_text(json.dumps(raw))
    again = Checkout.load(path)
    assert again.clip("b-type").data["at"] == pytest.approx(2.5)
    assert again.clip("b-type").duration == pytest.approx(1.0)
    assert "_index" not in again.document() and '"_timeline"' not in json.dumps(again.document())


def test_three_way_merges_disjoint_edits_and_refuses_overlapping_ones():
    base = bundle()
    ours, theirs = copy.deepcopy(base), copy.deepcopy(base)
    Checkout(ours).clip("R").nudge(0.5)
    Checkout(theirs).clip("b-type").set(text="LIVE!")
    merged, conflicts = three_way(base, theirs, ours)
    assert not conflicts
    m = Checkout(merged)
    assert m.clip("R").start == pytest.approx(1.5) and m.clip("b-type").params["text"] == "LIVE!"
    Checkout(theirs).clip("R").nudge(-0.5)
    _merged, conflicts = three_way(base, theirs, ours)
    assert conflicts and "a-rocket" in conflicts[0]


def test_add_overlay_on_a_word_in_a_corner():
    tl = Checkout(bundle())
    sparkle = tl.add("am-type", at="Live", hold=0.5, params={"text": "*", "width": 200}, corner="top-right")
    assert sparkle.start == pytest.approx(tl.word("Live").start)
    assert sparkle.shot_id == "B" and sparkle.params["x"] == 1920 - 96 - 200 and sparkle.params["y"] == 54
    assert sparkle.anchor == '"Live"'



def test_add_takes_canvas_px_like_set():
    """tl.add(params={x, y}) stored canvas px raw, so a sprite (stored on its 320x180 grid) landed at 6x the
    position, off screen (the film's c26 wrench at x=4212). add and set now share one path."""
    tl = Checkout(bundle())
    added = tl.add("am-sprite", at="Live", hold=0.5, params={"x": 1290, "y": 600, "scale": 5})
    assert added.get("x") == 1290 and added.get("y") == 600
    assert added.params["x"] == tl.clip("R").set(x=1290).params["x"]

# ---- re-flow: the voice track lays out the film ------------------------------------------

def _with_beats(data):
    music = data["shots"]["A"]["internal_timeline"]["clips"][3]
    music["app"] = {"beats": {"beats": [i * 0.5 for i in range(8)], "bpm": 120, "time": "cue_seconds"}}
    return data


def test_a_longer_take_reflows_the_film_and_keeps_the_gap_overlays_and_music():
    tl = Checkout(_with_beats(bundle()))
    tl.music = "seams"  # the old internal name of keep_end (accepted): cut the bed on a beat, with a crossfade
    tl.clip("b-type").enter_at("now")
    gap = tl.gaps()["s1"]  # "viral" ends 1.7 → the next take begins 5.9
    assert gap == pytest.approx(4.2)
    moved = tl.voice("s1").replace("Q", words=[[0.1, 0.5, "it"], [0.6, 1.0, "went"], [1.4, 2.4, "viral"]])
    assert tl.word("viral").end == pytest.approx(2.9)
    assert tl.voice("s2").clips[0].start - tl.word("viral").end == pytest.approx(gap, abs=1 / FPS)  # the silence is kept
    assert tl.clip("b-type").start == pytest.approx(tl.word("now").start, abs=1 / FPS)  # the overlay is still on its word
    assert [m[0] for m in moved] == ["b-type"]
    music = sorted((c for c in tl.clips() if c.track == "music"), key=lambda c: c.start)
    # the seam is on the beat before the change (1.5 s); the crossfade starts half its length earlier. Was
    # [0.0, 1.5] and from == 0.3 before keep_end's crossfade (a 0.25 s overlap, so the bed never has a hole)
    assert [round(m.start, 3) for m in music] == [0.0, 1.375]
    assert music[1].data["from"] == pytest.approx(0.175)  # repeats 1.2 s (less the crossfade): in sync after it
    assert any("repeats 1.20 s" in line and "beats" in line for line in tl.report)


def test_a_declared_gap_is_honoured():
    tl = Checkout(bundle())
    tl.voice("s1").set_gap_after(0.5)  # the next take begins 0.5 s after "viral" ends ("Live" is 0.1 s into it)
    assert tl.word("Live").start == pytest.approx(tl.word("viral").end + 0.5 + 0.1, abs=1 / FPS)
    assert tl.voice("s1").gap_after == 0.5
    tl.voice("s1").replace("Q", words=[[0.1, 0.3, "it"], [0.4, 0.6, "went"], [0.8, 1.0, "viral"]])  # shorter
    assert tl.word("Live").start == pytest.approx(tl.word("viral").end + 0.5 + 0.1, abs=1 / FPS)


def test_insert_and_remove_a_line():
    tl = Checkout(bundle())
    tl.clip("b-type").enter_at("now")
    gap = tl.gaps()["s1"]
    report = tl.insert_line("s1b", "Q", words=[[0.0, 0.4, "and"], [0.5, 1.0, "then"]], after="s1", gap_after=0.3)
    assert report[0].startswith("inserted line s1b")
    assert [v.segment for v in tl.lines()] == ["s1", "s1b", "s2"]
    assert tl.voice("s1b").clips[0].start == pytest.approx(tl.word("viral").end + gap, abs=1 / FPS)
    assert tl.voice("s2").clips[0].start == pytest.approx(tl.word("then").end + 0.3, abs=1 / FPS)
    assert tl.clip("b-type").start == pytest.approx(tl.word("now").start, abs=1 / FPS)
    tl.remove_line("s1b")
    assert [v.segment for v in tl.lines()] == ["s1", "s2"]
    assert tl.voice("s2").clips[0].start == pytest.approx(tl.word("viral").end + gap, abs=1 / FPS)
    assert tl.clip("b-type").start == pytest.approx(tl.word("now").start, abs=1 / FPS)


def test_formula_post_steps_cover_the_generator_maths():
    data = bundle()
    data["shots"]["A"]["internal_timeline"]["clips"][1]["app"] = {"formulas": {
        # the LIFETIME flip: 50 frames before "viral", never below 0
        "params.flip": {"word": "s1:2", "as": "clip_frame", "offset_frames": -50, "min": 0},
        # a hop keyframe on an even frame (stepFrames 2), at least frame 4, then +4
        "params.keyframes[0].frame": {"word": "s1:2", "as": "clip_frame", "min": 4, "step": 2},
        "params.keyframes[1].frame": {"word": "s1:2", "as": "clip_frame", "offset_frames": 4, "min": 8, "step": 2},
        # held until "Live", floored to a whole frame
        "hold": {"word": "s2:0", "as": "clip_seconds", "snap": "floor"},
    }}
    tl = Checkout(data)
    tl.resolve()
    rocket = tl.clip("R")  # starts at 1.0; "viral" at 1.3 (frame 9); "Live" at 6.0
    assert rocket.params["flip"] == 0
    assert rocket.params["keyframes"][0]["frame"] == 8 and rocket.params["keyframes"][1]["frame"] == 12
    assert rocket.duration == pytest.approx(5.0)


def test_an_anchored_cut_rolls_so_the_picture_track_has_no_hole():
    data = bundle()
    clips = data["shots"]["A"]["internal_timeline"]["clips"]
    clips[0]["hold"] = 2.0
    clips.insert(1, {"id": "a-plate2", "clipType": "am-snap-plate", "track": "plate", "asset": "Q", "at": 2.0, "hold": 2.0, "params": {}})
    clips[0]["app"] = {"cut": "c1"}
    clips[1]["app"] = {"cut": "c2", "on": '"viral"'}
    tl = Checkout(data)
    tl.retime()  # "viral" is at 1.3: the cut rolls back from 2.0 to 1.3
    assert tl.clip("a-plate2").start == pytest.approx(1.3) and tl.clip("a-plate2").end == pytest.approx(4.0)
    assert tl.clip("a-plate").end == pytest.approx(1.3)


def test_a_tightened_line_keeps_its_inner_silence():
    data = bundle()
    vo = data["shots"]["A"]["internal_timeline"]["clips"][2]
    first = copy.deepcopy(vo)
    first.update({"id": "a-vo-0", "to": 0.7})
    first["app"] = {"segment": "s1", "words": [[0.1, 0.3, "it"], [0.4, 0.6, "went"]], "gap_after_s": 0.5}
    vo.update({"id": "a-vo-1", "at": 1.3, "from": 0.7, "to": 2.0})
    vo["app"] = {"segment": "s1", "words": [[0.1, 0.5, "viral"]]}
    data["shots"]["A"]["internal_timeline"]["clips"].insert(2, first)
    tl = Checkout(data)
    assert [w.text for w in tl.voice("s1").words] == ["it", "went", "viral"]
    report = tl.reflow()  # "went" ends at 1.1; "viral" starts at 1.4: the declared 0.5 s moves it (and all after) to 1.6
    assert tl.word("viral").start == pytest.approx(1.6, abs=1 / FPS)
    assert tl.word("Live").start == pytest.approx(6.2, abs=1 / FPS)
    assert report and report[0].startswith("s1 ('viral')")


def test_a_sequence_fit_races_through_its_steps_and_lands_on_the_word():
    data = bundle()
    clips = data["shots"]["A"]["internal_timeline"]["clips"]
    plate = clips.pop(0)
    for i in range(3):
        step = copy.deepcopy(plate)
        step.update({"id": f"seq-{i}", "at": round(i * 4 / 3, 6), "hold": round(4 / 3, 6), "asset": "P" if i % 2 else "Q"})
        step["app"] = {"sequence": "seq", "sequence_index": i}
        clips.append(step)
    clips[-3]["app"]["sequence_fit"] = {"land": {"word": "s1:2", "text": "viral"}, "lead": [12], "race": [6, 4, 2],
                                        "cycle": ["P", "Q"], "then": "R"}
    tl = Checkout(data)
    changes = tl.resolve()  # "viral" at 1.3 s = frame 39: 12 lead, then 6+4+2+2+2+2+2+2+2+2+2 = 27, then R to frame 120
    steps = sorted((c for c in tl.clips() if c.id.startswith("seq-")), key=lambda c: c.start)
    assert any("lands on 'viral' at frame 39" in line for line in changes)
    assert [round(c.duration * FPS) for c in steps][:4] == [12, 6, 4, 2]
    landing = next(c for c in steps if c.asset == "R")
    assert landing.start == pytest.approx(1.3) and landing.end == pytest.approx(4.0)
    assert len(tl.cuts) == 2  # still one cut per shot: the steps are one sequence
    assert tl.resolve() == []  # resolving again changes nothing


def test_canvas_pixels_convert_to_the_elements_unit():
    tl = Checkout(bundle())
    assert tl.clip("R").set(x="1290px", y="-12px").params["x"] == 215  # am-sprite: logical px
    assert tl.clip("b-type").set(x="144px").params["x"] == 144


def test_narration_comes_from_the_lines_and_is_re_pinned_only_when_it_changes():
    import hashlib

    data = bundle()
    data["shots"]["A"]["internal_timeline"]["clips"][2]["app"]["text"] = "It went viral."
    data["shots"]["A"]["payload"]["text_bindings"] = [{"kind": "voiceover_script", "head": 3,
                                                       "content_hash": "sha256:" + hashlib.sha256(b"It went viral.\n").hexdigest()}]
    tl = Checkout(data)
    assert tl.narration() == {"A": "It went viral.\n"}  # shot B's line declares no text: not managed
    assert tl.narration_changes() == {}
    tl.voice("s1").replace("Q", words=[[0.1, 0.5, "it"], [0.6, 1.0, "really"], [1.1, 1.4, "went"], [1.5, 2.0, "viral"]],
                           text="It really went viral.")

    class Shots:
        calls = []

        def set_text_binding(self, project, **kw):
            self.calls.append(kw)
            return {"kind": "voiceover_script", "head": kw["expected_head"] + 1, "content_hash": "new"}

    class Client:
        shots = Shots()

    assert tl.pin_narration(Client(), idempotency_key="k") == ["A"]
    call = Shots.calls[0]
    assert call["text"] == "It really went viral.\n" and call["expected_head"] == 3
    assert tl.bundle["shots"]["A"]["payload"]["text_bindings"][0]["head"] == 4


def test_narration_cas_conflict_is_not_retried_at_actual_head():
    import hashlib

    from astrid.sdk.contracts import DomainResult, ErrorObject

    data = bundle()
    data["shots"]["A"]["internal_timeline"]["clips"][2]["app"]["text"] = "It went viral."
    data["shots"]["A"]["payload"]["text_bindings"] = [{
        "kind": "voiceover_script",
        "head": 3,
        "content_hash": "sha256:" + hashlib.sha256(b"It went viral.\n").hexdigest(),
    }]
    tl = Checkout(data)
    tl.voice("s1").replace(
        "Q",
        words=[[0.1, 0.5, "it"], [0.6, 1.0, "really"], [1.1, 1.4, "went"], [1.5, 2.0, "viral"]],
        text="It really went viral.",
    )

    class Shots:
        def __init__(self):
            self.calls = []

        def set_text_binding(self, project, **kwargs):
            self.calls.append((project, kwargs))
            return DomainResult.failure(
                ErrorObject("conflict", "text binding head moved", {"actual_head": 4}),
                idempotency_key=kwargs["idempotency_key"],
            )

        def list_text_bindings(self, project, **kwargs):
            return DomainResult.success(([], None))

    class Client:
        def __init__(self):
            self.shots = Shots()

    client = Client()
    with pytest.raises(TimelineEditError, match="text binding head moved"):
        tl.pin_narration(client, idempotency_key="k")

    assert len(client.shots.calls) == 1
    project, call = client.shots.calls[0]
    assert project == "p"
    assert call["expected_head"] == 3
    assert call["idempotency_key"] == "k-narration-A"
    assert tl.bundle["shots"]["A"]["payload"]["text_bindings"][0]["head"] == 3


def test_a_stand_in_is_remembered_and_swapped_when_the_real_asset_exists():
    tl = Checkout(bundle())
    clip = tl.add("am-sprite", at="Live", asset="TOWER", standin="R", params={"x": 10})
    assert clip.asset == "R" and "TOWER" in json.dumps(clip.data)
    assert tl.fill_standins() == []  # still missing
    tl.bundle["shots"]["B"]["internal_timeline"]["registry"]["assets"]["TOWER"] = {"media_id": "m-t"}
    assert tl.fill_standins() == [f"{clip.id}: stand-in → TOWER"]
    assert tl.clip(clip.id).asset == "TOWER"


def test_apply_script_swaps_changed_takes_and_adds_new_lines(tmp_path, monkeypatch):
    import astrid.sdk.timeline_checkout as tc

    monkeypatch.setattr(tc, "import_media", lambda path, project: {"media_id": f"m-{Path(path).stem}"})
    vo = tmp_path / "vo"
    vo.mkdir()
    (vo / "s1.wav").write_bytes(b"RIFF")
    (vo / "s1.words.json").write_text(json.dumps({"words": [{"start_s": 0.1, "end_s": 0.5, "word": "it"},
                                                             {"start_s": 0.6, "end_s": 1.0, "word": "went"},
                                                             {"start_s": 1.4, "end_s": 2.4, "word": "viral"}]}))
    (vo / "s1b.wav").write_bytes(b"RIFF")
    (vo / "s1b.words.json").write_text(json.dumps({"words": [[0.0, 0.5, "really"]]}))
    (vo / "s2.wav").write_bytes(b"RIFF")
    (vo / "s2.words.json").write_text(json.dumps({"words": [[0.1, 0.4, "Live"], [0.5, 0.8, "now"]]}))  # unchanged
    script = {"segments": [{"id": "s1", "text": "It went viral."}, {"id": "s1b", "text": "Really.", "gap_after_s": 0.4},
                           {"id": "s2", "text": "Live now."}]}
    tl = Checkout(bundle())
    tl.clip("b-type").enter_at("now")
    report = tl.apply_script(script, takes=vo)
    assert [v.segment for v in tl.lines()] == ["s1", "s1b", "s2"]
    assert "line s1: new take" in report and any(r.startswith("inserted line s1b") for r in report)
    assert tl.word("viral").end == pytest.approx(2.9)
    assert tl.voice("s2").clips[0].start == pytest.approx(tl.word("really").end + 0.4, abs=1 / FPS)
    assert tl.clip("b-type").start == pytest.approx(tl.word("now").start, abs=1 / FPS)
    assert tl.voice("s2").text == "Live now."


# ---- formula-backed params are visible and never silently override an edit (F1) --------

def _formula_bundle():
    data = bundle()
    data["parent"]["config"]["slots"] = {"HAND": {"hand_mark": {"x": 1200, "y": 600}}}
    rocket = data["shots"]["A"]["internal_timeline"]["clips"][1]
    rocket["params"]["x"] = 193
    rocket["app"] = {"cut": "c1", "layer": "rocket", "formulas": {"params.x": {"mark": "HAND", "axis": "x", "offset": -7, "unit": "logical"}}}
    return data


def test_python_set_on_a_formula_param_replaces_it_and_says_so():
    tl = Checkout(_formula_bundle())
    tl.resolve()
    tl.clip("c1.rocket").set(x=120)
    tl.resolve()  # what check/status do: the value must survive
    assert tl.clip("c1.rocket").get("x") == 120 and "params.x" not in intent.formulas(tl.clip("c1.rocket").data)
    lines = describe_changes(Checkout(_formula_bundle()), tl)
    assert any("x was ƒ(HAND -42) → now fixed = 120" in line for line in lines)


def test_a_formula_can_be_edited_in_place():
    tl = Checkout(_formula_bundle())
    tl.clip("c1.rocket").set(x="ƒ(HAND -60)")
    tl.resolve()
    assert intent.formulas(tl.clip("c1.rocket").data)["params.x"] == {"mark": "HAND", "axis": "x", "offset": -60, "unit": "canvas"}
    assert tl.clip("c1.rocket").get("x") == (round(1200 / 6) - 10) * 6  # stored as written: no ÷6 noise
    tl.clip("c1.rocket").set(x="ƒ(HAND -124)")
    tl.resolve()
    assert intent.formulas(tl.clip("c1.rocket").data)["params.x"]["offset"] == -124  # not -124.002
    assert tl.clip("c1.rocket").get("x") == round((1200 - 124) / 6) * 6  # canvas first, then the 6 px grid


def test_the_sheet_and_the_verb_show_and_replace_formulas_the_same_way():
    from astrid.sdk.timeline_sheet import apply_sheet, render_sheet

    tl = Checkout(_formula_bundle())
    tl.resolve()
    sheet = render_sheet(tl)
    assert "x=ƒ(HAND -42)" in sheet  # visible, in canvas px
    assert apply_sheet(tl, sheet) == []  # unchanged round-trip
    lines = apply_sheet(tl, sheet.replace("x=ƒ(HAND -42)", "x=120"))
    assert tl.clip("c1.rocket").get("x") == 120 and any("now fixed" in line for line in lines)
    tl2 = Checkout(_formula_bundle())
    tl2.resolve()
    apply_sheet(tl2, render_sheet(tl2).replace("x=ƒ(HAND -42)", "x=ƒ(HAND -60)"))
    assert intent.formulas(tl2.clip("c1.rocket").data)["params.x"]["offset"] == -60


def test_positions_are_canvas_px_for_every_element():
    tl = Checkout(bundle())
    rocket = tl.clip("R").set(x=1290)  # am-sprite: stored on its 320x180 grid
    assert rocket.params["x"] == 215 and rocket.get("x") == 1290
    card = tl.clip("b-type").set(x=900)  # am-type: stored as canvas px
    assert card.params["x"] == 900 and card.get("x") == 900


def test_swap_asset_takes_a_media_handle(monkeypatch):
    import astrid.sdk.timeline_checkout as tc

    seen = {}

    def fake(project, handle, client=None):
        seen["handle"] = handle
        return {"key": "robot-native", "media_id": "sha256:" + "a" * 64, "content_sha256": "sha256:" + "a" * 64, "type": "image/png"}

    monkeypatch.setattr(tc, "resolve_handle_entry", fake)
    tl = Checkout(bundle())
    tl.clip("R").swap_asset("run:01ABC/images#0")
    assert seen["handle"] == "run:01ABC/images#0" and tl.clip("a-rocket").asset == "robot-native"
    tl.clip("a-rocket").clear_asset()
    assert tl.clip("a-rocket").asset is None



def test_swap_asset_can_name_the_asset_it_brings_in(monkeypatch):
    """A handle's asset was always keyed by its file name (sprite-00, sprite-00-2 in the sheet);
    KEY=HANDLE (CLI) or {"key", "ref"} (Python) gives it a readable key instead."""
    import astrid.sdk.timeline_checkout as tc

    digests = {"run:01ABC/sprites#0": "a", "run:01ABC/sprites#1": "b"}
    monkeypatch.setattr(tc, "resolve_handle_entry", lambda project, handle, client=None: {
        "key": "sprite-00", "media_id": "sha256:" + digests[handle] * 64,
        "content_sha256": "sha256:" + digests[handle] * 64, "type": "image/png"})
    tl = Checkout(bundle())
    tl.clip("R").swap_asset("BROOM=run:01ABC/sprites#0")
    assert tl.clip("a-rocket").asset == "BROOM"
    tl.clip("a-rocket").swap_asset({"key": "BROOM", "ref": "run:01ABC/sprites#0"})  # same media: same key, no -2
    assert tl.clip("a-rocket").asset == "BROOM"
    with pytest.raises(TimelineEditError, match="already names other media"):
        tl.clip("a-rocket").swap_asset({"key": "BROOM", "ref": "run:01ABC/sprites#1"})

# ---- moments on params (P2/P3) ---------------------------------------------------------

def _param_moment_bundle():
    data = bundle()
    card = data["shots"]["B"]["internal_timeline"]["clips"][1]  # b-type 6.0–7.0 s
    card["params"]["states"] = [{"at": 0}, {"at": 3}]
    card["app"] = {"cut": "c2", "layer": "card",
                   "formulas": {"params.states[1].at": {"moment": '"now"', "as": "clip_frame"}}}
    return data


def test_a_nested_param_takes_a_moment_and_check_rejects_text_in_a_number():
    tl = Checkout(_param_moment_bundle())
    tl.resolve()
    card = tl.clip("c2.card")
    card.set(**{"states[1].at": '"Live"'})  # show's own nested address; a moment, not a literal string
    assert intent.formulas(card.data)["params.states[1].at"]["moment"] == '"Live"'
    assert card.params["states"][1]["at"] == 0  # "Live" is at the clip's start
    card.set(**{"states[0].at": 5})  # a fixed value
    assert card.params["states"][0]["at"] == 5
    with pytest.raises(TimelineEditError, match="not spoken"):
        tl.clip("R").set(scale='"oops"')  # a quoted word is read as a moment, and this one is not spoken
    tl.clip("R").data["params"]["scale"] = '"adapt" in w05c'  # an old literal string where a number belongs
    assert any(line.startswith("type    a-rocket.scale") and "ƒ(" in line for line in tl.check().blocking)


def test_a_param_moment_outside_its_clip_blocks_and_keep_fixes_an_orphan():
    tl = Checkout(_param_moment_bundle())
    card = tl.clip("c2.card")
    card.set(**{"states[1].at": '"viral"'})  # 1.3 s: long before this clip (6.0 s)
    blocking = tl.check().blocking
    assert any(line.startswith("outside c2.card.states[1].at") for line in blocking)
    card.set(**{"states[1].at": '"now"'})
    assert not any("outside" in line for line in tl.check().blocking)
    intent.formulas(card.data)["params.states[1].at"]["moment"] = '"gone"'
    card.data["app"]["astrid"]["formulas"]["params.states[1].at"] = {"moment": '"gone"', "as": "clip_frame"}
    assert any("states[1].at" in o for o in tl.orphans())
    card.keep()
    assert not any("states[1].at" in o for o in tl.orphans())


def test_check_flags_offcanvas_mixed_scale_and_damaged_sprite_by_address():
    data = bundle()
    sprite = data["shots"]["A"]["internal_timeline"]["clips"][1]
    # Never on canvas (the add() units bug put sprites at x=4212): blocking.
    sprite["params"].update({"x": 4212, "scale": 8,
                              "keyframes": [{"frame": 12, "x": 4300, "y": 20}]})
    data["shots"]["A"]["internal_timeline"]["registry"]["assets"]["R"]["pixel_provenance"] = {
        "executor": "pixel.sprites", "run_id": "sprite-run",
        "lattice": {"detected": True, "block": 4.0},
        "outline_integrity": {"fraction": 0.70},
    }
    second = {"id": "a-hourglass", "clipType": "am-sprite", "track": "sprite", "asset": "H",
              "at": 1.25, "hold": 0.5, "params": {"x": 20, "scale": 6},
              "app": {"cut": "c01", "layer": "hourglass"}}
    data["shots"]["A"]["internal_timeline"]["clips"].append(second)
    data["shots"]["A"]["internal_timeline"]["registry"]["assets"]["H"] = {
        "media_id": "m-h", "pixel_provenance": {"outline_integrity": {"fraction": 0.56}},
    }
    # Runs in from off screen (the film's mink): deliberate, not flagged.
    runner = {"id": "a-runner", "clipType": "am-sprite", "track": "sprite", "asset": "H",
              "at": 1.25, "hold": 0.5, "params": {"x": -900, "scale": 6,
                                                   "keyframes": [{"frame": 8, "x": 300, "y": 20}]},
              "app": {"cut": "c01", "layer": "runner"}}
    data["shots"]["A"]["internal_timeline"]["clips"].append(runner)
    lines = Checkout(data).check().problems
    assert any("OFFCANVAS" in line and "a-rocket" in line for line in lines)
    assert not any("OFFCANVAS" in line and "runner" in line for line in lines)
    assert any("MIXED-SCALE" in line and "a-rocket" in line and "hourglass" in line for line in lines)
    assert any("DAMAGED-ART" in line and "a-rocket" in line and "0.70" in line for line in lines)
    assert any("DAMAGED-ART" in line and "hourglass" in line and "0.56" in line for line in lines)
    # Outline-free art (the film's sparks measure 0.02) is a style, not damage.
    data["shots"]["A"]["internal_timeline"]["registry"]["assets"]["H"]["pixel_provenance"] = {
        "outline_integrity": {"fraction": 0.02}}
    assert not any("DAMAGED-ART" in line and "hourglass" in line for line in Checkout(data).check().problems)


def _bed_on_beat(data):
    """The music (Q, 4 s) carries a 120 bpm grid; the rocket starts on beat 2 after "went"."""
    data = _with_beats(data)
    tl = Checkout(data)
    tl.clip("a-rocket").on('beat 2 after "went"')
    return tl


def test_swap_music_to_a_compose_run_brings_its_beats_and_its_length(monkeypatch):
    import astrid.sdk.timeline_checkout as tc

    tl = _bed_on_beat(bundle())
    before = tl.clip("a-rocket").start  # "went" starts at 0.9 s; beats every 0.5 s → beat 2 after it is 1.5
    assert before == pytest.approx(1.5)
    new = {"key": "music", "media_id": "sha256:" + "b" * 64, "content_sha256": "sha256:" + "b" * 64, "type": "audio/wav"}
    monkeypatch.setattr(tc, "resolve_handle_entry", lambda project, handle, client=None: dict(new))
    monkeypatch.setattr(tc, "media_seconds", lambda project, media, client=None: 6.0 if media.get("media_id") == new["media_id"] else 4.0)
    seen = {}

    def beats(project, handle, client=None):
        seen["handle"] = handle
        return intent.beats_grid({"beats": [0.0, 0.7, 1.4, 2.1, 2.8, 3.5], "downbeats": [0.0, 2.8], "bpm": 85.7}, "run:R1/beats"), "run:R1/beats"

    monkeypatch.setattr(tc, "handle_beats", beats)
    music = tl.clips(track="music")[0]
    music.swap_asset("run:R1/music")
    assert seen["handle"] == "run:R1/music"
    assert music.duration == pytest.approx(6.0)  # it played its whole file: it plays the whole new one
    assert intent.beat_sources(music.data)[:3] == [0.0, 0.7, 1.4]
    assert tl.clip("a-rocket").start == pytest.approx(2.1)  # beat 2 after "went" (0.9 s): 1.4, then 2.1
    assert any("beats from run:R1/beats" in n and "re-resolved" in n for n in tl.notes)
    assert tl.check().blocking == []


def test_swap_music_without_a_grid_blocks_until_beats_are_attached_or_kept(monkeypatch, tmp_path):
    import astrid.sdk.timeline_checkout as tc

    tl = _bed_on_beat(bundle())
    monkeypatch.setattr(tc, "media_seconds", lambda project, media, client=None: None)
    music = tl.clips(track="music")[0]
    music.swap_asset("P")  # a registry key: no run behind it, so no grid comes along
    assert music.duration == pytest.approx(4.0) and any("could not read the new file's length" in n for n in tl.notes)
    assert any(line.startswith("beats") for line in tl.check().blocking)
    grid = tmp_path / "cue.beats.json"
    grid.write_text(json.dumps({"bpm": 120, "beats": [0.0, 0.25, 0.5, 0.75, 1.0, 1.25, 1.5, 1.75, 2.0],
                                "hits": [{"t": 1.0, "kind": "stab"}]}))
    music.set_beats(str(grid))
    assert tl.clip("a-rocket").start == pytest.approx(37 / 30)  # beat 2 after 0.9 s: 1.0, 1.25 (floored to frame 37)
    assert intent._app(music.data)["beats"]["hits"] == [[1.0, "stab"]]
    assert tl.check().blocking == []
    music.swap_asset("R")
    assert any(line.startswith("beats") for line in tl.check().blocking)
    music.set_beats("keep")
    assert tl.check().blocking == []


def test_check_blocks_a_literal_formula_string_where_the_element_wants_a_frame():
    """T04c: allAt='ƒ("adapt" in w05c)' as TEXT failed visualize's schema for the whole film; check must catch it."""
    tl = Checkout(bundle())
    clip = tl.clip("a-rocket")
    clip.data["clipType"] = "am-ui-sketch"
    clip.data["params"] = {"allAt": 'ƒ("adapt" in w05c)', "states": [{"at": 0, "data": "A"}, {"at": '"went"', "data": "B"}]}
    blocking = [line for line in tl.check().blocking if line.startswith("type")]
    assert any("a-rocket.allAt is the text" in line and "--set 'allAt=ƒ(\"adapt\" in w05c)'" in line for line in blocking)
    assert any("a-rocket.states[1].at is the text" in line for line in blocking)


def test_check_reports_new_lint_only_and_an_edit_reports_its_own_cuts():
    """P9/T15a(d): findings the published head already had are counted, not printed; an edit sees its cuts."""
    from astrid.sdk.timeline_checkout import CheckReport, _new_findings

    class Base:
        cuts: list = []

        def lint(self):
            return ["SMALL  c02 +0.40s am-discord text 22 px < 32 px at 1080p"]

    lint = ["SMALL  c02 +0.70s am-discord text 22 px < 32 px at 1080p", "SYNC   c02 +0.40s keys 0.18 s late", "HOLD   c07 +6.70s 7.80 s still"]
    new, old = _new_findings(lint, Base())
    assert old == 1 and new == lint[1:]  # the same finding on a cut that moved is not new
    report = CheckReport(valid=True, validation={}, summary=[], lint=lint, problems=[], changed_cuts=[2, 7], diff={},
                         cut_names=["c02", "c07"], blocking=[], lint_new=new, lint_old=old)
    lines = report.brief()
    assert "new lint on the 2 changed cut(s): 2" in lines[0]
    assert any("1 lint finding(s) were already there" in line for line in lines)
    mine = report.brief(cuts=["c07"])
    assert any("HOLD   c07" in line for line in mine) and not any("SYNC" in line for line in mine)
    assert any("1 more on other cuts" in line for line in mine)
    assert len(report.brief(full=True)) > len(lines) - 1 and any("SMALL" in line for line in report.brief(full=True))


def test_two_writers_on_one_working_copy_merge_and_never_drop_each_others_edits(tmp_path):
    """Concurrency: two handles open the same working copy; each saves; nothing is lost silently."""
    path = tmp_path / "main.json"
    Checkout(bundle()).save(path)
    a, b = Checkout.load(path), Checkout.load(path)
    a.clip("a-rocket").nudge(0.5)
    a.save()
    b.clip("b-type").set(text="Now live.")
    b.save()  # b loaded before a saved: a three-way merge, a's nudge is kept
    assert any("merged with another writer's save" in line for line in b.merged)
    both = Checkout.load(path)
    assert both.clip("a-rocket").start == pytest.approx(1.5) and both.clip("b-type").params["text"] == "Now live."
    # the same clip changed by both: refused, with what to do
    c, d = Checkout.load(path), Checkout.load(path)
    c.clip("a-rocket").nudge(0.1)
    c.save()
    d.clip("a-rocket").nudge(-0.1)
    with pytest.raises(TimelineEditError, match="another writer"):
        d.save()
    assert Checkout.load(path).clip("a-rocket").start == pytest.approx(1.6)  # c's edit is there, d's was refused
    d.save(force=True)
    assert Checkout.load(path).clip("a-rocket").start == pytest.approx(1.4)


def test_position_units_come_from_the_element_declaration_so_am_pixel_shape_takes_marks_and_canvas_px():
    """Units are data: am-pixel-shape declares metadata.units in its element.yaml, so canvas px and ƒ(MARK) work."""
    from astrid.sdk.timeline_checkout import element_units
    from astrid.sdk.timeline_sheet import render_sheet

    assert element_units("am-pixel-shape") == (6, frozenset({"x", "y"}))
    assert element_units("am-type") == (1, frozenset())  # declares nothing: canvas px
    tl = Checkout(_formula_bundle())
    shape = tl.clip("c1.rocket")
    shape.data["clipType"] = "am-pixel-shape"
    shape.data["params"] = {"shape": "ring", "x": 10, "y": 20}
    intent.set_formula(shape.data, "params.x", None)
    assert shape.get("x") == 60  # stored on the 320×180 grid, shown in canvas px
    shape.set(x="ƒ(HAND -42)", y=600)
    tl.resolve()
    assert shape.params["x"] == round((1200 - 42) / 6) and shape.get("x") == round((1200 - 42) / 6) * 6
    assert shape.params["y"] == 100 and "x=ƒ(HAND -42)" in render_sheet(tl) and "y=600" in render_sheet(tl)
    assert not tl.check().blocking


def test_time_valued_params_take_their_unit_from_the_element_declaration():
    """am-terminal declares lines[].at in clip frames: a moment set on it resolves to a frame (T12)."""
    from astrid.sdk.timeline_checkout import param_unit

    assert param_unit("am-terminal", "params.lines[3].at") == "clip frames"
    assert param_unit("am-sprite", "x") == "canvas px" and param_unit("am-type", "size") is None
    tl = Checkout(_param_moment_bundle())
    tl.resolve()
    term = tl.clip("c2.card")
    term.data["clipType"] = "am-terminal"
    term.data["params"] = {"lines": [{"kind": "cmd", "text": "ls"}]}
    term.set(**{"lines[0].at": '"Live"'})
    assert intent.formulas(term.data)["params.lines[0].at"]["as"] == "clip_frame"
    assert isinstance(term.params["lines"][0]["at"], int)


def test_python_setters_are_discoverable_and_refuse_what_is_not_a_param():
    """Acceptance fixes 1 and 3: .until("for 0.6s"), .for_("0.6s"), did-you-mean, and set() refusing junk keys."""
    tl = Checkout(bundle())
    card = tl.clip("b-type")
    card.until("for 0.6s")
    assert card.duration == pytest.approx(0.6)
    card.for_("18f")
    assert card.duration == pytest.approx(0.6)
    with pytest.raises(TimelineEditError, match=r"for is not a param: use for → \.hold_for"):
        card.set(**{"for": "0.6s"})
    with pytest.raises(TimelineEditError, match="has no param colour .*did you mean color"):
        card.set(colour="#fff")
    card.set(text="Now.")
    assert card.text == "Now."
    with pytest.raises(AttributeError, match="for that use hold_for"):
        card.lasting(0.6)
    with pytest.raises(AttributeError, match="did you mean hold_for"):
        card.holdfor(0.6)
    card.set(brand_new=1, _allow_new=True)
    assert card.params["brand_new"] == 1


def test_media_set_refuses_unknown_params_and_accepts_declared_fades():
    tl = Checkout(bundle())
    clip = tl.clip("a-music")
    before = copy.deepcopy(clip.data)
    with pytest.raises(TimelineEditError, match="has no param zzbogus"):
        clip.set(zzbogus=2)
    assert clip.data == before
    clip.set(fadeIn=0.5, fadeOut=0.25, fadeInCurve="equal-power", fadeOutCurve="linear")
    assert clip.params == {"fadeIn": 0.5, "fadeOut": 0.25, "fadeInCurve": "equal-power", "fadeOutCurve": "linear"}


@pytest.mark.parametrize("clip_id,fields,stored", [
    ("a-music", {"volume": "-3dB", "fade_in": "8f curve=equal-power"}, {"volume", "params"}),
    ("a-rocket", {"opacity": 0.6, "entrance": "fade 8f"}, {"opacity", "entrance"}),
])
def test_look_none_clears_fields_without_changing_other_clip_data(clip_id, fields, stored):
    clip = Checkout(bundle()).clip(clip_id)
    clip.data.setdefault("params", {})
    before = copy.deepcopy(clip.data)
    clip.look.set(**fields)
    assert all(name in clip.data for name in stored)
    clip.look.set(**{name: None for name in fields})
    assert clip.data == before


def test_verify_in_python_uses_the_fast_lane_at_what_changed(monkeypatch):
    """Script-mode parity with edit --verify: tl.verify() picks the changed moments, or at=."""
    from astrid.packs.rendering.actions.timeline_visualize import fast_lane

    seen = {}

    def fake(client, project, timeline, moments, *, draft=None, footer=()):
        seen.update(project=project, timeline=timeline, moments=[(round(m.t, 2), m.label) for m in moments], draft=draft)
        return {"lines": ["verify ok"], "rows": [], "page": "p.png"}

    monkeypatch.setattr(fast_lane, "verify", fake)
    tl = Checkout(bundle())
    before = tl.document()
    tl.clip("a-rocket").nudge(0.5)
    assert tl.verify(since=before, client=object())["lines"] == ["verify ok"]
    assert seen["project"] == "p" and seen["draft"] is tl and seen["moments"]
    tl.verify(at=["viral", 6.0], client=object())
    assert [label for _t, label in seen["moments"]] == ["viral", "6.0"]


def test_verify_moments_keep_boundary_and_parameter_change_samples():
    from astrid.packs.rendering.actions.timeline_visualize.fast_lane import Moment, moments_from_changes

    moments = moments_from_changes([
        {"clip_id": "a:rocket", "before": None, "after": [1.0, 2.0], "fields": ["added"]},
        {"clip_id": "b:type", "before": [3.0, 4.0], "after": [3.0, 4.0], "fields": ["params.text"]},
    ], FPS, names={"a:rocket": "c1.rocket"})

    assert moments == [
        Moment(1.0 - 1.0 / FPS, "c1.rocket: the frame before it starts", "a:rocket"),
        Moment(1.0, "c1.rocket starts", "a:rocket"),
        Moment(3.5, "type (params.text) mid-clip", "b:type"),
    ]


def test_fast_lane_uses_v3_filmstrip_and_checks_the_managed_video_digest(tmp_path, monkeypatch):
    import hashlib
    import json
    from types import SimpleNamespace

    from astrid.packs.rendering.actions.timeline_visualize import fast_lane

    video = tmp_path / "managed.mp4"
    video.write_bytes(b"managed-video")
    digest = hashlib.sha256(video.read_bytes()).hexdigest()
    index = tmp_path / "frame-index.json"
    index.write_text(json.dumps({
        "provenance": {"render_run_id": "composed-capture:main:saved-revision"},
        "media": {"verified": True, "digest": f"sha256:{digest}", "render_run_id": "candidate-run"},
    }))
    from astrid.sdk.client import AstridClient
    from astrid.sdk.remote import RemoteAstridClient
    from tests.sdk.test_native_timeline_facades import _NativeTransport
    import astrid.sdk.invocation as invocation

    calls = []
    remote = RemoteAstridClient(_NativeTransport())
    client = AstridClient(remote=remote)

    def public_dispatch_is_not_the_private_facade(*args, **kwargs):
        raise AssertionError("verify must use the canonical timeline facade")

    def dispatch(capability_id, **kwargs):
        calls.append((capability_id, kwargs))
        return SimpleNamespace(ok=True, outputs={"media": str(video), "frame_index": str(index)})

    monkeypatch.setattr(client, "invoke_result", public_dispatch_is_not_the_private_facade)
    monkeypatch.setattr(invocation, "_invoke_internal_result", dispatch)
    assert fast_lane._invoke_video(client, "project-1", "main", "candidate-run", 12) == video
    capability, request = calls[0]
    assert capability == "rendering.timeline_visualize"
    assert request["client"] is remote and request["project"] == "project-1"
    assert request["kind"] == "executor" and request["wait"] is True
    assert request["inputs"] == {
        "timeline_slug": "main", "render_run": "candidate-run", "view": "filmstrip",
        "formats": ["png"], "show": ["output"], "frame": 12, "include_media": True,
        "composed_capture": True,
    }
    # Neither current-head substitution nor changed managed-video bytes can pass.
    assert "revision_id" not in request["inputs"]
    video.write_bytes(b"changed-managed-video")
    with pytest.raises(fast_lane.FastLaneError, match="digest"):
        fast_lane._invoke_video(client, "project-1", "main", "candidate-run", 12)
    video.write_bytes(b"managed-video")
    # A matching capture provenance cannot bless a wrong or absent media run.
    for media_run in ("foreign-run", None):
        media = {"verified": True, "digest": f"sha256:{digest}"}
        if media_run is not None:
            media["render_run_id"] = media_run
        index.write_text(json.dumps({"provenance": {"render_run_id": "candidate-run"}, "media": media}))
        with pytest.raises(fast_lane.FastLaneError, match="requested managed render"):
            fast_lane._invoke_video(client, "project-1", "main", "candidate-run", 12)
    index.write_text(json.dumps({"provenance": {"render_run_id": "composed-capture:main:saved-revision"},
                                "media": {"verified": False, "digest": f"sha256:{digest}", "render_run_id": "candidate-run"}}))
    with pytest.raises(fast_lane.FastLaneError, match="attest"):
        fast_lane._invoke_video(client, "project-1", "main", "candidate-run", 12)



def test_fast_lane_verdict_comes_from_full_decoded_frame_pixels(tmp_path, monkeypatch):
    from PIL import Image

    from astrid.packs.rendering.actions.timeline_visualize import fast_lane

    published = tmp_path / "published.png"
    working = tmp_path / "working.png"
    Image.new("RGB", (8, 8), "black").save(published)
    changed = Image.new("RGB", (8, 8), "black")
    changed.putpixel((3, 4), (255, 255, 255))
    changed.save(working)
    snapshot = {"fps_rational": [30, 1], "config": {"theme_overrides": {"visual": {"canvas": {
        "width": 8, "height": 8,
    }}}}}
    pair = {"base_run": "base-run", "working_run": "candidate-run",
            "base_snapshot": snapshot, "working_snapshot": snapshot,
            "base_video": Path("published.mp4"),
            "base_media": {"render_run_id": "base-run", "selection": "explicit_current_render"},
            "working_video": Path("working.mp4"),
            "working_media": {"render_run_id": "candidate-run", "selection": "explicit_candidate_preview"}}
    monkeypatch.setattr(fast_lane, "prepare_pair", lambda *args, **kwargs: pair)
    monkeypatch.setattr(fast_lane, "_invoke_video", lambda _client, _project, _timeline, run, _frame:
                        Path("published.mp4" if run == "base-run" else "working.mp4"))

    def decode(video, frames, _out_dir):
        path = published if video.name == "published.mp4" else working
        return {frame: path for frame in frames}

    monkeypatch.setattr(fast_lane, "decode_frames", decode)
    import time

    monkeypatch.setenv("ASTRID_CLI_START_MONO", str(time.monotonic() - 5.0))
    result = fast_lane.verify(object(), "project", "main", [fast_lane.Moment(0.5, "changed layer")],
                              draft=object(), root=tmp_path / "results")

    # The timing accounts for the CLI's own start, not only the capture.
    assert result["timing"]["startup_s"] >= 5.0
    assert result["timing"]["end_to_end_s"] >= result["timing"]["total_s"] + 5.0
    row = result["rows"][0]
    assert row["different_pixels"] == 1
    assert row["bounds"] == (3, 4, 4, 5)
    assert "1.56% differ" in row["pixels"]
    # More moments than a page holds: say which were left out instead of dropping them silently.
    many = [fast_lane.Moment(29.0 + i, f"m{i}") for i in range(6)]
    capped = fast_lane.verify(object(), "project", "main", many, draft=object(), root=tmp_path / "results")
    assert len(capped["rows"]) == fast_lane.MAX_MOMENTS
    assert any("first 4 of 6 moments" in note and "m4, m5" in note for note in capped["notes"])


def test_sweep_marks_direct_edit_cuts_but_not_pure_reflow():
    from astrid.packs.rendering.actions.timeline_visualize.sweep import expected_cuts

    changes = [
        {"clip_id": "rocket", "kind": "changed", "before": [1.0, 2.0],
         "after": [1.5, 2.5], "fields": ["start", "end"]},
        {"clip_id": "card", "kind": "changed", "before": [3.0, 4.0],
         "after": [3.0, 4.0], "fields": ["params.text"]},
        {"clip_id": "short", "kind": "changed", "before": [5.0, 6.0],
         "after": [5.0, 5.5], "fields": ["end"]},
    ]

    assert expected_cuts(changes, {"rocket": "c1", "card": "c2", "short": "c3"}) == {"c2", "c3"}


def test_type_boxes_are_measured_with_the_fonts_and_check_says_when_text_leaves_title_safe():
    """Text-fit (T12/T15): the record states the measured box and the size that fits; check flags overflow."""
    from astrid.core.timeline import text_fit

    assert text_fit.available("label"), "bundled WOFF2 fonts require the fonttools[woff] development dependency"
    label = text_fit.layout("PART 01 · ADAPT, LIVE", font="label", size=28, width=1200)
    assert label["w"] > 21 * 28 * 0.62  # the old estimate ended the highlight at "ADAP"
    tl = Checkout(bundle())
    card = tl.clip("b-type")
    card.set(text="A very long headline that will not fit", size=120, x=900, width=1600)
    problems = [p for p in tl.check().problems if p.startswith("text")]
    assert problems and "outside title-safe (right)" in problems[0] and "fits" in problems[0]


def test_duplicate_is_all_or_nothing(monkeypatch):
    """A refused duplicate leaves no empty timeline: an invalid head is refused before creating, a failure
    after creating archives what was created."""
    from types import SimpleNamespace

    from astrid.sdk import timeline_duplicate as td

    calls = []

    class Timelines:
        def create_empty(self, **kw):
            calls.append("create")
            return SimpleNamespace(ok=True, data={"timeline_id": "dup-1"})

        def archive(self, project, ref):
            calls.append(f"archive {ref}")
            return SimpleNamespace(ok=True)

    client = SimpleNamespace(timelines=Timelines())
    bad = Checkout(bundle())
    bad.clip("a-rocket").data["params"]["scale"] = '"oops"'  # text in a number: check blocks it
    monkeypatch.setattr(td.Checkout, "open", classmethod(lambda cls, p, t, client=None, revision_id=None: Checkout(copy.deepcopy(bad.bundle))))
    with pytest.raises(TimelineEditError, match="nothing was created"):
        td._duplicate(client, "p", "t", slug=None, name=None, revision_id=None)
    assert calls == []
    good = Checkout(bundle())
    good.check = lambda: SimpleNamespace(valid=True, blocking=[], problems=[])
    monkeypatch.setattr(td.Checkout, "open", classmethod(lambda cls, p, t, client=None, revision_id=None: good))
    monkeypatch.setattr(td, "copy_content", lambda *a, **k: (_ for _ in ()).throw(TimelineEditError("the copy failed")))
    with pytest.raises(TimelineEditError, match="archived again"):
        td._duplicate(client, "p", "t", slug=None, name=None, revision_id=None)
    assert calls == ["create", "archive dup-1"]


def test_working_preview_is_not_relabelled_as_saved_composed_capture(monkeypatch):
    from astrid.sdk.timeline_filmstrip import prepare_filmstrip
    from astrid.sdk.exceptions import CapabilityValidationError
    from tests.sdk.test_timeline_filmstrip import ComposedCaptureClient, _patch_composed_parent
    reads = _patch_composed_parent(monkeypatch)
    with pytest.raises(CapabilityValidationError, match="unpublished candidate"):
        prepare_filmstrip(
            {"composed_capture": True, "render_run": "render-run", "timeline_ref": "tl"},
            project="p", client=ComposedCaptureClient(candidate=True),
        )
    assert reads == [], "candidate preview must not substitute saved-parent pixels"


@pytest.mark.parametrize("failure", [None, "missing_reference", "wrong_run", "wrong_kernel_run", "wrong_task", "wrong_project", "wrong_snapshot_run", "wrong_selection", "wrong_digest", "changed_bytes", "non_bytes", "unadmitted_media", "run_unavailable", "task_unavailable", "project_unavailable", "pending_run", "running_run", "failed_run"])
def test_candidate_video_retains_original_working_artifact_and_rejects_wrong_authority(tmp_path, monkeypatch, failure):
    """Reconstruct the observed Runtime descriptor through the actual SDK projection.

    T410 did not retain caller output keys. Its retained descriptor/CAS bytes
    are used when bound by the qualification runner; the ordinary unit fixture
    uses the same descriptor shape without claiming to be that live envelope.
    """
    import hashlib
    import os
    from dataclasses import replace
    from types import SimpleNamespace
    from astrid.sdk.invocation import _invocation_outputs
    from astrid.sdk.results import InvocationResult
    from astrid.sdk.client import AstridClient
    from astrid.sdk.remote import RemoteAstridClient
    from astrid.sdk.workspace_client import WorkspaceClientError
    from astrid.sdk.exceptions import ServiceError
    from astrid.packs.rendering.actions.timeline_visualize import fast_lane
    retained = os.environ.get("ASTRID_T4_RETAINED_CANDIDATE_CAS")
    data = Path(retained).read_bytes() if retained else b"WORKING original managed video"
    if retained:
        assert len(data) == 94586
        assert hashlib.sha256(data).hexdigest() == "7600c1295f6da3054c9a381838d6f5d97500384e0e10617faa5e340dea7d09c6"
    digest = "sha256:" + hashlib.sha256(data).hexdigest()
    run_id = "becb0c1168c3438bb0db5a8e04683b8c"
    task_id = "036010b383504700a66ef403fdbf3750"
    # Safe retained output fields only: never copy admission tokens.
    raw = {"outputs": [{"name": "video", "digest": digest, "filename": "hype.mp4",
                       "media_type": "video/mp4", "size": len(data), "kind": "object",
                       "role": "result", "ordinal": 0, "is_primary": True}]}
    candidate = InvocationResult("rendering.render", "executor", "executor", True,
        run_id=run_id, kernel_run_id=run_id, kernel_task_id=task_id,
        raw_result=raw, outputs=_invocation_outputs(raw, manifest_path=None))
    assert "video" not in candidate.outputs, "old consumer cannot treat durable descriptor as a local path"
    snapshot = {"render_run_id": run_id, "video_digest": digest,
                "metadata": {"selection": "explicit_candidate_preview"},
                "config": {"clips": [{"id": "title", "params": {"content": "WORKING"}}]}}
    authority = {"render_run_id": run_id, "video_digest": digest, "filmstrip_snapshot": snapshot,
                 "project_id": "p", "video_object_id": digest, "include_media": True}
    run = {"id": run_id, "project_id": "p", "status": "completed", "task_ids": [task_id]}
    task = {"id": task_id, "state": "succeeded"}
    reads = []
    def read_bytes(ref):
        reads.append(ref)
        assert ref == digest
        return data
    transport = SimpleNamespace(get_run=lambda ref: run, get_task=lambda ref: task,
        get_project=lambda ref: {"id": "p"}, get_object=lambda ref: {"data": read_bytes(ref)})
    client = AstridClient(remote=RemoteAstridClient(transport))
    def unavailable(*args): raise WorkspaceClientError(404, "not_found", "fixture unavailable", {})
    if failure == "run_unavailable": transport.get_run = unavailable
    if failure == "task_unavailable": transport.get_task = unavailable
    if failure == "project_unavailable": transport.get_project = unavailable
    monkeypatch.setattr(fast_lane.tempfile, "tempdir", str(tmp_path))
    if failure == "missing_reference": authority.pop("video_object_id")
    if failure == "wrong_run": authority["render_run_id"] = "base-run"
    if failure == "wrong_kernel_run": candidate = replace(candidate, kernel_run_id="base-run")
    if failure == "wrong_task": candidate = replace(candidate, kernel_task_id="base-task")
    if failure == "wrong_project": run["project_id"] = "other"
    if failure in {"pending_run", "running_run", "failed_run"}: run["status"] = failure.removesuffix("_run")
    if failure == "wrong_snapshot_run": snapshot["render_run_id"] = "base-run"
    if failure == "wrong_selection": snapshot["metadata"]["selection"] = "explicit_archived_render"
    if failure == "wrong_digest": snapshot["video_digest"] = "sha256:" + "b" * 64
    if failure == "changed_bytes": data = b"BASE substituted video"
    if failure == "non_bytes": data = "not object bytes"
    if failure == "unadmitted_media": authority["include_media"] = False
    if failure is not None:
        with pytest.raises((fast_lane.FastLaneError, ServiceError)):
            fast_lane._candidate_video(candidate, authority, client=client, project="p", media_root=tmp_path)
        assert not list(tmp_path.glob("astrid-working-preview-*"))
    else:
        path, receipt = fast_lane._candidate_video(candidate, authority, client=client, project="p", media_root=tmp_path)
        assert path.parent.parent == tmp_path and path.name == "video.mp4"
        assert path.read_bytes() == data and reads == [digest]
        assert receipt == {"render_run_id": run_id, "digest": digest, "selection": "explicit_candidate_preview", "verified": True}
        assert snapshot["config"]["clips"][0]["params"]["content"] == "WORKING"


@pytest.mark.integration  # imports runtime_protocol from the sibling Runtime checkout
@pytest.mark.parametrize("base_policy", ["clean", "historical", "review", "profile", "canvas"])
def test_fast_lane_pair_keeps_real_explicit_working_snapshot_and_original_video(tmp_path, monkeypatch, request, base_policy):
    """Only transport/kernel are fake: real admission, wait and DTO projection.

    Retained T410 bytes/descriptor reconstruct the output boundary. Its caller
    keys were not recorded; this test does not claim they were observed.
    """
    import hashlib
    import os
    from copy import deepcopy
    from types import SimpleNamespace
    from astrid.core.timeline.authoring_bundle import open_authoring_bundle
    from astrid.sdk import invocation
    from astrid.sdk.results import InvocationResult
    from astrid.sdk.client import AstridClient
    from astrid.sdk.remote import RemoteAstridClient
    from astrid.packs.rendering.actions.timeline_visualize import fast_lane
    from tests.sdk.test_timeline_filmstrip import ComposedCaptureClient

    retained = os.environ.get("ASTRID_T4_RETAINED_CANDIDATE_CAS")
    data = Path(retained).read_bytes() if retained else b"original WORKING managed artifact"
    digest = "sha256:" + hashlib.sha256(data).hexdigest()
    retained_base = os.environ.get("ASTRID_T4_RETAINED_BASE_CAS")
    base_data = Path(retained_base).read_bytes() if retained_base else b"original BASE managed artifact"
    base_digest = "sha256:" + hashlib.sha256(base_data).hexdigest()
    config = {"duration": 1, "theme_overrides": {"visual": {"canvas": {"fps": 24, "width": 320, "height": 180}}},
              "tracks": [{"id": "title", "kind": "visual", "label": "Title"}],
              "clips": [{"id": "title", "track": "title", "clipType": "text-card", "at": 0, "hold": 1,
                         "params": {"content": "BASE"}}]}
    payload = {"config": config, "registry": {"assets": {}}, "clips": deepcopy(config["clips"]), "occurrences": []}
    parent = {"project_id": "p", "timeline_id": "tl", "revision_id": "saved-revision",
              "content_digest": "sha256:" + hashlib.sha256(json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()).hexdigest(),
              "payload": payload}
    candidate = open_authoring_bundle(parent, shot_revisions=[], internal_timeline_revisions=[])
    candidate["parent"]["config"]["clips"][0]["params"]["content"] = "frozen-WORKING"
    candidate["parent"]["clips"] = deepcopy(candidate["parent"]["config"]["clips"])
    produced = {}
    from runtime_protocol.service import RuntimeService
    def result(value): return SimpleNamespace(ok=True, data=value)

    class Runtime(ComposedCaptureClient):
        def __init__(self):
            super().__init__()
            self.invocations = []
            # Exact T412 retained public projection: run.completed, task.succeeded.
            # RuntimeService.run and _task_resource remain real; only transport
            # storage/kernel admission are fixture-owned.
            original_runs = [("base-run", "base-task"), ("candidate", "candidate-task")]
            if base_policy == "historical": original_runs.append(("newer-run", "newer-task"))
            store = _seed_terminal_projection_runtime(tmp_path / "runtime-realm", "p", original_runs, request)
            self.public_service = RuntimeService.__new__(RuntimeService)
            self.public_service.store = store
            self.projects = SimpleNamespace(show=lambda ref: result(self.get_project(ref)))
            self.timelines = SimpleNamespace(list=lambda project, **kwargs: result(self.list_timelines(project)),
                inspect=lambda project, timeline, **kwargs: result({"representation": "canonical_head", "is_current_head": True, "revision_id": "saved-revision", "timeline_id": "tl"}))
            self.media = SimpleNamespace(list=lambda *args, **kwargs: result([[], None]))
            self.tasks = SimpleNamespace(show=lambda task: result(self.get_task(task)), list_managed_outputs=lambda task: result(self.list_managed_outputs(task)))
            self.objects = SimpleNamespace(read_bytes=lambda ref: data if ref == digest else (_ for _ in ()).throw(AssertionError("wrong object")))

        def list_timelines(self, project, **kwargs):
            # A saved BASE can have older render provenance while the draft's
            # immutable parent remains current; stale-parent preview is rejected.
            return [[{"project_id": "p", "timeline_id": "tl", "slug": "cut", "version": 1,
                      "config_version": 2 if base_policy == "historical" else 1,
                      "head_revision_id": "saved-revision"}], None]

        def get_project_parent_composition_revision(self, project, timeline, revision):
            allowed = {"saved-revision", "newer-revision"} if base_policy == "historical" else {"saved-revision"}
            assert (project, timeline) == ("p", "tl") and revision in allowed
            value = deepcopy(parent)
            value["revision_id"] = revision
            return value

        def get_project_shot_revision(self, *args):
            raise AssertionError("native text fixture has no shot dependencies")

        def get_project_timeline_revision(self, *args):
            raise AssertionError("native text fixture has no internal dependencies")

        def inspect_timeline(self, project, timeline, **kwargs):
            return {"representation": "canonical_head", "is_current_head": True, "revision_id": "saved-revision", "timeline_id": "tl"}

        def list_managed_outputs(self, task_id):
            # The synchronous SDK now requires the completed task's public
            # managed-output listing to settle. Bind it to the original video
            # descriptor produced by this fake kernel; never fabricate bytes.
            assert task_id == "candidate-task"
            produced.setdefault("managed_output_reads", []).append(task_id)
            rows = deepcopy(produced["task"]["result"]["outputs"])
            for ordinal, row in enumerate(rows):
                row.update(
                    association_id=f"candidate-association-{ordinal}",
                    task_id=task_id, run_id="candidate", attempt_id="candidate-attempt",
                    project_id="p", output_port=row["name"], object_id=row["digest"],
                )
            return rows

        def get_object(self, object_id):
            assert object_id in {digest, base_digest}
            return {"data": data if object_id == digest else base_data}

        def list_project_runs(self, project, **kwargs):
            values = [self.get_run("base-run")]
            if base_policy == "historical": values.insert(0, self.get_run("newer-run"))
            return [values, None]

        def get_run(self, run_id):
            return self.public_service.run(run_id)

        def get_task(self, task_id):
            if task_id == "candidate-task": return deepcopy(produced["task"])
            value = super().get_task(task_id)
            value["spec"]["spec"]["inputs"].pop("profile")
            admitted_inputs = value["spec"]["spec"]["inputs"]
            admitted_inputs["timeline_authority"]["config_version"] = 1
            admitted_inputs["timeline_snapshot"]["config"] = deepcopy(config)
            if base_policy == "review": admitted_inputs["review"] = True
            if base_policy == "profile": admitted_inputs["profile"] = {"fps_rational": [24, 1]}
            if base_policy == "canvas": admitted_inputs["timeline_snapshot"]["config"]["theme_overrides"]["visual"]["canvas"]["width"] = 640
            value["result"]["outputs"][0]["name"] = "video"
            value["result"]["outputs"][0]["digest"] = base_digest
            value["result"]["outputs"][0]["size"] = len(base_data)
            value["result"]["outputs"][0]["managed_object_reference"] = {"object_id": base_digest,
                "digest": base_digest, "size": len(base_data), "media_type": "video/mp4"}
            if task_id == "newer-task":
                admitted_inputs["timeline_authority"]["parent_revision_id"] = "newer-revision"
                admitted_inputs["timeline_authority"]["config_version"] = 2
            # Preserve the actual Runtime public resource encoder for BASE too.
            raw_task = {"id": task_id, "status": "completed", "capability": "rendering.render",
                        "spec": value["spec"], "result": value["result"],
                        "created_at": "2026-10-13T17:20:10.592Z", "updated_at": "2026-10-13T17:20:10.592Z"}
            return self.public_service._task_resource({"task": raw_task, "run": {"id": "newer-run" if task_id == "newer-task" else "base-run", "project_id": "p"}})

        def list_project_objects(self, project, **kwargs):
            values, cursor = super().list_project_objects(project, **kwargs)
            return [*values, {"object_id": digest, "digest": digest, "size": len(data)},
                    {"object_id": base_digest, "digest": base_digest, "size": len(base_data)}], cursor


    def kernel(capability, **kwargs):
        assert capability.id == "rendering.render"
        assert kwargs["inputs"]["timeline_authority"]["render_mode"] == "authoring_candidate_preview"
        produced.setdefault("review_routing", []).append(kwargs["inputs"]["review"])
        assert kwargs["inputs"]["review"] is False, "verification must match the non-review BASE render"
        task = {"id": "candidate-task", "task_id": "candidate-task", "run_id": "candidate", "attempt_id": "candidate-attempt",
                "state": "succeeded", "capability_id": "rendering.render", "spec": {"spec": {"inputs": deepcopy(kwargs["inputs"])}},
                "result": {"outputs": [{"name": "video", "digest": digest, "filename": "hype.mp4", "media_type": "video/mp4",
                    "size": len(data), "kind": "object", "role": "result", "ordinal": 0, "is_primary": True}]}}
        stored = deepcopy(task)
        stored.update(status="completed", capability="rendering.render", attempt=0, runtime_epoch=1,
                      created_at="2026-10-13T15:18:00Z", updated_at="2026-10-13T15:18:00Z")
        stored.pop("state")
        task = transport.public_service._task_resource({"run": transport.get_run("candidate"), "task": stored})
        assert task["state"] == "succeeded" and transport.get_run("candidate")["status"] == "completed"
        produced["task"] = task
        return "candidate", "candidate-task", "candidate-attempt", None, {"ok": True, "task": task}, True, None

    monkeypatch.setattr(invocation, "_kernel_invoke", kernel)
    monkeypatch.setattr(fast_lane.tempfile, "tempdir", str(tmp_path))
    transport = Runtime()
    retained_shape = os.environ.get("ASTRID_T4_RETAINED_RUNTIME_SHAPE")
    if retained_shape:
        observed = json.loads(Path(retained_shape).read_text())
        assert observed["source"] == "ACTUAL_RETAINED_SQLITE_READONLY_THROUGH_INSTALLED_RUNTIME_PUBLIC_NORMALIZERS"
        assert observed["run"]["status"] == transport.get_run("candidate")["status"] == "completed"
        assert observed["task"]["state"] == "succeeded"
        assert observed["task"]["run_id"] == observed["run"]["id"]
        assert observed["task"]["task_id"] in observed["run"]["task_ids"]
    runtime = AstridClient(remote=RemoteAstridClient(transport))
    assert not hasattr(runtime._remote, "get_run") and not hasattr(runtime, "objects")
    draft = SimpleNamespace(bundle={"base_parent": {"revision_id": "saved-revision"}}, document=lambda: deepcopy(candidate))
    preimage = os.environ.get("ASTRID_T4_FAST_LANE_PREIMAGE")
    if preimage:
        import ast
        source = Path(preimage).read_text()
        function = next(node for node in ast.parse(source).body if isinstance(node, ast.FunctionDef) and node.name == "_candidate_video")
        old = {"Any": object, "Mapping": __import__("collections.abc", fromlist=["Mapping"]).Mapping,
               "Path": Path, "hashlib": hashlib, "FastLaneError": fast_lane.FastLaneError}
        exec(compile(ast.Module(body=[function], type_ignores=[]), preimage, "exec"), old)
        current = fast_lane._candidate_video
        def red_then_green(value, authority, **kwargs):
            assert isinstance(value, InvocationResult) and value.ok
            assert value.outputs["artifacts"][0]["digest"] == digest
            shape_path = os.environ.get("ASTRID_T4_RETAINED_CANDIDATE_SHAPE")
            if shape_path:
                shape = json.loads(Path(shape_path).read_text())
                assert shape["ok"] is True and shape["error"] is None
                assert {key: type(v).__name__ for key,v in value.outputs.items()} == shape["output_key_types"]
            with pytest.raises(fast_lane.FastLaneError, match="successful original task and project"):
                old["_candidate_video"](value, authority, **kwargs)
            return current(value, authority, **kwargs)
        monkeypatch.setattr(fast_lane, "_candidate_video", red_then_green)
    if base_policy not in {"clean", "historical"}:
        match = "normal non-review render" if base_policy in {"review", "profile"} else "same frozen canvas"
        with pytest.raises(fast_lane.FastLaneError, match=match):
            fast_lane._require_clean_base_render(runtime, "base-run", draft)
        assert produced.get("review_routing", []) == [], "unsupported BASE must not render a candidate"
        return
    preimage = os.environ.get("ASTRID_T4_REVIEW_PROFILE_PREIMAGE")
    if preimage:
        import ast
        tree = ast.parse(Path(preimage).read_text())
        old_pair = next(node for node in tree.body if isinstance(node, ast.FunctionDef) and node.name == "prepare_pair")
        namespace = dict(fast_lane.__dict__)
        exec(compile(ast.Module(body=[old_pair], type_ignores=[]), preimage, "exec"), namespace)
        with pytest.raises((fast_lane.FastLaneError, AssertionError)):
            namespace["prepare_pair"](runtime, "p", "cut", draft, media_root=tmp_path)
        assert produced["review_routing"] == [True], "old red must reach the actual review producer"
    pair = fast_lane.prepare_pair(runtime, "p", "cut", draft, media_root=tmp_path)
    assert produced["review_routing"] == ([True, False] if preimage else [False])
    assert pair["base_run"] == "base-run" and pair["working_run"] == "candidate"
    assert pair["base_snapshot"]["render_run_id"] == "base-run"
    assert pair["working_snapshot"]["render_run_id"] == "candidate"
    assert pair["working_snapshot"]["metadata"]["selection"] == "explicit_candidate_preview"
    assert pair["working_snapshot"]["clips"][0]["params"]["content"] == "frozen-WORKING"
    assert pair["base_video"].read_bytes() == base_data
    assert pair["base_media"]["digest"] == base_digest
    assert pair["base_media"]["selection"] == ("explicit_historical_render" if base_policy == "historical" else "explicit_current_render")
    assert pair["working_video"].read_bytes() == data
    assert pair["working_media"]["digest"] == digest
    # No outputs.video fabrication: the awaited real SDK observer projects
    # the retained descriptor to artifacts; CAS materialization is explicit.
    assert produced["managed_output_reads"]
    assert set(produced["managed_output_reads"]) == {"candidate-task"}
    assert produced["task"]["result"]["outputs"][0]["digest"] == digest
    assert produced["task"]["spec"]["spec"]["inputs"]["timeline_snapshot"]["config"]["clips"][0]["params"]["content"] == "frozen-WORKING"


@pytest.mark.parametrize("route", ["verify", "sweep"])
@pytest.mark.parametrize("later_failure", [None, "base", "decode", "page"])
def test_candidate_cas_materialization_has_run_owned_lifetime(tmp_path, monkeypatch, route, later_failure):
    from types import SimpleNamespace
    import hashlib
    from PIL import Image
    from astrid.sdk.results import InvocationResult
    from astrid.sdk.client import AstridClient
    from astrid.sdk.remote import RemoteAstridClient
    from astrid.packs.rendering.actions.timeline_visualize import fast_lane, sweep
    data = b"owned working CAS video"
    digest = "sha256:" + hashlib.sha256(data).hexdigest()
    base_data = b"owned BASE CAS video"
    base_digest = "sha256:" + hashlib.sha256(base_data).hexdigest()
    base_snapshot = {"render_run_id": "base", "video_digest": base_digest, "fps_rational": [30, 1],
                     "metadata": {"selection": "explicit_current_render"}}
    base_authority = {"render_run_id": "base", "project_id": "p", "include_media": True,
                      "video_digest": base_digest, "video_object_id": base_digest, "filmstrip_snapshot": base_snapshot}
    candidate = InvocationResult("rendering.render", "executor", "executor", True,
        run_id="working", kernel_run_id="working", kernel_task_id="task")
    snapshot = {"render_run_id": "working", "video_digest": digest, "fps_rational": [30, 1],
                "metadata": {"selection": "explicit_candidate_preview"}}
    authority = {"render_run_id": "working", "project_id": "p", "video_digest": digest,
                 "video_object_id": digest, "filmstrip_snapshot": snapshot, "include_media": True}
    transport = SimpleNamespace(get_project=lambda ref: {"id": "p"},
        get_run=lambda ref: {"id": ref, "project_id": "p", "task_ids": ["base-task" if ref == "base" else "task"], "status": "completed"},
        get_task=lambda ref: {"id": ref, "task_id": ref, "run_id": "base" if ref == "base-task" else "working",
                              "state": "succeeded", "capability_id": "rendering.render",
                              "result": {"outputs": [{"name": "video", "digest": base_digest if ref == "base-task" else digest}]}},
        get_object=lambda ref: {"data": base_data if ref == base_digest else data})
    remote = AstridClient(remote=RemoteAstridClient(transport))
    materialized = []
    def pair(*args, media_root):
        video, media = fast_lane._candidate_video(candidate, authority, client=remote, project="p", media_root=media_root)
        materialized.append(video)
        base_video, base_media = fast_lane._base_video(base_authority, client=remote, project="p", run_id="base", media_root=media_root)
        materialized.append(base_video)
        if later_failure == "base": raise fast_lane.FastLaneError("base failure")
        return {"base_run": "base", "working_run": "working", "base_snapshot": base_snapshot,
                "working_snapshot": snapshot, "base_video": base_video, "base_media": base_media,
                "working_video": video, "working_media": media}
    monkeypatch.setattr(fast_lane, "prepare_pair", pair)
    foreign = tmp_path / "foreign-video.mp4"
    foreign.write_bytes(b"foreign original remains")
    def base_video(client, project, timeline, run, frame):
        assert run == "base", "WORKING must use admitted original CAS video, never composed/saved capture"
        if later_failure == "base": raise fast_lane.FastLaneError("base failure")
        return foreign
    monkeypatch.setattr(fast_lane, "_invoke_video", base_video)
    def decode(video, frames, output):
        is_base = video == foreign or video.read_bytes() == base_data
        if not is_base:
            assert video.read_bytes() == data
            if later_failure == "decode": raise fast_lane.FastLaneError("decode failure")
        output.mkdir(parents=True)
        paths = {}
        for frame in frames:
            paths[frame] = output / f"frame-{frame}.png"
            Image.new("RGB", (8, 8), "black" if is_base else "white").save(paths[frame])
        return paths
    monkeypatch.setattr(fast_lane, "decode_frames", decode)
    if route == "sweep" and later_failure is None:
        real_page = fast_lane._compose_page
        def page(rows, output, **kwargs):
            assert rows[0]["kind"] == "changed" and rows[0]["published"] and rows[0]["working"]
            assert rows[0]["label"] == "title" and rows[0]["frame"] == 15 and rows[0]["t"] == 0.5
            assert "100.00% differ" in rows[0]["pixels"]
            return real_page(rows, output, **kwargs)
        monkeypatch.setattr(fast_lane, "_compose_page", page)
    if later_failure == "page":
        monkeypatch.setattr(fast_lane, "_compose_page", lambda *args, **kwargs: (_ for _ in ()).throw(fast_lane.FastLaneError("page failure")))
    monkeypatch.setattr(sweep, "plan_sweep", lambda *args: [{"key": "title", "cut_id": "title", "kind": "same", "render": True,
        "moved": 0, "samples": [{"label": "middle", "published_frame": 15, "working_frame": 15}]}])
    root = tmp_path / "inspection"
    def call():
        if route == "verify":
            return fast_lane.verify(remote, "p", "cut", [fast_lane.Moment(0.5, "middle")], draft=object(), root=root)
        return sweep.sweep(remote, "p", "cut", draft=object(), root=root)
    if later_failure:
        with pytest.raises(fast_lane.FastLaneError, match=later_failure + " failure"): call()
        assert list(root.iterdir()) == []
    else:
        result = call()
        assert Path(result["page"]).is_file(), "returned inspection evidence remains"
    assert len(materialized) == 2
    assert all(not video.exists() and not video.parent.exists() for video in materialized)
    assert not list(root.rglob("astrid-base-render-*"))
    assert not list(root.rglob("astrid-working-preview-*"))
    assert foreign.read_bytes() == b"foreign original remains"


def test_fast_lane_real_ffmpeg_decodes_exact_selected_frame_numbers(tmp_path):
    """Use the actual decoder and lossless known pixels, including sparse samples."""
    import shutil
    import subprocess
    from PIL import Image
    from astrid.packs.rendering.actions.timeline_visualize import fast_lane

    ffmpeg = shutil.which("ffmpeg")
    assert ffmpeg, "the rendering qualification requires its installed FFmpeg"
    source = tmp_path / "source"
    source.mkdir()
    colors = {n: (n * 4, 255 - n * 4, n * 3) for n in range(60)}
    for n, color in colors.items():
        Image.new("RGB", (16, 16), color).save(source / f"frame-{n:05d}.png")
    video = tmp_path / "known-lossless.mkv"
    encoded = subprocess.run(
        [ffmpeg, "-v", "error", "-framerate", "30", "-i",
         str(source / "frame-%05d.png"), "-c:v", "ffv1", "-pix_fmt", "bgr0",
         "-y", str(video)], capture_output=True, text=True, timeout=15,
    )
    assert encoded.returncode == 0, encoded.stderr
    selected = fast_lane.decode_frames(video, [48, 3, 33, 18, 3], tmp_path / "decoded")
    assert list(selected) == [3, 18, 33, 48]
    for n, path in selected.items():
        with Image.open(path) as image:
            assert image.size == (16, 16)
            assert set(image.convert("RGB").getdata()) == {colors[n]}
    with pytest.raises(fast_lane.FastLaneError, match="yielded 0 of 1"):
        fast_lane.decode_frames(video, [60], tmp_path / "missing-frame")
    with pytest.raises(fast_lane.FastLaneError, match="could not decode"):
        fast_lane.decode_frames(tmp_path / "missing-video.mkv", [3], tmp_path / "missing-video")


def test_normal_candidate_preview_keeps_review_default_and_allows_verification_override():
    from astrid.sdk.authoring_bundle import render_authoring_candidate_preview
    class Caller:
        def invoke_result(self, capability, **kwargs):
            assert capability == "rendering.render"
            return kwargs
    frozen = {"kind": "authoring-candidate-preview", "candidate": {"marker": "frozen"}}
    default = render_authoring_candidate_preview(frozen, Caller(), project="p", timeline_ref="cut")
    verification = render_authoring_candidate_preview(frozen, Caller(), project="p", timeline_ref="cut", review=False)
    assert default["inputs"]["review"] is True
    assert verification["inputs"]["review"] is False
    assert default["inputs"]["authoring_preview"] == verification["inputs"]["authoring_preview"] == frozen
    assert frozen == {"kind": "authoring-candidate-preview", "candidate": {"marker": "frozen"}}


@pytest.mark.integration  # imports runtime_protocol from the sibling Runtime checkout
@pytest.mark.parametrize("failure", [None, "wrong_run", "wrong_snapshot", "wrong_project", "wrong_task", "wrong_task_digest", "pending_task", "preview", "missing_ref", "digest", "bytes", "no_media"])
def test_base_video_materializes_only_exact_managed_media_through_real_public_client(tmp_path, request, failure):
    """Real SDK public families normalize only transport-owned Runtime rows/CAS."""
    import hashlib
    from copy import deepcopy
    from types import SimpleNamespace
    from runtime_protocol.service import RuntimeService
    from astrid.sdk.client import AstridClient
    from astrid.sdk.remote import RemoteAstridClient
    from astrid.packs.rendering.actions.timeline_visualize import fast_lane
    data = b"original saved BASE media"
    digest = "sha256:" + hashlib.sha256(data).hexdigest()
    snapshot = {"render_run_id": "base-run", "video_digest": digest, "metadata": {"selection": "explicit_current_render"}}
    authority = {"render_run_id": "base-run", "project_id": "project", "include_media": True,
                 "filmstrip_snapshot": snapshot, "video_digest": digest, "video_object_id": digest}
    store = _seed_terminal_projection_runtime(tmp_path / "runtime-realm", "project", [("base-run", "base-task")], request)
    producer = RuntimeService.__new__(RuntimeService)
    producer.store = store
    raw = {"id": "base-task", "status": "completed", "capability": "rendering.render", "spec": {}, "result": {"outputs": [{"name": "video", "digest": digest}]},
           "created_at": "2026-10-13T18:10:25.451Z", "updated_at": "2026-10-13T18:10:25.451Z"}
    task = producer._task_resource({"task": raw, "run": {"id": "base-run", "project_id": "project"}})
    reads = []
    class Transport:
        def get_run(self, run): return producer.run(run)
        def get_task(self, ref): return deepcopy(task)
        def get_project(self, ref): return {"id": "project", "project_id": "project"}
        def get_object(self, object_id):
            reads.append(object_id)
            assert object_id == digest
            return {"data": b"wrong bytes" if failure == "bytes" else data}
    client = AstridClient(remote=RemoteAstridClient(Transport()))
    if failure == "wrong_run": authority["render_run_id"] = "other-run"
    if failure == "wrong_snapshot": snapshot["render_run_id"] = "other-run"
    if failure == "wrong_project": authority["project_id"] = "other-project"
    if failure == "wrong_task": task["run_id"] = "other-run"
    if failure == "wrong_task_digest": task["result"]["outputs"][0]["digest"] = "sha256:" + "0" * 64
    if failure == "pending_task": task["state"] = "running"
    if failure == "preview": snapshot["metadata"]["selection"] = "explicit_candidate_preview"
    if failure == "missing_ref": authority.pop("video_object_id")
    if failure == "digest": snapshot["video_digest"] = "sha256:" + "0" * 64
    if failure == "no_media": authority["include_media"] = False
    before = deepcopy(authority)
    existing_entries = set(tmp_path.iterdir())
    if failure:
        with pytest.raises(fast_lane.FastLaneError):
            fast_lane._base_video(authority, client=client, project="project", run_id="base-run", media_root=tmp_path)
        assert set(tmp_path.iterdir()) == existing_entries
        assert reads == ([digest] if failure == "bytes" else [])
    else:
        video, receipt = fast_lane._base_video(authority, client=client, project="project", run_id="base-run", media_root=tmp_path)
        assert video.read_bytes() == data and reads == [digest]
        assert video.parent.parent == tmp_path
        assert receipt == {"render_run_id": "base-run", "digest": digest, "selection": "explicit_current_render", "verified": True}
    assert authority == before


def _seed_terminal_projection_runtime(root, project_id, run_tasks, request):
    """Build a real Runtime store containing only terminal projection rows."""
    from runtime_protocol.store import RealmStore
    from runtime_protocol.util import now

    store = RealmStore.initialize(root)
    request.addfinalizer(store.close)
    with store._transaction():
        realm_id = store.conn.execute("SELECT id FROM realm").fetchone()[0]
        timestamp = now()
        store.conn.execute(
            "INSERT INTO projects (id, realm_id, slug, name, metadata_json, version, created_at, updated_at) "
            "VALUES (?,?,?,?,?,1,?,?)",
            (project_id, realm_id, project_id, project_id, "{}", timestamp, timestamp),
        )
        spec_digest = store._store_spec({})
        for run_id, task_id in run_tasks:
            store.conn.execute(
                "INSERT INTO runs (id, project_id, capability, spec_digest, status, created_at, updated_at) "
                "VALUES (?,?,?,?,?,?,?)",
                (run_id, project_id, "rendering.render", spec_digest, "completed", timestamp, timestamp),
            )
            store.conn.execute(
                "INSERT INTO tasks (id, run_id, capability, spec_digest, status, created_at, updated_at) "
                "VALUES (?,?,?,?,?,?,?)",
                (task_id, run_id, "rendering.render", spec_digest, "completed", timestamp, timestamp),
            )
    return store
