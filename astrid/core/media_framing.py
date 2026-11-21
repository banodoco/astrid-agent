"""FFmpeg stream inspection and display-aspect framing shared by media workflows."""

from __future__ import annotations

import json
import subprocess
from fractions import Fraction
from pathlib import Path


def probe_streams(path: Path) -> dict:
    """Read stream clocks, duration, and sample aspect ratio through FFprobe."""
    try:
        proc = subprocess.run(
            ['ffprobe', '-v', 'error', '-show_entries',
             'stream=codec_type,start_time,start_pts,duration,duration_ts,time_base,'
             'sample_rate,nb_frames,sample_aspect_ratio,avg_frame_rate:format=start_time,duration',
             '-of', 'json', str(path)],
            capture_output=True, text=True, check=True, timeout=30,
        )
        return json.loads(proc.stdout)
    except (OSError, subprocess.SubprocessError, ValueError) as exc:
        raise ValueError(f'cannot inspect source audiovisual streams: {exc}') from exc


def fill_filter(width: int, height: int, *, square_default: bool) -> str:
    """Center crop in source pixels using display aspect, then scale to square pixels."""
    ratio = Fraction(width, height)
    w, h = ratio.numerator, ratio.denominator
    prefix = 'setsar=1,' if square_default else ''
    return (f"{prefix}crop=w='if(gt(dar,{w}/{h}),ih*{w}/({h}*sar),iw)':"
            f"h='if(gt(dar,{w}/{h}),ih,iw*sar*{h}/{w})',scale={width}:{height},setsar=1")
