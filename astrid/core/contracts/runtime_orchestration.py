"""Shared Runtime orchestration contract declarations."""

from __future__ import annotations

import copy
import json
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

SCHEMA_VERSION = "1"
STITCH_FAMILY = "stitch_finalization"
STITCH_CAPABILITIES = {
    "travel_stitch": "rendering.render",
    "join_final_stitch": "rendering.render",
}


class OrchestrationContractError(ValueError):
    """A typed orchestration contract cannot be represented safely."""


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


@dataclass(frozen=True)
class CapabilityIdentity:
    capability_id: str
    capability_digest: str

    def __post_init__(self) -> None:
        _non_empty(self.capability_id, "capability_id")
        _digest(self.capability_digest, "capability_digest")

    def to_dict(self) -> dict[str, str]:
        return {
            "capability_id": self.capability_id,
            "capability_digest": self.capability_digest,
        }


@dataclass(frozen=True)
class RuntimeAdmission:
    """Logical body plus its transport-only idempotency key."""

    body: Mapping[str, Any]
    idempotency_key: str

    def __post_init__(self) -> None:
        _non_empty(self.idempotency_key, "idempotency_key")
        if "idempotency_key" in self.body:
            raise OrchestrationContractError("idempotency_key is transport-only")

    @property
    def canonical_bytes(self) -> bytes:
        return json.dumps(self.body, sort_keys=True, separators=(",", ":")).encode()


def is_native_assembler_spec(spec: Mapping[str, Any]) -> bool:
    """Select native settlement from immutable admission, never a caller flag."""
    if spec.get("capability_id") != "rendering.assemble_timeline":
        return False
    inputs = spec.get("inputs", {})
    capture = inputs.get("resolved_children") if isinstance(inputs, Mapping) else None
    if not isinstance(capture, Mapping) or "admitted_target" not in capture:
        return False
    outputs = spec.get("outputs", {})
    dependencies = spec.get("runtime_dependencies", {})
    declaration = outputs.get("authoring_proposal") if isinstance(outputs, Mapping) else None
    publication = dependencies.get("publication", {}) if isinstance(dependencies, Mapping) else {}
    target = publication.get("admitted_target") if isinstance(publication, Mapping) else None
    if (declaration != {"type": "file", "artifact_type": "timeline/authoring-proposal"}
            or target != capture["admitted_target"]):
        raise OrchestrationContractError(
            "native assembler requires its admitted target and proposal declaration"
        )
    return True


def build_assembler_admission(
    *,
    capture: Mapping[str, Any],
    assembler_digest: str,
    idempotency_key: str,
) -> RuntimeAdmission:
    """Freeze the original target and exact child selections for native assembly."""
    capture = copy.deepcopy(dict(capture))
    target = capture["admitted_target"]
    if set(target) != {"project_id", "timeline_id", "base_revision_id", "base_publication_id"}:
        raise OrchestrationContractError("assembler requires the original admitted target pair")
    for key, value in target.items():
        _non_empty(value, key)
    bundle = capture["bundle"]
    if (bundle["project_id"] != target["project_id"]
            or bundle["timeline_id"] != target["timeline_id"]
            or bundle["base_parent"]["revision_id"] != target["base_revision_id"]):
        raise OrchestrationContractError("assembler closure differs from the admitted target")
    children = capture["children"]
    if (len(children) != 2 or [child["ordinal"] for child in children] != [0, 1]
            or len({child["task_id"] for child in children}) != 2
            or len({child["association"]["association_id"] for child in children}) != 2):
        raise OrchestrationContractError("assembler requires two distinct ordered child associations")
    order = []
    for child in children:
        _non_empty(child["task_id"], "child task id")
        selection = child["media"]
        if (child["association"]["object_id"] != selection["object_id"]
                or child["verified_media"]["selection"] != selection):
            raise OrchestrationContractError("assembler media differs from the exact child association")
        order.append(selection["object_id"])
    identity = CapabilityIdentity("rendering.assemble_timeline", assembler_digest)
    # The frozen capture and native proposal are staged before the host runs.
    json_bytes = 1024 * 1024 + 32 * len(
        json.dumps(capture, ensure_ascii=False, allow_nan=False).encode("utf-8")
    )
    return RuntimeAdmission(body={
        "project": target["project_id"],
        **identity.to_dict(),
        "schema_version": SCHEMA_VERSION,
        "input_object_ids": [],
        "spec": {
            "capability_id": identity.capability_id,
            "kind": "executor",
            "family": STITCH_FAMILY,
            "inputs": {"resolved_children": capture},
            "outputs": {
                "authoring_proposal": {
                    "type": "file", "artifact_type": "timeline/authoring-proposal"
                }
            },
            "runtime_dependencies": {
                "edges": [
                    {"from_task_id": child["task_id"], "to": "self",
                     "requires_event": "task.succeeded", "fence": "runtime_task"}
                    for child in children
                ],
                "aggregation": {"kind": "ordered_cas_inputs", "order": order},
                "publication": {"admitted_target": copy.deepcopy(target)},
            },
        },
        "storage_estimate": {"scratch_bytes": 2 * json_bytes, "output_bytes": json_bytes},
    }, idempotency_key=idempotency_key)
