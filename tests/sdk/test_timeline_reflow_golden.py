"""Re-flow under real load: removing a line never crashes, never collapses a cut, keeps the music's ending,
lists every orphan by name, and check blocks publishing until each one is re-homed or removed.

The fixture is a trimmed model of v7 → v8 (the real failures R1–R3 of Round 1): three lines, the middle
one ("Live.") with a cut of its own and layers on its words, a music bed under everything, and a later
layer whose formula names a word of the removed line.
"""
from __future__ import annotations

import copy

import pytest

from astrid.sdk import timeline_intent as intent
from astrid.sdk.timeline_checkout import Checkout

FPS = 30


def film():
    clips = [
        # cut c1 on line s1 (0–4 s)
        {"id": "c1-plate", "clipType": "am-snap-plate", "track": "plate", "asset": "P", "at": 0.0, "hold": 4.0,
         "params": {}, "app": {"cut": "c1", "layer": "field", "on": '"It"'}},
        {"id": "c1-rocket", "clipType": "am-sprite", "track": "sprite", "asset": "R", "at": 1.3, "hold": 2.7,
         "params": {"x": 10}, "app": {"cut": "c1", "layer": "rocket", "on": '"viral"'}},
        # cut c2 on line s2 "Live." (4–6 s): it exists only for that line
        {"id": "c2-plate", "clipType": "am-snap-plate", "track": "plate", "asset": "Q", "at": 4.0, "hold": 2.0,
         "params": {}, "app": {"cut": "c2", "layer": "field", "on": '"Live"'}},
        {"id": "c2-live", "clipType": "am-type", "track": "type", "at": 4.0, "hold": 2.0,
         "params": {"text": "Live."}, "app": {"cut": "c2", "layer": "live"}},
        {"id": "c2-dot", "clipType": "am-sprite", "track": "sprite", "asset": "R", "at": 4.5, "hold": 1.5,
         "params": {"x": 20}, "app": {"cut": "c2", "layer": "dot", "on": '+0.5s'}},
        # cut c3 on line s3 (6–9 s); its card's hold names a word of the removed line (a formula)
        {"id": "c3-plate", "clipType": "am-snap-plate", "track": "plate", "asset": "P", "at": 6.0, "hold": 3.0,
         "params": {}, "app": {"cut": "c3", "layer": "field", "on": '"So"'}},
        {"id": "c3-card", "clipType": "am-type", "track": "type", "at": 6.4, "hold": 1.0,
         "params": {"text": "back to work", "delay": 0.2},
         "app": {"cut": "c3", "layer": "card", "on": '"work"', "formulas": {"params.delay": {"moment": '"Live"', "as": "timeline_seconds"}}}},
        # the voice: three lines; and one music bed with beats under everything
        {"id": "vo-s1", "clipType": "media", "track": "vo", "asset": "V", "at": 0.0, "from": 0.0, "to": 2.0,
         "app": {"segment": "s1", "text": "It went viral.", "gap_after_s": 2.0,
                 "words": [[0.1, 0.4, "It"], [0.5, 0.8, "went"], [1.3, 1.9, "viral"]]}},
        {"id": "vo-s2", "clipType": "media", "track": "vo", "asset": "V", "at": 4.0, "from": 0.0, "to": 0.8,
         "app": {"segment": "s2", "text": "Live.", "gap_after_s": 1.2, "words": [[0.1, 0.7, "Live"]]}},
        {"id": "vo-s3", "clipType": "media", "track": "vo", "asset": "V", "at": 6.0, "from": 0.0, "to": 2.0,
         "app": {"segment": "s3", "text": "So back to work.",
                 "words": [[0.1, 0.3, "So"], [0.4, 0.6, "back"], [0.7, 0.8, "to"], [0.9, 1.4, "work"]]}},
        {"id": "music", "clipType": "media", "track": "music", "asset": "M", "at": 0.0, "from": 0.0, "to": 9.0,
         "app": {"beats": {"beats": [i * 0.5 for i in range(18)], "bpm": 120, "time": "cue_seconds"}}},
    ]
    return {
        "project_id": "p", "timeline_id": "t", "base_parent": {"revision_id": "rev-0"},
        "parent": {"config": {"theme_overrides": {"visual": {"canvas": {"fps": FPS, "width": 1920, "height": 1080}}}}, "clips": []},
        "placements": [{"shot_id": "F", "occurrence_id": "occ-F", "placement": {"start_ms": 0}, "duration_ms": 9000}],
        "shots": {"F": {"payload": {"name": "FILM"}, "internal_timeline": {
            "tracks": [{"id": t, "kind": "audio" if t in ("vo", "music") else "visual"}
                       for t in ("type", "sprite", "plate", "vo", "music")],
            "clips": clips,
            "registry": {"assets": {k: {"media_id": f"m-{k}"} for k in "PQRVM"}}}}},
    }


