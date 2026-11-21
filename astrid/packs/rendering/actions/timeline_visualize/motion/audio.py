"""A decimated loudness envelope of the timeline's audio clips (no render needed).

The contact sheet and the motion sheet draw a thin VO / music lane with
silence marks from the clips themselves: each audio clip's materialized WAV
is read only over the part the timeline plays (``from``…``to``), decimated,
and reduced to one RMS value per bin, scaled by the clip's ``volume``. Cost is
bounded: at most ``MAX_SECONDS`` of source per call and a fixed decimation, so
a 90 s film reads in well under a second. Non-WAV media is decoded with ffmpeg
when it is on PATH; media that cannot be read at all is named, and its span is
*unknown* (never reported as silence).

Silence is only a problem where a line is expected: ``expected`` holds the
spans of the timeline's VO clips (and of shots whose script or plan has a line
but no VO clip). ``missing_lines`` reports quiet only inside those spans, so an intended
silence (a logo opening, the gap between two lines) is not flagged
(``missing_lines``); ``dead_air`` still reports every silent stretch of the mix.
"""
from __future__ import annotations

import math
import shutil
import subprocess
import wave
from array import array
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable, Mapping

from .model import Element

LANES = ("vo", "music", "sfx")
TARGET_RATE = 2000  # samples per second kept after decimation (envelope only)
MAX_SECONDS = 600.0
SILENCE_DB = -45.0
SILENCE_MIN_S = 0.4
MIN_BIN_S = 0.02
UNKNOWN_COLOUR = "#6b7280"


@dataclass
class Envelope:
    start: float
    step: float
    lanes: dict[str, list[float]]  # RMS in 0..1 per bin
    notes: list[str] = field(default_factory=list)
    # spans where a line is expected (VO clips, scripted shots); None = unknown, flag everywhere
    expected: list[tuple[float, float]] | None = None
    # spans whose audio could not be read: never called silent
    unknown: list[tuple[float, float]] = field(default_factory=list)

    def db(self, lane: str, index: int) -> float:
        value = self.lanes[lane][index]
        return 20 * math.log10(value) if value > 1e-6 else -120.0

    def level_db(self, lane: str, begin: float, finish: float) -> float:
        """RMS level of ``lane`` over ``[begin, finish)`` in dBFS (power mean of the bins)."""
        first = max(0, int((begin - self.start) / self.step))
        last = min(len(self.lanes[lane]), max(first + 1, int((finish - self.start) / self.step)))
        values = self.lanes[lane][first:last]
        power = sum(v * v for v in values) / len(values) if values else 0.0
        return 10 * math.log10(power) if power > 1e-12 else -120.0

    def _bin_in(self, index: int, spans: Iterable[tuple[float, float]]) -> bool:
        t = self.start + (index + 0.5) * self.step
        return any(lo <= t < hi for lo, hi in spans)

    def dead_air(self, *, minimum: float = 0.3) -> list[tuple[float, float]]:
        """Stretches where VO, music and sfx are all quiet (true silence on the mix)."""
        quiet = [
            all(self.db(lane, index) < SILENCE_DB for lane in LANES)
            for index in range(len(self.lanes["vo"]))
        ]
        return self._runs(quiet, minimum)

    def missing_lines(self, *, minimum: float = 1.0) -> list[tuple[float, float]]:
        """Silence worth flagging: the mix is quiet *where a line is expected* (``expected``), for ``minimum`` s+.

        Intended silence (no line placed or planned there) and audio that could not be read (``unknown``)
        are never flagged. Without ``expected`` (no line information) every silent stretch counts."""
        quiet = [
            all(self.db(lane, index) < SILENCE_DB for lane in LANES)
            and (self.expected is None or self._bin_in(index, self.expected))
            and not self._bin_in(index, self.unknown)
            for index in range(len(self.lanes["vo"]))
        ]
        return self._runs(quiet, minimum)

    def _runs(self, flags: list[bool], minimum: float) -> list[tuple[float, float]]:
        runs, begin = [], None
        for index, value in enumerate(flags + [False]):
            if value and begin is None:
                begin = index
            elif not value and begin is not None:
                if (index - begin) * self.step >= minimum - 1e-9:
                    runs.append((self.start + begin * self.step, self.start + index * self.step))
                begin = None
        return runs

    def silences(self, lane: str = "vo", *, minimum: float = SILENCE_MIN_S, quiet_db: float = SILENCE_DB) -> list[tuple[float, float]]:
        """Runs of bins below ``quiet_db`` lasting at least ``minimum`` seconds."""
        runs, begin = [], None
        values = self.lanes.get(lane) or []
        for index in range(len(values) + 1):
            quiet = index < len(values) and self.db(lane, index) < quiet_db
            if quiet and begin is None:
                begin = index
            elif not quiet and begin is not None:
                if (index - begin) * self.step >= minimum - 1e-9:
                    runs.append((self.start + begin * self.step, self.start + index * self.step))
                begin = None
        return runs


