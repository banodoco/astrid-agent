"""Real-media boundary contract for H3 source timing and audio repair."""
from __future__ import annotations

import hashlib
import json
import subprocess
import tempfile
from fractions import Fraction
from pathlib import Path

import pytest

from astrid.core.generation.engine_facts import comfyui_facts
from astrid.packs.h3_av.shared import engine_facts
from astrid.packs.h3_av.shared.errors import PreparationError
from astrid.packs.h3_av.shared.prepare import prepare_request
from astrid.packs.h3_av.shared.request import normalize_request


FPS = 24
SAMPLE_RATE = 48_000
SAMPLES_PER_FRAME = SAMPLE_RATE // FPS


def _ffmpeg(*args: str) -> None:
    subprocess.run(
        ["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", *args],
        check=True,
        capture_output=True,
        text=True,
        timeout=60,
    )


def _source(
    path: Path,
    *,
    duration: Fraction = Fraction(4),
    audio_offset_samples: int = 0,
    audio_samples: int | None = None,
) -> Path:
    """Create MOV/PCM media with exact ffprobe stream starts and durations."""
    audio_offset = Fraction(audio_offset_samples, SAMPLE_RATE)
    video_offset_samples = max(0, -audio_offset_samples)
    video_offset = Fraction(video_offset_samples, SAMPLE_RATE)
    audio_mux_offset = max(Fraction(0), audio_offset)
    video_seconds = float(duration)
    if audio_samples is None:
        audio_samples = round(duration * SAMPLE_RATE)
    audio_duration = Fraction(audio_samples, SAMPLE_RATE)
    # Place a 880 Hz marker at a fixed video-relative time even when the audio
    # stream starts early or late. Everything else is a quieter 440 Hz tone.
    pulse_start = 1.5 - audio_offset
    pulse_end = pulse_start + Fraction(1, 4)
    signal = (
        "aevalsrc=0.08*sin(2*PI*440*t)+0.5*sin(2*PI*880*t)*"
        f"between(t\\,{float(pulse_start):.12f}\\,{float(pulse_end):.12f}):"
        f"s=48000:d={float(audio_duration):.12f}"
    )
    command = []
    command += ["-f", "lavfi", "-i", f"color=c=black:s=320x180:r=24:d={video_seconds:.12f}"]
    if audio_mux_offset:
        command += ["-itsoffset", f"{float(audio_mux_offset):.12f}"]
    command += ["-f", "lavfi", "-i", signal,
                "-map", "0:v:0", "-map", "1:a:0"]
    if video_offset_samples:
        command += ["-vf", f"settb=expr=1/48000,setpts=PTS+{video_offset_samples}"]
    command += [
                "-c:v", "libx264", "-crf", "0", "-pix_fmt", "yuv420p",
                "-fps_mode:v", "passthrough", "-enc_time_base:v", "1/48000",
                "-c:a", "pcm_s16le", "-ar", "48000",
                "-movie_timescale", "48000", "-video_track_timescale", "48000",
                "-t", f"{float(max(video_offset + duration, audio_mux_offset + audio_duration) + Fraction(1, 10)):.12f}",
                str(path)]
    _ffmpeg(*command)
    return path


def _source_with_internal_aac_gap(path: Path) -> Path:
    """Write a transport stream whose AAC PTS jumps inside a covered interval."""
    with tempfile.TemporaryDirectory(prefix="h3-internal-audio-gap-", dir=path.parent) as raw:
        scratch = Path(raw)
        first = scratch / "first.m4a"
        second = scratch / "second.m4a"
        _ffmpeg("-f", "lavfi", "-i", "sine=frequency=440:sample_rate=48000:duration=1.5",
                "-c:a", "aac", "-ar", "48000", str(first))
        _ffmpeg("-f", "lavfi", "-i", "sine=frequency=440:sample_rate=48000:duration=2.5",
                "-c:a", "aac", "-ar", "48000", str(second))
        concat = scratch / "segments.ffconcat"
        concat.write_text(
            "ffconcat version 1.0\n"
            f"file '{first}'\n"
            "duration 1.6\n"
            f"file '{second}'\n",
            encoding="utf-8",
        )
        _ffmpeg(
            "-copyts", "-f", "lavfi", "-i", "color=c=black:s=320x180:r=24:d=4",
            "-f", "concat", "-safe", "0", "-i", str(concat),
            "-map", "0:v:0", "-map", "1:a:0",
            "-c:v", "libx264", "-pix_fmt", "yuv420p", "-fps_mode:v", "passthrough",
            "-c:a", "copy", "-f", "mpegts", "-t", "4.2", str(path),
        )
    return path


