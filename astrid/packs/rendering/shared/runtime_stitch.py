"""Runtime-owned stitch finalizer admission contract.

The finalizer receives ordered Runtime CAS outputs and Runtime event-derived
dependency edges.  It only emits typed admission data; the Runtime owns
claiming, execution lifecycle, commit acceptance, and fenced settlement.

build_stitch_admission and publish_authoring_proposal are the narrow supported
internal handoff consumed by Video Editing. The requester commits the settled
native proposal before admitting an exact-revision render.
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
    runtime_attempt: Mapping[str, Any] | None = None,
    render_capability_digest: str,
    output_name: str = "hype.mp4",
    selector: str = "rendering.ffmpeg",
    idempotency_key: str,
    requester_session=None,
    label="Assemble timeline", steps=None, merge="fields", reads=None, dry_run=False,
    group_id=None,
) -> Mapping[str, Any]:
    """Requester commit followed by ordinary, exact-revision render admission.

    The worker only supplies a settled managed proposal. Runtime authenticates its
    producer guard; the requester remains the publisher and renderer task owner.
    """
    if not isinstance(proposal, Mapping):
        raise OrchestrationContractError("timeline authoring proposal must be an object")
    if requester_session is None:
        raise OrchestrationContractError("native assembly publication requires its original requester session")
    target = proposal.get("admitted_target", {})
    if (target.get("project_id"), target.get("timeline_id")) != (
            requester_session.project_id, requester_session.timeline_id):
        raise OrchestrationContractError("assembly proposal belongs to a different requester scope")
    if client is not requester_session.client or timeline_id != requester_session.timeline_id:
        raise OrchestrationContractError("assembly publication requires the original requester client and timeline")
    _string(output_name, "output_name")
    _string(selector, "selector")
    identity = CapabilityIdentity("rendering.render", render_capability_digest)
    producer = proposal.get("producer", {})
    if runtime_attempt is not None and any(
            runtime_attempt.get(key) != producer.get(key) for key in ("attempt_id", "fence", "runtime_epoch")):
        raise OrchestrationContractError("assembly publication attempt fence changed")
    result = client.tasks.publish_timeline_render(
        producer.get("attempt_id"), proposal, requester_session=requester_session,
        idempotency_key=idempotency_key, label=label, steps=steps, merge=merge,
        reads=reads, group_id=group_id, dry_run=dry_run,
    )
    if not result.ok:
        from astrid.sdk.workspace_client import WorkspaceClientError

        error = result.error
        raise WorkspaceClientError(0, error.code, error.message, dict(error.details))
    envelope = result.as_dict()
    if dry_run:
        return envelope
    data = result.data
    parent = data["canonical_document"]["parent_revision"]
    render = client.tasks.create(
        project_id=requester_session.project_id, capability=identity.capability_id,
        capability_digest=identity.capability_digest, input_manifest=[],
        spec={"capability_id": identity.capability_id, "kind": "executor", "outputs": {},
              "params": {"timeline_ref": requester_session.timeline_id,
                         "canonical_revision": {
                             "project_id": requester_session.project_id,
                             "timeline_id": requester_session.timeline_id,
                             "revision_id": data["accepted_revision_id"],
                             "publication_id": result.receipt.receipt_id,
                             "content_digest": parent["content_digest"]},
                         "selector": selector, "output_name": output_name}},
        idempotency_key=idempotency_key + ":render",
    )
    if not render.ok:
        from astrid.sdk.workspace_client import WorkspaceClientError

        error = render.error
        raise WorkspaceClientError(0, error.code, error.message, dict(error.details))
    return {**envelope, "render": render.as_dict(), "render_task_id": render.data["task_id"]}


def _string(value: Any, field: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise OrchestrationContractError(f"{field} must be a non-empty string")
    return value


def _typed_settlement_effect(value: Mapping[str, Any] | None) -> dict[str, Any]:
    if not isinstance(value, Mapping) or not value:
        raise OrchestrationContractError("settlement_effect must be a non-empty typed settlement effect")
    return dict(value)


__all__ = ["build_stitch_admission", "publish_authoring_proposal"]
