#!/usr/bin/env python3
"""Render retro one-shot sound effects (chiptune.sfx), singly or as a batch zip.

Invoked by the Astrid runtime from the pack's V3 action declaration. The synthesis
lives in ``astrid.packs.chiptune.actions._synth``.
"""

from __future__ import annotations

from astrid.pack_api import guard_canonical_entrypoint, run_pack_main

guard_canonical_entrypoint("chiptune.sfx")

import argparse
import io
import json
import wave
import zipfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from astrid.pack_api import build_manifest, write_json_atomic, write_manifest
from astrid.pack_api import AstridError
from astrid.packs.chiptune.actions import _synth as syn

_ZIP_EPOCH = (1980, 1, 1, 0, 0, 0)  # fixed zip timestamps keep batch archives byte-identical


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="chiptune.sfx", description="Render chiptune sound effects.")
    parser.add_argument("--kind", default="blip", help="One of: " + ", ".join(syn.SFX_KINDS))
    parser.add_argument("--kinds", default=None, help="Batch mode: comma-separated kinds, zipped into one artifact.")
    parser.add_argument("--variant", type=int, default=1, help="Variant seed; the same seed gives identical bytes.")
    parser.add_argument("--pitch", type=float, default=0.0, help="Semitones relative to A4 (440 Hz).")
    parser.add_argument("--duration-s", type=float, default=None, help="Exact length; default is per kind.")
    parser.add_argument("--out", type=Path, required=True, help="Output directory.")
    return parser


def _parse_kinds(raw: str) -> list[str]:
    text = raw.strip()
    if text.startswith("["):
        parsed = syn.parse_structured(text, "kinds")
        if not isinstance(parsed, list):
            raise AstridError("kinds must be a list of sfx kinds")
        items = [str(item) for item in parsed]
    else:
        items = [part.strip() for part in text.split(",")]
    kinds = [item for item in items if item]
    if not kinds:
        raise AstridError("kinds must name at least one sfx kind", valid_options=list(syn.SFX_KINDS))
    return kinds


def _wav_bytes(pcm: Any) -> bytes:
    buffer = io.BytesIO()
    with wave.open(buffer, "wb") as handle:
        handle.setnchannels(1)
        handle.setsampwidth(2)
        handle.setframerate(syn.SAMPLE_RATE)
        handle.writeframes(pcm.tobytes())
    return buffer.getvalue()


def _metadata(kind: str, variant: int, pitch: float, samples: Any) -> dict[str, Any]:
    peak_db, rms_db = syn.measure_db(samples)
    return {
        "schema_version": 1,
        "source": "chiptune.sfx",
        "kind": kind,
        "variant": variant,
        "pitch_semitones": pitch,
        "sample_rate": syn.SAMPLE_RATE,
        "channels": 1,
        "bit_depth": 16,
        "sample_count": int(samples.shape[0]),
        "duration_s": round(samples.shape[0] / syn.SAMPLE_RATE, 6),
        "peak_dbfs": peak_db,
        "rms_dbfs": rms_db,
    }


def main(argv: list[str] | None = None) -> int:
    def _run() -> int:
        args = build_parser().parse_args(argv)
        out = args.out.expanduser().resolve()
        out.mkdir(parents=True, exist_ok=True)
        for stale in ("sfx.wav", "sfx.json", "sfx-batch.zip", "sfx-batch.json", "manifest.json"):
            (out / stale).unlink(missing_ok=True)
        variant = int(args.variant)
        pitch = float(args.pitch)
        created = datetime.now(timezone.utc).isoformat()

        if args.kinds:
            kinds = _parse_kinds(args.kinds)
            for kind in kinds:
                if kind not in syn.SFX_KINDS:
                    raise AstridError(f"unknown sfx kind {kind!r}", valid_options=list(syn.SFX_KINDS))
            zip_path = out / "sfx-batch.zip"
            index: list[dict[str, Any]] = []
            with zipfile.ZipFile(zip_path, "w", compression=zipfile.ZIP_DEFLATED) as archive:
                for position, kind in enumerate(kinds):
                    samples = syn.render_sfx(kind, variant=variant, pitch=pitch, duration_s=args.duration_s)
                    stem = f"{position:02d}-{kind}-v{variant}"
                    meta = _metadata(kind, variant, pitch, samples)
                    meta["file"] = f"{stem}.wav"
                    index.append(meta)
                    info_wav = zipfile.ZipInfo(f"{stem}.wav", date_time=_ZIP_EPOCH)
                    archive.writestr(info_wav, _wav_bytes(syn.to_int16_mono(samples)))
                    info_json = zipfile.ZipInfo(f"{stem}.json", date_time=_ZIP_EPOCH)
                    archive.writestr(info_json, json.dumps(meta, indent=2, sort_keys=True) + "\n")
            index_path = out / "sfx-batch.json"
            write_json_atomic(index_path, {"schema_version": 1, "source": "chiptune.sfx", "kinds": kinds, "items": index})
            manifest = build_manifest(
                kind="chiptune.sfx",
                inputs={"kinds": kinds, "variant": variant, "pitch": pitch, "duration_s": args.duration_s},
                outputs=[
                    {
                        "name": "sfx_batch",
                        "path": "sfx-batch.zip",
                        "type": "file",
                        "role": "result",
                        "is_primary": True,
                        "media_type": "application/zip",
                        "label": f"{len(kinds)} chiptune sfx",
                    },
                    {"name": "sfx_meta", "path": "sfx-batch.json", "type": "file", "role": "auxiliary", "media_type": "application/json"},
                ],
                created=created,
                kinds=kinds,
                items=index,
            )
            write_manifest(out / "manifest.json", manifest)
            return 0

        kind = args.kind.strip()
        samples = syn.render_sfx(kind, variant=variant, pitch=pitch, duration_s=args.duration_s)
        wav_path = out / "sfx.wav"
        wav_path.write_bytes(_wav_bytes(syn.to_int16_mono(samples)))
        meta = _metadata(kind, variant, pitch, samples)
        meta["file"] = "sfx.wav"
        write_json_atomic(out / "sfx.json", meta)
        manifest = build_manifest(
            kind="chiptune.sfx",
            inputs={"kind": kind, "variant": variant, "pitch": pitch, "duration_s": args.duration_s},
            outputs=[
                {
                    "name": "sfx",
                    "path": "sfx.wav",
                    "type": "file",
                    "artifact_type": "audio",
                    "role": "result",
                    "is_primary": True,
                    "media_type": "audio/wav",
                    "duration_seconds": meta["duration_s"],
                },
                {"name": "sfx_meta", "path": "sfx.json", "type": "file", "role": "auxiliary", "media_type": "application/json"},
            ],
            created=created,
            kind_name=kind,
            peak_dbfs=meta["peak_dbfs"],
            rms_dbfs=meta["rms_dbfs"],
        )
        write_manifest(out / "manifest.json", manifest)
        return 0

    return run_pack_main("chiptune.sfx", _run, argv=argv)


if __name__ == "__main__":
    raise SystemExit(main())
