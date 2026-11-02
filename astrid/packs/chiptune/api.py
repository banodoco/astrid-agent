"""Public deterministic synthesis and MIDI surface for dependent packs."""

from astrid.packs.chiptune.actions._midi import Note, parse_midi
from astrid.packs.chiptune.actions._midi import render as render_midi
from astrid.packs.chiptune.actions._synth import (
    SAMPLE_RATE,
    compose_music,
    render_sfx,
    to_int16_mono,
    to_int16_stereo,
)

__all__ = [
    "Note",
    "SAMPLE_RATE",
    "compose_music",
    "parse_midi",
    "render_midi",
    "render_sfx",
    "to_int16_mono",
    "to_int16_stereo",
]
