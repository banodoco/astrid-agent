"""Flat parent clips use the same explicit ripple helpers as shot timelines."""
from __future__ import annotations

from copy import deepcopy

import pytest

from astrid.sdk import timeline_intent as intent
from astrid.sdk.timeline._common import TimelineEditError
from astrid.sdk.timeline_checkout import Checkout


def checkout(clips):
    return Checkout({
        "project_id": "p",
        "timeline_id": "t",
        "parent": {
            "config": {
                "theme_overrides": {"visual": {"canvas": {"fps": 30, "width": 1920, "height": 1080}}},
                "tracks": [{"id": "V1", "kind": "visual"}, {"id": "A1", "kind": "audio"}],
                "registry": {"assets": {"picture": {"duration": 20}, "voice": {"duration": 20}}},
            },
            "clips": deepcopy(clips),
        },
        "placements": [],
        "shots": {},
    })


def clip(cid, track, start, source_in, source_out, *, hold=None, asset="picture"):
    row = {"id": cid, "clipType": "media", "track": track, "at": start, "from": source_in,
           "to": source_out, "asset": asset}
    if hold is not None:
        row["hold"] = hold
    return row


def test_insert_time_stretches_crossing_picture_and_splits_line_audio():
    picture = clip("picture", "V1", 3, 1, 5)
    voice = clip("voice", "A1", 2, 0, 6, hold=6, asset="voice")
    intent.set_line(voice, "s1", [[0.2, 0.5, "hello"], [1.0, 1.3, "world"]])
    tl = checkout([picture, voice, clip("later", "V1", 7, 0, 1)])
    handle = tl.clip("picture")
    data = handle.data

    assert tl.insert_time(4, 2) == pytest.approx(2)

    by_id = {item.id: item for item in tl.clips()}
    assert (by_id["picture"].start, by_id["picture"].end) == pytest.approx((3, 9))
    assert by_id["picture"].data["to"] == pytest.approx(7)
    assert handle.data is data and handle.end == pytest.approx(9)
    assert by_id["later"].start == pytest.approx(9)
    left, right = by_id["voice"], by_id["voice~4000"]
    assert (left.start, left.end, left.data["from"], left.data["to"]) == pytest.approx((2, 4, 0, 2))
    assert (right.start, right.end, right.data["from"], right.data["to"], right.data["hold"]) == pytest.approx((6, 10, 2, 6, 4))


def test_ripple_delete_splits_crossing_audio_and_preserves_source_windows():
    picture = clip("picture", "V1", 3, 1, 5)
    audio = clip("audio", "A1", 2, 1, 7, hold=6, asset="voice")
    tl = checkout([picture, audio, clip("later", "V1", 8, 0, 1)])

    assert tl.ripple_delete(4, 6) == pytest.approx(2)

    by_id = {item.id: item for item in tl.clips()}
    assert (by_id["picture"].start, by_id["picture"].end, by_id["picture"].data["from"],
            by_id["picture"].data["to"]) == pytest.approx((3, 5, 1, 3))
    assert (by_id["audio"].start, by_id["audio"].end, by_id["audio"].data["from"],
            by_id["audio"].data["to"]) == pytest.approx((2, 4, 1, 3))
    assert (by_id["audio~4000"].start, by_id["audio~4000"].end, by_id["audio~4000"].data["from"],
            by_id["audio~4000"].data["to"], by_id["audio~4000"].data["hold"]) == pytest.approx((4, 6, 5, 7, 2))
    assert by_id["later"].start == pytest.approx(6)


def test_close_gap_before_a_word_preserves_the_source_window_of_crossing_voice():
    voice = clip("voice", "A1", 0, 2, 10, hold=8, asset="voice")
    intent.set_line(voice, "s1", [[1, 1.3, "before"], [5, 5.3, "after"]])
    tl = checkout([voice, clip("later", "V1", 7, 0, 1)])

    assert tl.close_gap(before='"after"') == pytest.approx(3.7)

    by_id = {item.id: item for item in tl.clips()}
    assert (by_id["voice"].start, by_id["voice"].end, by_id["voice"].data["from"],
            by_id["voice"].data["to"]) == pytest.approx((0, 1.3, 2, 3.3))
    assert (by_id["voice~1300"].start, by_id["voice~1300"].end, by_id["voice~1300"].data["from"],
            by_id["voice~1300"].data["to"]) == pytest.approx((1.3, 4.3, 7, 10))
    assert by_id["later"].start == pytest.approx(3.3)
    assert tl.word("after").start == pytest.approx(1.3)


def test_ripple_delete_refuses_a_new_overlap_with_a_skipped_clip():
    tl = checkout([clip("skipped", "V1", 2, 0, 2), clip("later", "V1", 6, 0, 2)])
    skipped = tl.clip("skipped")
    before = deepcopy(tl.bundle["parent"]["clips"])

    with pytest.raises(TimelineEditError, match="into skipped clip"):
        tl.ripple_delete(2, 6, skip=[skipped.data])

    assert tl.bundle["parent"]["clips"] == before


