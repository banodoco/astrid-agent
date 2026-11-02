#!/usr/bin/env python3
"""Play a MIDI file as an NES-style chiptune (chiptune.midi).

Invoked by the Astrid runtime from the pack's V3 action declaration. The parsing, voice allocation and
rendering live in ``astrid.packs.chiptune.actions._midi``.
"""

from __future__ import annotations

from astrid.pack_api import guard_canonical_entrypoint, run_pack_main

guard_canonical_entrypoint("chiptune.midi")

import argparse
import wave
from datetime import datetime, timezone
from pathlib import Path

from astrid.pack_api import build_manifest, write_json_atomic, write_manifest
from astrid.pack_api import AstridError
from astrid.packs.chiptune.actions import _midi
from astrid.packs.chiptune.actions import _synth as syn


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="chiptune.midi", description="Play a MIDI file as an NES-style chiptune.")
    parser.add_argument("--midi", type=Path, required=True, help="A Standard MIDI File.")
    parser.add_argument("--duration-s", type=float, default=None, help="Cue length from --start-s (default: to the last note + 1 s).")
    parser.add_argument("--start-s", type=float, default=0.0)
    parser.add_argument("--transpose", type=int, default=0)
    parser.add_argument("--master-db", type=float, default=-16.0)
    parser.add_argument("--out", type=Path, required=True, help="Output directory.")
    return parser


def main(argv: list[str] | None = None) -> int:
    def _run() -> int:
        args = build_parser().parse_args(argv)
        path = args.midi.expanduser()
        if not path.is_file():
            raise AstridError(f"no MIDI file at {path}")
        notes, summary = _midi.parse_midi(path.read_bytes())
        if not notes:
            raise AstridError("the MIDI file has no notes outside the drum channel")
        start_s = max(0.0, float(args.start_s))
        duration_s = float(args.duration_s) if args.duration_s else max(0.5, summary["duration_s"] + 1.0 - start_s)
        buf, voices = _midi.render(notes, duration_s=duration_s, transpose=int(args.transpose),
                                   master_db=float(args.master_db), start_s=start_s)
        out = args.out.expanduser().resolve()
        out.mkdir(parents=True, exist_ok=True)
        music_path, voices_path = out / "music.wav", out / "voices.json"
        for stale in (music_path, voices_path, out / "manifest.json"):
            stale.unlink(missing_ok=True)
        with wave.open(str(music_path), "wb") as handle:
            handle.setnchannels(2)
            handle.setsampwidth(2)
            handle.setframerate(syn.SAMPLE_RATE)
            handle.writeframes(syn.to_int16_stereo(buf).tobytes())
        sample_count = int(buf.shape[1])
        peak_db, rms_db = syn.measure_db(buf)
        write_json_atomic(voices_path, {"schema_version": 1, "source": "chiptune.midi", "midi": summary,
                                        "voices": voices, "start_s": start_s,
                                        "duration_s": round(sample_count / syn.SAMPLE_RATE, 6)})
        manifest = build_manifest(
            kind="chiptune.midi",
            inputs={"midi": path.name, "start_s": start_s, "duration_s": duration_s,
                    "transpose": int(args.transpose), "master_db": float(args.master_db)},
            outputs=[
                {"name": "music", "path": "music.wav", "type": "file", "artifact_type": "audio", "role": "result",
                 "is_primary": True, "media_type": "audio/wav",
                 "duration_seconds": round(sample_count / syn.SAMPLE_RATE, 6)},
                {"name": "voices", "path": "voices.json", "type": "file", "role": "auxiliary",
                 "media_type": "application/json"},
            ],
            created=datetime.now(timezone.utc).isoformat(),
            sample_rate=syn.SAMPLE_RATE,
            channels=2,
            peak_dbfs=peak_db,
            rms_dbfs=rms_db,
            midi=summary,
            voices=voices,
        )
        write_manifest(out / "manifest.json", manifest)
        return 0

    return run_pack_main("chiptune.midi", _run, argv=argv)


if __name__ == "__main__":
    raise SystemExit(main())
