"""Pack-owned R03 composition for the F04/A10 ordinary-generation handoff.

The front door supplies its validated source/inputs, disclosed R02 consent,
and selected deployment files. Runtime/SDK retain admission, recovery, CAS
publication and cancellation; this adapter creates no task or output ledger.
"""
from __future__ import annotations

import hashlib
from dataclasses import dataclass, replace
from typing import Any, Callable, Mapping

from astrid.core.generation.interfaces import RunPodPreparationRequest

from .prepared_task import PreparedWorkerError
from .remote_placement import Claim, RemotePlacement, RunPodInstallation, connect_claimed_for_preparation, source_digest
from .target_selection import ClaimedJob
from .worker_owner import RunPodLease, RunPodLeaseOwner, _off_event_loop


@dataclass(frozen=True)
class PreparedRunPodLease:
    """One job's prepared machine: the exact lease, its connected claim and custody."""

    lease: RunPodLease
    installed: Mapping[str, Any]
    claim: Claim | None = None
    state: Any = None  # remote_placement.RemoteRun of the preparation
    custody: Mapping[str, Any] | None = None
    reused: bool = False


@_off_event_loop
def prepare_retained_generation(
    request: RunPodPreparationRequest, *, consent: ClaimedJob,
    lease_owner: RunPodLeaseOwner, placement: RemotePlacement,
    installation: RunPodInstallation, validate: Callable[[RunPodPreparationRequest], None],
    requested_idle_seconds: float | None = None,
    on_acquired: Callable[[RunPodLease], None] | None = None,
    entry: str | None = None,
    pod_ready: bool = False,
    observe_mounts: Callable[[str, str], Mapping[str, Any]] | None = None,
    manifest_rows: tuple[Mapping[str, Any], ...] = (),
    cancelled: Callable[[], bool] | None = None,
) -> PreparedRunPodLease:
    """Prepare one machine under A3's existing lock and journal, without a worker on pod.

    Static request/source/recipe checks precede paid admission. A3 alone owns
    settle, expiry, reconciliation and release; no second account lock is taken.
    A warm machine is reused only when its machine custody (account, observed
    GPU, image, pinned volume, source digest) matches this job's; a saved volume
    is pinned by the storage probe before install, and with ``pod_ready`` the
    pod's one Runtime is ready (reset in place when no ready.json is left).
    """
    from .machine import (
        CustodyChanged,
        custody_differences,
        ensure_pod_ready,
        machine_custody,
        pin_storage,
    )

    if not isinstance(consent, ClaimedJob):
        raise PreparedWorkerError("job preparation requires a claimed machine")
    if (request.entry.get("engine", "comfyui") != installation.engine
            or request.max_usd != consent.max_usd or request.deadline != consent.deadline
            or request.consent != consent.consent or request.execution_request is not None
            or lease_owner.selection.preferences.mount_path != "/workspace"
            or lease_owner.selection.https_token_file is None):
        raise PreparedWorkerError("unsupported retained route or undisclosed paid limits")
    validate(request)
    sources = {}
    for name, path in (("source", request.source), ("workflow", request.workflow)):
        if path.is_symlink() or not path.is_file():
            raise PreparedWorkerError("validated source/workflow must be regular files")
        sources[name] = hashlib.sha256(path.read_bytes()).hexdigest()
    if dict(installation.compatibility) != lease_owner.selection.compatibility:
        raise PreparedWorkerError("installer and retained lease compatibility differ")
    if placement.source_root is None:
        # Lazy on first RunPod prepare: the Agent derives its pinned bootstrap sources.
        from astrid.packs.runpod import managed_sources
        placement.source_root = managed_sources.ensure_managed_sources()
    bootstrap_digest = installation.validate_sources(placement.source_root)
    preferences = lease_owner.selection.preferences
    with lease_owner.lock:
        prior = lease_owner._read()
        prior_preparation = prior.get("lease", {}).get("preparation") if prior else None
        if (prior is not None and prior["state"] != "cleanup_complete" and prior_preparation is not None
                and prior_preparation["state"] not in {"prepared", "stale_after_failure"}):
            raise PreparedWorkerError("retained preparation is unresolved; reconcile the same journal")
        if (prior is not None and prior["state"] != "cleanup_complete" and prior_preparation is not None
                and prior.get("lease", {}).get("state") == "idle"):
            # Refuse warm reuse before the claim when custody that needs no pod
            # observation already differs (the GPU is the warm pod's own).
            expected = machine_custody(
                account=preferences.provider_account_ref,
                gpu_type=str((prior.get("provider_observation") or {}).get("gpu_type") or prior.get("gpu_type")),
                image=preferences.image,
                volume={**(prior_preparation.get("custody") or {}).get("volume", {}),
                        "id": preferences.volume_id, "mount_path": preferences.mount_path},
                source_digest=bootstrap_digest)
            if set(custody_differences(prior_preparation.get("custody"), expected)) - {"source_digest"}:
                lease_owner.release(reason="custody_changed", _expected_operation=prior["operation_id"])
        invocation = lease_owner.acquire(consent, requested_idle_seconds=requested_idle_seconds,
                                         defer_unsafe_release=True, cancelled=cancelled)
        if on_acquired is not None:
            on_acquired(invocation)
        claimed = lease_owner.preparation_claim(invocation)
        previous = claimed["lease"].get("preparation")
        preparation = {
            "schema": "astrid.runpod.lease-preparation.v1", "state": "preparing",
            "operation_id": invocation.operation_id, "pod_id": claimed["pod_id"],
            "compatibility": dict(installation.compatibility), "bootstrap_digest": bootstrap_digest,
        }

        def save(**changes):
            nonlocal preparation
            current = lease_owner.preparation_claim(invocation)
            preparation = {**preparation, **changes}
            return lease_owner._save(current, {**current["lease"], "preparation": preparation})

        save()
        try:
            claim = connect_claimed_for_preparation(claimed)
            # The claim bounds the phase; recipe steps keep their own timeouts.
            remaining = claimed["lease"]["paid_expires_at"] - lease_owner.clock()
            # A saved network volume is pinned by the probe; the install is model-free and align fetches the manifest's rows.
            cache_root = None if preferences.volume_id is None else (
                f"{preferences.mount_path}/astrid/engines/{installation.engine}/models")
            # The install binds the saved volume's model cache, so align and ComfyUI read it from there.
            installation = replace(installation, models_root=cache_root)
            out = lease_owner.selection.path.parent / "preparation" / invocation.operation_id
            storage: dict[str, Any] = {"mode": "ephemeral"}
            if preferences.volume_id is not None:
                # Pin the mount before anything is written: an unmounted volume
                # refuses here instead of caching onto container disk.
                assert cache_root is not None
                if observe_mounts is None:
                    raise PreparedWorkerError("a saved volume needs the provider's typed mount observation")
                probe_state = placement.begin_claimed(claim, out=out, deadline_seconds=remaining)
                storage = pin_storage(
                    placement, probe_state, pod_id=claimed["pod_id"], volume_id=preferences.volume_id,
                    mount_path=preferences.mount_path, model_cache_root=cache_root, engine_root=installation.root,
                    required_volume_bytes=installation.model_bytes(manifest_rows), observe_mounts=observe_mounts)
            observation = claimed.get("provider_observation") or {}
            custody = machine_custody(
                account=claimed["provider_account_ref"],
                gpu_type=str(observation.get("gpu_type") or claimed.get("gpu_type")),
                image=preferences.image,
                volume={"id": preferences.volume_id, "mount_path": preferences.mount_path,
                        "cache_device": storage.get("cache_device"), "mount_id": storage.get("mount_id")},
                source_digest=bootstrap_digest)
            differences = (custody_differences(previous.get("custody"), custody)
                           if previous is not None and previous.get("state") in {"prepared", "stale_after_failure"}
                           else [])
            reuse = previous is not None and previous.get("state") == "prepared" and not differences
            if set(differences) - {"source_digest"}:
                raise CustodyChanged("warm machine custody differs: " + ", ".join(differences))
            save(custody=custody, storage=storage)
            state = placement.prepare_claimed(
                claim, out=out,
                deadline_seconds=remaining, installation=installation,
                checkpoint=lambda job: save(job=dict(job)),
                reuse_snapshot=previous["installed"] if reuse else None,
            )
            if pod_ready:
                # The pod's one Runtime: ready as left by the last reset, or reset now.
                save(pod_ready=ensure_pod_ready(placement, state))
            if (lease_owner.clock() >= claimed["lease"]["paid_expires_at"]
                    or source_digest(placement.source_root) != bootstrap_digest
                    or any(hashlib.sha256(path.read_bytes()).hexdigest() != sources[name]
                           for name, path in (("source", request.source), ("workflow", request.workflow)))):
                raise PreparedWorkerError("preparation consumed its paid lifetime or source bytes changed")
            saved = save(state="prepared", installed=dict(state.installed))
        except BaseException as exc:
            current = lease_owner._read()
            if current is None:
                raise PreparedWorkerError(
                    "retained lease journal disappeared during preparation failure"
                ) from exc
            jobs = (preparation.get("job"), current.get("lease", {}).get("job"))
            safe = (lease_owner.clock() < claimed["lease"]["paid_expires_at"]
                    and not isinstance(exc, CustodyChanged) and "claim" in locals()
                    and "custody" in preparation and current.get("state") == "claimed"
                    and current.get("pod_id") == claimed.get("pod_id") == claim.pod_id
                    and all(job is None or job.get("state") in {"exited", "cancelled"} for job in jobs))
            if safe:
                try:
                    safe = (claim.ops.exec("true", timeout=10)[0] == 0
                            and lease_owner.clock() < claimed["lease"]["paid_expires_at"])
                except Exception:  # noqa: BLE001 (unknown transport cannot prove reachability)
                    safe = False
            if safe:
                save(state="stale_after_failure")
                raise
            # A failed current mount/custody observation cannot prove that an
            # old installed snapshot is still safe, even before installation.
            # Preserve exact custody for immediate release/recovery.
            current = lease_owner._read()
            if current is None:
                raise PreparedWorkerError(
                    "retained lease journal disappeared during preparation failure"
                ) from exc
            lease_owner._save(current, {**current["lease"], "preparation": {**preparation, "state": "unresolved"}},
                              state="cleanup_pending", reconciliation_required=True)
            raise
        return PreparedRunPodLease(
            RunPodLease(invocation.operation_id, invocation.owner_epoch, invocation.generation,
                        invocation.invocation_id, saved), state.installed,
            claim=claim, state=state, custody=custody, reused=reuse,
        )
