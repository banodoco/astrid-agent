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
import stat
import tempfile
import threading
import time
import uuid
from contextlib import contextmanager
from dataclasses import asdict, dataclass
from dataclasses import replace as _dc_replace
from datetime import datetime, timezone
from functools import wraps
from pathlib import Path
from typing import Any, Awaitable, Callable, Literal, Mapping, cast

from runtime_protocol import platform

from astrid.core.generation.engine_contracts import Gate, PlacementRefused
from astrid.core.util.time import utc_now_iso

from .lease_states import LeaseEvent, apply_lease_event, journal_dispatch_ticket, lease_now
from .pod_fields import InvalidPodField, pod_field
from .provider import (
    ProviderNotStarted,
    ProviderRefused,
    account_gate,
    async_provider_call,
    async_provider_operation,
    bounded_observation,
    provider_budget,
    provider_call,
)


def _target_event(
    record: Mapping[str, Any], event: LeaseEvent, *,
    changes: Mapping[str, Any] | None = None,
    evidence: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Encode a journal lifecycle decision through the canonical lease table."""
    proof = dict(evidence or {})
    if event in {
        LeaseEvent.CREATE_SUCCEEDED, LeaseEvent.LATE_CREATE_SUCCEEDED,
        LeaseEvent.CREATE_FAILED_EXACT, LeaseEvent.CREATE_NOT_SENT,
        LeaseEvent.CAPACITY_REJECTED,
    }:
        control = record.get("lt_control")
        attempt_id = control.get("create_attempt") if isinstance(control, Mapping) else record.get("create_attempt")
        if attempt_id and "attempt_id" not in proof:
            proof["attempt_id"] = attempt_id
    return cast(dict[str, Any], apply_lease_event(
        record, event, now=lease_now(), changes=changes, evidence=proof,
    ))

_SHARED_GATES = {
    "GPU": "hardware", "volume": "storage", "datacenter": "region",
    "machine": "hardware", "status": "state", "provider": "inventory",
    "offer": "inventory", "unclassified": "inventory",
}


class TargetObservationError(PlacementRefused):
    """Provider observation refused; retain the provider gate for old receipts."""

    def __init__(self, message: str, *, gate: str, retryable: bool = False,
                 retry_after: float | None = None,
                 custody: Literal["none", "exact", "unknown"] = "unknown") -> None:
        self.runpod_gate = gate
        super().__init__(message, gate=cast(Gate, _SHARED_GATES.get(gate, gate)
                                         if isinstance(gate, str) else "inventory"),
                         retryable=retryable, retry_after=retry_after, custody=custody)


def observation_failure_gate(exc: BaseException) -> str | None:
    """Only fixed names may cross the provider-error/journal boundary."""
    if not isinstance(exc, TargetObservationError):
        return None
    gate = getattr(exc, "runpod_gate", None)
    return gate if isinstance(gate, str) and gate in {
        "volume", "GPU", "datacenter", "rate", "lifetime", "machine",
        "identity", "status", "image", "inventory", "capacity", "install",
    } else "provider"


class AcquisitionUnresolvedError(TargetObservationError):
    """An earlier create/attach may have succeeded and must be reconciled."""

    def __init__(self, message: str, *, gate: str = "identity", **kwargs) -> None:
        super().__init__(message, gate=gate, **kwargs)


class CapacityExhaustedError(TargetObservationError):
    """The bounded, safe capacity retry policy was exhausted."""

    code = "capacity_exhausted"

    def __init__(self, message: str, *, gate: str = "capacity", **kwargs) -> None:
        super().__init__(message, gate=gate, **kwargs)


@dataclass(frozen=True)
class RunPodTargetContract:
    pod_id: str
    gpu_type: str
    worker_image: str
    network_volume_id: str | None
    network_volume_name: str | None
    network_volume_size_gb: int
    expected_pod_name: str | None = None
    datacenter_id: str | None = None
    # Acquisition only: GPU/datacenter are decided by confirm_acquired_machine
    # (GraphQL pod.machine), not by the inventory record this contract inspects.
    defer_machine: bool = False


@dataclass(frozen=True)
class RunPodTargetObservation:
    pod_id: str
    pod_name: str
    desired_status: str
    actual_status: str | None
    gpu_type: str
    worker_image: str
    network_volume_id: str | None
    network_volume_name: str | None
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


def _record_custody(record: Mapping[str, Any]) -> Literal["none", "exact", "unknown"]:
    """Only persisted identities or a proven pre-create state establish custody."""
    if _text(record.get("pod_id")):
        return "exact"
    if (record.get("state") == "allocation_ready"
        and type(record.get("create_attempts", 0)) is int
        and record.get("create_attempts", 0) == 0) or (
        record.get("state") in {"capacity_retry_ready", "capacity_exhausted"}
        and record.get("safe_to_retry") is True
    ):
        return "none"
    return "unknown"


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
    with target_owner(path, blocking=True):
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        # A partial or unflushed intent must block admission, rather than
        # disappear and make an uncertain acquisition look like a first run.
        with os.fdopen(fd, "wb") as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        platform.fsync_directory(path.parent)


def replace_target_record(path: Path, value: dict[str, Any], *, operation_id: str) -> None:
    """Transition only the record owned by this acquisition operation."""
    with target_owner(path, blocking=True):
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


def apply_target_event(
    path: Path, operation_id: str, event: LeaseEvent, *,
    changes: Mapping[str, Any] | None = None,
    evidence: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Read, decide, and publish one lifecycle event under the short owner lock."""
    with target_owner(path, blocking=True):
        try:
            current = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise RuntimeError(f"cannot read RunPod custody at {path}: {exc}") from exc
        if not isinstance(current, dict) or current.get("operation_id") != operation_id:
            raise RuntimeError("RunPod custody belongs to another operation")
        updated = _target_event(current, event, changes=changes, evidence=evidence)
        write_target_record(path, updated)
        return updated


@contextmanager
def target_owner(handle_path: Path, *, blocking: bool = False):
    """Own the existing claim lock for a short transition or journal access.

    Product selection and worker preparation use the same lock pathname.
    Provider calls must happen after leaving this lock. Same-thread nesting on
    this exact path reuses the held descriptor; another thread always locks.
    """
    path = handle_path.expanduser()
    path.parent.mkdir(parents=True, exist_ok=True)
    key = str(path.resolve())
    held = getattr(_target_owner_local, "held", None)
    if held is None:
        held = {}
        _target_owner_local.held = held
    if key in held:
        fd, depth = held[key]
        held[key] = (fd, depth + 1)
        try:
            yield fd
        finally:
            current_fd, current_depth = held[key]
            if current_fd != fd or current_depth < 2:
                raise RuntimeError("RunPod target lock nesting is inconsistent")
            held[key] = (fd, current_depth - 1)
        return
    lock_path = path.with_name(f".{path.name}.owner.lock")
    try:
        fd = os.open(lock_path, os.O_CREAT | os.O_RDWR | getattr(os, "O_NOFOLLOW", 0), 0o600)
    except OSError:
        raise AcquisitionUnresolvedError("RunPod account lock cannot be opened safely", gate="identity", retryable=False, custody="unknown") from None
    try:
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            raise AcquisitionUnresolvedError("RunPod account lock must be a regular file", gate="identity", retryable=False, custody="unknown")
        try:
            platform.lock_file(fd, blocking=blocking)
        except BlockingIOError as exc:
            raise AcquisitionUnresolvedError(
                f"another process currently owns RunPod target journal {path}; retry after it exits",
                gate="identity", retryable=False, custody="unknown",
            ) from exc
        held[key] = (fd, 1)
        try:
            yield fd
        finally:
            held.pop(key, None)
    finally:
        try:
            platform.unlock_file(fd)
        finally:
            os.close(fd)


_target_owner_local = threading.local()


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
        cleanup = function.__name__ == "_release_exact_target_after_quiescence"
        with target_owner(path):
            try:
                current = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError) as exc:
                raise AcquisitionUnresolvedError(
                    f"cannot verify RunPod target journal {path} before provider use",
                    gate="identity", retryable=False, custody="unknown",
                ) from exc
            if (
                not isinstance(current, dict)
                or current != attempt
            ):
                raise AcquisitionUnresolvedError(
                    f"RunPod target journal {path} changed before this operation acquired ownership",
                    gate="identity", retryable=False, custody="unknown",
                )
        if current.get("provider_account_ref") and isinstance(getattr(config, "api_key", None), str):
            if cleanup:
                gate = account_gate
                await async_provider_call(
                    config.api_key, gate, config.api_key,
                    account_ref=current["provider_account_ref"], already_gated=True,
                )
            else:
                await async_provider_call(
                    config.api_key, account_gate, config.api_key,
                    account_ref=current["provider_account_ref"], already_gated=True,
                )
        try:
            if cleanup:
                with provider_budget(30., cleanup=True):
                    return await function(
                        config,
                        handle_path=handle_path,
                        attempt=current,
                        **kwargs,
                    )
            return await function(
                config,
                handle_path=handle_path,
                attempt=current,
                **kwargs,
            )
        except TargetObservationError as exc:
            # The operation may have journaled a returned pod ID before a
            # lower-level observer failed. Never infer custody from that
            # observer's contract alone.
            with target_owner(path, blocking=True):
                try:
                    latest = json.loads(path.read_text(encoding="utf-8"))
                except (OSError, json.JSONDecodeError):
                    exc.custody = "unknown"
                else:
                    exc.custody = (_record_custody(latest) if isinstance(latest, dict)
                                   and latest.get("operation_id") == current.get("operation_id") else "unknown")
            raise
    return wrapped


