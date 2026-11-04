"""Plan-v2 template for stream_content.distill (self-contained).

The former implementation delegated to the retired task-mode orchestrator
plan-template (and through it to the deleted task-plan schema). The plan-v2
document emitted here is an informational artifact: the pack executes its
steps directly via ``run_full``'s subprocess calls, so the builder is
self-contained.
"""

from __future__ import annotations

import hashlib
import json
import shlex
import uuid
from pathlib import Path
from typing import Any

from astrid.pack_api import canonical_event_json


def _file_output(name: str, path: str | Path) -> dict[str, Any]:
    """Named produced-file entry with the default file_nonempty check."""

    return {
        name: {
            "path": str(path),
            "check": {"check_id": "file_nonempty", "params": {}, "sentinel": False},
        }
    }


def _cost_entry(amount: float, *, source: str) -> dict[str, Any]:
    return {"amount": float(amount), "currency": "USD", "source": source}


def _step_dict(
    step_id: str,
    *,
    command: str,
    produces: list[dict[str, Any]],
    cost: dict[str, Any] | None,
) -> dict[str, Any]:
    step: dict[str, Any] = {"id": step_id, "adapter": "local", "command": command}
    if produces:
        merged: dict[str, Any] = {}
        for entry in produces:
            merged.update(entry)
        step["produces"] = merged
    if cost is not None:
        step["cost"] = cost
    return step


def build_plan_v2(
    *,
    python_exec: str,
    run_root: str | Path,
    video: str | Path,
    transcript: str | Path | None = None,
    brief: str | Path | None = None,
    include_scenes: bool = True,
    run_id: str | None = None,
) -> dict[str, Any]:
    run_root = Path(run_root)
    plan_id = f"stream-content-{run_id or uuid.uuid4().hex[:12]}"
    local_zero = _cost_entry(0, source="local")
    steps: list[dict[str, Any]] = []

    if transcript is None:
        steps.append(
            _step_dict(
                "transcribe",
                command=_cmd_transcribe(python_exec, video),
                produces=[_file_output("transcript", "transcript.json")],
                cost=local_zero,
            )
        )
        transcript_ref = run_root / "steps" / "transcribe" / "v1" / "produces" / "transcript.json"
    else:
        transcript_ref = Path(transcript).resolve()

    scenes_ref: Path | None = None
    if include_scenes:
        steps.append(
            _step_dict(
                "scenes",
                command=_cmd_scenes(python_exec, video),
                produces=[_file_output("scenes", "scenes.json")],
                cost=local_zero,
            )
        )
        scenes_ref = run_root / "steps" / "scenes" / "v1" / "produces" / "scenes.json"

    steps.extend(
        [
            _step_dict(
                "segment-map",
                command=_cmd_segment_map(python_exec, video, transcript_ref, scenes_ref),
                produces=[_file_output("segment_map", "segment_map.json")],
                cost=local_zero,
            ),
            _step_dict(
                "extract-segments",
                command=_cmd_extract_segments(python_exec, video, run_root),
                produces=[_file_output("segments_manifest", "segments/segments.json")],
                cost=local_zero,
            ),
            _step_dict(
                "clip-candidates",
                command=_cmd_clip_candidates(python_exec, transcript_ref, run_root, brief),
                produces=[_file_output("candidates", "candidates.json")],
                cost=local_zero,
            ),
            _step_dict(
                "review",
                command=_cmd_review(python_exec, video, run_root),
                produces=[_file_output("review", "review.html")],
                cost=local_zero,
            ),
        ]
    )
    return {"plan_id": plan_id, "version": 2, "steps": steps}


def emit_plan_json(plan: dict[str, Any], path: str | Path) -> None:
    """Write canonical plan JSON."""

    output_path = Path(path)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    payload = json.dumps(plan, indent=2, sort_keys=True, ensure_ascii=False) + "\n"
    output_path.write_text(payload, encoding="utf-8")