def _request(start: float, end: float) -> dict:
    return {
        "version": 1,
        "operation": "edit",
        "source": {"asset": "source", "range": [start, end]},
        "output": {"duration": end - start},
        "content": {"prompt": "Preserve the source interval."},
        "changes": {"video": [], "audio": []},
        "references": [],
        "overrides": {"seed": 7, "steps": 8},
    }


def _prepare(tmp_path: Path, source: Path, start: float, end: float) -> dict:
    engine_facts.install(comfyui_facts())
    public = _request(start, end)
    request = normalize_request(public)
    result = prepare_request(
        request, asset_map={"source": str(source)}, out_dir=tmp_path / "prepared",
    )
    result["test_request"] = request
    return result


def _probe(path: Path) -> dict:
    result = subprocess.run(
        ["ffprobe", "-v", "error", "-show_entries",
         "stream=codec_type,start_time,start_pts,duration,duration_ts,time_base,nb_frames:format=start_time,duration",
         "-of", "json", str(path)],
        check=True, capture_output=True, text=True, timeout=30,
    )
    return json.loads(result.stdout)


def _audio_packet_ticks(path: Path) -> list[tuple[int, int]]:
    result = subprocess.run(
        ["ffprobe", "-v", "error", "-select_streams", "a:0", "-show_packets",
         "-show_entries", "packet=pts,duration", "-of", "json", str(path)],
        check=True, capture_output=True, text=True, timeout=30,
    )
    return [(int(row["pts"]), int(row["duration"])) for row in json.loads(result.stdout)["packets"]]


def _tone_peak(path: Path, start: float) -> float:
    result = subprocess.run(
        ["ffmpeg", "-hide_banner", "-loglevel", "error", "-i", str(path), "-map", "0:a:0",
         "-ac", "1", "-ar", str(SAMPLE_RATE), "-f", "f32le", "-"],
        check=True, capture_output=True, timeout=30,
    )
    import numpy as np

    samples = np.frombuffer(result.stdout, dtype=np.float32)
    window = samples[round(start * SAMPLE_RATE):round(start * SAMPLE_RATE) + 4800]
    assert len(window) == 4800
    spectrum = np.abs(np.fft.rfft(window * np.hanning(len(window))))
    frequencies = np.fft.rfftfreq(len(window), d=1 / SAMPLE_RATE)
    band = (frequencies >= 300) & (frequencies <= 1200)
    return float(frequencies[band][np.argmax(spectrum[band])])


def _decoded_audio_samples(path: Path, target_samples: int) -> int:
    duration = target_samples / SAMPLE_RATE
    result = subprocess.run(
        ["ffmpeg", "-hide_banner", "-loglevel", "error", "-i", str(path),
         "-t", f"{duration:.12g}", "-map", "0:a:0",
         "-ac", "1", "-ar", str(SAMPLE_RATE), "-f", "s16le", "-"],
        check=True, capture_output=True, timeout=30,
    )
    assert len(result.stdout) % 2 == 0
    return len(result.stdout) // 2


