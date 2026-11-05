#!/usr/bin/env python3
"""Deterministic pool.json construction from triage, descriptions, and quotes."""


from __future__ import annotations

from astrid.pack_api import AstridError, AstridValueError
from astrid.pack_api import guard_canonical_entrypoint, run_pack_main

guard_canonical_entrypoint('training.pool_build')
import argparse
import json
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Sequence

from astrid.pack_api import POOL_VERSION, Pool, save_pool, validate_pool
from astrid.pack_api import build_manifest, write_manifest
from astrid.pack_api import register_outputs
from astrid.pack_api import utc_now_seconds

AUDIO_EVENT_RE = re.compile(r"\b(applause|laughter|cheer|audience)\b", re.IGNORECASE)
KIND_LETTER = {"dialogue": "d", "visual": "v", "reaction": "r", "applause": "a", "music": "m"}


def scene_id_for(scene: dict[str, Any]) -> str:
    return f"scene_{int(scene['index']):03d}"


def _entries_by_id(payload: dict[str, Any], key: str) -> dict[str, dict[str, Any]]:
    if "entries" in payload:
        entries = payload.get("entries")
    else:
        entries = payload.get("candidates")
    if not isinstance(entries, list):
        raise AstridValueError(f"{key} payload must contain a list")
    mapping: dict[str, dict[str, Any]] = {}
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        entry_id = entry.get(key)
        if isinstance(entry_id, str):
            mapping[entry_id] = entry
    return mapping


def _segments(transcript: dict[str, Any] | list[dict[str, Any]]) -> list[dict[str, Any]]:
    segments = transcript.get("segments") if isinstance(transcript, dict) else transcript
    if not isinstance(segments, list):
        raise AstridValueError("transcript must be a list or an object with segments")
    return segments


def _next_pool_id(kind: str, counters: dict[str, int]) -> str:
    counters[kind] = counters.get(kind, 0) + 1
    return f"pool_{KIND_LETTER[kind]}_{counters[kind]:04d}"