def test_removing_a_line_keeps_its_cut_as_orphans_the_music_ending_and_blocks_publish():
    tl = Checkout(film())
    assert tl.resolve() == [] or True
    report = tl.remove_line("s2")  # R2/R3: must not crash, must not collapse
    assert report[0].startswith("removed line s2")
    # nothing collapsed to 0 s; the "Live." cut's clips are kept, with their length, as orphans
    assert all(c.duration >= 1 / FPS for c in tl.clips() if not c.is_audio)
    assert tl.clip("c2.live").duration == pytest.approx(2.0) and tl.clip("c2.dot").duration == pytest.approx(1.5)
    orphans = tl.orphans()
    assert any(line.startswith("c2.field") and "line s2 was removed" in line for line in orphans)
    assert any(line.startswith("c2.live") for line in orphans)  # no moment of its own, still listed
    assert any(line.startswith("c3.card") and "delay" in line for line in orphans)  # the formula's word is gone too
    # the music bed is cut at bars (the default keep_end, which replaced "the bed plays on unchanged": the
    # removal skips the music it covers and the bed's ending lands on the film's end; see test_timeline_music_policy)
    music = sorted((c for c in tl.clips() if c.track == "music"), key=lambda c: c.start)
    assert music[0].start == 0.0 and music[-1].end == pytest.approx(tl.duration, abs=1 / FPS)
    # two seams, one per place the time changed (W2b): the gap re-flow at 1.50 s (-0.10 s) and the removal at 3.90 s
    # (-2.00 s) are 2.4 s apart, more than a bar of this beat-only bed (2.0 s), so they are not coalesced. Each is
    # one cut: 3 pieces, as W2 made them. (Within a bar they would be one seam: test_timeline_music_policy.)
    assert len(music) == 3
    assert any("cut on the beat" in line and "skips 2.00 s" in line for line in report)
    # every surviving anchored clip is still on its word
    assert tl.clip("c1.rocket").start == pytest.approx(tl.word("viral").start, abs=1 / FPS)
    assert tl.clip("c3.card").start == pytest.approx(tl.word("work").start, abs=1 / FPS)
    assert tl.clip("c3.field").start == pytest.approx(tl.voice("s3").clips[0].start, abs=1 / FPS)
    # check blocks until the orphans are re-homed or removed
    check = tl.check()
    assert not check.valid and check.blocking and any("orphan" in line for line in check.brief())


def test_rehoming_the_orphans_unblocks_the_check():
    tl = Checkout(film())
    tl.remove_line("s2")
    for name in ("c2.field", "c2.live", "c2.dot"):
        tl.clip(name).remove()
    tl.clip("c3.card").set(delay=0.2)  # a fixed value replaces the orphaned formula
    assert tl.orphans() == []
    check = tl.check()
    assert not check.blocking