def lane_of(element: Element) -> str | None:
    if not element.audio:
        return None
    if element.track in ("music",):
        return "music"
    if element.track in ("sfx",):
        return "sfx"
    return "vo"


def _read_window(path: Path, offset: float, seconds: float) -> tuple[array, int]:
    """Mono 16-bit-equivalent samples of ``seconds`` from ``offset``, decimated to ~TARGET_RATE.

    WAV is read directly; anything else (mp4, m4a, mp3 …) is decoded by ffmpeg when it is on PATH."""
    try:
        return _read_wav_window(path, offset, seconds)
    except (wave.Error, EOFError):
        decoded = _ffmpeg_window(path, offset, seconds)
        if decoded is None:
            raise
        return decoded


def _ffmpeg_window(path: Path, offset: float, seconds: float) -> tuple[array, int] | None:
    """Decode ``seconds`` of any audio ffmpeg reads to mono s16 at TARGET_RATE; None when it cannot."""
    binary = shutil.which("ffmpeg")
    if binary is None or seconds <= 0:
        return None
    argv = [binary, "-v", "error", "-nostdin", "-ss", f"{max(0.0, offset):.3f}", "-t", f"{seconds:.3f}",
            "-i", str(path), "-vn", "-ac", "1", "-ar", str(TARGET_RATE), "-f", "s16le", "-"]
    try:
        proc = subprocess.run(argv, capture_output=True, timeout=60, check=False)
    except (OSError, subprocess.TimeoutExpired):
        return None
    if proc.returncode != 0 or not proc.stdout:
        return None
    samples = array("h")
    samples.frombytes(proc.stdout[: len(proc.stdout) - len(proc.stdout) % 2])
    return array("f", (value / 32768.0 for value in samples)), TARGET_RATE


