#!/usr/bin/env python3
"""Rendered-cut editor review helpers."""


from __future__ import annotations

from astrid.pack_api import AstridError
from astrid.pack_api import guard_canonical_entrypoint

guard_canonical_entrypoint('editorial.editor_review')
import argparse
import json
import re
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Sequence

from astrid.pack_api import build_manifest, write_manifest
from astrid.pack_api import AuditContext
from astrid.pack_api import executor_argv
from astrid.pack_api import ffprobe_duration_seconds
from astrid.pack_api import load_arrangement, load_metadata, load_pool
from astrid.pack_api import build_claude_client

from astrid.packs.editorial.shared._common import load_api_key
from ..arrange.run import pool_digest

EDITOR_ACTIONS = (
    "accept",
    "micro-fix",
    "swap",
    "reorder",
    "insert-stinger",
    "needs-better-pool-entry",
)
EDITOR_PRIORITIES = ("high", "medium", "low")
DEFAULT_MODEL = "claude-sonnet-4-6"

RESPONSE_SCHEMA: dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "properties": {
        "iteration": {"type": "integer", "minimum": 1, "maximum": 2},
        "notes": {
            "type": "array",
            "items": {
                "type": "object",
                "additionalProperties": False,
                "properties": {
                    "clip_order": {"type": "integer"},
                    "clip_uuid": {"type": "string", "pattern": "^[0-9a-f]{8}$"},
                    "observation": {"type": "string"},
                    "brief_impact": {"type": "string"},
                    "action": {"type": "string", "enum": list(EDITOR_ACTIONS)},
                    "action_detail": {
                        "anyOf": [
                            {
                                "type": "object",
                                "properties": {
                                    "trim_delta_start_sec": {"type": "number"},
                                    "trim_delta_end_sec": {"type": "number"},
                                    "reason": {"type": "string"},
                                },
                                "required": ["trim_delta_start_sec", "trim_delta_end_sec", "reason"],
                            },
                            {
                                "type": "object",
                                "properties": {
                                    "candidate_pool_id": {"type": "string"},
                                    "role": {"type": "string"},
                                    "reason": {"type": "string"},
                                },
                                "required": ["candidate_pool_id", "role", "reason"],
                            },
                            {
                                "type": "object",
                                "properties": {
                                    "new_order": {"type": "integer"},
                                    "reason": {"type": "string"},
                                },
                                "required": ["new_order", "reason"],
                            },
                            {
                                "type": "object",
                                "properties": {
                                    "after_clip_order": {"type": "integer"},
                                    "candidate_pool_id": {"type": "string"},
                                    "duration_sec": {"type": "number"},
                                    "reason": {"type": "string"},
                                },
                                "required": ["after_clip_order", "candidate_pool_id", "duration_sec", "reason"],
                            },
                            {
                                "type": "object",
                                "properties": {"reason": {"type": "string"}},
                                "required": ["reason"],
                            },
                            {"type": "null"},
                        ]
                    },
                    "priority": {"type": "string", "enum": list(EDITOR_PRIORITIES)},
                    "candidate_pool_id": {"type": ["string", "null"]},
                },
                "required": [
                    "clip_order",
                    "clip_uuid",
                    "observation",
                    "brief_impact",
                    "action",
                    "action_detail",
                    "priority",
                    "candidate_pool_id",
                ],
            },
        },
        "verdict": {"type": "string", "enum": ["ship", "iterate", "rework"]},
        "ship_confidence": {"type": "number", "minimum": 0, "maximum": 1},
    },
    "required": ["iteration", "notes", "verdict", "ship_confidence"],
}


def _arrangement_orders(arrangement: dict[str, Any]) -> set[int]:
    clips = arrangement.get("clips")
    if not isinstance(clips, list):
        raise AstridError(
            "arrangement.clips must be a list",
            recovery_command="fix arrangement.json: ensure 'clips' is a JSON array",
        )
    orders: set[int] = set()
    for index, clip in enumerate(clips):
        if not isinstance(clip, dict):
            raise AstridError(
                f"arrangement.clips[{index}] must be an object",
                recovery_command="fix arrangement.json: ensure each clip in 'clips' is a JSON object",
            )
        order = clip.get("order")
        if not isinstance(order, int):
            raise AstridError(
                f"arrangement.clips[{index}].order must be an integer",
                recovery_command="fix arrangement.json: ensure each clip has an integer 'order' field",
            )
        orders.add(order)
    return orders


