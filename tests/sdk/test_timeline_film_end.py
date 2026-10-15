"""Where the film ends, as a moment (W3): tl.film_end(moment) holds the last picture and the layers that end with
the film to the moment, and the music bed keeps its ending (keep_end) or stays whole (plays_on).

Offline: the tiny fixture (the film is 8 s; shot B is 4-8 s; the last spoken word, "now", ends at 6.7 s).
"""
from __future__ import annotations

import copy
import json
from pathlib import Path

import pytest

from astrid.sdk import timeline_intent as intent
from astrid.sdk.timeline._common import TimelineEditError
from astrid.sdk.timeline_checkout import Checkout
from astrid.sdk.timeline import commands, journal

TINY = Path(__file__).resolve().parents[1] / "fixtures" / "timeline_editing" / "tiny.json"
FRAME = 1 / 30
AFTER_NOW = 'after "now"'


def tiny(*, bed_to: float = 8.0, layer_in_b: bool = False):
    data = json.loads(TINY.read_text(encoding="utf-8"))
    bed = next(c for c in data["shots"]["A"]["internal_timeline"]["clips"] if c["id"] == "a-music")
    bed["to"] = bed_to
    if layer_in_b:  # a sprite in the last cut that runs to the film's end with the picture
        data["shots"]["B"]["internal_timeline"]["clips"].append({
            "id": "b-glow", "clipType": "am-sprite", "track": "sprite", "asset": "R", "at": 0.0, "hold": 4.0,
            "params": {"x": 5}, "app": {"cut": "c2", "layer": "glow"}})
    return Checkout(data)


def bed(tl):
    return sorted((c for c in tl.clips() if c.track == "music"), key=lambda c: c.start)


def shot_b_duration_ms(tl):
    return next(p for p in tl.document()["placements"] if p["shot_id"] == "B")["duration_ms"]


def test_extending_by_a_second_moves_the_film_end_and_the_last_picture():
    tl = tiny()
    assert tl.duration == pytest.approx(8.0, abs=FRAME)
    notes = tl.film_end(f'{AFTER_NOW} +2.3s')  # 6.7 + 2.3 = 9.0
    assert tl.duration == pytest.approx(9.0, abs=FRAME)
    plate = tl.clip("c2.plate")
    assert plate.end == pytest.approx(9.0, abs=FRAME)
    assert "now" in intent.until(plate.data)  # the moment is stored on the clip, so a re-flow keeps it
    assert shot_b_duration_ms(tl) == 5000  # the placement follows its last picture
    assert any(n.startswith("the film now ends at 9.00 s (was 8.00 s)") and "c2.plate" in n for n in notes)


def test_a_layer_that_ended_with_the_film_ends_with_the_new_end_too():
    tl = tiny(layer_in_b=True)
    assert tl.clip("c2.glow").end == pytest.approx(8.0, abs=FRAME)
    tl.film_end(f'{AFTER_NOW} +2.3s')
    assert tl.clip("c2.glow").end == pytest.approx(9.0, abs=FRAME)
    assert tl.clip("c1.plate").end == pytest.approx(4.0, abs=FRAME)  # an earlier cut is untouched


def test_the_music_ending_lands_on_the_new_end_when_the_bed_spans_it():
    tl = tiny()  # the bed runs to 8 s: it spans the old end
    tl.film_end(f'{AFTER_NOW} +2.3s')
    pieces = bed(tl)
    assert len(pieces) == 2  # repeats from a bar before its ending
    assert pieces[-1].end == pytest.approx(9.0, abs=FRAME)
    assert pieces[-1].end == pytest.approx(tl.duration, abs=FRAME)


def test_shortening_moves_the_end_back_and_the_music_skips_to_it():
    tl = tiny()
    tl.film_end(f'{AFTER_NOW} +0.5s')  # 6.7 + 0.5 = 7.2
    assert tl.duration == pytest.approx(7.2, abs=FRAME)
    assert tl.clip("c2.plate").end == pytest.approx(7.2, abs=FRAME)
    assert shot_b_duration_ms(tl) == 3200
    pieces = bed(tl)
    assert len(pieces) == 2 and pieces[-1].end == pytest.approx(7.2, abs=FRAME)


def test_a_bed_that_stops_before_the_old_end_is_left_alone():
    tl = tiny(bed_to=4.0)  # stops at 4 s of an 8 s film: no ending to keep
    tl.film_end(f'{AFTER_NOW} +2.3s')
    assert len(bed(tl)) == 1 and bed(tl)[0].data["from"] == 0.0 and bed(tl)[0].data["to"] == 4.0


def test_plays_on_leaves_the_bed_untouched():
    tl = tiny()
    notes = tl.film_end(f'{AFTER_NOW} +2.3s', music="plays_on")
    assert tl.duration == pytest.approx(9.0, abs=FRAME)
    pieces = bed(tl)
    assert len(pieces) == 1 and pieces[0].data["from"] == 0.0 and pieces[0].data["to"] == 8.0
    assert tl.music == "keep_end"  # the call's policy is restored
    assert any("plays on unchanged" in n for n in notes)


def test_a_moment_before_the_last_word_is_refused_and_changes_nothing():
    tl = tiny()
    before = json.dumps(tl.document(), sort_keys=True)
    with pytest.raises(TimelineEditError, match="can't end before its last word, which ends at 6.70 s"):
        tl.film_end('"viral"')  # the word at 0.8 s
    assert json.dumps(tl.document(), sort_keys=True) == before


