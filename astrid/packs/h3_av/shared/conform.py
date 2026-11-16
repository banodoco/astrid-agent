"""Explicit, bounded audiovisual source preparation for native H3."""
from __future__ import annotations

import hashlib
import json
import subprocess
import tempfile
from fractions import Fraction
from pathlib import Path

from .errors import PreparationError
from .prepare import _check_source_av_rate


def _digest(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open('rb') as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b''):
            digest.update(chunk)
    return digest.hexdigest()


def _probe(path: Path) -> dict:
    try:
        proc = subprocess.run(['ffprobe', '-v', 'error', '-show_entries',
            'stream=codec_type,start_time,start_pts,duration,duration_ts,time_base,sample_rate,nb_frames,sample_aspect_ratio:format=start_time,duration',
            '-of', 'json', str(path)], capture_output=True, text=True, check=True, timeout=30)
        return json.loads(proc.stdout)
    except (OSError, subprocess.SubprocessError, ValueError) as exc:
        raise PreparationError(f'cannot inspect source audiovisual streams: {exc}') from exc


def _stream_time(stream: dict, ticks_field: str, seconds_field: str) -> Fraction:
    """Prefer exact stream ticks; decimal ffprobe timestamps may be rounded."""
    ticks = stream.get(ticks_field)
    time_base = stream.get("time_base")
    if ticks is not None and time_base is not None:
        return Fraction(int(ticks)) * Fraction(str(time_base))
    return Fraction(str(stream[seconds_field]))


def _decoded_audio_coverage(path: Path, stream: dict, start: Fraction, end: Fraction) -> tuple[Fraction, Fraction]:
    """Measure decoded frame support and reject discontinuities in the selected span."""
    try:
        proc = subprocess.run(
            ['ffprobe', '-v', 'error', '-select_streams', 'a:0', '-show_frames',
             '-show_entries', 'frame=best_effort_timestamp,nb_samples', '-of', 'json', str(path)],
            capture_output=True, text=True, check=True, timeout=120,
        )
        frames = json.loads(proc.stdout)['frames']
        base = Fraction(str(stream['time_base']))
        rate = int(stream['sample_rate'])
        if base <= 0 or rate <= 0:
            raise ValueError('invalid audio clock')
        tolerance = max(base, Fraction(1, rate))
        coverage_start = coverage_end = None
        for frame in frames:
            frame_start = int(frame['best_effort_timestamp']) * base
            samples = int(frame['nb_samples'])
            if samples <= 0:
                raise ValueError('invalid decoded audio frame length')
            frame_end = frame_start + Fraction(samples, rate)
            if frame_end <= start or frame_start >= end:
                continue
            if coverage_end is not None and abs(frame_start - coverage_end) > tolerance:
                raise PreparationError('decoded source audio has a timestamp gap or overlap in the requested range')
            if coverage_start is None:
                coverage_start = frame_start
            coverage_end = frame_end
        if coverage_start is None or coverage_end is None:
            raise ValueError('no decoded audio frames in the requested range')
        return coverage_start, coverage_end
    except (OSError, subprocess.SubprocessError, KeyError, TypeError, ValueError, ZeroDivisionError) as exc:
        raise PreparationError(f'cannot verify decoded source audio coverage: {exc}') from exc


