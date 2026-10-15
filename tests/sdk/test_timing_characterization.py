"""Focused record of current timing behavior for F#CHARACTERIZE.

The inline bundle mutations below are disposable synthetic examples. They do not
represent or reproduce the historical v11 bytes.
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

import pytest

from astrid.core.timeline import moments
from astrid.core.timeline.duration import clip_end_frame, clip_start_frame
from astrid.core.timeline.music_edit import plan_keep_end
from astrid.sdk.timeline_checkout import Checkout
from astrid.sdk.timeline.doc import _split_audio
from astrid.sdk.timeline.model import retime_scope
from astrid.sdk.timeline.reflow import _bed_clusters
from astrid.sdk.timeline_editing import quantize_time


FPS = 30
TINY = Path(__file__).resolve().parents[1] / "fixtures" / "timeline_editing" / "tiny.json"


def _tiny(*, full_music: bool = False) -> Checkout:
    data = json.loads(TINY.read_text(encoding="utf-8"))
    if full_music:
        bed = next(c for c in data["shots"]["A"]["internal_timeline"]["clips"] if c["id"] == "a-music")
        bed["to"] = 8.0
    return Checkout(data)


def _music(tl: Checkout):
    return sorted((c for c in tl.clips(audio=True) if c.track == "music"), key=lambda c: c.start)


def _bed(**extra):
    value = {"id": "bed", "at": 0.0, "from": 0.0, "to": 60.0,
             "params": {"fadeIn": 0.5, "fadeOut": 1.0}, "app": {"kind": "music"}}
    value.update(extra)
    return value


@dataclass(frozen=True)
class _Word:
    text: str
    segment: str
    index: int
    start: float
    end: float


class _MomentContext:
    fps = FPS

    def __init__(self, words=(), *, beats=None, own=(2.0, 5.0), line_in=0.05):
        self._words = list(words)
        self._beats = beats or {}
        self._own = own
        self._line_in = line_in

    def words(self):
        return self._words

    def beats(self, kind):
        return self._beats.get(kind, [])

    def cut_start(self, cut_id):
        assert cut_id == "c22"
        return 2.0

    def cut_end(self, cut_id):
        assert cut_id == "c22"
        return 5.0

    def own_cut(self):
        return self._own

    def line_in_point(self, line):
        assert line == "s1"
        return self._line_in


def test_moment_reference_grammar_records_words_phrases_occurrences_beats_cuts_offsets_and_end():
    """Source: astrid/core/timeline/moments.py:83-152, 201-254 (parse and resolve grammar)."""
    words = [
        _Word("We", "s1", 0, 0.1, 0.2), _Word("went", "s1", 1, 0.3, 0.5),
        _Word("viral", "s1", 2, 0.6, 0.9), _Word("tool", "s1", 3, 1.0, 1.2),
        _Word("tool", "s1", 4, 1.4, 1.7), _Word("tool", "s2", 0, 3.0, 3.2),
    ]
    ctx = _MomentContext(words, beats={"beat": [0.5, 1.0, 1.5, 2.0]})

    assert moments.resolve('"viral"', ctx) == pytest.approx(0.6)
    assert moments.resolve('"went viral"', ctx) == pytest.approx(0.3)
    assert moments.resolve('after "went viral"', ctx) == pytest.approx(0.9)
    assert moments.resolve('"tool" in s1 #2', ctx) == pytest.approx(1.4)
    assert moments.resolve('beat 2 after "viral"', ctx) == pytest.approx(1.5)
    assert moments.resolve('beat 1 before "viral"', ctx) == pytest.approx(0.5)
    assert moments.resolve("c22", ctx) == pytest.approx(2.0)
    assert moments.resolve("end of c22", ctx) == pytest.approx(5.0)
    assert moments.resolve("c22 +3f", ctx) == pytest.approx(2.1)
    assert moments.resolve("+0.8s", ctx) == pytest.approx(2.8)
    assert moments.resolve("end", ctx) == pytest.approx(5.0)
    assert moments.resolve("end", _MomentContext(words, own=(0.0, 9.0))) == pytest.approx(9.0)


@pytest.mark.parametrize("text", ["end", "end +1s", "+1s"])
def test_clip_relative_moments_refuse_a_clip_with_no_owning_cut(text):
    with pytest.raises(moments.MomentError, match="belongs to no cut"):
        moments.resolve(text, _MomentContext(own=None))


def test_first_word_picture_anchor_uses_take_in_point_only_without_offset():
    """Source: astrid/core/timeline/moments.py:229-233; astrid/sdk/timeline/model.py:1129-1136."""
    ctx = _MomentContext([_Word("We", "s1", 0, 0.1, 0.2)], line_in=0.05)
    assert moments.resolve('"We"', ctx, in_point=True) == pytest.approx(0.05)
    assert moments.resolve('"We" +1f', ctx, in_point=True) == pytest.approx(0.1 + 1 / FPS)


def test_beat_on_tolerance_includes_half_a_frame_around_the_base():
    """Source: astrid/core/timeline/moments.py:234-242 (the strict half-frame beat filter)."""
    base = _Word("base", "s1", 0, 1.0, 1.1)
    at_boundary = 1.0 + 0.5 / FPS
    just_after = at_boundary + 0.001
    ctx = _MomentContext([base], beats={"beat": [at_boundary, just_after]})
    assert moments.resolve('beat 1 after "base"', ctx) == pytest.approx(just_after)


def test_moments_sdk_and_compositor_keep_their_distinct_quantizers():
    """Source: astrid/core/timeline/moments.py:257-258; astrid/sdk/timeline_editing.py:490-498; astrid/core/timeline/duration.py:145-171."""
    requested = 1.6 / FPS
    clip = {"at": requested, "hold": requested}
    assert moments.floor_frame(requested, FPS) == pytest.approx(1 / FPS)
    assert quantize_time(requested, FPS) == 2
    assert clip_start_frame(clip, FPS) == 2
    # The compositor rounds start and duration independently: 2 + 2 frames.
    assert clip_end_frame(clip, FPS) == 4


def test_narration_word_coordinates_stay_clip_relative_when_clip_speed_is_two():
    """Source: astrid/sdk/timeline/lookup.py:154-173 (word seconds add to clip.at without speed division)."""
    tl = _tiny()
    voice = next(c for c in tl.bundle["shots"]["A"]["internal_timeline"]["clips"] if c["id"] == "a-vo")
    voice["speed"] = 2.0
    assert tl.word("it").start == pytest.approx(0.6)


def test_ripple_moves_eligible_unanchored_clip_but_does_not_extend_playing_sfx():
    """Source: astrid/sdk/timeline/reflow.py:904-919, 1049-1051, 1099-1114."""
    tl = _tiny()
    internal = tl.bundle["shots"]["A"]["internal_timeline"]
    internal["tracks"].append({"id": "fx", "kind": "audio"})
    internal["clips"].extend([
        {"id": "loose", "clipType": "am-sprite", "track": "sprite", "at": 3.0, "hold": 1.0, "params": {}},
        {"id": "sfx", "clipType": "media", "track": "fx", "at": 1.5, "from": 0.0, "to": 2.0},
    ])
    tl.insert_time(2.0, 0.5)
    assert tl.clip("loose").start == pytest.approx(3.5)
    assert tl.clip("sfx").start == pytest.approx(1.5)
    assert tl.clip("sfx").end == pytest.approx(3.5)


def test_opening_and_closing_time_split_a_narration_piece():
    """Source: astrid/sdk/timeline/reflow.py:103-125, 1023-1116; astrid/sdk/timeline/doc.py:78-95 (VO pieces and split mechanics)."""
    opened = _tiny()
    opened.insert_time(1.0, 0.5)
    open_pieces = opened.voice("s1").clips
    assert [c.id for c in open_pieces] == ["a-vo", "a-vo~1000"]
    assert open_pieces[0].end == pytest.approx(1.0)
    assert open_pieces[1].start == pytest.approx(1.5)
    assert open_pieces[1].data["from"] == pytest.approx(0.5)

    closed = _tiny()
    closed.ripple_delete(1.0, 1.5)
    close_pieces = closed.voice("s1").clips
    assert [c.id for c in close_pieces] == ["a-vo", "a-vo~1000"]
    assert close_pieces[0].end == pytest.approx(1.0)
    assert close_pieces[1].start == pytest.approx(1.0)
    assert close_pieces[1].data["from"] == pytest.approx(1.0)


def test_declared_gap_belongs_to_the_previous_line_and_beats_reflow_fallback():
    """Source: astrid/sdk/timeline/reflow.py:123-145, 190-214, 283-340 (piece owner and gap precedence)."""
    tl = _tiny()
    previous = tl.voice("s1")
    previous.set_gap_after(0.5, reflow=False)
    assert previous.gap_after == pytest.approx(0.5)
    tl.reflow(gaps={"a-vo": 3.0})
    assert tl.voice("s2").clips[0].start == pytest.approx(2.2)


def test_trimmed_media_until_converts_timeline_length_back_to_source_out():
    """Source: astrid/sdk/timeline/model.py:325-342; astrid/sdk/timeline/doc.py:51-57; astrid/sdk/timeline/reflow.py:702-711."""
    tl = _tiny()
    internal = tl.bundle["shots"]["A"]["internal_timeline"]
    internal["clips"].append({
        "id": "trimmed", "clipType": "media", "track": "sprite", "asset": "P",
        "at": 0.5, "from": 2.0, "to": 10.0, "speed": 2.0,
        "app": {"layer": "trimmed"},
    })
    clip = tl.clip("trimmed").until('"viral"')
    assert clip.start == pytest.approx(0.5)
    assert clip.end == pytest.approx(1.3)
    assert clip.data["from"] == pytest.approx(2.0)
    assert clip.data["to"] == pytest.approx(3.6)


def test_music_uses_preceding_downbeat_then_beat_then_edit_point_and_crossfades():
    """Source: astrid/core/timeline/music_edit.py:67-182 (seam choice, overlap and source trims)."""
    bars = [2.0 * i for i in range(31)]
    beats = [0.5 * i for i in range(121)]
    downbeat = plan_keep_end(_bed(), at=10.3, delta=-3.0, bars=bars, beats=beats)
    beat = plan_keep_end(_bed(), at=10.3, delta=-3.0, beats=beats)
    edit = plan_keep_end(_bed(), at=10.3, delta=-3.0)
    assert (downbeat.seam_at, downbeat.on_bar) == (10.0, True)
    assert (beat.seam_at, beat.on_bar) == (10.0, False)
    assert (edit.seam_at, edit.on_bar) == (10.3, False)
    assert downbeat.overlap == pytest.approx(0.25)
    assert downbeat.pieces[0]["params"]["fadeOut"] == pytest.approx(0.25)
    assert downbeat.pieces[1]["params"]["fadeIn"] == pytest.approx(0.25)


def test_music_repeat_shortage_is_reported_as_explicit_silence():
    """Source: astrid/core/timeline/music_edit.py:127-139, 170-177 (short bed repeat and silence note)."""
    cut = plan_keep_end(_bed(), at=1.0, delta=2.0)
    assert cut.repeated == pytest.approx(1.0)
    assert "0.88 s of silence" in cut.note


def test_ripple_locations_cluster_within_a_bar_and_sum_their_deltas():
    """Source: astrid/sdk/timeline/reflow.py:36-50 (_bed_clusters)."""
    assert _bed_clusters([(4.0, 0.1), (1.1, 0.5), (1.0, -0.2)], 2.0, 1 / FPS) == [
        (1.0, pytest.approx(0.3)), (4.0, pytest.approx(0.1)),
    ]


def test_per_call_music_override_is_temporary():
    """Source: astrid/sdk/timeline/reflow.py:77-88, 908-920 (_music_as and music_keyword restoration)."""
    tl = _tiny(full_music=True)
    assert tl.music == "keep_end"
    tl.insert_time(2.0, 0.5, music="plays_on")
    assert tl.music == "keep_end"
    assert len(_music(tl)) == 1


def test_film_length_sync_resizes_only_the_last_shot():
    """Source: astrid/sdk/timeline/reflow.py:438-458 (_sync_film_length)."""
    tl = _tiny()
    tl.clip("c2.plate").extend(1.0)
    tl.retime()
    durations = {row["shot_id"]: row["duration_ms"] for row in tl.document()["placements"]}
    assert durations == {"A": 4000, "B": 5000}


def test_sequence_relay_allocates_new_frames_in_proportion_to_old_frames():
    """Source: astrid/sdk/timeline/reflow.py:949-972 (_relay_steps and largest-remainder allocation)."""
    tl = _tiny()
    steps = [
        {"id": "step-1", "at": 0.0, "hold": 1 / FPS},
        {"id": "step-2", "at": 1 / FPS, "hold": 3 / FPS},
    ]
    tl._relay_steps(steps, 0.0, 8 / FPS)
    assert [round(c["hold"] * FPS) for c in steps] == [2, 6]


def test_inserted_narration_take_keeps_the_sixty_millisecond_tail():
    """Source: astrid/sdk/timeline/reflow.py:217-245 (insert_line source out is last word + 0.06 s)."""
    tl = _tiny()
    tl.insert_line("s1b", "Q", words=[[0.1, 0.3, "and"], [0.4, 0.7, "then"]], after="s1", gap_after=0.3)
    inserted = tl.voice("s1b").clips[0]
    assert inserted.data["to"] == pytest.approx(0.76)


def test_split_audio_keeps_the_cut_gap_and_source_skip_rules():
    """Source: astrid/sdk/timeline/doc.py:78-95 (_split_audio)."""
    cut_source = {"id": "audio", "at": 1.0, "from": 10.0, "to": 20.0, "speed": 2.0}
    first, second = _split_audio(cut_source, 1.5, cut=0.5)
    assert first["to"] == pytest.approx(13.0)
    assert second["from"] == pytest.approx(14.0)
    assert second["at"] == pytest.approx(2.5)

    gap_source = {"id": "narration", "at": 1.0, "from": 10.0, "to": 20.0, "speed": 2.0}
    first, second = _split_audio(gap_source, 1.5, gap=0.5)
    assert first["to"] == pytest.approx(13.0)
    assert second["from"] == pytest.approx(13.0)
    assert second["at"] == pytest.approx(3.0)


def test_music_reflow_does_not_accumulate_a_seam_on_an_existing_seam():
    """Source: astrid/sdk/timeline/reflow.py:1139-1185; owner: MUSIC-SEGMENTS."""
    tl = _tiny(full_music=True)
    tl.insert_time(2.0, 1.0)
    tl.insert_time(2.0, 1.0)
    pieces = _music(tl)
    # Two edits at one ripple location should be rebuilt as one seam, not cut
    # once into each already-existing segment.
    assert len(pieces) == 2


def test_split_music_piece_ids_are_unique_across_seams_in_one_retime():
    """Source: astrid/sdk/timeline/reflow.py:1153-1185; owner: MUSIC-SEGMENTS (unique split-piece ids)."""
    tl = _tiny(full_music=True)
    with retime_scope(tl):
        tl.insert_time(1.0, 0.5)
        tl.ripple_delete(6.0, 6.3)
    ids = [c.id for c in _music(tl)]
    assert len(ids) > 1
    assert len(ids) == len(set(ids))
