"""Play a MIDI file as an NES-style chiptune (chiptune.midi).

A Standard MIDI File (format 0 or 1, with its tempo map) becomes three NES voices:

- **pulse 1** (25 % duty) plays the melody: the highest note sounding, with a delayed vibrato;
- **pulse 2** (12.5 % duty) arpeggiates the inner notes of the chord (the classic NES chord trick);
- **triangle** plays the bass: the lowest note sounding, below middle C.

Velocity becomes a 4-bit volume. The note times are the file's own, so the cue lines up with the
recording a transcription (e.g. Basic Pitch) was made from. Pure numpy, deterministic.
"""

from __future__ import annotations

import math
import struct
from dataclasses import dataclass
from typing import Any, Sequence

import numpy as np

from astrid.pack_api import AstridError
from astrid.packs.chiptune.actions import _synth as syn

MELODY_FLOOR = 55  # G3: below it a note is never the tune
BASS_CEILING = 60  # middle C: the triangle takes the lowest note under it
ARP_RATE_HZ = 20.0  # 60 Hz frames, 3 per step
VIBRATO_DELAY_S = 0.25
VIBRATO_HZ = 5.5
VIBRATO_SEMITONES = 0.18
GAINS = {"pulse1": 0.42, "pulse2": 0.24, "triangle": 0.55}
PANS = {"pulse1": -0.2, "pulse2": 0.25, "triangle": 0.0}


@dataclass(frozen=True, order=True)
class Note:
    start_s: float
    end_s: float
    pitch: int
    velocity: int


# ---------------------------------------------------------------------------
# Standard MIDI File
# ---------------------------------------------------------------------------


def _vlq(data: bytes, i: int) -> tuple[int, int]:
    value = 0
    for _ in range(4):
        if i >= len(data):
            raise AstridError("the MIDI file ends inside a variable-length number")
        byte = data[i]
        i += 1
        value = (value << 7) | (byte & 0x7F)
        if not byte & 0x80:
            return value, i
    raise AstridError("the MIDI file has a variable-length number longer than 4 bytes")


def parse_midi(data: bytes) -> tuple[list[Note], dict[str, Any]]:
    """Notes in seconds (tempo map applied) and a summary. Drums (channel 10) are skipped."""
    if data[:4] != b"MThd" or len(data) < 14:
        raise AstridError("not a Standard MIDI File (no MThd header)")
    header_len = struct.unpack(">I", data[4:8])[0]
    fmt, ntracks, division = struct.unpack(">HHH", data[8:14])
    if fmt not in (0, 1):
        raise AstridError(f"MIDI format {fmt} is not supported (0 or 1)")
    if division & 0x8000:
        raise AstridError("SMPTE-timed MIDI files are not supported (ticks per quarter only)")
    i = 8 + header_len
    tempo_events: list[tuple[int, int]] = []
    raw: list[tuple[int, int, int, int]] = []  # (start tick, end tick, pitch, velocity)
    for _ in range(ntracks):
        if data[i:i + 4] != b"MTrk":
            raise AstridError("a MIDI track chunk is missing its MTrk header")
        length = struct.unpack(">I", data[i + 4:i + 8])[0]
        j, end = i + 8, i + 8 + length
        tick, status = 0, None
        sounding: dict[tuple[int, int], list[tuple[int, int]]] = {}
        while j < end:
            delta, j = _vlq(data, j)
            tick += delta
            byte = data[j]
            if byte == 0xFF:
                kind = data[j + 1]
                size, k = _vlq(data, j + 2)
                if kind == 0x51 and size == 3:
                    tempo_events.append((tick, int.from_bytes(data[k:k + 3], "big")))
                j = k + size
                continue
            if byte in (0xF0, 0xF7):
                size, k = _vlq(data, j + 1)
                j = k + size
                continue
            if byte & 0x80:
                status = byte
                j += 1
            elif status is None:
                raise AstridError("a MIDI event uses running status before any status byte")
            kind, channel = status & 0xF0, status & 0x0F
            if kind in (0xC0, 0xD0):
                j += 1
                continue
            a, b = data[j], data[j + 1]
            j += 2
            if channel == 9:
                continue
            if kind == 0x90 and b > 0:
                sounding.setdefault((channel, a), []).append((tick, b))
            elif kind in (0x80, 0x90) and sounding.get((channel, a)):
                on_tick, velocity = sounding[(channel, a)].pop(0)
                if tick > on_tick:
                    raw.append((on_tick, tick, a, velocity))
        i = end
    tempo_events.sort()
    if not tempo_events or tempo_events[0][0] > 0:
        tempo_events.insert(0, (0, 500_000))

    def seconds(tick: int) -> float:
        total, last_tick, tempo = 0.0, 0, tempo_events[0][1]
        for at, value in tempo_events[1:]:
            if at >= tick:
                break
            total += (at - last_tick) / division * tempo / 1e6
            last_tick, tempo = at, value
        return total + (tick - last_tick) / division * tempo / 1e6

    notes = sorted(Note(seconds(s), seconds(e), p, v) for s, e, p, v in raw)
    summary = {
        "format": fmt,
        "tracks": ntracks,
        "ticks_per_quarter": division,
        "tempo_changes": len(tempo_events),
        "first_bpm": round(60e6 / tempo_events[0][1], 3),
        "notes": len(notes),
        "duration_s": round(max((n.end_s for n in notes), default=0.0), 6),
        "pitch_range": [min((n.pitch for n in notes), default=0), max((n.pitch for n in notes), default=0)],
    }
    return notes, summary


