"""Exact retained source inputs for attached native timeline commands."""
from __future__ import annotations

import copy
import hashlib
import json
import math
from collections.abc import Mapping
from typing import Any

from astrid.sdk.media_transcript import SCHEMA, transcript_document_id

from ._common import TimelineEditError


def _canonical_digest(value: Any) -> str:
    try:
        encoded = json.dumps(value, sort_keys=True, separators=(",", ":"),
                            ensure_ascii=False, allow_nan=False).encode("utf-8")
    except (TypeError, ValueError, UnicodeEncodeError) as exc:
        raise TimelineEditError("verified transcript content is not canonical JSON") from exc
    return "sha256:" + hashlib.sha256(encoded).hexdigest()


def _transcript_number(value: Any, label: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        raise TimelineEditError(f"verified transcript {label} must be a finite number")
    if value < 0:
        raise TimelineEditError(f"verified transcript {label} must not be negative")
    return float(value)


def _validate_transcript_v1(project: str, pin: Mapping[str, Any], document: Mapping[str, Any]) -> None:
    content = document.get("content")
    if (document.get("kind") != "media.transcript"
            or not isinstance(content, Mapping)
            or set(content) != {"schema", "media_id", "source_id", "segments"}):
        raise TimelineEditError("verified document is not a canonical media.transcript/v1 document")
    if content.get("schema") != SCHEMA or not isinstance(content.get("media_id"), str):
        raise TimelineEditError("verified document is not a canonical media.transcript/v1 document")
    source_id = content.get("source_id")
    if not isinstance(source_id, str) or not source_id:
        raise TimelineEditError("verified transcript source identity is invalid")
    if document["document_id"] != transcript_document_id(project, content["media_id"], source_id):
        raise TimelineEditError("verified transcript document identity does not match its source")
    segments = content.get("segments")
    if not isinstance(segments, list):
        raise TimelineEditError("verified transcript segments must be an array")
    segment_ids, word_ids = set(), set()
    for segment in segments:
        if (not isinstance(segment, dict)
                or set(segment) - {"id", "start", "end", "text", "speaker", "words"}
                or not {"id", "start", "end", "text"}.issubset(segment)):
            raise TimelineEditError("verified transcript segment has an invalid shape")
        segment_id = segment["id"]
        if not isinstance(segment_id, str) or not segment_id or segment_id in segment_ids:
            raise TimelineEditError("verified transcript segment identity is invalid or duplicated")
        segment_ids.add(segment_id)
        start, end = (_transcript_number(segment["start"], "segment start"),
                      _transcript_number(segment["end"], "segment end"))
        if end < start or not isinstance(segment["text"], str):
            raise TimelineEditError("verified transcript segment interval or text is invalid")
        if "speaker" in segment and segment["speaker"] is not None and not isinstance(segment["speaker"], str):
            raise TimelineEditError("verified transcript speaker is invalid")
        words = segment.get("words", [])
        if not isinstance(words, list):
            raise TimelineEditError("verified transcript words must be an array")
        for word in words:
            if not isinstance(word, dict) or set(word) != {"id", "start", "end", "text"}:
                raise TimelineEditError("verified transcript word has an invalid shape")
            word_id = word["id"]
            if not isinstance(word_id, str) or not word_id or word_id in word_ids:
                raise TimelineEditError("verified transcript word identity is invalid or duplicated")
            word_ids.add(word_id)
            word_start, word_end = (_transcript_number(word["start"], "word start"),
                                    _transcript_number(word["end"], "word end"))
            if (word_end < word_start or word_start < start or word_end > end
                    or not isinstance(word["text"], str)):
                raise TimelineEditError("verified transcript word interval or text is invalid")


def read_verified_transcript(client: Any, project: str, pin: Mapping[str, Any]) -> dict[str, Any]:
    """Read and validate one exact retained transcript pin through Runtime."""
    if not isinstance(pin, Mapping) or set(pin) != {"document_id", "version", "content_digest"}:
        raise TimelineEditError("word anchor requires an exact DocumentPin")
    exact_pin = dict(pin)
    try:
        verified = client.get_project_verified_document(project, exact_pin)
    except Exception as exc:
        if getattr(exc, "code", None) == "not_found":
            raise TimelineEditError("exact transcript pin is missing or unavailable") from exc
        raise TimelineEditError(f"exact retained transcript read failed: {exc}") from exc
    if not isinstance(verified, Mapping) or verified.get("pin") != exact_pin:
        raise TimelineEditError("verified transcript read returned a different DocumentPin")
    document = verified.get("document")
    if not isinstance(document, Mapping):
        raise TimelineEditError("verified transcript read did not return a document")
    if (document.get("project_id") != project
            or document.get("document_id") != exact_pin["document_id"]
            or document.get("version") != exact_pin["version"]):
        raise TimelineEditError("verified transcript document scope or version does not match its pin")
    content = document.get("content")
    if _canonical_digest(content) != exact_pin["content_digest"]:
        raise TimelineEditError("verified transcript content bytes do not match its pin")
    _validate_transcript_v1(project, exact_pin, document)
    return {"pin": exact_pin, "document": copy.deepcopy(dict(document))}


def materialize_transcript_inputs(bundle: dict[str, Any], commands: list[Mapping[str, Any]], *,
                                  client: Any, project: str) -> dict[str, Any]:
    """Hydrate exact transcript captures needed by pending or new anchors."""
    pins: list[dict[str, Any]] = []
    for command in commands:
        if command.get("op") != "clip.anchor":
            continue
        choice = (command.get("args") or {}).get("choice")
        if not isinstance(choice, Mapping):
            continue
        pin = choice.get("document")
        if isinstance(pin, Mapping) and dict(pin) not in pins:
            pins.append(dict(pin))
    if not pins:
        return bundle
    mapping = bundle.setdefault("source_mapping", {})
    captures = mapping.setdefault("pinned_documents", [])
    for pin in pins:
        capture = read_verified_transcript(client, project, pin)
        captures[:] = [row for row in captures if not isinstance(row, Mapping) or row.get("pin") != pin]
        captures.append(capture)
    return bundle
