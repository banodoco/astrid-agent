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
import tarfile
from contextlib import contextmanager
from pathlib import Path


def _assert_private_destination(path: Path, borrowed: Path) -> None:
    """Reject writes whose destination or ancestor is a link or borrowed path."""
    resolved = path.resolve(strict=False)
    if resolved == borrowed or resolved.is_relative_to(borrowed):
        raise ValueError(f"staging destination resolves into borrowed graph: {path}")
    current = path
    while current != current.parent:
        if current.is_symlink():
            raise ValueError(f"staging destination has a linked ancestor: {path}")
        current = current.parent
    if path.exists() and not path.is_dir():
        raise ValueError(f"staging destination is not a directory: {path}")


def _extract_private_tarball(archive: Path, destination: Path, *, borrowed: Path) -> None:
    """Extract only after proving the destination is a private real directory."""
    _assert_private_destination(destination, borrowed)
    destination.mkdir(parents=True, exist_ok=True)
    _assert_private_destination(destination, borrowed)
    with tarfile.open(archive) as source:
        source.extractall(destination, filter="data")


def _stage_private_node_modules(
    peers: Path,
    modules: Path,
    *,
    skip_scopes: frozenset[str] | set[str] = frozenset(),
) -> None:
    """Link a borrowed peer graph into a private node_modules root.

    Scope directories are owned by the stage; only their package leaves link
    to the read-only peer graph.  The realpath check must run before every
    directory or leaf write so a linked parent cannot turn a link operation
    into a write through the borrowed graph.
    """
    borrowed = peers.resolve(strict=True)

    _assert_private_destination(modules, borrowed)
    modules.mkdir(parents=True, exist_ok=True)
    _assert_private_destination(modules, borrowed)
    for peer in sorted(peers.iterdir(), key=lambda item: item.name):
        if peer.name in skip_scopes:
            continue
        if peer.name.startswith("@"):
            scope = modules / peer.name
            _assert_private_destination(scope, borrowed)
            scope.mkdir(parents=True, exist_ok=True)
            if scope.is_symlink() or not scope.is_dir():
                raise ValueError(f"staging scope is not a real directory: {scope}")
            for leaf in sorted(peer.iterdir(), key=lambda item: item.name):
                target = scope / leaf.name
                _assert_private_destination(target.parent, borrowed)
                subprocess.run(
                    ["ln", "-sfnT", str(leaf.resolve(strict=True)), str(target)],
                    check=True,
                )
        else:
            target = modules / peer.name
            _assert_private_destination(target.parent, borrowed)
            subprocess.run(
                ["ln", "-sfnT", str(peer.resolve(strict=True)), str(target)],
                check=True,
            )


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
    if not old_schema_root:
        os.environ[schema_env] = str(
            Path(__file__).resolve().parents[3] / "astrid/core/timeline/schema/timeline-config.schema.json"
        )
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
