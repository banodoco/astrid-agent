#!/usr/bin/env python3
"""Build a canonical timeline proposal from a runtime continuation.

This executor is deliberately result-only.  It consumes the Runtime's resolved
terminal children and emits a proposal for the Runtime's timeline save and
render admissions.  It does not open a local database, read event logs, poll,
or mutate a timeline.
"""

# The canonical-entrypoint guard intentionally runs before imports.
# ruff: noqa: E402

from __future__ import annotations

from astrid.core.pack.entrypoint import guard_canonical_entrypoint

guard_canonical_entrypoint("rendering.assemble_timeline")

import argparse
import hashlib
import json
import math
import re
from collections.abc import Mapping, Sequence
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from astrid.core._shared.result_manifest import build_manifest, write_manifest
from astrid.core.foundation.atomic_io import write_json_atomic
from astrid.core.pack.entrypoint import run_pack_main
from astrid.core.timeline import canonical_timeline_config
from astrid.core.timeline.validators.registry import validate_registry


class TimelineAuthoringError(ValueError):
    """A runtime continuation cannot be safely authored as a timeline."""


_VIDEO_MIME_RE = re.compile(r"^video/[a-z0-9.+-]+$")
_DIGEST_RE = re.compile(r"^(?:sha256:)?([0-9a-f]{64})$")


