#!/usr/bin/env python3
"""Build a labeled segment map for a stream recording."""

from __future__ import annotations

from astrid.pack_api import AstridError
from astrid.pack_api import guard_canonical_entrypoint, run_pack_main

guard_canonical_entrypoint("stream_content.segment_map")
import argparse
from pathlib import Path

from astrid.packs.stream_content.actions.segment_map.core import (
    build_segment_map,
    write_segment_map,
)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Fuse OCR, transcript density, and scene cuts into a stream segment map.")
    parser.add_argument("--video", type=Path, required=True, help="Source stream recording.")
    parser.add_argument("--transcript", type=Path, help="Optional Whisper transcript.json.")
    parser.add_argument("--scenes", type=Path, help="Optional scenes.json.")
    parser.add_argument("--out", type=Path, required=True, help="Output segment_map.json path.")
    return parser


def main(argv: list[str] | None = None) -> int:
    def _run() -> int:
        args = build_parser().parse_args(argv)
        video = args.video.expanduser().resolve()
        if not video.is_file():
            raise AstridError(f"video not found: {video}", recovery_command="rerun with an existing --video file")
        transcript = args.transcript.expanduser().resolve() if args.transcript else None
        if transcript is not None and not transcript.is_file():
            raise AstridError(f"transcript not found: {transcript}", recovery_command="rerun with an existing --transcript file")
        scenes = args.scenes.expanduser().resolve() if args.scenes else None
        if scenes is not None and not scenes.is_file():
            raise AstridError(f"scenes not found: {scenes}", recovery_command="rerun with an existing --scenes file")
        out = args.out.expanduser().resolve()
        payload = build_segment_map(
            video=video,
            transcript_path=transcript,
            scenes_path=scenes,
            ocr_work_dir=out.parent / "_ocr_frames",
        )
        write_segment_map(payload, out)
        print(f"stream_content.segment_map: wrote={out} segments={len(payload['segments'])}")
        return 0

    return run_pack_main("stream_content.segment_map", _run, argv=argv)


if __name__ == "__main__":
    raise SystemExit(main())
