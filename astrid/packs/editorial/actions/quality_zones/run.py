#!/usr/bin/env python3
from __future__ import annotations

from astrid.pack_api import guard_canonical_entrypoint

guard_canonical_entrypoint('editorial.quality_zones')


import argparse
import hashlib
import json
import re
import subprocess
import sys
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Sequence

from astrid.pack_api import build_manifest, write_manifest
from astrid.pack_api import require_runtime_materialized_file
from astrid.pack_api import register_outputs
from astrid.packs.editorial.shared.hype import enriched_arrangement

sys.modules.setdefault("quality_zones", sys.modules[__name__])

_SILENCE_START_RE = re.compile(r"silence_start[:=]\s*([0-9.]+)")
_SILENCE_END_RE = re.compile(r"silence_end[:=]\s*([0-9.]+)")
_BLACK_RANGE_RE = re.compile(r"black_start[:=]\s*([0-9.]+).*black_end[:=]\s*([0-9.]+)")


@dataclass(slots=True)
class QualityZonesReport:
    source_sha256: str
    asset_key: str = "main"
    zones: list[enriched_arrangement.QualityZone] = field(default_factory=list)

    def to_payload(self) -> dict[str, object]:
        return {
            "source_sha256": self.source_sha256,
            "asset_key": self.asset_key,
            "zones": [
                {"kind": zone.kind.value, "start": zone.start, "end": zone.end}
                for zone in self.zones
            ],
        }


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Detect audio/video dead zones with ffmpeg.")
    parser.add_argument("source_path", type=str)
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--silence-db", type=float, default=-35.0)
    parser.add_argument("--silence-dur", type=float, default=0.5)
    parser.add_argument("--black-dur", type=float, default=0.3)
    parser.add_argument("--black-pic-th", type=float, default=0.98)
    return parser


def compute(
    source_path: Path,
    *,
    silence_db: float = -35.0,
    silence_dur: float = 0.5,
    black_dur: float = 0.3,
    black_pic_th: float = 0.98,
    source_sha256: str | None = None,
) -> QualityZonesReport:
    source_path = source_path.resolve()
    # SD3 — accept a pre-computed SHA from the caller to close the TOCTOU
    # window: if the caller already hashed the file (e.g. main() at the top
    # of the entrypoint), re-hashing here would let in-flight file mutations
    # silently shift the recorded cache key. ffmpeg still reads the on-disk
    # Path below — that's unavoidable — but the recorded source_sha256 must
    # match the caller's single source of truth.
    if source_sha256 is None:
        source_sha256 = hashlib.sha256(source_path.read_bytes()).hexdigest()
    audio_logs = _run_ffmpeg(
        [
            "ffmpeg",
            "-hide_banner",
            "-i",
            str(source_path),
            "-af",
            f"silencedetect=n={silence_db}dB:d={silence_dur}",
            "-f",
            "null",
            "-",
        ]
    )
    video_logs = _run_ffmpeg(
        [
            "ffmpeg",
            "-hide_banner",
            "-i",
            str(source_path),
            "-vf",
            f"blackdetect=d={black_dur}:pic_th={black_pic_th}",
            "-f",
            "null",
            "-",
        ]
    )
    zones = _parse_ranges(audio_logs, enriched_arrangement.ZoneKind.AUDIO_DEAD)
    zones.extend(_parse_black_ranges(video_logs))
    zones.sort(key=lambda zone: (zone.start, zone.end, zone.kind.value))
    return QualityZonesReport(source_sha256=source_sha256, zones=zones)


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    args.source_path = require_runtime_materialized_file(args.source_path, label="source path")
    source_path = args.source_path.resolve()
    out_path = args.out.resolve()
    if not source_path.is_file():
        raise SystemExit(f"Source file not found: {source_path}")
    if out_path.exists() and out_path.is_dir():
        raise SystemExit(f"--out must be a file path, got directory: {out_path}")

    source_sha256 = hashlib.sha256(source_path.read_bytes()).hexdigest()
    cached = _load_cached_payload(out_path, source_sha256)
    payload = cached or compute(
        source_path,
        silence_db=args.silence_db,
        silence_dur=args.silence_dur,
        black_dur=args.black_dur,
        black_pic_th=args.black_pic_th,
        source_sha256=source_sha256,
    ).to_payload()
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
    register_outputs(
        stage="quality_zones",
        outputs=[("quality_zones", out_path, "Quality zones")],
        metadata={"zones": len(payload.get("zones", [])), "cached": cached is not None},
    )
    print(f"zones={len(payload['zones'])} cached={str(cached is not None).lower()} out={out_path}")

    # --- universal result manifest -----------------------
    manifest_path = out_path.parent / "manifest.json"
    manifest = build_manifest(
        kind="quality_zones",
        inputs={
            "source_path": str(source_path),
        },
        outputs=[
            {"path": out_path.name, "type": "file"},
        ],
        created=datetime.now(timezone.utc).isoformat(),
    )
    write_manifest(manifest_path, manifest)
    # -------------------------------------------------------------------------

    return 0


def _load_cached_payload(out_path: Path, source_sha256: str) -> dict[str, object] | None:
    if not out_path.is_file():
        return None
    try:
        payload = json.loads(out_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return None
    if not isinstance(payload, dict):
        return None
    if payload.get("source_sha256") != source_sha256:
        return None
    zones = payload.get("zones")
    if payload.get("asset_key") != "main" or not isinstance(zones, list):
        return None
    return {"source_sha256": source_sha256, "asset_key": "main", "zones": list(zones)}


def _run_ffmpeg(command: list[str]) -> str:
    try:
        result = subprocess.run(command, capture_output=True, text=True, check=True)
    except FileNotFoundError as exc:
        raise SystemExit("ffmpeg is required for quality_zones.py but was not found on PATH") from exc
    except subprocess.CalledProcessError as exc:
        raise SystemExit(exc.stderr.strip() or "ffmpeg failed while detecting quality zones") from exc
    return result.stderr


def _parse_ranges(
    stderr_text: str, kind: enriched_arrangement.ZoneKind
) -> list[enriched_arrangement.QualityZone]:
    starts = [float(match.group(1)) for match in _SILENCE_START_RE.finditer(stderr_text)]
    ends = [float(match.group(1)) for match in _SILENCE_END_RE.finditer(stderr_text)]
    return [
        enriched_arrangement.QualityZone(kind=kind, start=start, end=end)
        for start, end in zip(starts, ends)
        if end > start
    ]


def _parse_black_ranges(stderr_text: str) -> list[enriched_arrangement.QualityZone]:
    zones: list[enriched_arrangement.QualityZone] = []
    for line in stderr_text.splitlines():
        match = _BLACK_RANGE_RE.search(line)
        if not match:
            continue
        start, end = float(match.group(1)), float(match.group(2))
        if end <= start:
            continue
        zones.append(
            enriched_arrangement.QualityZone(
                kind=enriched_arrangement.ZoneKind.VIDEO_DEAD,
                start=start,
                end=end,
            )
        )
    return zones


if __name__ == "__main__":
    raise SystemExit(main())