async def resolve_existing_volume(api_key: str, name_or_id: str) -> dict[str, Any]:
    """Resolve one existing account volume without creating or resizing it."""
    from runpod_lifecycle.api import get_network_volumes

    try:
        volumes = await async_provider_call(api_key, get_network_volumes, api_key)
    except Exception as exc:
        raise TargetObservationError(
            f"could not observe RunPod network-volume inventory for {name_or_id!r}: {exc}",
            gate="inventory", retryable=True, custody="none") from exc
    if not isinstance(volumes, list):
        raise TargetObservationError("RunPod network-volume inventory returned an invalid response", gate="inventory", retryable=True, custody="none")

    matches = [
        record for record in volumes
        if isinstance(record, dict)
        and name_or_id in {_text(record.get("id")), _text(record.get("name"))}
    ]
    if len(matches) != 1:
        raise TargetObservationError(
            f"existing network volume {name_or_id!r} cannot be uniquely confirmed in the authenticated account",
            gate="inventory", retryable=True, custody="none")
    size = _positive_integer(matches[0].get("size"))
    if size is None:
        raise TargetObservationError(
            f"existing network volume {name_or_id!r} has no valid provider size",
            gate="inventory", retryable=True, custody="none")
    return dict(matches[0])


def _text(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    value = value.strip()
    return value or None


def _required_text(record: Mapping[str, Any], key: str, *, context: str, gate: str = "provider") -> str:
    value = _text(record.get(key))
    if value is None:
        raise TargetObservationError(f"provider {context} is missing required {key}", gate=gate, retryable=False, custody="unknown")
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
    release: bool = False,
) -> RunPodTargetObservation:
    """Validate exact inventory identity; attachment checks also require readiness.

    ``contract.defer_machine`` is for acquisition: the GPU and datacenter are decided by
    :func:`confirm_acquired_machine` from GraphQL ``pod.machine`` (the provider
    gpuTypes catalog for the GPU), never from this inventory/REST record.

    ``release`` is for terminating a pod this journal created. It is keyed on
    the pod id the create response returned (journaled before any acquisition
    check) plus the journal's unique request name, image and volume. GPU and
    datacenter labels are recorded as evidence but never gate release: RunPod
    reports them inconsistently (REST ``machine: {}`` on a fresh pod; GraphQL
    ``gpuDisplayName`` "RTX 5090" for type id "NVIDIA GeForce RTX 5090"), and
    refusing release over a label strands a paid pod this product created.
    """
    if not contract.pod_id.strip():
        raise ValueError("pod_id must be nonempty")
    if contract.network_volume_id is not None and not contract.network_volume_id.strip():
        raise ValueError("network volume identity must be pinned by ID")
    if contract.network_volume_id is not None and contract.network_volume_size_gb <= 0:
        raise ValueError("network volume size must be positive")

    pod_matches = [
        record for record in pods
        if isinstance(record, dict) and _text(record.get("id")) == contract.pod_id
    ]
    if len(pod_matches) != 1:
        raise TargetObservationError(
            f"exact pod {contract.pod_id!r} is not uniquely visible in the authenticated account inventory",
            gate="identity", retryable=False, custody="unknown")
    pod = pod_matches[0]

    pod_name = _required_text(pod, "name", context=f"pod {contract.pod_id}", gate="identity")
    if contract.expected_pod_name and pod_name != contract.expected_pod_name:
        raise TargetObservationError(
            f"pod {contract.pod_id!r} name mismatch: expected {contract.expected_pod_name!r}, observed {pod_name!r}",
            gate="identity", retryable=False, custody="unknown")

    desired_status = _required_text(
        pod, "desiredStatus", context=f"pod {contract.pod_id}", gate="status"
    ).upper()
    if not allow_inactive and desired_status not in {"RUNNING", "PROVISIONING"}:
        raise TargetObservationError(
            f"pod {contract.pod_id!r} is not attachable (desiredStatus={desired_status!r})",
            gate="status", retryable=False, custody="unknown")
    actual_status = _text(pod.get("actualStatus"))
    if (
        not allow_inactive
        and actual_status
        and actual_status.upper() not in {"RUNNING", "PROVISIONING"}
    ):
        raise TargetObservationError(
            f"pod {contract.pod_id!r} is not currently attachable (actualStatus={actual_status!r})",
            gate="status", retryable=False, custody="unknown")

    machine = pod.get("machine") if isinstance(pod.get("machine"), dict) else {}
    try:
        gpu_name = pod_field(pod, "gpu_name")
    except InvalidPodField:
        raise TargetObservationError("pod GPU evidence is invalid", gate="GPU", retryable=False, custody="unknown") from None
    gpu_candidates = [
        gpu_name,
        _text(machine.get("gpuDisplayName")),
        _text(machine.get("gpuTypeId")),
    ]
    observed_gpu = next(
        (candidate for candidate in gpu_candidates if candidate and _equivalent(candidate, contract.gpu_type)),
        None,
    )
    defer_machine = contract.defer_machine
    if observed_gpu is None and (release or defer_machine):
        # Evidence only: the created pod is pinned by id+name+image+volume.
        observed_gpu = next((item for item in gpu_candidates if item), contract.gpu_type)
    if observed_gpu is None:
        raise TargetObservationError(
            f"pod {contract.pod_id!r} GPU mismatch: expected {contract.gpu_type!r}, "
            f"observed {[item for item in gpu_candidates if item]!r}",
            gate="GPU", retryable=False, custody="unknown")

    try:
        image = pod_field(pod, "image")
    except InvalidPodField:
        raise TargetObservationError("pod image evidence is invalid", gate="image", retryable=False, custody="unknown") from None
    if image != contract.worker_image:
        raise TargetObservationError(
            f"pod {contract.pod_id!r} image mismatch: expected {contract.worker_image!r}, observed {image!r}",
            gate="image", retryable=False, custody="unknown")

    try:
        attached_volume_id = pod_field(pod, "volume")
    except InvalidPodField:
        raise TargetObservationError("pod volume evidence is invalid", gate="volume", retryable=False, custody="unknown") from None
    if attached_volume_id != contract.network_volume_id:
        raise TargetObservationError(
            f"pod {contract.pod_id!r} volume mismatch: expected {contract.network_volume_id!r}, "
            f"observed {attached_volume_id!r}",
            gate="volume", retryable=False, custody="unknown")

    volume = inspect_volume_records(contract, volumes=volumes)
    volume_name = _text(volume.get("name"))
    size = volume["size"]
    datacenter_id = _text(volume.get("dataCenterId"))
    if contract.network_volume_id is None:
        datacenter_id = _text(pod.get("dataCenterId")) or _text(machine.get("dataCenterId"))
        if (release or defer_machine) and datacenter_id is None:
            datacenter_id = contract.datacenter_id  # unreported; not a reason to strand a paid pod
        if (not release and not defer_machine and contract.datacenter_id is not None
                and datacenter_id != contract.datacenter_id):
            raise TargetObservationError("no-volume pod datacenter could not be confirmed", gate="datacenter", retryable=False, custody="unknown")

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
    if contract.network_volume_id is None:
        if contract.network_volume_name is not None or contract.network_volume_size_gb != 0:
            raise TargetObservationError("no-volume target has inconsistent volume identity", gate="volume", retryable=False, custody="unknown")
        return {"id": None, "name": None, "size": 0, "dataCenterId": contract.datacenter_id}
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
            f"network volume {contract.network_volume_id!r} cannot be confirmed in the authenticated account inventory",
            gate="volume", retryable=False, custody="unknown")
    volume = volume_matches[0]
    volume_name = _required_text(
        volume, "name", context=f"volume {contract.network_volume_id}", gate="volume"
    )
    if contract.network_volume_name and volume_name != contract.network_volume_name:
        raise TargetObservationError(
            f"network volume {contract.network_volume_id!r} name mismatch: "
            f"expected {contract.network_volume_name!r}, observed {volume_name!r}",
            gate="volume", retryable=False, custody="unknown")
    size = _positive_integer(volume.get("size"))
    if size != contract.network_volume_size_gb:
        raise TargetObservationError(
            f"network volume {contract.network_volume_id!r} size mismatch: "
            f"expected {contract.network_volume_size_gb} GB, observed {size!r}",
            gate="volume", retryable=False, custody="unknown")
    datacenter_id = _text(volume.get("dataCenterId"))
    if contract.datacenter_id and datacenter_id != contract.datacenter_id:
        raise TargetObservationError(
            f"network volume {contract.network_volume_id!r} datacenter mismatch: "
            f"expected {contract.datacenter_id!r}, observed {datacenter_id!r}",
            gate="volume", retryable=False, custody="unknown")
    return dict(volume)


