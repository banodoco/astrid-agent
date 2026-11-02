#!/usr/bin/env python3
"""Build standardized visual-review candidate frame sets around event talk boundaries."""


from __future__ import annotations

from astrid.pack_api import guard_canonical_entrypoint

guard_canonical_entrypoint('editorial.boundary_candidates')
import argparse
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Sequence

from astrid.pack_api import build_manifest, write_manifest
from astrid.pack_api import add_choice_arg

VERSION = 1


from astrid.pack_api import pack_die as _die


def _fmt_time(seconds: float) -> str:
    whole = int(seconds)
    h, rem = divmod(whole, 3600)
    m, s = divmod(rem, 60)
    if h:
        return f"{h:02d}:{m:02d}:{s:02d}"
    return f"{m:02d}:{s:02d}"


def _load_json(path: Path | None, fallback: Any) -> Any:
    if path is None:
        return fallback
    if not path.is_file():
        _die(f"file not found: {path}")
    return json.loads(path.read_text(encoding="utf-8"))


def _load_talks(path: Path) -> list[dict[str, Any]]:
    data = _load_json(path, {})
    talks = data.get("talks") if isinstance(data, dict) else None
    if not isinstance(talks, list):
        _die(f"manifest must contain talks[]: {path}")
    return [talk for talk in talks if isinstance(talk, dict)]


def _time_key(seconds: float) -> float:
    return round(float(seconds), 3)


def _add_candidate(
    candidates: dict[float, dict[str, Any]],
    seconds: float,
    *,
    reason: str,
    note: str | None = None,
    scene_index: int | None = None,
) -> None:
    key = _time_key(seconds)
    entry = candidates.setdefault(
        key,
        {
            "time": key,
            "timecode": _fmt_time(key),
            "reasons": [],
        },
    )
    if reason not in entry["reasons"]:
        entry["reasons"].append(reason)
    if note and "note" not in entry:
        entry["note"] = note.strip()
    if scene_index is not None:
        entry["scene_index"] = scene_index


def _transcript_segments_near(segments: list[dict[str, Any]], target: float, window: float) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    start = target - window
    end = target + window
    for segment in segments:
        seg_start = float(segment.get("start", 0.0))
        seg_end = float(segment.get("end", seg_start))
        text = str(segment.get("text", "")).strip()
        if not text or text == "...":
            continue
        if seg_end >= start and seg_start <= end:
            out.append(segment)
    return out


def _scenes_near(scenes: list[dict[str, Any]], target: float, window: float) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    start = target - window
    end = target + window
    for scene in scenes:
        scene_start = float(scene.get("start", 0.0))
        scene_end = float(scene.get("end", scene_start))
        if start <= scene_start <= end or start <= scene_end <= end or (scene_start <= target <= scene_end):
            out.append(scene)
    return out


def _holding_near(intervals: list[dict[str, Any]], target: float, window: float) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    start = target - window
    end = target + window
    for interval in intervals:
        hold_start = float(interval.get("start", 0.0))
        hold_end = float(interval.get("end", hold_start))
        if hold_end >= start and hold_start <= end:
            out.append(interval)
    return out


def _candidate_set(
    *,
    talk: dict[str, Any],
    kind: str,
    target: float,
    video: Path,
    transcript_segments: list[dict[str, Any]],
    scenes: list[dict[str, Any]],
    holding_intervals: list[dict[str, Any]],
    window: float,
    max_candidates: int,
) -> dict[str, Any]:
    candidates: dict[float, dict[str, Any]] = {}
    for offset in (-30.0, -15.0, -5.0, 0.0, 5.0, 15.0, 30.0):
        seconds = target + offset
        if seconds >= 0:
            _add_candidate(candidates, seconds, reason="manifest_window")

    for segment in _transcript_segments_near(transcript_segments, target, window):
        text = str(segment.get("text", "")).strip()
        _add_candidate(candidates, float(segment.get("start", 0.0)), reason="transcript_start", note=text)
        _add_candidate(candidates, float(segment.get("end", segment.get("start", 0.0))), reason="transcript_end", note=text)

    for scene in _scenes_near(scenes, target, window):
        scene_index = int(scene.get("index", 0))
        _add_candidate(candidates, float(scene.get("start", 0.0)), reason="scene_start", scene_index=scene_index)
        _add_candidate(candidates, float(scene.get("end", scene.get("start", 0.0))), reason="scene_end", scene_index=scene_index)

    for interval in _holding_near(holding_intervals, target, window):
        label = ",".join(str(item) for item in interval.get("matched", [])) or "holding_screen"
        _add_candidate(candidates, float(interval.get("start", 0.0)), reason="holding_start", note=label)
        _add_candidate(candidates, float(interval.get("end", interval.get("start", 0.0))), reason="holding_end", note=label)

    ordered = sorted(candidates.values(), key=lambda item: (abs(float(item["time"]) - target), float(item["time"])))
    selected = sorted(ordered[:max_candidates], key=lambda item: float(item["time"]))
    at_arg = ",".join(str(item["time"]) for item in selected)
    speaker = str(talk.get("speaker", "talk"))
    title = str(talk.get("title", ""))
    query_kind = "first talk-content frame to keep" if kind == "start" else "last talk-content frame to keep"
    query = (
        f"These are candidate {kind} boundary frames for {speaker} - {title}. "
        f"Identify pre/post talk material and recommend the {query_kind}. "
        "Return frame numbers only, with brief reasoning."
    )
    return {
        "id": f"{str(talk.get('slug') or speaker).strip()}:{kind}",
        "talk": {"speaker": speaker, "title": title, "slug": talk.get("slug")},
        "kind": kind,
        "target": round(target, 3),
        "target_timecode": _fmt_time(target),
        "candidates": selected,
        "visual_understand": {
            "command": [
                "python3",
                "visual_understand.py",
                "--video",
                str(video),
                "--at",
                at_arg,
                "--query",
                query,
            ]
        },
    }