# ---------------------------------------------------------------------------
# Voices
# ---------------------------------------------------------------------------


def allocate(notes: Sequence[Note], transpose: int = 0) -> dict[str, list[tuple[float, float, Any]]]:
    """Split the polyphony into the three NES voices, slice by slice.

    Returns per voice a list of ``(start_s, end_s, value)``. For pulse 1 and the triangle the value is
    ``(note index, pitch)``; for pulse 2 it is the tuple of inner pitches to arpeggiate.
    """
    if not notes:
        return {"pulse1": [], "pulse2": [], "triangle": []}
    edges = sorted({n.start_s for n in notes} | {n.end_s for n in notes})
    order = sorted(range(len(notes)), key=lambda k: notes[k].start_s)
    voices: dict[str, list[tuple[float, float, Any]]] = {"pulse1": [], "pulse2": [], "triangle": []}
    active: list[int] = []
    cursor = 0
    for a, b in zip(edges, edges[1:]):
        while cursor < len(order) and notes[order[cursor]].start_s <= a + 1e-9:
            active.append(order[cursor])
            cursor += 1
        active = [k for k in active if notes[k].end_s > a + 1e-9]
        if not active:
            continue
        by_pitch = sorted(active, key=lambda k: (notes[k].pitch, notes[k].start_s))
        top, low = by_pitch[-1], by_pitch[0]
        melody = top if notes[top].pitch >= MELODY_FLOOR else None
        bass = low if notes[low].pitch < BASS_CEILING and low != melody else None
        inner = tuple(sorted({notes[k].pitch + transpose for k in by_pitch if k not in (melody, bass)}))
        if melody is not None:
            voices["pulse1"].append((a, b, (melody, notes[melody].pitch + transpose)))
        if bass is not None:
            voices["triangle"].append((a, b, (bass, notes[bass].pitch + transpose)))
        if inner:
            voices["pulse2"].append((a, b, inner))
    return {name: _merge(spans) for name, spans in voices.items()}


def _merge(spans: list[tuple[float, float, Any]]) -> list[tuple[float, float, Any]]:
    out: list[tuple[float, float, Any]] = []
    for a, b, value in spans:
        if out and out[-1][2] == value and abs(out[-1][1] - a) < 1e-9:
            out[-1] = (out[-1][0], b, value)
        else:
            out.append((a, b, value))
    return out