async def observe_exact_target(
    api_key: str,
    contract: RunPodTargetContract,
) -> RunPodTargetObservation:
    """Read exact pod and volume identity from this credential's account inventory."""
    return await asyncio.to_thread(observe_target, api_key, contract)


_MACHINE_POLL_SECONDS = 3.0


def confirm_acquired_machine(
    api_key: str,
    contract: RunPodTargetContract,
    *,
    timeout_seconds: float,
) -> tuple[str, str | None]:
    """Confirm a freshly acquired pod's GPU and datacenter from GraphQL ``pod.machine``.

    REST reports ``machine: {}`` for a correct fresh pod, so REST never decides.
    Datacenter: ``machine.dataCenterId`` must equal the contract's exactly. GPU:
    the contract's gpu_type id is resolved to its ``displayName`` through the
    provider's gpuTypes catalog (same GraphQL endpoint) and must equal
    ``machine.gpuDisplayName`` exactly. No normalization, aliases or fuzzy match.
    GraphQL still reporting no machine at the timeout fails closed.
    Returns ``(gpuDisplayName, dataCenterId)``.
    """
    from runpod_lifecycle import graphql as lifecycle_graphql

    deadline = None

    def read_machine():
        nonlocal deadline
        if deadline is None:
            # Admission has its own inherited acquisition deadline. Begin the
            # readiness observation window when the first query can start,
            # so inventory pacing does not consume the whole polling window.
            deadline = time.monotonic() + max(0.0, float(timeout_seconds))
        return lifecycle_graphql.get_pod(contract.pod_id, api_key)

    machine: Mapping[str, Any] = {}
    while True:
        try:
            record = provider_call(api_key, read_machine)
            candidate = record.get("machine") if isinstance(record, dict) else None
            machine = candidate if isinstance(candidate, dict) else {}
        except ProviderRefused as exc:
            raise TargetObservationError(
                "pod machine observation could not enter the provider account gate",
                gate="machine", retryable=True, custody="unknown",
            ) from exc
        except Exception:
            machine = {}
        if deadline is None:
            deadline = time.monotonic() + max(0.0, float(timeout_seconds))
        if machine.get("gpuDisplayName") and machine.get("dataCenterId"):
            break
        if time.monotonic() >= deadline:
            raise TargetObservationError(
                f"pod {contract.pod_id!r} machine was not reported by GraphQL before the readiness timeout",
                gate="machine",
             retryable=True, custody="unknown")
        time.sleep(min(_MACHINE_POLL_SECONDS, max(0., deadline - time.monotonic())))

    observed_gpu = machine["gpuDisplayName"]
    observed_dc = machine["dataCenterId"]
    if contract.datacenter_id is not None and observed_dc != contract.datacenter_id:
        raise TargetObservationError(
            f"pod {contract.pod_id!r} datacenter mismatch: expected {contract.datacenter_id!r}, "
            f"observed {observed_dc!r}",
            gate="datacenter",
         retryable=False, custody="unknown")
    try:
        gpu = _gpu_type_from_graphql(contract.gpu_type, api_key)
    except Exception:
        raise TargetObservationError(
            f"provider gpuTypes catalog could not resolve GPU type {contract.gpu_type!r}",
            gate="GPU",
         retryable=False, custody="unknown") from None
    expected_display = gpu.get("displayName") if isinstance(gpu, dict) and gpu.get("id") == contract.gpu_type else None
    if not expected_display or expected_display != observed_gpu:
        raise TargetObservationError(
            f"pod {contract.pod_id!r} GPU mismatch: expected {expected_display!r} "
            f"(catalog displayName of {contract.gpu_type!r}), observed {observed_gpu!r}",
            gate="GPU",
         retryable=False, custody="unknown")
    return observed_gpu, observed_dc


def read_acquired_gpu_count(api_key: str, pod_id: str) -> int:
    """The pod's GPU count from GraphQL ``pod.gpuCount``; unknown fails closed (rate gate)."""
    from runpod_lifecycle import graphql as lifecycle_graphql

    try:
        record = provider_call(api_key, lifecycle_graphql.get_pod, pod_id, api_key)
    except Exception:
        record = None
    count = record.get("gpuCount") if isinstance(record, dict) else None
    if type(count) is not int or count < 1:
        raise TargetObservationError(
            f"pod {pod_id!r} gpuCount was not reported by GraphQL; the pod total rate cannot be compared",
            gate="rate",
         retryable=True, custody="unknown")
    return count


async def observe_and_confirm_acquired_target(
    api_key: str,
    contract: RunPodTargetContract,
    *,
    ready_timeout_seconds: int,
) -> RunPodTargetObservation:
    """Acquisition confirmation: inventory identity, then GPU/datacenter from GraphQL machine."""
    observation = await observe_exact_target(api_key, _dc_replace(contract, defer_machine=True))
    gpu_display, datacenter = await asyncio.to_thread(
        confirm_acquired_machine, api_key, contract, timeout_seconds=ready_timeout_seconds
    )
    return _dc_replace(observation, gpu_type=gpu_display, datacenter_id=datacenter)


def read_exact_pod(api_key: str, pod_id: str) -> tuple[int, dict[str, Any] | None]:
    """Preserve exact REST GET status; null/errors never become absence proof.

    Return only to the owner: provider bodies may contain transport details and
    must not be copied into custody journals or public error messages.
    """
    from .provider import RunPodMachineProvider

    try:
        record = RunPodMachineProvider(api_key).read_raw(pod_id)
        return (404, None) if record is None else (200, record)
    except Exception:
        raise TargetObservationError("exact pod GET could not be confirmed", gate="identity", retryable=True, custody="unknown") from None



def _target_contract(
    config: Any,
    *,
    pod_id: str,
    expected_name: str | None,
    volume: Mapping[str, Any],
    defer_machine: bool = False,
) -> RunPodTargetContract:
    return RunPodTargetContract(
        pod_id=pod_id,
        expected_pod_name=expected_name,
        gpu_type=getattr(config, "gpu_type_id", None) or config.gpu_type_candidates[0],
        worker_image=config.worker_image,
        network_volume_id=_text(volume.get("id")),
        network_volume_name=_text(volume.get("name")),
        network_volume_size_gb=int(volume["size"]),
        datacenter_id=_text(volume.get("dataCenterId")),
        defer_machine=defer_machine,
    )


async def _get_provider_pod(pod_id: str, config: Any, *, name: str) -> Any:
    """Attach a handle after one observation; never hide 429s in fallbacks."""
    from runpod_lifecycle import LaunchFailure, Pod, graphql

    record = await async_provider_call(config.api_key, graphql.get_pod, pod_id, config.api_key)
    if not isinstance(record, dict) or record.get("id") != pod_id:
        raise LaunchFailure("exact pod observation did not confirm the requested identity")
    return Pod(pod_id=pod_id, name=name, config=config)


async def _wait_provider_ready(config: Any, pod: Any, timeout: int) -> Any:
    """Keep lifecycle's worker readiness checks, gating each control-plane poll."""
    status = getattr(pod, "status", None)
    if status is None:
        # Test/transport facades without provider status polling.
        return await pod.wait_ready(timeout=timeout)

    async def gated_status():
        from runpod_lifecycle import api, graphql

        # Lifecycle's status() hides errors and tries several transports. Use
        # one exact observation so a 429 cannot trigger ungated fallbacks or
        # be reported to the gate as a successful None response.
        record = await async_provider_call(config.api_key, graphql.get_pod, pod.id, config.api_key)
        return api._normalize_pod_status(pod.id, record) if isinstance(record, dict) else None

    pod.status = gated_status
    try:
        with provider_budget(timeout):
            return await pod.wait_ready(timeout=timeout)
    finally:
        pod.status = status


async def _protect_allocated_target(config: Any, record: dict[str, Any],
                                    path: Path, timeout: int) -> dict[str, Any]:
    """Publish at first reachable transport, before readiness or verification."""
    from .pod_watchdog import PodWatchdogTransportError, write_deadline
    from .remote_placement import _HttpsOps

    if not record.get("paid_limits") or record.get("transport") != "https":
        return record
    deadline = datetime.fromisoformat(record["paid_limits"]["expires_at"].replace("Z", "+00:00")).timestamp()
    generation = record.get("self_stop", {}).get("generation", 0) + 1
    value = {**record, "self_stop": {"deadline": deadline, "generation": generation, "acknowledged": False}}
    replace_target_record(path, value, operation_id=record["operation_id"])
    until = time.monotonic() + timeout
    ops = _HttpsOps(record["pod_id"], config.https_token, deadline=until)
    while True:
        try:
            protected = await asyncio.to_thread(write_deadline, ops, deadline, generation)
            break
        except PodWatchdogTransportError:
            if time.monotonic() >= until:
                raise
            await asyncio.sleep(min(0.25, max(0, until - time.monotonic())))
    value["self_stop"]["acknowledged"] = protected
    replace_target_record(path, value, operation_id=record["operation_id"])
    return value


