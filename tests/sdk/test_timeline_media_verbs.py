from __future__ import annotations

import json
from pathlib import Path

import pytest

from astrid.sdk.timeline._common import TimelineEditError
from astrid.sdk.timeline_checkout import Checkout

TINY = Path(__file__).resolve().parents[1] / "fixtures" / "timeline_editing" / "tiny.json"


def tiny():
    data = json.loads(TINY.read_text(encoding="utf-8"))
    data["shots"]["A"]["internal_timeline"]["registry"]["assets"]["Q"]["duration"] = 10.0
    return Checkout(data)


def test_add_audio_has_no_cut_and_uses_the_source_duration():
    tl = tiny()
    clip = tl.add("media", asset="Q", track="music", at=1, cut=None, clip_id="bed")
    assert clip.cut_id is None
    assert clip.start == pytest.approx(1)
    assert clip.duration == pytest.approx(10)
    assert clip.data["from"] == 0
    assert clip.data["to"] == 10


def test_trim_keeps_film_start_and_slip_keeps_film_span():
    tl = tiny()
    clip = tl.clip("a-music")
    clip.trim(in_s=1, out_s=3)
    assert clip.start == pytest.approx(0)
    assert clip.duration == pytest.approx(2)
    clip.slip(2)
    assert clip.start == pytest.approx(0)
    assert clip.duration == pytest.approx(2)
    assert clip.data["from"] == 3
    assert clip.data["to"] == 5


@pytest.mark.parametrize("method", [lambda clip: clip.trim(in_s=-1), lambda clip: clip.trim(out_s=11), lambda clip: clip.slip(-1), lambda clip: clip.slip(7)])
def test_source_bounds_name_the_clip_and_file_limit(method):
    tl = tiny()
    with pytest.raises(TimelineEditError, match=r"a-music:.*(0–10|0–10\.0)"):
        method(tl.clip("a-music"))


def _held_music():
    """A music bed carrying both a hold and a source window (as the film's beds do)."""
    tl = tiny()
    clip = tl.add("media", asset="Q", track="music", at=1, cut=None, clip_id="bed")
    clip.trim(in_s=2, out_s=5)
    clip.data["hold"] = 3.0
    return tl, clip


def _window_matches_hold(clip):
    return clip.data["to"] - clip.data["from"] == pytest.approx(clip.data["hold"])


def test_for_on_a_media_clip_moves_the_source_out_point_with_the_hold():
    tl, clip = _held_music()
    clip.hold_for(6)
    assert clip.data["hold"] == pytest.approx(6)
    assert clip.data["from"] == pytest.approx(2)
    assert clip.data["to"] == pytest.approx(8)
    assert _window_matches_hold(clip)
    clip.hold_for(1)
    assert clip.data["to"] == pytest.approx(3)
    assert _window_matches_hold(clip)


def test_extend_on_a_media_clip_keeps_the_window_equal_to_the_hold():
    tl, clip = _held_music()
    clip.extend(2)
    assert clip.data["hold"] == pytest.approx(5)
    assert _window_matches_hold(clip)


def test_for_past_the_end_of_the_source_is_refused_clearly():
    tl, clip = _held_music()
    with pytest.raises(TimelineEditError, match=r"bed:.*10"):
        clip.hold_for(9)  # from 2 s + 9 s = 11 s, the file is 10 s
    assert clip.data["to"] == pytest.approx(5)
    assert clip.data["hold"] == pytest.approx(3)


def test_trim_slip_and_move_keep_the_window_equal_to_the_hold():
    tl, clip = _held_music()
    clip.trim(in_s=1, out_s=4)
    assert _window_matches_hold(clip)
    clip.slip(2)
    assert _window_matches_hold(clip)
    clip.nudge(1)
    assert _window_matches_hold(clip)


def _windowless_footage():
    """Footage with a hold but no source window (the film's timelapse clips), beside an unrelated text clip."""
    tl = tiny()
    foot = tl.add("media", asset="Q", track="music", at=1, cut=None, clip_id="foot")
    for key in ("from", "to"):
        foot.data.pop(key, None)
    foot.data["hold"] = 3.0
    return tl, foot


def test_length_helpers_never_stamp_a_window_onto_windowless_footage():
    from astrid.sdk.timeline.doc import _set_length, _trim_front
    for helper, arg in ((_set_length, 2.0), (_trim_front, 1.0)):
        clip = {"id": "foot", "clipType": "media", "asset": "Q", "hold": 3.0}
        helper(clip, arg)
        assert "from" not in clip and "to" not in clip


def test_for_on_windowless_footage_stays_windowless_and_unrefused():
    tl, foot = _windowless_footage()
    foot.hold_for(6)
    assert foot.data["hold"] == pytest.approx(6)
    assert "from" not in foot.data and "to" not in foot.data


def test_helpers_still_follow_an_existing_window():
    from astrid.sdk.timeline.doc import _set_length
    clip = {"hold": 3.0, "from": 2.0, "to": 5.0}
    _set_length(clip, 6.0)
    assert (clip["from"], clip["to"], clip["hold"]) == (2.0, 8.0, 6.0)
