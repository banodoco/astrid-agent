"""Partial L13-01 producer assertions from timing review §3.4.

This disposable example deliberately places the target word exactly on a beat.
It checks authored onset and the compositor's first *scheduled* frame separately
against each reference. It is not the original ticket input or observed pixel
visibility. The ticket's "near" tolerance and rendered first visible frame must
still be supplied; the beat-selector's half-frame exclusion is not that tolerance.
"""
from __future__ import annotations

import pytest

from astrid.core.timeline import moments
from astrid.core.timeline.duration import clip_start_frame
from astrid.sdk import timeline_intent as intent
from astrid.sdk.timeline.compiler import compile_timing
from astrid.sdk.timeline.model import _MomentContext
from astrid.sdk.timeline_checkout import Checkout


WORD_REF = '"Astrid" in n21'
BEAT_REF = 'beat 1 after "ready" in n21'


def _example(onset: str) -> Checkout:
    # Source conventions: placement milliseconds; clip-relative narration words;
    # source-second modern beat grids mapped through the placed bed's trim/speed.
    tracks = [{"id": name, "kind": kind} for name, kind in
              (("plate", "visual"), ("sprite", "visual"),
               ("vo", "audio"), ("music", "audio"))]
    clips = [
        {"id": "plate", "clipType": "am-snap-plate", "track": "plate",
         "at": 0.0, "hold": 4.0, "app": {"astrid": {"cut": "c1"}}},
        {"id": "sprite", "clipType": "am-sprite", "track": "sprite",
         "at": 0.0, "hold": 1.0, "params": {},
         "app": {"astrid": {"cut": "c1"}}},
        {"id": "voice", "clipType": "media", "track": "vo",
         "at": 0.0, "from": 0.0, "to": 2.0,
         "app": {"astrid": {"segment": "n21", "words": [
             [0.25, 0.4, "ready"], [0.5, 0.8, "Astrid"]]}}},
        {"id": "music-main", "clipType": "media", "track": "music",
         "at": 0.0, "from": 2.0, "to": 10.0, "speed": 2.0,
         "app": {"astrid": {"beats": {"beats": [2.0, 3.0, 4.0, 5.0]}}}},
    ]
    tl = Checkout({
        "parent": {"config": {"theme_overrides": {"visual": {"canvas": {"fps": 30}}}},
                   "clips": []},
        "placements": [{"shot_id": "S", "occurrence_id": "occ-S",
                        "placement": {"start_ms": 2000}, "duration_ms": 4000}],
        "shots": {"S": {"internal_timeline": {"tracks": tracks, "clips": clips}}},
    })
    tl.clip("sprite").on(onset)
    return tl


@pytest.mark.parametrize("onset", [BEAT_REF, WORD_REF], ids=["beat-onset", "word-onset"])
def test_l13_01_authored_onset_reference_survives_compilation(onset):
    tl = _example(onset)
    assert intent.on(tl.clip("sprite").data) == onset
    # Resolution is driven by persistent onset, not merely a cached placement.
    tl.clip("sprite").data["at"] = 1.0
    compiled = Checkout(compile_timing(tl.bundle).bundle)
    sprite = compiled.clip("sprite")
    assert intent.on(sprite.data) == onset
    assert sprite.data["app"]["astrid"]["on"] == onset
    assert sprite.data["at"] == 0.5
    assert sprite.start == 2.5


@pytest.mark.parametrize("onset", [BEAT_REF, WORD_REF], ids=["beat-onset", "word-onset"])
def test_l13_01_first_scheduled_frame_matches_referenced_placed_beat(onset):
    tl = _example(onset)
    sprite = tl.clip("sprite")
    ctx = _MomentContext(tl, sprite)
    beat_seconds = moments.resolve(BEAT_REF, ctx)
    # Resolve the beat independently from clip placement and word onset.
    assert ctx.beats("beat") == [2.0, 2.5, 3.0, 3.5]
    assert beat_seconds == 2.5
    first_scheduled_frame = clip_start_frame({**sprite.data, "at": sprite.start}, tl.fps)
    assert first_scheduled_frame == 75
    assert first_scheduled_frame - beat_seconds * tl.fps == 0


@pytest.mark.parametrize("onset", [BEAT_REF, WORD_REF], ids=["beat-onset", "word-onset"])
def test_l13_01_first_scheduled_frame_matches_referenced_word(onset):
    tl = _example(onset)
    sprite = tl.clip("sprite")
    word_seconds = moments.resolve(WORD_REF, _MomentContext(tl, sprite))
    # A separate comparison prevents beat alignment from standing in for speech.
    assert word_seconds == 2.5
    first_scheduled_frame = clip_start_frame({**sprite.data, "at": sprite.start}, tl.fps)
    assert first_scheduled_frame == 75
    assert first_scheduled_frame - word_seconds * tl.fps == 0