async def _wait_and_observe_target(
    config: Any,
    pod: Any,
    *,
    expected_name: str | None,
    volume: Mapping[str, Any],
    ready_timeout_seconds: int,
) -> RunPodTargetObservation:
    await _wait_provider_ready(config, pod, ready_timeout_seconds)
    return await observe_and_confirm_acquired_target(
        config.api_key,
        _target_contract(
            config,
            pod_id=str(pod.id),
            expected_name=expected_name,
            volume=volume,
        ),
        ready_timeout_seconds=ready_timeout_seconds,
    )


def _write_target_failure(
    handle_path: Path,
    attempt: dict[str, Any],
    *,
    operation_id: str,
    phase: str,
    exc: BaseException,
) -> None:
    if isinstance(exc, TargetObservationError):
        exc.custody = _record_custody(attempt)
    failed = _target_event(attempt, LeaseEvent.ACQUISITION_FAILED, changes={
        "failure_phase": phase,
        "failure_type": type(exc).__name__,
        "failure_gate": observation_failure_gate(exc),
        "failed_at": utc_now_iso(),
        "reconciliation_required": True,
    })
    replace_target_record(handle_path, failed, operation_id=operation_id)


def observe_target(
    api_key: str,
    contract: RunPodTargetContract,
) -> RunPodTargetObservation:
    """Synchronous provider observation for CLI and executor boundaries."""
    from runpod_lifecycle.api import get_network_volumes, list_pods

    try:
        pods = provider_call(api_key, list_pods, api_key)
    except Exception as exc:
        raise TargetObservationError(
            f"could not observe RunPod account pod inventory for exact pod {contract.pod_id!r}: {exc}",
            gate="inventory", retryable=True, custody="unknown") from exc
    if not isinstance(pods, list):
        raise TargetObservationError("RunPod account pod inventory returned an invalid response", gate="inventory", retryable=True, custody="unknown")

    try:
        volumes = provider_call(api_key, get_network_volumes, api_key) if contract.network_volume_id is not None else []
    except Exception as exc:
        raise TargetObservationError(
            f"could not confirm network volume {contract.network_volume_id!r}: {exc}",
            gate="inventory", retryable=True, custody="unknown") from exc
    if not isinstance(volumes, list):
        raise TargetObservationError("RunPod network-volume inventory returned an invalid response", gate="inventory", retryable=True, custody="unknown")

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
        pods = await bounded_observation(list_pods, config.api_key)
    except Exception as exc:
        raise TargetObservationError(
            "could not observe RunPod account pod inventory for exact release",
            gate="inventory", retryable=True, custody="unknown") from exc
    if not isinstance(pods, list):
        raise TargetObservationError("RunPod account pod inventory returned an invalid response", gate="inventory", retryable=True, custody="unknown")
    ids = [_text(pod.get("id")) if isinstance(pod, dict) else None for pod in pods]
    if any(pod_id is None for pod_id in ids) or len(set(ids)) != len(ids):
        raise TargetObservationError("RunPod account pod inventory is malformed or ambiguous", gate="inventory", retryable=True, custody="unknown")

    try:
        volumes = (await async_provider_call(config.api_key, get_network_volumes, config.api_key)
                   if contract.network_volume_id is not None else [])
    except Exception:
        raise TargetObservationError(
            "could not confirm the persistent release volume",
            gate="inventory", retryable=True, custody="unknown") from None
    if not isinstance(volumes, list):
        raise TargetObservationError("RunPod network-volume inventory returned an invalid response", gate="inventory", retryable=True, custody="unknown")
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
        release=True,
    )
    return observation, volume


def _release_contract_from_record(attempt: Mapping[str, Any]) -> RunPodTargetContract:
    if (attempt.get("acquisition_mode") != "allocate"
            or not _text(attempt.get("request_name"))
            or attempt.get("name", attempt["request_name"]) != attempt["request_name"]
            or ("ordinary_preparation" in attempt
                and attempt["request_name"] != "astrid-product-" + str(attempt.get("operation_id")))):
        raise TargetObservationError("release requires this journal's created pod identity", gate="identity", retryable=False, custody=_record_custody(attempt))
    pod_id = _text(attempt.get("pod_id"))
    gpu_type = _text(attempt.get("gpu_type"))
    worker_image = _text(attempt.get("worker_image"))
    volume_id = _text(attempt.get("network_volume_id"))
    volume_name = _text(attempt.get("network_volume_name")) or _text(
        attempt.get("storage_name")
    )
    volume_size = _positive_integer(attempt.get("network_volume_size_gb"))
    ephemeral = (attempt.get("storage_mode") == "ephemeral"
                 and attempt.get("network_volume_id") is None
                 and attempt.get("network_volume_name") is None
                 and type(attempt.get("network_volume_size_gb")) is int
                 and attempt.get("network_volume_size_gb") == 0)
    if not all((pod_id, gpu_type, worker_image)) or (not ephemeral and not all((volume_id, volume_name, volume_size))):
        raise TargetObservationError(
            "release record is missing exact pod, GPU, image or persistent-volume identity",
            gate="identity", retryable=False, custody=_record_custody(attempt),
        )
    return RunPodTargetContract(
        pod_id=pod_id,
        expected_pod_name=_text(attempt.get("name")) or _text(attempt.get("request_name")),
        gpu_type=gpu_type,
        worker_image=worker_image,
        network_volume_id=volume_id,
        network_volume_name=volume_name,
        network_volume_size_gb=0 if ephemeral else volume_size,
        datacenter_id=_text(attempt.get("network_volume_datacenter_id")),
    )


def unactivated_target_quiescence(attempt: Mapping[str, Any]) -> dict[str, Any]:
    """Evidence for a failed allocation before any claim handle was published.

    Claim publication changes the journal schema and drops worker_activation.
    Attached pods and published/activated workers must use the task drain gate.
    """
    return _validate_release_quiescence({
        "schema": "astrid.runpod.unactivated-quiescence.v1",
        **{key: attempt.get(key) for key in ("operation_id", "pod_id", "provider_account_ref")},
        "worker_activation": "not_started",
    }, attempt)


def validate_lease_preparation(attempt: Mapping[str, Any], *, releasing: bool = False) -> None:
    """Preparation and the machine job are child custody of A3, never a second release owner."""
    lease = attempt.get("lease", {})
    machine_job = lease.get("job") if isinstance(lease, Mapping) else None
    if machine_job is not None and (
        not isinstance(machine_job, Mapping) or set(machine_job) != {"id", "state"}
        or not isinstance(machine_job.get("id"), str) or not re.fullmatch(r"[0-9a-f]{32}", machine_job["id"])
        or machine_job.get("state") not in {"starting", "running", "exited", "cancelled"}
    ):
        raise TargetObservationError("invalid exact machine job custody", gate="install", retryable=False, custody=_record_custody(attempt))
    if releasing and machine_job is not None and machine_job["state"] not in {"exited", "cancelled"}:
        raise TargetObservationError("machine job remains unresolved; retain the exact lease", gate="install", retryable=True, custody=_record_custody(attempt))
    preparation = lease.get("preparation")
    if preparation is None:
        return
    if (not isinstance(preparation, Mapping)
            or preparation.get("schema") != "astrid.runpod.lease-preparation.v1"
            or preparation.get("state") not in {"preparing", "prepared", "unresolved", "stale_after_failure"}
            or preparation.get("operation_id") != attempt.get("operation_id")
            or preparation.get("pod_id") != attempt.get("pod_id")
            or preparation.get("compatibility") != attempt.get("lease_compatibility")
            or not isinstance(preparation.get("bootstrap_digest"), str)
            or not re.fullmatch(r"[0-9a-f]{16}", preparation["bootstrap_digest"])):
        raise TargetObservationError("invalid retained lease preparation custody", gate="install", retryable=False, custody=_record_custody(attempt))
    job = preparation.get("job")
    if job is not None and (
        not isinstance(job, Mapping) or set(job) != {"id", "state"}
        or not isinstance(job.get("id"), str) or not re.fullmatch(r"[0-9a-f]{32}", job["id"])
        or job.get("state") not in {"starting", "running", "exited", "cancelled"}
    ):
        raise TargetObservationError("invalid exact preparation job custody", gate="install", retryable=False, custody=_record_custody(attempt))
    if preparation["state"] == "stale_after_failure" and any(
            child is not None and child["state"] not in {"exited", "cancelled"}
            for child in (job, machine_job)):
        raise TargetObservationError("stale preparation has an unresolved job", gate="install", retryable=True, custody=_record_custody(attempt))
    if preparation["state"] == "prepared":
        snapshot = preparation.get("installed")
        if (not isinstance(snapshot, Mapping)
                or snapshot.get("schema") != "astrid.runpod.installed-snapshot.v1"
                or snapshot.get("compatibility") != attempt.get("lease_compatibility")
                or not isinstance(snapshot.get("receipt_sha256"), str)
                or not re.fullmatch(r"sha256:[0-9a-f]{64}", snapshot["receipt_sha256"])
                or (job is not None and job["state"] != "exited")):
            raise TargetObservationError("retained lease has no verified installed snapshot", gate="install", retryable=False, custody=_record_custody(attempt))
    if releasing and (preparation["state"] == "preparing"
                      or (job is not None and job["state"] not in {"exited", "cancelled"})):
        raise TargetObservationError("preparation job remains unresolved; retain the exact lease", gate="install", retryable=True, custody=_record_custody(attempt))


