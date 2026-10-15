#!/usr/bin/env python3
"""Compose a tempo-locked chiptune cue and its beat grid (chiptune.compose).

Invoked by the Astrid runtime from the pack's V3 action declaration. The synthesis
lives in ``astrid.packs.chiptune.actions._synth``.
"""

from __future__ import annotations

from astrid.core.pack.entrypoint import guard_canonical_entrypoint, run_pack_main

guard_canonical_entrypoint("chiptune.compose")

import argparse
import wave
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from astrid.core._shared.result_manifest import build_manifest, write_json_atomic, write_manifest
from astrid.core.contracts.errors import AstridError
from astrid.packs.chiptune.actions import _synth as syn


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="chiptune.compose", description="Compose a chiptune music cue.")
    parser.add_argument("--duration-s", type=float, required=True, help="Exact cue length in seconds.")
    parser.add_argument("--bpm", type=float, default=132.0)
    parser.add_argument("--key", default="A minor", help="Tonic and mode, e.g. 'A minor' or 'F# major'.")
    parser.add_argument("--seed", type=int, default=1)
    parser.add_argument("--sections", required=True, help="JSON array of {start_s, end_s, energy, mood}.")
    parser.add_argument("--hits", default=None, help="Optional JSON array of accent times or {t, kind} objects (kind: stab or thud).")
    parser.add_argument("--duck", default=None, help="Optional JSON array of {start_s, end_s, gain_db}.")
    parser.add_argument("--vo-mask", default=None, help="Optional JSON array of [start_s, end_s] speech spans.")
    parser.add_argument("--mutes", default=None, help="Optional JSON array of [start_s, end_s] hard-mute windows (exact silence).")
    parser.add_argument("--duck-db", type=float, default=-10.0)
    parser.add_argument("--master-db", type=float, default=-16.0)
    parser.add_argument("--style", default="nes")
    parser.add_argument("--out", type=Path, required=True, help="Output directory.")
    return parser


def _parse_duck(raw: Any, default_db: float) -> list[tuple[float, float, float]]:
    items = syn.parse_structured(raw, "duck") or []
    if not isinstance(items, list):
        raise AstridError("duck must be a JSON array of {start_s, end_s, gain_db}")
    out: list[tuple[float, float, float]] = []
    for index, item in enumerate(items):
        if not isinstance(item, dict) or "start_s" not in item or "end_s" not in item:
            raise AstridError(f"duck[{index}] needs start_s and end_s")
        out.append((float(item["start_s"]), float(item["end_s"]), float(item.get("gain_db", default_db))))
    return out


def _parse_vo_mask(raw: Any) -> list[tuple[float, float]]:
    items = syn.parse_structured(raw, "vo_mask") or []
    if not isinstance(items, list):
        raise AstridError("vo_mask must be a JSON array of [start_s, end_s] pairs")
    out: list[tuple[float, float]] = []
    for index, item in enumerate(items):
        if not isinstance(item, (list, tuple)) or len(item) != 2:
            raise AstridError(f"vo_mask[{index}] must be [start_s, end_s]")
        out.append((float(item[0]), float(item[1])))
    return out


def _parse_mutes(raw: Any) -> list[tuple[float, float]]:
    items = syn.parse_structured(raw, "mutes") or []
    if not isinstance(items, list):
        raise AstridError("mutes must be a JSON array of [start_s, end_s] pairs")
    out: list[tuple[float, float]] = []
    for index, item in enumerate(items):
        if not isinstance(item, (list, tuple)) or len(item) != 2:
            raise AstridError(f"mutes[{index}] must be [start_s, end_s]")
        out.append((float(item[0]), float(item[1])))
    return out


def _write_wav(path: Path, pcm: Any, sample_rate: int) -> None:
    with wave.open(str(path), "wb") as handle:
        handle.setnchannels(2)
        handle.setsampwidth(2)
        handle.setframerate(sample_rate)
        handle.writeframes(pcm.tobytes())


