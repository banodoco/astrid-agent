"""Duck action integration fixtures and behaviour contracts.

The action uses ffmpeg, so tests in this module belong to the integration
lane. Signal construction stays local and deterministic; no Runtime is faked.
"""

from __future__ import annotations

import wave
from pathlib import Path

import numpy as np
import pytest

from astrid.core.contracts.errors import AstridError
from astrid.core.foundation.hash import sha256_file
from astrid.core.pack.entrypoint import canonical_runtime_entrypoint
from astrid.packs.music.actions.duck._audio import (
    integrated,
    load_audio,
    read_duck_tag,
    weighted_power,
)
from astrid.packs.music.actions.duck.core import (
    Placement,
    duck_audio,
    placed_samples,
    select_music_clip,
)

with canonical_runtime_entrypoint("music.level"):
    from astrid.packs.music.actions.level.run import measure as measure_level

pytestmark = pytest.mark.integration


_SAMPLE_RATE = 48_000


def _write_pcm16(path: Path, samples: np.ndarray, *, sample_rate: int = _SAMPLE_RATE) -> Path:
    """Write clipped float samples as a deterministic mono PCM16 WAV."""
    pcm = np.rint(np.clip(samples, -1.0, 1.0) * 32767).astype("<i2")
    with wave.open(str(path), "wb") as output:
        output.setnchannels(1)
        output.setsampwidth(2)
        output.setframerate(sample_rate)
        output.writeframes(pcm.tobytes())
    return path


def _write_pcm24_stereo(path: Path, samples: np.ndarray, *, sample_rate: int = _SAMPLE_RATE) -> Path:
    """Write a dual-mono 24-bit PCM fixture without an encoder dependency."""
    stereo = np.repeat(np.asarray(samples)[:, None], 2, axis=1)
    pcm = np.rint(np.clip(stereo, -1.0, 1.0) * 8_388_607).astype(np.int32)
    packed = ((pcm[..., None] >> np.array([0, 8, 16])) & 255).astype(np.uint8)
    with wave.open(str(path), "wb") as output:
        output.setnchannels(2)
        output.setsampwidth(3)
        output.setframerate(sample_rate)
        output.writeframes(packed.tobytes())
    return path


def _bed(*, duration_s: float = 8.0, jump_db: float | None = None) -> np.ndarray:
    """Create a quiet, steady tonal bed, optionally with one abrupt level jump."""
    count = round(duration_s * _SAMPLE_RATE)
    t = np.arange(count, dtype=np.float64) / _SAMPLE_RATE
    level = np.full(count, 0.055, dtype=np.float64)
    if jump_db is not None:
        level[t >= duration_s / 2] *= 10 ** (jump_db / 20)
    # Two incommensurate partials give the detector a music-like, non-silent bed.
    return (level * (0.72 * np.sin(2 * np.pi * 220 * t) + 0.28 * np.sin(2 * np.pi * 331 * t))).astype(np.float32)


def _voice_bursts(
    duration_s: float,
    *,
    starts_s: tuple[float, ...] | None = None,
    burst_s: float = 0.24,
) -> np.ndarray:
    """Create a standalone voice-like track with repeatable bursts and gaps."""
    mixed = np.zeros(round(duration_s * _SAMPLE_RATE), dtype=np.float64)
    count = round(burst_s * _SAMPLE_RATE)
    t = np.arange(count, dtype=np.float64) / _SAMPLE_RATE
    # A smooth syllable envelope avoids clicks while retaining distinct phrases.
    envelope = np.sin(np.linspace(0, np.pi, count, endpoint=True)) ** 1.4
    syllable = 0.34 * envelope * (
        0.65 * np.sin(2 * np.pi * 185 * t)
        + 0.25 * np.sin(2 * np.pi * 370 * t)
        + 0.10 * np.sin(2 * np.pi * 555 * t)
    )
    if starts_s is None:
        # Repeated syllables sustain a long speech line while leaving clear gaps.
        starts_s = tuple(np.arange(1.0, duration_s - burst_s, 0.58).tolist())
    for start_s in starts_s:
        begin = round(start_s * _SAMPLE_RATE)
        end = min(begin + count, len(mixed))
        mixed[begin:end] += syllable[: end - begin]
    return mixed.astype(np.float32)