def _validate_release_quiescence(value: Any, attempt: Mapping[str, Any]) -> dict[str, Any]:
    """Accept only durable exact-task drain and process-stop evidence."""
    if attempt.get("state") == "cleanup_complete":
        _validate_absence_receipt(attempt)
    if isinstance(value, Mapping) and value.get("schema") == "astrid.runpod.lease-drain.v1":
        validate_lease_preparation(attempt, releasing=True)
        lease = attempt.get("lease")
        if (not isinstance(lease, Mapping) or lease.get("schema") != "astrid.runpod.lease.v1"
                or lease.get("state") != "releasing"
                or attempt.get("worker_activation") != "not_started"
                or attempt.get("state") not in {"release_pending", "cleanup_pending", "cleanup_complete"}
                or any(attempt.get(key) is not None for key in
                       ("managed_worker", "managed_session", "ordinary_preparation", "warm_session", "task_id", "activation_id"))
                or value.get("state") != "drained" or value.get("admission_closed") is not True
                or set(value) != {"schema", "state", "admission_closed", "operation_id", "pod_id",
                                  "provider_account_ref", "owner_epoch", "generation", "invocation_id"}
                or type(value.get("generation")) is not int
                or any(not value.get(key) or value[key] != attempt.get(key)
                       for key in ("operation_id", "pod_id", "provider_account_ref"))
                or any(value.get(key) != lease.get(key) for key in
                       ("owner_epoch", "generation", "invocation_id"))):
            raise TargetObservationError("release lacks this lease's exact host drain and bridge fence", gate="identity", retryable=False, custody=_record_custody(attempt))
        return dict(value)
    if isinstance(value, Mapping) and value.get("schema") == "astrid.runpod.unactivated-quiescence.v1":
        if (attempt.get("schema_version") != "astrid.runpod.allocation-attempt.v1"
                or attempt.get("acquisition_mode") != "allocate"
                or not _text(attempt.get("request_name"))
                or (attempt.get("name") is not None and attempt["name"] != attempt["request_name"])
                or attempt.get("worker_activation") != "not_started"
                or value.get("worker_activation") != "not_started"
                or attempt.get("state") not in {
                    "allocated_unverified", "release_pending", "cleanup_pending", "cleanup_complete",
                }
                or any(attempt.get(key) is not None for key in (
                    "managed_worker", "managed_session", "task_id", "activation_id",
                ))
                or any(not value.get(key) or value[key] != attempt.get(key) for key in (
                    "operation_id", "pod_id", "provider_account_ref",
                ))):
            raise TargetObservationError("release lacks exclusive unactivated allocation custody", gate="identity", retryable=False, custody=_record_custody(attempt))
        return dict(value)
    if not isinstance(value, Mapping) or value.get("schema") != "astrid.runpod.worker-quiescence.v1":
        raise TargetObservationError("release requires exact persisted worker-quiescence evidence", gate="identity", retryable=False, custody=_record_custody(attempt))
    if (value.get("operation_id") != attempt.get("operation_id")
            or value.get("pod_id") != attempt.get("pod_id")
            or value.get("provider_account_ref") != attempt.get("provider_account_ref")):
        raise TargetObservationError("worker-quiescence evidence belongs to another RunPod claim", gate="identity", retryable=False, custody=_record_custody(attempt))
    for field in ("task_id", "activation_id", "runtime_instance_id", "runtime_session_id"):
        if not isinstance(value.get(field), str) or not value[field]:
            raise TargetObservationError(f"worker-quiescence evidence is missing {field}", gate="identity", retryable=False, custody=_record_custody(attempt))
    if type(value.get("runtime_epoch")) is not int or value["runtime_epoch"] <= 0:
        raise TargetObservationError("worker-quiescence evidence has an invalid Runtime epoch", gate="identity", retryable=False, custody=_record_custody(attempt))
    drain = value.get("drain")
    if (not isinstance(drain, Mapping) or drain.get("state") != "complete"
            or drain.get("task_id") != value["task_id"]
            or drain.get("activation_id") != value["activation_id"]):
        raise TargetObservationError("Runtime did not confirm this exact task activation drain", gate="identity", retryable=False, custody=_record_custody(attempt))
    stopped = value.get("process_stop")
    if (not isinstance(stopped, Mapping) or stopped.get("stopped") is not True
            or type(stopped.get("pid")) is not int or stopped["pid"] <= 0
            or not all(isinstance(stopped.get(field), str) and stopped[field]
                       for field in ("birth_id", "incarnation"))):
        raise TargetObservationError("exact managed-worker process stop is not positively recorded", gate="identity", retryable=False, custody=_record_custody(attempt))
    managed = attempt.get("managed_worker")
    if managed is not None:
        if not isinstance(managed, Mapping):
            raise TargetObservationError("managed-worker custody is invalid", gate="identity", retryable=False, custody=_record_custody(attempt))
        qualification = managed.get("activation_qualification")
        process = managed.get("process_handle")
        if not isinstance(qualification, Mapping) or not isinstance(process, Mapping):
            raise TargetObservationError("managed-worker release identities are missing", gate="identity", retryable=False, custody=_record_custody(attempt))
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
            raise TargetObservationError("quiescence differs from the persisted worker generation", gate="identity", retryable=False, custody=_record_custody(attempt))
        if value.get("runtime_drain") != {"state": "drained", "activation_id": value["activation_id"]}:
            raise TargetObservationError("release lacks the exact Runtime drain response", gate="identity", retryable=False, custody=_record_custody(attempt))
        if value.get("credential_remove") != {"removed": True}:
            raise TargetObservationError("release lacks exact remote Runtime credential cleanup evidence", gate="identity", retryable=False, custody=_record_custody(attempt))
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
            raise TargetObservationError("release lacks the exact physical target fence", gate="identity", retryable=False, custody=_record_custody(attempt))
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
            raise TargetObservationError("release lacks exact managed VibeComfy session stop evidence", gate="identity", retryable=False, custody=_record_custody(attempt))
        if managed_session.get("session_ref") == "comfyui":
            receipt = managed_session.get("receipt")
            if (not isinstance(receipt, Mapping)
                    or stopped_session.get("supervisor_stop") != "original_installation_owner"
                    or any(stopped_session.get(key) != receipt.get(key) or not receipt.get(key)
                           for key in ("daemon_pid", "daemon_birth_id", "comfy_pid", "comfy_birth_id"))):
                raise TargetObservationError("release lacks original installation supervisor stop evidence", gate="identity", retryable=False, custody=_record_custody(attempt))
    return json.loads(json.dumps(dict(value), sort_keys=True, separators=(",", ":")))


def _validate_absence_receipt(attempt: Mapping[str, Any]) -> None:
    cleanup = attempt.get("cleanup")
    absence = cleanup.get("absence") if isinstance(cleanup, Mapping) else None
    expected = {
        "provider_account_ref": attempt.get("provider_account_ref"),
        "pod_id": attempt.get("pod_id"), "exact_get_status": 404,
        "account_list_succeeded": True, "account_list_absent": True,
    }
    if (not isinstance(absence, Mapping) or dict(absence) != expected
            or type(absence.get("exact_get_status")) is not int
            or absence.get("account_list_succeeded") is not True
            or absence.get("account_list_absent") is not True
            or not expected["provider_account_ref"] or not expected["pod_id"]):
        raise TargetObservationError("completed release lacks independent exact GET404 and account-list absence", gate="identity", retryable=False, custody=_record_custody(attempt))


def _validate_completed_release(attempt: Mapping[str, Any]) -> None:
    _release_contract_from_record(attempt)
    _validate_release_quiescence(attempt.get("release_quiescence"), attempt)
    cleanup = attempt.get("cleanup")
    if (attempt.get("state") != "cleanup_complete" or not isinstance(cleanup, Mapping)
            or cleanup.get("pod_id") != attempt.get("pod_id")
            or cleanup.get("backup_volume_id") != attempt.get("network_volume_id")
            or cleanup.get("backup_volume_preserved") is not True
            or cleanup.get("status") not in {"already_gone", "terminated"}
            or attempt.get("cleanup_status") != "complete"
            or attempt.get("reconciliation_required") is not False):
        raise TargetObservationError("completed release lacks its exact provider cleanup receipt", gate="identity", retryable=False, custody=_record_custody(attempt))


async def _confirm_release_absence(config: Any, contract: RunPodTargetContract,
                                   attempt: Mapping[str, Any]) -> tuple[dict[str, Any], dict[str, Any]]:
    # Lifecycle's DELETE-404 fast path and its list-only reconciliation cannot
    # establish this product receipt. Always make both independent reads here.
    status, body = await async_provider_call(
        config.api_key, read_exact_pod, config.api_key, contract.pod_id, already_gated=True,
    )
    after, volume = await _release_inventory(config, contract)
    if type(status) is not int or status != 404 or body is not None or after is not None:
        raise TargetObservationError("exact GET404 and successful account-list absence are both required", gate="identity", retryable=False, custody=_record_custody(attempt))
    return {
        "provider_account_ref": attempt["provider_account_ref"],
        "pod_id": contract.pod_id, "exact_get_status": 404,
        "account_list_succeeded": True, "account_list_absent": True,
    }, volume


