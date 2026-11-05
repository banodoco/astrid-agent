#!/usr/bin/env python3
"""Extract a clip segment from a video using ffmpeg stream copy.

Invoked by the Astrid runtime per command.argv in executor.yaml.
Parses --input, --start, --dur, --output from argv, validates them,
and shells out to ffmpeg.
"""

from __future__ import annotations

from astrid.pack_api import AstridError
from astrid.pack_api import guard_canonical_entrypoint, run_pack_main

guard_canonical_entrypoint("media.clip_extract")
import argparse
import subprocess
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable

from astrid.pack_api import build_manifest, write_manifest

Runner = Callable[..., subprocess.CompletedProcess[str]]


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="media.clip_extract",
        description="Extract a clip from a video using ffmpeg -ss -t -c copy.",
    )
    parser.add_argument(
        "--input", type=Path, required=True, help="Source video file path."
    )
    parser.add_argument(
        "--start", type=float, required=True, help="Start time in seconds."
    )
    parser.add_argument(
        "--dur", type=float, required=True, help="Duration in seconds."
    )
    parser.add_argument(
        "--output", type=Path, required=True, help="Output video file path."
    )
    return parser


def validate_args(args: argparse.Namespace) -> str | None:
    """Return an error string or None if valid."""
    if not args.input.is_file():
        return f"input file not found: {args.input}"
    if args.start < 0:
        return f"start time must be >= 0, got {args.start}"
    if args.dur <= 0:
        return f"duration must be > 0, got {args.dur}"
    return None


def build_ffmpeg_cmd(src: Path, start: float, dur: float, out: Path) -> list[str]:
    return [
        "ffmpeg",
        "-y",
        "-i",
        str(src),
        "-ss",
        str(start),
        "-t",
        str(dur),
        "-c",
        "copy",
        str(out),
    ]


def main(argv: list[str] | None = None, *, runner: Runner = subprocess.run) -> int:
    def _run() -> int:
        args = build_parser().parse_args(argv)

        # Resolve paths
        src = args.input.expanduser().resolve()
        out = args.output.expanduser().resolve()

        # Guard against missing/invalid inputs
        error = validate_args(
            argparse.Namespace(input=src, start=args.start, dur=args.dur)
        )
        if error is not None:
            raise AstridError(
                error,
                recovery_command="check the input file path, start time, and duration are correct, then rerun",
            )

        # Ensure the output directory exists before ffmpeg writes to it.
        out.parent.mkdir(parents=True, exist_ok=True)

        cmd = build_ffmpeg_cmd(src, args.start, args.dur, out)

        result = runner(cmd, check=False)

        if result.returncode != 0:
            raise AstridError(
                f"ffmpeg exited with {result.returncode}",
                recovery_command="verify the input file is a valid video and ffmpeg is installed, then rerun",
                state_snapshot={"ffmpeg_stderr": result.stderr} if result.stderr else None,
            )

        # --- universal result manifest -------------------
        manifest_path = out.parent / "manifest.json"
        manifest = build_manifest(
            kind="clip_extract",
            inputs={
                "input": str(src),
                "start": args.start,
                "dur": args.dur,
            },
            outputs=[{"name": "output", "path": out.name, "type": "file"}],
            created=datetime.now(timezone.utc).isoformat(),
        )
        write_manifest(manifest_path, manifest)
        # ---------------------------------------------------------------------

        return 0

    return run_pack_main("media.clip_extract", _run, argv=argv)


if __name__ == "__main__":
    raise SystemExit(main())
