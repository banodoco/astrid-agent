#!/usr/bin/env python3
"""Validate that a rendered hype.mp4 matches the captions in hype.metadata.json.


Runs a fresh Whisper transcription on the output video, then for each clip in
hype.timeline.json, compares the audio transcript in that clip's timeline range
against the `source_transcript_text` recorded in hype.metadata.json.

Usage:
  python validate.py --video runs/<id>/hype.mp4 [--threshold 0.5] [--env-file .env]

Writes validation.json next to the video with per-clip results and a summary.
Exits non-zero when any non-skipped clip fails the similarity threshold.
"""

from __future__ import annotations

from astrid.pack_api import guard_canonical_entrypoint

guard_canonical_entrypoint('editorial.validate')
import argparse
import json
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from astrid.pack_api import build_manifest, write_manifest
from astrid.pack_api import register_outputs
from astrid.pack_api import executor_argv
from astrid.pack_api import load_timeline
from astrid.packs.editorial.shared.hype.text_match import (
    segments_in_range,
    token_set_similarity,
    tokenize,
)


def clip_timeline_duration_sec(clip: dict[str, Any]) -> float:
    clip_type = clip.get("clipType")
    if clip_type in ("text", "hold"):
        hold = clip.get("hold")
        if isinstance(hold, (int, float)):
            return max(0.0, float(hold))
    # ``load_timeline`` returns the model's Python field name (``from_``),
    # while callers may also pass raw JSON clips containing the ``from`` alias.
    frm = clip.get("from")
    if frm is None:
        frm = clip.get("from_")
    to = clip.get("to")
    if isinstance(frm, (int, float)) and isinstance(to, (int, float)):
        speed = clip.get("speed") or 1.0
        try:
            speed = float(speed)
        except (TypeError, ValueError):
            speed = 1.0
        if speed <= 0:
            speed = 1.0
        return max(0.0, (float(to) - float(frm)) / speed)
    return 0.0

def joined_text(segments: list[dict]) -> str:
    return " ".join(str(seg.get("text", "")).strip() for seg in segments).strip()


def run_transcribe(video: Path, out_dir: Path, env_file: Path | None) -> Path:
    transcript_json = out_dir / "transcript.json"
    cmd = [
        *executor_argv("editorial.transcribe", sys.executable),
        "--audio",
        str(video),
        "--out",
        str(out_dir),
    ]
    if env_file is not None:
        cmd.extend(["--env-file", str(env_file)])
    print(f"Transcribing {video.name} -> {out_dir}/ ...", flush=True)
    result = subprocess.run(cmd, check=False)
    if result.returncode != 0:
        raise SystemExit(f"validate: transcribe.py exited {result.returncode}")
    if not transcript_json.is_file():
        raise SystemExit(f"validate: transcribe.py did not produce {transcript_json}")
    return transcript_json


