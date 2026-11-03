#!/usr/bin/env python3
"""Extract representative still frames for each scene from a source video and record their paths and timestamps in JSON output."""


from __future__ import annotations

from astrid.pack_api import guard_canonical_entrypoint

guard_canonical_entrypoint('editorial.shots')
import argparse
import json
import subprocess
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Sequence

from astrid.pack_api import build_manifest, write_manifest
from astrid.pack_api import require_runtime_materialized_file
from astrid.pack_api import register_outputs


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Extract representative still frames for each scene."
    )
    parser.add_argument("--video", type=str, required=True, help="Source video file.")
    parser.add_argument("--scenes", type=Path, required=True, help="Path to scenes.json.")
    parser.add_argument(
        "--out",
        type=Path,
        required=True,
        help="Directory where JPG frames and shots.json will be written.",
    )
    parser.add_argument(
        "--per-scene",
        type=int,
        default=3,
        help="Number of evenly spaced frames to extract for each scene.",
    )
    return parser


def load_scenes(scenes_path: Path) -> list[dict[str, Any]]:
    data = json.loads(scenes_path.read_text(encoding="utf-8"))
    if not isinstance(data, list):
        raise SystemExit(f"Expected a JSON list in {scenes_path}")
    return data


def frame_timestamps(start_sec: float, end_sec: float, per_scene: int) -> list[float]:
    if per_scene <= 0:
        raise SystemExit("--per-scene must be >= 1")
    duration = max(0.0, end_sec - start_sec)
    if duration == 0.0:
        return [start_sec for _ in range(per_scene)]
    step = duration / (per_scene + 1)
    return [round(start_sec + step * (index + 1), 6) for index in range(per_scene)]


def extract_frame(video_path: Path, timestamp: float, output_path: Path) -> None:
    subprocess.run(
        [
            "ffmpeg",
            "-y",
            "-ss",
            str(timestamp),
            "-i",
            str(video_path),
            "-frames:v",
            "1",
            "-q:v",
            "3",
            str(output_path),
        ],
        check=True,
        capture_output=True,
        text=True,
    )


def build_shots(
    video_path: Path, scenes: list[dict[str, Any]], out_dir: Path, per_scene: int
) -> list[dict[str, Any]]:
    out_dir.mkdir(parents=True, exist_ok=True)
    shots: list[dict[str, Any]] = []
    for scene in scenes:
        scene_index = int(scene["index"])
        timestamps = frame_timestamps(float(scene["start"]), float(scene["end"]), per_scene)
        frames: list[dict[str, Any]] = []
        for frame_index, timestamp in enumerate(timestamps, start=1):
            frame_path = out_dir / f"scene{scene_index:03d}_k{frame_index}.jpg"
            extract_frame(video_path, timestamp, frame_path)
            frames.append({"path": str(frame_path.resolve()), "timestamp": timestamp})
        shots.append({"scene_index": scene_index, "frames": frames})
    return shots


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    args.video = require_runtime_materialized_file(args.video, label="--video")

    video_path = args.video.resolve()
    scenes_path = args.scenes.resolve()
    out_dir = args.out.resolve()

    if not video_path.is_file():
        raise SystemExit(f"Video file not found: {video_path}")
    if not scenes_path.is_file():
        raise SystemExit(f"Scenes file not found: {scenes_path}")

    scenes = load_scenes(scenes_path)
    shots = build_shots(video_path, scenes, out_dir, args.per_scene)
    shots_path = out_dir / "shots.json"
    shots_path.write_text(json.dumps(shots, indent=2), encoding="utf-8")
    register_outputs(
        stage="shots",
        outputs=[("shots", shots_path, "Shot keyframes")],
        metadata={"scenes": len(shots), "frames": sum(len(item.get("frames", [])) for item in shots)},
    )
    print(f"wrote_shots={len(shots)} shots_json={shots_path}")

    # --- universal result manifest -----------------------
    manifest_outputs: list[dict[str, Any]] = [
        {"path": str(shots_path), "type": "file"},
    ]
    manifest_path = out_dir / "manifest.json"
    manifest = build_manifest(
        kind="shots",
        inputs={
            "video": str(video_path),
            "scenes": str(scenes_path),
        },
        outputs=manifest_outputs,
        created=datetime.now(timezone.utc).isoformat(),
    )
    write_manifest(manifest_path, manifest)
    # -------------------------------------------------------------------------

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