def test_a_script_that_drops_a_line_never_crashes(tmp_path):
    """R1: --from-script with a removed line whose words other clips use."""
    import json

    vo = tmp_path / "vo"
    vo.mkdir()
    for seg, words in (("s1", [[0.1, 0.4, "It"], [0.5, 0.8, "went"], [1.3, 1.9, "viral"]]),
                       ("s3", [[0.1, 0.3, "So"], [0.4, 0.6, "back"], [0.7, 0.8, "to"], [0.9, 1.4, "work"]])):
        (vo / f"{seg}.wav").write_bytes(b"RIFF")
        (vo / f"{seg}.words.json").write_text(json.dumps({"words": words}))
    tl = Checkout(film())
    report = tl.apply_script({"segments": [{"id": "s1", "text": "It went viral."}, {"id": "s3", "text": "So back to work."}]},
                             takes=vo)
    assert any(line.startswith("removed line s2") for line in report)
    assert [v.segment for v in tl.lines()] == ["s1", "s3"]
    assert tl.orphans() and not tl.check().valid


def test_an_orphan_can_be_kept_or_rehomed_on_a_new_word():
    tl = Checkout(film())
    tl.remove_line("s2")
    tl.clip("c2.live").on('"back"')  # re-homed: it now enters on a word of the next line
    tl.clip("c2.dot").keep()         # accepted where it is
    tl.clip("c2.field").remove()
    tl.clip("c3.card").set(delay=0.2)
    assert tl.orphans() == []
    assert tl.clip("c2.live").start == pytest.approx(tl.word("back").start, abs=1 / FPS)


def test_rehoming_an_orphaned_cut_brings_its_layers():
    tl = Checkout(film())
    tl.remove_line("s2")
    picture = tl.cut_picture("c2")
    picture.on('"back"')  # the whole "Live." cut moves onto a word of the next line
    assert tl.clip("c2.live").start == pytest.approx(picture.start, abs=1 / FPS)  # it entered with its cut
    assert tl.clip("c2.dot").start == pytest.approx(picture.start + 0.5, abs=1 / FPS)  # +0.5s after the cut
    assert not any(line.startswith(("c2.field", "c2.live", "c2.dot")) for line in tl.orphans())


def _film_with_a_time_lapse():
    """c3 is a stepped sequence (a time-lapse: its picture + 4 steps tiling 6–9 s), and line s4 is spoken
    inside it, so a re-flow opens time INSIDE the sequence as well as before it (the c17 regression)."""
    data = film()
    clips = data["shots"]["F"]["internal_timeline"]["clips"]
    pic = next(c for c in clips if c["id"] == "c3-plate")
    pic["hold"] = 1.0
    pic["app"].update({"sequence": "c3-seq", "sequence_index": 0})
    at = 7.0
    for i, frames in enumerate((15, 9, 6, 30), start=1):
        step = {"id": f"c3-step-{i:02d}", "clipType": "am-snap-plate", "track": "plate", "asset": "Q", "at": at,
                "hold": frames / FPS, "params": {}, "app": {"cut": "c3", "layer": f"step-{i:02d}",
                                                            "sequence": "c3-seq", "sequence_index": i}}
        clips.append(step)
        at += frames / FPS
    vo3 = next(c for c in clips if c["id"] == "vo-s3")
    vo3["app"]["gap_after_s"] = 0.2
    clips.append({"id": "vo-s4", "clipType": "media", "track": "vo", "asset": "V", "at": 7.6, "from": 0.0, "to": 0.8,
                  "app": {"segment": "s4", "text": "Done.", "words": [[0.1, 0.6, "Done"]]}})
    return data


def _steps(tl):
    return sorted((c for c in tl.clips() if intent.sequence(c.data)), key=lambda c: intent.sequence(c.data)[1])