def test_a_plain_number_is_refused_with_the_hint():
    tl = tiny()
    with pytest.raises(TimelineEditError, match='write a moment, e.g. after "Astrid" in n27 \\+2.5s'):
        tl.film_end(9.0)
    with pytest.raises(TimelineEditError, match="write a moment"):
        tl.film_end("9.0s")
    assert tl.duration == pytest.approx(8.0, abs=FRAME)


def test_the_current_end_is_a_no_change():
    tl = tiny()
    before = json.dumps(tl.document(), sort_keys=True)
    notes = tl.film_end(f'{AFTER_NOW} +1.3s')  # 6.7 + 1.3 = 8.0: where the film already ends
    assert any("no change" in n for n in notes)
    assert json.dumps(tl.document(), sort_keys=True) == before
    assert tl.music == "keep_end"


def test_the_moment_is_stored_so_a_re_flow_keeps_the_new_end():
    tl = tiny()
    tl.film_end(f'{AFTER_NOW} +2.3s')
    tl.retime()
    assert tl.clip("c2.plate").end == pytest.approx(9.0, abs=FRAME)
    assert tl.duration == pytest.approx(9.0, abs=FRAME)


@pytest.mark.parametrize("cut_free", [True, False])
def test_timeline_end_offset_keeps_its_basis_through_retime_save_and_replay(tmp_path, cut_free):
    tl = tiny(layer_in_b=True)
    if cut_free:
        for clip in tl.clips():
            intent.set_cut(clip.data, None)
        assert not tl._cut_groups()
    original = copy.deepcopy(tl.document())
    path = tmp_path / "film.checkout.json"
    tl.save(path, quiet=True)
    tl.film_end("end +1s")
    assert tl.duration == pytest.approx(9.0, abs=FRAME)
    assert tl.clip("b-plate").end == pytest.approx(9.0, abs=FRAME)
    assert tl.clip("b-glow").end == pytest.approx(9.0, abs=FRAME)
    assert intent.until(tl.clip("b-plate").data) == "end +1s"
    tl.save(quiet=True)
    journal_path = path.with_suffix(path.suffix + ".journal.jsonl")
    record = journal.read(journal_path)[-1]
    assert journal.verify(journal_path) == []
    assert record.calls[0]["bindings"]["end +1s"] == "occurrence:occ-B/clip:b-plate"

    reloaded = Checkout.load(path)
    for _ in range(2):
        reloaded.retime()
        assert reloaded.duration == pytest.approx(9.0, abs=FRAME)
        assert reloaded.clip("b-plate").end == pytest.approx(9.0, abs=FRAME)
        assert reloaded.clip("b-glow").end == pytest.approx(9.0, abs=FRAME)
        assert not reloaded.orphans()
    if cut_free:
        assert not reloaded._cut_groups()
        assert all(intent.cut_of(c.data) is None for c in reloaded.clips())

    replayed = Checkout(original)
    commands.run(replayed, record.calls[0])
    replayed.retime()
    assert replayed.document() == reloaded.document()


def test_a_clip_end_still_needs_its_own_cut_after_timeline_film_end():
    tl = tiny()
    picture = tl.clip("b-plate")
    intent.set_cut(picture.data, None)
    tl.film_end("end +1s")
    before = copy.deepcopy(tl.document())
    with pytest.raises(TimelineEditError, match="belongs to no cut"):
        picture.until("end +1s")
    assert tl.document() == before


def test_replacing_timeline_end_with_an_explicit_cut_end_restores_cut_scope():
    tl = tiny()
    tl.film_end("end +1s")
    tl.clip("b-plate").until("end of c2")
    tl.retime()
    assert intent.until_film_end(tl.clip("b-plate").data) is None
    assert tl.duration == pytest.approx(9.0, abs=FRAME)


def test_a_failed_moment_leaves_the_bed_untouched():
    tl = tiny()
    with pytest.raises(TimelineEditError):
        tl.film_end('"viral"')
    assert len(bed(tl)) == 1 and bed(tl)[0].data["to"] == 8.0
    assert tl._scope is None


def test_a_new_music_piece_never_reuses_an_id_the_shot_already_has():
    # A piece left by an earlier re-flow (here kept as an orphan) already holds "a-music:b": cutting the
    # bed again must not create a second clip with that id (14 Oct: the film's shot became invalid).
    data = json.loads(TINY.read_text(encoding="utf-8"))
    next(c for c in data["shots"]["A"]["internal_timeline"]["clips"] if c["id"] == "a-music")["to"] = 8.0
    data["shots"]["A"]["internal_timeline"]["clips"].append({
        "id": "a-music:b", "clipType": "am-sprite", "track": "sprite", "asset": "R", "at": 0.0, "hold": 0.5,
        "params": {}, "app": {"cut": "c1", "layer": "left-over"}})
    tl = Checkout(data)
    tl.film_end(f'{AFTER_NOW} +2.3s')
    ids = [c["id"] for c in tl.document()["shots"]["A"]["internal_timeline"]["clips"]]
    assert len(ids) == len(set(ids))
    assert len(bed(tl)) == 2 and bed(tl)[-1].end == pytest.approx(9.0, abs=FRAME)
