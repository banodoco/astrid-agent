"""Infer a beat grid from imported audio."""
from __future__ import annotations

from astrid.pack_api import guard_canonical_entrypoint, run_pack_main

guard_canonical_entrypoint("music.beats")

import argparse
from datetime import datetime, timezone
from pathlib import Path

from astrid.pack_api import build_manifest, write_json_atomic, write_manifest
from astrid.packs.music.actions import _analysis


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="music.beats")
    parser.add_argument("--audio", type=Path, required=True)
    parser.add_argument("--out", type=Path, required=True)
    return parser


def main(argv: list[str] | None = None) -> int:
    def _run() -> int:
        args = build_parser().parse_args(argv)
        onset, _ = _analysis.features(_analysis.decode(args.audio))
        result = _analysis.track_beats(onset)
        result["duration_s"] = round(len(onset) * _analysis.FRAME_S, 4)
        result["audio"] = args.audio.name
        out = args.out.expanduser().resolve()
        out.mkdir(parents=True, exist_ok=True)
        write_json_atomic(out / "beats.json", result)
        write_manifest(out / "manifest.json", build_manifest(kind="music.beats",
            inputs={"audio": args.audio.name}, outputs=[{"name": "beats", "path": "beats.json",
            "type": "file", "role": "result", "is_primary": True, "media_type": "application/json"}],
            created=datetime.now(timezone.utc).isoformat(), bpm=result["bpm"],
            beat_count=len(result["beats"]), downbeat_count=len(result["downbeats"])))
        return 0
    return run_pack_main("music.beats", _run, argv=argv)


if __name__ == "__main__":
    raise SystemExit(main())
