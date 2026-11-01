"""Product-neutral workspace.v1 client and terminal evidence extensions."""

import json
from typing import Any, Mapping

from .contract_metadata import CONTRACT_VERSION

from .generated import (
    ApiError,
    AttemptFence,
    ClaimWaiting,
    ByteResponse,
    Capability,
    Event,
    Executor,
    Generation,
    GenerationVariant,
    Health,
    ServiceStatus,
    IntegrityCheck,
    IntegrityReport,
    Handshake,
    ManagedObject,
    ManagedOutput,
    ObjectLocation,
    ShotTextBinding,
    MutationResult,
    Project,
    ProjectDocument,
    PreferenceResource,
    PreferenceMutationResult,
    Realm,
    RealmLifecycle,
    RecoveryAuthorization,
    RecoveryCheckpointReceipt,
    RecoveryReceipt,
    RecoveryResumeReceipt,
    Task,
    WorkspaceClient as _GeneratedWorkspaceClient,
    _path_part,
)

__all__ = ["CONTRACT_VERSION", "ApiError", "AttemptFence", "ClaimWaiting", "ByteResponse", "Capability", "Event", "Executor", "Generation", "GenerationVariant", "Handshake", "Health", "ServiceStatus", "IntegrityCheck", "IntegrityReport", "ManagedObject", "ManagedOutput", "ObjectLocation", "ShotTextBinding", "MutationResult", "Project", "ProjectDocument", "PreferenceResource", "PreferenceMutationResult", "Realm", "RealmLifecycle", "RecoveryAuthorization", "RecoveryCheckpointReceipt", "RecoveryReceipt", "RecoveryResumeReceipt", "Task", "WorkspaceClient"]


class WorkspaceClient(_GeneratedWorkspaceClient):
    """Public client with durable failure and cancellation evidence support."""

    def fail_attempt(self, attempt_id: str, *, lease_id: str, fence: int, error: Any,
                     runtime_epoch: int, idempotency_key: str,
                     outputs: list[Mapping[str, Any]] | None = None,
                     result: Mapping[str, Any] | None = None) -> MutationResult:
        payload = {"lease_id": lease_id, "fence": fence, "runtime_epoch": runtime_epoch, "error": error}
        if outputs is not None:
            payload["outputs"] = outputs
        if result is not None:
            payload["result"] = dict(result)
        return self._terminal_evidence_request(f"/v1/attempts/{_path_part(attempt_id)}/fail", payload, idempotency_key)

    def cancel_task(self, task_id: str, *, idempotency_key: str, expected_version: int | None = None,
                    attempt_id: str | None = None, lease_id: str | None = None,
                    fence: int | None = None, runtime_epoch: int | None = None,
                    outputs: list[Mapping[str, Any]] | None = None,
                    result: Mapping[str, Any] | None = None,
                    stop_observed: bool | None = None) -> MutationResult:
        payload = {key: value for key, value in {
            "expected_version": expected_version, "attempt_id": attempt_id, "lease_id": lease_id,
            "fence": fence, "runtime_epoch": runtime_epoch, "outputs": outputs,
            "result": dict(result) if result is not None else None, "stop_observed": stop_observed,
        }.items() if value is not None}
        return self._terminal_evidence_request(f"/v1/tasks/{_path_part(task_id)}/cancel", payload, idempotency_key)

    def _terminal_evidence_request(self, path, payload, key):
        _, _, body = self._request("POST", path, body=json.dumps(payload, separators=(",", ":")).encode(),
            headers={"Content-Type": "application/json", "Idempotency-Key": key})
        return self._mutation_json(body)