# ---------------------------------------------------------------------------
# Rendering
# ---------------------------------------------------------------------------


def _volume(velocity: int) -> float:
    """NES 4-bit volume from a MIDI velocity."""
    return max(1, round(velocity / 127.0 * 15.0)) / 15.0


def _envelope(n: int, offset_s: float, tau_s: float, sustain: float) -> np.ndarray:
    """A plucked level (decaying to ``sustain``) seen from ``offset_s`` after the note's onset."""
    t = offset_s + np.arange(n) / syn.SAMPLE_RATE
    return (sustain + (1.0 - sustain) * np.exp(-t / tau_s)) * syn._ramped(n)


def render(notes: Sequence[Note], *, duration_s: float, transpose: int = 0, master_db: float = -16.0,
           start_s: float = 0.0) -> tuple[np.ndarray, dict[str, Any]]:
    """Stereo float buffer (2, n) of ``duration_s`` from ``start_s`` of the MIDI, and the voice summary."""
    if duration_s <= 0:
        raise AstridError("duration_s must be positive")
    if not -24 <= transpose <= 24:
        raise AstridError("transpose must be within two octaves (-24..24)")
    n_total = int(round(duration_s * syn.SAMPLE_RATE))
    buf = np.zeros((2, n_total), dtype=np.float32)
    voices = allocate(notes, transpose)

    def window(a: float, b: float) -> tuple[float, float] | None:
        lo, hi = max(a, start_s), min(b, start_s + duration_s)
        return (lo, hi) if hi - lo > 1.0 / syn.SAMPLE_RATE else None

    for a, b, (idx, pitch) in voices["pulse1"]:
        span = window(a, b)
        if span is None:
            continue
        n = int(round((span[1] - span[0]) * syn.SAMPLE_RATE))
        since = span[0] - notes[idx].start_s
        t = since + np.arange(n) / syn.SAMPLE_RATE
        depth = np.clip((t - VIBRATO_DELAY_S) / 0.3, 0.0, 1.0) * VIBRATO_SEMITONES
        freq = syn.hz(pitch) * 2.0 ** (depth * np.sin(2 * np.pi * VIBRATO_HZ * t) / 12.0)
        sig = syn._oscillate("pulse", 0.25, freq) * _envelope(n, since, 0.9, 0.45)
        syn._place(buf, sig, span[0] - start_s, GAINS["pulse1"] * _volume(notes[idx].velocity), PANS["pulse1"])

    step = 1.0 / ARP_RATE_HZ
    for a, b, inner in voices["pulse2"]:
        span = window(a, b)
        if span is None:
            continue
        n = int(round((span[1] - span[0]) * syn.SAMPLE_RATE))
        t_abs = span[0] + np.arange(n) / syn.SAMPLE_RATE
        which = (np.floor(t_abs / step).astype(np.int64)) % len(inner)  # a global clock: the arp never stutters
        freq = np.array([syn.hz(p) for p in inner])[which]
        sig = syn._oscillate("pulse", 0.125, freq) * syn._ramped(n)
        syn._place(buf, sig, span[0] - start_s, GAINS["pulse2"], PANS["pulse2"])

    for a, b, (idx, pitch) in voices["triangle"]:
        span = window(a, b)
        if span is None:
            continue
        n = int(round((span[1] - span[0]) * syn.SAMPLE_RATE))
        freq = np.full(n, syn.hz(pitch))
        sig = syn._oscillate("triangle", 0.0, freq) * syn._ramped(n)
        syn._place(buf, sig, span[0] - start_s, GAINS["triangle"], PANS["triangle"])

    _, rms_db = syn.measure_db(buf)
    if rms_db > -119.0:
        buf *= np.float32(10.0 ** ((master_db - rms_db) / 20.0))
    buf = syn._limit(buf, 10.0 ** (syn.PEAK_CEILING_DB / 20.0))
    summary = {name: {"spans": len(spans), "seconds": round(sum(b - a for a, b, _ in spans), 3)}
               for name, spans in voices.items()}
    return buf, summary