def compute_plan_hash(plan_path: str | Path) -> str:
    """Return the canonical ``sha256:<hex>`` digest of an emitted plan file."""

    path = Path(plan_path)
    payload = json.loads(path.read_text(encoding="utf-8"))
    digest = hashlib.sha256(canonical_event_json(payload).encode("utf-8")).hexdigest()
    return f"sha256:{digest}"


def _cmd_public_action(capability_id: str, inputs: dict[str, str]) -> str:
    encoded = ", ".join(f"{json.dumps(name)}: {json.dumps(value)}" for name, value in inputs.items())
    return (
        f"astrid.sdk.invoke({json.dumps(capability_id)}, kind='action', "
        f"inputs={{{encoded}}}, wait=True)"
    )


def _cmd_transcribe(python_exec: str, video: str | Path) -> str:
    return _cmd_public_action(
        "editorial.transcribe",
        {"audio": str(Path(video).resolve())},
    )


def _cmd_scenes(python_exec: str, video: str | Path) -> str:
    return _cmd_public_action(
        "editorial.scenes",
        {"video": str(Path(video).resolve())},
    )


def _cmd_segment_map(python_exec: str, video: str | Path, transcript: Path, scenes: Path | None) -> str:
    scenes_arg = f" --scenes {shlex.quote(str(scenes))}" if scenes is not None else ""
    return (
        f"{shlex.quote(str(python_exec))} -X utf8 -m astrid.packs.stream_content.actions.segment_map.run "
        f"--video {shlex.quote(str(Path(video).resolve()))} "
        f"--transcript {shlex.quote(str(transcript))}{scenes_arg} "
        f"--out {shlex.quote('{produces_root}/segment_map.json')}"
    )


def _cmd_extract_segments(python_exec: str, video: str | Path, run_root: Path) -> str:
    segment_map = run_root / "steps" / "segment-map" / "v1" / "produces" / "segment_map.json"
    return (
        f"{shlex.quote(str(python_exec))} -X utf8 -m astrid.packs.stream_content.actions.distill.run "
        f"extract-segments --video {shlex.quote(str(Path(video).resolve()))} "
        f"--segment-map {shlex.quote(str(segment_map))} "
        f"--out-dir {shlex.quote('{produces_root}/segments')} "
        f"--manifest {shlex.quote('{produces_root}/segments/segments.json')}"
    )


def _cmd_clip_candidates(python_exec: str, transcript: Path, run_root: Path, brief: str | Path | None) -> str:
    segment_map = run_root / "steps" / "segment-map" / "v1" / "produces" / "segment_map.json"
    brief_arg = f" --brief {shlex.quote(str(Path(brief).resolve()))}" if brief is not None else ""
    return (
        f"{shlex.quote(str(python_exec))} -X utf8 -m astrid.packs.stream_content.actions.clip_candidates.run "
        f"--transcript {shlex.quote(str(transcript))} "
        f"--segment-map {shlex.quote(str(segment_map))}{brief_arg} "
        f"--out {shlex.quote('{produces_root}/candidates.json')}"
    )


def _cmd_review(python_exec: str, video: str | Path, run_root: Path) -> str:
    segment_map = run_root / "steps" / "segment-map" / "v1" / "produces" / "segment_map.json"
    candidates = run_root / "steps" / "clip-candidates" / "v1" / "produces" / "candidates.json"
    segments_manifest = run_root / "steps" / "extract-segments" / "v1" / "produces" / "segments" / "segments.json"
    return (
        f"{shlex.quote(str(python_exec))} -X utf8 -m astrid.packs.stream_content.actions.distill.run "
        f"review --video {shlex.quote(str(Path(video).resolve()))} "
        f"--segment-map {shlex.quote(str(segment_map))} "
        f"--candidates {shlex.quote(str(candidates))} "
        f"--segments-manifest {shlex.quote(str(segments_manifest))} "
        f"--out {shlex.quote('{produces_root}/review.html')}"
    )


__all__ = ["build_plan_v2", "compute_plan_hash", "emit_plan_json"]