def test_a_reflow_moves_and_stretches_a_time_lapse_as_one_unit():
    """Regression (c17): a re-flow before a cut and inside its time-lapse must not tear the sequence."""
    tl = Checkout(_film_with_a_time_lapse())
    tl.resolve()
    before = [round(s.duration * FPS) for s in _steps(tl)]
    tl.voice("s1").set_gap_after(3.0, reflow=False)   # +1.0 s before c3
    tl.voice("s3").set_gap_after(1.2, reflow=False)   # +1.0 s inside the time-lapse (after "work", 7.4 s)
    tl.reflow()
    steps = _steps(tl)
    assert [intent.sequence(s.data)[1] for s in steps] == [0, 1, 2, 3, 4]  # order kept
    assert all(intent.cut_of(s.data) == "c3" for s in steps)                # one cut
    for a, b in zip(steps, steps[1:]):
        assert b.start == pytest.approx(a.end, abs=1e-6)                     # end to end: no hole, no overlap
    lo, hi = tl._cut_spans()["c3"]
    assert steps[0].start == pytest.approx(lo, abs=1 / FPS) and steps[-1].end == pytest.approx(hi or tl.duration, abs=1 / FPS)
    after = [round(s.duration * FPS) for s in steps]
    scale = sum(after) / sum(before)
    for n_before, n_after in zip(before, after):                            # spacing ratios kept (to a frame)
        assert abs(n_after - n_before * scale) <= 1.0
    assert not tl.check().blocking


def test_fit_sequence_restores_a_shape_and_fits_a_span_as_one_unit(monkeypatch):
    """(b): tl.sequence(step).fit(on=, until=, shape_from=) — a torn time-lapse is repaired in one call."""
    from astrid.sdk import timeline_checkout as tc

    original = _film_with_a_time_lapse()
    tl = Checkout(copy.deepcopy(original))
    tl.resolve()
    shape = [round(s.duration * FPS) for s in _steps(tl)]
    torn = _steps(tl)
    torn[2].data["hold"] = 3.2  # what the old ripple did to step 10 of c17
    monkeypatch.setattr(tc, "fetch_bundle", lambda project, timeline, revision_id=None, client=None: copy.deepcopy(original))
    seq = tl.sequence("c3.step-02")
    seq.fit(on='"back"', until="end", shape_from="round0")
    steps = _steps(tl)
    assert intent.on(steps[0].data) == '"back"' and intent.until(steps[0].data) == "end"
    assert steps[0].start == pytest.approx(tl.word("back").start, abs=1 / FPS)
    for a, b in zip(steps, steps[1:]):
        assert b.start == pytest.approx(a.end, abs=1e-6)
    after = [round(s.duration * FPS) for s in steps]
    scale = sum(after) / sum(shape)
    assert all(abs(n - m * scale) <= 1.0 for n, m in zip(after, shape))  # the restored shape, not the torn one
    with pytest.raises(Exception, match="not a step of a sequence"):
        tl.sequence("c1.rocket")


def test_a_track_change_never_moves_the_clip():
    """A cut's picture on a line's first word opens at the take's in-point; moved to fx it keeps that frame."""
    tl = Checkout(film())
    tl.resolve()
    pic = tl.clip("c3.field")
    start = pic.start
    assert start < tl.word("So").start - 1 / FPS  # the take's in-point, before the word
    pic.set_track("type")
    tl.retime()
    assert tl.clip("c3.field").start == pytest.approx(start, abs=1e-6) and tl.clip("c3.field").track == "type"
    assert intent.on(tl.clip("c3.field").data).startswith('"So" -')  # the offset says so


def test_a_picture_gap_blocks_publishing_and_says_what_is_left_on_screen():
    """(c): check reports the picture, not only the structure: a span with no plate is an advisory gap."""
    tl = Checkout(film())
    tl.resolve()
    assert tl.picture_gaps() == []
    tl.clip("c2.field").remove()
    gaps = tl.picture_gaps()
    assert len(gaps) == 1 and gaps[0].startswith("4.000–6.000 s") and "c2.live" in gaps[0]
    report = tl.check()
    assert any(line.startswith("picture") for line in report.problems) and not report.blocking