def _arrangement_order_uuid_map(arrangement: dict[str, Any]) -> dict[int, str]:
    clips = arrangement.get("clips")
    if not isinstance(clips, list):
        raise AstridError(
            "arrangement.clips must be a list",
            recovery_command="fix arrangement.json: ensure 'clips' is a JSON array",
        )
    result: dict[int, str] = {}
    for index, clip in enumerate(clips):
        if not isinstance(clip, dict):
            raise AstridError(
                f"arrangement.clips[{index}] must be an object",
                recovery_command="fix arrangement.json: ensure each clip in 'clips' is a JSON object",
            )
        order = clip.get("order")
        if not isinstance(order, int):
            raise AstridError(
                f"arrangement.clips[{index}].order must be an integer",
                recovery_command="fix arrangement.json: ensure each clip has an integer 'order' field",
            )
        clip_uuid = clip.get("uuid")
        if not isinstance(clip_uuid, str) or re.fullmatch(r"[0-9a-f]{8}", clip_uuid) is None:
            raise AstridError(
                f"arrangement.clips[{index}].uuid must be 8 lowercase hex characters",
                recovery_command="fix arrangement.json: ensure each clip has a 'uuid' field with 8 lowercase hex characters",
            )
        result[order] = clip_uuid
    return result


def _validate_note_uuid_references(notes: list[Any], arrangement: dict[str, Any]) -> dict[int, str]:
    order_to_uuid = _arrangement_order_uuid_map(arrangement)
    valid_uuids = set(order_to_uuid.values())
    for index, note in enumerate(notes):
        if not isinstance(note, dict):
            continue
        clip_order = note.get("clip_order")
        if not isinstance(clip_order, int):
            continue
        clip_uuid = note.get("clip_uuid")
        if not isinstance(clip_uuid, str) or re.fullmatch(r"[0-9a-f]{8}", clip_uuid) is None:
            raise AstridError(
                f"editor_review.notes[{index}].clip_uuid must be 8 lowercase hex characters",
                recovery_command="fix editor_review.json: ensure each note's clip_uuid is 8 lowercase hex characters matching an arrangement clip",
            )
        if clip_uuid not in valid_uuids:
            raise AstridError(
                f"editor_review.notes[{index}].clip_uuid {clip_uuid!r} is not in arrangement",
                recovery_command="fix editor_review.json: ensure each note's clip_uuid references a clip that exists in the arrangement",
            )
        if clip_order not in order_to_uuid:
            raise AstridError(
                f"editor_review.notes[{index}].clip_order {clip_order!r} is not in arrangement",
                recovery_command="fix editor_review.json: ensure each note's clip_order references a clip that exists in the arrangement",
            )
        if order_to_uuid[clip_order] != clip_uuid:
            raise AstridError(
                f"editor_review.notes[{index}].clip_uuid {clip_uuid!r} does not match clip_order {clip_order!r}",
                recovery_command="fix editor_review.json: ensure each note's clip_uuid matches the arrangement clip at the given clip_order",
            )
    return order_to_uuid


def _require_detail(note: dict[str, Any], action: str) -> dict[str, Any]:
    detail = note.get("action_detail")
    if not isinstance(detail, dict):
        raise AstridError(
            f"{action} note requires action_detail object",
            recovery_command=f"fix editor_review.json: set action_detail to an object for the {action} note, or use the correct action",
        )
    return detail


def _require_numeric(detail: dict[str, Any], field: str, action: str) -> None:
    if not isinstance(detail.get(field), (int, float)):
        raise AstridError(
            f"{action} action_detail.{field} must be numeric",
            recovery_command=f"fix editor_review.json: set action_detail.{field} to a number for the {action} note",
        )


