"""The film's length follows its content: extending or shortening the last cut re-times the last shot's placement,
and check blocks any visible clip that would run past its shot (the render stops at the shot's end)."""
from __future__ import annotations

import copy
import json
from pathlib import Path

import pytest

from astrid.sdk.timeline_checkout import Checkout

TINY = Path(__file__).resolve().parents[1] / "fixtures" / "timeline_editing" / "tiny.json"


def tiny() -> dict:
    return json.loads(TINY.read_text(encoding="utf-8"))


def placement(tl: Checkout, sid: str) -> int:
    return next(int(p["duration_ms"]) for p in tl.document()["placements"] if p["shot_id"] == sid)


def length_lines(report) -> list[str]:
    return [line for line in report.blocking if line.startswith("length")]


def test_extending_the_last_picture_lengthens_the_last_shot():
    tl = Checkout(tiny())
    tl.clip("c2.plate").extend(1.0)
    tl.retime()
    assert tl.duration == pytest.approx(9.0)
    assert placement(tl, "B") == 5000
    assert placement(tl, "A") == 4000  # the first shot is not resized


def test_shortening_the_last_picture_shortens_the_last_shot():
    tl = Checkout(tiny())
    tl.clip("c2.plate").extend(-1.0)
    tl.check()
    assert tl.duration == pytest.approx(7.0)
    assert placement(tl, "B") == 3000


def test_check_syncs_the_placement_and_says_so_in_words():
    tl = Checkout(tiny())
    tl.clip("c2.plate").extend(1.0)
    report = tl.check()
    assert placement(tl, "B") == 5000
    assert "the film now runs 9.00 s (was 8.00 s): the last shot follows its last picture" in report.problems


def test_a_layer_longer_than_its_middle_shot_blocks_check():
    data = tiny()
    Checkout._pin_initial_base(data)
    tl = Checkout(data)
    # Add the overrun *after* opening the checkout. The release contract reports
    # unchanged pre-existing overruns without blocking unrelated new edits, but
    # a newly introduced six-second layer in this four-second shot must block.
    tl.add("am-type", at=0.0, hold=6.0, track="type", cut="c1", layer="card",
           clip_id="a-card", params={"text": "Long."})
    report = tl.check()
    lines = length_lines(report)
    assert any("c1.card ends at 6.00 s, after its shot ends at 4.00 s" in line for line in lines), lines
    assert report.valid is False
    assert placement(tl, "A") == 4000  # middle shots are not resized


def test_audio_past_the_end_is_not_flagged():
    data = tiny()
    shot_b = data["shots"]["B"]["internal_timeline"]["clips"]
    shot_b.append({"id": "b-bed", "clipType": "media", "track": "music", "asset": "Q", "at": 0.0,
                   "from": 0.0, "to": 6.0})
    report = Checkout(data).check()
    assert not any("b-bed" in line for line in length_lines(report))


def test_save_writes_the_synced_placement(tmp_path: Path):
    tl = Checkout(tiny())
    tl.clip("c2.plate").extend(1.0)
    target = tmp_path / "film.json"
    tl.save(target, quiet=True)
    saved = json.loads(target.read_text(encoding="utf-8"))
    row = next(p for p in saved["placements"] if p["shot_id"] == "B")
    assert int(row["duration_ms"]) == 5000


def test_save_repairs_a_source_backed_music_bed_ending_before_the_film(tmp_path: Path):
    data = tiny()
    internal = data["shots"]["B"]["internal_timeline"]
    internal["registry"]["assets"]["M"] = {"duration": 4.0}
    internal["clips"].append({"id": "b-music", "clipType": "media", "track": "music", "asset": "M",
                              "at": 0.0, "from": 0.0, "to": 3.0, "hold": 3.0,
                              "params": {"fadeOut": 3.0}})
    tl = Checkout(data)

    tl.save(tmp_path / "film.json", quiet=True)

    bed = tl.clip("b-music")
    assert bed.end == pytest.approx(tl.duration)
    assert bed.data["to"] == pytest.approx(4.0)
    assert bed.data["params"]["fadeOut"] == 3.0


def test_save_repairs_music_when_existing_source_to_covers_the_film(tmp_path: Path):
    data = tiny()
    data["shots"]["B"]["internal_timeline"]["clips"].append(
        {"id": "b-music", "clipType": "media", "track": "music", "asset": "M",
         "at": 0.0, "from": 0.0, "to": 4.0, "hold": 3.0,
         "params": {"fadeIn": 1.0, "fadeOut": 2.0}}
    )
    tl = Checkout(data)

    tl.save(tmp_path / "film.json", quiet=True)

    bed = tl.clip("b-music")
    assert bed.end == pytest.approx(tl.duration)
    assert bed.data["to"] == pytest.approx(4.0)
    assert bed.data["params"] == {"fadeIn": 1.0, "fadeOut": 2.0}


def test_a_shot_without_a_picture_is_left_alone():
    data = tiny()
    data["shots"]["B"]["internal_timeline"]["clips"] = [
        c for c in data["shots"]["B"]["internal_timeline"]["clips"] if c["id"] != "b-plate"]
    tl = Checkout(data)
    tl.retime()
    assert placement(tl, "B") == 4000


def test_an_unchanged_film_gets_no_length_note():
    tl = Checkout(tiny())
    assert tl._sync_film_length() == []
    assert not any("the film now runs" in note for note in tl.notes)


def cut_ended_card() -> dict:
    data = tiny()
    card = next(c for c in data["shots"]["B"]["internal_timeline"]["clips"] if c["id"] == "b-type")
    card["app"].pop("for")  # cut-ended: the card runs to the end of its cut (the last cut)
    return data


def test_a_cut_ended_layer_in_the_last_cut_follows_an_extended_picture():
    tl = Checkout(cut_ended_card())
    tl.clip("c2.plate").extend(1.0)
    tl.retime()
    assert tl.clip("c2.card").end == pytest.approx(9.0)
    assert placement(tl, "B") == 5000


def test_a_cut_ended_layer_in_the_last_cut_follows_a_shortened_picture():
    tl = Checkout(cut_ended_card())
    tl.clip("c2.plate").extend(-1.0)
    tl.retime()
    assert tl.clip("c2.card").end == pytest.approx(7.0)
    assert placement(tl, "B") == 3000


def test_a_literal_for_in_the_last_cut_keeps_its_own_length():
    data = tiny()  # b-type keeps "for": 1.0
    tl = Checkout(data)
    tl.clip("c2.plate").extend(1.0)
    tl.retime()
    card = tl.clip("c2.card")
    assert card.start == pytest.approx(4.0)
    assert card.end == pytest.approx(5.0)


def test_an_until_layer_in_the_last_cut_keeps_its_moment():
    data = cut_ended_card()
    card = next(c for c in data["shots"]["B"]["internal_timeline"]["clips"] if c["id"] == "b-type")
    card["app"]["until"] = '"now"'  # ends on the spoken word "now" (b-vo, 5.5 s in film time)
    tl = Checkout(data)
    assert tl.time('"now"') == pytest.approx(6.4)
    tl.clip("c2.plate").extend(1.0)
    tl.retime()
    assert tl.clip("c2.card").end == pytest.approx(6.4)  # the moment, not the new film end (9.0)
