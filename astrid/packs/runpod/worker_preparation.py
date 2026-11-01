"""Exact RunPod target checks used by the prepared-worker owner.

This module validates provider observations before Astrid stages files, starts
workers, or releases a pod. It deliberately does not own generic RunPod
``session`` jobs or their lifetime policy.
"""

from __future__ import annotations

import asyncio
import json
import math
import os
import re
import tempfile
import time
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from functools import wraps
from pathlib import Path
from typing import Any, Awaitable, Callable, Mapping

from runtime_protocol import platform

from astrid.core.util.time import utc_now_iso


class TargetObservationError(RuntimeError):
    """Provider state was absent, ambiguous, or could not be verified safely."""


class AcquisitionUnresolvedError(TargetObservationError):
    """An earlier create/attach may have succeeded and must be reconciled."""


@dataclass(frozen=True)
class RunPodTargetContract:
    pod_id: str
    gpu_type: str
    worker_image: str
    network_volume_id: str
    network_volume_name: str | None
    network_volume_size_gb: int
    expected_pod_name: str | None = None
    datacenter_id: str | None = None


@dataclass(frozen=True)
class RunPodTargetObservation:
    pod_id: str
    pod_name: str
    desired_status: str
    actual_status: str | None
    gpu_type: str
    worker_image: str
    network_volume_id: str
    network_volume_name: str
    network_volume_size_gb: int
    datacenter_id: str | None
    observed_at: str

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class AcquiredRunPodTarget:
    pod: Any
    volume: dict[str, Any]
    attempt: dict[str, Any]
    observation: RunPodTargetObservation
    acquisition_mode: str


def write_target_record(path: Path, value: dict[str, Any]) -> None:
    """Atomically replace a private, secret-free target record."""
    path = path.expanduser()
    path.parent.mkdir(parents=True, exist_ok=True)
    data = (json.dumps(value, indent=2, sort_keys=True) + "\n").encode("utf-8")
    fd, temporary = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
    temporary_path = Path(temporary)
    try:
        with os.fdopen(fd, "wb") as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        platform.durable_replace(temporary_path, path)
    finally:
        temporary_path.unlink(missing_ok=True)


def create_target_record_exclusive(path: Path, value: dict[str, Any]) -> None:
    """Create an intent before provider side effects without replacing custody."""
    path = path.expanduser()
    path.parent.mkdir(parents=True, exist_ok=True)
    data = (json.dumps(value, indent=2, sort_keys=True) + "\n").encode("utf-8")
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(fd, "wb") as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
    except BaseException:
        path.unlink(missing_ok=True)
        raise


def replace_target_record(path: Path, value: dict[str, Any], *, operation_id: str) -> None:
    """Transition only the record owned by this acquisition operation."""
    try:
        current = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise RuntimeError(f"cannot verify RunPod custody at {path}: {exc}") from exc
    if current.get("operation_id") != operation_id:
        raise RuntimeError(
            f"RunPod custody at {path} belongs to another operation; refusing to overwrite it"
        )
    if value.get("operation_id") != operation_id:
        raise RuntimeError("replacement RunPod custody changed operation identity")
    write_target_record(path, value)


def _exclusive_target_owner(function: Any) -> Any:
    """Serialize acquisition/reconciliation and reject stale journal snapshots."""

    @wraps(function)
    async def wrapped(
        config: Any,
        *,
        handle_path: Path,
        attempt: dict[str, Any],
        **kwargs: Any,
    ) -> Any:
        path = handle_path.expanduser()
        path.parent.mkdir(parents=True, exist_ok=True)
        lock_path = path.with_name(f".{path.name}.owner.lock")
        fd = os.open(lock_path, os.O_CREAT | os.O_RDWR, 0o600)
        try:
            try:
                platform.lock_file(fd, blocking=False)
            except BlockingIOError as exc:
                raise AcquisitionUnresolvedError(
                    f"another process currently owns RunPod target journal {path}; retry after it exits"
                ) from exc
            try:
                current = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError) as exc:
                raise AcquisitionUnresolvedError(
                    f"cannot verify RunPod target journal {path} before provider use"
                ) from exc
            if (
                not isinstance(current, dict)
                or current != attempt
            ):
                raise AcquisitionUnresolvedError(
                    f"RunPod target journal {path} changed before this operation acquired ownership"
                )
            return await function(
                config,
                handle_path=handle_path,
                attempt=current,
                **kwargs,
            )
        finally:
            try:
                platform.unlock_file(fd)
            finally:
                os.close(fd)

    return wrapped


async def resolve_existing_volume(api_key: str, name_or_id: str) -> dict[str, Any]:
    """Resolve one existing account volume without creating or resizing it."""
    from runpod_lifecycle.api import get_network_volumes

    try:
        volumes = await asyncio.to_thread(get_network_volumes, api_key)
    except Exception as exc:
        raise TargetObservationError(
            f"could not observe RunPod network-volume inventory for {name_or_id!r}: {exc}"
        ) from exc
    if not isinstance(volumes, list):
        raise TargetObservationError("RunPod network-volume inventory returned an invalid response")

    matches = [
        record for record in volumes
        if isinstance(record, dict)
        and name_or_id in {_text(record.get("id")), _text(record.get("name"))}
    ]
    if len(matches) != 1:
        raise TargetObservationError(
            f"existing network volume {name_or_id!r} cannot be uniquely confirmed in the authenticated account"
        )
    size = _positive_integer(matches[0].get("size"))
    if size is None:
        raise TargetObservationError(
            f"existing network volume {name_or_id!r} has no valid provider size"
        )
    return dict(matches[0])


