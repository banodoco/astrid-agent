"""Private managed Scene handoff for timeline filmstrip review."""
from __future__ import annotations

import hashlib
import io
import json
import zipfile
from collections.abc import Mapping
from copy import deepcopy
from pathlib import PurePosixPath
from types import SimpleNamespace
from typing import Any

from .exceptions import CapabilityValidationError

# Private D09 handoff.  This remains an input to the existing managed
# visualization invocation; it is not a second visualization capability.
_SCENE_ONLY_MODE = "scene_only"
_SCENE_ONLY_FORMATS = ("md",)
_SCENE_ONLY_FORBIDDEN_INPUTS = frozenset({
    "source_ref", "source", "source_path", "certification", "certificate",
    "authority", "authority_context", "provider", "store", "endpoint",
})


def _scene_only_unavailable(context: Mapping[str, Any] | None = None, *, reason: str = "scene result unavailable") -> list:
    """Return advisory-only Scene input for a missing or stale private result."""
    snapshot: Mapping[str, Any] = {}
    if isinstance(context, Mapping):
        snapshot = (context.get("capture_snapshot") or context.get("filmstrip_snapshot") or {})
    duration = snapshot.get("duration_frames") if isinstance(snapshot, Mapping) else None
    end_frame = max(1, int(duration)) if isinstance(duration, (int, float)) else 1
    return [SimpleNamespace(
        start_frame=0, end_frame=end_frame, cls="UNAVAILABLE", content=None,
        clips=(), findings=(reason,),
    )]


def _scene_only_authority(context: Mapping[str, Any]) -> dict[str, Any] | None:
    """Check the exact capture/source/map binding needed by the Scene reader."""
    snapshot = context.get("capture_snapshot") or context.get("filmstrip_snapshot")
    if not isinstance(snapshot, Mapping):
        return None
    metadata = snapshot.get("metadata")
    if not isinstance(metadata, Mapping):
        return None
    capture_identity = context.get("capture_identity")
    if not isinstance(capture_identity, str) or not capture_identity:
        return None
    if metadata.get("capture_identity") != capture_identity:
        return None
    clip_map = metadata.get("scene_clips")
    if not isinstance(clip_map, Mapping) or not clip_map:
        return None
    if not all(
        isinstance(key, str) and isinstance(value, Mapping) and isinstance(value.get("address"), str)
        for key, value in clip_map.items()
    ):
        return None
    if snapshot.get("video_digest") != capture_identity:
        return None
    capture_authority = metadata.get("capture_authority")
    if not isinstance(capture_authority, Mapping):
        return None
    expected_label = _scene_only_expected_label(snapshot)
    if expected_label is None:
        return None
    return {
        "capture_identity": context["capture_identity"],
        "scene_clips": deepcopy(dict(clip_map)),
        "snapshot_label": expected_label,
        "timeline_id": snapshot.get("timeline_id"),
    }


def _scene_only_expected_label(snapshot: Mapping[str, Any]) -> dict[str, Any] | None:
    """Project the prepared capture's admitted source kind into label fields.

    This is only a comparison target.  The SDK never turns these fields into
    authority; the executor's label in ``scene-answers.json`` must match them.
    """
    metadata = snapshot.get("metadata")
    if not isinstance(metadata, Mapping):
        return None
    capture = metadata.get("capture_authority")
    if not isinstance(capture, Mapping):
        return None
    working_copy = metadata.get("working_copy")
    if not isinstance(working_copy, Mapping):
        working_copy = capture.get("working_copy")
    if isinstance(working_copy, Mapping):
        base_parent = working_copy.get("base_parent")
        base_revision = base_parent.get("revision_id") if isinstance(base_parent, Mapping) else None
        if not isinstance(base_revision, str) or not base_revision:
            return None
        return {
            "status": "candidate",
            "label": f"unsaved candidate based on committed revision {base_revision}",
            "actual_revision_id": None,
            "base_revision_id": base_revision,
        }
    revision = capture.get("parent_revision_id")
    if not isinstance(revision, str) or not revision:
        return None
    return {
        "status": "committed",
        "label": f"committed revision {revision}",
        "actual_revision_id": revision,
        "base_revision_id": None,
    }


