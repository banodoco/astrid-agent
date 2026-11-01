"""Shared rendering test helpers (execution env, probes, source fixtures).

Shared by the three.js, Remotion, and FFmpeg backend tests so the duplicated
ffprobe/ffmpeg scaffolding lives in one
place.  The env-skip trios (``_missing_environment`` etc.) stay in their own
test modules on purpose: they are genuinely divergent.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from contextlib import contextmanager
from pathlib import Path


@contextmanager
def _execution_env():
    """Prepend the active python bin and node bin to PATH so transport-spawned
    children resolve the same node and the same python3 (with the banodoco
    timeline schema) as the test process."""
    node_bin = (
        str(Path(shutil.which("node")).resolve().parent)
        if shutil.which("node")
        else ""
    )
    # Do not resolve the interpreter symlink: in a virtual environment the
    # resolved binary lives in the base Python installation, while the
    # sibling ``python3`` we need is in the venv's own ``bin`` directory.
    python_bin = str(Path(sys.executable).parent)
    old_path = os.environ.get("PATH", "")
    schema_env = "ASTRID_TIMELINE_SCHEMA_PYTHONPATH"
    old_schema_root = os.environ.get(schema_env)
    # A test runner may import the canonical package from a venv/site-packages
    # path without exporting the install root.  Renderer commands use
    # ``python3`` from PATH, so make that dependency explicit for the child
    # instead of relying on whichever interpreter happens to be first there.
    if not old_schema_root:
        try:
            import banodoco_timeline_schema

            schema_root = Path(banodoco_timeline_schema.__file__).resolve().parent
        except (ImportError, AttributeError):
            schema_root = None
        if schema_root is not None:
            os.environ[schema_env] = str(schema_root.parent)
    os.environ["PATH"] = ":".join(
        [d for d in (python_bin, node_bin) if d] + [old_path]
    )
    try:
        yield
    finally:
        os.environ["PATH"] = old_path
        if old_schema_root is None:
            os.environ.pop(schema_env, None)
        else:
            os.environ[schema_env] = old_schema_root


def _probe(path: Path) -> dict:
    out = subprocess.run(
        [
            "ffprobe",
            "-v",
            "error",
            "-count_frames",
            "-show_entries",
            "stream=codec_name,codec_type,width,height,pix_fmt,time_base,avg_frame_rate,nb_read_frames,duration",
            "-show_entries",
            "format=duration",
            "-of",
            "json",
            str(path),
        ],
        check=True,
        capture_output=True,
        text=True,
    ).stdout
    return json.loads(out)


def _frame_md5(path: Path, frame: int) -> str:
    out = subprocess.run(
        [
            "ffmpeg",
            "-v",
            "error",
            "-i",
            str(path),
            "-vf",
            f"select=eq(n\\,{frame})",
            "-frames:v",
            "1",
            "-f",
            "md5",
            "-",
        ],
        check=True,
        capture_output=True,
        text=True,
    ).stdout
    return out.strip().split("=")[-1].strip()


def _source_video(tmp_path: Path, *, audio: bool = False) -> Path:
    """A tiny real h264 (+aac when audio=True) source clip used by media clips."""
    source_path = tmp_path / "source.mp4"
    command = [
        "ffmpeg",
        "-hide_banner",
        "-loglevel",
        "error",
        "-y",
        "-f",
        "lavfi",
        "-i",
        "color=size=320x180:rate=24",
    ]
    if audio:
        command += [
            "-f",
            "lavfi",
            "-i",
            "anullsrc=channel_layout=stereo:sample_rate=48000",
        ]
    command += ["-frames:v", "24"]
    if audio:
        command += ["-shortest"]
    command += [
        "-c:v",
        "libx264",
        "-profile:v",
        "main",
        "-pix_fmt",
        "yuv420p",
    ]
    if audio:
        command += ["-c:a", "aac"]
    command += ["-video_track_timescale", "12288", str(source_path)]
    subprocess.run(command, check=True, capture_output=True, text=True)
    return source_path
