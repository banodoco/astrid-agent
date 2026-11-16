"""Serialize pinned ComfyUI-Whisper output into Runtime transcript documents."""
from __future__ import annotations

import hashlib
import json
import math
from collections.abc import Mapping, Sequence
from typing import Any

from .media_transcript import SCHEMA as TRANSCRIPT_SCHEMA, transcript_document_id

WHISPER_SOURCE_URL = (
    "https://raw.githubusercontent.com/yuvraj108c/ComfyUI-Whisper/"
    "006a709848259ed3d69e2f9fcfd7a75f6c860ddd/apply_whisper.py"
)


def _finite_time(value: Any, label: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"{label} must be numeric seconds")
    value = float(value)
    if not math.isfinite(value) or value < 0 or value > 1_000_000_000:
        raise ValueError(f"{label} must be finite seconds in the Runtime transcript range")
    return value


def _alignment_rows(value: Any, label: str) -> list[tuple[str, float, float]]:
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes)):
        raise ValueError(f"{label} must be a sequence")
    rows = []
    for row in value:
        if not isinstance(row, Mapping):
            raise ValueError(f"each {label} item must be an object")
        text = row.get("value")
        if not isinstance(text, str) or not text:
            raise ValueError(f"each {label} value must be a non-empty string")
        start = _finite_time(row.get("start"), f"{label} start")
        end = _finite_time(row.get("end"), f"{label} end")
        if end < start:
            raise ValueError(f"{label} end must be at or after start")
        rows.append((text, start, end))
    return rows


def whisper_result_digest(result: Any) -> str:
    """Return the canonical digest of the tuple's serialized alignment payload."""
    if not isinstance(result, Sequence) or isinstance(result, (str, bytes)) or len(result) != 3:
        raise ValueError("Whisper result must be (text, segments_alignment, words_alignment)")
    encoded = json.dumps(result, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)
    return "sha256:" + hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def whisper_source_id(model_id: str) -> str:
    """Encode the pinned node revision and model identity in Runtime source_id."""
    if not isinstance(model_id, str) or not model_id.strip():
        raise ValueError("model identity must be non-empty")
    source_id = f"astrid.transcription/{WHISPER_SOURCE_URL.rsplit('/', 2)[-2]}/{model_id}"
    if len(source_id) > 512:
        raise ValueError("model identity is too long for source_id")
    return source_id


def build_whisper_transcript(
    media_id: str,
    source_id: str,
    result: Any,
    *,
    media_duration: float,
) -> dict[str, Any]:
    """Build media.transcript content while preserving each aligned word once.

    Words are assigned only when exactly one source segment fully contains their
    time range. Overlapping word ranges are supported; ambiguous segment
    membership is rejected rather than guessed.
    """
    if (not isinstance(media_id, str) or len(media_id) != 71 or not media_id.startswith("sha256:")
            or any(c not in "0123456789abcdef" for c in media_id[7:])):
        raise ValueError("media_id must be a sha256 digest")
    if not isinstance(source_id, str) or not source_id.strip() or len(source_id) > 512:
        raise ValueError("source_id must be non-empty and at most 512 characters")
    duration = _finite_time(media_duration, "media duration")
    if not isinstance(result, Sequence) or isinstance(result, (str, bytes)) or len(result) != 3:
        raise ValueError("Whisper result must be (text, segments_alignment, words_alignment)")
    text, segment_data, word_data = result
    if not isinstance(text, str):
        raise ValueError("Whisper text must be a string")
    segments = _alignment_rows(segment_data, "segments_alignment")
    words = _alignment_rows(word_data, "words_alignment")
    for _, start, end in [*segments, *words]:
        if end > duration:
            raise ValueError("alignment extends beyond media duration")

    result_digest = whisper_result_digest(result)
    grouped: list[list[dict[str, Any]]] = [[] for _ in segments]
    for word_index, (word_text, start, end) in enumerate(words):
        matches = [i for i, (_, seg_start, seg_end) in enumerate(segments)
                   if start >= seg_start and end <= seg_end]
        if len(matches) != 1:
            raise ValueError(f"word {word_index} has ambiguous or missing segment membership")
        identity = json.dumps([media_id, source_id, result_digest, word_index, start, end, word_text],
                              ensure_ascii=False, separators=(",", ":"), allow_nan=False)
        word_id = "word:" + hashlib.sha256(identity.encode("utf-8")).hexdigest()
        grouped[matches[0]].append({"id": word_id, "start": start, "end": end, "text": word_text})

    serialized_segments = []
    for index, (segment_text, start, end) in enumerate(segments):
        identity = json.dumps([media_id, source_id, result_digest, index, start, end, segment_text],
                              ensure_ascii=False, separators=(",", ":"), allow_nan=False)
        serialized_segments.append({
            "id": "segment:" + hashlib.sha256(identity.encode("utf-8")).hexdigest(),
            "start": start, "end": end, "text": segment_text, "words": grouped[index],
        })
    return {
        "schema": TRANSCRIPT_SCHEMA,
        "media_id": media_id,
        "source_id": source_id,
        "segments": serialized_segments,
    }


def import_whisper_transcript(
    client: Any,
    project: str,
    media_id: str,
    source_id: str,
    result: Any,
    *,
    media_duration: float,
    replace: bool = False,
    expected_version: int | None = None,
) -> str:
    """Persist with Runtime CAS; differing content needs explicit versioned replacement."""
    content = build_whisper_transcript(media_id, source_id, result,
                                       media_duration=media_duration)
    project_result = client.projects.show(project)
    if not project_result.ok:
        raise ValueError(f"cannot resolve project {project!r}: {project_result.error}")
    project_row = project_result.data
    project_id = (project_row.get("project_id") or project_row.get("id")
                  if isinstance(project_row, Mapping) else None)
    if not isinstance(project_id, str) or not project_id:
        raise ValueError("project has no Runtime identity")
    document_id = transcript_document_id(project_id, media_id, source_id)
    current = client.documents.show(document_id, project=project_id)
    if current.ok:
        row = current.data
        if not isinstance(row, Mapping) or row.get("kind") != "media.transcript":
            raise ValueError("existing transcript document response is invalid")
        if not replace:
            return "unchanged"
        version = row.get("version")
        if type(expected_version) is not int or expected_version < 1 or expected_version != version:
            raise ValueError(f"expected version {expected_version} does not match current version {version}")
        mutation = client.documents.update(document_id, content=content,
                                           expected_version=expected_version, project=project_id)
        status = "updated"
    elif current.error and current.error.code == "not_found":
        if replace:
            raise ValueError("cannot replace a transcript that does not exist")
        mutation = client.documents.create(kind="media.transcript", content=content,
                                           project=project_id, document_id=document_id)
        status = "created"
    else:
        raise RuntimeError(f"cannot read transcript before import: {current.error}")
    if not mutation.ok:
        raise RuntimeError(f"cannot import transcript: {mutation.error}")
    return status


__all__ = ["WHISPER_SOURCE_URL", "build_whisper_transcript", "import_whisper_transcript",
           "whisper_result_digest", "whisper_source_id"]
