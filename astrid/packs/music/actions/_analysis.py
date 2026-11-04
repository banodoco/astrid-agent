"""Bounded-memory, numpy-only features for imported music analysis."""

from __future__ import annotations

import math
import subprocess
from pathlib import Path

import numpy as np

from astrid.pack_api import AstridError

RATE = 11025
HOP = 256
FFT = 2048
FRAME_S = HOP / RATE


def decode(path: Path) -> np.ndarray:
    if not path.is_file():
        raise AstridError(f"audio file does not exist: {path}")
    proc = subprocess.run(
        ["ffmpeg", "-nostdin", "-v", "error", "-i", str(path), "-vn", "-ac", "1",
         "-ar", str(RATE), "-f", "f32le", "pipe:1"], capture_output=True, check=False,
    )
    if proc.returncode:
        raise AstridError(f"ffmpeg could not decode {path.name}: {proc.stderr.decode(errors='replace').strip()}")
    audio = np.frombuffer(proc.stdout, dtype="<f4")
    if len(audio) < FFT:
        raise AstridError(f"audio is too short to analyze: {path.name}")
    return audio


def features(audio: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    """Half-wave spectral flux and whitened pitch-class energy at 43 Hz."""
    nframes = 1 + (len(audio) - FFT) // HOP
    onset = np.zeros(nframes, np.float32)
    chroma = np.zeros((nframes, 12), np.float32)
    freqs = np.fft.rfftfreq(FFT, 1 / RATE)
    valid = (freqs >= 60) & (freqs <= 2000)
    bins = np.flatnonzero(valid)
    midi = np.rint(69 + 12 * np.log2(freqs[valid] / 440)).astype(int)
    classes = midi % 12
    window = np.hanning(FFT).astype(np.float32)
    previous = np.zeros(len(freqs), np.float32)
    # Batch the FFT so a long recording never materializes a giant spectrogram.
    for start in range(0, nframes, 256):
        end = min(start + 256, nframes)
        frames = np.stack([audio[i * HOP:i * HOP + FFT] for i in range(start, end)])
        magnitude = np.abs(np.fft.rfft(frames * window, axis=1)).astype(np.float32)
        extended = np.vstack([previous[None], magnitude])
        onset[start:end] = np.maximum(np.diff(extended, axis=0), 0).sum(axis=1)
        previous = magnitude[-1]
        for pitch_class in range(12):
            chroma[start:end, pitch_class] = np.sqrt((magnitude[:, bins[classes == pitch_class]] ** 2).sum(axis=1))
    onset = np.maximum(onset - np.median(onset), 0)
    if onset.max() > 0:
        onset /= onset.max()
    # Smooth chord changes over ~140 ms and normalize each frame, so matching
    # tracks harmonic content rather than overall level or instrumentation.
    kernel = np.ones(7, np.float32) / 7
    chroma = np.stack([np.convolve(chroma[:, i], kernel, mode="same") for i in range(12)], axis=1)
    chroma = np.log1p(chroma)
    chroma -= chroma.mean(axis=1, keepdims=True)
    chroma /= np.maximum(np.linalg.norm(chroma, axis=1, keepdims=True), 1e-8)
    return onset, chroma


def midi_onsets(notes: list, length: int) -> np.ndarray:
    signal = np.zeros(length, np.float32)
    for note in notes:
        index = round(note.start_s / FRAME_S)
        if 0 <= index < length:
            signal[index] += max(0.1, note.velocity / 127)
    kernel_x = np.arange(-5, 6)
    kernel = np.exp(-0.5 * (kernel_x / 2.0) ** 2).astype(np.float32)
    signal = np.convolve(signal, kernel, mode="same")
    return signal / max(float(signal.max()), 1e-8)


def _normalized_match(reference: np.ndarray, target: np.ndarray) -> tuple[int, float]:
    """Sliding normalized correlation, with partial overlaps forbidden."""
    if len(reference) > len(target):
        raise AstridError("reference window is longer than the target")
    size = 1 << (len(reference) + len(target) - 2).bit_length()
    numerator = np.fft.irfft(
        np.fft.rfft(target, size) * np.fft.rfft(reference[::-1], size), size
    )[len(reference) - 1:len(target)]
    cumulative = np.concatenate(([0.0], np.cumsum(target.astype(np.float64) ** 2)))
    energy = cumulative[len(reference):] - cumulative[:-len(reference)]
    denom = np.sqrt(np.maximum(energy * float(reference @ reference), 1e-12))
    scores = numerator / denom
    index = int(np.argmax(scores))
    return index, float(scores[index])


def align_midi(notes: list, target_onset: np.ndarray, ref_s: float | None, window_s: float) -> dict:
    duration = max(note.end_s for note in notes)
    length = max(1, math.ceil(duration / FRAME_S) + 1)
    reference = midi_onsets(notes, length)
    origin = 0
    if ref_s is not None:
        origin = max(0, round((ref_s - window_s / 2) / FRAME_S))
        reference = reference[origin:min(len(reference), round((ref_s + window_s / 2) / FRAME_S))]
    if float(reference @ reference) < 1e-5:
        raise AstridError("MIDI reference window has no notes")
    padding = round(2 / FRAME_S)
    padded_target = np.pad(target_onset, (padding, padding))
    position, score = _normalized_match(reference, padded_target)
    rng = np.random.default_rng(0)
    _, baseline = _normalized_match(rng.permutation(reference), padded_target)
    return {"offset_s": round((position - origin - padding) * FRAME_S, 4), "tempo_ratio": 1.0,
            "score": round(score, 4), "baseline": round(baseline, 4),
            "confidence": round(max(0.0, score - baseline), 4)}


def align_audio(reference: np.ndarray, target: np.ndarray, ref_s: float | None, window_s: float) -> dict:
    if ref_s is None:
        ref_s = len(reference) * FRAME_S / 2
    center = round(ref_s / FRAME_S)
    half = max(20, round(window_s / (2 * FRAME_S)))
    lo, hi = max(0, center - half), min(len(reference), center + half)
    if hi - lo < 40:
        raise AstridError("reference window is too short")
    # Sample every ~93 ms. Score every candidate against the same reference
    # frames. The ratio scales reference time to target time.
    reference_idx = np.arange(lo, hi, 4)
    reference_vectors = reference[reference_idx]
    candidates = np.arange(0, len(target), 4)
    best = (-np.inf, 0, 1.0)
    for ratio in np.arange(0.94, 1.0601, 0.005):
        offsets = np.rint((reference_idx - center) * ratio).astype(int)
        valid = (candidates[:, None] + offsets[None, :] >= 0) & (candidates[:, None] + offsets[None, :] < len(target))
        # Evaluate in batches to cap temporary memory.
        for start in range(0, len(candidates), 128):
            c = candidates[start:start + 128]
            indices = c[:, None] + offsets[None, :]
            mask = valid[start:start + 128]
            similarities = (target[np.clip(indices, 0, len(target) - 1)] * reference_vectors[None]).sum(axis=2)
            scores = np.where(mask, similarities, 0).sum(axis=1) / len(reference_idx)
            winner = int(np.argmax(scores))
            if scores[winner] > best[0]:
                best = (float(scores[winner]), int(c[winner]), float(ratio))
    # Resolve the coarse hop/ratio quantization around the winning passage.
    fine_idx = np.arange(lo, hi)
    fine_vectors = reference[fine_idx]
    coarse_center = best[1]
    for ratio in np.arange(max(0.94, best[2] - 0.03), min(1.06, best[2] + 0.03) + 0.0001, 0.001):
        offsets = np.rint((fine_idx - center) * ratio).astype(int)
        for candidate in range(max(0, coarse_center - 8), min(len(target), coarse_center + 9)):
            indices = candidate + offsets
            if indices.min() < 0 or indices.max() >= len(target):
                continue
            score_here = float(np.mean(np.sum(target[indices] * fine_vectors, axis=1)))
            if score_here > best[0]:
                best = (score_here, candidate, float(ratio))
    score, match_center, ratio = best
    rng = np.random.default_rng(0)
    shuffled = reference_vectors[rng.permutation(len(reference_vectors))]
    offsets = np.rint((reference_idx - center) * ratio).astype(int)
    indices = np.clip(match_center + offsets, 0, len(target) - 1)
    baseline = float(np.mean(np.sum(target[indices] * shuffled, axis=1)))
    return {"offset_s": round((match_center - center * ratio) * FRAME_S, 4),
            "tempo_ratio": round(ratio, 4), "score": round(score, 4),
            "baseline": round(baseline, 4), "confidence": round(max(0.0, score - baseline), 4),
            "matched_ref_s": round(ref_s, 4), "matched_target_s": round(match_center * FRAME_S, 4)}


def track_beats(onset: np.ndarray) -> dict:
    """Autocorrelation tempo, dynamic beat path, then four-beat accent phase."""
    smooth = np.convolve(onset, np.ones(5) / 5, mode="same")
    smooth = np.maximum(smooth - np.median(smooth), 0)
    if smooth.max() < 1e-5:
        raise AstridError("no beat accents found in audio")
    smooth /= smooth.max()
    lag_lo, lag_hi = round(60 / 180 / FRAME_S), round(60 / 60 / FRAME_S)
    # Only the candidate tempo lags are needed; avoid quadratic full correlation.
    # Favor the dominant period while suppressing half/double tempo aliases.
    lags = np.arange(lag_lo, lag_hi + 1)
    strength = np.array([float(smooth[k:] @ smooth[:-k]) / (len(smooth) - k) for k in lags])
    by_lag = dict(zip(lags, strength))
    lag = int(lags[np.argmax(strength)])
    # A pulse near 60 BPM can be the every-other-beat accent of a moderate
    # tempo. Prefer that moderate pulse when its evidence is nearly as strong.
    if 60 / (lag * FRAME_S) < 75:
        shorter = np.arange(max(lag_lo, lag // 2 - 1), min(lag_hi, lag // 2 + 1) + 1)
        short_strength = [by_lag[k] for k in shorter]
        if max(short_strength) >= 0.85 * float(strength.max()):
            lag = int(shorter[np.argmax(short_strength)])
    # Arrangements with prominent eighth notes often peak at double tempo.
    # Prefer the metrical quarter if its autocorrelation is almost as strong.
    if 60 / (lag * FRAME_S) > 140 and 2 * lag <= lag_hi:
        doubled = np.arange(max(lag_lo, 2 * lag - 1), min(lag_hi, 2 * lag + 1) + 1)
        half_strength = max(by_lag[k] for k in doubled)
        if half_strength >= 0.9 * float(strength.max()):
            lag = int(doubled[np.argmax([by_lag[k] for k in doubled])])
    # Dynamic programming lets each beat move up to 18% of the period.
    path = np.full(len(smooth), -np.inf, np.float32)
    back = np.full(len(smooth), -1, np.int32)
    radius = max(2, round(lag * 0.18))
    for i in range(len(smooth)):
        lower, upper = max(0, i - lag - radius), max(0, i - lag + radius + 1)
        if lower < upper:
            choices = path[lower:upper] - 0.35 * ((np.arange(lower, upper) - (i - lag)) / lag) ** 2
            parent = int(np.argmax(choices)) + lower
            previous = float(path[parent])
            if previous > 0:
                path[i] = previous + smooth[i]
                back[i] = parent
                continue
        path[i] = smooth[i]
    # Choose an endpoint near the end; path score alone would favor a noisy tail.
    end = int(np.argmax(path[max(0, len(path) - lag):]) + max(0, len(path) - lag))
    indices = []
    while end >= 0:
        indices.append(end)
        end = int(back[end])
    indices.reverse()
    if len(indices) < 4:
        raise AstridError("could not establish a beat grid")
    # Extend the stable period through quiet intros/outros.
    while indices[0] - lag >= 0:
        indices.insert(0, indices[0] - lag)
    while indices[-1] + lag < len(smooth):
        indices.append(indices[-1] + lag)
    accents = np.array([smooth[max(0, i - 3):min(len(smooth), i + 4)].max() for i in indices])
    phase = max(range(4), key=lambda p: float(accents[p::4].mean()))
    beats = [round(i * FRAME_S, 4) for i in indices]
    downbeats = beats[phase::4]
    return {"schema_version": 1, "source": "music.beats", "bpm": round(60 / (lag * FRAME_S), 3),
            "beats": beats, "downbeats": downbeats, "bars": downbeats[::4], "sections": []}