def build_payload(args: argparse.Namespace) -> dict[str, Any]:
    talks = _load_talks(args.manifest)
    transcript = _load_json(args.transcript, {})
    segments = transcript.get("segments") if isinstance(transcript, dict) else []
    if not isinstance(segments, list):
        segments = []
    scenes = _load_json(args.scenes, [])
    if not isinstance(scenes, list):
        scenes = []
    holding = _load_json(args.holding_screens, {})
    intervals = holding.get("intervals") if isinstance(holding, dict) else []
    if not isinstance(intervals, list):
        intervals = []

    boundaries: list[dict[str, Any]] = []
    for talk in talks:
        if args.kind in ("start", "both") and talk.get("start") is not None:
            boundaries.append(
                _candidate_set(
                    talk=talk,
                    kind="start",
                    target=float(talk["start"]),
                    video=args.video,
                    transcript_segments=segments,
                    scenes=scenes,
                    holding_intervals=intervals,
                    window=args.window,
                    max_candidates=args.max_candidates,
                )
            )
        if args.kind in ("end", "both") and talk.get("end") is not None:
            boundaries.append(
                _candidate_set(
                    talk=talk,
                    kind="end",
                    target=float(talk["end"]),
                    video=args.video,
                    transcript_segments=segments,
                    scenes=scenes,
                    holding_intervals=intervals,
                    window=args.window,
                    max_candidates=args.max_candidates,
                )
            )
    return {
        "version": VERSION,
        "video": str(args.video),
        "asset_key": args.asset_key,
        "manifest": str(args.manifest),
        "asset_analysis": {
            "asset_key": args.asset_key,
            "transcript": str(args.transcript) if args.transcript else None,
            "scenes": str(args.scenes) if args.scenes else None,
            "shots": str(args.shots) if args.shots else None,
            "quality_zones": str(args.quality_zones) if args.quality_zones else None,
            "holding_screens": str(args.holding_screens) if args.holding_screens else None,
        },
        "metadata_source_refs": {
            args.asset_key: {
                key: value
                for key, value in {
                    "transcript_ref": str(args.transcript) if args.transcript else None,
                    "scenes_ref": str(args.scenes) if args.scenes else None,
                    "shots_ref": str(args.shots) if args.shots else None,
                    "quality_zones_ref": str(args.quality_zones) if args.quality_zones else None,
                    "holding_screens_ref": str(args.holding_screens) if args.holding_screens else None,
                    "boundary_candidates_ref": str(args.out),
                }.items()
                if value is not None
            }
        },
        "window": args.window,
        "max_candidates": args.max_candidates,
        "boundaries": boundaries,
    }


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Package candidate frames for visual boundary review.")
    parser.add_argument("--video", type=Path, required=True)
    parser.add_argument("--manifest", type=Path, required=True)
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--asset-key", default="main")
    parser.add_argument("--transcript", type=Path)
    parser.add_argument("--scenes", type=Path)
    parser.add_argument("--shots", type=Path)
    parser.add_argument("--quality-zones", type=Path)
    parser.add_argument("--holding-screens", type=Path)
    add_choice_arg(parser, "--kind", values=("start", "end", "both"), default="both")
    parser.add_argument("--window", type=float, default=45.0)
    parser.add_argument("--max-candidates", type=int, default=16)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.max_candidates < 1 or args.max_candidates > 20:
        _die("--max-candidates must be between 1 and 20")
    payload = build_payload(args)
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
    print(f"wrote={args.out} boundaries={len(payload['boundaries'])}")
    for boundary in payload["boundaries"][:5]:
        print(f"{boundary['id']} candidates={len(boundary['candidates'])}")

    # --- universal result manifest -----------------------
    manifest_path = args.out.parent / "manifest.json"
    manifest = build_manifest(
        kind="boundary_candidates",
        inputs={
            "video": str(args.video),
            "manifest": str(args.manifest.resolve()),
        },
        outputs=[
            {"path": args.out.name, "type": "file"},
        ],
        created=datetime.now(timezone.utc).isoformat(),
    )
    write_manifest(manifest_path, manifest)
    # -------------------------------------------------------------------------

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