def _read_release_checkpoint(path: Path, expected: Mapping[str, Any]) -> dict[str, Any]:
    """Read callback progress while the enclosing P1 owner lock remains held."""
    try:
        current = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise TargetObservationError("release checkpoint cannot be read safely", gate="identity", retryable=False, custody=_record_custody(expected)) from exc
    if (not isinstance(current, dict) or current.get("state") != "release_pending"
            or {k: v for k, v in current.items() if k != "release_progress"}
            != {k: v for k, v in expected.items() if k != "release_progress"}):
        raise TargetObservationError("release custody changed during its quiescence callback", gate="identity", retryable=False, custody=_record_custody(expected))
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
        raise AcquisitionUnresolvedError("release record has no operation ID", gate="identity", retryable=False, custody=_record_custody(attempt))
    if attempt.get("release_authorized") is not True:
        raise TargetObservationError(
            "this attached RunPod target has no explicit exclusive release authority",
            gate="identity", retryable=False, custody=_record_custody(attempt),
        )
    if not callable(quiesce_worker):
        raise TypeError("quiesce_worker must be the prepared-worker drain owner")
    if state == "cleanup_complete" and resume:
        _validate_completed_release(attempt)
        return attempt
    if state == "claimed" and resume:
        raise AcquisitionUnresolvedError("a claimed target has no interrupted release to resume", gate="identity", retryable=False, custody=_record_custody(attempt))
    if state in {"release_pending", "cleanup_pending"} and not resume:
        raise AcquisitionUnresolvedError(
            "this target already has an interrupted release; resume that exact release",
            gate="identity", retryable=False, custody=_record_custody(attempt),
        )
    if state not in {"claimed", "allocated_unverified", "release_pending", "cleanup_pending"}:
        raise AcquisitionUnresolvedError(
            f"journal state {state!r} is not a releasable prepared target",
            gate="identity", retryable=False, custody=_record_custody(attempt),
        )

    contract = _release_contract_from_record(attempt)
    if state in {"claimed", "allocated_unverified"}:
        pending = _target_event(attempt, LeaseEvent.RELEASE, changes={
            "release_requested_at": utc_now_iso(),
            "reconciliation_required": True,
        })
        replace_target_record(handle_path, pending, operation_id=operation_id)
    else:
        pending = _target_event(attempt, LeaseEvent.RELEASE, changes={
            "release_requested_at": utc_now_iso(), "reconciliation_required": True})
        replace_target_record(handle_path, pending, operation_id=operation_id)
    attempt = pending

    saved_quiescence = attempt.get("release_quiescence")
    if saved_quiescence is not None:
        if not resume:
            raise AcquisitionUnresolvedError(
                "release already has durable quiescence evidence; resume that exact release",
                gate="identity", retryable=False, custody=_record_custody(attempt),
            )
        quiescence = _validate_release_quiescence(saved_quiescence, attempt)
        if (attempt.get("lt_control") or {}).get("canonical_state") != "RELEASING":
            attempt = _target_event(attempt, LeaseEvent.QUIESCED, evidence={
                "drain_confirmed": True,
            })
            replace_target_record(handle_path, attempt, operation_id=operation_id)
    else:
        try:
            result = await quiesce_worker(dict(attempt))
            attempt = _read_release_checkpoint(handle_path, attempt)
            quiescence = _validate_release_quiescence(result, attempt)
        except BaseException as exc:
            attempt = _read_release_checkpoint(handle_path, attempt)
            blocked = _target_event(attempt, LeaseEvent.RELEASE_FAILED, changes={
                "last_release_gate_failure_type": type(exc).__name__,
                "last_release_gate_failure_at": utc_now_iso(),
            })
            replace_target_record(handle_path, blocked, operation_id=operation_id)
            raise
        # Publication may fail before or after atomic replacement. Preserve
        # either exact disk state for resume instead of overwriting it with a
        # stale pre-publication snapshot in the callback-failure handler.
        attempt = _target_event(attempt, LeaseEvent.QUIESCED, evidence={
            "drain_confirmed": True,
        }, changes={
            "release_quiescence": quiescence,
            "release_quiescence_recorded_at": utc_now_iso(),
        })
        replace_target_record(handle_path, attempt, operation_id=operation_id)

    if resume:
        try:
            # First reconcile a possibly completed delete. This operation is
            # exact-ID only and never mutates provider state.
            await async_provider_call(
                config.api_key,
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
            unresolved = _target_event(attempt, LeaseEvent.RELEASE_FAILED, changes={
                "cleanup_failure_type": type(exc).__name__,
                "cleanup_failure_at": utc_now_iso(),
                "reconciliation_required": True,
            })
            replace_target_record(handle_path, unresolved, operation_id=operation_id)
            raise

    try:
        observed, volume = await _release_inventory(config, contract)
    except BaseException as exc:
        unresolved = _target_event(attempt, LeaseEvent.RELEASE_FAILED, changes={
            "cleanup_failure_type": type(exc).__name__,
            "cleanup_failure_at": utc_now_iso(),
            "reconciliation_required": True,
        })
        replace_target_record(handle_path, unresolved, operation_id=operation_id)
        raise

    cleanup_status = "already_gone" if observed is None else "terminated"
    if observed is not None:
        releasing = {
            **attempt,
            "release_pod_observation": asdict(observed),
            "release_delete_started_at": utc_now_iso(),
        }
        replace_target_record(handle_path, releasing, operation_id=operation_id)
        attempt = releasing
        try:
            await async_provider_call(
                config.api_key, terminate_pod, contract.pod_id, config.api_key,
            )
        except BaseException as exc:
            unresolved = _target_event(attempt, LeaseEvent.RELEASE_FAILED, changes={
                "cleanup_failure_type": type(exc).__name__,
                "cleanup_failure_at": utc_now_iso(),
                "reconciliation_required": True,
            })
            replace_target_record(handle_path, unresolved, operation_id=operation_id)
            raise

    try:
        absence, volume = await _confirm_release_absence(config, contract, attempt)
    except BaseException as exc:
        replace_target_record(handle_path, _target_event(attempt, LeaseEvent.RELEASE_FAILED, changes={
            "cleanup_failure_type": type(exc).__name__,
            "cleanup_failure_at": utc_now_iso(), "reconciliation_required": True,
        }), operation_id=operation_id)
        raise

    cleanup = {
        "pod_id": contract.pod_id,
        "status": cleanup_status,
        "backup_volume_id": volume.get("id"),
        "backup_volume_preserved": True,
        "confirmed_at": utc_now_iso(),
        "absence": absence,
    }
    complete = _target_event(attempt, LeaseEvent.RELEASE_SUCCEEDED, evidence={
        "absence_proof": True,
    }, changes={
        "cleanup_status": "complete",
        "cleanup": cleanup,
        "reconciliation_required": False,
    })
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
    max_create_attempts: int | None = None,
    single_attempt: bool = False,
    before_post: Callable[[dict[str, Any]], dict[str, Any]] | None = None,
) -> AcquiredRunPodTarget:
    """Allocate from a fresh/safe intent or attach to one exact pod.

    The caller must durably create ``attempt`` before calling this function.
    Ambiguous/nonretryable outcomes remain in that record, and this function
    never converts missing evidence into a replacement allocation. A persisted
    in-flight attempt must go through :func:`reconcile_target` first.
    """
    from runpod_lifecycle import AllocationUnknown, LaunchFailure, launch_when_available
    from runpod_lifecycle.errors import ProviderCapacityUnavailable

    operation_id = str(attempt["operation_id"])
    mode = "attach" if pod_id is not None else "allocate"
    initial_state = str(attempt.get("state") or "")

    if pod_id is not None:
        if initial_state != "attachment_pending":
            raise AcquisitionUnresolvedError(
                f"exact attachment is not a fresh intent (state={initial_state!r}); resume by exact pod ID",
                gate="identity", retryable=False, custody=_record_custody(attempt),
            )
        requested_pod_id = str(pod_id).strip()
        if not requested_pod_id:
            raise ValueError("pod_id must be nonempty")
        attempt = apply_target_event(
            handle_path, operation_id, LeaseEvent.ACQUISITION_CONTINUE,
            evidence={"attempt_id": str(uuid.uuid4())},
        )
        attachment_ticket = journal_dispatch_ticket(attempt)
        attempt = apply_target_event(
            handle_path, operation_id, LeaseEvent.PROVIDER_ENTRY,
            evidence={"ticket": attachment_ticket},
        )
        control = attempt.get("lt_control", {})
        if control.get("dispatch_admitted") is not True or control.get("canonical_state") != "ATTACHMENT_PENDING":
            raise AcquisitionUnresolvedError(
                "attachment observation authority expired or was cancelled",
                gate="identity", retryable=False, custody=_record_custody(attempt),
            )
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
                    defer_machine=True,
                ),
            )
            pod = await _get_provider_pod(requested_pod_id, config, name=initial.pod_name)
            if str(getattr(pod, "id", "")) != requested_pod_id or str(
                getattr(pod, "name", "")
            ) != initial.pod_name:
                raise TargetObservationError(
                    f"RunPod attach returned pod identity "
                    f"({getattr(pod, 'id', None)!r}, {getattr(pod, 'name', None)!r}) "
                    f"for requested exact pod ({requested_pod_id!r}, {initial.pod_name!r})",
                    gate="identity", retryable=False, custody=_record_custody(attempt),
                )
        except BaseException as exc:
            unresolved = _target_event(attempt, LeaseEvent.ACQUISITION_FAILED, changes={
                "failure_type": type(exc).__name__,
                "failure_gate": observation_failure_gate(exc),
                "failed_at": utc_now_iso(),
                "reconciliation_required": True,
            })
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
                "reconcile the existing request before another create",
                gate="identity", retryable=False, custody=_record_custody(attempt),
            )
        if initial_state != "allocation_ready" and not allow_capacity_retry:
            raise AcquisitionUnresolvedError(
                "a prior capacity response must be explicitly resumed before another create",
                gate="identity", retryable=False, custody=_record_custody(attempt),
            )
        create_attempts = attempt.get("create_attempts", 0)
        if isinstance(create_attempts, bool) or not isinstance(create_attempts, int):
            raise AcquisitionUnresolvedError(
                "allocation journal has an invalid create_attempts count",
                gate="identity", retryable=False, custody=_record_custody(attempt),
            )
        if initial_state == "allocation_ready" and create_attempts != 0:
            raise AcquisitionUnresolvedError(
                "allocation journal says a create already started; reconcile it before another create",
                gate="identity", retryable=False, custody=_record_custody(attempt),
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
                "allocation journal does not prove the prior capacity response was safe to retry",
                gate="identity", retryable=False, custody=_record_custody(attempt),
            )
        # Every lifecycle operation must contain one concrete POST, including
        # callers without an attempt cap. The journal owns every retry.
        if not hasattr(config, "ram_tiers_enabled"):
            # Minimal provider configurations must also produce a single RAM
            # tier. Set it explicitly rather than let lifecycle infer a matrix.
            import copy

            config = copy.copy(config)
            config.ram_tiers_enabled = False
        storages = {name for name in [getattr(config, "storage_name", None),
                                      *getattr(config, "storage_volumes", ())] if name}
        exact_volume = getattr(config, "network_volume_id", None) is not None
        if (len(config.gpu_type_candidates) != 1 or config.ram_tiers_enabled
                or len(storages) > 1 or (exact_volume and storages)
                or (max_create_attempts is not None and (max_create_attempts < 1
                    or capacity_window_seconds < 60 or retry_interval_seconds < 15))):
            raise ValueError("bounded capacity retries require one GPU, RAM tier and storage")
        expected_name = str(attempt.get("request_name") or "").strip()
        if not expected_name:
            raise ValueError("allocation attempt must have an exact request_name")
        deadline = (
            time.monotonic() + max_wait_seconds
            if max_wait_seconds > 0
            else None
        )
        while True:
            if max_create_attempts is not None and attempt.get("create_attempts", 0) >= max_create_attempts:
                exhausted = _target_event(attempt, LeaseEvent.CAPACITY_EXHAUSTED, changes={
                    "reconciliation_required": False, "completed_at": utc_now_iso(),
                })
                replace_target_record(handle_path, exhausted, operation_id=operation_id)
                raise CapacityExhaustedError("capacity_exhausted: create attempt limit reached", gate="capacity", retryable=False, custody="none")
            if deadline is not None:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    exhausted = _target_event(attempt, LeaseEvent.CAPACITY_EXHAUSTED, changes={
                        "reconciliation_required": False,
                        "completed_at": utc_now_iso(),
                    })
                    replace_target_record(
                        handle_path, exhausted, operation_id=operation_id
                    )
                    if max_create_attempts is not None:
                        raise CapacityExhaustedError("capacity_exhausted: capacity window elapsed", gate="capacity", retryable=False, custody="none")
                    raise TimeoutError(
                        f"capacity did not appear within {max_wait_seconds}s"
                    )
                window = min(capacity_window_seconds, max(1, int(remaining)))
            else:
                window = capacity_window_seconds

            if before_post is not None:
                attempt = before_post(attempt)
            attempt_id = str(uuid.uuid4())
            pending = _target_event(attempt, LeaseEvent.CREATE_STARTED, evidence={
                "attempt_id": attempt_id,
            }, changes={
                "create_attempts": int(attempt.get("create_attempts", 0)) + 1,
                "reconciliation_required": True,
                "last_create_started_at": utc_now_iso(),
            })
            replace_target_record(handle_path, pending, operation_id=operation_id)
            attempt = pending
            ticket = journal_dispatch_ticket(pending)

            def enter_create() -> None:
                # ProviderGate calls this after rate admission and cancellation
                # checks, immediately before the billable POST.
                admitted = apply_target_event(
                    handle_path, operation_id, LeaseEvent.PROVIDER_ENTRY,
                    evidence={"ticket": ticket},
                )
                control = admitted.get("lt_control")
                if (not isinstance(control, Mapping)
                        or control.get("dispatch_admitted") is not True
                        or control.get("canonical_state") != "CREATE_PENDING"):
                    raise AcquisitionUnresolvedError(
                        "create intent expired or was fenced before provider entry",
                        gate="identity", retryable=False, custody=_record_custody(admitted),
                    )

            def retain_late_pod(pod):
                # The cancelled caller releases ownership before this worker
                # can update custody. Re-read rather than restoring its stale
                # snapshot over a concurrent reconciliation or cleanup.
                with target_owner(handle_path, blocking=True):
                    current = json.loads(handle_path.read_text(encoding="utf-8"))
                    if (current.get("operation_id") != operation_id
                            or current.get("request_name") != expected_name
                            or current.get("pod_id") not in (None, str(pod.id))):
                        raise AcquisitionUnresolvedError("late create result has conflicting journal custody")
                apply_target_event(handle_path, operation_id, LeaseEvent.LATE_CREATE_SUCCEEDED, evidence={
                    "pod_id": str(pod.id),
                }, changes={
                    "name": str(pod.name),
                    "allocated_at": utc_now_iso(),
                    "reconciliation_required": current.get("state") != "cleanup_complete",
                })

            try:
                with provider_budget(
                    max(0., deadline - time.monotonic()) if deadline is not None else window,
                    entry=enter_create,
                ):
                    pod = await async_provider_operation(
                        config.api_key, launch_when_available, config,
                        on_late_result=retain_late_pod,
                        name=expected_name,
                        # Zero asks lifecycle for one launch; the bounded loop
                        # owns timing and persists each POST before it starts.
                        max_wait_sec=0,
                        retry_interval_sec=retry_interval_seconds,
                    )
            except ProviderNotStarted:
                # Only admission can produce this type. No provider operation
                # began, so restore the exact pre-create custody and counter.
                apply_target_event(handle_path, operation_id, LeaseEvent.CREATE_NOT_SENT,
                    evidence={"attempt_id": attempt_id, "no_allocation_proof": True},
                    changes={"reconciliation_required": False})
                raise
            except AllocationUnknown as exc:
                apply_target_event(handle_path, operation_id, LeaseEvent.CREATE_UNKNOWN, changes={
                    "reconciliation_required": True,
                    "unresolved_at": utc_now_iso(),
                    "gpu_type_attempted": exc.gpu_type,
                    "ram_tier_attempted": exc.ram_tier,
                    "storage_name_attempted": exc.storage_name,
                    "storage_volume_id_attempted": exc.storage_volume_id,
                })
                raise
            except LaunchFailure as exc:
                if (not getattr(exc, "retryable", True) or
                        not isinstance(exc.__cause__, ProviderCapacityUnavailable)):
                    known_pod_id = getattr(exc, "pod_id", None)
                    if known_pod_id:
                        failed = apply_target_event(handle_path, operation_id,
                            LeaseEvent.CREATE_FAILED_EXACT, evidence={"pod_id": str(known_pod_id)}, changes={
                            "failure_type": type(exc).__name__,
                            "failure_gate": observation_failure_gate(exc),
                            "failed_at": utc_now_iso(),
                            "reconciliation_required": True,
                        })
                    else:
                        failed = apply_target_event(handle_path, operation_id,
                            LeaseEvent.CREATE_UNKNOWN, changes={
                        "failure_type": type(exc).__name__,
                        "failure_gate": observation_failure_gate(exc),
                        "failed_at": utc_now_iso(),
                        "reconciliation_required": True,
                        })
                    raise
                retry_ready = apply_target_event(handle_path, operation_id,
                    LeaseEvent.CAPACITY_REJECTED, evidence={"no_allocation_proof": True}, changes={
                    "reconciliation_required": False,
                    "last_capacity_rejection_at": utc_now_iso(),
                    "last_failure_type": type(exc).__name__,
                })
                attempt = retry_ready
                if single_attempt:
                    raise CapacityExhaustedError("capacity_unavailable: one definite capacity rejection", gate="capacity", retryable=True, custody="none") from exc
                if max_create_attempts is None or attempt["create_attempts"] < max_create_attempts:
                    delay = min(60.0, retry_interval_seconds * 1.5 ** (attempt["create_attempts"] - 1))
                    if deadline is not None:
                        delay = min(delay, max(0.0, deadline - time.monotonic()))
                    await asyncio.sleep(delay)
                continue
            except asyncio.CancelledError as exc:
                # A cancellation or lost response during the POST cannot prove
                # that no pod exists. Preserve this exact attempt for recovery.
                apply_target_event(handle_path, operation_id, LeaseEvent.CREATE_UNKNOWN, changes={
                    "reconciliation_required": True,
                    "failure_type": type(exc).__name__,
                    "unresolved_at": utc_now_iso(),
                })
                raise

            allocated = apply_target_event(handle_path, operation_id, LeaseEvent.CREATE_SUCCEEDED,
                evidence={"pod_id": str(pod.id)}, changes={
                "name": str(pod.name),
                "allocated_at": utc_now_iso(),
                "reconciliation_required": True,
            })
            break

    if mode == "attach":
        allocated = _target_event(attempt, LeaseEvent.ATTACHMENT_BOUND, evidence={
            "pod_id": str(pod.id), "ticket": attachment_ticket,
        }, changes={
            "name": str(pod.name),
            "allocated_at": utc_now_iso(),
            "reconciliation_required": True,
        })
        replace_target_record(handle_path, allocated, operation_id=operation_id)

    try:
        if expected_name is not None and str(getattr(pod, "name", "")) != expected_name:
            raise TargetObservationError(
                f"RunPod launch returned pod name {getattr(pod, 'name', None)!r}; "
                f"expected exact request name {expected_name!r}",
                gate="identity", retryable=False, custody="exact",
            )
        allocated = await _protect_allocated_target(config, allocated, handle_path, ready_timeout_seconds)
        await _wait_provider_ready(config, pod, ready_timeout_seconds)
    except BaseException as exc:
        if isinstance(exc, TargetObservationError):
            exc.custody = _record_custody(allocated)
        failed = _target_event(allocated, LeaseEvent.ACQUISITION_FAILED, changes={
            "failure_phase": "readiness",
            "failure_type": type(exc).__name__,
            "failure_gate": observation_failure_gate(exc),
            "failed_at": utc_now_iso(),
            "reconciliation_required": True,
        })
        replace_target_record(handle_path, failed, operation_id=operation_id)
        raise

    try:
        observation = await observe_and_confirm_acquired_target(
            config.api_key,
            _target_contract(
                config,
                pod_id=str(pod.id),
                expected_name=expected_name,
                volume=volume,
            ),
            ready_timeout_seconds=ready_timeout_seconds,
        )
    except BaseException as exc:
        if isinstance(exc, TargetObservationError):
            exc.custody = _record_custody(allocated)
        failed = _target_event(allocated, LeaseEvent.ACQUISITION_FAILED, changes={
            "failure_phase": "provider_contract",
            "failure_type": type(exc).__name__,
            "failure_gate": observation_failure_gate(exc),
            "failed_at": utc_now_iso(),
            "reconciliation_required": True,
        })
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
            f"journal state {state!r} does not require reconciliation",
            gate="identity", retryable=False, custody=_record_custody(attempt),
        )

    pod_id = _text(attempt.get("pod_id"))
    if mode == "attach" and pod_id is None:
        pod_id = _text(attempt.get("requested_pod_id"))

    if mode == "allocate" and pod_id is None:
        request_name = _text(attempt.get("request_name"))
        if request_name is None:
            raise AcquisitionUnresolvedError(
                "allocation journal has no exact request name; cannot reconcile",
                gate="identity", retryable=False, custody=_record_custody(attempt),
            )
        started = _target_event(attempt, LeaseEvent.RECONCILIATION_UNKNOWN, changes={
            "last_reconcile_started_at": utc_now_iso(),
            "reconciliation_required": True,
        })
        replace_target_record(handle_path, started, operation_id=operation_id)
        attempt = started
        try:
            pods = await bounded_observation(list_pods, config.api_key)
        except Exception as exc:
            failed_observation = _target_event(attempt, LeaseEvent.RECONCILIATION_UNKNOWN, changes={
                "last_reconciled_at": utc_now_iso(),
                "last_reconciliation_failure_type": type(exc).__name__,
            })
            replace_target_record(
                handle_path, failed_observation, operation_id=operation_id
            )
            raise AcquisitionUnresolvedError(
                "RunPod account inventory is unavailable; the original allocation remains unresolved",
                gate="identity", retryable=True, custody=_record_custody(attempt),
            ) from exc
        if not isinstance(pods, list):
            raise AcquisitionUnresolvedError(
                "RunPod account inventory returned malformed data; the original allocation remains unresolved",
                gate="identity", retryable=True, custody=_record_custody(attempt),
            )

        # Count every exact-name match before checking the desired resource
        # contract. A valid-looking match cannot disambiguate a duplicate name.
        matches = [
            record for record in pods
            if isinstance(record, dict) and _text(record.get("name")) == request_name
        ]
        if len(matches) != 1:
            unresolved = _target_event(attempt, LeaseEvent.RECONCILIATION_UNKNOWN, changes={
                "last_reconciled_at": utc_now_iso(),
                "last_reconciliation_match_count": len(matches),
                "last_reconciliation_candidate_ids": [
                    value
                    for record in matches
                    if (value := _text(record.get("id"))) is not None
                ],
                "reconciliation_required": True,
            })
            replace_target_record(handle_path, unresolved, operation_id=operation_id)
            raise AcquisitionUnresolvedError(
                f"request {request_name!r} has {len(matches)} exact-name matches; "
                "preserve custody and retry reconciliation later",
                gate="identity", retryable=False, custody=_record_custody(attempt),
            )

        pod_id = _text(matches[0].get("id"))
        if pod_id is None:
            unresolved = _target_event(attempt, LeaseEvent.RECONCILIATION_UNKNOWN, changes={
                "last_reconciled_at": utc_now_iso(),
                "last_reconciliation_failure_type": "MissingPodId",
                "reconciliation_required": True,
            })
            replace_target_record(handle_path, unresolved, operation_id=operation_id)
            raise AcquisitionUnresolvedError(
                "the exact-name provider record has no pod ID; the original allocation remains unresolved",
                gate="identity", retryable=True, custody=_record_custody(attempt),
            )

    expected_name = _text(attempt.get("name"))
    if expected_name is None:
        expected_name = _text(attempt.get("request_name"))

    bound = _target_event(attempt, LeaseEvent.RECONCILIATION_FOUND, evidence={
        "pod_id": pod_id,
    }, changes={
        "name": expected_name,
        "reconciled_pod_id_at": utc_now_iso(),
        "reconciliation_required": True,
    })
    replace_target_record(handle_path, bound, operation_id=operation_id)

    try:
        bound = await _protect_allocated_target(config, bound, handle_path, ready_timeout_seconds)
        initial_observation = await observe_exact_target(
            config.api_key,
            _target_contract(
                config,
                pod_id=pod_id,
                expected_name=expected_name,
                volume=volume,
                defer_machine=True,
            ),
        )
        if expected_name is None:
            expected_name = initial_observation.pod_name
            bound = _target_event(bound, LeaseEvent.CHECKPOINT, changes={"name": expected_name})
            replace_target_record(handle_path, bound, operation_id=operation_id)
        pod = await _get_provider_pod(pod_id, config, name=expected_name)
        if str(getattr(pod, "id", "")) != pod_id or (
            expected_name is not None
            and str(getattr(pod, "name", "")) != expected_name
        ):
            raise TargetObservationError(
                f"RunPod lookup returned pod identity "
                f"({getattr(pod, 'id', None)!r}, {getattr(pod, 'name', None)!r}) "
                f"for exact reconciled pod ({pod_id!r}, {expected_name!r})",
                gate="identity", retryable=False, custody=_record_custody(bound),
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

    verified = _target_event(bound, LeaseEvent.CHECKPOINT, changes={
        "name": observation.pod_name,
        "provider_observation": observation.to_dict(),
        "target_identity_verified": True,
        "last_reconciled_at": utc_now_iso(),
    })
    replace_target_record(handle_path, verified, operation_id=operation_id)
    return AcquiredRunPodTarget(
        pod=pod,
        volume=dict(volume),
        attempt=verified,
        observation=observation,
        acquisition_mode=mode,
    )


def _gpu_type_from_graphql(gpu_id: str, api_key: str) -> dict:
    """The GPU row from the provider's GraphQL gpuTypes catalog (the design source, not REST)."""
    from runpod.api.graphql import run_graphql_query

    query = "query { gpuTypes(input: {id: %s}) { id displayName } }" % json.dumps(gpu_id)
    response = provider_call(api_key, lambda: run_graphql_query(query, api_key=api_key))
    rows = ((response or {}).get("data") or {}).get("gpuTypes") or []
    for row in rows:
        if isinstance(row, dict) and row.get("id") == gpu_id:
            return row
    raise LookupError(f"GPU type {gpu_id!r} is not in the gpuTypes catalog")