def _require_detail_keys(detail: dict[str, Any], action: str, expected: set[str]) -> None:
    missing = expected - set(detail)
    if missing:
        raise AstridError(
            f"{action} action_detail missing required keys: {sorted(missing)}",
            recovery_command=f"fix editor_review.json: add the missing keys {sorted(missing)} to action_detail for the {action} note",
        )


def _validate_editor_notes(payload: dict[str, Any], arrangement: dict[str, Any]) -> None:
    """Validate editor-review semantics that are awkward to express in JSON Schema."""

    notes = payload.get("notes")
    if not isinstance(notes, list):
        raise AstridError(
            "editor_review.notes must be a list",
            recovery_command="fix editor_review.json: ensure 'notes' is a JSON array",
        )
    order_to_uuid = _validate_note_uuid_references(notes, arrangement)
    valid_orders = set(order_to_uuid)

    for index, note in enumerate(notes):
        if not isinstance(note, dict):
            raise AstridError(
                f"editor_review.notes[{index}] must be an object",
                recovery_command="fix editor_review.json: ensure each note in 'notes' is a JSON object",
            )
        action = note.get("action")
        if action not in EDITOR_ACTIONS:
            raise AstridError(
                f"editor_review.notes[{index}].action is invalid",
                valid_options=list(EDITOR_ACTIONS),
                recovery_command=f"fix editor_review.json: set note[{index}].action to one of {list(EDITOR_ACTIONS)}",
            )
        clip_order = note.get("clip_order")
        if not isinstance(clip_order, int):
            raise AstridError(
                f"editor_review.notes[{index}].clip_order must be an integer",
                recovery_command="fix editor_review.json: ensure each note has an integer 'clip_order' field",
            )

        if action == "insert-stinger":
            detail = _require_detail(note, action)
            _require_detail_keys(detail, action, {"after_clip_order", "candidate_pool_id", "duration_sec", "reason"})
            after_clip_order = detail.get("after_clip_order")
            if not isinstance(after_clip_order, int) or after_clip_order not in valid_orders:
                raise AstridError(
                    "insert-stinger action_detail.after_clip_order must reference an existing clip order",
                    recovery_command="fix editor_review.json: set after_clip_order to a clip order that exists in the arrangement",
                )
            candidate_pool_id = detail.get("candidate_pool_id")
            if not isinstance(candidate_pool_id, str) or not candidate_pool_id:
                raise AstridError(
                    "insert-stinger action_detail.candidate_pool_id must be a non-empty string",
                    recovery_command="fix editor_review.json: set candidate_pool_id to a valid pool entry id for the insert-stinger note",
                )
            _require_numeric(detail, "duration_sec", action)
            continue

        if action == "micro-fix":
            detail = _require_detail(note, action)
            _require_detail_keys(detail, action, {"trim_delta_start_sec", "trim_delta_end_sec", "reason"})
            _require_numeric(detail, "trim_delta_start_sec", action)
            _require_numeric(detail, "trim_delta_end_sec", action)
        elif action == "swap":
            detail = _require_detail(note, action)
            _require_detail_keys(detail, action, {"candidate_pool_id", "role", "reason"})
            candidate_pool_id = note.get("candidate_pool_id")
            if not isinstance(candidate_pool_id, str) or not candidate_pool_id:
                raise AstridError(
                    "swap note requires candidate_pool_id",
                    recovery_command="fix editor_review.json: set candidate_pool_id on the swap note to a valid pool entry id",
                )
            if detail.get("candidate_pool_id") != candidate_pool_id:
                raise AstridError(
                    "swap action_detail.candidate_pool_id must match note candidate_pool_id",
                    recovery_command="fix editor_review.json: ensure swap action_detail.candidate_pool_id matches the note-level candidate_pool_id",
                )
            if not isinstance(detail.get("role"), str) or not detail["role"]:
                raise AstridError(
                    "swap action_detail.role must be a non-empty string",
                    recovery_command="fix editor_review.json: set action_detail.role to a non-empty string for the swap note (e.g., 'primary', 'b-roll')",
                )
        elif action == "reorder":
            detail = _require_detail(note, action)
            _require_detail_keys(detail, action, {"new_order", "reason"})
            new_order = detail.get("new_order")
            if not isinstance(new_order, int):
                raise AstridError(
                    "reorder action_detail.new_order must be an integer",
                    recovery_command="fix editor_review.json: set action_detail.new_order to an integer for the reorder note",
                )
        elif action == "needs-better-pool-entry":
            detail = _require_detail(note, action)
            _require_detail_keys(detail, action, {"reason"})
            if not isinstance(detail.get("reason"), str) or not detail["reason"].strip():
                raise AstridError(
                    "needs-better-pool-entry action_detail.reason must be a non-empty string",
                    recovery_command="fix editor_review.json: set action_detail.reason to a non-empty string explaining why a better pool entry is needed",
                )
        elif action == "accept" and note.get("action_detail") is not None:
            raise AstridError(
                "accept action_detail must be null",
                recovery_command="fix editor_review.json: set action_detail to null (or omit it) for accept notes",
            )


