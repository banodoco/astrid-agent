"""Pack-owned R03 composition for the F04/A10 ordinary-generation handoff.

The front door supplies its validated source/inputs, disclosed R02 consent,
and selected deployment files. Runtime/SDK retain admission, recovery, CAS
publication and cancellation; this adapter creates no task or output ledger.
"""
from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any, Callable, Mapping

from astrid.core.generation.interfaces import GenerationAdmission, RunPodPreparationRequest

from .preparation_custody import JournalPreparationOps
from .prepared_task import PreparedWorkerError, PreparedWorkerSelection, _read_mapping
from .remote_placement import (
    ENGINE_ROOT,
    POD_VENV,
    Claim,
    RemotePlacement,
    connect_claimed_for_preparation,
)
from .target_selection import PaidRunConsent, RunPodSelectionOwner, _utc
from .worker_owner import _run_sync
from .worker_preparation import replace_target_record, target_owner


def _digest(value: Any) -> str:
    return "sha256:" + hashlib.sha256(json.dumps(
        value, sort_keys=True, separators=(",", ":"), allow_nan=False,
    ).encode()).hexdigest()


@dataclass(frozen=True)
class PreparedRunPodGeneration:
    selection: PreparedWorkerSelection
    inputs: Mapping[str, Any]
    receipt: Mapping[str, Any]

    def execution_request(self, supplied: Mapping[str, Any] | None) -> dict[str, Any]:
        return self.selection.execution_request(supplied)

    def bind_client(self, client: Any, *, workflow_path: Path, workflow_bundle_path: Path,
                    workflow_inputs: Mapping[str, Any] | None = None) -> Any:
        inputs = dict(self.inputs) if workflow_inputs is None else dict(workflow_inputs)
        if inputs != self.inputs:
            raise PreparedWorkerError("ordinary prepared inputs changed after preparation")
        if (workflow_path.is_symlink() or not workflow_path.is_file()
                or "sha256:" + hashlib.sha256(workflow_path.read_bytes()).hexdigest()
                != self.receipt["workflow_sha256"]):
            raise PreparedWorkerError("ordinary prepared workflow changed after preparation")
        return self.selection.bind_client(
            client, workflow_path=workflow_path, workflow_bundle_path=workflow_bundle_path,
            workflow_inputs=inputs,
        )