def _text(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    value = value.strip()
    return value or None


def _required_text(record: Mapping[str, Any], key: str, *, context: str) -> str:
    value = _text(record.get(key))
    if value is None:
        raise TargetObservationError(f"provider {context} is missing required {key}")
    return value


def _equivalent(left: str, right: str) -> bool:
    return " ".join(left.casefold().split()) == " ".join(right.casefold().split())


def _positive_integer(value: Any) -> int | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    if not math.isfinite(value) or int(value) != value or value <= 0:
        return None
    return int(value)


def inspect_target_records(
    contract: RunPodTargetContract,
    *,
    pods: list[dict[str, Any]],
    volumes: list[dict[str, Any]],
    allow_inactive: bool = False,
) -> RunPodTargetObservation:
    """Validate exact inventory identity; attachment checks also require readiness."""
    if not contract.pod_id.strip():
        raise ValueError("pod_id must be nonempty")
    if not contract.network_volume_id.strip():
        raise ValueError("network volume identity must be pinned by ID")
    if contract.network_volume_size_gb <= 0:
        raise ValueError("network volume size must be positive")

    pod_matches = [
        record for record in pods
        if isinstance(record, dict) and _text(record.get("id")) == contract.pod_id
    ]
    if len(pod_matches) != 1:
        raise TargetObservationError(
            f"exact pod {contract.pod_id!r} is not uniquely visible in the authenticated account inventory"
        )
    pod = pod_matches[0]

    pod_name = _required_text(pod, "name", context=f"pod {contract.pod_id}")
    if contract.expected_pod_name and pod_name != contract.expected_pod_name:
        raise TargetObservationError(
            f"pod {contract.pod_id!r} name mismatch: expected {contract.expected_pod_name!r}, observed {pod_name!r}"
        )

    desired_status = _required_text(
        pod, "desiredStatus", context=f"pod {contract.pod_id}"
    ).upper()
    if not allow_inactive and desired_status not in {"RUNNING", "PROVISIONING"}:
        raise TargetObservationError(
            f"pod {contract.pod_id!r} is not attachable (desiredStatus={desired_status!r})"
        )
    actual_status = _text(pod.get("actualStatus"))
    if (
        not allow_inactive
        and actual_status
        and actual_status.upper() not in {"RUNNING", "PROVISIONING"}
    ):
        raise TargetObservationError(
            f"pod {contract.pod_id!r} is not currently attachable (actualStatus={actual_status!r})"
        )

    machine = pod.get("machine") if isinstance(pod.get("machine"), dict) else {}
    gpu_candidates = [
        _text(pod.get("machineType")),
        _text(machine.get("gpuDisplayName")),
        _text(machine.get("gpuTypeId")),
    ]
    observed_gpu = next(
        (candidate for candidate in gpu_candidates if candidate and _equivalent(candidate, contract.gpu_type)),
        None,
    )
    if observed_gpu is None:
        raise TargetObservationError(
            f"pod {contract.pod_id!r} GPU mismatch: expected {contract.gpu_type!r}, "
            f"observed {[item for item in gpu_candidates if item]!r}"
        )

    image = _required_text(pod, "imageName", context=f"pod {contract.pod_id}")
    if image != contract.worker_image:
        raise TargetObservationError(
            f"pod {contract.pod_id!r} image mismatch: expected {contract.worker_image!r}, observed {image!r}"
        )

    attached_volume_id = _required_text(
        pod, "networkVolumeId", context=f"pod {contract.pod_id}"
    )
    if attached_volume_id != contract.network_volume_id:
        raise TargetObservationError(
            f"pod {contract.pod_id!r} volume mismatch: expected {contract.network_volume_id!r}, "
            f"observed {attached_volume_id!r}"
        )

    volume = inspect_volume_records(contract, volumes=volumes)
    volume_name = _required_text(volume, "name", context=f"volume {contract.network_volume_id}")
    size = _positive_integer(volume.get("size"))
    datacenter_id = _text(volume.get("dataCenterId"))

    return RunPodTargetObservation(
        pod_id=contract.pod_id,
        pod_name=pod_name,
        desired_status=desired_status,
        actual_status=actual_status,
        gpu_type=observed_gpu,
        worker_image=image,
        network_volume_id=contract.network_volume_id,
        network_volume_name=volume_name,
        network_volume_size_gb=size,
        datacenter_id=datacenter_id,
        observed_at=datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
    )


def inspect_volume_records(
    contract: RunPodTargetContract,
    *,
    volumes: list[dict[str, Any]],
) -> dict[str, Any]:
    """Prove the pinned volume's account identity and immutable sizing."""
    if not contract.network_volume_id.strip():
        raise ValueError("network volume identity must be pinned by ID")
    if contract.network_volume_size_gb <= 0:
        raise ValueError("network volume size must be positive")
    volume_matches = [
        record
        for record in volumes
        if isinstance(record, dict)
        and _text(record.get("id")) == contract.network_volume_id
    ]
    if len(volume_matches) != 1:
        raise TargetObservationError(
            f"network volume {contract.network_volume_id!r} cannot be confirmed in the authenticated account inventory"
        )
    volume = volume_matches[0]
    volume_name = _required_text(
        volume, "name", context=f"volume {contract.network_volume_id}"
    )
    if contract.network_volume_name and volume_name != contract.network_volume_name:
        raise TargetObservationError(
            f"network volume {contract.network_volume_id!r} name mismatch: "
            f"expected {contract.network_volume_name!r}, observed {volume_name!r}"
        )
    size = _positive_integer(volume.get("size"))
    if size != contract.network_volume_size_gb:
        raise TargetObservationError(
            f"network volume {contract.network_volume_id!r} size mismatch: "
            f"expected {contract.network_volume_size_gb} GB, observed {size!r}"
        )
    datacenter_id = _text(volume.get("dataCenterId"))
    if contract.datacenter_id and datacenter_id != contract.datacenter_id:
        raise TargetObservationError(
            f"network volume {contract.network_volume_id!r} datacenter mismatch: "
            f"expected {contract.datacenter_id!r}, observed {datacenter_id!r}"
        )
    return dict(volume)


async def observe_exact_target(
    api_key: str,
    contract: RunPodTargetContract,
) -> RunPodTargetObservation:
    """Read exact pod and volume identity from this credential's account inventory."""
    return await asyncio.to_thread(observe_target, api_key, contract)


def _target_contract(
    config: Any,
    *,
    pod_id: str,
    expected_name: str | None,
    volume: Mapping[str, Any],
) -> RunPodTargetContract:
    return RunPodTargetContract(
        pod_id=pod_id,
        expected_pod_name=expected_name,
        gpu_type=config.gpu_type_candidates[0],
        worker_image=config.worker_image,
        network_volume_id=str(volume.get("id") or ""),
        network_volume_name=_text(volume.get("name")),
        network_volume_size_gb=int(volume["size"]),
    )


async def _wait_and_observe_target(
    config: Any,
    pod: Any,
    *,
    expected_name: str | None,
    volume: Mapping[str, Any],
    ready_timeout_seconds: int,
) -> RunPodTargetObservation:
    await pod.wait_ready(timeout=ready_timeout_seconds)
    return await observe_exact_target(
        config.api_key,
        _target_contract(
            config,
            pod_id=str(pod.id),
            expected_name=expected_name,
            volume=volume,
        ),
    )


def _write_target_failure(
    handle_path: Path,
    attempt: dict[str, Any],
    *,
    operation_id: str,
    phase: str,
    exc: BaseException,
) -> None:
    failed = {
        **attempt,
        "state": "allocated_unverified",
        "failure_phase": phase,
        "failure_type": type(exc).__name__,
        "failed_at": utc_now_iso(),
        "reconciliation_required": True,
    }
    replace_target_record(handle_path, failed, operation_id=operation_id)


def observe_target(
    api_key: str,
    contract: RunPodTargetContract,
) -> RunPodTargetObservation:
    """Synchronous provider observation for CLI and executor boundaries."""
    from runpod_lifecycle.api import get_network_volumes, list_pods

    try:
        pods = list_pods(api_key)
    except Exception as exc:
        raise TargetObservationError(
            f"could not observe RunPod account pod inventory for exact pod {contract.pod_id!r}: {exc}"
        ) from exc
    if not isinstance(pods, list):
        raise TargetObservationError("RunPod account pod inventory returned an invalid response")

    try:
        volumes = get_network_volumes(api_key)
    except Exception as exc:
        raise TargetObservationError(
            f"could not confirm network volume {contract.network_volume_id!r}: {exc}"
        ) from exc
    if not isinstance(volumes, list):
        raise TargetObservationError("RunPod network-volume inventory returned an invalid response")

    # get_network_volumes currently degrades some transport errors to an empty
    # list. The record validator treats empty/missing evidence as unconfirmed,
    # never as proof that a volume was deleted or belongs to another account.
    return inspect_target_records(contract, pods=pods, volumes=volumes)


async def _release_inventory(
    config: Any,
    contract: RunPodTargetContract,
) -> tuple[RunPodTargetObservation | None, dict[str, Any]]:
    """Observe one exact pod (if present) and its still-pinned volume."""
    from runpod_lifecycle.api import get_network_volumes, list_pods

    try:
        pods = await asyncio.to_thread(list_pods, config.api_key)
    except Exception as exc:
        raise TargetObservationError(
            f"could not observe RunPod account pod inventory for exact pod {contract.pod_id!r}: {exc}"
        ) from exc
    if not isinstance(pods, list):
        raise TargetObservationError("RunPod account pod inventory returned an invalid response")

    try:
        volumes = await asyncio.to_thread(get_network_volumes, config.api_key)
    except Exception as exc:
        raise TargetObservationError(
            f"could not confirm network volume {contract.network_volume_id!r}: {exc}"
        ) from exc
    if not isinstance(volumes, list):
        raise TargetObservationError("RunPod network-volume inventory returned an invalid response")
    volume = inspect_volume_records(contract, volumes=volumes)

    matches = [
        pod for pod in pods
        if isinstance(pod, dict) and _text(pod.get("id")) == contract.pod_id
    ]
    if not matches:
        return None, volume
    observation = inspect_target_records(
        contract,
        pods=matches,
        volumes=volumes,
        allow_inactive=True,
    )
    return observation, volume


def _release_contract_from_record(attempt: Mapping[str, Any]) -> RunPodTargetContract:
    pod_id = _text(attempt.get("pod_id"))
    gpu_type = _text(attempt.get("gpu_type"))
    worker_image = _text(attempt.get("worker_image"))
    volume_id = _text(attempt.get("network_volume_id"))
    volume_name = _text(attempt.get("network_volume_name")) or _text(
        attempt.get("storage_name")
    )
    volume_size = _positive_integer(attempt.get("network_volume_size_gb"))
    if not all((pod_id, gpu_type, worker_image, volume_id, volume_name, volume_size)):
        raise TargetObservationError(
            "release record is missing exact pod, GPU, image or persistent-volume identity"
        )
    return RunPodTargetContract(
        pod_id=pod_id,
        expected_pod_name=_text(attempt.get("name")) or _text(attempt.get("request_name")),
        gpu_type=gpu_type,
        worker_image=worker_image,
        network_volume_id=volume_id,
        network_volume_name=volume_name,
        network_volume_size_gb=volume_size,
        datacenter_id=_text(attempt.get("network_volume_datacenter_id")),
    )


def _validate_release_quiescence(value: Any, attempt: Mapping[str, Any]) -> dict[str, Any]:
    """Accept only durable exact-task drain and process-stop evidence."""
    if not isinstance(value, Mapping) or value.get("schema") != "astrid.runpod.worker-quiescence.v1":
        raise TargetObservationError("release requires exact persisted worker-quiescence evidence")
    if (value.get("operation_id") != attempt.get("operation_id")
            or value.get("pod_id") != attempt.get("pod_id")
            or value.get("provider_account_ref") != attempt.get("provider_account_ref")):
        raise TargetObservationError("worker-quiescence evidence belongs to another RunPod claim")
    for field in ("task_id", "activation_id", "runtime_instance_id", "runtime_session_id"):
        if not isinstance(value.get(field), str) or not value[field]:
            raise TargetObservationError(f"worker-quiescence evidence is missing {field}")
    if type(value.get("runtime_epoch")) is not int or value["runtime_epoch"] <= 0:
        raise TargetObservationError("worker-quiescence evidence has an invalid Runtime epoch")
    drain = value.get("drain")
    if (not isinstance(drain, Mapping) or drain.get("state") != "complete"
            or drain.get("task_id") != value["task_id"]
            or drain.get("activation_id") != value["activation_id"]):
        raise TargetObservationError("Runtime did not confirm this exact task activation drain")
    stopped = value.get("process_stop")
    if (not isinstance(stopped, Mapping) or stopped.get("stopped") is not True
            or type(stopped.get("pid")) is not int or stopped["pid"] <= 0
            or not all(isinstance(stopped.get(field), str) and stopped[field]
                       for field in ("birth_id", "incarnation"))):
        raise TargetObservationError("exact managed-worker process stop is not positively recorded")
    managed = attempt.get("managed_worker")
    if managed is not None:
        if not isinstance(managed, Mapping):
            raise TargetObservationError("managed-worker custody is invalid")
        qualification = managed.get("activation_qualification")
        process = managed.get("process_handle")
        if not isinstance(qualification, Mapping) or not isinstance(process, Mapping):
            raise TargetObservationError("managed-worker release identities are missing")
        binding = process.get("binding")
        if (not isinstance(binding, Mapping)
                or any(value.get(key) != qualification.get(key) for key in (
                    "task_id", "activation_id", "runtime_session_id", "runtime_epoch",
                ))
                or value["runtime_instance_id"] != binding.get("runtime_instance_id")
                or qualification.get("executor_incarnation") != process.get("incarnation")
                or binding.get("pod_id") != attempt.get("pod_id")
                or binding.get("account_ref") != attempt.get("provider_account_ref")
                or any(stopped.get(key) != process.get(key) for key in ("pid", "birth_id", "incarnation"))):
            raise TargetObservationError("quiescence differs from the persisted worker generation")
        if value.get("runtime_drain") != {"state": "drained", "activation_id": value["activation_id"]}:
            raise TargetObservationError("release lacks the exact Runtime drain response")
        if value.get("credential_remove") != {"removed": True}:
            raise TargetObservationError("release lacks exact remote Runtime credential cleanup evidence")
        fence = value.get("release_fence")
        expected_fence = {
            "retired": True, "operation_id": attempt["operation_id"], "provider": "runpod",
            "account_ref": attempt["provider_account_ref"], "pod_id": attempt["pod_id"],
            "incarnation": process["incarnation"],
        }
        if (not isinstance(fence, Mapping) or set(fence) != {*expected_fence, "release_fence_digest"}
                or fence.get("retired") is not True
                or any(fence.get(key) != item for key, item in expected_fence.items())
                or not isinstance(fence.get("release_fence_digest"), str)
                or not re.fullmatch(r"sha256:[0-9a-f]{64}", fence["release_fence_digest"])):
            raise TargetObservationError("release lacks the exact physical target fence")
    managed_session = attempt.get("managed_session")
    if managed_session is not None:
        stopped_session = value.get("session_stop")
        if (not isinstance(managed_session, Mapping)
                or managed_session.get("schema") != "astrid.runpod.managed-session.v1"
                or not isinstance(stopped_session, Mapping)
                or stopped_session.get("state") != "stopped"
                or stopped_session.get("session_ref") != managed_session.get("session_ref")
                or stopped_session.get("config_digest") != managed_session.get("config_digest")
                or stopped_session.get("gpu_qualified") is not False):
            raise TargetObservationError("release lacks exact managed VibeComfy session stop evidence")
    return json.loads(json.dumps(dict(value), sort_keys=True, separators=(",", ":")))


def _read_release_checkpoint(path: Path, expected: Mapping[str, Any]) -> dict[str, Any]:
    """Read callback progress while the enclosing P1 owner lock remains held."""
    try:
        current = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise TargetObservationError("release checkpoint cannot be read safely") from exc
    if (not isinstance(current, dict) or current.get("state") != "release_pending"
            or {k: v for k, v in current.items() if k != "release_progress"}
            != {k: v for k, v in expected.items() if k != "release_progress"}):
        raise TargetObservationError("release custody changed during its quiescence callback")
    return current


def _checkpoint_release_progress(
    path: Path, expected: Mapping[str, Any], progress: Mapping[str, Any],
) -> None:
    """Persist only progress; caller must be P1's lock-held quiescence callback.

    No second flock is acquired. The callback cannot replace the claim,
    activation generation, process handle, or provider-release state.
    """
    current = _read_release_checkpoint(path, expected)
    replace_target_record(
        path, {**current, "release_progress": dict(progress)},
        operation_id=str(current["operation_id"]),
    )


@_exclusive_target_owner
async def _release_exact_target_after_quiescence(
    config: Any,
    *,
    handle_path: Path,
    attempt: dict[str, Any],
    quiesce_worker: Callable[[Mapping[str, Any]], Awaitable[Mapping[str, Any]]],
    resume: bool = False,
    verify_timeout_seconds: float = 90.0,
) -> dict[str, Any]:
    """Release the journal's exact pod after its owner proves it is quiescent.

    ``quiesce_worker`` is the P3 Runtime/process boundary: it must stop new
    claims, reconcile already-claimed work, and positively stop this exact
    worker incarnation. It must raise on active or uncertain work. This
    provider helper deliberately does not infer task safety from a claim
    handle or a caller-supplied boolean.
    """
    from runpod_lifecycle.api import reconcile_pod_cleanup, terminate_pod
    from runpod_lifecycle.errors import CleanupPendingError

    operation_id = str(attempt.get("operation_id") or "")
    state = str(attempt.get("state") or "")
    if not operation_id:
        raise AcquisitionUnresolvedError("release record has no operation ID")
    if attempt.get("release_authorized") is not True:
        raise TargetObservationError(
            "this attached RunPod target has no explicit exclusive release authority"
        )
    if not callable(quiesce_worker):
        raise TypeError("quiesce_worker must be the prepared-worker drain owner")
    if state == "cleanup_complete" and resume:
        _validate_release_quiescence(attempt.get("release_quiescence"), attempt)
        cleanup = attempt.get("cleanup")
        if (not isinstance(cleanup, Mapping) or cleanup.get("pod_id") != attempt.get("pod_id")
                or cleanup.get("backup_volume_id") != attempt.get("network_volume_id")
                or cleanup.get("backup_volume_preserved") is not True
                or cleanup.get("status") not in {"already_gone", "terminated"}
                or attempt.get("cleanup_status") != "complete"
                or attempt.get("reconciliation_required") is not False):
            raise TargetObservationError("completed release lacks its exact provider cleanup receipt")
        return attempt
    if state == "claimed" and resume:
        raise AcquisitionUnresolvedError("a claimed target has no interrupted release to resume")
    if state in {"release_pending", "cleanup_pending"} and not resume:
        raise AcquisitionUnresolvedError(
            "this target already has an interrupted release; resume that exact release"
        )
    if state not in {"claimed", "release_pending", "cleanup_pending"}:
        raise AcquisitionUnresolvedError(
            f"journal state {state!r} is not a releasable prepared target"
        )

    contract = _release_contract_from_record(attempt)
    pending = {
        **attempt,
        "state": "release_pending",
        "release_requested_at": utc_now_iso(),
        "reconciliation_required": True,
    }
    replace_target_record(handle_path, pending, operation_id=operation_id)
    attempt = pending

    saved_quiescence = attempt.get("release_quiescence")
    if saved_quiescence is not None:
        if not resume:
            raise AcquisitionUnresolvedError(
                "release already has durable quiescence evidence; resume that exact release"
            )
        quiescence = _validate_release_quiescence(saved_quiescence, attempt)
    else:
        try:
            result = await quiesce_worker(dict(attempt))
            attempt = _read_release_checkpoint(handle_path, attempt)
            quiescence = _validate_release_quiescence(result, attempt)
        except BaseException as exc:
            attempt = _read_release_checkpoint(handle_path, attempt)
            blocked = {
                **attempt,
                "last_release_gate_failure_type": type(exc).__name__,
                "last_release_gate_failure_at": utc_now_iso(),
            }
            replace_target_record(handle_path, blocked, operation_id=operation_id)
            raise
        # Publication may fail before or after atomic replacement. Preserve
        # either exact disk state for resume instead of overwriting it with a
        # stale pre-publication snapshot in the callback-failure handler.
        attempt = {
            **attempt,
            "release_quiescence": quiescence,
            "release_quiescence_recorded_at": utc_now_iso(),
        }
        replace_target_record(handle_path, attempt, operation_id=operation_id)

    if resume:
        try:
            # First reconcile a possibly completed delete. This operation is
            # exact-ID only and never mutates provider state.
            await asyncio.to_thread(
                reconcile_pod_cleanup,
                contract.pod_id,
                config.api_key,
                verify_timeout_seconds=0,
                poll_interval_seconds=0,
            )
        except CleanupPendingError:
            # The same exact pod remains visible. The identity recheck below
            # authorizes an idempotent retry of DELETE /pods/{pod_id}.
            pass
        except Exception as exc:
            unresolved = {
                **attempt,
                "state": "cleanup_pending",
                "cleanup_failure_type": type(exc).__name__,
                "cleanup_failure_at": utc_now_iso(),
                "reconciliation_required": True,
            }
            replace_target_record(handle_path, unresolved, operation_id=operation_id)
            raise

    try:
        observed, volume = await _release_inventory(config, contract)
    except BaseException as exc:
        unresolved = {
            **attempt,
            "state": "cleanup_pending" if resume else "release_pending",
            "cleanup_failure_type": type(exc).__name__,
            "cleanup_failure_at": utc_now_iso(),
            "reconciliation_required": True,
        }
        replace_target_record(handle_path, unresolved, operation_id=operation_id)
        raise

    cleanup_status = "already_gone" if observed is None else "terminated"
    if observed is not None:
        releasing = {
            **attempt,
            "state": "release_pending",
            "release_pod_observation": asdict(observed),
            "release_delete_started_at": utc_now_iso(),
        }
        replace_target_record(handle_path, releasing, operation_id=operation_id)
        attempt = releasing
        try:
            await asyncio.to_thread(terminate_pod, contract.pod_id, config.api_key)
            after, volume = await _release_inventory(config, contract)
            if after is not None:
                raise CleanupPendingError(
                    contract.pod_id,
                    "pod remained present after exact-ID termination and follow-up observation",
                )
        except BaseException as exc:
            unresolved = {
                **attempt,
                "state": "cleanup_pending",
                "cleanup_failure_type": type(exc).__name__,
                "cleanup_failure_at": utc_now_iso(),
                "reconciliation_required": True,
            }
            replace_target_record(handle_path, unresolved, operation_id=operation_id)
            raise

    cleanup = {
        "pod_id": contract.pod_id,
        "status": cleanup_status,
        "backup_volume_id": str(volume.get("id")),
        "backup_volume_preserved": True,
        "confirmed_at": utc_now_iso(),
    }
    complete = {
        **attempt,
        "state": "cleanup_complete",
        "cleanup_status": "complete",
        "cleanup": cleanup,
        "reconciliation_required": False,
    }
    replace_target_record(handle_path, complete, operation_id=operation_id)
    return complete


@_exclusive_target_owner
async def acquire_target(
    config: Any,
    *,
    handle_path: Path,
    attempt: dict[str, Any],
    volume: dict[str, Any],
    pod_id: str | None,
    max_wait_seconds: int,
    capacity_window_seconds: int,
    retry_interval_seconds: int,
    ready_timeout_seconds: int,
    allow_capacity_retry: bool = False,
) -> AcquiredRunPodTarget:
    """Allocate from a fresh/safe intent or attach to one exact pod.

    The caller must durably create ``attempt`` before calling this function.
    Ambiguous/nonretryable outcomes remain in that record, and this function
    never converts missing evidence into a replacement allocation. A persisted
    in-flight attempt must go through :func:`reconcile_target` first.
    """
    from runpod_lifecycle import AllocationUnknown, LaunchFailure, get_pod, launch_when_available

    operation_id = str(attempt["operation_id"])
    mode = "attach" if pod_id is not None else "allocate"
    initial_state = str(attempt.get("state") or "")

    if pod_id is not None:
        if initial_state != "attachment_pending":
            raise AcquisitionUnresolvedError(
                f"exact attachment is not a fresh intent (state={initial_state!r}); resume by exact pod ID"
            )
        requested_pod_id = str(pod_id).strip()
        if not requested_pod_id:
            raise ValueError("pod_id must be nonempty")
        try:
            initial = await observe_exact_target(
                config.api_key,
                RunPodTargetContract(
                    pod_id=requested_pod_id,
                    expected_pod_name=None,
                    gpu_type=config.gpu_type_candidates[0],
                    worker_image=config.worker_image,
                    network_volume_id=str(volume.get("id") or ""),
                    network_volume_name=str(volume.get("name") or ""),
                    network_volume_size_gb=int(volume["size"]),
                ),
            )
            pod = await get_pod(requested_pod_id, config, name=initial.pod_name)
            if str(getattr(pod, "id", "")) != requested_pod_id or str(
                getattr(pod, "name", "")
            ) != initial.pod_name:
                raise TargetObservationError(
                    f"RunPod attach returned pod identity "
                    f"({getattr(pod, 'id', None)!r}, {getattr(pod, 'name', None)!r}) "
                    f"for requested exact pod ({requested_pod_id!r}, {initial.pod_name!r})"
                )
        except BaseException as exc:
            unresolved = {
                **attempt,
                "state": "attached_unverified",
                "requested_pod_id": requested_pod_id,
                "failure_type": type(exc).__name__,
                "failed_at": utc_now_iso(),
                "reconciliation_required": True,
            }
            replace_target_record(handle_path, unresolved, operation_id=operation_id)
            raise
        expected_name = initial.pod_name
    else:
        if initial_state not in {
            "allocation_ready",
            "capacity_retry_ready",
            "capacity_exhausted",
        }:
            raise AcquisitionUnresolvedError(
                f"allocation intent may already be in flight (state={initial_state!r}); "
                "reconcile the existing request before another create"
            )
        if initial_state != "allocation_ready" and not allow_capacity_retry:
            raise AcquisitionUnresolvedError(
                "a prior capacity response must be explicitly resumed before another create"
            )
        create_attempts = attempt.get("create_attempts", 0)
        if isinstance(create_attempts, bool) or not isinstance(create_attempts, int):
            raise AcquisitionUnresolvedError(
                "allocation journal has an invalid create_attempts count"
            )
        if initial_state == "allocation_ready" and create_attempts != 0:
            raise AcquisitionUnresolvedError(
                "allocation journal says a create already started; reconcile it before another create"
            )
        if (
            initial_state == "capacity_retry_ready" and create_attempts <= 0
        ) or (
            initial_state == "capacity_exhausted" and create_attempts < 0
        ) or (
            initial_state in {"capacity_retry_ready", "capacity_exhausted"}
            and attempt.get("safe_to_retry") is not True
        ):
            raise AcquisitionUnresolvedError(
                "allocation journal does not prove the prior capacity response was safe to retry"
            )
        expected_name = str(attempt.get("request_name") or "").strip()
        if not expected_name:
            raise ValueError("allocation attempt must have an exact request_name")
        deadline = (
            time.monotonic() + max_wait_seconds
            if max_wait_seconds > 0
            else None
        )
        while True:
            if deadline is not None:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    exhausted = {
                        **attempt,
                        "state": "capacity_exhausted",
                        "reconciliation_required": False,
                        "safe_to_retry": True,
                        "completed_at": utc_now_iso(),
                    }
                    replace_target_record(
                        handle_path, exhausted, operation_id=operation_id
                    )
                    raise TimeoutError(
                        f"capacity did not appear within {max_wait_seconds}s"
                    )
                window = min(capacity_window_seconds, max(1, int(remaining)))
            else:
                window = capacity_window_seconds

            pending = {
                **attempt,
                "state": "allocation_pending",
                "create_attempts": int(attempt.get("create_attempts", 0)) + 1,
                "reconciliation_required": True,
                "safe_to_retry": False,
                "last_create_started_at": utc_now_iso(),
            }
            replace_target_record(handle_path, pending, operation_id=operation_id)
            attempt = pending
            try:
                pod = await launch_when_available(
                    config,
                    name=expected_name,
                    max_wait_sec=window,
                    retry_interval_sec=retry_interval_seconds,
                )
            except AllocationUnknown as exc:
                unknown = {
                    **attempt,
                    "state": "allocation_unknown",
                    "reconciliation_required": True,
                    "unresolved_at": utc_now_iso(),
                    "gpu_type_attempted": exc.gpu_type,
                    "ram_tier_attempted": exc.ram_tier,
                    "storage_name_attempted": exc.storage_name,
                    "storage_volume_id_attempted": exc.storage_volume_id,
                }
                replace_target_record(handle_path, unknown, operation_id=operation_id)
                raise
            except LaunchFailure as exc:
                if not getattr(exc, "retryable", True):
                    known_pod_id = getattr(exc, "pod_id", None)
                    failed = {
                        **attempt,
                        "state": "allocated_unverified" if known_pod_id else "allocation_unknown",
                        "failure_type": type(exc).__name__,
                        "failed_at": utc_now_iso(),
                        "reconciliation_required": True,
                    }
                    if known_pod_id:
                        failed["pod_id"] = str(known_pod_id)
                    replace_target_record(handle_path, failed, operation_id=operation_id)
                    raise
                retry_ready = {
                    **attempt,
                    "state": "capacity_retry_ready",
                    "reconciliation_required": False,
                    "safe_to_retry": True,
                    "last_capacity_rejection_at": utc_now_iso(),
                    "last_failure_type": type(exc).__name__,
                }
                replace_target_record(
                    handle_path, retry_ready, operation_id=operation_id
                )
                attempt = retry_ready
                continue

            allocated = {
                **attempt,
                "state": "allocated_unverified",
                "pod_id": str(pod.id),
                "name": str(pod.name),
                "allocated_at": utc_now_iso(),
                "reconciliation_required": True,
                "safe_to_retry": False,
            }
            replace_target_record(handle_path, allocated, operation_id=operation_id)
            break

    if mode == "attach":
        allocated = {
            **attempt,
            "state": "attached_unverified",
            "pod_id": str(pod.id),
            "name": str(pod.name),
            "allocated_at": utc_now_iso(),
            "reconciliation_required": True,
        }
        replace_target_record(handle_path, allocated, operation_id=operation_id)

    try:
        if expected_name is not None and str(getattr(pod, "name", "")) != expected_name:
            raise TargetObservationError(
                f"RunPod launch returned pod name {getattr(pod, 'name', None)!r}; "
                f"expected exact request name {expected_name!r}"
            )
        await pod.wait_ready(timeout=ready_timeout_seconds)
    except BaseException as exc:
        failed = {
            **allocated,
            "state": "allocated_unverified",
            "failure_phase": "readiness",
            "failure_type": type(exc).__name__,
            "failed_at": utc_now_iso(),
            "reconciliation_required": True,
        }
        replace_target_record(handle_path, failed, operation_id=operation_id)
        raise

    try:
        observation = await observe_exact_target(
            config.api_key,
            _target_contract(
                config,
                pod_id=str(pod.id),
                expected_name=expected_name,
                volume=volume,
            ),
        )
    except BaseException as exc:
        failed = {
            **allocated,
            "state": "allocated_unverified",
            "failure_phase": "provider_contract",
            "failure_type": type(exc).__name__,
            "failed_at": utc_now_iso(),
            "reconciliation_required": True,
        }
        replace_target_record(handle_path, failed, operation_id=operation_id)
        raise

    verified = {
        **allocated,
        "provider_observation": observation.to_dict(),
        "target_identity_verified": True,
    }
    replace_target_record(handle_path, verified, operation_id=operation_id)
    return AcquiredRunPodTarget(
        pod=pod,
        volume=dict(volume),
        attempt=verified,
        observation=observation,
        acquisition_mode=mode,
    )


@_exclusive_target_owner
async def reconcile_target(
    config: Any,
    *,
    handle_path: Path,
    attempt: dict[str, Any],
    volume: dict[str, Any],
    ready_timeout_seconds: int,
) -> AcquiredRunPodTarget:
    """Resume one persisted target without opening a replacement allocation."""
    from runpod_lifecycle import get_pod
    from runpod_lifecycle.api import list_pods

    operation_id = str(attempt["operation_id"])
    mode = str(attempt.get("acquisition_mode") or "allocate")
    state = str(attempt.get("state") or "")
    if state not in {
        "allocation_ready",
        "allocation_pending",
        "allocation_unknown",
        "allocated_unverified",
        "attachment_pending",
        "attached_unverified",
    }:
        raise AcquisitionUnresolvedError(
            f"journal state {state!r} does not require reconciliation"
        )

    pod_id = _text(attempt.get("pod_id"))
    if mode == "attach" and pod_id is None:
        pod_id = _text(attempt.get("requested_pod_id"))

    if mode == "allocate" and pod_id is None:
        request_name = _text(attempt.get("request_name"))
        if request_name is None:
            raise AcquisitionUnresolvedError(
                "allocation journal has no exact request name; cannot reconcile"
            )
        started = {
            **attempt,
            "state": "allocation_unknown",
            "last_reconcile_started_at": utc_now_iso(),
            "reconciliation_required": True,
        }
        replace_target_record(handle_path, started, operation_id=operation_id)
        attempt = started
        try:
            pods = await asyncio.to_thread(list_pods, config.api_key)
        except Exception as exc:
            failed_observation = {
                **attempt,
                "last_reconciled_at": utc_now_iso(),
                "last_reconciliation_failure_type": type(exc).__name__,
            }
            replace_target_record(
                handle_path, failed_observation, operation_id=operation_id
            )
            raise AcquisitionUnresolvedError(
                "RunPod account inventory is unavailable; the original allocation remains unresolved"
            ) from exc
        if not isinstance(pods, list):
            raise AcquisitionUnresolvedError(
                "RunPod account inventory returned malformed data; the original allocation remains unresolved"
            )

        # Count every exact-name match before checking the desired resource
        # contract. A valid-looking match cannot disambiguate a duplicate name.
        matches = [
            record for record in pods
            if isinstance(record, dict) and _text(record.get("name")) == request_name
        ]
        if len(matches) != 1:
            unresolved = {
                **attempt,
                "state": "allocation_unknown",
                "last_reconciled_at": utc_now_iso(),
                "last_reconciliation_match_count": len(matches),
                "last_reconciliation_candidate_ids": [
                    value
                    for record in matches
                    if (value := _text(record.get("id"))) is not None
                ],
                "reconciliation_required": True,
            }
            replace_target_record(handle_path, unresolved, operation_id=operation_id)
            raise AcquisitionUnresolvedError(
                f"request {request_name!r} has {len(matches)} exact-name matches; "
                "preserve custody and retry reconciliation later"
            )

        pod_id = _text(matches[0].get("id"))
        if pod_id is None:
            unresolved = {
                **attempt,
                "state": "allocation_unknown",
                "last_reconciled_at": utc_now_iso(),
                "last_reconciliation_failure_type": "MissingPodId",
                "reconciliation_required": True,
            }
            replace_target_record(handle_path, unresolved, operation_id=operation_id)
            raise AcquisitionUnresolvedError(
                "the exact-name provider record has no pod ID; the original allocation remains unresolved"
            )

    expected_name = _text(attempt.get("name"))
    if expected_name is None:
        expected_name = _text(attempt.get("request_name"))

    bound = {
        **attempt,
        "state": "attached_unverified" if mode == "attach" else "allocated_unverified",
        "pod_id": pod_id,
        "name": expected_name,
        "reconciled_pod_id_at": utc_now_iso(),
        "reconciliation_required": True,
    }
    replace_target_record(handle_path, bound, operation_id=operation_id)

    try:
        initial_observation = await observe_exact_target(
            config.api_key,
            _target_contract(
                config,
                pod_id=pod_id,
                expected_name=expected_name,
                volume=volume,
            ),
        )
        if expected_name is None:
            expected_name = initial_observation.pod_name
            bound["name"] = expected_name
            replace_target_record(handle_path, bound, operation_id=operation_id)
        pod = await get_pod(pod_id, config, name=expected_name)
        if str(getattr(pod, "id", "")) != pod_id or (
            expected_name is not None
            and str(getattr(pod, "name", "")) != expected_name
        ):
            raise TargetObservationError(
                f"RunPod lookup returned pod identity "
                f"({getattr(pod, 'id', None)!r}, {getattr(pod, 'name', None)!r}) "
                f"for exact reconciled pod ({pod_id!r}, {expected_name!r})"
            )
    except BaseException as exc:
        _write_target_failure(
            handle_path,
            bound,
            operation_id=operation_id,
            phase="provider_contract_or_lookup",
            exc=exc,
        )
        raise

    try:
        observation = await _wait_and_observe_target(
            config,
            pod,
            expected_name=expected_name,
            volume=volume,
            ready_timeout_seconds=ready_timeout_seconds,
        )
    except BaseException as exc:
        _write_target_failure(
            handle_path,
            bound,
            operation_id=operation_id,
            phase="readiness_or_provider_contract",
            exc=exc,
        )
        raise

    verified = {
        **bound,
        "name": observation.pod_name,
        "provider_observation": observation.to_dict(),
        "target_identity_verified": True,
        "last_reconciled_at": utc_now_iso(),
    }
    replace_target_record(handle_path, verified, operation_id=operation_id)
    return AcquiredRunPodTarget(
        pod=pod,
        volume=dict(volume),
        attempt=verified,
        observation=observation,
        acquisition_mode=mode,
    )