@pytest.mark.ffmpeg
def test_rounded_range_uses_rounded_duration_and_retains_public_request(tmp_path: Path) -> None:
    source = _source(tmp_path / "source.mov")
    public = _request(0.5, 2.502)  # 48.048 frames: non-tie rounds to 48.
    request = normalize_request(public)
    original = json.loads(json.dumps(request.value))
    engine_facts.install(comfyui_facts())
    preparation = prepare_request(request, asset_map={"source": str(source)}, out_dir=tmp_path / "prepared")
    record = preparation["source_conform"]
    expected_frames = round((2.502 - 0.5) * FPS)

    assert preparation["original_request"] == original
    assert preparation["original_request_digest"] == request.digest
    assert record["source"]["range_seconds"] == ["0.5", "2.502"]
    assert record["output"]["frames"] == expected_frames == 48
    assert record["output"]["range_seconds"] == [0, expected_frames / FPS]
    output = tmp_path / "prepared" / "conformed-source" / record["output"]["file"]
    probed_video = next(row for row in _probe(output)["streams"] if row["codec_type"] == "video")
    assert int(probed_video["nb_frames"]) == expected_frames
    timing = record["timing"]
    assert timing["requested_duration_seconds"] == pytest.approx(2.002)
    assert timing["prepared_duration_seconds"] == pytest.approx(2.0)
    assert timing["rounding_delta_seconds"] == pytest.approx(-0.002)


@pytest.mark.ffmpeg
@pytest.mark.parametrize("offset", [SAMPLES_PER_FRAME, -SAMPLES_PER_FRAME])
def test_audio_start_offset_of_one_frame_preserves_the_source_pulse(
    tmp_path: Path, offset: int,
) -> None:
    source = _source(tmp_path / "source-offset.mov", audio_offset_samples=offset)
    probe = _probe(source)
    starts = {
        row["codec_type"]: Fraction(int(row["start_pts"])) * Fraction(row["time_base"])
        for row in probe["streams"]
    }
    assert abs(starts["audio"] - starts["video"]) == Fraction(1, FPS)

    preparation = _prepare(tmp_path, source, 0.5, 2.5)
    record = preparation["source_conform"]
    output = tmp_path / "prepared" / "conformed-source" / record["output"]["file"]
    timing = record["timing"]
    assert timing["audio_offset_samples"] == offset
    assert _tone_peak(output, 1.08) == pytest.approx(880, abs=15)
    assert _tone_peak(output, 0.55) == pytest.approx(440, abs=15)
    assert _decoded_audio_samples(output, record["timing"]["target_audio_samples"]) == record["timing"]["target_audio_samples"]


@pytest.mark.ffmpeg
@pytest.mark.parametrize("offset", [SAMPLES_PER_FRAME + 1, -(SAMPLES_PER_FRAME + 1)])
def test_audio_start_offset_over_one_frame_is_rejected(tmp_path: Path, offset: int) -> None:
    source = _source(tmp_path / "source-offset-too-far.mov", audio_offset_samples=offset)
    with pytest.raises(PreparationError, match="audio start offset exceeds one 24 fps frame"):
        _prepare(tmp_path, source, 0.5, 2.5)


@pytest.mark.ffmpeg
def test_selected_audio_head_shortfall_is_padded_with_exact_silence(tmp_path: Path) -> None:
    source = _source(tmp_path / "source-head-short.mov", audio_offset_samples=SAMPLES_PER_FRAME)
    probe = _probe(source)
    starts = {
        row["codec_type"]: Fraction(int(row["start_pts"])) * Fraction(row["time_base"])
        for row in probe["streams"]
    }
    assert starts["audio"] - starts["video"] == Fraction(1, FPS)

    preparation = _prepare(tmp_path, source, 0.0, 2.0)
    record = preparation["source_conform"]
    output = tmp_path / "prepared" / "conformed-source" / record["output"]["file"]
    timing = record["timing"]
    assert timing["head_pad_samples"] == SAMPLES_PER_FRAME
    assert timing["target_audio_samples"] == record["output"]["frames"] * SAMPLES_PER_FRAME
    assert _decoded_audio_samples(output, timing["target_audio_samples"]) == timing["target_audio_samples"]
    assert _tone_peak(output, 1.58) == pytest.approx(880, abs=15)