def main() -> int:
    parser = argparse.ArgumentParser(description="Validate a rendered hype.mp4 against its metadata captions.")
    parser.add_argument("--video", type=Path, required=True, help="Rendered hype.mp4 to validate.")
    parser.add_argument("--timeline", type=Path, help="hype.timeline.json (defaults to <video-dir>/hype.timeline.json).")
    parser.add_argument("--metadata", type=Path, help="hype.metadata.json (defaults to <video-dir>/hype.metadata.json).")
    parser.add_argument("--out", type=Path, help="validation.json (defaults to <video-dir>/validation.json).")
    parser.add_argument("--env-file", dest="env_file", type=Path, help="Env file forwarded to transcribe.py.")
    parser.add_argument("--threshold", type=float, default=0.5, help="Token-overlap threshold for pass (0-1). Default 0.5.")
    parser.add_argument("--skip-transcribe", action="store_true", help="Reuse existing <video-dir>/_validate/transcript.json.")
    args = parser.parse_args()

    video = args.video.resolve()
    if not video.is_file():
        raise SystemExit(f"validate: video not found: {video}")
    video_dir = video.parent
    timeline_path = (args.timeline or (video_dir / "hype.timeline.json")).resolve()
    metadata_path = (args.metadata or (video_dir / "hype.metadata.json")).resolve()
    out_path = (args.out or (video_dir / "validation.json")).resolve()
    for required in (timeline_path, metadata_path):
        if not required.is_file():
            raise SystemExit(f"validate: required file not found: {required}")

    timeline = load_timeline(timeline_path)
    metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
    if not any(clip.get("track") == "a1" for clip in timeline.get("clips", [])):
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_text(
            json.dumps(
                {
                    "summary": {
                        "passes": 0,
                        "failures": 0,
                        "skipped": 0,
                        "skipped_visual": 0,
                        "skipped_no_audio": True,
                    },
                    "clips": [],
                },
                indent=2,
            )
            + "\n",
            encoding="utf-8",
        )
        register_outputs(
            stage="validate",
            outputs=[("validation", out_path, "Validation report")],
            metadata={"skipped_no_audio": True},
        )
        print("validate: skipped because timeline has no audio track")

        # --- universal result manifest -----------------------
        manifest_path = out_path.parent / "manifest.json"
        manifest = build_manifest(
            kind="validate",
            inputs={
                "video": str(video),
                "timeline": str(timeline_path),
                "metadata": str(metadata_path),
            },
            outputs=[
                {"path": out_path.name, "type": "file"},
            ],
            created=datetime.now(timezone.utc).isoformat(),
        )
        write_manifest(manifest_path, manifest)
        # -------------------------------------------------------------------------

        return 0

    validate_dir = video_dir / "_validate"
    transcript_path = validate_dir / "transcript.json"
    if not (args.skip_transcribe and transcript_path.is_file()):
        validate_dir.mkdir(parents=True, exist_ok=True)
        transcript_path = run_transcribe(video, validate_dir, args.env_file)

    transcript_raw = json.loads(transcript_path.read_text(encoding="utf-8"))
    segments = transcript_raw.get("segments") if isinstance(transcript_raw, dict) else transcript_raw
    if not isinstance(segments, list):
        raise SystemExit(f"validate: unexpected transcript shape in {transcript_path}")

    full_transcript_text = joined_text(segments).lower()

    clips_meta = metadata.get("clips", {})

    results: list[dict[str, Any]] = []
    passes = fails = skipped = skipped_visual = 0

    for clip in timeline.get("clips", []):
        clip_id = clip.get("id")
        start = float(clip.get("at", 0.0))
        end = start + clip_timeline_duration_sec(clip)
        clip_meta = (clips_meta.get(clip_id, {}) or {})
        caption_kind = clip_meta.get("caption_kind")
        expected = clip_meta.get("source_transcript_text")

        if caption_kind == "visual":
            skipped_visual += 1
            results.append({
                "clip_id": clip_id,
                "timeline_range": [round(start, 3), round(end, 3)],
                "status": "skipped-visual",
                "reason": "caption describes imagery, not dialogue",
            })
            continue

        if not expected:
            skipped += 1
            results.append({
                "clip_id": clip_id,
                "timeline_range": [round(start, 3), round(end, 3)],
                "status": "skipped",
                "reason": "no source_transcript_text in metadata",
            })
            continue

        segs = segments_in_range(segments, start, end)
        actual = joined_text(segs)
        similarity = token_set_similarity(expected, actual)
        # Global fallback: if the expected text shows up elsewhere in the cut,
        # flag that so low-similarity doesn't silently hide misaligned clips.
        expected_tokens = set(tokenize(expected))
        global_hit = bool(expected_tokens) and len(expected_tokens & set(tokenize(full_transcript_text))) / max(1, len(expected_tokens)) >= args.threshold
        status = "pass" if similarity >= args.threshold else "fail"
        if status == "pass":
            passes += 1
        else:
            fails += 1
        entry: dict[str, Any] = {
            "clip_id": clip_id,
            "timeline_range": [round(start, 3), round(end, 3)],
            "expected": expected,
            "actual": actual,
            "similarity": round(similarity, 3),
            "status": status,
        }
        if status == "fail" and global_hit:
            entry["note"] = "expected text appears elsewhere in final transcript; caption may be misaligned to the wrong clip range"
        elif status == "fail" and not global_hit and not expected_tokens:
            entry["note"] = "expected text has no word tokens (likely a visual description, not dialogue)"
        elif status == "fail" and not global_hit:
            entry["note"] = "expected text not present anywhere in final transcript; likely a visual-only caption or missing audio"
        results.append(entry)

    report = {
        "video": str(video),
        "timeline": str(timeline_path),
        "metadata": str(metadata_path),
        "summary": {
            "total": len(results),
            "passed": passes,
            "failed": fails,
            "skipped": skipped,
            "skipped_visual": skipped_visual,
            "threshold": args.threshold,
        },
        "clips": results,
    }
    out_path.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    register_outputs(
        stage="validate",
        outputs=[("validation", out_path, "Validation report")],
        metadata=report.get("summary", {}),
    )

    total_checked = passes + fails
    print(
        f"\nvalidate: {passes}/{total_checked} passed (threshold={args.threshold}); "
        f"{skipped} skipped; {skipped_visual} skipped-visual",
        flush=True,
    )
    print(f"validate: wrote {out_path}", flush=True)
    if fails:
        print("\nFailed clips:", flush=True)
        for entry in results:
            if entry.get("status") != "fail":
                continue
            note = f" -- {entry['note']}" if entry.get("note") else ""
            print(
                f"  {entry['clip_id']} [sim={entry['similarity']:.2f}] "
                f"expected={entry['expected'][:60]!r} actual={entry['actual'][:60]!r}{note}",
                flush=True,
            )

    # --- universal result manifest -----------------------
    manifest_path = out_path.parent / "manifest.json"
    manifest = build_manifest(
        kind="validate",
        inputs={
            "video": str(video),
            "timeline": str(timeline_path),
            "metadata": str(metadata_path),
        },
        outputs=[
            {"path": out_path.name, "type": "file"},
        ],
        created=datetime.now(timezone.utc).isoformat(),
    )
    write_manifest(manifest_path, manifest)
    # -------------------------------------------------------------------------

    return 0 if fails == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
