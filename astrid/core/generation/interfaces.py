"""Phase 1 generation handoffs for F06/F07 and RunPod R03.

These types document existing admission, result and MTS ownership boundaries;
they introduce no wire protocol, task ledger, session manager or provider calls.
RunPod implementations live in the RunPod pack. A prepared target must pass
through its ordinary prepared-task client, never an attached HTTP URL.
"""
from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any, Literal, Protocol, TypedDict

from astrid.core.generation.targets import TargetRecord, TargetSelection

if TYPE_CHECKING:
    from astrid.core.execution.managed_tool_session import AdmissionToken, SessionBinding
    from astrid.sdk.results import InvocationResult


class InputInventoryRow(TypedDict):
    name: str
    type: str
    default: Any


class ReadinessIssue(TypedDict):
    code: str
    severity: Literal["error", "warning"]
    message: str
    details: dict[str, Any]


class ReadinessFix(TypedDict):
    argv: list[str]
    scope: Literal["read_only", "managed_write", "user_env", "paid"]
    details: dict[str, Any]


class ReadinessEnvelope(TypedDict):
    entry: dict[str, Any] | None
    target: dict[str, Any] | None
    status: Literal["ready", "ready_with_warnings", "blocked"]
    issues: list[ReadinessIssue]
    fixes: list[ReadinessFix]


class GenerationErrorEnvelope(ReadinessEnvelope):
    task_id: str | None


class ResultEvidence(TypedDict):
    """The only namespaces in managed-generation-result.v1 evidence.

    Producer owns engine/entry/model/version/build facts; transport owns exact
    Runtime task/attempt/binding, target, admission readiness and release facts.
    Source and effective-settings CAS refs remain in the manifest's inputs.
    Failure/cancel may retain partial evidence; no synthetic attempt is created.
    """

    producer: dict[str, Any]
    transport: dict[str, Any]


class OwnedSessionAdapter(Protocol):
    """Duck-typed MTS hooks. MTS owns tokens, fences and the occupied slot.

    A human endpoint receives prompt-specific cancel only. Uncertain cancel or
    release retains custody; ok:true requires owner-specific release proof.
    Child adapters use the existing JSON pipe, not a second process owner.
    """

    def observe(self, *, binding: SessionBinding) -> Mapping[str, Any]: ...
    def invoke(self, **kwargs: Any) -> Any: ...
    def fence(self, *, reason: str) -> Any: ...
    def cancel(self, *, reason: str) -> Mapping[str, Any]: ...
    def release(self, *, reason: str) -> Mapping[str, Any]: ...


@dataclass(frozen=True)
class GenerationAdmission:
    """Local projection of existing Runtime/MTS identities, never a receipt DB."""

    task_id: str
    attempt_id: str | None
    execution_binding: Mapping[str, Any]
    target: TargetRecord
    session_binding: SessionBinding | None = None
    admission_token: AdmissionToken | None = None


@dataclass(frozen=True)
class RunPodPreparationRequest:
    """Validated Comfy work passed to R03 before any paid claim.

    Caller validates source/inputs first. R02/R03 verify fresh paid consent,
    disclosed rate/cap/deadline and exact journal/account/pod/storage identity.
    Rejected reuse must not extend a hold. Preparation preserves human defaults.
    """

    entry: Mapping[str, Any]
    source: Path
    workflow: Path
    inputs: Mapping[str, Any]
    consent: tuple[str, ...]
    max_usd: float | None
    deadline: str | None
    execution_request: Mapping[str, Any] | None = None


class PreparedGeneration(Protocol):
    """Compatible with PreparedWorkerSelection's existing handoff methods."""

    def execution_request(self, supplied: Mapping[str, Any] | None) -> dict[str, Any]: ...
    def bind_client(
        self, client: Any, *, workflow_path: Path, workflow_bundle_path: Path,
    ) -> Any: ...


class RunPodGenerationPreparation(Protocol):
    """R03 implementation seam; no provider implementation is supplied here.

    prepare returns exact prepared custody; ordinary invoke_result(wait=False)
    admits once and returns the task. Follow resumes that admission/activation.
    cancel requests exact work cancellation and reports observed_stop honestly;
    uncertainty is fenced and retained, never replayed or declared stopped.
    """

    def prepare(self, request: RunPodPreparationRequest) -> PreparedGeneration: ...
    def cancel(self, *, admission: GenerationAdmission, reason: str) -> Mapping[str, Any]: ...


class GenerationInvocationClient(Protocol):
    def invoke_result(self, capability_id: str, *, kind: str, **kwargs: Any) -> InvocationResult: ...


__all__ = [
    "TargetRecord", "TargetSelection", "InputInventoryRow", "ReadinessIssue",
    "ReadinessFix", "ReadinessEnvelope", "GenerationErrorEnvelope", "ResultEvidence",
    "OwnedSessionAdapter", "GenerationAdmission", "RunPodPreparationRequest",
    "PreparedGeneration", "RunPodGenerationPreparation", "GenerationInvocationClient",
]
