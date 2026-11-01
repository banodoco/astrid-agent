"""The deterministic ducking core, independent of Runtime admission and timeline edits."""
from __future__ import annotations

import math
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Callable, NotRequired, TypedDict

import numpy as np
from numpy.typing import NDArray

from astrid.core.contracts.errors import AstridError
from astrid.core.foundation.hash import sha256_file
from astrid.core.timeline.audio_curves import registry
from astrid.packs.music.actions.duck._audio import (
    TAG_KIND,
    Audio,
    integrated,
    load_audio,
    moments,
    read_duck_tag,
    true_peak,
    weighted_power,
    write_duck_wav,
)


@dataclass(frozen=True)
class Placement:
    path: Path
    start: float
    duration: float
    from_s: float = 0.
    speed: float = 1.
    volume: float = 1.
    fade_in: float = 0.
    fade_out: float = 0.
    id: str = ""
    fade_in_curve: str = "linear"
    fade_out_curve: str = "linear"


CAP_LU = 4.
SLEW_DB_PER_S = 2.
SMOOTH_S = 1.
FLOOR_DB = -12.
MERGE_GAP_S = 1.2


@dataclass(frozen=True)
class DuckResult:
    report: dict
    times: np.ndarray
    gain_db: np.ndarray
    target_db: np.ndarray


class _Line(TypedDict):
    id: str
    start_s: float
    end_s: float
    vo_i: float
    music_i_before: float
    gap_before_lu: float
    bed_db: float
    voiced: NotRequired[list[tuple[float, float]]]
    sample_start: NotRequired[int]
    sample_end: NotRequired[int]
    music_i_after: NotRequired[float | None]
    gap_after_lu: NotRequired[float | None]
    worst_voiced_gap_lu: NotRequired[float | None]
    floor_held: NotRequired[bool]


def select_music_clip(clips: list[dict], clip: str | None = None) -> dict:
    candidates = [c for c in clips if c.get("track") == "music" and (clip is None or c.get("id") == clip)]
    if len(candidates) != 1:
        raise AstridError("music.duck requires one music clip; pass clip explicitly when there are several")
    selected = candidates[0]
    app = selected.get("app") or {}
    nested = app.get("astrid") or {}
    if app.get("duck") is not None or nested.get("duck") is not None:
        raise AstridError("music.duck refuses a clip carrying app.duck; remove that intent before baking ducking")
    return selected


def _validate_placement(p: Placement) -> None:
    values = (p.start, p.duration, p.from_s, p.speed, p.volume, p.fade_in, p.fade_out)
    if (not all(math.isfinite(v) for v in values) or p.duration <= 0 or p.speed <= 0
            or min(p.from_s, p.volume, p.fade_in, p.fade_out) < 0):
        raise AstridError(f"invalid audio placement: {p.id}")
    for curve in (p.fade_in_curve, p.fade_out_curve):
        if curve not in registry():
            raise AstridError(f"unknown audio fade curve {curve!r}")


def _fade(progress: np.ndarray, curve: str) -> np.ndarray:
    p = np.clip(progress, 0, 1)
    spec = registry()[curve]
    kind = spec["kind"]
    if kind == "equal-power":
        return np.sin(p * math.pi / 2)
    if kind == "exponential":
        exponent = float(spec["exponent"])
        return np.expm1(p * exponent) / math.expm1(exponent)
    if kind == "s-curve":
        return p * p * (3 - 2 * p)
    return p


def placed_samples(audio: Audio, placement: Placement, rate: int,
                   start: float, duration: float) -> np.ndarray:
    """Sample one placed clip over a timeline interval, including source trim/rate and look."""
    count = round(duration * rate)
    local = start + np.arange(count) / rate - placement.start
    positions = (placement.from_s + local * placement.speed) * audio.rate
    source_time_end = placement.from_s + placement.duration * placement.speed
    if source_time_end > len(audio.samples) / audio.rate + 1 / rate:
        raise AstridError(f"clip {placement.id!r} runs past its source audio")
    # Linear sample-clock interpolation is deterministic and leaves speed=1 samples exact.
    result = np.stack([np.interp(positions, np.arange(len(audio.samples)), audio.samples[:, c],
                                  left=0., right=0.) for c in range(audio.samples.shape[1])], axis=1)
    gain = np.full(count, placement.volume)
    gain[(local < 0) | (local >= placement.duration)] = 0
    if placement.fade_in:
        gain *= _fade(local / placement.fade_in, placement.fade_in_curve)
    if placement.fade_out:
        gain *= _fade((placement.duration - local) / placement.fade_out, placement.fade_out_curve)
    result *= gain[:, None]
    return result