def _read_wav_window(path: Path, offset: float, seconds: float) -> tuple[array, int]:
    with wave.open(str(path)) as source:
        rate, channels, width = source.getframerate(), source.getnchannels(), source.getsampwidth()
        start = max(0, int(offset * rate))
        count = max(0, min(source.getnframes() - start, int(seconds * rate)))
        source.setpos(start)
        raw = source.readframes(count)
    step = max(1, rate // TARGET_RATE)
    frame_bytes = width * channels
    out = array("f")
    if width == 2:
        samples = array("h")
        samples.frombytes(raw[: len(raw) - len(raw) % 2])
        out.extend(samples[i] / 32768.0 for i in range(0, len(samples), step * channels))
    elif width == 4:
        samples = array("i")
        samples.frombytes(raw[: len(raw) - len(raw) % 4])
        out.extend(samples[i] / 2147483648.0 for i in range(0, len(samples), step * channels))
    elif width == 1:
        out.extend((raw[i] - 128) / 128.0 for i in range(0, len(raw), step * frame_bytes))
    else:
        raise ValueError(f"unsupported WAV sample width {width}")
    return out, max(1, rate // step)


def envelope(
    elements: Iterable[Element], files: Mapping[str, str | Path], start: float, end: float, *, bins: int,
    scripted: Iterable[tuple[float, float]] = (),
) -> Envelope:
    """One RMS value per bin over ``[start, end)`` for the VO, music and sfx lanes."""
    # a bin holds at least MIN_BIN_S of signal so its RMS is a level, not a sample
    elements = list(elements)
    bins = max(1, min(int(bins), int((end - start) / MIN_BIN_S) or 1))
    step = max(1e-3, (end - start) / bins)
    lanes = {lane: [0.0] * bins for lane in LANES}
    energy = {lane: [0.0] * bins for lane in LANES}
    notes: list[str] = []
    unknown: list[tuple[float, float]] = []
    budget = MAX_SECONDS
    for element in elements:
        lane = lane_of(element)
        if lane is None or element.end <= start or element.start >= end:
            continue
        key = element.asset or ""
        path = files.get(key) or files.get(key.rsplit(":", 1)[-1])
        if not path:
            notes.append(f"{element.short_id}: no materialized file (its {element.start:.2f}–{element.end:.2f} s "
                         "are not judged)")
            unknown.append((element.start, element.end))
            continue
        path = Path(path)
        lo, hi = max(start, element.start), min(end, element.end)
        if hi - lo > budget:
            notes.append(f"audio budget reached at {element.short_id}")
            break
        budget -= hi - lo
        offset = float(element.clip.get("from") or 0.0) + (lo - element.start)
        try:
            samples, rate = _read_window(path, offset, hi - lo)
        except (wave.Error, EOFError, ValueError, OSError) as exc:
            notes.append(f"{element.short_id}: audio not readable ({type(exc).__name__}); its "
                         f"{lo:.2f}–{hi:.2f} s are not judged")
            unknown.append((lo, hi))
            continue
        gain = float(element.clip.get("volume") if element.clip.get("volume") is not None else 1.0)
        for index, value in enumerate(samples):
            t = lo + index / rate
            slot = int((t - start) / step)
            if 0 <= slot < bins:
                energy[lane][slot] += (value * gain) ** 2
                lanes[lane][slot] += 1.0  # sample count, turned into RMS below
    for lane in LANES:
        for slot in range(bins):
            count = lanes[lane][slot]
            lanes[lane][slot] = math.sqrt(energy[lane][slot] / count) if count else 0.0
    return Envelope(start, step, lanes, notes, expected=expected_line_spans(elements, scripted),
                    unknown=unknown)


def expected_line_spans(elements: Iterable[Element],
                        scripted: Iterable[tuple[float, float]] = ()) -> list[tuple[float, float]]:
    """Where a line is expected: every VO clip's span, plus scripted shot spans no VO clip covers."""
    spans = sorted((e.start, e.end) for e in elements if lane_of(e) == "vo" and e.end > e.start)
    for lo, hi in scripted:
        if hi > lo and not any(a < hi and lo < b for a, b in spans):
            spans.append((float(lo), float(hi)))
    return sorted(spans)


def draw_lane(draw, env: Envelope, *, x0: float, x1: float, y0: float, y1: float, colours: Mapping[str, str],
              silence_colour: str, silence_lane: str = "vo", dead_air_colour: str = "#ef4444") -> None:
    """Music grows up from the midline, VO down, sfx as ticks on it; silences as a strip under the lane."""
    bins = len(next(iter(env.lanes.values())))
    width = (x1 - x0) / max(1, bins)
    mid = (y0 + y1 - 4) / 2
    half = (y1 - y0 - 5) / 2

    def height(value: float) -> float:
        # -60 dB .. 0 dB onto 0 .. half
        db = 20 * math.log10(value) if value > 1e-6 else -120.0
        return max(0.0, min(1.0, (db + 60) / 60)) * half

    for lane, sign in (("music", -1), ("vo", 1), ("sfx", 0)):
        colour = colours.get(lane)
        if not colour:
            continue
        for index, value in enumerate(env.lanes.get(lane) or ()):
            h = height(value)
            if h < 0.5:
                continue
            x = x0 + index * width
            if sign:
                top, bottom = (mid - h, mid) if sign < 0 else (mid, mid + h)
            else:
                top, bottom = mid - h / 2, mid + h / 2
            draw.rectangle((x, top, x + max(1.0, width) - 0.2, bottom), fill=colour)
    for begin, finish in env.silences(silence_lane):
        a = x0 + (begin - env.start) / env.step * width
        b = x0 + (finish - env.start) / env.step * width
        draw.rectangle((a, y1 - 3, b, y1 - 1), fill=silence_colour)
    for begin, finish in env.unknown:  # audio that could not be read: grey ticks, never "silent"
        a = max(x0, x0 + (begin - env.start) / env.step * width)
        b = min(x1, x0 + (finish - env.start) / env.step * width)
        for x in range(int(a), int(b), 6):
            draw.line((x, mid - 3, x + 3, mid + 3), fill=UNKNOWN_COLOUR, width=1)
    for begin, finish in env.missing_lines():  # red: silent where a line is placed or planned
        a = x0 + (begin - env.start) / env.step * width
        b = x0 + (finish - env.start) / env.step * width
        draw.rectangle((a, y0, b, y1 - 1), outline=dead_air_colour, width=1)
        draw.rectangle((a, y1 - 3, b, y1 - 1), fill=dead_air_colour)
