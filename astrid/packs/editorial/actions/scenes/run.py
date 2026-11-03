#!/usr/bin/env python3
"""Detect scene boundaries from a source video and write scene timing data to JSON and CSV files for downstream shot and cut selection."""


from __future__ import annotations

from astrid.pack_api import guard_canonical_entrypoint

guard_canonical_entrypoint('editorial.scenes')
import argparse
import csv
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Sequence

from astrid.pack_api import build_manifest, write_manifest
from astrid.pack_api import require_runtime_materialized_file
from astrid.pack_api import register_outputs
from astrid.pack_api import ffprobe_duration_seconds


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Detect scene boundaries from a source video."
    )
    parser.add_argument("--video", type=str, required=True, help="Source video file.")
    parser.add_argument(
        "--out",
        type=Path,
        required=True,
        help="Output JSON path, or a directory where scenes.json should be written.",
    )
    parser.add_argument(
        "--threshold",
        type=float,
        default=27.0,
        help="PySceneDetect ContentDetector threshold.",
    )
    return parser


def resolve_output_paths(out_path: Path) -> tuple[Path, Path, Path]:
    resolved = out_path.resolve()
    if resolved.exists() and resolved.is_dir():
        json_path = resolved / "scenes.json"
    elif resolved.suffix.lower() == ".json":
        json_path = resolved
    else:
        json_path = resolved / "scenes.json"
    csv_path = json_path.with_name("scenes.csv")
    items_path = json_path.with_name("scene_items.json")
    return json_path, csv_path, items_path


def timecode_seconds(value: Any) -> float:
    if hasattr(value, "get_seconds"):
        return float(value.get_seconds())
    return float(value)


def probe_duration(video_path: Path) -> float:
    return ffprobe_duration_seconds(video_path)


def detect_scenes(video_path: Path, threshold: float) -> list[dict[str, float | int]]:
    try:
        from scenedetect import SceneManager, open_video
        from scenedetect.detectors import ContentDetector
    except ImportError as exc:
        duration_sec = probe_duration(video_path)
        if duration_sec <= 0:
            raise SystemExit(
                "scenedetect is unavailable and the video duration could not be probed."
            ) from exc
        return [{"index": 1, "start": 0.0, "end": duration_sec, "duration": duration_sec}]

    scene_manager = SceneManager()
    scene_manager.add_detector(ContentDetector(threshold=threshold))
    video = open_video(str(video_path))
    scene_manager.detect_scenes(video, show_progress=False)
    scene_list = scene_manager.get_scene_list(start_in_scene=True)

    scenes: list[dict[str, float | int]] = []
    for index, (start_time, end_time) in enumerate(scene_list, start=1):
        start_seconds = timecode_seconds(start_time)
        end_seconds = timecode_seconds(end_time)
        scenes.append(
            {
                "index": index,
                "start": start_seconds,
                "end": end_seconds,
                "duration": max(0.0, end_seconds - start_seconds),
            }
        )
    return scenes


def write_outputs(
    scenes: list[dict[str, float | int]],
    json_path: Path,
    csv_path: Path,
    items_path: Path | None = None,
) -> None:
    json_path.parent.mkdir(parents=True, exist_ok=True)
    items_path = items_path or json_path.with_name("scene_items.json")
    json_path.write_text(json.dumps(scenes, indent=2), encoding="utf-8")
    scene_items = [f"scene-{int(scene['index']):04d}" for scene in scenes]
    items_path.write_text(json.dumps(scene_items, indent=2), encoding="utf-8")

    with csv_path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.writer(handle)
        writer.writerow(["index", "start", "end", "duration"])
        for scene in scenes:
            writer.writerow(
                [scene["index"], scene["start"], scene["end"], scene["duration"]]
            )
    register_outputs(
        stage="scenes",
        outputs=[
            ("scenes", json_path, "Scene list"),
            ("scenes_csv", csv_path, "Scene CSV"),
            ("scene_items", items_path, "Scene item ids"),
        ],
        metadata={"scenes": len(scenes)},
    )


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    args.video = require_runtime_materialized_file(args.video, label="--video")

    video_path = args.video.resolve()
    if not video_path.is_file():
        raise SystemExit(f"Video file not found: {video_path}")

    json_path, csv_path, items_path = resolve_output_paths(args.out)
    scenes = detect_scenes(video_path, args.threshold)
    write_outputs(scenes, json_path, csv_path, items_path)

    # --- universal result manifest -----------------------
    output_specs = (
        ("scenes", json_path.name),
        ("scene_items", items_path.name),
    )
    manifest_outputs: list[dict[str, Any]] = []
    for ordinal, (name, relative_path) in enumerate(output_specs):
        manifest_outputs.append(
            {
                "name": name,
                "path": relative_path,
                "type": "file",
                "ordinal": ordinal,
                "role": "result",
                "is_primary": ordinal == 0,
            }
        )
    manifest_path = json_path.parent / "manifest.json"
    manifest = build_manifest(
        kind="scenes",
        inputs={
            "video": str(video_path),
            "threshold": args.threshold,
        },
        outputs=manifest_outputs,
        created=datetime.now(timezone.utc).isoformat(),
    )
    write_manifest(manifest_path, manifest)
    # -------------------------------------------------------------------------

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