def _validate_review_payload_shape(payload: dict[str, Any], arrangement: dict[str, Any] | None = None) -> None:
    if not isinstance(payload, dict):
        raise AstridError(
            "editor_review payload must be an object",
            recovery_command="fix editor_review.json: ensure the top-level payload is a JSON object",
        )
    allowed = set(RESPONSE_SCHEMA["properties"])
    unknown = sorted(set(payload) - allowed)
    if unknown:
        raise AstridError(
            f"editor_review payload has unknown keys: {unknown}",
            recovery_command=f"fix editor_review.json: remove unknown keys {unknown} from the payload",
        )
    for field in RESPONSE_SCHEMA["required"]:
        if field not in payload:
            raise AstridError(
                f"editor_review payload missing required field {field!r}",
                recovery_command=f"fix editor_review.json: add the required field {field!r} to the payload",
            )
    iteration = payload.get("iteration")
    if not isinstance(iteration, int) or not 1 <= iteration <= 2:
        raise AstridError(
            "editor_review.iteration must be an integer between 1 and 2",
            valid_options=[1, 2],
            recovery_command="fix editor_review.json: set iteration to 1 or 2",
        )
    notes = payload.get("notes")
    if not isinstance(notes, list):
        raise AstridError(
            "editor_review.notes must be a list",
            recovery_command="fix editor_review.json: ensure 'notes' is a JSON array",
        )
    note_allowed = set(RESPONSE_SCHEMA["properties"]["notes"]["items"]["properties"])
    note_required = set(RESPONSE_SCHEMA["properties"]["notes"]["items"]["required"])
    for index, note in enumerate(notes):
        if not isinstance(note, dict):
            raise AstridError(
                f"editor_review.notes[{index}] must be an object",
                recovery_command="fix editor_review.json: ensure each note in 'notes' is a JSON object",
            )
        unknown_note = sorted(set(note) - note_allowed)
        if unknown_note:
            raise AstridError(
                f"editor_review.notes[{index}] has unknown keys: {unknown_note}",
                recovery_command=f"fix editor_review.json: remove unknown keys {unknown_note} from note[{index}]",
            )
        missing_note = sorted(note_required - set(note))
        if missing_note:
            raise AstridError(
                f"editor_review.notes[{index}] missing required keys: {missing_note}",
                recovery_command=f"fix editor_review.json: add required keys {missing_note} to note[{index}]",
            )
        if not isinstance(note.get("clip_order"), int):
            raise AstridError(
                f"editor_review.notes[{index}].clip_order must be an integer",
                recovery_command="fix editor_review.json: ensure each note has an integer 'clip_order' field",
            )
        clip_uuid = note.get("clip_uuid")
        if not isinstance(clip_uuid, str) or re.fullmatch(r"[0-9a-f]{8}", clip_uuid) is None:
            raise AstridError(
                f"editor_review.notes[{index}].clip_uuid must be 8 lowercase hex characters",
                recovery_command="fix editor_review.json: ensure each note's clip_uuid is 8 lowercase hex characters",
            )
        for key in ("observation", "brief_impact"):
            if not isinstance(note.get(key), str):
                raise AstridError(
                    f"editor_review.notes[{index}].{key} must be a string",
                    recovery_command=f"fix editor_review.json: set note[{index}].{key} to a string",
                )
        if note.get("action") not in EDITOR_ACTIONS:
            raise AstridError(
                f"editor_review.notes[{index}].action is invalid",
                valid_options=list(EDITOR_ACTIONS),
                recovery_command=f"fix editor_review.json: set note[{index}].action to one of {list(EDITOR_ACTIONS)}",
            )
        detail = note.get("action_detail")
        if detail is not None and not isinstance(detail, dict):
            raise AstridError(
                f"editor_review.notes[{index}].action_detail must be an object or null",
                recovery_command=f"fix editor_review.json: set note[{index}].action_detail to an object or null",
            )
        if note.get("priority") not in EDITOR_PRIORITIES:
            raise AstridError(
                f"editor_review.notes[{index}].priority is invalid",
                valid_options=list(EDITOR_PRIORITIES),
                recovery_command=f"fix editor_review.json: set note[{index}].priority to one of {list(EDITOR_PRIORITIES)}",
            )
        candidate_pool_id = note.get("candidate_pool_id")
        if candidate_pool_id is not None and not isinstance(candidate_pool_id, str):
            raise AstridError(
                f"editor_review.notes[{index}].candidate_pool_id must be a string or null",
                recovery_command=f"fix editor_review.json: set note[{index}].candidate_pool_id to a string or null",
            )
    if payload.get("verdict") not in {"ship", "iterate", "rework"}:
        raise AstridError(
            "editor_review.verdict is invalid",
            valid_options=["ship", "iterate", "rework"],
            recovery_command="fix editor_review.json: set verdict to 'ship', 'iterate', or 'rework'",
        )
    ship_confidence = payload.get("ship_confidence")
    if not isinstance(ship_confidence, (int, float)) or not 0 <= float(ship_confidence) <= 1:
        raise AstridError(
            "editor_review.ship_confidence must be a number between 0 and 1",
            recovery_command="fix editor_review.json: set ship_confidence to a number between 0.0 and 1.0",
        )
    if arrangement is not None:
        _validate_note_uuid_references(notes, arrangement)


