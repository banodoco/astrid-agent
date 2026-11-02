"""Align MIDI or audio to a target recording."""
from __future__ import annotations

from astrid.pack_api import guard_canonical_entrypoint, run_pack_main

guard_canonical_entrypoint("music.align")

import argparse
from datetime import datetime, timezone
from pathlib import Path

from astrid.pack_api import build_manifest, write_json_atomic, write_manifest
from astrid.pack_api import AstridError
from astrid.packs.chiptune.actions import _midi
from astrid.packs.music.actions import _analysis


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="music.align")
    parser.add_argument("--reference", type=Path, required=True)
    parser.add_argument("--target", type=Path, required=True)
    parser.add_argument("--ref-s", type=float)
    parser.add_argument("--window-s", type=float, default=20)
    parser.add_argument("--out", type=Path, required=True)
    return parser


def main(argv: list[str] | None = None) -> int:
    def _run() -> int:
        args = build_parser().parse_args(argv)
        if args.window_s <= 0 or args.ref_s is not None and args.ref_s < 0:
            raise AstridError("window_s must be positive and ref_s nonnegative")
        target_onset, target_chroma = _analysis.features(_analysis.decode(args.target))
        if args.reference.suffix.lower() in (".mid", ".midi"):
            notes, _ = _midi.parse_midi(args.reference.read_bytes())
            if not notes:
                raise AstridError("reference MIDI has no playable notes")
            result = _analysis.align_midi(notes, target_onset, args.ref_s, args.window_s)
            result["method"] = "midi_onset_flux"
        else:
            _, ref_chroma = _analysis.features(_analysis.decode(args.reference))
            result = _analysis.align_audio(ref_chroma, target_chroma, args.ref_s, args.window_s)
            result["method"] = "audio_chroma"
        result["reference"] = args.reference.name
        result["target"] = args.target.name
        out = args.out.expanduser().resolve()
        out.mkdir(parents=True, exist_ok=True)
        write_json_atomic(out / "alignment.json", result)
        manifest = build_manifest(kind="music.align", inputs={"reference": args.reference.name,
                                  "target": args.target.name, "ref_s": args.ref_s,
                                  "window_s": args.window_s},
                                  outputs=[{"name": "alignment", "path": "alignment.json", "type": "file",
                                            "role": "result", "is_primary": True, "media_type": "application/json"}],
                                  created=datetime.now(timezone.utc).isoformat(), **result)
        write_manifest(out / "manifest.json", manifest)
        return 0
    return run_pack_main("music.align", _run, argv=argv)


if __name__ == "__main__":
    raise SystemExit(main())