def _voiced(samples: np.ndarray, rate: int) -> list[tuple[float, float]]:
    hop = max(1, round(.02 * rate))
    power = (samples * samples).sum(axis=1)
    starts = np.arange(0, len(power), hop)
    energy = np.add.reduceat(power, starts) / np.minimum(hop, len(power) - starts)
    db = 10 * np.log10(np.maximum(energy, 1e-30))
    threshold = max(-70., float(np.percentile(db, 95)) - 25.)
    on = np.flatnonzero(db > threshold)
    return [(float(starts[i] / rate), float(min(starts[i] + hop, len(power)) / rate)) for i in on]


def gain_curve(target: np.ndarray, dt: float) -> np.ndarray:
    """Loudest slew-bounded minorant, followed by the requested positive Hann smoothing."""
    g = target.copy()
    step = SLEW_DB_PER_S * dt
    for i in range(1, len(g)):
        g[i] = min(g[i], g[i - 1] + step)
    for i in range(len(g) - 2, -1, -1):
        g[i] = min(g[i], g[i + 1] + step)
    if SMOOTH_S > 0:
        n = int(SMOOTH_S / dt) | 1
        window = np.hanning(n + 2)[1:-1]
        window /= window.sum()
        g = np.convolve(np.pad(g, (n, n), mode="edge"), window, mode="same")[n:-n]
    g[np.abs(g) < 1e-12] = 0
    return np.minimum(g, 0.)


