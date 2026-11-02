#!/usr/bin/env python3
"""Claude-powered first-pass scene triage over keyframe batches."""


from __future__ import annotations

from astrid.pack_api import AstridError
from astrid.pack_api import guard_canonical_entrypoint

guard_canonical_entrypoint('editorial.triage')
import argparse
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Sequence

from astrid.pack_api import build_manifest, write_manifest
from astrid.pack_api import register_outputs
from astrid.pack_api import ClaudeClient, build_claude_client
from astrid.pack_api import utc_now_seconds

TRIAGE_VERSION = 1
FORBIDDEN_TIME_KEYS = frozenset({"start", "end", "timestamp", "seconds", "time", "src_start", "src_end", "from", "to", "at"})
RESPONSE_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "properties": {
        "entries": {
            "type": "array",
            "items": {
                "type": "object",
                "additionalProperties": False,
                "properties": {
                    "scene_id": {"type": "string"},
                    "triage_score": {"type": "integer", "minimum": 1, "maximum": 5},
                    "triage_tag": {"type": "string"},
                },
                "required": ["scene_id", "triage_score", "triage_tag"],
            },
        }
    },
    "required": ["entries"],
}
SYSTEM_PROMPT = (
    "You are triaging source-video scenes for later pool construction. "
    "Rate only the provided scene_ids. Never return timestamps or numeric ranges."
)


def scene_id_for(scene: dict[str, Any]) -> str:
    return f"scene_{int(scene['index']):03d}"


def validate_scene_triage(payload: Any) -> None:
    if not isinstance(payload, dict):
        raise AstridError(
            "scene_triage payload must be an object",
            recovery_command="provide a valid JSON object as the scene_triage payload",
        )
    if payload.get("version") != TRIAGE_VERSION:
        raise AstridError(
            f"scene_triage.version must be {TRIAGE_VERSION}",
            valid_options=[str(TRIAGE_VERSION)],
            recovery_command=f"set version to {TRIAGE_VERSION} in the scene_triage payload",
        )
    generated_at = payload.get("generated_at")
    if not isinstance(generated_at, str) or not generated_at.endswith("Z"):
        raise AstridError(
            "scene_triage.generated_at must be a UTC timestamp ending in 'Z'",
            recovery_command="provide a UTC timestamp ending in 'Z' for generated_at",
        )
    entries = payload.get("entries")
    if not isinstance(entries, list):
        raise AstridError(
            "scene_triage.entries must be a list",
            recovery_command="provide entries as a JSON array",
        )
    seen_ids: set[str] = set()
    for index, entry in enumerate(entries):
        path = f"scene_triage.entries[{index}]"
        if not isinstance(entry, dict):
            raise AstridError(
                f"{path} must be an object",
                recovery_command=f"make {path} a JSON object with scene_id, triage_score, triage_tag",
            )
        if set(entry) != {"scene_id", "triage_score", "triage_tag"}:
            raise AstridError(
                f"{path} has unexpected keys",
                valid_options=["scene_id", "triage_score", "triage_tag"],
                recovery_command=f"ensure {path} has only the keys: scene_id, triage_score, triage_tag",
            )
        scene_id = entry.get("scene_id")
        triage_score = entry.get("triage_score")
        triage_tag = entry.get("triage_tag")
        if not isinstance(scene_id, str) or not scene_id:
            raise AstridError(
                f"{path}.scene_id must be a non-empty string",
                recovery_command=f"provide a non-empty string for {path}.scene_id",
            )
        if scene_id in seen_ids:
            raise AstridError(
                f"{path}.scene_id {scene_id!r} is duplicated",
                recovery_command=f"remove or rename duplicate scene_id {scene_id!r} so every entry is unique",
            )
        seen_ids.add(scene_id)
        if not isinstance(triage_score, int) or triage_score < 0 or triage_score > 5:
            raise AstridError(
                f"{path}.triage_score must be an integer from 0 to 5",
                valid_options=["0", "1", "2", "3", "4", "5"],
                recovery_command=f"set {path}.triage_score to an integer from 0 to 5",
            )
        if not isinstance(triage_tag, str) or not triage_tag:
            raise AstridError(
                f"{path}.triage_tag must be a non-empty string",
                recovery_command=f"provide a non-empty string for {path}.triage_tag",
            )


def _resolve_frame_path(frame_path: str, shots_dir: Path) -> Path:
    path = Path(frame_path)
    return path if path.is_absolute() else (shots_dir / path).resolve()


def _shot_map(shots: list[dict[str, Any]]) -> dict[int, dict[str, Any]]:
    mapping: dict[int, dict[str, Any]] = {}
    for shot in shots:
        if not isinstance(shot, dict):
            continue
        scene_index = shot.get("scene_index")
        if isinstance(scene_index, int):
            mapping[scene_index] = shot
    return mapping


def _scene_prompt(chunk: list[dict[str, Any]]) -> str:
    lines = [
        "Review the labeled keyframes for each scene_id and return JSON only.",
        "Score keep potential from 1 to 5 and provide a short triage_tag.",
        "Never return timestamps, durations, indexes, or any numeric ranges.",
        "",
        "Scenes in this batch:",
    ]
    for scene in chunk:
        lines.append(f"- {scene_id_for(scene)}")
    return "\n".join(lines)


