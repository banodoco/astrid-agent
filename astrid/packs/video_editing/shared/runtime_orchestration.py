"""Typed Runtime contract for the migrated Astrid orchestration families.

This module describes Runtime task admissions; it does not own task lifecycle,
persist a graph, or execute an engine.  The Runtime owns task identity,
dependency gating, event ordering, retries, and fenced settlement.  The pack
only supplies typed capability/specification data and deterministic transport
keys for child replay.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from astrid.core.contracts.runtime_orchestration import (
    CapabilityIdentity,
    OrchestrationContractError,
    RuntimeAdmission,
    SCHEMA_VERSION,
    STITCH_CAPABILITIES,
    STITCH_FAMILY,
)

CHILD_KEY_PREFIX = "astrid.orchestration:v1"
ROOT_FAMILY = "orchestration_roots"
CHILD_FAMILY = "generic_child_generation"

ROOT_CAPABILITIES = {
    "travel_orchestrator": "video_editing.travel_orchestrator",
    "join_clips_orchestrator": "video_editing.join_clips_orchestrator",
    "edit_video_orchestrator": "video_editing.edit_video_orchestrator",
}
CHILD_CAPABILITIES = {
    "travel_segment": "video_editing.travel_segment",
    "individual_travel_segment": "video_editing.individual_travel_segment",
    "join_clips_segment": "video_editing.join_clips_segment",
}
_ALLOWED_CHILDREN = {
    "travel_orchestrator": frozenset({"travel_segment", "individual_travel_segment"}),
    "join_clips_orchestrator": frozenset({"join_clips_segment"}),
    "edit_video_orchestrator": frozenset(),
}
_ROOT_STITCH = {
    "travel_orchestrator": "travel_stitch",
    "join_clips_orchestrator": "join_final_stitch",
}


def _non_empty(value: Any, field: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise OrchestrationContractError(f"{field} must be a non-empty string")
    return value


def _digest(value: Any, field: str) -> str:
    value = _non_empty(value, field)
    if not value.startswith("sha256:") or len(value) != 71:
        raise OrchestrationContractError(f"{field} must be a sha256: digest")
    try:
        int(value[7:], 16)
    except ValueError as exc:
        raise OrchestrationContractError(f"{field} must be a sha256: digest") from exc
    return value


def _object_ids(value: Sequence[str], field: str) -> tuple[str, ...]:
    if isinstance(value, (str, bytes)):
        raise OrchestrationContractError(f"{field} must be an ordered object-id list")
    result = tuple(_non_empty(item, f"{field}[{index}]") for index, item in enumerate(value))
    if len(set(result)) != len(result):
        raise OrchestrationContractError(f"{field} must not contain duplicate object IDs")
    return result


def _settlement_effect(value: Any, field: str) -> dict[str, Any]:
    if not isinstance(value, Mapping) or not value:
        raise OrchestrationContractError(f"{field} must be a non-empty typed settlement effect")
    return dict(value)


@dataclass(frozen=True)
class ChildSpec:
    role: str
    index: int
    capability: CapabilityIdentity
    input_object_ids: tuple[str, ...]
    params: Mapping[str, Any]

    def __post_init__(self) -> None:
        _non_empty(self.role, "child.role")
        if isinstance(self.index, bool) or not isinstance(self.index, int) or self.index < 0:
            raise OrchestrationContractError("child.index must be a non-negative integer")
        _object_ids(self.input_object_ids, "child.input_object_ids")
        if not isinstance(self.params, Mapping):
            raise OrchestrationContractError("child.params must be an object")

    def to_dict(self) -> dict[str, Any]:
        return {
            "role": self.role,
            "index": self.index,
            "capability": self.capability.to_dict(),
            "input_object_ids": list(self.input_object_ids),
            "params": dict(self.params),
        }


@dataclass(frozen=True)
class RuntimeOrchestrationHandoff:
    """Producer-facing, immutable description of one root dependency graph."""

    project: str
    root_name: str
    root: CapabilityIdentity
    root_input_object_ids: tuple[str, ...]
    root_params: Mapping[str, Any]
    children: tuple[ChildSpec, ...]
    stitch: CapabilityIdentity | None
    output_policy: Mapping[str, Any]
    settlement_effect: Mapping[str, Any]
    child_settlement_effect: Mapping[str, Any] | None
    stitch_settlement_effect: Mapping[str, Any] | None

    def __post_init__(self) -> None:
        _non_empty(self.project, "project")
        if self.root_name not in ROOT_CAPABILITIES:
            raise OrchestrationContractError(f"unknown orchestration root {self.root_name!r}")
        if self.root.capability_id != ROOT_CAPABILITIES[self.root_name]:
            raise OrchestrationContractError("root capability identity does not match root route")
        _object_ids(self.root_input_object_ids, "root_input_object_ids")
        if not isinstance(self.root_params, Mapping):
            raise OrchestrationContractError("root_params must be an object")
        if not isinstance(self.output_policy, Mapping):
            raise OrchestrationContractError("output_policy must be an object")
        _settlement_effect(self.settlement_effect, "settlement_effect")
        if self.children and self.child_settlement_effect is None:
            raise OrchestrationContractError("child_settlement_effect is required when children are admitted")
        if self.child_settlement_effect is not None:
            _settlement_effect(self.child_settlement_effect, "child_settlement_effect")
        allowed = _ALLOWED_CHILDREN[self.root_name]
        seen_slots: set[tuple[str, int]] = set()
        for child in self.children:
            allowed_ids = {CHILD_CAPABILITIES[short_id] for short_id in allowed}
            if child.capability.capability_id not in allowed_ids:
                raise OrchestrationContractError(
                    f"child {child.capability.capability_id!r} is not allowed for {self.root_name!r}"
                )
            slot = (child.role, child.index)
            if slot in seen_slots:
                raise OrchestrationContractError(f"duplicate child slot {slot!r}")
            seen_slots.add(slot)
        expected_stitch = _ROOT_STITCH.get(self.root_name)
        if expected_stitch is None:
            if self.stitch is not None:
                raise OrchestrationContractError("childless edit root cannot declare a stitch")
        elif self.stitch is None or self.stitch.capability_id != STITCH_CAPABILITIES[expected_stitch]:
            raise OrchestrationContractError("stitch capability identity does not match root route")
        if self.stitch is not None and self.stitch_settlement_effect is None:
            raise OrchestrationContractError("stitch_settlement_effect is required when a stitch is admitted")
        if self.stitch_settlement_effect is not None:
            _settlement_effect(self.stitch_settlement_effect, "stitch_settlement_effect")

    @property
    def dependency_edges(self) -> tuple[dict[str, Any], ...]:
        root_ref = "root"
        edges = [
            {
                "from": root_ref,
                "to": f"child:{child.role}:{child.index}",
                "requires_event": "task.running",
                "fence": "parent_attempt",
            }
            for child in self.children
        ]
        if self.stitch is not None:
            edges.extend(
                {
                    "from": f"child:{child.role}:{child.index}",
                    "to": "stitch:0",
                    "requires_event": "task.succeeded",
                    "fence": "runtime_task",
                }
                for child in self.children
            )
        return tuple(edges)

    @property
    def aggregation(self) -> dict[str, Any]:
        return {
            "kind": "ordered_children",
            "order": [f"{child.role}:{child.index}" for child in self.children],
            "source": "runtime.events",
            "require_terminal": "task.succeeded",
            "output_field": "outputs",
        }

    def root_admission(self, *, idempotency_key: str) -> RuntimeAdmission:
        return RuntimeAdmission(
            body={
                "project": self.project,
                "capability_id": self.root.capability_id,
                "capability_digest": self.root.capability_digest,
                "schema_version": SCHEMA_VERSION,
                "input_object_ids": list(self.root_input_object_ids),
                "spec": {
                    "family": ROOT_FAMILY,
                    "params": dict(self.root_params),
                    "output_policy": dict(self.output_policy),
                    "runtime_dependencies": {
                        "children": [child.to_dict() for child in self.children],
                        "edges": list(self.dependency_edges),
                        "aggregation": self.aggregation,
                    },
                },
                "storage_estimate": {"estimated_scratch_bytes": 0, "estimated_output_bytes": 0},
                "settlement_effect": dict(self.settlement_effect),
            },
            idempotency_key=idempotency_key,
        )

    def child_admissions(self, *, root_task_id: str) -> tuple[RuntimeAdmission, ...]:
        root_task_id = _non_empty(root_task_id, "root_task_id")
        admissions: list[RuntimeAdmission] = []
        for child in self.children:
            key = derive_child_idempotency_key(root_task_id, child.role, child.index)
            admissions.append(
                RuntimeAdmission(
                    body={
                        "project": self.project,
                        "capability_id": child.capability.capability_id,
                        "capability_digest": child.capability.capability_digest,
                        "schema_version": SCHEMA_VERSION,
                        "input_object_ids": list(child.input_object_ids),
                        "spec": {
                            "family": CHILD_FAMILY,
                            "params": {**dict(child.params), "role": child.role, "index": child.index},
                            "output_policy": dict(self.output_policy),
                            "runtime_dependencies": {
                                "edges": [
                                    {
                                        "from_task_id": root_task_id,
                                        "to": "self",
                                        "requires_event": "task.running",
                                        "fence": "parent_attempt",
                                    }
                                ]
                            },
                        },
                        "storage_estimate": {"estimated_scratch_bytes": 0, "estimated_output_bytes": 0},
                        "settlement_effect": dict(self.child_settlement_effect),
                    },
                    idempotency_key=key,
                )
            )
        return tuple(admissions)

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": SCHEMA_VERSION,
            "project": self.project,
            "root": {"name": self.root_name, **self.root.to_dict()},
            "root_input_object_ids": list(self.root_input_object_ids),
            "children": [child.to_dict() for child in self.children],
            "stitch": self.stitch.to_dict() if self.stitch else None,
            "dependency_edges": list(self.dependency_edges),
            "aggregation": self.aggregation,
            "settlement_effect": dict(self.settlement_effect),
            "idempotency": {
                "transport": "Idempotency-Key",
                "derivation": f"{CHILD_KEY_PREFIX}:<root_task_id>:<role>:<index>",
            },
        }


def derive_child_idempotency_key(root_task_id: str, role: str, index: int) -> str:
    """Derive an attempt-independent key; callers send it only as a header."""

    root_task_id = _non_empty(root_task_id, "root_task_id")
    role = _non_empty(role, "role")
    if isinstance(index, bool) or not isinstance(index, int) or index < 0:
        raise OrchestrationContractError("index must be a non-negative integer")
    return f"{CHILD_KEY_PREFIX}:{root_task_id}:{role}:{index}"


def derive_stitch_idempotency_key(root_task_id: str) -> str:
    """Derive the stitch key; it is sent only as Runtime transport metadata."""

    return f"{CHILD_KEY_PREFIX}:{_non_empty(root_task_id, 'root_task_id')}:stitch:0"


def build_runtime_handoff(
    *,
    project: str,
    root_name: str,
    root_digest: str,
    root_input_object_ids: Sequence[str],
    root_params: Mapping[str, Any],
    children: Sequence[ChildSpec] = (),
    stitch_digest: str | None = None,
    output_policy: Mapping[str, Any] | None = None,
    settlement_effect: Mapping[str, Any] | None = None,
    child_settlement_effect: Mapping[str, Any] | None = None,
    stitch_settlement_effect: Mapping[str, Any] | None = None,
) -> RuntimeOrchestrationHandoff:
    """Build one producer-consumable orchestration handoff."""

    if root_name not in ROOT_CAPABILITIES:
        raise OrchestrationContractError(f"unknown orchestration root {root_name!r}")
    root_settlement_effect = _settlement_effect(settlement_effect, "settlement_effect")
    child_settlement = (
        _settlement_effect(child_settlement_effect, "child_settlement_effect")
        if child_settlement_effect is not None else None
    )
    stitch_settlement = (
        _settlement_effect(stitch_settlement_effect, "stitch_settlement_effect")
        if stitch_settlement_effect is not None else None
    )
    short_stitch = _ROOT_STITCH.get(root_name)
    stitch = (
        CapabilityIdentity(STITCH_CAPABILITIES[short_stitch], _digest(stitch_digest, "stitch_digest"))
        if short_stitch is not None
        else None
    )
    return RuntimeOrchestrationHandoff(
        project=_non_empty(project, "project"),
        root_name=root_name,
        root=CapabilityIdentity(ROOT_CAPABILITIES[root_name], root_digest),
        root_input_object_ids=_object_ids(root_input_object_ids, "root_input_object_ids"),
        root_params=dict(root_params),
        children=tuple(children),
        stitch=stitch,
        output_policy=dict(output_policy or {}),
        settlement_effect=root_settlement_effect,
        child_settlement_effect=child_settlement,
        stitch_settlement_effect=stitch_settlement,
    )


def _result_data(result: Any) -> Mapping[str, Any]:
    if hasattr(result, "ok"):
        if not bool(result.ok):
            error = getattr(result, "error", None)
            raise OrchestrationContractError(str(error or "Runtime task admission failed"))
        result = getattr(result, "data", None)
    if not isinstance(result, Mapping):
        raise OrchestrationContractError("Runtime task admission returned an invalid resource")
    return result


def _task_id(result: Any) -> str:
    data = _result_data(result)
    task = data.get("task")
    candidate = task.get("id") if isinstance(task, Mapping) else data.get("task_id", data.get("id"))
    return _non_empty(candidate, "Runtime task id")


def admit_runtime_task(client: Any, admission: RuntimeAdmission) -> str:
    """Admit one record through the generated Runtime task client."""

    body = admission.body
    tasks = getattr(client, "tasks", None)
    create = getattr(tasks, "create", None)
    if not callable(create):
        raise OrchestrationContractError("Runtime client does not expose tasks.create")
    result = create(
        project_id=body["project"],
        capability=body["capability_id"],
        capability_digest=body["capability_digest"],
        spec=body["spec"],
        input_manifest=list(body["input_object_ids"]),
        storage_estimate=dict(body["storage_estimate"]),
        settlement_effect=dict(body["settlement_effect"]),
        idempotency_key=admission.idempotency_key,
    )
    return _task_id(result)


def admit_runtime_handoff(
    client: Any,
    handoff: RuntimeOrchestrationHandoff,
    *,
    root_idempotency_key: str,
) -> tuple[str, tuple[str, ...], str | None]:
    """Admit root/children, aggregate Runtime outputs, then admit the stitch."""

    root_task_id = admit_runtime_task(client, handoff.root_admission(idempotency_key=root_idempotency_key))
    child_ids = tuple(
        admit_runtime_task(client, admission)
        for admission in handoff.child_admissions(root_task_id=root_task_id)
    )
    if handoff.stitch is None:
        return root_task_id, child_ids, None

    # Child completion is runtime-owned. Do not poll or snapshot events here;
    # callers admit the stitch after the continuation/authoritative event read.
    return root_task_id, child_ids, None


def admit_runtime_stitch(
    client: Any,
    handoff: RuntimeOrchestrationHandoff,
    *,
    root_task_id: str,
    child_task_ids: Sequence[str],
    timeline_id: str | None = None,
    expected_version: int | None = None,
    render: Mapping[str, Any] | None = None,
) -> str:
    """Admit the finalizer from an authoritative terminal event page."""
    if handoff.stitch is None:
        raise OrchestrationContractError("handoff has no stitch finalizer")
    # Runtime continuation admission derives predecessor outputs durably; input
    # object ids must remain empty on the wire. The declared aggregation order is
    # carried in the stitch spec and resolved by the owning runtime.
    stitch_name = next(name for name, capability_id in STITCH_CAPABILITIES.items() if capability_id == handoff.stitch.capability_id)
    from astrid.packs.rendering.shared.runtime_stitch import build_stitch_admission
    return admit_runtime_task(client, build_stitch_admission(
        project=handoff.project, stitch_name=stitch_name,
        stitch_digest=handoff.stitch.capability_digest, root_task_id=root_task_id,
        child_task_ids=child_task_ids, input_object_ids=(),
        output_policy=handoff.output_policy, settlement_effect=handoff.stitch_settlement_effect,
        timeline_id=timeline_id, expected_version=expected_version, render=render,
        idempotency_key=derive_stitch_idempotency_key(root_task_id)))


def publish_stitched_render(
    client: Any,
    *,
    project: str,
    timeline_ref: str,
    expected_version: int,
    config: Mapping[str, Any],
    registry: Mapping[str, Any],
    output_name: str = "hype.mp4",
    selector: str = "rendering.ffmpeg",
    runtime_attempt: Mapping[str, Any] | None = None,
    render_capability_digest: str | None = None,
    idempotency_key: str,
) -> Any:
    """Publish a timeline proposal through the fenced Runtime checkpoint.

    ``project`` remains in the signature for compatibility with callers that
    already carry project scope. Runtime derives the project from the leased
    authoring task, so this function never performs a second client-side save
    or renderer invocation.
    """
    del project
    if runtime_attempt is None:
        raise OrchestrationContractError("runtime_attempt is required for Runtime-owned publication")
    if render_capability_digest is None:
        raise OrchestrationContractError("render_capability_digest is required for Runtime-owned publication")
    from astrid.packs.rendering.shared.runtime_stitch import publish_authoring_proposal
    return publish_authoring_proposal(
        client,
        proposal={
            "timeline": config,
            "registry": registry,
            "publication": {
                "authority": "workspace_runtime",
                "render_capability": "rendering.render",
            },
        },
        timeline_id=timeline_ref,
        expected_version=expected_version,
        runtime_attempt=runtime_attempt,
        render_capability_digest=render_capability_digest,
        output_name=output_name,
        selector=selector,
        idempotency_key=idempotency_key,
    )


def read_runtime_events(client: Any, project: str, run_id: str) -> tuple[Mapping[str, Any], ...]:
    """Read the ordered event snapshot from the Runtime run service."""

    runs = getattr(client, "runs", None)
    read = getattr(runs, "events", None)
    if not callable(read):
        raise OrchestrationContractError("Runtime client does not expose runs.events")
    result = read(_non_empty(run_id, "run_id"))
    if hasattr(result, "ok"):
        if not bool(result.ok):
            error = getattr(result, "error", None)
            raise OrchestrationContractError(str(error or "Runtime event read failed"))
        result = getattr(result, "data", None)
    if not isinstance(result, (list, tuple)) or any(not isinstance(item, Mapping) for item in result):
        raise OrchestrationContractError("Runtime events returned an invalid ordered event list")
    return tuple(result)


def aggregate_child_outputs(
    handoff: RuntimeOrchestrationHandoff,
    events: Sequence[Mapping[str, Any]],
    *,
    child_task_ids: Sequence[str],
) -> tuple[dict[str, Any], ...]:
    """Aggregate terminal Runtime event outputs in the declared child order."""

    expected_ids = tuple(_non_empty(value, f"child_task_ids[{index}]") for index, value in enumerate(child_task_ids))
    if len(expected_ids) != len(handoff.children) or len(set(expected_ids)) != len(expected_ids):
        raise OrchestrationContractError("child_task_ids must match the admitted child list exactly")
    by_task: dict[str, Mapping[str, Any]] = {}
    for event in events:
        if not isinstance(event, Mapping):
            raise OrchestrationContractError("Runtime events contain a malformed event")
        if event.get("event_type") != "task.succeeded":
            continue
        payload = event.get("payload")
        if not isinstance(payload, Mapping):
            raise OrchestrationContractError("Runtime success event has a malformed payload")
        task_id = payload.get("task_id")
        if not isinstance(task_id, str) or not task_id.strip():
            raise OrchestrationContractError("Runtime success event has an invalid task_id")
        if task_id not in expected_ids:
            continue
        if task_id in by_task:
            raise OrchestrationContractError(f"Runtime emitted duplicate success events for {task_id!r}")
        role = payload.get("role")
        if not isinstance(role, str) or not role.strip():
            raise OrchestrationContractError(f"Runtime success event for {task_id!r} has an invalid role")
        index = payload.get("index")
        if isinstance(index, bool) or not isinstance(index, int) or index < 0:
            raise OrchestrationContractError(f"Runtime success event for {task_id!r} has an invalid index")
        outputs = payload.get("outputs")
        if isinstance(outputs, (str, bytes)) or not isinstance(outputs, Sequence) or not outputs:
            raise OrchestrationContractError(f"Runtime success event for {task_id!r} has invalid outputs")
        by_task[task_id] = {**payload, "outputs": list(_object_ids(outputs, f"outputs[{task_id}]"))}

    ordered: list[dict[str, Any]] = []
    for child, task_id in zip(handoff.children, expected_ids):
        slot = f"{child.role}:{child.index}"
        payload = by_task.get(task_id)
        if payload is None:
            raise OrchestrationContractError(f"Runtime events do not contain one terminal output for {slot}")
        if payload.get("role") != child.role or payload.get("index") != child.index:
            raise OrchestrationContractError(f"Runtime output task {task_id!r} does not match child slot {slot}")
        ordered.append({"slot": slot, "payload": dict(payload)})
    return tuple(ordered)


__all__ = [
    "CHILD_CAPABILITIES",
    "CHILD_FAMILY",
    "CHILD_KEY_PREFIX",
    "CapabilityIdentity",
    "ChildSpec",
    "OrchestrationContractError",
    "ROOT_CAPABILITIES",
    "ROOT_FAMILY",
    "RuntimeAdmission",
    "RuntimeOrchestrationHandoff",
    "SCHEMA_VERSION",
    "STITCH_CAPABILITIES",
    "STITCH_FAMILY",
    "aggregate_child_outputs",
    "admit_runtime_handoff",
    "admit_runtime_stitch",
    "admit_runtime_task",
    "build_runtime_handoff",
    "derive_child_idempotency_key",
    "derive_stitch_idempotency_key",
    "read_runtime_events",
    "publish_stitched_render",
]
