#!/usr/bin/env python3
"""Score transcript windows as stream clip candidates."""

from __future__ import annotations

# ruff: noqa: E402
from astrid.pack_api import AstridError
from astrid.pack_api import guard_canonical_entrypoint, run_pack_main

guard_canonical_entrypoint("stream_content.clip_candidates")
import argparse
from pathlib import Path

from astrid.packs.stream_content.actions.clip_candidates.scoring import (
    build_candidates,
    write_candidates,
)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Score publishable clip candidates from a stream transcript.")
    parser.add_argument("--transcript", type=Path, required=True, help="Whisper transcript.json.")
    parser.add_argument("--segment-map", type=Path, help="Optional segment_map.json to restrict to content/screening.")
    parser.add_argument("--brief", type=Path, help="Optional markdown brief for topic boosts.")
    parser.add_argument("--out", type=Path, required=True, help="Output candidates.json path.")
    return parser


def main(argv: list[str] | None = None) -> int:
    def _run() -> int:
        args = build_parser().parse_args(argv)
        transcript = args.transcript.expanduser().resolve()
        if not transcript.is_file():
            raise AstridError(f"transcript not found: {transcript}", recovery_command="rerun with an existing --transcript file")
        segment_map = args.segment_map.expanduser().resolve() if args.segment_map else None
        if segment_map is not None and not segment_map.is_file():
            raise AstridError(f"segment_map not found: {segment_map}", recovery_command="rerun with an existing --segment-map file")
        brief = args.brief.expanduser().resolve() if args.brief else None
        if brief is not None and not brief.is_file():
            raise AstridError(f"brief not found: {brief}", recovery_command="rerun with an existing --brief file")
        out = args.out.expanduser().resolve()
        payload = build_candidates(transcript=transcript, segment_map=segment_map, brief=brief)
        write_candidates(payload, out)
        print(f"stream_content.clip_candidates: wrote={out} candidates={len(payload['candidates'])}")
        return 0

    return run_pack_main("stream_content.clip_candidates", _run, argv=argv)


if __name__ == "__main__":
    raise SystemExit(main())
