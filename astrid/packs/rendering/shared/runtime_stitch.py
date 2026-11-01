"""Runtime-owned stitch finalizer admission contract.

The finalizer receives ordered Runtime CAS outputs and Runtime event-derived
dependency edges.  It only emits typed admission data; the Runtime owns
claiming, execution lifecycle, publication, and fenced settlement.

build_stitch_admission and publish_authoring_proposal are the narrow supported
internal handoff consumed by Video Editing; Runtime retains lifecycle/publication
authority and the existing attempt fence.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any

from astrid.core.contracts.runtime_orchestration import (
    CapabilityIdentity,
    OrchestrationContractError,
    RuntimeAdmission,
    SCHEMA_VERSION,
    STITCH_CAPABILITIES,
    STITCH_FAMILY,
)


def build_stitch_admission(
    *,
    project: str,
    stitch_name: str,
    stitch_digest: str,
    root_task_id: str,
    child_task_ids: Sequence[str],
    input_object_ids: Sequence[str],
    output_policy: Mapping[str, Any] | None = None,
    settlement_effect: Mapping[str, Any] | None = None,
    timeline_id: str | None = None,
    expected_version: int | None = None,
    render: Mapping[str, Any] | None = None,
    idempotency_key: str,
) -> RuntimeAdmission:
    """Create the finalizer body with Runtime event dependencies."""

    if stitch_name not in STITCH_CAPABILITIES:
        raise OrchestrationContractError(f"unknown stitch finalizer {stitch_name!r}")
    root_task_id = _string(root_task_id, "root_task_id")
    child_task_ids = tuple(_string(value, f"child_task_ids[{i}]") for i, value in enumerate(child_task_ids))
    input_object_ids = tuple(_string(value, f"input_object_ids[{i}]") for i, value in enumerate(input_object_ids))
    if len(set(child_task_ids)) != len(child_task_ids):
        raise OrchestrationContractError("child_task_ids must be unique and ordered")
    if len(set(input_object_ids)) != len(input_object_ids):
        raise OrchestrationContractError("input_object_ids must be unique and ordered")
    identity = CapabilityIdentity(STITCH_CAPABILITIES[stitch_name], stitch_digest)
    publication: dict[str, Any] = {}
    if timeline_id is not None or expected_version is not None or render is not None:
        if timeline_id is None or expected_version is None:
            raise OrchestrationContractError(
                "timeline_id and expected_version are required for Runtime publication settings"
            )
        if isinstance(expected_version, bool) or not isinstance(expected_version, int) or expected_version < 1:
            raise OrchestrationContractError("expected_version must be a positive integer")
        publication = {
            "timeline_id": _string(timeline_id, "timeline_id"),
            "expected_version": expected_version,
            "render": dict(render or {
                "capability_id": "rendering.render",
                "capability_digest": identity.capability_digest,
                "schema_version": SCHEMA_VERSION,
                "spec": {"capability_id": "rendering.render", "kind": "executor", "inputs": {}, "outputs": {}},
            }),
        }
    edges = [
        {"from_task_id": task_id, "to": "self", "requires_event": "task.succeeded", "fence": "runtime_task"}
        for task_id in child_task_ids
    ]
    return RuntimeAdmission(
        body={
            "project": _string(project, "project"),
            "capability_id": identity.capability_id,
            "capability_digest": identity.capability_digest,
            "schema_version": SCHEMA_VERSION,
            "input_object_ids": [],
            "spec": {
                "family": STITCH_FAMILY,
                "params": {"root_task_id": root_task_id, "child_task_ids": list(child_task_ids)},
                "output_policy": dict(output_policy or {}),
                "runtime_dependencies": {
                    "edges": edges,
                    "event_source": "runtime.events",
                    "aggregation": {
                        "kind": "ordered_cas_inputs",
                        "order": list(input_object_ids),
                    },
                    **({"publication": publication} if publication else {}),
                },
            },
            "storage_estimate": {"estimated_scratch_bytes": 0, "estimated_output_bytes": 0},
            "settlement_effect": _typed_settlement_effect(settlement_effect),
        },
        idempotency_key=idempotency_key,
    )


def publish_authoring_proposal(
    client: Any,
    *,
    proposal: Mapping[str, Any],
    timeline_id: str,
    expected_version: int,
    runtime_attempt: Mapping[str, Any],
    render_capability_digest: str,
    output_name: str = "hype.mp4",
    selector: str = "rendering.ffmpeg",
    idempotency_key: str,
) -> Mapping[str, Any]:
    """Commit an ``assemble_timeline`` proposal through Runtime's checkpoint.

    This is the trusted host boundary after the result-only authoring executor.
    It deliberately has no timeline-save or invoke-render fallback: the
    Runtime receives the authoring attempt fence and creates the canonical
    revision and ``rendering.render`` task in one transaction.
    """
    if not isinstance(proposal, Mapping):
        raise OrchestrationContractError("timeline authoring proposal must be an object")
    timeline = proposal.get("timeline")
    registry = proposal.get("registry")
    if not isinstance(timeline, Mapping) or not isinstance(registry, Mapping):
        raise OrchestrationContractError("timeline authoring proposal must contain timeline and registry objects")
    publication = proposal.get("publication")
    if not isinstance(publication, Mapping) or publication.get("authority") != "workspace_runtime":
        raise OrchestrationContractError("timeline authoring proposal is not Runtime-owned")
    if publication.get("render_capability") != "rendering.render":
        raise OrchestrationContractError("timeline authoring proposal must use rendering.render")

    context = _runtime_attempt(runtime_attempt)
    if isinstance(expected_version, bool) or not isinstance(expected_version, int) or expected_version < 1:
        raise OrchestrationContractError("expected_version must be a positive integer")
    _string(timeline_id, "timeline_id")
    _string(output_name, "output_name")
    _string(selector, "selector")
    identity = CapabilityIdentity("rendering.render", render_capability_digest)

    tasks = getattr(client, "tasks", None)
    publish = getattr(tasks, "publish_timeline_render", None)
    if not callable(publish):
        raise OrchestrationContractError("Runtime client does not expose tasks.publish_timeline_render")
    result = publish(
        context["attempt_id"],
        {
            "lease_id": context["lease_id"],
            "fence": context["fence"],
            "runtime_epoch": context["runtime_epoch"],
            "timeline_id": timeline_id,
            "expected_version": expected_version,
            "config": dict(timeline),
            "registry": dict(registry),
            "render": {
                "capability_id": identity.capability_id,
                "capability_digest": identity.capability_digest,
                "schema_version": "1",
                "spec": {
                    "capability_id": identity.capability_id,
                    "kind": "executor",
                    "inputs": {"selector": selector, "output_name": output_name},
                    "outputs": {},
                },
            },
        },
        idempotency_key=idempotency_key,
    )
    if hasattr(result, "ok"):
        if not bool(result.ok):
            error = getattr(result, "error", None)
            raise OrchestrationContractError(str(error or "Runtime timeline publication failed"))
        result = getattr(result, "data", None)
    if not isinstance(result, Mapping):
        raise OrchestrationContractError("Runtime timeline publication returned an invalid result")
    return dict(result)


def _runtime_attempt(value: Mapping[str, Any]) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        raise OrchestrationContractError("runtime_attempt is required")
    required = ("attempt_id", "lease_id", "fence", "runtime_epoch")
    missing = [key for key in required if key not in value]
    if missing:
        raise OrchestrationContractError(
            f"runtime_attempt is missing required fence fields: {', '.join(missing)}"
        )
    result = {key: value[key] for key in required}
    for key in ("attempt_id", "lease_id"):
        _string(result[key], f"runtime_attempt.{key}")
    for key in ("fence", "runtime_epoch"):
        if isinstance(result[key], bool) or not isinstance(result[key], int) or result[key] < 1:
            raise OrchestrationContractError(f"runtime_attempt.{key} must be a positive integer")
    return result


def _string(value: Any, field: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise OrchestrationContractError(f"{field} must be a non-empty string")
    return value


def _typed_settlement_effect(value: Mapping[str, Any] | None) -> dict[str, Any]:
    if not isinstance(value, Mapping) or not value:
        raise OrchestrationContractError("settlement_effect must be a non-empty typed settlement effect")
    return dict(value)


__all__ = ["build_stitch_admission", "publish_authoring_proposal"]