@pytest.mark.ffmpeg
@pytest.mark.parametrize("shortfall", [SAMPLES_PER_FRAME, SAMPLES_PER_FRAME + 1])
def test_selected_audio_tail_shortfall_is_padded_only_through_one_frame(
    tmp_path: Path, shortfall: int,
) -> None:
    # [0, 2.5) is selected; the source audio ends exactly `shortfall` samples
    # before its video timeline endpoint.
    audio_samples = round(Fraction(5, 2) * SAMPLE_RATE) - shortfall
    source = _source(
        tmp_path / f"source-tail-short-{shortfall}.mov",
        duration=Fraction(4), audio_samples=audio_samples,
    )
    if shortfall > SAMPLES_PER_FRAME:
        with pytest.raises(PreparationError, match="audio shortfall exceeds one 24 fps frame"):
            _prepare(tmp_path, source, 0.0, 2.5)
        return

    preparation = _prepare(tmp_path, source, 0.0, 2.5)
    record = preparation["source_conform"]
    output = tmp_path / "prepared" / "conformed-source" / record["output"]["file"]
    timing = record["timing"]
    assert timing["tail_pad_samples"] == shortfall
    assert timing["target_audio_samples"] == record["output"]["frames"] * SAMPLES_PER_FRAME
    assert _decoded_audio_samples(output, timing["target_audio_samples"]) == timing["target_audio_samples"]
    assert _tone_peak(output, 1.58) == pytest.approx(880, abs=15)


@pytest.mark.ffmpeg
def test_audio_surplus_is_cut_at_selected_end_without_moving_pulse(tmp_path: Path) -> None:
    source = _source(
        tmp_path / "source-audio-surplus.mov",
        audio_samples=round(Fraction(9, 2) * SAMPLE_RATE),
    )
    preparation = _prepare(tmp_path, source, 0.5, 2.5)
    record = preparation["source_conform"]
    output = tmp_path / "prepared" / "conformed-source" / record["output"]["file"]

    assert record["timing"]["target_audio_samples"] == record["output"]["frames"] * SAMPLES_PER_FRAME
    assert record["timing"]["trimmed_tail_samples"] >= round(Fraction(2, 1) * SAMPLE_RATE)
    assert _decoded_audio_samples(output, record["timing"]["target_audio_samples"]) == record["timing"]["target_audio_samples"]
    assert _tone_peak(output, 1.08) == pytest.approx(880, abs=15)


@pytest.mark.ffmpeg
def test_internal_audio_timestamp_gap_over_one_frame_is_rejected_despite_full_stream_coverage(
    tmp_path: Path,
) -> None:
    source = _source_with_internal_aac_gap(tmp_path / "source-internal-gap.ts")
    probe = _probe(source)
    video = next(row for row in probe["streams"] if row["codec_type"] == "video")
    audio = next(row for row in probe["streams"] if row["codec_type"] == "audio")
    video_start = Fraction(int(video["start_pts"])) * Fraction(video["time_base"])
    video_duration = Fraction(int(video["duration_ts"])) * Fraction(video["time_base"])
    audio_start = Fraction(int(audio["start_pts"])) * Fraction(audio["time_base"])
    audio_duration = Fraction(int(audio["duration_ts"])) * Fraction(audio["time_base"])
    selected_start, selected_end = video_start + Fraction(1, 2), video_start + Fraction(5, 2)
    assert selected_start >= audio_start
    assert selected_end <= audio_start + audio_duration
    assert selected_end <= video_start + video_duration

    ticks = _audio_packet_ticks(source)
    time_base = Fraction(audio["time_base"])
    gaps = [
        (right_pts - (left_pts + left_duration)) * time_base
        for (left_pts, left_duration), (right_pts, _right_duration) in zip(ticks, ticks[1:])
    ]
    assert max(gaps) > Fraction(1, FPS)

    with pytest.raises(PreparationError, match="decoded source audio has a timestamp gap or overlap"):
        _prepare(tmp_path, source, 0.5, 2.5)


