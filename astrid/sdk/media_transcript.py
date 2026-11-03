"""Runtime media.transcript/v1 identities, content, and document operations."""
from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Mapping, Sequence
from typing import Any

SCHEMA = "media.transcript/v1"
NARRATION_SOURCE = "astrid.narration"
WHISPER_SOURCE = "whisper"
_DOCUMENT_DIGEST = re.compile(r"sha256:[0-9a-f]{64}\Z")


def _resource_mapping(value: Any) -> Mapping[str, Any] | None:
    """Read an ordinary document resource without narrowing future fields."""
    if isinstance(value, Mapping):
        return value
    try:
        fields = vars(value)
    except TypeError:
        return None
    return fields if isinstance(fields, Mapping) else None


def _document_pin(value: Any) -> dict[str, Any] | None:
    """Preserve a server-provided pin; never derive one from document content."""
    resource = _resource_mapping(value)
    if resource is None:
        return None
    document_id = resource.get("document_id")
    version = resource.get("version")
    digest = resource.get("content_digest")
    if (not isinstance(document_id, str) or not document_id
            or type(version) is not int or version <= 0
            or not isinstance(digest, str) or _DOCUMENT_DIGEST.fullmatch(digest) is None):
        return None
    return {"document_id": document_id, "version": version, "content_digest": digest}


def transcript_document_id(project_id: str, media_id: str, source_id: str) -> str:
    identity = [SCHEMA, project_id, media_id, source_id]
    canonical = json.dumps(identity, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)
    return "transcript:" + hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def line_segment(segment_id: str, text: str, words: Sequence[Sequence[Any]]) -> dict[str, Any]:
    """One line's words in media source seconds, with repeatable IDs."""
    entries: list[dict[str, Any]] = []
    seen: dict[str, int] = {}
    for row in words:
        start, end, spoken = float(row[0]), float(row[1]), str(row[2])
        identity = json.dumps([segment_id, start, end, spoken], ensure_ascii=False, separators=(",", ":"), allow_nan=False)
        digest = hashlib.sha256(identity.encode("utf-8")).hexdigest()
        occurrence = seen.get(digest, 0)
        seen[digest] = occurrence + 1
        word_id = f"word:{digest}" + (f":{occurrence}" if occurrence else "")
        entries.append({"id": word_id, "start": start, "end": end, "text": spoken})
    if not entries:
        raise ValueError("a narration line needs timed words")
    return {"id": str(segment_id), "start": min(w["start"] for w in entries),
            "end": max(w["end"] for w in entries), "text": str(text), "words": entries}


def build_transcript(media_id: str, segment_id: str, text: str, words: Sequence[Sequence[Any]],
                     *, source_id: str = NARRATION_SOURCE) -> dict[str, Any]:
    return {"schema": SCHEMA, "media_id": media_id, "source_id": source_id,
            "segments": [line_segment(segment_id, text, words)]}


def _project_id(client: Any, project: str) -> str:
    result = client.projects.show(project)
    if not result.ok:
        raise ValueError(f"cannot resolve project {project!r}: {result.error}")
    row = result.data
    identity = row.get("project_id") or row.get("id") if isinstance(row, Mapping) else None
    if not identity:
        raise ValueError(f"project {project!r} has no Runtime identity")
    return str(identity)


def read_transcript(client: Any, project: str, media_id: str, source: str) -> Mapping[str, Any] | None:
    document, _pin = _read_transcript_result(client, project, media_id, source)
    return document


def _read_transcript_result(
    client: Any, project: str, media_id: str, source: str,
) -> tuple[Mapping[str, Any] | Any | None, dict[str, Any] | None]:
    project_id = _project_id(client, project)
    result = client.documents.show(transcript_document_id(project_id, media_id, source), project=project_id)
    if result.ok:
        return result.data, _document_pin(result.data)
    if result.error and result.error.code == "not_found":
        return None, None
    raise RuntimeError(f"cannot read transcript: {result.error}")


def write_transcript(client: Any, project: str, media_id: str, source: str,
                     content: Mapping[str, Any]) -> str:
    """Return created, updated, or unchanged; update with Runtime version CAS."""
    status, _pin = _write_transcript_result(client, project, media_id, source, content)
    return status


def _write_transcript_result(
    client: Any, project: str, media_id: str, source: str,
    content: Mapping[str, Any],
) -> tuple[str, dict[str, Any] | None]:
    """Write a transcript and retain optional server result metadata privately."""
    if content.get("schema") != SCHEMA or content.get("media_id") != media_id or content.get("source_id") != source:
        raise ValueError("transcript content identity does not match media and source")
    project_id = _project_id(client, project)
    document_id = transcript_document_id(project_id, media_id, source)
    current = client.documents.show(document_id, project=project_id)
    if current.ok:
        row = current.data
        if row.get("kind") != "media.transcript":
            raise ValueError(f"{document_id} has a different document kind")
        if row.get("content") == content:
            return "unchanged", _document_pin(row)
        result = client.documents.update(document_id, content=dict(content), expected_version=int(row["version"]),
                                         project=project_id)
        status = "updated"
    elif current.error and current.error.code == "not_found":
        result = client.documents.create(kind="media.transcript", content=dict(content), project=project_id,
                                         document_id=document_id)
        status = "created"
    else:
        raise RuntimeError(f"cannot read transcript before write: {current.error}")
    if not result.ok:
        raise RuntimeError(f"cannot write transcript: {result.error}")
    return status, _document_pin(result.data)
