"""Download YouTube media through the host-managed yt-dlp boundary."""

from __future__ import annotations

from astrid.pack_api import AstridError
from astrid.pack_api import guard_canonical_entrypoint

guard_canonical_entrypoint("youtube.youtube_audio")

import argparse
import shutil
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from astrid.pack_api import build_manifest, write_manifest
from astrid.pack_api import add_choice_arg
from astrid.pack_api import pack_die
from astrid.pack_api import run_pack_main


def _die(msg: str, code: int = 2) -> None:
    pack_die(
        msg,
        recovery_command="install the missing dependency or fix the YouTube download inputs, then rerun",
        state_snapshot={"exit_code": code},
    )


def _expected_output(out: Path, *, mode: str, audio_format: str) -> Path:
    if out.suffix:
        return out
    extension = audio_format if mode == "audio" else "mp4"
    return out.with_suffix(f".{extension}")


def _manifest_for(
    *, target: str, mode: str, output: Path, audio_format: str
) -> dict[str, Any]:
    media_type = "audio/mpeg" if mode == "audio" else "video/mp4"
    artifact_type = "audio" if mode == "audio" else "video/clip"
    return build_manifest(
        kind="youtube.youtube_audio",
        inputs={"target": target, "mode": mode},
        outputs=[
            {
                "name": "media",
                "path": output.name,
                "type": "file",
                "artifact_type": artifact_type,
                "media_type": media_type,
                "ordinal": 0,
                "role": "result",
                "is_primary": True,
            }
        ],
        created=datetime.now(timezone.utc).isoformat(),
        warnings=[],
        target=target,
        mode=mode,
        output={
            "name": "media",
            "path": output.name,
            "extension": output.suffix.lstrip("."),
            "media_type": media_type,
            "bytes": output.stat().st_size,
        },
        provenance={
            "source": target,
            "tool": "yt-dlp",
            "mode": mode,
            "audio_format": audio_format if mode == "audio" else None,
        },
    )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--query",
        required=True,
        help="Free-text YouTube search query; a direct http(s) URL is downloaded directly.",
    )
    add_choice_arg(
        parser,
        "--mode",
        values=("audio", "video"),
        default="audio",
        help="audio: extract to MP3 (default). video: download MP4 without audio extraction.",
    )
    parser.add_argument(
        "--out",
        required=True,
        type=Path,
        help="Output path. The mode extension is appended when absent.",
    )
    # These remain CLI-level yt-dlp controls for the current boundary. They
    # are intentionally not public action inputs.
    parser.add_argument("--audio-format", default="mp3")
    parser.add_argument("--audio-quality", default="0")
    parser.add_argument(
        "--video-format",
        default="bv*[ext=mp4]+ba[ext=m4a]/b[ext=mp4]/bv*+ba/b",
    )
    parser.add_argument("--merge-output-format", default="mp4")
    return parser


def main(argv: list[str] | None = None) -> int:
    def _run() -> int:
        args = build_parser().parse_args(argv)

        if not shutil.which("yt-dlp"):
            _die("yt-dlp not found on PATH. Install via `pip install yt-dlp`.")
        if args.mode == "audio" and not shutil.which("ffmpeg"):
            _die("ffmpeg not found on PATH. yt-dlp needs it to extract audio.")

        out = _expected_output(args.out, mode=args.mode, audio_format=args.audio_format)
        out.parent.mkdir(parents=True, exist_ok=True)
        output_template = str(out.with_suffix(".%(ext)s"))
        target = (
            args.query
            if args.query.startswith(("http://", "https://"))
            else f"ytsearch1:{args.query}"
        )

        command = ["yt-dlp", "--no-warnings", "--output", output_template]
        if args.mode == "audio":
            command += [
                "--extract-audio",
                f"--audio-format={args.audio_format}",
                f"--audio-quality={args.audio_quality}",
            ]
        else:
            command += [
                "-f",
                args.video_format,
                "--merge-output-format",
                args.merge_output_format,
            ]
        command.append(target)

        print(f"[youtube_audio] {' '.join(command)}", file=sys.stderr)
        completed = subprocess.run(command, capture_output=True, text=True)
        if completed.returncode != 0:
            raise AstridError(
                f"yt-dlp failed (exit {completed.returncode})",
                recovery_command="inspect the YouTube URL/query and retry the download",
                state_snapshot={
                    "stdout": completed.stdout,
                    "stderr": completed.stderr,
                },
            )

        if not out.is_file() or out.stat().st_size <= 0:
            raise AstridError(
                f"yt-dlp returned success but expected output {out} is missing or corrupt",
                recovery_command="re-run youtube.youtube_audio and inspect the yt-dlp output",
                state_snapshot={
                    "stdout": completed.stdout,
                    "stderr": completed.stderr,
                    "output": str(out),
                },
            )

        print(f"Downloaded: {out} ({out.stat().st_size:,} bytes)", file=sys.stderr)
        write_manifest(
            out.parent / "manifest.json",
            _manifest_for(
                target=target,
                mode=args.mode,
                output=out,
                audio_format=args.audio_format,
            ),
        )
        return 0

    return run_pack_main("youtube.youtube_audio", _run, argv=argv)


if __name__ == "__main__":
    raise SystemExit(main())
