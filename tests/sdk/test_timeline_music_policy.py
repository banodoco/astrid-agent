"""The music bed on a re-time (W2): by default (keep_end) the bed is cut at a bar with a crossfade, so its
ending lands on the film's end; plays_on keeps it whole (the old behaviour, now opt-in).

Offline: the tiny fixture, with its music bed run to the film's end (in the fixture it stops at 4 s of an
8 s film, so there would be no ending to keep). Times are seconds; the crossfade is 0.25 s.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from astrid.sdk import timeline_intent as intent
from astrid.sdk.timeline._common import TimelineEditError
from astrid.sdk.timeline_checkout import Checkout
from astrid.sdk.timeline.model import retime_scope

TINY = Path(__file__).resolve().parents[1] / "fixtures" / "timeline_editing" / "tiny.json"
FRAME = 1 / 30


def tiny():
    data = json.loads(TINY.read_text(encoding="utf-8"))
    bed = next(c for c in data["shots"]["A"]["internal_timeline"]["clips"] if c["id"] == "a-music")
    bed["to"] = 8.0  # the bed runs to the film's end (8 s)
    return Checkout(data)


def bed(tl):
    return sorted((c for c in tl.clips() if c.track == "music"), key=lambda c: c.start)


def test_the_default_is_keep_end_and_the_bed_ends_on_the_films_end_after_a_removal():
    tl = tiny()
    assert tl.music == "keep_end"
    report = tl.remove_line("s1")  # removes 0.5 s .. 5.9 s: 5.4 s of film
    assert tl.duration == pytest.approx(2.6, abs=FRAME)
    pieces = bed(tl)
    assert len(pieces) == 2
    assert pieces[0].data["params"]["fadeOut"] == 0.25 and pieces[1].data["params"]["fadeIn"] == 0.25
    assert pieces[-1].end == pytest.approx(tl.duration, abs=FRAME)  # its ending is the film's end
    assert pieces[1].data["from"] == pytest.approx(5.775)  # skipped the removed 5.4 s (less the crossfade)
    assert any(line.startswith("music a-music: cut at the change") and "skips 5.40 s" in line for line in report)


def test_plays_on_keeps_the_bed_whole_as_before():
    tl = tiny()
    tl.music = "plays_on"
    tl.remove_line("s1")
    pieces = bed(tl)
    assert len(pieces) == 1 and pieces[0].data["from"] == 0.0 and pieces[0].data["to"] == 8.0
    assert pieces[0].end == pytest.approx(8.0)  # the film is 2.6 s: the bed runs past it, as it always did
    assert tl.duration == pytest.approx(2.6, abs=FRAME)


def test_a_per_call_policy_wins_over_the_checkout_and_is_restored():
    tl = tiny()
    tl.music = "plays_on"
    tl.remove_line("s1", music="keep_end")  # this call only
    assert len(bed(tl)) == 2 and tl.music == "plays_on"
    tl2 = tiny()
    tl2.music = "keep_end"
    tl2.remove_line("s1", music="plays_on")
    assert len(bed(tl2)) == 1 and tl2.music == "keep_end"


def test_the_old_internal_names_are_accepted_silently():
    tl = tiny()
    tl.music = "whole"
    assert tl.music == "plays_on"
    tl.music = "seams"
    assert tl.music == "keep_end"
    tl.music = "plays-on"  # the CLI spelling
    assert tl.music == "plays_on"


def test_a_bad_policy_is_refused_and_lists_the_two():
    tl = tiny()
    with pytest.raises(TimelineEditError) as err:
        tl.music = "loop"
    assert "keep_end" in str(err.value) and "plays_on" in str(err.value)
    assert tl.music == "keep_end"  # the bad value changed nothing
    with pytest.raises(TimelineEditError):
        tl.remove_line("s1", music="sometimes")
    assert tl.music == "keep_end" and len(bed(tl)) == 1  # refused before anything moved


def test_an_opened_gap_repeats_the_music_and_the_ending_still_lands():
    tl = tiny()
    tl.insert_time(2.0, 1.0)  # open 1 s inside the bed: the music repeats it
    assert tl.duration == pytest.approx(9.0, abs=FRAME)
    pieces = bed(tl)
    assert len(pieces) == 2
    assert pieces[1].data["from"] == pytest.approx(0.875)  # repeats 1 s (less the crossfade)
    assert pieces[-1].end == pytest.approx(tl.duration, abs=FRAME)


def test_a_removal_after_the_bed_moves_its_ending_too():
    tl = tiny()
    tl.remove_line("s2")  # the last line: 0.8 s removed in shot B, where the bed still runs
    assert tl.duration == pytest.approx(7.2, abs=FRAME)
    assert bed(tl)[-1].end == pytest.approx(tl.duration, abs=FRAME)


# ---- W2b: one music seam per edit (the cuts are made at the end of the re-time, coalesced within a bar) ----------

def ends_on_the_films_end(tl):
    assert bed(tl)[-1].end == pytest.approx(tl.duration, abs=FRAME)


def bed_notes(tl):
    return [n for n in tl.notes if n.startswith("music a-music:")]


def test_a_line_removal_makes_exactly_one_seam_in_the_bed():
    tl = tiny()
    report = tl.remove_line("s1")  # the window's removal and the gap re-flow after it: one seam, not two
    assert len(bed(tl)) == 2  # two pieces from one bed: one seam
    assert sum(1 for line in report if line.startswith("music a-music:")) == 1
    ends_on_the_films_end(tl)


def test_changes_far_apart_in_one_operation_make_two_seams():
    tl = tiny()
    with retime_scope(tl):  # one re-time: an opening at 1.0 s and a removal at 6.0 s, more than a bar apart
        tl.insert_time(1.0, 0.5)
        tl.ripple_delete(6.0, 6.3)
    assert tl.duration == pytest.approx(8.2, abs=FRAME)
    assert len(bed(tl)) == 3  # two seams
    assert len(bed_notes(tl)) == 2
    ends_on_the_films_end(tl)


def test_changes_within_a_bar_are_one_seam_with_the_net_delta():
    tl = tiny()  # no downbeats in the fixture: a bar is 2.0 s
    # Splitting and repeating the take also repeats "viral"; choose explicitly.
    intent.set_on(tl.clip('a-rocket').data, '"viral" #1')
    with retime_scope(tl):
        tl.ripple_delete(1.0, 1.2)  # -0.2 s at 1.0 s
        tl.insert_time(1.1, 0.5)  # +0.5 s at 1.1 s: 0.1 s from the first
    assert tl.duration == pytest.approx(8.3, abs=FRAME)
    assert len(bed(tl)) == 2  # one seam
    assert len(bed_notes(tl)) == 1 and "repeats 0.30 s" in bed_notes(tl)[0]  # the net +0.3 s
    ends_on_the_films_end(tl)


def test_an_exception_inside_the_operation_leaves_the_bed_untouched():
    tl = tiny()
    with pytest.raises(RuntimeError, match="boom"):
        with retime_scope(tl):
            tl.ripple_delete(1.0, 2.0)
            raise RuntimeError("boom")
    assert len(bed(tl)) == 1 and bed(tl)[0].start == 0.0
    assert bed(tl)[0].data["from"] == 0.0 and bed(tl)[0].data["to"] == 8.0
    assert bed_notes(tl) == []
    assert tl._scope is None  # the scope is closed: the next edit opens a new one


def test_an_exception_in_a_real_edit_leaves_the_bed_untouched(monkeypatch):
    tl = tiny()

    def boom(self):
        raise RuntimeError("boom")

    monkeypatch.setattr(Checkout, "retime", boom)  # the re-flow fails after the time has moved
    with pytest.raises(RuntimeError, match="boom"):
        tl.remove_line("s1")
    assert len(bed(tl)) == 1 and bed(tl)[0].data["to"] == 8.0
    assert tl._scope is None
