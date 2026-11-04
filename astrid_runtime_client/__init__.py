"""Generated, product-neutral client for the workspace.v1 protocol."""

import json
from typing import Any, Mapping

from .contract_metadata import CONTRACT_VERSION
from .generated import (
    ApiError,
    AttemptFence,
    ByteResponse,
    Capability,
    ClaimWaiting,
    Event,
    Executor,
    Generation,
    GenerationVariant,
    Handshake,
    Health,
    ServiceStatus,
    IntegrityCheck,
    IntegrityReport,
    ManagedObject,
    ManagedOutput,
    MutationResult,
    ObjectLocation,
    PreferenceMutationResult,
    PreferenceResource,
    Project,
    ProjectDocument,
    Realm,
    RealmLifecycle,
    RecoveryAuthorization,
    RecoveryCheckpointReceipt,
    RecoveryReceipt,
    RecoveryResumeReceipt,
    ShotTextBinding,
    Task,
    WorkspaceClient as _GeneratedWorkspaceClient,
    _path_part,
)
from .timeline_commit_types import (
    AssembledTargetArgs,
    ClipAddArgs,
    ClipAddMediaArgs,
    ClipAnchorArgs,
    ClipDeleteArgs,
    ClipMoveArgs,
    ClipSetArgs,
    ClipSetElementArgs,
    ElementRef,
    OccurrenceCreateArgs,
    OccurrenceDeleteArgs,
    OccurrenceDuplicateArgs,
    PlacementCreateArgs,
    PlacementUnplaceArgs,
    ProducerAdmittedTarget,
    ProducerAttemptPin,
    ProducerGuardArgs,
    ProducerIdentity,
    SettledProducerProposal,
    ShotPoolArgs,
    ShotSetArgs,
    TimelineInitArgs,
    TextBindingSetArgs,
    ClipSetParamsArgs,
    ClipSplitArgs,
    ClipTrimArgs,
    DocumentPin,
    DocPatchArgs,
    InverseArgs,
    MediaSelection,
    OccurrenceAddMediaArgs,
    OccurrenceSwapMediaArgs,
    RestoreArgs,
    TrackCreateArgs,
    TrackSetArgs,
    VerifiedDocument,
    VerifiedDocumentObject,
    VerifiedMedia,
    WordAnchor,
)

__all__ = ['AssembledTargetArgs', 'ClipSetElementArgs', 'ClipAddArgs', 'ClipAddMediaArgs', 'ClipAnchorArgs', 'ElementRef', 'OccurrenceCreateArgs', 'OccurrenceDeleteArgs', 'OccurrenceDuplicateArgs', 'PlacementCreateArgs', 'PlacementUnplaceArgs', 'ProducerAdmittedTarget', 'ProducerAttemptPin', 'ProducerGuardArgs', 'ProducerIdentity', 'SettledProducerProposal', 'ShotPoolArgs', 'ShotSetArgs', 'TimelineInitArgs', 'TextBindingSetArgs', 'WordAnchor', "CONTRACT_VERSION", "ApiError", "AttemptFence", "ByteResponse", "Capability", "ClaimWaiting", 'ClipDeleteArgs', 'ClipMoveArgs', 'ClipSetArgs', 'ClipSetParamsArgs', 'ClipSplitArgs', 'ClipTrimArgs', 'DocumentPin', 'DocPatchArgs', "Event", "Executor", "Generation", "GenerationVariant", "Handshake", "Health", "ServiceStatus", "IntegrityCheck", "IntegrityReport", 'InverseArgs', "ManagedObject", "ManagedOutput", 'MediaSelection', "MutationResult", "ObjectLocation", 'OccurrenceAddMediaArgs', 'OccurrenceSwapMediaArgs', "PreferenceMutationResult", "PreferenceResource", "Project", "ProjectDocument", "Realm", "RealmLifecycle", "RecoveryAuthorization", "RecoveryCheckpointReceipt", "RecoveryReceipt", "RecoveryResumeReceipt", 'RestoreArgs', "ShotTextBinding", "Task", 'TrackCreateArgs', 'TrackSetArgs', 'VerifiedDocument', 'VerifiedDocumentObject', 'VerifiedMedia', "WorkspaceClient"]


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

    def _terminal_evidence_request(self, path: str, payload: Mapping[str, Any], key: str) -> MutationResult:
        _, _, body = self._request("POST", path, body=json.dumps(payload, separators=(",", ":")).encode(),
            headers={"Content-Type": "application/json", "Idempotency-Key": key})
        return self._mutation_json(body)
