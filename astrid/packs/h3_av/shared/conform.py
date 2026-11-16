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
            'stream=codec_type,start_time,duration,nb_frames,sample_aspect_ratio:format=start_time,duration',
            '-of', 'json', str(path)], capture_output=True, text=True, check=True, timeout=30)
        return json.loads(proc.stdout)
    except (OSError, subprocess.SubprocessError, ValueError) as exc:
        raise PreparationError(f'cannot inspect source audiovisual streams: {exc}') from exc


def conform_source(source: Path, destination: Path, *, start: str, end: str, fit: str, validate) -> dict:
    """Extract a half-open source interval, with explicit centered fit, to a new directory.

    Source timestamps must start together at zero and available streams must cover the
    requested interval. No padding, speed change or missing-audio substitution is
    performed. The interval length must be an exact number of 24 fps frames.
    """
    try:
        first, last = Fraction(start), Fraction(end)
    except (ValueError, ZeroDivisionError) as exc:
        raise PreparationError('start and end must be finite decimal seconds') from exc
    frame_value = (last - first) * 24
    frames = round(frame_value)
    if first < 0 or last <= first or frames <= 0 or abs(frame_value - frames) > Fraction(1, 1000000):
        raise PreparationError('range must be positive and its duration an exact number of 24 fps frames')
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
    present = [s for s in selected if s is not None]
    if selected[0].get('sample_aspect_ratio') != '1:1':
        raise PreparationError('source must have a known square pixel aspect ratio')
    try:
        starts = [Fraction(str(info['format']['start_time']))] + [Fraction(str(s['start_time'])) for s in present]
        durations = [Fraction(str(s['duration'])) for s in present]
    except (KeyError, ValueError, ZeroDivisionError) as exc:
        raise PreparationError('source stream start times and durations must be measurable') from exc
    if any(value != 0 for value in starts):
        raise PreparationError('source stream and container start times must all be zero')
    if any(value < last for value in durations):
        raise PreparationError('source streams must cover the requested range')
    source_hash = _digest(source)
    destination.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix='.h3-conform-', dir=destination.parent) as temporary:
        work = Path(temporary)
        output = work / 'source.mp4'
        # Centered cover crops; centered contain adds black padding. Neither stretches.
        scale = ('scale=1024:576:force_original_aspect_ratio=increase,crop=1024:576'
                 if fit == 'fill' else
                 'scale=1024:576:force_original_aspect_ratio=decrease,pad=1024:576:(ow-iw)/2:(oh-ih)/2')
        start_time, end_time = format(float(first), '.15f'), format(float(last), '.15f')
        video = f'trim=start={start_time}:end={end_time},setpts=PTS-({start_time})/TB,fps=24:start_time=0,trim=end_frame={int(frames)},{scale},setsar=1'
        audio = f'atrim=start={start_time}:end={end_time},asetpts=PTS-({start_time})/TB,aresample=48000'
        filters = f'[0:v:0]{video}[v]' + (f';[0:a:0]{audio}[a]' if has_audio else '')
        cmd = ['ffmpeg', '-v', 'error', '-nostdin', '-i', str(source), '-filter_complex', filters, '-map', '[v]']
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
                    'source': {'path': str(source), 'sha256': source_hash, 'range_seconds': [start, end]},
                    'output': {'file': 'source.mp4', 'sha256': _digest(output), 'range_seconds': [0, float(duration)],
                               'frames': int(frames), 'fps': 24, 'width': 1024, 'height': 576, 'fit': fit},
                    'audio': {'sample_rate': 48000, 'codec': 'aac'} if has_audio else None, 'decoded_av': measurement}
        (work / 'manifest.json').write_text(json.dumps(manifest, indent=2) + '\n', encoding='utf-8')
        work.rename(destination)
    return manifest