def conform_source(source: Path, destination: Path, *, start: str, end: str, fit: str, validate) -> dict:
    """Extract a half-open source interval, with explicit centered fit, to a new directory.

    Round the selected duration to the 24 fps grid without changing playback speed.
    Reject significant audio offsets or shortfalls; record any bounded repair.
    Sources without an audio stream remain video-only.
    """
    try:
        first, last = Fraction(start), Fraction(end)
    except (ValueError, ZeroDivisionError) as exc:
        raise PreparationError('start and end must be finite decimal seconds') from exc
    frame_value = (last - first) * 24
    frames = round(frame_value)
    if first < 0 or last <= first or frames <= 0:
        raise PreparationError('range must be positive and round to at least one 24 fps frame')
    if fit not in {'fill', 'fit'}:
        raise PreparationError('fit must be fill or fit')
    source = source.expanduser().resolve()
    destination = destination.expanduser().resolve()
    if not source.is_file():
        raise PreparationError('source must be an existing file')
    if destination.exists():
        raise PreparationError('output directory already exists')
    info = _probe(source)
    streams = info.get('streams', [])
    selected = [next((s for s in streams if s.get('codec_type') == kind), None) for kind in ('video', 'audio')]
    if selected[0] is None:
        raise PreparationError('source must contain video')
    has_audio = selected[1] is not None
    aspect = selected[0].get('sample_aspect_ratio')
    square_default = aspect in (None, 'N/A', '0:1')
    if not square_default:
        try:
            sample_aspect = Fraction(str(aspect).replace(':', '/'))
            if sample_aspect <= 0:
                raise ValueError('non-positive pixel aspect ratio')
        except (ValueError, ZeroDivisionError) as exc:
            raise PreparationError('source pixel aspect ratio is invalid') from exc
    try:
        video_start = _stream_time(selected[0], 'start_pts', 'start_time')
        video_duration = _stream_time(selected[0], 'duration_ts', 'duration')
        audio_start = _stream_time(selected[1], 'start_pts', 'start_time') if has_audio else None
        audio_duration = _stream_time(selected[1], 'duration_ts', 'duration') if has_audio else None
    except (KeyError, ValueError, TypeError, ZeroDivisionError) as exc:
        raise PreparationError('source stream start times and durations must be measurable') from exc
    requested = f"[{float(first):.6f}, {float(last):.6f})"
    available = f"video [0.000000, {float(video_duration):.6f})"
    if has_audio:
        audio_relative_start = audio_start - video_start
        available += (f", audio [{float(audio_relative_start):.6f}, "
                      f"{float(audio_relative_start + audio_duration):.6f})")
    if last > video_duration:
        raise PreparationError(f'source streams must cover the requested range {requested}; available {available}')
    target_duration = Fraction(frames, 24)
    target_samples = frames * 2000
    anchor = video_start + first
    requested_end = video_start + last
    target_end = anchor + target_duration
    audio_offset_samples = head_pad = tail_pad = trimmed_head = trimmed_tail = 0
    audio_end = None
    original_audio_start = audio_start
    original_audio_end = audio_start + audio_duration if has_audio else None
    if has_audio:
        audio_end = audio_start + audio_duration
        audio_offset_samples = round((audio_start - video_start) * 48000)
        if abs(audio_offset_samples) > 2000:
            raise PreparationError(
                f'audio start offset exceeds one 24 fps frame: observed {audio_offset_samples} samples; '
                f'requested {requested}; available {available}'
            )
        selected_end = min(requested_end, target_end)
        decoded_start, decoded_end = _decoded_audio_coverage(source, selected[1], anchor, selected_end)
        audio_start = max(audio_start, decoded_start)
        audio_end = min(audio_end, decoded_end)
        if min(audio_end, selected_end) <= max(audio_start, anchor):
            raise PreparationError(f'source audio has no coverage of requested range {requested}; available {available}')
        head_pad = max(0, round((audio_start - anchor) * 48000))
        source_tail_shortfall = max(0, round((selected_end - audio_end) * 48000))
        rounding_tail = max(0, round((target_end - selected_end) * 48000))
        tail_pad = source_tail_shortfall + rounding_tail
        if head_pad > 2000 or tail_pad > 2000:
            raise PreparationError(
                f'source streams must cover the requested range: audio shortfall exceeds one 24 fps frame: '
                f'observed head={head_pad}, tail={tail_pad} samples; '
                f'requested {requested}; available {available}'
            )
        trimmed_head = max(0, round((anchor - original_audio_start) * 48000))
        trimmed_tail = max(0, round((original_audio_end - selected_end) * 48000))
    source_hash = _digest(source)
    destination.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix='.h3-conform-', dir=destination.parent) as temporary:
        work = Path(temporary)
        output = work / 'source.mp4'
        # dar includes the input sample aspect ratio. Crop in source pixel
        # coordinates or fit by display shape before declaring square pixels.
        if fit == 'fill':
            scale = ("crop=w='if(gt(dar,16/9),ih*16/(9*sar),iw)':"
                     "h='if(gt(dar,16/9),ih,iw*sar*9/16)',scale=1024:576,setsar=1")
        else:
            scale = ("scale=w='if(gt(dar,16/9),1024,max(2,trunc(576*dar/2)*2))':"
                     "h='if(gt(dar,16/9),max(2,trunc(1024/dar/2)*2),576)',"
                     "setsar=1,pad=1024:576:(ow-iw)/2:(oh-ih)/2")
        if square_default:
            scale = 'setsar=1,' + scale
        start_time = format(float(anchor), '.15f')
        end_time = format(float(requested_end), '.15f')
        audio_end_time = format(float(min(requested_end, target_end)), '.15f')
        video = f'trim=start={start_time}:end={end_time},setpts=PTS-({start_time})/TB,fps=24:start_time=0:round=near,trim=end_frame={int(frames)},{scale},setsar=1'
        audio = (f'atrim=start={start_time}:end={audio_end_time},asetpts=PTS-STARTPTS,aresample=48000,'
                 f'adelay={head_pad}S:all=1,apad=whole_len={target_samples},'
                 f'atrim=end_sample={target_samples},asetpts=N/SR/TB')
        filters = f'[0:v:0]{video}[v]' + (f';[0:a:0]{audio}[a]' if has_audio else '')
        cmd = ['ffmpeg', '-v', 'error', '-nostdin', '-copyts', '-i', str(source), '-filter_complex', filters, '-map', '[v]']
        if has_audio:
            cmd += ['-map', '[a]', '-c:a', 'aac', '-ar', '48000']
        cmd += ['-c:v', 'libx264', '-crf', '12', '-pix_fmt', 'yuv420p', str(output)]
        try:
            subprocess.run(cmd, check=True, capture_output=True, text=True)
        except (OSError, subprocess.CalledProcessError) as exc:
            raise PreparationError(f'could not conform source: {getattr(exc, "stderr", "") or exc}') from exc
        if _digest(source) != source_hash:
            raise PreparationError('source changed while conforming')
        _check_source_av_rate('conformed source', output)
        try:
            measurement = validate(output) if has_audio else {'has_audio': False}
            if hasattr(measurement, 'to_dict'):
                measurement = measurement.to_dict()
        except ValueError as exc:
            raise PreparationError(f'conformed source failed decoded AV validation: {exc}') from exc
        result = _probe(output)
        video_out = next(s for s in result['streams'] if s.get('codec_type') == 'video')
        audio_out = next((s for s in result['streams'] if s.get('codec_type') == 'audio'), None)
        if bool(audio_out) != has_audio or int(video_out.get('nb_frames', 0)) != frames:
            raise PreparationError('conformed output did not contain the expected video frames and streams')
        duration = Fraction(frames, 24)
        if audio_out is not None and abs(Fraction(str(audio_out['duration'])) - duration) > Fraction(1, 24):
            raise PreparationError('conformed audio does not cover the selected interval')
        manifest = {'kind': 'h3_source_conform', 'version': 1,
                    'source': {'path': str(source), 'sha256': source_hash, 'range_seconds': [start, end],
                               'sample_aspect_ratio': '1:1' if square_default else str(aspect),
                               'sample_aspect_assumed_square': square_default},
                    'output': {'file': 'source.mp4', 'sha256': _digest(output), 'range_seconds': [0, float(duration)],
                               'frames': int(frames), 'fps': 24, 'width': 1024, 'height': 576, 'fit': fit},
                    'audio': {'sample_rate': 48000, 'codec': 'aac'} if has_audio else None,
                    'timing': {'requested_duration_seconds': float(last - first),
                               'prepared_duration_seconds': float(target_duration),
                               'rounding_delta_seconds': float(target_duration - (last - first)),
                               'video_start_seconds': float(video_start),
                               'audio_start_seconds': float(original_audio_start) if has_audio else None,
                               'audio_offset_samples': audio_offset_samples,
                               'head_pad_samples': head_pad, 'tail_pad_samples': tail_pad,
                               'trimmed_head_samples': trimmed_head, 'trimmed_tail_samples': trimmed_tail,
                               'target_audio_samples': target_samples if has_audio else 0},
                    'decoded_av': measurement}
        (work / 'manifest.json').write_text(json.dumps(manifest, indent=2) + '\n', encoding='utf-8')
        work.rename(destination)
    return manifest
