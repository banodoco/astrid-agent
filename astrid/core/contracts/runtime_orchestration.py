"""Shared Runtime orchestration contract declarations."""

from __future__ import annotations

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
