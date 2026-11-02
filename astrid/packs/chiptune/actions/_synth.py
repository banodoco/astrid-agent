"""Programmatic chiptune synthesis for the chiptune pack (numpy only).

Pure functions: seeded numpy arithmetic only. No samples, no network, no
models, no subprocesses. ``chiptune.compose`` and ``chiptune.sfx`` import this
module.

Voices are NES-style: two pulse channels (duty 12.5/25/50 %), a triangle bass
and a noise drum channel. Pulse and triangle waves come from band-limited
wavetables that are mip-mapped by pitch, so high notes do not alias. Every
note and one-shot starts and ends on a 10 ms ramp that reaches exactly zero.
"""

from __future__ import annotations

import ast
import json
import math
import re
import zlib
from functools import lru_cache
from typing import Any, Sequence

import numpy as np

from astrid.pack_api import AstridError

SAMPLE_RATE = 48_000
EDGE_S = 0.010
PEAK_CEILING_DB = -1.05  # strictly below the -1 dBFS requirement
TABLE_SIZE = 2048
MAX_TABLE_LEVEL = 9  # 2**9 harmonics at most; the 2048-point table holds 1023
MOODS = ("bright", "tense", "wistful", "triumphant", "silent")
STYLES = ("nes",)
SFX_KINDS = (
    "stamp", "flip", "blip", "whoosh", "chime",
    "error", "coin", "typewriter_tick", "wipe", "thud",
)
SFX_DEFAULT_SECONDS = {
    "stamp": 0.14, "flip": 0.28, "blip": 0.09, "whoosh": 0.70, "chime": 1.40,
    "error": 0.32, "coin": 0.36, "typewriter_tick": 0.035, "wipe": 0.50, "thud": 0.35,
}
SFX_PEAK = 0.70  # one-shots normalise to -3.1 dBFS peak

_NOTE_PC = {"C": 0, "D": 2, "E": 4, "F": 5, "G": 7, "A": 9, "B": 11}
_MAJOR = (0, 2, 4, 5, 7, 9, 11)
_HARMONIC_MINOR = (0, 2, 3, 5, 7, 8, 11)  # the dominant chord needs the raised leading tone
_ARP_ORDER = (0, 1, 2, 3, 2, 1)

# Chord = (semitones above the key tonic, "M" or "m"). Four chords per cycle.
PROGRESSIONS: dict[tuple[str, str], tuple[tuple[int, str], ...]] = {
    ("minor", "bright"): ((3, "M"), (10, "M"), (0, "m"), (8, "M")),  # III VII i VI
    ("minor", "wistful"): ((0, "m"), (8, "M"), (3, "M"), (10, "M")),  # i VI III VII
    ("minor", "tense"): ((0, "m"), (5, "m"), (8, "M"), (7, "M")),  # i iv VI V
    ("minor", "triumphant"): ((0, "M"), (5, "M"), (7, "M"), (0, "M")),  # I IV V I
    ("major", "bright"): ((0, "M"), (7, "M"), (9, "m"), (5, "M")),  # I V vi IV
    ("major", "wistful"): ((9, "m"), (5, "M"), (0, "M"), (7, "M")),  # vi IV I V
    ("major", "tense"): ((9, "m"), (5, "M"), (2, "m"), (7, "M")),  # vi IV ii V
    ("major", "triumphant"): ((0, "M"), (5, "M"), (7, "M"), (0, "M")),  # I IV V I
}
MOOD_VOICING = {
    "bright": {"lead_duty": 0.25, "arp_duty": 0.5, "gap": 0.9},
    "tense": {"lead_duty": 0.125, "arp_duty": 0.25, "gap": 0.8},
    "wistful": {"lead_duty": 0.5, "arp_duty": 0.25, "gap": 1.0},
    "triumphant": {"lead_duty": 0.25, "arp_duty": 0.5, "gap": 0.9},
}
_NOTE_NAMES = ("C", "C#", "D", "Eb", "E", "F", "F#", "G", "Ab", "A", "Bb", "B")


# ---------------------------------------------------------------------------
# Parsing
# ---------------------------------------------------------------------------


def parse_key(text: Any) -> tuple[int, str]:
    """Parse ``"A minor"``, ``"F# major"``, ``"Bb"`` or ``"Am"`` into (root pitch class, mode)."""
    raw = str(text or "").strip()
    match = re.match(r"^([A-Ga-g])([#b]?)\s*(.*)$", raw)
    if match is None:
        raise AstridError(
            f"invalid key {text!r}",
            valid_options=["A minor", "C major", "F# minor", "Bb major"],
            recovery_command="pass key such as 'A minor' or 'C major'",
        )
    letter, accidental, mode_text = match.groups()
    pc = _NOTE_PC[letter.upper()] + {"": 0, "#": 1, "b": -1}[accidental]
    mode_text = mode_text.strip().lower()
    if mode_text in ("", "major", "maj", "ionian"):
        mode = "major"
    elif mode_text in ("m", "min", "minor", "aeolian"):
        mode = "minor"
    else:
        raise AstridError(
            f"unsupported mode {mode_text!r} in key {text!r}",
            valid_options=["major", "minor"],
            recovery_command="use major or minor",
        )
    return pc % 12, mode


def parse_structured(value: Any, name: str) -> Any:
    """Accept lists/dicts, JSON text, or Python-literal text.

    The runner joins list inputs with commas and stringifies dicts, so the
    literal fallback is what keeps ``hits=1.5,3.0`` and friends working.
    """
    if value is None:
        return None
    if isinstance(value, (list, tuple, dict)):
        return value
    text = str(value).strip()
    if text.lower() in ("", "none", "null", "off"):
        return None
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass
    try:
        return ast.literal_eval(text)
    except (ValueError, SyntaxError) as exc:
        raise AstridError(
            f"{name} is not valid JSON: {text[:80]!r}",
            recovery_command=f"pass {name} as a JSON array string, for example '[[0, 4.5]]'",
        ) from exc