def _probe_duration(
    hype_mp4: Path,
    *,
    ffprobe_runner: Any = subprocess.run,
) -> float:
    return ffprobe_duration_seconds(hype_mp4, runner=ffprobe_runner)


def sample_frames(
    hype_mp4: Path,
    cache_dir: Path,
    *,
    cadence_sec: float = 1.5,
    max_frames: int = 50,
    ffmpeg_runner: Any = subprocess.run,
    ffprobe_runner: Any = subprocess.run,
) -> list[Path]:
    """Sample review frames from the rendered artifact without copying the video."""

    hype_mp4 = Path(hype_mp4)
    cache_dir = Path(cache_dir)
    cache_dir.mkdir(parents=True, exist_ok=True)
    duration_sec = _probe_duration(hype_mp4, ffprobe_runner=ffprobe_runner)
    count = min(int(max_frames), int(duration_sec // float(cadence_sec)) + 1)
    frame_pattern = cache_dir / "frame_%03d.jpg"
    ffmpeg_runner(
        [
            "ffmpeg",
            "-ss",
            "0",
            "-i",
            str(hype_mp4),
            "-vf",
            f"fps=1/{float(cadence_sec):g}",
            "-frames:v",
            str(count),
            str(frame_pattern),
            "-hide_banner",
        ],
        check=True,
        capture_output=True,
        text=True,
    )
    return sorted(cache_dir.glob("frame_*.jpg"))


def _model_dump_or_dict(response: Any) -> dict[str, Any]:
    if hasattr(response, "model_dump"):
        return dict(response.model_dump())
    if isinstance(response, dict):
        return dict(response)
    return dict(response)


def transcribe_hype_audio(
    hype_mp4: Path,
    cache_dir: Path,
    *,
    openai_client: Any,
    model: str = "whisper-1",
    ffmpeg_runner: Any = subprocess.run,
) -> dict[str, Any]:
    """Extract audio from hype.mp4 and transcribe that rendered artifact."""

    hype_mp4 = Path(hype_mp4)
    cache_dir = Path(cache_dir)
    cache_dir.mkdir(parents=True, exist_ok=True)
    audio_path = cache_dir / "hype_audio.mp3"
    ffmpeg_runner(
        [
            "ffmpeg",
            "-y",
            "-i",
            str(hype_mp4),
            "-vn",
            "-acodec",
            "libmp3lame",
            str(audio_path),
        ],
        check=True,
        capture_output=True,
        text=True,
    )
    with audio_path.open("rb") as handle:
        response = openai_client.audio.transcriptions.create(
            model=model,
            file=handle,
            response_format="verbose_json",
            timestamp_granularities=["segment"],
        )
    data = _model_dump_or_dict(response)
    segments = [
        {
            "start": float(segment.get("start", 0.0)),
            "end": float(segment.get("end", 0.0)),
            "text": str(segment.get("text", "")).strip(),
        }
        for segment in list(data.get("segments") or [])
        if isinstance(segment, dict)
    ]
    return {"text": str(data.get("text", "")).strip(), "segments": segments}


def _note_key_set(value: Any) -> set[tuple[str, str]]:
    notes = value.get("notes") if isinstance(value, dict) else value
    if not isinstance(notes, list):
        return set()
    result: set[tuple[str, str]] = set()
    for note in notes:
        if not isinstance(note, dict):
            continue
        clip_uuid = note.get("clip_uuid")
        action = note.get("action")
        if (
            isinstance(clip_uuid, str)
            and re.fullmatch(r"[0-9a-f]{8}", clip_uuid) is not None
            and isinstance(action, str)
        ):
            result.add((clip_uuid, action))
    return result


def notes_overlap_ratio(prev: Any, curr: Any) -> float:
    prev_keys = _note_key_set(prev)
    curr_keys = _note_key_set(curr)
    denominator = max(len(prev_keys), len(curr_keys))
    if denominator == 0:
        return 0.0
    return len(prev_keys & curr_keys) / denominator


def plan_next_action(review: dict[str, Any]) -> str:
    if review.get("verdict") == "ship":
        return "ship"
    notes = review.get("notes")
    if not isinstance(notes, list):
        notes = []
    actionable = [note for note in notes if isinstance(note, dict) and note.get("action") != "accept"]
    if all(note.get("action") == "micro-fix" for note in actionable):
        return "micro-fix"
    return "rework"


def build_openai_client(env_file: Path | None = None) -> Any:
    from openai import OpenAI

    return OpenAI(api_key=load_api_key(env_file))


def _read_json(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def _brief_text(brief_dir: Path, arrangement: dict[str, Any]) -> str:
    brief_path = brief_dir / "brief.txt"
    if brief_path.is_file():
        return brief_path.read_text(encoding="utf-8").strip()
    text = arrangement.get("brief_text")
    if isinstance(text, str) and text.strip():
        return text.strip()
    raise AstridError(
        f"brief text not found in {brief_path} or arrangement.brief_text",
        recovery_command="ensure brief.txt exists in the brief directory or arrangement.json has a non-empty 'brief_text' field",
    )


def _clip_pool_and_role(clip: dict[str, Any]) -> tuple[str, str]:
    audio_source = clip.get("audio_source")
    visual_source = clip.get("visual_source")
    pool_id = "none"
    role = "primary"
    if isinstance(audio_source, dict) and isinstance(audio_source.get("pool_id"), str):
        pool_id = audio_source["pool_id"]
    if isinstance(visual_source, dict):
        if pool_id == "none" and isinstance(visual_source.get("pool_id"), str):
            pool_id = visual_source["pool_id"]
        if isinstance(visual_source.get("role"), str):
            role = visual_source["role"]
    return pool_id, role


def arrangement_summary(arrangement: dict[str, Any]) -> str:
    lines: list[str] = []
    for clip in sorted(arrangement.get("clips", []), key=lambda item: int(item.get("order", 0)) if isinstance(item, dict) else 0):
        if not isinstance(clip, dict):
            continue
        pool_id, role = _clip_pool_and_role(clip)
        rationale = str(clip.get("rationale", "")).replace("\n", " ").strip()
        lines.append(
            f"[{clip['order']}] uuid={clip.get('uuid')} pool={pool_id} role={role} rationale={rationale[:80]}"
        )
    return "\n".join(lines)


def inspect_cut_text(brief_dir: Path, *, runner: Any | None = None) -> str:
    if runner is None:
        runner = subprocess.run
    result = runner(
        [*executor_argv("editorial.inspect_cut", sys.executable), str(brief_dir), "--no-color"],
        check=False,
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        stderr = str(result.stderr or result.stdout or "").strip()
        return f"! inspect_cut failed: {stderr}"
    return str(result.stdout or "").strip()


def _transcript_text(transcript: dict[str, Any]) -> str:
    segments = transcript.get("segments")
    if not isinstance(segments, list) or not segments:
        return str(transcript.get("text", "")).strip()
    lines = []
    for segment in segments:
        if not isinstance(segment, dict):
            continue
        start = float(segment.get("start", 0.0))
        end = float(segment.get("end", 0.0))
        text = str(segment.get("text", "")).strip()
        lines.append(f"[{start:.2f}-{end:.2f}] {text}")
    return "\n".join(lines)


def build_system_prompt(
    *,
    brief_text: str,
    arrangement_text: str,
    refine_report: dict[str, Any],
    inspect_text: str,
    quality_zones: dict[str, Any],
    pool_text: str,
    transcript: dict[str, Any],
) -> str:
    return "\n\n".join(
        [
            "You are a senior short-form video editor reviewing the rendered hype.mp4 against the brief. "
            "Use the artifact evidence first. Return only structured editor_review JSON. "
            "Prefer minimal downstream actions: accept, micro-fix, swap, reorder, insert-stinger, or needs-better-pool-entry.",
            "Every note must include `clip_uuid` copied verbatim from the arrangement clip you reference.",
            f"BRIEF:\n{brief_text}",
            f"ARRANGEMENT SUMMARY:\n{arrangement_text}",
            f"REFINE JSON:\n{json.dumps(refine_report, indent=2, sort_keys=True)}",
            f"INSPECT CUT:\n{inspect_text}",
            f"QUALITY ZONES:\n{json.dumps(quality_zones, indent=2, sort_keys=True)}",
            f"POOL DIGEST:\n{pool_text}",
            f"RENDERED AUDIO TRANSCRIPT:\n{_transcript_text(transcript)}",
        ]
    )


def build_vision_messages(
    *,
    frames: list[Path],
    cadence_sec: float,
    summary_text: str,
) -> list[dict[str, Any]]:
    content: list[dict[str, Any]] = [
        {
            "type": "text",
            "text": (
                "Review these sampled frames from the rendered hype.mp4 together with the system evidence. "
                "Identify only material issues that affect the brief, pacing, clarity, quality, or ordering.\n\n"
                f"{summary_text}"
            ),
        }
    ]
    for index, frame in enumerate(frames):
        content.append({"type": "text", "text": f"(t={index * float(cadence_sec):.1f}s)"})
        content.append({"type": "image", "source": {"type": "path", "path": str(frame)}})
    return [{"role": "user", "content": content}]


def _validated_review(response: dict[str, Any], arrangement: dict[str, Any], *, iteration: int) -> dict[str, Any]:
    payload = dict(response)
    payload["iteration"] = int(iteration)
    _validate_review_payload_shape(payload, arrangement)
    _validate_editor_notes(payload, arrangement)
    return payload


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Review a rendered hype.mp4 against its brief.")
    parser.add_argument("--brief-dir", type=Path, required=True)
    parser.add_argument("--run-dir", type=Path, required=True)
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--iteration", type=int, default=1)
    parser.add_argument("--env-file", type=Path)
    parser.add_argument("--model", default=DEFAULT_MODEL)
    parser.add_argument("--max-frames", type=int, default=50)
    parser.add_argument("--cadence-sec", type=float, default=1.5)
    parser.add_argument("--skip-llm", action="store_true")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if not 1 <= int(args.iteration) <= 2:
        parser.error("--iteration must be 1 or 2")

    brief_dir = args.brief_dir.resolve()
    run_dir = args.run_dir.resolve()
    out_dir = args.out.resolve()
    hype_mp4 = brief_dir / "hype.mp4"
    arrangement = load_arrangement(brief_dir / "arrangement.json", assign_missing_uuids=True)
    refine_report = _read_json(brief_dir / "refine.json")
    quality_zones_path = run_dir / "quality_zones.json"
    quality_zones = _read_json(quality_zones_path) if quality_zones_path.is_file() else {}
    _ = load_metadata(brief_dir / "hype.metadata.json")
    pool = load_pool(run_dir / "pool.json")
    brief = _brief_text(brief_dir, arrangement)

    review_cache = out_dir / ".editor_review_cache" / f"iteration_{int(args.iteration)}"
    frames = sample_frames(hype_mp4, review_cache / "frames", cadence_sec=args.cadence_sec, max_frames=args.max_frames)
    if args.skip_llm:
        transcript = {"text": "", "segments": []}
        response = {"iteration": int(args.iteration), "notes": [], "verdict": "ship", "ship_confidence": 1.0}
    else:
        transcript = transcribe_hype_audio(
            hype_mp4,
            review_cache / "audio",
            openai_client=build_openai_client(args.env_file),
        )
        arrangement_text = arrangement_summary(arrangement)
        inspect_text = inspect_cut_text(brief_dir)
        system_prompt = build_system_prompt(
            brief_text=brief,
            arrangement_text=arrangement_text,
            refine_report=refine_report,
            inspect_text=inspect_text,
            quality_zones=quality_zones,
            pool_text=pool_digest(pool),
            transcript=transcript,
        )
        messages = build_vision_messages(
            frames=frames,
            cadence_sec=args.cadence_sec,
            summary_text=(
                "Return editor_review JSON with clip_order and clip_uuid references matching arrangement order. "
                "Every note must include clip_uuid copied verbatim from the arrangement clip you reference. "
                "For swap actions include candidate_pool_id when a replacement exists."
            ),
        )
        response = build_claude_client(args.env_file).complete_json(
            model=args.model,
            system=system_prompt,
            messages=messages,
            response_schema=RESPONSE_SCHEMA,
            max_tokens=4000,
        )

    review = _validated_review(response, arrangement, iteration=int(args.iteration))
    out_dir.mkdir(parents=True, exist_ok=True)
    out_path = out_dir / "editor_review.json"
    out_path.write_text(json.dumps(review, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    audit = AuditContext.from_env()
    if audit is not None:
        review_id = audit.register_asset(
            kind="editor_review",
            path=out_path,
            label="Editor review",
            stage="editor_review",
            metadata={
                "model": args.model,
                "iteration": int(args.iteration),
                "verdict": review.get("verdict"),
                "notes": len(review.get("notes", [])),
            },
        )
        selected = [str(note.get("clip_uuid") or note.get("clip_order")) for note in review.get("notes", [])]
        audit.register_decision(
            stage="editor_review",
            label=f"Editor verdict: {review.get('verdict')}",
            selected=selected,
            metadata={"review_asset": review_id, "ship_confidence": review.get("ship_confidence")},
        )
        audit.register_node(stage="editor_review", label="Review rendered cut", outputs=[review_id])
    print(out_path)

    # --- universal result manifest -----------------------
    manifest_path = out_dir / "manifest.json"
    manifest = build_manifest(
        kind="editor_review",
        inputs={
            "brief_dir": str(brief_dir),
            "run_dir": str(run_dir),
        },
        outputs=[
            {"path": "editor_review.json", "type": "file"},
        ],
        created=datetime.now(timezone.utc).isoformat(),
    )
    write_manifest(manifest_path, manifest)
    # -------------------------------------------------------------------------

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
