"""Measure EBU R128 integrated loudness of a used audio source span."""
from __future__ import annotations

from astrid.pack_api import guard_canonical_entrypoint, run_pack_main

guard_canonical_entrypoint("music.level")

import argparse
import math
import re
import subprocess
from datetime import datetime, timezone
from pathlib import Path

from astrid.pack_api import build_manifest, write_json_atomic, write_manifest
from astrid.pack_api import AstridError


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="music.level")
    parser.add_argument("--audio", type=Path, required=True)
    parser.add_argument("--from-s", type=float, required=True)
    parser.add_argument("--to-s", type=float, required=True)
    parser.add_argument("--target-lufs", type=float, default=-16)
    parser.add_argument("--out", type=Path, required=True)
    return parser


def measure(audio: Path, from_s: float, to_s: float) -> float:
    if not audio.is_file():
        raise AstridError(f"audio file does not exist: {audio}")
    if not (math.isfinite(from_s) and math.isfinite(to_s) and 0 <= from_s < to_s):
        raise AstridError("source span must satisfy 0 <= from_s < to_s")
    proc = subprocess.run(
        ["ffmpeg", "-nostdin", "-hide_banner", "-v", "info", "-ss", str(from_s),
         "-i", str(audio), "-t", str(to_s - from_s), "-vn", "-af", "ebur128",
         "-f", "null", "-"], capture_output=True, text=True, check=False,
    )
    if proc.returncode:
        raise AstridError(f"ffmpeg loudness measurement failed: {proc.stderr.strip()[-500:]}")
    summaries = re.findall(r"Summary:.*?Integrated loudness:.*?I:\s*([+-]?(?:\d+(?:\.\d+)?|inf))\s*LUFS",
                           proc.stderr, re.S)
    if not summaries:
        raise AstridError("ffmpeg did not report integrated loudness")
    measured = float(summaries[-1])
    if not math.isfinite(measured):
        raise AstridError("source span has no measurable gated loudness")
    return measured


def main(argv: list[str] | None = None) -> int:
    def _run() -> int:
        args = build_parser().parse_args(argv)
        if not math.isfinite(args.target_lufs):
            raise AstridError("target_lufs must be finite")
        measured = measure(args.audio, args.from_s, args.to_s)
        result = {"audio": args.audio.name, "from_s": args.from_s, "to_s": args.to_s,
                  "integrated_lufs": round(measured, 2), "target_lufs": args.target_lufs,
                  "gain_db": round(args.target_lufs - measured, 2), "method": "ffmpeg_ebur128"}
        out = args.out.expanduser().resolve()
        out.mkdir(parents=True, exist_ok=True)
        write_json_atomic(out / "level.json", result)
        write_manifest(out / "manifest.json", build_manifest(kind="music.level", inputs={
            "audio": args.audio.name, "from_s": args.from_s, "to_s": args.to_s,
            "target_lufs": args.target_lufs}, outputs=[{"name": "level", "path": "level.json",
            "type": "file", "role": "result", "is_primary": True, "media_type": "application/json"}],
            created=datetime.now(timezone.utc).isoformat(), **result))
        return 0
    return run_pack_main("music.level", _run, argv=argv)


if __name__ == "__main__":
    raise SystemExit(main())