def _attachments_for_chunk(chunk: list[dict[str, Any]], shots_by_scene: dict[int, dict[str, Any]], shots_dir: Path) -> list[dict[str, Any]]:
    attachments: list[dict[str, Any]] = []
    for scene in chunk:
        shot = shots_by_scene.get(int(scene["index"]))
        if not isinstance(shot, dict):
            continue
        frames = shot.get("frames")
        if not isinstance(frames, list):
            continue
        for frame in frames:
            if not isinstance(frame, dict):
                continue
            frame_path = frame.get("path")
            if not isinstance(frame_path, str):
                continue
            resolved = _resolve_frame_path(frame_path, shots_dir)
            attachments.append(
                {
                    "type": "image",
                    "source": {"type": "path", "path": str(resolved)},
                    "label": scene_id_for(scene),
                }
            )
    return attachments


def build_triage(
    scenes: list[dict[str, Any]],
    shots: list[dict[str, Any]],
    shots_dir: Path,
    *,
    client: ClaudeClient,
    grid_size: int = 20,
    model: str = "claude-haiku-4-5-20251001",
) -> dict[str, Any]:
    if grid_size <= 0:
        raise AstridError(
            "grid_size must be > 0",
            recovery_command="set grid_size to a positive integer (e.g. 20)",
        )
    shots_by_scene = _shot_map(shots)
    entries: list[dict[str, Any]] = []
    batch: list[dict[str, Any]] = []
    for scene in scenes:
        duration = float(scene.get("duration", float(scene.get("end", 0.0)) - float(scene.get("start", 0.0))))
        if duration < 0.3 or duration > 20.0:
            entries.append(
                {
                    "scene_id": scene_id_for(scene),
                    "triage_score": 0,
                    "triage_tag": "hard_filtered",
                }
            )
            continue
        batch.append(scene)
        if len(batch) < grid_size:
            continue
        response = client.complete_json(
            model=model,
            system=SYSTEM_PROMPT,
            messages=[
                {
                    "role": "user",
                    "content": [{"type": "text", "text": _scene_prompt(batch)}, *_attachments_for_chunk(batch, shots_by_scene, shots_dir)],
                }
            ],
            response_schema=RESPONSE_SCHEMA,
            max_tokens=2000,
        )
        raw_entries = response.get("entries")
        if not isinstance(raw_entries, list):
            raise AstridError(
                "Claude triage response is missing entries",
                recovery_command="check the Claude API response format; it must contain an 'entries' array",
                state_snapshot={"response_keys": list(response.keys()) if isinstance(response, dict) else None},
            )
        entries.extend(raw_entries)
        batch = []
    if batch:
        response = client.complete_json(
            model=model,
            system=SYSTEM_PROMPT,
            messages=[
                {
                    "role": "user",
                    "content": [{"type": "text", "text": _scene_prompt(batch)}, *_attachments_for_chunk(batch, shots_by_scene, shots_dir)],
                }
            ],
            response_schema=RESPONSE_SCHEMA,
            max_tokens=2000,
        )
        raw_entries = response.get("entries")
        if not isinstance(raw_entries, list):
            raise AstridError(
                "Claude triage response is missing entries",
                recovery_command="check the Claude API response format; it must contain an 'entries' array",
                state_snapshot={"response_keys": list(response.keys()) if isinstance(response, dict) else None},
            )
        entries.extend(raw_entries)

    # Claude occasionally emits the same scene_id twice across batch responses.
    # Keep the first occurrence and log the dupes rather than aborting the run.
    seen: set[str] = set()
    deduped: list[dict[str, Any]] = []
    dupes: list[str] = []
    for entry in entries:
        if isinstance(entry, dict):
            scene_id = entry.get("scene_id")
            if isinstance(scene_id, str) and scene_id in seen:
                dupes.append(scene_id)
                continue
            if isinstance(scene_id, str):
                seen.add(scene_id)
        deduped.append(entry)
    if dupes:
        print(f"triage: dropped {len(dupes)} duplicate entries from Claude response ({', '.join(dupes[:5])}{'…' if len(dupes) > 5 else ''})", flush=True)
    payload = {"version": TRIAGE_VERSION, "generated_at": utc_now_seconds(), "entries": deduped}
    validate_scene_triage(payload)
    return payload


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Run first-pass Claude scene triage over shot keyframes.")
    parser.add_argument("--scenes", type=Path, required=True)
    parser.add_argument("--shots", type=Path, required=True)
    parser.add_argument("--shots-dir", type=Path, required=True)
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--env-file", dest="env_file", type=Path)
    parser.add_argument("--model", default="claude-haiku-4-5-20251001")
    parser.add_argument("--grid-size", type=int, default=20)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    scenes = json.loads(args.scenes.read_text(encoding="utf-8"))
    shots = json.loads(args.shots.read_text(encoding="utf-8"))
    payload = build_triage(
        scenes,
        shots,
        args.shots_dir.resolve(),
        client=build_claude_client(args.env_file),
        grid_size=args.grid_size,
        model=args.model,
    )
    out_dir = args.out.resolve()
    out_dir.mkdir(parents=True, exist_ok=True)
    out_path = out_dir / "scene_triage.json"
    out_path.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
    register_outputs(
        stage="triage",
        outputs=[("scene_triage", out_path, "Scene triage")],
        metadata={"model": args.model, "grid_size": args.grid_size},
    )
    print(out_path)

    # --- universal result manifest -----------------------
    manifest_path = out_dir / "manifest.json"
    manifest = build_manifest(
        kind="triage",
        inputs={
            "scenes": str(args.scenes.resolve()),
            "shots": str(args.shots.resolve()),
        },
        outputs=[
            {"path": "scene_triage.json", "type": "file"},
        ],
        created=datetime.now(timezone.utc).isoformat(),
    )
    write_manifest(manifest_path, manifest)
    # -------------------------------------------------------------------------

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