def _scene_only_request(
    inputs: Mapping[str, Any], *, project: str, client: Any = None,
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Prepare one private Scene-only request through the existing filmstrip authority.

    The request deliberately carries no caller source or authority object.  The
    existing ``prepare_filmstrip`` path selects the exact saved/candidate
    capture and its already-admitted managed output; the executor later owns
    the retained Root/query lifetime.
    """
    if _SCENE_ONLY_FORBIDDEN_INPUTS.intersection(inputs):
        raise CapabilityValidationError("Scene-only review accepts only managed filmstrip authority")
    request = dict(inputs)
    request.update({
        "mode": _SCENE_ONLY_MODE,
        "view": "filmstrip",
        "formats": list(_SCENE_ONLY_FORMATS),
        "composed_capture": True,
        "include_media": False,
        "every_frames": 1,
    })
    from . import timeline_filmstrip

    context = timeline_filmstrip.prepare_filmstrip(request, project=project, client=client)
    if _scene_only_authority(context) is None:
        raise CapabilityValidationError(
            "Scene-only review has no exact saved/candidate capture and managed output"
        )
    return request, context


def _scene_only_candidate_request(
    candidate: Mapping[str, Any], *, project: str, timeline_ref: str, client: Any = None,
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Freeze an authoring candidate, then use the same managed capture path."""
    from astrid.core.timeline.authoring_bundle import preview_authoring_candidate

    preview = (
        deepcopy(dict(candidate))
        if candidate.get("kind") == "authoring-candidate-preview"
        else preview_authoring_candidate(candidate)
    )
    return _scene_only_request({
        "timeline_ref": timeline_ref,
        "authoring_preview": preview,
    }, project=project, client=client)


def _scene_only_result_sources(result: Any) -> tuple[Mapping[str, Any], Mapping[str, Any]] | None:
    """Return the managed bundle row and its task artifact row, if both exist."""
    ok = result.get("ok") if isinstance(result, Mapping) else getattr(result, "ok", True)
    if ok is False:
        return None
    outputs = getattr(result, "outputs", None)
    candidates: list[object] = [outputs] if isinstance(outputs, Mapping) and outputs else [
        getattr(result, "raw_result", None),
        result if isinstance(result, Mapping) else None,
    ]
    managed: list[Mapping[str, Any]] = []
    artifacts: list[Mapping[str, Any]] = []
    seen: set[int] = set()
    while candidates:
        value = candidates.pop(0)
        if not isinstance(value, Mapping) or id(value) in seen:
            continue
        seen.add(id(value))
        rows = value.get("managed_outputs")
        if isinstance(rows, list):
            managed.extend(row for row in rows if isinstance(row, Mapping))
        rows = value.get("artifacts")
        if isinstance(rows, list):
            artifacts.extend(row for row in rows if isinstance(row, Mapping))
        for key in ("outputs", "payload", "data", "value", "result"):
            payload = value.get(key)
            if isinstance(payload, Mapping):
                candidates.append(payload)

    def is_bundle(row: Mapping[str, Any]) -> bool:
        return row.get("output_port", row.get("name")) == "filmstrip_bundle"

    managed_rows = [row for row in managed if is_bundle(row) and row.get("role", "result") == "result"]
    artifact_rows = [row for row in artifacts if is_bundle(row)]
    if len(managed_rows) != 1 or (artifact_rows and len(artifact_rows) != 1):
        return None
    managed_row = managed_rows[0]
    artifact_row = artifact_rows[0] if artifact_rows else managed_row
    managed_digest = managed_row.get("digest") or managed_row.get("object_id")
    artifact_digest = artifact_row.get("digest") or artifact_row.get("object_id") or artifact_row.get("content_hash")
    if not isinstance(managed_digest, str) or not isinstance(artifact_digest, str):
        return None
    if managed_digest.removeprefix("sha256:") != artifact_digest.removeprefix("sha256:"):
        return None
    managed_size = managed_row.get("size", managed_row.get("size_bytes"))
    artifact_size = artifact_row.get("size", artifact_row.get("bytes"))
    if artifact_rows and managed_size is not None and artifact_size is not None and managed_size != artifact_size:
        return None
    return managed_row, artifact_row


def _scene_only_bundle_payload(
    result: Any, *, client: Any = None,
) -> tuple[Mapping[str, Any], Mapping[str, Any]] | None:
    """Read the executor's verified ``filmstrip_bundle`` Scene sidecar."""
    sources = _scene_only_result_sources(result)
    if sources is None:
        return None
    managed_row, artifact_row = sources
    digest = str(managed_row.get("digest") or managed_row.get("object_id") or "")
    normalized = digest.removeprefix("sha256:")
    if len(normalized) != 64 or any(char not in "0123456789abcdef" for char in normalized):
        return None
    size = managed_row.get("size", managed_row.get("size_bytes"))
    if type(size) is not int or size <= 0:
        return None
    reader = getattr(getattr(client, "media", None), "read_bytes", None)
    if not callable(reader):
        return None
    try:
        bundle = reader("sha256:" + normalized)
    except Exception:  # noqa: BLE001 - unavailable managed output is advisory
        return None
    if not isinstance(bundle, bytes) or len(bundle) != size or hashlib.sha256(bundle).hexdigest() != normalized:
        return None
    try:
        with zipfile.ZipFile(io.BytesIO(bundle)) as archive:
            members = archive.infolist()
            names: set[str] = set()
            for member in members:
                path = PurePosixPath(member.filename)
                if (path.is_absolute() or not path.parts or ".." in path.parts
                        or "\\" in member.filename or member.filename in names
                        or (member.external_attr >> 16) & 0o170000 == 0o120000):
                    return None
                names.add(member.filename)
            manifest_name = "manifest.json"
            if manifest_name not in names:
                return None
            manifest = json.loads(archive.read(manifest_name))
            if not isinstance(manifest, Mapping) or manifest.get("kind") != "timeline_filmstrip":
                return None
            declared = manifest.get("outputs")
            if not isinstance(declared, list):
                return None
            verified: set[str] = set()
            for entry in declared:
                if not isinstance(entry, Mapping) or not isinstance(entry.get("path"), str):
                    return None
                member_name = entry["path"]
                member_path = PurePosixPath(member_name)
                if (member_path.is_absolute() or not member_path.parts or ".." in member_path.parts
                        or "\\" in member_name or member_name in verified or member_name == manifest_name
                        or member_name not in names):
                    return None
                member_bytes = archive.read(member_name)
                if (entry.get("bytes") != len(member_bytes)
                        or entry.get("content_hash") != "sha256:" + hashlib.sha256(member_bytes).hexdigest()):
                    return None
                verified.add(member_name)
            actual_files = {member.filename for member in members if not member.is_dir()}
            if actual_files != verified | {manifest_name}:
                return None
            entrypoints = manifest.get("entrypoints")
            # The current managed executor names this existing entrypoint
            # ``scene``; its stable file is still ``scene-answers.json``.
            scene_name = entrypoints.get("scene") if isinstance(entrypoints, Mapping) else None
            if scene_name != "scene-answers.json" or scene_name not in names:
                return None
            if scene_name not in verified:
                return None
            scene_bytes = archive.read(scene_name)
            payload = json.loads(scene_bytes)
    except (OSError, ValueError, TypeError, KeyError, json.JSONDecodeError, zipfile.BadZipFile):
        return None
    if not isinstance(payload, Mapping) or set(payload) != {"answers", "scene_clips", "snapshot_label"}:
        return None
    if not isinstance(managed_row.get("provenance"), Mapping) and isinstance(artifact_row.get("provenance"), Mapping):
        managed_row = {**managed_row, "provenance": artifact_row["provenance"]}
    return payload, managed_row


def _scene_only_result_payload(result: Any, *, client: Any = None) -> Mapping[str, Any] | None:
    """Read the private Scene result from the existing managed bundle."""
    source = _scene_only_bundle_payload(result, client=client)
    return source[0] if source is not None else None


def _scene_only_spans(
    result: Any, context: Mapping[str, Any], *, duration_frames: int | None = None,
    client: Any = None,
) -> list:
    """Join a bound Scene result to the existing ``_scene_problems`` span shape.

    Any missing, stale, or mismatched binding becomes one advisory UNAVAILABLE
    span.  It never turns a Scene query failure into a structural/save failure.
    """
    authority = _scene_only_authority(context)
    source = _scene_only_bundle_payload(result, client=client)
    payload = source[0] if source is not None else None
    managed_row = source[1] if source is not None else None
    if authority is None or payload is None:
        return _scene_only_unavailable(context)
    provenance = managed_row.get("provenance") if isinstance(managed_row, Mapping) else None
    if not isinstance(provenance, Mapping) or provenance.get("video_digest") != authority["capture_identity"]:
        return _scene_only_unavailable(context, reason="scene source binding unavailable")
    if authority.get("timeline_id") is not None and provenance.get("timeline_id") != authority["timeline_id"]:
        return _scene_only_unavailable(context, reason="stale scene source")
    label = payload.get("snapshot_label")
    if not isinstance(label, Mapping) or label != authority["snapshot_label"]:
        return _scene_only_unavailable(context, reason="scene snapshot label unavailable")
    if label.get("status") not in {"committed", "candidate"}:
        return _scene_only_unavailable(context, reason="scene snapshot label unavailable")
    compiled = payload.get("scene_clips")
    if not isinstance(compiled, Mapping):
        return _scene_only_unavailable(context, reason="scene address map unavailable")
    expected = authority["scene_clips"]
    actual = {}
    for key, value in compiled.items():
        if not isinstance(key, str) or not isinstance(value, Mapping) or not isinstance(value.get("address"), str):
            return _scene_only_unavailable(context, reason="scene address map invalid")
        actual[key] = dict(value)
    if actual != expected:
        return _scene_only_unavailable(context, reason="stale scene address map")
    addresses = {key: value["address"] for key, value in actual.items()}
    answers = payload.get("answers")
    if not isinstance(answers, list) or not answers:
        return _scene_only_unavailable(context)
    expected_duration = duration_frames
    if expected_duration is None:
        snapshot = context.get("capture_snapshot") or context.get("filmstrip_snapshot")
        expected_duration = snapshot.get("duration_frames") if isinstance(snapshot, Mapping) else None
    if type(expected_duration) is not int or expected_duration <= 0:
        return _scene_only_unavailable(context, reason="scene frame extent unavailable")
    if [answer.get("frame") if isinstance(answer, Mapping) else None for answer in answers] != list(range(expected_duration)):
        return _scene_only_unavailable(context, reason="scene answers incomplete")

    spans: list[SimpleNamespace] = []
    for answer in sorted(answers, key=lambda item: item.get("frame", -1) if isinstance(item, Mapping) else -1):
        if not isinstance(answer, Mapping) or type(answer.get("frame")) is not int or answer["frame"] < 0:
            return _scene_only_unavailable(context, reason="scene answers invalid")
        layers = answer.get("layers")
        if not isinstance(layers, list):
            return _scene_only_unavailable(context, reason="scene layers unavailable")
        clips = []
        for layer in layers:
            if not isinstance(layer, Mapping) or layer.get("clipId") not in actual:
                return _scene_only_unavailable(context, reason="scene layer binding unavailable")
            bounds_source = layer.get("bounds_source", layer.get("boundsSource"))
            if bounds_source is not None and not isinstance(bounds_source, str):
                return _scene_only_unavailable(context, reason="scene bounds source unavailable")
            clips.append(addresses[layer["clipId"]])
        findings = answer.get("findings") or ()
        if isinstance(findings, Mapping):
            findings = [json.dumps(dict(findings), sort_keys=True, separators=(",", ":"))]
        if not isinstance(findings, (list, tuple)):
            return _scene_only_unavailable(context, reason="scene findings invalid")
        findings = tuple(str(item) for item in findings)
        span = SimpleNamespace(
            start_frame=answer["frame"], end_frame=answer["frame"] + 1,
            cls=str(answer.get("coverage") or "UNAVAILABLE").upper(),
            content=answer.get("content"), clips=tuple(clips), findings=findings,
        )
        if spans:
            previous = spans[-1]
            if (
                previous.end_frame == span.start_frame
                and previous.cls == span.cls and previous.content == span.content
                and previous.clips == span.clips and previous.findings == span.findings
            ):
                previous.end_frame = span.end_frame
                continue
        spans.append(span)
    return spans


def _invoke_scene_only(
    inputs: Mapping[str, Any], *, project: str, client: Any = None,
) -> tuple[list, dict[str, Any]]:
    """Invoke the existing managed visualization executor in private Scene mode."""
    if client is None:
        from astrid.sdk import AstridClient
        with AstridClient.open_from_launcher(start_pack_host=False) as connected:
            return _invoke_scene_only(inputs, project=project, client=connected)
    request, context = _scene_only_request(inputs, project=project, client=client)
    from astrid.sdk.invocation import _invoke_internal_result

    result = _invoke_internal_result(
        "rendering.timeline_visualize", kind="executor", project=project,
        inputs=request, client=client, wait=True, read_managed_outputs=True,
    )
    return _scene_only_spans(result, context, client=client), context