def build_pool(
    triage: dict[str, Any],
    scene_descriptions: dict[str, Any],
    quote_candidates: dict[str, Any],
    transcript: dict[str, Any] | list[dict[str, Any]],
    scenes: list[dict[str, Any]],
    *,
    source_slug: str,
) -> dict[str, Any]:
    triage_by_scene = _entries_by_id(triage, "scene_id")
    descriptions_by_scene = _entries_by_id(scene_descriptions, "scene_id")
    transcript_segments = _segments(transcript)
    quote_rows = quote_candidates.get("candidates")
    if not isinstance(quote_rows, list):
        raise AstridValueError("quote_candidates must contain candidates")

    counters: dict[str, int] = {}
    entries: list[dict[str, Any]] = []

    for scene in scenes:
        scene_id = scene_id_for(scene)
        triage_entry = triage_by_scene.get(scene_id)
        if not triage_entry or int(triage_entry.get("triage_score", 0)) <= 0:
            continue
        deep_entry = descriptions_by_scene.get(scene_id, {})
        duration = float(scene["duration"])
        excluded = duration < 0.8 or duration > 6.0
        entries.append(
            {
                "id": _next_pool_id("visual", counters),
                "kind": "source",
                "category": "visual",
                "asset": "main",
                "src_start": float(scene["start"]),
                "src_end": float(scene["end"]),
                "duration": duration,
                "source_ids": {"scene_id": scene_id},
                "scores": {
                    "triage": float(triage_entry["triage_score"]) / 5.0,
                    **({"deep": float(deep_entry["deep_score"])} if "deep_score" in deep_entry else {}),
                },
                "excluded": excluded,
                **({"excluded_reason": "duration_out_of_window"} if excluded else {}),
                "motion_tags": list(deep_entry.get("motion_tags", [])),
                "mood_tags": list(deep_entry.get("mood_tags", [])),
                "subject": str(deep_entry.get("description", scene_id)),
                "camera": str(deep_entry.get("mood", triage_entry.get("triage_tag", "unknown"))),
            }
        )

    for candidate in quote_rows:
        if not isinstance(candidate, dict):
            continue
        segment_ids = candidate.get("segment_ids")
        if not isinstance(segment_ids, list) or not segment_ids:
            continue
        segment_start = min(float(transcript_segments[index]["start"]) for index in segment_ids)
        segment_end = max(float(transcript_segments[index]["end"]) for index in segment_ids)
        entries.append(
            {
                "id": _next_pool_id("dialogue", counters),
                "kind": "source",
                "category": "dialogue",
                "asset": "main",
                "src_start": segment_start,
                "src_end": segment_end,
                "duration": round(segment_end - segment_start, 6),
                "source_ids": {"segment_ids": list(segment_ids)},
                "scores": {"quotability": float(candidate["power"]) / 5.0},
                "excluded": False,
                "text": str(candidate["text"]),
                "speaker": candidate.get("speaker"),
                "quote_kind": str(candidate["quote_kind"]),
            }
        )

    for index, segment in enumerate(transcript_segments):
        text = str(segment.get("text", ""))
        match = AUDIO_EVENT_RE.search(text)
        if not match:
            continue
        token = match.group(1).lower()
        kind = "applause" if token == "applause" else "reaction"
        start_sec = float(segment["start"])
        end_sec = float(segment["end"])
        entries.append(
            {
                "id": _next_pool_id(kind, counters),
                "kind": "source",
                "category": kind,
                "asset": "main",
                "src_start": start_sec,
                "src_end": end_sec,
                "duration": round(end_sec - start_sec, 6),
                "source_ids": {"segment_ids": [index]},
                "scores": {},
                "excluded": False,
                "intensity": 1.0,
                "event_label": token,
            }
        )

    visual_survivors = [entry for entry in entries if entry["category"] == "visual" and not entry["excluded"]]
    dialogue_survivors = [entry for entry in entries if entry["category"] == "dialogue" and not entry["excluded"]]
    if not visual_survivors or not dialogue_survivors:
        raise AstridError(
            "pool_build requires at least one surviving visual and one surviving dialogue entry",
            recovery_command=(
                "re-run triage/scene_describe/quote_scout so at least one visual and one dialogue entry survive"
            ),
        )

    payload: Pool = {
        "version": POOL_VERSION,
        "generated_at": utc_now_seconds(),
        "source_slug": source_slug,
        "entries": entries,
    }
    validate_pool(payload)
    return payload


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Build deterministic pool.json from triage, descriptions, and quotes.")
    parser.add_argument("--triage", type=Path, required=True)
    parser.add_argument("--scene-descriptions", type=Path, required=True)
    parser.add_argument("--quote-candidates", type=Path, required=True)
    parser.add_argument("--transcript", type=Path, required=True)
    parser.add_argument("--scenes", type=Path, required=True)
    parser.add_argument("--source-slug", required=True)
    parser.add_argument("--out", type=Path, required=True)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    def _run() -> int:
        parser = build_parser()
        args = parser.parse_args(argv)
        triage = json.loads(args.triage.read_text(encoding="utf-8"))
        scene_descriptions = json.loads(args.scene_descriptions.read_text(encoding="utf-8"))
        quote_candidates = json.loads(args.quote_candidates.read_text(encoding="utf-8"))
        transcript = json.loads(args.transcript.read_text(encoding="utf-8"))
        scenes = json.loads(args.scenes.read_text(encoding="utf-8"))
        payload = build_pool(
            triage,
            scene_descriptions,
            quote_candidates,
            transcript,
            scenes,
            source_slug=args.source_slug,
        )
        out_dir = args.out.resolve()
        out_dir.mkdir(parents=True, exist_ok=True)
        out_path = out_dir / "pool.json"
        save_pool(payload, out_path)
        register_outputs(
            stage="pool_build",
            outputs=[("pool", out_path, "Candidate pool")],
            metadata={"entries": len(payload.get("entries", [])), "source_slug": args.source_slug},
        )
        print(out_path)

        # --- universal result manifest -----------------------
        manifest_outputs: list[dict[str, Any]] = [
            {"path": str(out_path), "type": "file"},
        ]
        manifest_path = out_dir / "manifest.json"
        manifest = build_manifest(
            kind="pool",
            inputs={
                "triage": str(args.triage.resolve()),
                "scene_descriptions": str(args.scene_descriptions.resolve()),
                "quote_candidates": str(args.quote_candidates.resolve()),
                "transcript": str(args.transcript.resolve()),
                "scenes": str(args.scenes.resolve()),
                "source_slug": args.source_slug,
            },
            outputs=manifest_outputs,
            created=datetime.now(timezone.utc).isoformat(),
            schema_version=1,
            warnings=[],
        )
        write_manifest(manifest_path, manifest)
        # -------------------------------------------------------------------------

        return 0

    return run_pack_main("training.pool_build", _run, argv=list(argv) if argv is not None else None)


if __name__ == "__main__":
    raise SystemExit(main())