def test_ripple_delete_refuses_flat_keep_inside_without_mutating():
    tl = checkout([clip("inside", "V1", 3, 0, 1)])
    before = deepcopy(tl.bundle["parent"]["clips"])

    with pytest.raises(TimelineEditError, match="no shot or cut to own an orphan"):
        tl.ripple_delete(2, 4, keep_inside=True)

    assert tl.bundle["parent"]["clips"] == before


def test_insert_refuses_a_moved_flat_anchor_without_mutating():
    anchored = clip("anchored", "V1", 6, 0, 1)
    intent.set_on(anchored, '"a word"')
    tl = checkout([anchored])
    before = deepcopy(tl.bundle["parent"]["clips"])

    with pytest.raises(TimelineEditError, match="anchored"):
        tl.insert_time(4, 1)

    assert tl.bundle["parent"]["clips"] == before


@pytest.mark.parametrize("operation", ["insert", "delete"])
@pytest.mark.parametrize("policy", ["keep_end", "plays_on"])
@pytest.mark.parametrize("held", [False, True])
def test_flat_music_policy_is_applied_once_and_repeat_edits_accept_split_windows(operation, policy, held):
    picture = clip("picture", "V1", 0, 0, 10)
    music = clip("music", "music", 0, 0, 10, hold=10 if held else None, asset="voice")
    intent.set_line(music, "s1", [[0.2, 0.4, "music"]])  # a bed tagged with transcript data stays a bed
    tl = checkout([picture, music])
    tl.bundle["parent"]["config"]["tracks"].append({"id": "music", "kind": "audio"})
    tl.music = policy

    if operation == "insert":
        tl.insert_time(4, 2)
        expected_picture_end = 12
    else:
        tl.ripple_delete(4, 6)
        expected_picture_end = 8

    music_rows = [row for row in tl.bundle["parent"]["clips"] if row.get("track") == "music"]
    assert len(music_rows) == (2 if policy == "keep_end" else 1)
    assert not any("~" in str(row.get("id")) for row in music_rows)
    if held and policy == "keep_end":
        assert all("from" in row and "hold" in row and "to" not in row for row in music_rows)
    def end(row):
        duration = row.get("hold")
        if duration is None:
            duration = row["to"] - row.get("from", 0)
        return row["at"] + duration / row.get("speed", 1)

    assert max(end(row) for row in music_rows) == pytest.approx(expected_picture_end if policy == "keep_end" else 10)

    if policy == "keep_end":
        # A held bed uses `from` + `hold` without `to`; later edits must accept it.
        tl.insert_time(2, 0.5)


def test_insert_refuses_source_overrun_without_changing_any_flat_clip():
    original = clip("picture", "V1", 3, 1, 5)
    tl = checkout([original])
    # Make the registered file end at 5 s: extending this source from 1 s by 6 s cannot fit.
    tl.bundle["parent"]["config"]["registry"]["assets"]["picture"]["duration"] = 5
    before = deepcopy(tl.bundle["parent"]["clips"])

    with pytest.raises(TimelineEditError, match="only 5 s long"):
        tl.insert_time(4, 4)

    assert tl.bundle["parent"]["clips"] == before


def test_hold_only_audio_split_resumes_the_right_source_instead_of_replaying():
    audio = {"id": "voice", "clipType": "media", "track": "A1", "asset": "voice", "at": 0, "hold": 8}
    intent.set_line(audio, "s1", [[0.2, 0.4, "before"], [6.2, 6.4, "after"]])
    tl = checkout([audio])

    tl.ripple_delete(2, 4)

    right = tl.clip("voice~2000")
    assert (right.start, right.end, right.data["from"], right.data["hold"]) == pytest.approx((2, 6, 4, 4))


def test_picture_with_implicit_source_start_still_checks_extension_limit():
    picture = clip("picture", "V1", 0, 0, 4)
    picture.pop("from")
    tl = checkout([picture])
    tl.bundle["parent"]["config"]["registry"]["assets"]["picture"]["duration"] = 5
    before = deepcopy(tl.bundle["parent"]["clips"])

    with pytest.raises(TimelineEditError, match="only 5 s long"):
        tl.insert_time(2, 2)

    assert tl.bundle["parent"]["clips"] == before


@pytest.mark.parametrize("source_in", [None, 3])
def test_hold_audio_leading_trim_advances_its_source(source_in):
    audio = {"id": "audio", "clipType": "media", "track": "A1", "asset": "voice", "at": 3, "hold": 4}
    if source_in is not None:
        audio["from"] = source_in
    tl = checkout([audio])

    tl.ripple_delete(2, 4)

    trimmed = tl.clip("audio")
    assert (trimmed.start, trimmed.end, trimmed.data["from"], trimmed.data["hold"]) == pytest.approx(
        (2, 5, (source_in or 0) + 1, 3))