def duck_audio(music: Placement, voices: list[Placement], output: Path, *,
               bed_lu: float = 8.,
               resolve_source: Callable[[str], Path] | None = None) -> DuckResult:
    if not math.isfinite(bed_lu) or bed_lu < 0:
        raise AstridError("bed_lu must be finite and nonnegative")
    _validate_placement(music)
    if Path(output).resolve() == Path(music.path).resolve():
        raise AstridError("duck output must be a new file")
    source_path = Path(music.path)
    seen = set()
    while tag := read_duck_tag(source_path):
        digest = tag.get("source_digest")
        if not isinstance(digest, str) or digest in seen or resolve_source is None:
            raise AstridError("tagged music source cannot be resolved by digest; restore the original source")
        seen.add(digest)
        try:
            source_path = Path(resolve_source(digest))
        except Exception as exc:
            raise AstridError(f"original duck source {digest} cannot be found; restore it before re-running") from exc
        if not source_path.is_file() or "sha256:" + sha256_file(source_path) != digest:
            raise AstridError(f"original duck source {digest} is missing or has the wrong digest")
    if Path(output).resolve() == source_path.resolve():
        raise AstridError("duck output must not overwrite the original source")
    source_digest = "sha256:" + sha256_file(source_path)
    audio = load_audio(source_path)
    music = replace(music, path=source_path)
    rate = audio.rate
    played = placed_samples(audio, music, rate, music.start, music.duration)
    music_power = weighted_power(played, rate)
    # Voice channels are mixed with a constant-power mono pan, matching stereo playback.
    stem = np.zeros((len(played), 2))
    lines: list[_Line] = []
    ignored = []
    for voice in sorted(voices, key=lambda p: (p.start, p.id)):
        _validate_placement(voice)
        a = max(voice.start, music.start)
        b = min(voice.start + voice.duration, music.start + music.duration)
        if a >= b or voice.volume == 0:
            continue
        data = load_audio(Path(voice.path))
        x = placed_samples(data, voice, rate, a, b - a)
        if x.shape[1] == 1:
            x = np.repeat(x / math.sqrt(2), 2, axis=1)
        if x.shape[1] != 2:
            raise AstridError("voice clips must be mono or stereo")
        s = round((a - music.start) * rate)
        e = min(s + len(x), len(stem))
        x = x[:e - s]
        stem[s:e] += x
        vi = integrated(weighted_power(x, rate), rate)
        mi = integrated(music_power[s:e], rate)
        voiced = [(a + lo, a + hi) for lo, hi in _voiced(x, rate)]
        if vi is None or mi is None or not voiced:
            ignored.append(voice.id)
            continue
        line_bed = min(0., vi - bed_lu - mi)
        lines.append({"id": voice.id, "start_s": a, "end_s": b, "vo_i": vi,
                      "music_i_before": mi, "gap_before_lu": vi - mi,
                      "bed_db": line_bed, "voiced": voiced, "sample_start": s, "sample_end": e})
    if not lines:
        raise AstridError("no measurable voice lines overlap the selected music clip")

    # Control times cover the entire source file, so re-trimming the baked asset is safe.
    dt = .01
    source_dt = dt * music.speed
    times = np.arange(0, len(audio.samples) / rate + source_dt, source_dt)
    timeline = music.start + (times - music.from_s) / music.speed
    relative = timeline - music.start
    ml = moments(music_power, rate, relative)
    intervals = sorted(iv for line in lines for iv in line["voiced"])
    spans: list[list[float]] = []
    for a, b in intervals:
        if spans and a - spans[-1][1] < MERGE_GAP_S:
            spans[-1][1] = max(spans[-1][1], b)
        else:
            spans.append([a, b])
    inside: NDArray[np.bool_] = np.zeros(len(times), dtype=bool)
    for a, b in spans:
        inside |= (timeline >= a) & (timeline < b)
    bed = np.zeros(len(times))
    cap = np.zeros(len(times))
    voiced_mask: NDArray[np.bool_] = np.zeros(len(times), dtype=bool)
    def require(line: _Line, zone: NDArray[np.bool_]) -> None:
        bed[zone] = np.minimum(bed[zone], line["bed_db"])
        cap[zone] = np.minimum(cap[zone], line["vo_i"] - CAP_LU - ml[zone])

    # Every line owns its full speech extent. Overlapping lines combine by
    # minimum, so a short louder line cannot erase a longer quieter line.
    extents = sorted((line["voiced"][0][0], line["voiced"][-1][1], i)
                     for i, line in enumerate(lines))
    previous_end = None
    previous_line = None
    for start, end, index in extents:
        line = lines[index]
        require(line, (timeline >= start) & (timeline < end))
        for a, b in line["voiced"]:
            voiced_mask |= (timeline >= a) & (timeline < b)
        if previous_end is not None and previous_line is not None and 0 < start - previous_end < MERGE_GAP_S:
            gap = (timeline >= previous_end) & (timeline < start)
            require(previous_line, gap)
            require(line, gap)
        if previous_end is None or end > previous_end:
            previous_end, previous_line = end, line
    # A merged speech span includes its natural gaps. Keep the cap in force
    # there too; resetting it between syllables would introduce gain rebounds.
    raw_target = np.where(inside, np.minimum(np.minimum(bed, cap), 0.), 0.)
    target = np.maximum(FLOOR_DB, raw_target)
    gain = gain_curve(target, dt)
    source_gain = np.interp(np.arange(len(audio.samples)) / rate, times, gain)
    tag = {"kind": TAG_KIND, "source_digest": source_digest, "parameters": {"bed_lu": bed_lu}}
    write_duck_wav(Path(output), audio, source_gain, tag)
    played_gain = np.interp(music.from_s + np.arange(len(played)) / rate * music.speed, times, gain)
    played *= np.power(10., played_gain[:, None] / 20)
    after_power = weighted_power(played, rate)
    stem_power = weighted_power(stem, rate)
    vo_m = moments(stem_power, rate, relative)
    after_m = moments(after_power, rate, relative)
    floor_lines = []
    for line in lines:
        lo, hi = line.pop("sample_start"), line.pop("sample_end")
        after = integrated(after_power[lo:hi], rate)
        line["music_i_after"] = after
        line["gap_after_lu"] = None if after is None else line["vo_i"] - after
        select = (timeline >= line["start_s"]) & (timeline < line["end_s"])
        eligible = select & voiced_mask & (vo_m >= line["vo_i"] - 6)
        line["worst_voiced_gap_lu"] = float((vo_m - after_m)[eligible].min()) if eligible.any() else None
        line["floor_held"] = bool((raw_target[select] < FLOOR_DB).any())
        if line["floor_held"]:
            floor_lines.append(line["id"])
        line.pop("voiced")
    gaps = [line["gap_after_lu"] for line in lines if line["gap_after_lu"] is not None]
    worst = [line["worst_voiced_gap_lu"] for line in lines if line["worst_voiced_gap_lu"] is not None]
    report = {"schema_version": 1, "parameters": {"bed_lu": bed_lu}, "source_digest": source_digest,
              "clip": music.id, "sample_rate": rate, "channels": audio.samples.shape[1],
              "sample_format": ("float" if audio.floating else "pcm") + str(audio.bits),
              "frames": len(audio.samples), "speech_spans": spans, "lines": lines,
              "unmeasurable_lines": ignored,
              "gap_lu": {"median": float(np.median(gaps)) if gaps else None,
                         "min": min(gaps) if gaps else None, "max": max(gaps) if gaps else None},
              "worst_voiced_gap_lu": min(worst) if worst else None,
              "floor_held_lines": floor_lines,
              "max_slope_db_per_s": float(np.abs(np.diff(gain)).max() / dt),
              "max_excess_db": max(0., float((gain - target).max())),
              "true_peak_dbfs": true_peak(Path(output)),
              "method": "BS.1770 K weighting; EBU R128 400ms/100ms absolute and relative gates"}
    return DuckResult(report, times, gain, target)