@pytest.fixture
def duck_fixture_paths(tmp_path: Path) -> dict[str, Path]:
    """Prepare a jump-bed source, long voice line, and steady-bed controls."""
    duration_s = 40.0
    jump_bed = _bed(duration_s=duration_s, jump_db=16.0)
    steady_bed = _bed(duration_s=duration_s)
    voice = _voice_bursts(duration_s)
    return {
        "jump_bed": _write_pcm16(tmp_path / "jump-bed.wav", jump_bed),
        "voice": _write_pcm16(tmp_path / "voice.wav", voice),
        "steady_bed": _write_pcm16(tmp_path / "steady-bed.wav", steady_bed),
        "steady_voice": _write_pcm16(tmp_path / "steady-voice.wav", voice),
        "stereo24_bed": _write_pcm24_stereo(tmp_path / "stereo24-bed.wav", steady_bed),
    }


def _mono(path: Path) -> np.ndarray:
    return load_audio(path).samples[:, 0]


def _integrated_source_span(path: Path, start_s: float, end_s: float) -> float:
    """Measure a source-file span independently with the action's EBU helper."""
    audio = load_audio(path)
    lo = round(start_s * audio.rate)
    hi = round(end_s * audio.rate)
    assert hi > lo
    return integrated(weighted_power(audio.samples[lo:hi], audio.rate), audio.rate)


def _integrated_placed_span(path: Path, placement: Placement) -> float:
    """Decode output, map it to timeline clock, then measure the used clip span."""
    audio = load_audio(path)
    samples = placed_samples(audio, placement, audio.rate, placement.start, placement.duration)
    return integrated(weighted_power(samples, audio.rate), audio.rate)


def test_constant_bed_keeps_line_gap_within_half_lu(duck_fixture_paths: dict[str, Path], tmp_path: Path) -> None:
    paths = duck_fixture_paths
    source = load_audio(paths["steady_bed"])
    own_measurement = integrated(weighted_power(source.samples, source.rate), source.rate)
    assert own_measurement is not None
    assert own_measurement == pytest.approx(measure_level(paths["steady_bed"], 0, 40), abs=0.15)

    result = duck_audio(
        Placement(paths["steady_bed"], start=0, duration=40, id="music"),
        [Placement(paths["steady_voice"], start=0, duration=19, id="line-1"),
         Placement(paths["steady_voice"], start=20, duration=19, from_s=20, id="line-2")],
        tmp_path / "steady-ducked.wav",
    )
    assert len(result.report["lines"]) == 2
    for line in result.report["lines"]:
        assert line["gap_after_lu"] == pytest.approx(8, abs=0.5)
    assert result.report["floor_held_lines"] == []
    rendered = load_audio(tmp_path / "steady-ducked.wav")
    assert (rendered.rate, rendered.bits, rendered.samples.shape[1]) == (
        source.rate, source.bits, source.samples.shape[1],
    )
    assert len(rendered.samples) == len(source.samples)
    # The report is not the evidence of ducking: measure each emitted music span
    # against the corresponding independent voice span from the input files.
    output_path = tmp_path / "steady-ducked.wav"
    for music_start, music_end, voice_start, voice_end in (
        (0, 19, 0, 19), (20, 39, 20, 39),
    ):
        voice_i = _integrated_source_span(paths["steady_voice"], voice_start, voice_end)
        output_i = _integrated_source_span(output_path, music_start, music_end)
        assert voice_i - output_i == pytest.approx(8, abs=0.5)