def main(argv: list[str] | None = None) -> int:
    def _run() -> int:
        args = build_parser().parse_args(argv)
        duration_s = float(args.duration_s)
        sections = syn.parse_structured(args.sections, "sections")
        hits = syn.parse_hits(args.hits)
        hit_events = syn.normalize_hits(hits, duration_s)
        buf, meta = syn.compose_music(
            duration_s=duration_s,
            bpm=float(args.bpm),
            key=args.key,
            seed=int(args.seed),
            sections=sections,
            hits=hits,
            duck=_parse_duck(args.duck, float(args.duck_db)),
            vo_mask=_parse_vo_mask(args.vo_mask),
            duck_db=float(args.duck_db),
            master_db=float(args.master_db),
            style=args.style,
            mutes=_parse_mutes(args.mutes),
        )
        out = args.out.expanduser().resolve()
        out.mkdir(parents=True, exist_ok=True)
        music_path = out / "music.wav"
        beats_path = out / "beats.json"
        music_path.unlink(missing_ok=True)
        beats_path.unlink(missing_ok=True)
        (out / "manifest.json").unlink(missing_ok=True)

        _write_wav(music_path, syn.to_int16_stereo(buf), syn.SAMPLE_RATE)

        sections_out = []
        for sec in meta["sections"]:
            lo = int(round(sec["start_s"] * syn.SAMPLE_RATE))
            hi = int(round(sec["end_s"] * syn.SAMPLE_RATE))
            peak_db, rms_db = syn.measure_db(buf[:, lo:hi])
            sections_out.append({**sec, "peak_dbfs": peak_db, "rms_dbfs": rms_db})
        peak_db, rms_db = syn.measure_db(buf)
        sample_count = int(buf.shape[1])

        beats_doc = {
            "schema_version": 1,
            "source": "chiptune.compose",
            "bpm": meta["bpm"],
            "key": meta["key"],
            "seed": int(args.seed),
            "duration_s": round(sample_count / syn.SAMPLE_RATE, 6),
            "sample_rate": syn.SAMPLE_RATE,
            "bar_s": meta["bar_s"],
            "bars_per_phrase": 4,
            "beats": meta["beats"],
            "downbeats": meta["downbeats"],
            "bars": meta["bars"],
            "sections": sections_out,
            "hits": [{"t": round(t, 6), "kind": kind} for t, kind in hit_events],
            "mutes": [[round(a, 6), round(b, 6)] for a, b in _parse_mutes(args.mutes)],
        }
        write_json_atomic(beats_path, beats_doc)

        created = datetime.now(timezone.utc).isoformat()
        manifest = build_manifest(
            kind="chiptune.compose",
            inputs={
                "duration_s": duration_s,
                "bpm": float(args.bpm),
                "key": args.key,
                "seed": int(args.seed),
                "sections": sections,
                "hits": hits,
                "duck_db": float(args.duck_db),
                "master_db": float(args.master_db),
                "style": args.style,
                "mutes": _parse_mutes(args.mutes),
            },
            outputs=[
                {
                    "name": "music",
                    "path": "music.wav",
                    "type": "file",
                    "artifact_type": "audio",
                    "role": "result",
                    "is_primary": True,
                    "media_type": "audio/wav",
                    "duration_seconds": round(sample_count / syn.SAMPLE_RATE, 6),
                },
                {
                    "name": "beats",
                    "path": "beats.json",
                    "type": "file",
                    "artifact_type": "application/json",
                    "role": "auxiliary",
                    "media_type": "application/json",
                },
            ],
            created=created,
            sample_rate=syn.SAMPLE_RATE,
            channels=2,
            peak_dbfs=peak_db,
            rms_dbfs=rms_db,
            sections=sections_out,
        )
        write_manifest(out / "manifest.json", manifest)
        return 0

    return run_pack_main("chiptune.compose", _run, argv=argv)


if __name__ == "__main__":
    raise SystemExit(main())