def parse_number_list(value: Any, name: str) -> list[float]:
    parsed = parse_structured(value, name)
    if parsed is None:
        return []
    if isinstance(parsed, (int, float)):
        return [float(parsed)]
    if isinstance(parsed, tuple):
        parsed = list(parsed)
    if not isinstance(parsed, list) or not all(isinstance(item, (int, float)) for item in parsed):
        raise AstridError(f"{name} must be a list of numbers", recovery_command=f"pass {name} as [1.0, 2.5]")
    return [float(item) for item in parsed]


# ---------------------------------------------------------------------------
# Band-limited wavetables
# ---------------------------------------------------------------------------


@lru_cache(maxsize=None)
def _raw_table(shape: str, duty_milli: int, level: int) -> np.ndarray:
    """One DC-free period with 2**level harmonics (band-limited)."""
    n = TABLE_SIZE
    kmax = min(2**level, n // 2 - 1)
    kk = np.arange(1, kmax + 1, dtype=np.float64)
    if shape == "pulse":
        duty = duty_milli / 1000.0
        cos_c = 2.0 * np.sin(2.0 * np.pi * kk * duty) / (np.pi * kk)
        sin_s = 2.0 * (1.0 - np.cos(2.0 * np.pi * kk * duty)) / (np.pi * kk)
    elif shape == "triangle":
        cos_c = np.zeros_like(kk)
        sign = np.where(((kk - 1) // 2) % 2 == 0, 1.0, -1.0)
        sin_s = np.where(kk % 2 == 1, (8.0 / np.pi**2) * sign / kk**2, 0.0)
    else:  # pragma: no cover - internal misuse
        raise ValueError(shape)
    spectrum = np.zeros(n // 2 + 1, dtype=np.complex128)
    spectrum[1 : kmax + 1] = (n / 2.0) * (cos_c - 1j * sin_s)
    return np.fft.irfft(spectrum, n=n)


@lru_cache(maxsize=None)
def _table_scale(shape: str, duty_milli: int) -> float:
    """Shared gain for every level of one waveform, taken from its richest level."""
    peak = float(np.max(np.abs(_raw_table(shape, duty_milli, MAX_TABLE_LEVEL))))
    return 1.0 / peak if peak > 0 else 1.0


def _oscillate(shape: str, duty: float, freq: np.ndarray, phase0: float = 0.0) -> np.ndarray:
    """Read a band-limited wavetable at ``freq`` (Hz per sample, array length n)."""
    n_samples = freq.shape[0]
    fmax = float(np.max(freq)) if n_samples else 0.0
    ratio = (SAMPLE_RATE / 2.0) / max(fmax, 1e-9)
    if ratio < 1.0:
        return np.zeros(n_samples)
    level = min(MAX_TABLE_LEVEL, int(math.floor(math.log2(ratio))))
    duty_milli = int(round(duty * 1000)) if shape == "pulse" else 0
    table = _raw_table(shape, duty_milli, level) * _table_scale(shape, duty_milli)
    increment = freq / SAMPLE_RATE
    phase = (phase0 + np.cumsum(increment) - increment) % 1.0
    position = phase * TABLE_SIZE
    index = np.floor(position).astype(np.int64)
    frac = position - index
    index %= TABLE_SIZE
    nxt = (index + 1) % TABLE_SIZE
    return table[index] * (1.0 - frac) + table[nxt] * frac


def hz(midi: float) -> float:
    return 440.0 * 2.0 ** ((midi - 69.0) / 12.0)


def _glide(f_start: float, f_end: float, n: int, tau_s: float) -> np.ndarray:
    t = np.arange(n) / SAMPLE_RATE
    return f_end + (f_start - f_end) * np.exp(-t / tau_s)


def _ramped(n: int, release_s: float = EDGE_S) -> np.ndarray:
    """Envelope that starts and ends at exactly 0, with 10 ms ramps."""
    env = np.ones(n)
    if n <= 1:
        return np.zeros(n)
    attack = max(1, min(int(round(EDGE_S * SAMPLE_RATE)), n // 2))
    env[:attack] = np.linspace(0.0, 1.0, attack, endpoint=False)
    release = max(1, min(int(round(release_s * SAMPLE_RATE)), n - attack))
    env[n - release :] *= np.linspace(1.0, 0.0, release)
    return env


def _decay(n: int, tau_s: float) -> np.ndarray:
    t = np.arange(n) / SAMPLE_RATE
    return np.exp(-t / tau_s) * _ramped(n)


def _pan_gains(pan: float) -> tuple[float, float]:
    angle = (float(pan) + 1.0) * math.pi / 4.0
    return math.cos(angle), math.sin(angle)


def _place(buf: np.ndarray, sig: np.ndarray, t_s: float, gain: float = 1.0, pan: float = 0.0) -> None:
    start = int(round(t_s * SAMPLE_RATE))
    if start < 0:
        sig = sig[-start:]
        start = 0
    if start >= buf.shape[1] or sig.size == 0:
        return
    end = min(buf.shape[1], start + sig.shape[0])
    seg = sig[: end - start] * gain
    left, right = _pan_gains(pan)
    buf[0, start:end] += (seg * left).astype(np.float32)
    buf[1, start:end] += (seg * right).astype(np.float32)


def _noise_bank(rng: np.random.Generator) -> np.ndarray:
    return rng.standard_normal(SAMPLE_RATE * 2)


def _noise_slice(bank: np.ndarray, rng: np.random.Generator, n: int) -> np.ndarray:
    start = int(rng.integers(0, bank.shape[0] - n))
    return bank[start : start + n]


def _highpass(x: np.ndarray) -> np.ndarray:
    return np.concatenate([x[:1], np.diff(x)])


# ---------------------------------------------------------------------------
# Drums (noise channel and triangle kick)
# ---------------------------------------------------------------------------


def _kick_sig() -> np.ndarray:
    n = int(0.16 * SAMPLE_RATE)
    freq = _glide(150.0, 42.0, n, 0.022)
    return 0.8 * _oscillate("triangle", 0.5, freq) * _decay(n, 0.06)


def _snare_sig(bank: np.ndarray, rng: np.random.Generator, level: float = 1.0) -> np.ndarray:
    n = int(0.12 * SAMPLE_RATE)
    t = np.arange(n) / SAMPLE_RATE
    noise = _noise_slice(bank, rng, n)
    tone = _oscillate("pulse", 0.125, np.full(n, 190.0))
    return level * (0.5 * noise * np.exp(-t / 0.045) + 0.35 * tone * np.exp(-t / 0.03)) * _ramped(n)


def _hat_sig(bank: np.ndarray, rng: np.random.Generator) -> np.ndarray:
    n = int(0.05 * SAMPLE_RATE)
    return 0.55 * _highpass(_noise_slice(bank, rng, n)) * _decay(n, 0.02)


def _crash_sig(bank: np.ndarray, rng: np.random.Generator) -> np.ndarray:
    n = int(0.7 * SAMPLE_RATE)
    return 0.5 * _highpass(_noise_slice(bank, rng, n)) * _decay(n, 0.25)


# ---------------------------------------------------------------------------
# Music composition
# ---------------------------------------------------------------------------


def _reg(pc: int, low: int) -> int:
    """Smallest MIDI note at or above ``low`` with pitch class ``pc``."""
    return low + ((pc - low) % 12)


def _snap_chord_tone(midi: int, chord_pcs: Sequence[int]) -> int:
    best = midi
    for pc in chord_pcs:
        below = midi - ((midi - pc) % 12)
        for candidate in (below, below + 12):
            if abs(candidate - midi) < abs(best - midi):
                best = candidate
    return best


def _chord_pcs(root_pc: int, chord: tuple[int, str]) -> tuple[int, int, int]:
    offset, quality = chord
    base = (root_pc + offset) % 12
    third = (base + (4 if quality == "M" else 3)) % 12
    return base, third, (base + 7) % 12


def _chord_name(root_pc: int, chord: tuple[int, str]) -> str:
    base, _, _ = _chord_pcs(root_pc, chord)
    return _NOTE_NAMES[base] + ("" if chord[1] == "M" else "m")


def _motifs(rng: np.random.Generator) -> list[list[tuple[int, int, int]]]:
    """Four 4-bar phrases (A, B, A, C) built from one seeded 32-eighth skeleton.

    Events are (start_eighth, duration_eighths, scale_degree). The skeleton is
    the motif; B and C are its variations, so the same tune returns with change.
    """
    events: list[tuple[int, int, int]] = []
    pos, degree = 0, 4
    while pos < 32:
        dur = int(rng.choice([1, 1, 2, 2, 3, 4]))
        dur = min(dur, 32 - pos)
        step = int(rng.choice([-2, -1, -1, 1, 1, 2, 3]))
        degree = min(max(degree + step, -1), 7)
        events.append((pos, dur, degree))
        pos += dur
    start, _, _ = events[-1]
    events[-1] = (start, 32 - start, 0)  # the phrase resolves to the tonic
    a_phrase = events
    b_phrase = [(s, d, deg + 2 if s < 16 else deg) for s, d, deg in a_phrase]
    b_phrase[-1] = (b_phrase[-1][0], b_phrase[-1][1], 0)
    c_phrase = [(s, d, deg - 2 if s >= 16 else deg) for s, d, deg in a_phrase]
    c_phrase[-1] = (c_phrase[-1][0], c_phrase[-1][1], 4)  # half cadence on the dominant
    return [a_phrase, b_phrase, a_phrase, c_phrase]


def _degree_midi(degree: int, root_pc: int, scale: Sequence[int], low: int) -> int:
    pc = (root_pc + scale[degree % 7]) % 12
    return _reg(pc, low) + 12 * (degree // 7)


def validate_sections(raw: Any, duration_s: float) -> list[dict[str, Any]]:
    if not isinstance(raw, list) or not raw:
        raise AstridError(
            "sections must be a non-empty list of {start_s, end_s, energy, mood}",
            recovery_command='pass sections as JSON, e.g. [{"start_s":0,"end_s":17,"energy":0.6,"mood":"bright"}]',
        )
    cleaned: list[dict[str, Any]] = []
    for index, item in enumerate(raw):
        if not isinstance(item, dict):
            raise AstridError(f"sections[{index}] must be an object")
        try:
            start_s = float(item["start_s"])
            end_s = float(item["end_s"])
            energy = float(item.get("energy", 0.5))
        except (KeyError, TypeError, ValueError) as exc:
            raise AstridError(f"sections[{index}] needs numeric start_s, end_s and energy") from exc
        mood = str(item.get("mood", "bright"))
        if mood not in MOODS:
            raise AstridError(f"sections[{index}].mood {mood!r} is not one of {list(MOODS)}")
        if not 0.0 <= energy <= 1.0:
            raise AstridError(f"sections[{index}].energy must be within 0..1")
        if not (0.0 <= start_s < end_s):
            raise AstridError(f"sections[{index}] needs 0 <= start_s < end_s")
        if start_s >= duration_s:
            raise AstridError(f"sections[{index}] starts after duration_s={duration_s}")
        cadence_bars = int(item.get("cadence_bars", 0) or 0)
        if not 0 <= cadence_bars <= 8:
            raise AstridError(f"sections[{index}].cadence_bars must be between 0 and 8")
        cleaned.append({"start_s": start_s, "end_s": min(end_s, duration_s), "energy": energy, "mood": mood,
                        "cadence_bars": cadence_bars})
    cleaned.sort(key=lambda sec: sec["start_s"])
    for before, after in zip(cleaned, cleaned[1:]):
        if after["start_s"] < before["end_s"] - 1e-9:
            raise AstridError("sections must not overlap")
    return cleaned


def _duck_gain(n: int, items: list[tuple[float, float, float]]) -> np.ndarray | None:
    """Linear per-sample gain for (start_s, end_s, gain_db) ducks: 120 ms in, 300 ms out.

    A gap of 0.6 s or more brings the bed back to full level (0.12 + 0.30 < 0.6).
    """
    if not items:
        return None
    attack_s, release_s = 0.12, 0.30
    db = np.zeros(n)
    for start_s, end_s, gain_db in items:
        lo = max(0, int((start_s - attack_s) * SAMPLE_RATE))
        hi = min(n, int(math.ceil((end_s + release_s) * SAMPLE_RATE)))
        if hi <= lo:
            continue
        t = np.arange(lo, hi) / SAMPLE_RATE
        curve = np.interp(t, [start_s - attack_s, start_s, end_s, end_s + release_s], [0.0, gain_db, gain_db, 0.0])
        db[lo:hi] = np.minimum(db[lo:hi], curve)
    return 10.0 ** (db / 20.0)


def _remove_dc(channel: np.ndarray, window: int) -> np.ndarray:
    """Subtract a centred running mean (a 1 s highpass that leaves music untouched)."""
    n = channel.shape[0]
    csum = np.concatenate([[0.0], np.cumsum(channel, dtype=np.float64)])
    idx = np.arange(n)
    half = window // 2
    lo = np.clip(idx - half, 0, n)
    hi = np.clip(idx + half + 1, 0, n)
    mean = (csum[hi] - csum[lo]) / (hi - lo)
    return channel - mean


def _remove_dc_active(channel: np.ndarray, active: np.ndarray, window: int) -> np.ndarray:
    """Centred running-mean removal over active samples only, so an exact rest stays exactly silent."""
    n = channel.shape[0]
    csum = np.concatenate([[0.0], np.cumsum(channel * active)])
    count_csum = np.concatenate([[0.0], np.cumsum(active)])
    idx = np.arange(n)
    half = window // 2
    lo = np.clip(idx - half, 0, n)
    hi = np.clip(idx + half + 1, 0, n)
    count = count_csum[hi] - count_csum[lo]
    mean = np.where(count > 0.0, (csum[hi] - csum[lo]) / np.maximum(count, 1.0), 0.0)
    return channel - mean * active


def _sliding_min(x: np.ndarray, width: int) -> np.ndarray:
    """out[i] = min(x[i : i + width]) via doubling; length len(x) - width + 1."""
    result = x
    span = 1
    while span * 2 <= width:
        result = np.minimum(result[:-span], result[span:])
        span *= 2
    if span < width:
        result = np.minimum(result[: result.shape[0] - (width - span)], result[width - span :])
    return result


def _limit(buf: np.ndarray, ceiling: float, half_s: float = 0.010) -> np.ndarray:
    """Transparent look-ahead limiter. The gain never exceeds what keeps |x| <= ceiling.

    The required gain is min-filtered over +/- half_s and then averaged over the same
    window. At every peak the averaged gain is at most the required gain, so the peak
    is reduced before it arrives and the release is smooth.
    """
    peak = np.max(np.abs(buf), axis=0).astype(np.float64)
    if float(peak.max(initial=0.0)) <= ceiling:
        return buf
    required = np.minimum(1.0, ceiling / np.maximum(peak, 1e-12))
    half = max(1, int(round(half_s * SAMPLE_RATE)))
    padded = np.concatenate([np.ones(half), required, np.ones(half)])
    windowed = _sliding_min(padded, 2 * half + 1)
    csum = np.concatenate([[0.0], np.cumsum(windowed)])
    n = windowed.shape[0]
    idx = np.arange(n)
    lo = np.clip(idx - half, 0, n)
    hi = np.clip(idx + half + 1, 0, n)
    gain = (csum[hi] - csum[lo]) / (hi - lo)
    return buf * gain.astype(np.float32)[None, :]


def _plan_sections(sections: list[dict[str, Any]], bar_s: float, duration_s: float) -> list[dict[str, Any]]:
    """Keep each section's exact edges. Every section gets its own bar grid from its start,
    so a chapter opens on a downbeat and a rest can be exactly one bar long."""
    out: list[dict[str, Any]] = []
    for index, sec in enumerate(sections):
        start = float(sec["start_s"])
        end = min(float(sec["end_s"]), duration_s)
        span = end - start
        out.append({
            **sec,
            "index": index,
            "start_s": start,
            "end_s": end,
            "n_bars": int(math.ceil(span / bar_s - 1e-9)),
            "n_full_bars": int(math.floor(span / bar_s + 1e-9)),
        })
    return out


def _cadence_chords(mode: str, count: int) -> list[tuple[int, str]]:
    """The last `count` bars resolve to the tonic: dominant, then tonic."""
    if count <= 0:
        return []
    seq = [(5, "m"), (7, "M"), (0, "m"), (0, "m")] if mode == "minor" else [(2, "m"), (7, "M"), (0, "M"), (0, "M")]
    return seq[-count:]


def _section_chords(mode: str, mood: str, n_bars: int, cadence_bars: int) -> list[tuple[int, str]]:
    prog = PROGRESSIONS[(mode, mood)]
    cadence = _cadence_chords(mode, min(cadence_bars, n_bars))
    chords: list[tuple[int, str]] = []
    for i in range(n_bars):
        cad_index = i - (n_bars - len(cadence))
        chords.append(cadence[cad_index] if cadence and cad_index >= 0 else prog[i % 4])
    return chords


def _emit(buf: np.ndarray, sig: np.ndarray, t_s: float, end_s: float, gain: float = 1.0, pan: float = 0.0) -> None:
    """Place a voice inside its section. Anything past the section edge is cut with a 10 ms release."""
    if t_s >= end_s:
        return
    avail = int(round((end_s - t_s) * SAMPLE_RATE))
    if avail < sig.shape[0]:
        sig = sig[: max(avail, 0)].copy()
        if sig.shape[0]:
            ramp = min(sig.shape[0], int(round(EDGE_S * SAMPLE_RATE)))
            sig[-ramp:] *= np.linspace(1.0, 0.0, ramp)
    _place(buf, sig, t_s, gain, pan)


HIT_KINDS = ("stab", "thud", "blip")


def parse_hits(value: Any) -> list[Any]:
    """Hits as JSON or literal text: numbers (stabs) or {"t": seconds, "kind": "stab"|"thud"}."""
    parsed = parse_structured(value, "hits")
    if parsed is None:
        return []
    if isinstance(parsed, (int, float, dict)):
        parsed = [parsed]
    if isinstance(parsed, tuple):
        parsed = list(parsed)
    if not isinstance(parsed, list):
        raise AstridError("hits must be a list of times or {t, kind} objects", recovery_command='pass hits as [4.5, {"t": 7.4, "kind": "thud"}]')
    return parsed


def _two_note_blip() -> np.ndarray:
    """A tiny two-note pulse blip (A5 then E6), for filling a joke slot."""
    first = int(0.10 * SAMPLE_RATE)
    second = int(0.12 * SAMPLE_RATE)
    a = _oscillate("pulse", 0.25, np.full(first, hz(81))) * _ramped(first)
    b = _oscillate("pulse", 0.25, np.full(second, hz(88))) * _ramped(second)
    return 0.6 * np.concatenate([a, b])


def _mute_gain(n: int, windows: Sequence[tuple[float, float]]) -> np.ndarray | None:
    """Hard mutes: exact silence from start to end, with a 10 ms ramp just outside each edge."""
    if not windows:
        return None
    gain = np.ones(n)
    ramp = int(round(EDGE_S * SAMPLE_RATE))
    for start_s, end_s in windows:
        lo = max(0, int(round(start_s * SAMPLE_RATE)))
        hi = min(n, int(round(end_s * SAMPLE_RATE)))
        if hi <= lo:
            continue
        gain[lo:hi] = 0.0
        before = max(0, lo - ramp)
        if lo > before:
            gain[before:lo] = np.minimum(gain[before:lo], np.linspace(1.0, 0.0, lo - before, endpoint=False))
        after = min(n, hi + ramp)
        if after > hi:
            gain[hi:after] = np.minimum(gain[hi:after], np.linspace(0.0, 1.0, after - hi, endpoint=False))
    return gain


def normalize_hits(raw: Sequence[Any], duration_s: float) -> list[tuple[float, str]]:
    out: list[tuple[float, str]] = []
    for index, item in enumerate(raw):
        if isinstance(item, dict):
            if "t" not in item:
                raise AstridError(f"hits[{index}] needs a t in seconds")
            t_s, kind = float(item["t"]), str(item.get("kind", "stab"))
        else:
            t_s, kind = float(item), "stab"
        if kind not in HIT_KINDS:
            raise AstridError(f"hits[{index}].kind {kind!r} is not one of {list(HIT_KINDS)}")
        if not 0.0 <= t_s < duration_s:
            raise AstridError(f"hits[{index}] at {t_s}s falls outside the cue (0..{duration_s}s)")
        out.append((t_s, kind))
    return sorted(out)


def compose_music(
    *,
    duration_s: float,
    bpm: float,
    key: str,
    seed: int,
    sections: list[dict[str, Any]],
    hits: Sequence[Any] = (),
    duck: Sequence[tuple[float, float, float]] = (),
    vo_mask: Sequence[tuple[float, float]] = (),
    duck_db: float = -10.0,
    master_db: float = -16.0,
    style: str = "nes",
    mutes: Sequence[tuple[float, float]] = (),
) -> tuple[np.ndarray, dict[str, Any]]:
    """Render a stereo chiptune cue. Returns (float32 array (2, n), beats/section metadata)."""
    if style not in STYLES:
        raise AstridError(f"unsupported style {style!r}", valid_options=list(STYLES), recovery_command="use style nes")
    if not 30.0 <= bpm <= 240.0:
        raise AstridError("bpm must be between 30 and 240", recovery_command="pass bpm such as 132")
    if not 1.0 <= duration_s <= 3600.0:
        raise AstridError("duration_s must be between 1 and 3600", recovery_command="pass duration_s in seconds")
    root_pc, mode = parse_key(key)
    scale = _MAJOR if mode == "major" else _HARMONIC_MINOR
    n = int(round(duration_s * SAMPLE_RATE))
    beat_s = 60.0 / bpm
    bar_s = 4.0 * beat_s
    eighth_s = beat_s / 2.0
    sixteenth_s = beat_s / 4.0
    hit_list = normalize_hits(hits, duration_s)

    rng = np.random.default_rng(int(seed))
    bank = _noise_bank(rng)
    phrases = _motifs(rng)
    planned = _plan_sections(validate_sections(sections, duration_s), bar_s, duration_s)
    buf = np.zeros((2, n), dtype=np.float32)
    lead_pan, arp_pan = -0.35, 0.35
    chord_for_time: list[tuple[float, float, tuple[int, str]]] = []

    for sec in planned:
        mood, energy = sec["mood"], sec["energy"]
        if mood == "silent" or energy <= 0.0:
            continue
        origin, end_s, n_bars = sec["start_s"], sec["end_s"], sec["n_bars"]
        voicing = MOOD_VOICING[mood]
        chords = _section_chords(mode, mood, n_bars, sec["cadence_bars"])
        gain = 0.35 + 0.65 * energy
        lead_oct = 12 if energy >= 0.7 else 0
        arp_octave = energy >= 0.75
        arp_step = eighth_s if energy < 0.45 else sixteenth_s
        boundary_after = end_s < duration_s - 1e-6
        fill_bar = sec["n_full_bars"] - 1
        for i in range(n_bars):
            bar_t = origin + i * bar_s
            chord = chords[i]
            chord_pcs = _chord_pcs(root_pc, chord)
            chord_for_time.append((bar_t, bar_t + bar_s, chord))
            phrase = phrases[(i // 4) % 4]
            bar_in_phrase = i % 4

            # Lead: the motif, snapped to chord tones on the beats.
            if energy >= 0.3:
                for start8, dur8, degree in phrase:
                    if start8 // 8 != bar_in_phrase:
                        continue
                    k = start8 - 8 * bar_in_phrase
                    midi = _degree_midi(degree, root_pc, scale, 69) + lead_oct
                    if k % 2 == 0:
                        midi = _snap_chord_tone(midi, chord_pcs)
                    dur = dur8 * eighth_s * voicing["gap"]
                    sig = _oscillate("pulse", voicing["lead_duty"], np.full(int(dur * SAMPLE_RATE), hz(midi)))
                    sig = sig * _ramped(sig.shape[0])
                    _emit(buf, sig, bar_t + k * eighth_s, end_s, gain * 0.30 * (0.9 if energy >= 0.7 else 1.0), lead_pan)

            # Arpeggio: chord tones at tempo-locked steps.
            if energy >= 0.25:
                root_m = _reg(chord_pcs[0], 60)
                tones = [root_m, root_m + (4 if chord[1] == "M" else 3), root_m + 7, root_m + 12]
                steps = int(round(bar_s / arp_step))
                for step in range(steps):
                    midi = tones[_ARP_ORDER[step % 6]]
                    if arp_octave and (step // 6) % 2 == 1:
                        midi += 12
                    dur = arp_step * 0.8
                    sig = _oscillate("pulse", voicing["arp_duty"], np.full(int(dur * SAMPLE_RATE), hz(midi)))
                    sig = sig * _ramped(sig.shape[0])
                    _emit(buf, sig, bar_t + step * arp_step, end_s, gain * 0.16, arp_pan)

            # Bass: triangle roots, density by energy.
            bass_root = _reg(chord_pcs[0], 40)
            if energy < 0.25:
                pattern = [(0, 2.0, 0), (2, 2.0, 0)]
            elif energy < 0.6:
                pattern = [(0, 1.0, 0), (1, 1.0, 0), (2, 1.0, 7), (3, 1.0, 0)]
            else:
                pattern = [(0, 0.5, 0), (0.5, 0.5, 0), (1, 0.5, 12), (1.5, 0.5, 0), (2, 0.5, 7), (2.5, 0.5, 0), (3, 0.5, 12), (3.5, 0.5, 0)]
            for at_beat, beats_long, semis in pattern:
                midi = bass_root + semis
                dur = beats_long * beat_s * 0.92
                sig = _oscillate("triangle", 0.5, np.full(int(dur * SAMPLE_RATE), hz(midi)))
                sig = sig * _ramped(sig.shape[0])
                _emit(buf, sig, bar_t + at_beat * beat_s, end_s, gain * 0.45)

            # Drums: kick, snare, hats; density by energy.
            if energy >= 0.2:
                kick_slots = [0, 8]
                if energy >= 0.6:
                    kick_slots += [14]
                if energy >= 0.85:
                    kick_slots = [0, 4, 8, 12]
                for slot in kick_slots:
                    _emit(buf, _kick_sig(), bar_t + slot * sixteenth_s, end_s, gain * 0.55)
            if energy >= 0.35:
                for slot in (4, 12):
                    _emit(buf, _snare_sig(bank, rng), bar_t + slot * sixteenth_s, end_s, gain * 0.28)
            if energy >= 0.45:
                hat_slots = list(range(16)) if energy >= 0.8 else [2, 6, 10, 14]
                for slot in hat_slots:
                    _emit(buf, _hat_sig(bank, rng), bar_t + slot * sixteenth_s, end_s, gain * (0.14 if energy < 0.8 else 0.09))
            # Fill: the last complete bar before a boundary rolls into the next section.
            if i == fill_bar and energy >= 0.2 and boundary_after:
                for slot_index, slot in enumerate(range(8, 16)):
                    ramp = 0.35 + 0.65 * slot_index / 7.0
                    _emit(buf, _snare_sig(bank, rng, level=ramp), bar_t + slot * sixteenth_s, end_s, gain * 0.30)
                _emit(buf, _kick_sig(), bar_t + 12 * sixteenth_s, end_s, gain * 0.5)
            if i == 0 and sec["index"] > 0 and energy >= 0.5:
                _emit(buf, _crash_sig(bank, rng), bar_t, end_s, gain * 0.5)

    # Ducks (explicit gain and voice-over masks). Accents are mixed after the duck,
    # so a hit on speech still lands.
    duck_items = [(float(s), float(e), float(g)) for s, e, g in duck]
    duck_items += [(float(s), float(e), float(duck_db)) for s, e in vo_mask]
    gain_curve = _duck_gain(n, duck_items)
    if gain_curve is not None:
        buf *= gain_curve.astype(np.float32)[None, :]
    # Mutes are not ducks: the bed is gone for the window, so a joke or a dead-air beat lands in silence.
    mute_windows = []
    for start_s, end_s in mutes:
        if not 0.0 <= float(start_s) < float(end_s) <= duration_s:
            raise AstridError(f"mute [{start_s}, {end_s}] must satisfy 0 <= start < end <= duration_s")
        mute_windows.append((float(start_s), float(end_s)))
    mute_curve = _mute_gain(n, mute_windows)
    if mute_curve is not None:
        buf *= mute_curve.astype(np.float32)[None, :]

    # Accents on exact times. Stabs are voiced on the chord sounding then; thuds are low and dry.
    for hit_s, kind in hit_list:
        if kind == "thud":
            _place(buf, render_sfx("thud", variant=1, duration_s=0.42), hit_s, 0.6)
            continue
        if kind == "blip":
            _place(buf, _two_note_blip(), hit_s, 0.5)
            continue
        chord = next((c for start, end, c in chord_for_time if start <= hit_s < end), (0, "M"))
        base, third, fifth = _chord_pcs(root_pc, chord)
        stab_len = int(0.22 * SAMPLE_RATE)
        stab = np.zeros(stab_len)
        for pc in (base, third, fifth):
            stab += _oscillate("pulse", 0.5, np.full(stab_len, hz(_reg(pc, 60))))
        stab += 0.8 * _oscillate("triangle", 0.5, np.full(stab_len, hz(_reg(base, 40))))
        stab *= _decay(stab_len, 0.08) * 0.5
        _place(buf, stab, hit_s, 0.4)

    # Tempo grid per section, from each section's own start. Editors cut on these.
    beats: set[float] = set()
    downbeats: set[float] = set()
    phrase_starts: set[float] = set()
    for sec in planned:
        origin, span = sec["start_s"], sec["end_s"] - sec["start_s"]
        beats.update(round(origin + k * beat_s, 6) for k in range(int(math.ceil(span / beat_s - 1e-9))))
        downbeats.update(round(origin + k * bar_s, 6) for k in range(sec["n_bars"]))
        phrase_starts.update(round(origin + k * 4 * bar_s, 6) for k in range(int(math.ceil(sec["n_bars"] / 4))))

    if n > 0:
        active = ((np.abs(buf[0]) + np.abs(buf[1])) > 0.0).astype(np.float64)
        for ch in range(2):
            buf[ch] = _remove_dc_active(buf[ch].astype(np.float64), active, SAMPLE_RATE).astype(np.float32)
    fade = max(1, int(round(EDGE_S * SAMPLE_RATE)))
    if n > 2 * fade:
        buf[:, :fade] *= np.linspace(0.0, 1.0, fade, dtype=np.float32)[None, :]
        buf[:, -fade:] *= np.linspace(1.0, 0.0, fade, dtype=np.float32)[None, :]
    ceiling = 10.0 ** (PEAK_CEILING_DB / 20.0)
    rms = float(np.sqrt(np.mean(buf.astype(np.float64) ** 2))) if n else 0.0
    if rms > 0.0:
        buf *= np.float32(10.0 ** ((master_db - 20.0 * math.log10(rms)) / 20.0))
        buf = _limit(buf, ceiling)
        # The limiter pulls RMS down, so take one correction pass toward the target.
        rms_after = float(np.sqrt(np.mean(buf.astype(np.float64) ** 2)))
        shortfall_db = master_db - 20.0 * math.log10(max(rms_after, 1e-12))
        if shortfall_db > 0.05:
            buf *= np.float32(10.0 ** (shortfall_db / 20.0))
            buf = _limit(buf, ceiling)

    section_meta = []
    for sec in planned:
        chords = (
            [_chord_name(root_pc, c) for c in _section_chords(mode, sec["mood"], sec["n_bars"], sec["cadence_bars"])]
            if sec["mood"] != "silent" else []
        )
        section_meta.append({
            "index": sec["index"],
            "mood": sec["mood"],
            "energy": round(sec["energy"], 4),
            "start_s": round(sec["start_s"], 6),
            "end_s": round(sec["end_s"], 6),
            "grid_origin_s": round(sec["start_s"], 6),
            "bars": sec["n_bars"],
            "cadence_bars": min(sec["cadence_bars"], sec["n_bars"]),
            "chords": chords,
        })
    meta = {
        "bpm": bpm,
        "bar_s": round(bar_s, 6),
        "beats": sorted(beats),
        "downbeats": sorted(downbeats),
        "bars": sorted(phrase_starts),
        "sections": section_meta,
        "hits": [{"t": round(t, 6), "kind": kind} for t, kind in hit_list],
        "mutes": [[round(a, 6), round(b, 6)] for a, b in mute_windows],
        "key": f"{_NOTE_NAMES[root_pc]} {mode}",
    }
    return buf, meta


def to_int16_stereo(buf: np.ndarray) -> np.ndarray:
    """Float (channels, n) to interleaved-ready int16 (n, channels)."""
    clipped = np.clip(buf.T.astype(np.float64), -1.0, 1.0)
    return np.round(clipped * 32767.0).astype("<i2")


def measure_db(samples: np.ndarray) -> tuple[float, float]:
    """(peak dBFS, RMS dBFS) of float samples in -1..1; silence reports -120."""
    if samples.size == 0:
        return -120.0, -120.0
    peak = float(np.max(np.abs(samples)))
    rms = float(np.sqrt(np.mean(samples.astype(np.float64) ** 2)))
    to_db = lambda value: 20.0 * math.log10(value) if value > 1e-6 else -120.0  # noqa: E731
    return round(to_db(peak), 3), round(to_db(rms), 3)


# ---------------------------------------------------------------------------
# One-shot sound effects
# ---------------------------------------------------------------------------


def _svf_bandpass(x: np.ndarray, centre_hz: np.ndarray, q: float) -> np.ndarray:
    """Chamberlin state-variable bandpass with a per-sample centre frequency."""
    f = 2.0 * np.sin(np.pi * np.minimum(centre_hz, SAMPLE_RATE / 6.0) / SAMPLE_RATE)
    damp = 1.0 / q
    low = band = 0.0
    out = np.empty_like(x)
    for i in range(x.shape[0]):
        low += f[i] * band
        high = x[i] - low - damp * band
        band += f[i] * high
        out[i] = band
    return out


def _sfx_stamp(n: int, rng: np.random.Generator, f0: float) -> np.ndarray:
    t = np.arange(n) / SAMPLE_RATE
    body = _oscillate("triangle", 0.5, _glide(f0 * 0.5, f0 * 0.17, n, 0.04)) * np.exp(-t / 0.05)
    click = np.convolve(rng.standard_normal(n), np.ones(4) / 4.0, mode="same") * np.exp(-t / 0.008)
    return 0.9 * body + 0.5 * click


def _sfx_flip(n: int, rng: np.random.Generator, f0: float) -> np.ndarray:
    t = np.arange(n) / SAMPLE_RATE
    sweep = 700.0 * (3000.0 / 700.0) ** (1.0 - np.minimum(t / 0.1, 1.0))
    whip = _svf_bandpass(rng.standard_normal(n), sweep * (f0 / 440.0), q=3.0)
    tick = _oscillate("pulse", 0.25, np.full(n, 1500.0 * f0 / 440.0)) * (t < 0.01)
    return whip * np.exp(-t / 0.12) * np.minimum(t / 0.02, 1.0) + 0.3 * tick


def _sfx_blip(n: int, rng: np.random.Generator, f0: float) -> np.ndarray:
    t = np.arange(n) / SAMPLE_RATE
    freq = _glide(f0 * 2.0, f0, n, 0.03)
    return _oscillate("pulse", 0.25, freq) * np.exp(-t / 0.05)


def _sfx_whoosh(n: int, rng: np.random.Generator, f0: float) -> np.ndarray:
    u = np.arange(n) / max(n - 1, 1)
    centre = (350.0 + 2400.0 * np.sin(np.pi * u) ** 2) * (f0 / 440.0)
    env = np.sin(np.pi * u) ** 1.5
    return 1.6 * _svf_bandpass(rng.standard_normal(n), centre, q=2.5) * env


def _sfx_chime(n: int, rng: np.random.Generator, f0: float) -> np.ndarray:
    t = np.arange(n) / SAMPLE_RATE
    out = np.zeros(n)
    for ratio, amp, tau in ((1.0, 1.0, 0.7), (2.76, 0.45, 0.35), (5.4, 0.2, 0.18), (8.93, 0.08, 0.1)):
        out += amp * np.sin(2.0 * np.pi * f0 * ratio * t) * np.exp(-t / tau)
    return out * _ramped(n)


def _sfx_error(n: int, rng: np.random.Generator, f0: float) -> np.ndarray:
    half = n // 2
    first = _oscillate("pulse", 0.125, np.full(half, f0 * 0.5)) * _ramped(half)
    second = _oscillate("pulse", 0.125, np.full(n - half, f0 * 0.5 * 2 ** (-1 / 12))) * _ramped(n - half)
    return np.concatenate([first, second])


def _sfx_coin(n: int, rng: np.random.Generator, f0: float) -> np.ndarray:
    first_n = int(0.08 * SAMPLE_RATE)
    second_n = max(1, n - first_n)
    first = _oscillate("pulse", 0.25, np.full(first_n, 987.77 * f0 / 440.0)) * _ramped(first_n)
    second = _oscillate("pulse", 0.25, np.full(second_n, 1318.5 * f0 / 440.0)) * _decay(second_n, 0.12)
    return np.concatenate([first, second])


def _sfx_typewriter_tick(n: int, rng: np.random.Generator, f0: float) -> np.ndarray:
    t = np.arange(n) / SAMPLE_RATE
    burst = _highpass(rng.standard_normal(n)) * np.exp(-t / 0.006)
    body = np.sin(2.0 * np.pi * 2800.0 * f0 / 440.0 * t) * np.exp(-t / 0.003)
    return burst + 0.3 * body


def _sfx_wipe(n: int, rng: np.random.Generator, f0: float) -> np.ndarray:
    t = np.arange(n) / SAMPLE_RATE
    rise = np.clip(t / 0.25, 0.0, 1.0)
    centre = (300.0 + 3700.0 * rise) * (f0 / 440.0)
    fall = np.clip(1.0 - (t - 0.25) / 0.2, 0.0, 1.0)  # hold for 0.25 s, then close over 0.2 s
    env = np.minimum(t / 0.12, 1.0) * fall
    band = _svf_bandpass(rng.standard_normal(n), centre, q=6.0)
    pulse = _oscillate("pulse", 0.5, _glide(800.0 * f0 / 440.0, 200.0 * f0 / 440.0, n, 0.2))
    return env * (band + 0.25 * pulse)


def _sfx_thud(n: int, rng: np.random.Generator, f0: float) -> np.ndarray:
    t = np.arange(n) / SAMPLE_RATE
    body = _oscillate("triangle", 0.5, _glide(110.0 * f0 / 440.0, 38.0 * f0 / 440.0, n, 0.05))
    body *= np.exp(-t / 0.12)
    transient = np.convolve(rng.standard_normal(n), np.ones(24) / 24.0, mode="same") * np.exp(-t / 0.012)
    return 0.8 * body + 0.4 * transient


_SFX_BUILDERS = {
    "stamp": _sfx_stamp, "flip": _sfx_flip, "blip": _sfx_blip, "whoosh": _sfx_whoosh,
    "chime": _sfx_chime, "error": _sfx_error, "coin": _sfx_coin,
    "typewriter_tick": _sfx_typewriter_tick, "wipe": _sfx_wipe, "thud": _sfx_thud,
}


def render_sfx(kind: str, *, variant: int = 1, pitch: float = 0.0, duration_s: float | None = None) -> np.ndarray:
    """Mono float one-shot, peak-normalised to SFX_PEAK, exactly round(duration_s * 48000) samples."""
    if kind not in _SFX_BUILDERS:
        raise AstridError(
            f"unknown sfx kind {kind!r}",
            valid_options=list(SFX_KINDS),
            recovery_command=f"use one of {', '.join(SFX_KINDS)}",
        )
    seconds = float(duration_s) if duration_s is not None else SFX_DEFAULT_SECONDS[kind]
    if not 0.01 <= seconds <= 10.0:
        raise AstridError("duration_s must be between 0.01 and 10", recovery_command="pass duration_s in seconds")
    n = int(round(seconds * SAMPLE_RATE))
    seed = (zlib.crc32(kind.encode("utf-8")) + 7919 * int(variant)) & 0xFFFFFFFF
    rng = np.random.default_rng(seed)
    # Variant jitter keeps repeated pulls distinct without losing determinism.
    jitter = 1.0 + 0.02 * float(rng.uniform(-1.0, 1.0))
    f0 = hz(69 + float(pitch)) * jitter
    sig = _SFX_BUILDERS[kind](n, rng, f0)
    sig = np.asarray(sig, dtype=np.float64)[:n]
    if sig.shape[0] < n:
        sig = np.concatenate([sig, np.zeros(n - sig.shape[0])])
    env = _ramped(n)
    sig = _remove_dc(sig, n) * env
    if n:
        # Remove what the ramp reintroduced, shaped by the envelope, so the mean is exactly zero
        # and both ends stay at zero (the envelope is zero there).
        sig = sig - (sig.mean() / max(env.mean(), 1e-12)) * env
    peak = float(np.max(np.abs(sig))) if n else 0.0
    if peak > 0.0:
        sig = sig * (SFX_PEAK / peak)
    return sig


def to_int16_mono(samples: np.ndarray) -> np.ndarray:
    return np.round(np.clip(samples, -1.0, 1.0) * 32767.0).astype("<i2")


__all__ = [
    "SAMPLE_RATE", "EDGE_S", "PEAK_CEILING_DB", "MOODS", "SFX_KINDS", "SFX_DEFAULT_SECONDS",
    "compose_music", "measure_db", "normalize_hits", "parse_hits", "parse_key", "parse_number_list", "parse_structured",
    "render_sfx", "to_int16_mono", "to_int16_stereo", "validate_sections",
]