@pytest.mark.ffmpeg
@pytest.mark.parametrize("video_duration,audio_duration,expected_error", [
    (1, 1, None),
    (1, 0.5, "audio shortfall exceeds"),
    (0.5, 1, "source streams must cover"),
])
def test_matroska_missing_stream_durations_measures_each_stream(
    tmp_path: Path, video_duration: float, audio_duration: float, expected_error: str | None,
) -> None:
    source = tmp_path / "source.mkv"
    _ffmpeg(
        "-f", "lavfi", "-i", f"color=c=black:s=320x180:r=24:d={video_duration}",
        "-f", "lavfi", "-i", f"sine=frequency=440:sample_rate=48000:duration={audio_duration}",
        "-map", "0:v:0", "-map", "1:a:0", "-c:v", "libx264", "-pix_fmt", "yuv420p",
        "-c:a", "pcm_s16le", str(source),
    )
    info = _probe(source)
    assert all("duration" not in row and "duration_ts" not in row for row in info["streams"])
    assert float(info["format"]["duration"]) == pytest.approx(1)
    if expected_error:
        with pytest.raises(PreparationError, match=expected_error):
            _prepare(tmp_path, source, 0, 1)
    else:
        result = _prepare(tmp_path, source, 0, 1)["source_conform"]
        assert result["output"]["frames"] == 24
        # Matroska's 1 ms timestamp clock rounds the last PCM frame start;
        # the reported repair accounts for the resulting 16-sample shortfall.
        assert result["timing"]["tail_pad_samples"] == 16
        assert result["timing"]["audio_offset_samples"] == 0


@pytest.mark.ffmpeg
def test_matroska_video_gap_is_rejected(tmp_path: Path) -> None:
    source = tmp_path / "gap.mkv"
    _ffmpeg(
        "-f", "lavfi", "-i", "color=c=black:s=320x180:r=24:d=1",
        "-f", "lavfi", "-i", "sine=frequency=440:sample_rate=48000:duration=1",
        "-vf", "select='not(between(t,0.4,0.8))'", "-fps_mode:v", "passthrough",
        "-c:v", "libx264", "-pix_fmt", "yuv420p", "-c:a", "pcm_s16le", str(source),
    )
    with pytest.raises(PreparationError, match="video has a timestamp gap"):
        _prepare(tmp_path, source, 0, 1)


@pytest.mark.ffmpeg
def test_coverage_probe_fails_closed_when_output_limit_exceeded(tmp_path: Path, monkeypatch) -> None:
    from astrid.packs.h3_av.shared import conform

    source = _source(tmp_path / "source.mov", duration=Fraction(1))
    monkeypatch.setattr(conform, "_MAX_PROBE_BYTES", 256)
    with pytest.raises(PreparationError, match="cannot verify decoded source audio coverage"):
        _prepare(tmp_path, source, 0, 1)
    assert not (tmp_path / "prepared" / "conformed-source").exists()


@pytest.mark.ffmpeg
def test_source_change_during_coverage_inspection_is_rejected(tmp_path: Path, monkeypatch) -> None:
    from astrid.packs.h3_av.shared import conform

    source = _source(tmp_path / "source.mov", duration=Fraction(1))
    original = conform._decoded_audio_coverage

    def mutate_after_probe(*args):
        result = original(*args)
        with source.open("ab") as changed:
            changed.write(b"changed")
        return result

    monkeypatch.setattr(conform, "_decoded_audio_coverage", mutate_after_probe)
    with pytest.raises(PreparationError, match="source changed while inspecting coverage"):
        _prepare(tmp_path, source, 0, 1)
    assert not (tmp_path / "prepared" / "conformed-source").exists()


def test_video_duplicate_packet_timestamps_are_rejected(monkeypatch, tmp_path: Path) -> None:
    from astrid.packs.h3_av.shared import conform

    monkeypatch.setattr(conform, "_bounded_probe", lambda args: {
        "packets": [{"pts": 0, "duration": 1000}, {"pts": 0, "duration": 1000}],
    })
    with pytest.raises(PreparationError, match="video has overlapping timestamps"):
        conform._video_packet_end(tmp_path / "source.mkv", {"time_base": "1/1000"}, Fraction(0), Fraction(1))


def test_bounded_probe_discards_stderr_and_propagates_failure(monkeypatch, tmp_path: Path) -> None:
    from astrid.packs.h3_av.shared import conform

    def failed_probe(*args, **kwargs):
        assert kwargs["stderr"] is subprocess.DEVNULL
        raise subprocess.CalledProcessError(1, args[0])

    monkeypatch.setattr(conform.subprocess, "run", failed_probe)
    with pytest.raises(PreparationError, match="cannot verify source video coverage"):
        conform._video_packet_end(tmp_path / "source.mkv", {"time_base": "1/1000"}, Fraction(0), Fraction(1))