class RunPodGenerationPreparer:
    """Compose R02 claim and existing bootstrap/install without another claim.

    A10 supplies validation and selection-file construction through these
    explicit seams. ``consent`` must describe the offer shown to the user;
    R02 independently observes it again and refuses a changed price.
    """

    def __init__(self, *, owner: RunPodSelectionOwner, placement: RemotePlacement,
                 validate: Callable[[RunPodPreparationRequest], None],
                 connect_claim: Callable[[Mapping[str, Any]], Claim] | None = None,
                 select_worker: Callable[[Mapping[str, Any]], PreparedWorkerSelection],
                 consent: PaidRunConsent,
                 cancel_task: Callable[..., Mapping[str, Any]] | None = None) -> None:
        self.owner, self.placement = owner, placement
        self.validate, self.select_worker = validate, select_worker
        self.connect_claim = connect_claim or connect_claimed_for_preparation
        self.consent, self.cancel_task = consent, cancel_task

    def prepare(self, request: RunPodPreparationRequest) -> PreparedRunPodGeneration:
        if request.entry.get("engine", "comfyui") != "comfyui":
            raise PreparedWorkerError("prepared RunPod generation supports only comfyui")
        if (request.max_usd != self.consent.max_usd or request.deadline != self.consent.deadline
                or request.consent != self.consent.consent):
            raise PreparedWorkerError("ordinary preparation limits differ from disclosed paid consent")
        # Validate/normalize entry or raw workflow before even observing an offer.
        self.validate(request)
        sources = {}
        for name, path in (("source", request.source), ("workflow", request.workflow)):
            if path.is_symlink() or not path.is_file():
                raise PreparedWorkerError("validated ordinary source/workflow must be regular files")
            sources[name + "_sha256"] = "sha256:" + hashlib.sha256(path.read_bytes()).hexdigest()
        inputs = json.loads(json.dumps(dict(request.inputs), allow_nan=False))
        # Refuse unresolved preparation before R02 could renew its paid limits.
        if self.owner.path.exists():
            prior = _read_mapping(self.owner.path, label="RunPod claim").get("ordinary_preparation")
            if isinstance(prior, Mapping) and prior.get("state") != "prepared":
                raise PreparedWorkerError("ordinary preparation is unresolved; retain the same journal")
        claimed = _run_sync(self.owner.select(self.consent))
        operation = claimed["operation_id"]
        with target_owner(self.owner.path):
            current = _read_mapping(self.owner.path, label="RunPod claim")
            if current != claimed:
                raise PreparedWorkerError("RunPod claim changed before ordinary preparation")
            receipt = {
                "schema": "astrid.runpod.ordinary-preparation.v1", "state": "preparing",
                "claim_operation_id": operation, "pod_id": claimed["pod_id"],
                "provider_account_ref": claimed["provider_account_ref"], "engine": "comfyui",
                **sources, "inputs_sha256": _digest(inputs), "paid_limits": dict(claimed["paid_limits"]),
            }
            replace_target_record(self.owner.path, {**claimed, "ordinary_preparation": receipt},
                                  operation_id=operation)
            try:
                claim = self.connect_claim(claimed)
                if (claim.pod_id != claimed["pod_id"] or claim.transport != "https"
                        or any(claim.identity.get(key) != claimed.get(key) for key in (
                            "operation_id", "pod_id", "provider_account_ref", "worker_image",
                            "network_volume_id", "volume_mount_path",
                        ))):
                    raise PreparedWorkerError("bootstrap transport differs from the journal-owned claim")
                jobs = JournalPreparationOps.begin(
                    self.owner.path, claim.ops, engine_root=ENGINE_ROOT, python=POD_VENV + "/bin/python",
                )
                claim = replace(claim, ops=jobs)
                remaining = (_utc(claimed["paid_limits"]["expires_at"]) - self.owner.clock()).total_seconds()
                state = self.placement.prepare_claimed(
                    claim, out=self.owner.path.parent / "preparation" / operation,
                    deadline_seconds=remaining,
                    supervisor_command=jobs.supervisor_command("reserve"),
                )
                if self.owner.clock() >= _utc(claimed["paid_limits"]["expires_at"]):
                    raise PreparedWorkerError("ordinary preparation consumed its paid lifetime")
                selection = self.select_worker(claimed)
                if selection.claim_handle_path != self.owner.path:
                    raise PreparedWorkerError("prepared worker selected a different claim journal")
                selection.execution_request(request.execution_request)
                if any("sha256:" + hashlib.sha256(path.read_bytes()).hexdigest() != sources[name + "_sha256"]
                       for name, path in (("source", request.source), ("workflow", request.workflow))):
                    raise PreparedWorkerError("ordinary source/workflow changed during preparation")
                receipt = {**receipt, "state": "prepared", "steps": list(state.steps)}
                current = _read_mapping(self.owner.path, label="RunPod claim")
                replace_target_record(self.owner.path, {**current, "ordinary_preparation": receipt,
                                      "preparation_custody": {**current["preparation_custody"], "state": "prepared"}},
                                      operation_id=operation)
            except BaseException:
                current = _read_mapping(self.owner.path, label="RunPod claim")
                replace_target_record(self.owner.path, {
                    **current, "state": "cleanup_pending", "reconciliation_required": True,
                    "ordinary_preparation": {**receipt, "state": "unresolved"},
                }, operation_id=operation)
                raise
        return PreparedRunPodGeneration(selection, inputs, receipt)

    def release_preparation(self, config: Any, *, resume: bool = False) -> Mapping[str, Any]:
        """Resume failed R03 cleanup through P1's existing exact-release owner."""
        from .prepared_task import release_preparation

        return release_preparation(config, self.owner.path, resume=resume)

    def cancel(self, *, admission: GenerationAdmission, reason: str) -> Mapping[str, Any]:
        # The SDK owner bridge supplies exact task cancellation. Missing or
        # uncertain observation cannot become a synthetic stopped receipt.
        if self.cancel_task is None:
            return {"task_id": admission.task_id, "custody_retained": True, "observed_stop": False,
                    "state": "cancellation_unresolved"}
        return self.cancel_task(admission=admission, reason=reason)


__all__ = ["RunPodGenerationPreparer", "PreparedRunPodGeneration"]