def _text(value: Any, field: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise TimelineAuthoringError(f"{field} must be a non-empty string")
    return value.strip()


def _finite_positive(value: Any, field: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise TimelineAuthoringError(f"{field} must be a positive finite number")
    result = float(value)
    if not math.isfinite(result) or result <= 0:
        raise TimelineAuthoringError(f"{field} must be a positive finite number")
    return result


def _mapping(value: Any, field: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise TimelineAuthoringError(f"{field} must be an object")
    return value


def _list(value: Any, field: str) -> list[Any]:
    if isinstance(value, (str, bytes)) or not isinstance(value, Sequence):
        raise TimelineAuthoringError(f"{field} must be an array")
    return list(value)


def _find_mapping(root: Mapping[str, Any], key: str) -> Mapping[str, Any] | None:
    """Find a named mapping in the bounded Runtime handoff envelope."""
    candidate = root.get(key)
    if isinstance(candidate, Mapping):
        return candidate
    for value in root.values():
        if isinstance(value, Mapping):
            candidate = _find_mapping(value, key)
            if candidate is not None:
                return candidate
    return None


def _find_list(root: Mapping[str, Any], key: str) -> list[Any] | None:
    candidate = root.get(key)
    if isinstance(candidate, Sequence) and not isinstance(candidate, (str, bytes)):
        return list(candidate)
    for value in root.values():
        if isinstance(value, Mapping):
            candidate = _find_list(value, key)
            if candidate is not None:
                return candidate
    return None


def _find_scalar(root: Mapping[str, Any], key: str) -> Any:
    """Find a scalar in the bounded Runtime claim envelope."""
    if key in root and (isinstance(root[key], str) or not isinstance(root[key], (Mapping, Sequence))):
        return root[key]
    for value in root.values():
        if isinstance(value, Mapping):
            candidate = _find_scalar(value, key)
            if candidate is not None:
                return candidate
    return None


def _continuation_id(payload: Mapping[str, Any], explicit: str | None) -> str:
    if explicit is not None:
        return _text(explicit, "continuation_id")
    for key in ("continuation_id", "continuationId", "stitch_task_id", "task_id", "id"):
        if payload.get(key) is not None:
            return _text(payload[key], f"continuation.{key}")
    nested = payload.get("continuation")
    if isinstance(nested, Mapping):
        for key in ("continuation_id", "continuationId", "stitch_task_id", "task_id", "id"):
            if nested.get(key) is not None:
                return _text(nested[key], f"continuation.{key}")
    raise TimelineAuthoringError("continuation identity is required")


def _edge_task_id(edge: Mapping[str, Any], index: int) -> str:
    for key in ("from_task_id", "task_id", "from"):
        if edge.get(key) is not None:
            value = edge[key]
            if isinstance(value, str) and value.startswith("child:"):
                raise TimelineAuthoringError(
                    f"continuation.edges[{index}] must identify a concrete child task id"
                )
            return _text(value, f"continuation.edges[{index}].{key}")
    raise TimelineAuthoringError(f"continuation.edges[{index}] is missing a child task id")


def _validate_graph(payload: Mapping[str, Any]) -> tuple[list[Mapping[str, Any]], list[str]]:
    family = payload.get("family")
    if family is None:
        spec = payload.get("spec")
        family = spec.get("family") if isinstance(spec, Mapping) else None
    if family is None:
        graph = payload.get("graph")
        family = graph.get("family") if isinstance(graph, Mapping) else None
    # A claimed Runtime task carries the capability id beside the admitted
    # spec, while the family is not repeated in the continuation snapshot.
    # Infer the one supported family only for our registered capability.
    if family is None and _find_scalar(payload, "capability_id") == "rendering.assemble_timeline":
        family = "stitch_finalization"
    if family != "stitch_finalization":
        raise TimelineAuthoringError(
            "unsupported continuation graph family; expected 'stitch_finalization'"
        )

    edges = _find_list(payload, "edges")
    if edges is None:
        raise TimelineAuthoringError("continuation is missing runtime dependency edges")
    if len(edges) != 2:
        raise TimelineAuthoringError("only a continuation with exactly two dependency edges is supported")
    normalized_edges: list[Mapping[str, Any]] = []
    task_ids: list[str] = []
    for index, raw_edge in enumerate(edges):
        edge = _mapping(raw_edge, f"continuation.edges[{index}]")
        event = edge.get("requires_event", edge.get("event", edge.get("event_type")))
        if event != "task.succeeded":
            raise TimelineAuthoringError(
                f"continuation.edges[{index}] must require task.succeeded"
            )
        fence = edge.get("fence")
        if fence not in {"runtime_task", "task", "child_task"}:
            raise TimelineAuthoringError(
                f"continuation.edges[{index}] must carry a runtime task fence"
            )
        task_id = _edge_task_id(edge, index)
        if task_id in task_ids:
            raise TimelineAuthoringError("continuation dependency edges must identify distinct child tasks")
        task_ids.append(task_id)
        normalized_edges.append(edge)

    aggregation = _find_mapping(payload, "aggregation")
    if aggregation is None:
        aggregation = _find_mapping(payload, "ordered_cas_inputs")
    if aggregation is None:
        raise TimelineAuthoringError("continuation is missing ordered_cas_inputs aggregation")
    kind = aggregation.get("kind")
    if kind != "ordered_cas_inputs":
        raise TimelineAuthoringError("unsupported continuation aggregation; expected ordered_cas_inputs")
    # The Runtime admission snapshot omits aggregation.order: its
    # authoritative ordered CAS list is materialized as input_object_ids on
    # the claimed task. Older handoffs may still carry order/object_ids.
    ordered = aggregation.get("order", aggregation.get("object_ids"))
    if ordered is None:
        ordered = _find_list(payload, "input_object_ids")
    object_order = [] if ordered is None else [_text(value, f"ordered_cas_inputs.order[{index}]") for index, value in enumerate(_list(ordered, "ordered_cas_inputs.order"))]
    if object_order and len(object_order) != 2:
        raise TimelineAuthoringError("ordered_cas_inputs must contain exactly two object references")
    return normalized_edges, object_order


def _output_identity(output: Mapping[str, Any], field: str) -> str:
    for key in ("object_id", "objectId", "cas_id", "cas_object_id", "digest", "object_digest", "media_id", "mediaId", "id"):
        if output.get(key) is not None:
            return _text(output[key], f"{field}.{key}")
    raise TimelineAuthoringError(f"{field} is missing a runtime object identity")


def _output_digest(output: Mapping[str, Any], field: str) -> str:
    raw = next((output.get(key) for key in ("content_sha256", "contentSha256", "content_hash", "sha256", "digest", "object_digest") if output.get(key) is not None), None)
    if raw is None and isinstance(output.get("object_id"), str) and output["object_id"].startswith("sha256:"):
        raw = output["object_id"]
    value = _text(raw, f"{field}.content_sha256")
    match = _DIGEST_RE.fullmatch(value.lower())
    if match is None:
        raise TimelineAuthoringError(f"{field}.content_sha256 must be a sha256 digest")
    return match.group(1)


def _output_media_type(output: Mapping[str, Any], field: str) -> str:
    raw = next((output.get(key) for key in ("media_type", "mime_type", "mime", "type") if output.get(key) is not None), None)
    media_type = _text(raw, f"{field}.media_type").lower()
    if not _VIDEO_MIME_RE.fullmatch(media_type):
        raise TimelineAuthoringError(
            f"{field} has unsupported media type {media_type!r}; primary visuals must be video/*"
        )
    return media_type


def _output_duration(output: Mapping[str, Any], field: str, child: Mapping[str, Any] | None = None) -> float:
    sources: list[tuple[Mapping[str, Any], str]] = [(output, "")]
    metadata = output.get("metadata")
    if isinstance(metadata, Mapping):
        sources.append((metadata, "metadata."))
    if isinstance(child, Mapping):
        sources.append((child, "child."))
        child_metadata = child.get("metadata")
        if isinstance(child_metadata, Mapping):
            sources.append((child_metadata, "child.metadata."))
    for source, prefix in sources:
        for key in ("duration", "duration_seconds", "duration_sec", "duration_s", "hold"):
            if source.get(key) is not None:
                return _finite_positive(source[key], f"{field}.{prefix}{key}")
    source_from, source_to = output.get("from"), output.get("to")
    if isinstance(source_from, (int, float)) and not isinstance(source_from, bool) and isinstance(source_to, (int, float)) and not isinstance(source_to, bool):
        duration = float(source_to) - float(source_from)
        return _finite_positive(duration, f"{field}.to-from")
    raise TimelineAuthoringError(f"{field} is missing a positive duration")


def _candidate_outputs(child: Mapping[str, Any], child_field: str) -> list[Mapping[str, Any]]:
    raw_outputs = None
    for key in ("outputs", "resolved_outputs", "result_outputs"):
        if child.get(key) is not None:
            raw_outputs = child[key]
            break
    outputs = _list(raw_outputs, f"{child_field}.outputs") if raw_outputs is not None else []
    candidates: list[Mapping[str, Any]] = []
    for index, raw_output in enumerate(outputs):
        output = _mapping(raw_output, f"{child_field}.outputs[{index}]")
        role = output.get("role", output.get("output_role", output.get("kind")))
        metadata = output.get("metadata")
        if role is None and isinstance(metadata, Mapping):
            role = metadata.get("role")
        if role in {"primary_visual", "primary"}:
            if output.get("is_primary") is False:
                continue
            if isinstance(metadata, Mapping) and metadata.get("status") == "superseded":
                continue
            candidates.append(output)

    direct = child.get("primary_visual")
    if direct is not None:
        if isinstance(direct, Mapping):
            if direct.get("is_primary") is False:
                raise TimelineAuthoringError(f"{child_field}.primary_visual is explicitly non-primary")
            direct_metadata = direct.get("metadata")
            if isinstance(direct_metadata, Mapping) and direct_metadata.get("status") == "superseded":
                raise TimelineAuthoringError(f"{child_field}.primary_visual is superseded")
            candidates.append(direct)
        else:
            direct_id = _text(direct, f"{child_field}.primary_visual")
            matches = [output for output in outputs if _output_identity(output, f"{child_field}.outputs") == direct_id]
            if len(matches) != 1:
                raise TimelineAuthoringError(f"{child_field}.primary_visual does not identify exactly one output")
            candidates.append(matches[0])
    if len(candidates) != 1:
        raise TimelineAuthoringError(
            f"{child_field} must identify exactly one primary_visual output; found {len(candidates)}"
        )
    return candidates


def _child_ordinal(child: Mapping[str, Any], field: str) -> int:
    raw = next((child.get(key) for key in ("ordinal", "index", "child_ordinal") if child.get(key) is not None), None)
    if isinstance(raw, bool) or not isinstance(raw, int) or raw not in (0, 1):
        raise TimelineAuthoringError(f"{field}.ordinal must be 0 or 1")
    return raw


def derive_clip_id(continuation_id: str, ordinal: int) -> str:
    """Derive an opaque, stable clip id from immutable continuation identity."""
    identity = f"{_text(continuation_id, 'continuation_id')}:{ordinal}"
    digest = hashlib.sha256(identity.encode("utf-8")).hexdigest()[:24]
    return f"continuation-{digest}-clip-{ordinal}"


def derive_asset_key(continuation_id: str, ordinal: int) -> str:
    identity = f"{_text(continuation_id, 'continuation_id')}:{ordinal}"
    digest = hashlib.sha256(identity.encode("utf-8")).hexdigest()[:24]
    return f"continuation-{digest}-asset-{ordinal}"


def build_authoring_proposal(
    resolved_children: Mapping[str, Any],
    *,
    continuation_id: str | None = None,
) -> dict[str, Any]:
    """Validate and build a deterministic Runtime timeline authoring proposal."""
    payload = _mapping(resolved_children, "resolved_children")
    identity = _continuation_id(payload, continuation_id)
    edges, ordered_objects = _validate_graph(payload)

    raw_children = _find_list(payload, "resolved_children")
    if raw_children is None:
        raw_children = payload.get("children")
    children = [_mapping(value, f"resolved_children[{index}]") for index, value in enumerate(_list(raw_children, "resolved_children"))]
    if len(children) != 2:
        raise TimelineAuthoringError("resolved_children must contain exactly two children")
    by_ordinal: dict[int, Mapping[str, Any]] = {}
    for index, child in enumerate(children):
        ordinal = _child_ordinal(child, f"resolved_children[{index}]")
        if ordinal in by_ordinal:
            raise TimelineAuthoringError("resolved_children must contain one child at each ordinal 0 and 1")
        by_ordinal[ordinal] = child
    if set(by_ordinal) != {0, 1}:
        raise TimelineAuthoringError("resolved_children must contain ordinals 0 and 1")

    # With the live Runtime claim envelope, ``aggregation`` is only a kind
    # marker and the ordered CAS ids live in ``input_object_ids``. If even
    # that transport field was omitted, ordinal-bound child outputs are the
    # only safe deterministic source of order.
    if not ordered_objects:
        derived_order: list[str] = []
        for ordinal in (0, 1):
            child = by_ordinal[ordinal]
            output = _candidate_outputs(child, f"resolved_children[{ordinal}]")[0]
            derived_order.append(_output_identity(output, f"resolved_children[{ordinal}].primary_visual"))
        ordered_objects = derived_order
    if len(ordered_objects) != 2:
        raise TimelineAuthoringError("ordered_cas_inputs must contain exactly two object references")

    clips: list[dict[str, Any]] = []
    assets: dict[str, dict[str, Any]] = {}
    seen_child_task_ids: set[str] = set()
    offset = 0.0
    for ordinal in (0, 1):
        child = by_ordinal[ordinal]
        child_task_id = next((child.get(key) for key in ("task_id", "taskId", "child_task_id", "childTaskId", "id") if child.get(key) is not None), None)
        child_task_id = _text(child_task_id, f"resolved_children[{ordinal}].task_id")
        edge_task_ids = [_edge_task_id(edge, index) for index, edge in enumerate(edges)]
        if child_task_id not in edge_task_ids:
            raise TimelineAuthoringError(f"resolved_children[{ordinal}] is not bound to a fenced dependency edge")
        if child_task_id in seen_child_task_ids:
            raise TimelineAuthoringError("resolved_children must identify distinct child tasks")
        seen_child_task_ids.add(child_task_id)
        output = _candidate_outputs(child, f"resolved_children[{ordinal}]")[0]
        object_id = _output_identity(output, f"resolved_children[{ordinal}].primary_visual")
        if object_id != ordered_objects[ordinal]:
            raise TimelineAuthoringError(
                f"resolved_children[{ordinal}] primary visual does not match ordered_cas_inputs[{ordinal}]"
            )
        digest = _output_digest(output, f"resolved_children[{ordinal}].primary_visual")
        media_type = _output_media_type(output, f"resolved_children[{ordinal}].primary_visual")
        duration = _output_duration(output, f"resolved_children[{ordinal}].primary_visual", child)
        asset_key = derive_asset_key(identity, ordinal)
        media_id = next((output.get(key) for key in ("media_id", "mediaId") if output.get(key) is not None), object_id)
        assets[asset_key] = {
            "media_id": _text(media_id, f"resolved_children[{ordinal}].primary_visual.media_id"),
            "content_sha256": digest,
            "type": media_type,
            "duration": duration,
            "origin": "immutable-public",
        }
        clips.append({
            "id": derive_clip_id(identity, ordinal),
            "at": offset,
            "track": "visual",
            "clipType": "media",
            "asset": asset_key,
            "from": 0,
            "to": duration,
        })
        offset += duration

    config = canonical_timeline_config({
        "tracks": [{"id": "visual", "kind": "visual", "label": "Generated visuals"}],
        "clips": clips,
    })
    registry = {"assets": assets}
    validate_registry(registry)
    return {
        "schema_version": 1,
        "capability_id": "rendering.assemble_timeline",
        "continuation_id": identity,
        "timeline": config,
        "registry": registry,
        "children": [
            {"ordinal": ordinal, "task_id": _text(next((by_ordinal[ordinal].get(key) for key in ("task_id", "taskId", "child_task_id", "childTaskId", "id") if by_ordinal[ordinal].get(key) is not None), None), f"resolved_children[{ordinal}].task_id"), "object_id": ordered_objects[ordinal], "clip_id": clips[ordinal]["id"]}
            for ordinal in (0, 1)
        ],
        "publication": {
            "authority": "workspace_runtime",
            "mutation": "authoring-bundle validate/commit",
            "expected_head": "runtime-current",
            "render_capability": "rendering.render",
        },
    }


def assemble_timeline(
    resolved_children: Mapping[str, Any], *, continuation_id: str | None = None
) -> dict[str, Any]:
    """Compatibility name for callers describing this operation as assembly."""
    return build_authoring_proposal(resolved_children, continuation_id=continuation_id)


def build_canonical_timeline(
    resolved_children: Mapping[str, Any], *, continuation_id: str | None = None
) -> dict[str, Any]:
    """Return the canonical timeline portion of an authoring proposal."""
    return build_authoring_proposal(resolved_children, continuation_id=continuation_id)["timeline"]


def _load_json_argument(value: str) -> Mapping[str, Any]:
    candidate = Path(value).expanduser()
    if candidate.is_file():
        try:
            decoded = json.loads(candidate.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise TimelineAuthoringError(f"failed to read resolved_children JSON: {exc}") from exc
    else:
        try:
            decoded = json.loads(value)
        except json.JSONDecodeError as exc:
            raise TimelineAuthoringError("--resolved-children must be JSON or a JSON file path") from exc
    return _mapping(decoded, "resolved_children")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Author a canonical timeline from runtime-resolved child outputs.")
    parser.add_argument("--resolved-children", required=True, help="Runtime continuation JSON or staged JSON path.")
    parser.add_argument("--continuation-id", default=None, help="Optional explicit continuation identity.")
    parser.add_argument("--out", type=Path, required=True, help="Output directory.")
    return parser


def main(argv: list[str] | None = None) -> int:
    def _run() -> int:
        args = build_parser().parse_args(argv)
        args.out.mkdir(parents=True, exist_ok=True)
        payload = _load_json_argument(args.resolved_children)
        proposal = build_authoring_proposal(payload, continuation_id=args.continuation_id)
        timeline_path = args.out / "timeline.json"
        assets_path = args.out / "assets.json"
        proposal_path = args.out / "authoring-proposal.json"
        write_json_atomic(timeline_path, proposal["timeline"])
        write_json_atomic(assets_path, proposal["registry"])
        write_json_atomic(proposal_path, proposal)
        manifest = build_manifest(
            kind="rendering.assemble_timeline",
            inputs={"resolved_children": args.resolved_children, "continuation_id": proposal["continuation_id"]},
            outputs=[
                {"name": "timeline", "path": timeline_path.name, "type": "file", "artifact_type": "timeline"},
                {"name": "assets", "path": assets_path.name, "type": "file", "artifact_type": "timeline/assets"},
                {"name": "authoring_proposal", "path": proposal_path.name, "type": "file", "artifact_type": "timeline/authoring-proposal"},
            ],
            created=datetime.now(timezone.utc).isoformat(),
            continuation_id=proposal["continuation_id"],
        )
        write_manifest(args.out / "manifest.json", manifest)
        return 0

    return run_pack_main("rendering.assemble_timeline", _run, argv=argv)


__all__ = [
    "TimelineAuthoringError",
    "assemble_timeline",
    "build_authoring_proposal",
    "build_canonical_timeline",
    "build_parser",
    "derive_asset_key",
    "derive_clip_id",
    "main",
]


if __name__ == "__main__":
    raise SystemExit(main())