def test_speech_keyed_gain_handles_bed_jump_and_nonunit_source_mapping(
    duck_fixture_paths: dict[str, Path], tmp_path: Path,
) -> None:
    paths = duck_fixture_paths
    output = tmp_path / "jump-ducked.wav"
    music = Placement(paths["jump_bed"], start=10, duration=12, from_s=10, speed=1.5, id="music")
    voice = Placement(paths["voice"], start=10, duration=12, from_s=0, speed=1.5, id="line")
    result = duck_audio(music, [voice], output)

    assert result.report["max_slope_db_per_s"] <= 2 + 1e-9
    assert np.max(np.abs(np.diff(result.gain_db) / np.diff(result.times))) * music.speed <= 2 + 1e-9
    # Voice's first source syllable is at 1s. It plays at timeline 10+1/1.5,
    # mapping to music source 10+(1/1.5)*1.5=11s (20ms energy-gate resolution).
    assert result.times[np.flatnonzero(result.target_db < 0)[0]] == pytest.approx(11, abs=.05)
    assert result.times[0] == pytest.approx(0)
    assert result.times[-1] >= 40
    # Even at the deepest floor, the ramp cannot reach further than
    # (12/2 + 1/2)*1.5 = 9.75 source seconds from speech.
    # The first second and last second are beyond that reach.
    original = _mono(paths["jump_bed"])
    rendered = _mono(output)
    assert np.array_equal(rendered[:_SAMPLE_RATE], original[:_SAMPLE_RATE])
    assert np.array_equal(rendered[39 * _SAMPLE_RATE:], original[39 * _SAMPLE_RATE:])
    assert result.gain_db[0] == pytest.approx(0, abs=1e-9)
    assert result.gain_db[-1] == pytest.approx(0, abs=1e-9)

    # Decode and place the emitted music WAV and input voice on the same
    # timeline clock, including the clip's source trim and 1.5x speed mapping.
    voice_i = _integrated_placed_span(paths["voice"], voice)
    bed_i = _integrated_placed_span(paths["jump_bed"], music)
    output_i = _integrated_placed_span(output, music)
    measured_gap = voice_i - output_i
    assert bed_i - output_i >= 3
    assert measured_gap == pytest.approx(result.report["lines"][0]["gap_after_lu"], abs=0.1)

    # A tagged rerun resolves the immutable original and reproduces every byte.
    tag = read_duck_tag(output)
    assert tag is not None
    assert tag["source_digest"] == "sha256:" + sha256_file(paths["jump_bed"])
    repeated = tmp_path / "jump-ducked-again.wav"
    duck_audio(
        Placement(output, start=10, duration=12, from_s=10, speed=1.5, id="music"),
        [voice],
        repeated,
        resolve_source=lambda digest: paths["jump_bed"] if digest == tag["source_digest"] else tmp_path / "missing.wav",
    )
    assert repeated.read_bytes() == output.read_bytes()
    with pytest.raises(AstridError):
        duck_audio(Placement(output, start=10, duration=12, from_s=10, speed=1.5), [voice],
                   tmp_path / "missing-source.wav")


@pytest.mark.parametrize("app", [{"duck": {}}, {"astrid": {"duck": {}}},
                                  {"duck": {"bed_lu": 8}}, {"astrid": {"duck": {"bed_lu": 8}}}])
def test_music_clip_with_duck_intent_is_refused(app: dict) -> None:
    with pytest.raises(AstridError):
        select_music_clip([{"id": "music-1", "track": "music", "app": app}])


def test_ambiguous_music_clip_requires_explicit_selection() -> None:
    clips = [
        {"id": "music-1", "track": "music", "app": {}},
        {"id": "music-2", "track": "music", "app": {}},
    ]
    with pytest.raises(AstridError):
        select_music_clip(clips)
    assert select_music_clip(clips, "music-2") is clips[1]


def test_ducked_asset_preserves_stereo_24_bit_source_format(
    duck_fixture_paths: dict[str, Path], tmp_path: Path,
) -> None:
    paths = duck_fixture_paths
    result_path = tmp_path / "stereo24-ducked.wav"
    duck_audio(
        Placement(paths["stereo24_bed"], start=0, duration=40, id="music"),
        [Placement(paths["steady_voice"], start=0, duration=40, id="line")],
        result_path,
    )
    rendered = load_audio(result_path)
    assert (rendered.rate, rendered.bits, rendered.samples.shape[1]) == (_SAMPLE_RATE, 24, 2)

