#!/usr/bin/env python3
"""Claim an RTX 5090 on the existing Astrid ``backup`` volume.

This is an operator-facing waiter, not a second RunPod client.  It uses the
shared Astrid environment and the canonical ``runpod-lifecycle`` launch path:
the exact GPU/storage request is retried until capacity appears.  The
network-volume size is discovered and echoed back unchanged; the 200 GB
request applies only to the pod's disposable container disk.

The defaults target the prepared H3 CUDA-13 release: they request its selected
image/host profile and verify mounted release paths and Python version before
the script reports success. They do not probe or qualify the GPU. Override the
profile only for another explicitly prepared runtime.

Before launch the script exclusively creates a secret-free allocation-attempt
marker.  On success it atomically replaces that marker with the claimed pod
handle and exits under the ``leave_running`` policy.  The handle can be handed
to ``runpod-lifecycle terminate <pod-id> --yes`` when an authorized owner later
chooses to terminate the pod.
"""

from __future__ import annotations

import argparse
import asyncio
from runtime_protocol import platform
import hashlib
import json
import os
import shlex
import sys
import uuid
from pathlib import Path
from typing import Any, Mapping

from runpod_lifecycle import (
    RunPodConfig,
)

from astrid.core.util.time import utc_now_iso
from astrid.packs.runpod.worker_preparation import (
    AcquiredRunPodTarget,
    acquire_target,
    reconcile_target,
    replace_target_record,
    resolve_existing_volume,
)
from astrid.packs.runpod.worker_preparation import (
    create_target_record_exclusive as _create_json_exclusive,
)
from astrid.packs.runpod.worker_preparation import (
    write_target_record as _write_json,
)

DEFAULT_GPU = "NVIDIA GeForce RTX 5090"
DEFAULT_STORAGE = "backup"
DEFAULT_CONTAINER_DISK_GB = 200
DEFAULT_POLL_SECONDS = 120
DEFAULT_CAPACITY_WINDOW_SECONDS = 3600
DEFAULT_MAX_WAIT_SECONDS = 43200
DEFAULT_READY_TIMEOUT_SECONDS = 900
DEFAULT_WORKER_IMAGE = "runpod/base:1.0.3-dev-fix-pytorch-version-verification-cuda1300-ubuntu2404"
DEFAULT_ALLOWED_CUDA_VERSIONS = ("13.0",)
DEFAULT_RELEASE_ROOT = "/workspace/h3-golden/releases/h3-cu130-v1-candidate"
DEFAULT_HANDLE_PATH = Path(
    ".otto/runs/h3-av-simplicity-20260924-T2/runpod/claim-handle.json"
)
DEFAULT_LIFECYCLE_POLICY = "leave_running"


async def _preflight_release(pod: Any, release_root: str) -> dict[str, str]:
    """Check mounted release paths and interpreter version without GPU probes."""
    python_path = f"{release_root}/runtime/venv/bin/python"
    launcher_path = f"{release_root}/runtime/launch-comfy.sh"
    command = f"""
set -eu
test -d /workspace
test -x {shlex.quote(python_path)}
test -x {shlex.quote(launcher_path)}
{shlex.quote(python_path)} -B - <<'PY'
import sys

if sys.version_info[:2] != (3, 12):
    raise SystemExit(f"release requires Python 3.12, got {{sys.version}}")
print(f"python={{sys.version.split()[0]}}")
PY
"""
    exit_code, stdout, stderr = await pod.exec_ssh(command, timeout=120)
    if exit_code != 0:
        detail = (stderr or stdout).strip()[-4000:]
        raise RuntimeError(f"H3 release path check failed for {release_root}: {detail}")
    return {
        "release_root": release_root,
        "release_python": python_path,
        "release_launcher": launcher_path,
        "probe": stdout.strip(),
    }


def _log(message: str) -> None:
    print(f"{utc_now_iso()} {message}", file=sys.stderr, flush=True)


def _existing_handle_error(path: Path) -> RuntimeError:
    try:
        existing = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        return RuntimeError(
            f"claim custody already exists at {path} and cannot be read safely: {exc}"
        )
    if not isinstance(existing, dict):
        return RuntimeError(
            f"claim custody already exists at {path} and is not a JSON object"
        )
    if existing.get("state") in {
        "allocation_ready",
        "capacity_retry_ready",
        "capacity_exhausted",
        "allocation_pending",
        "allocation_unknown",
        "allocated_unverified",
        "attachment_pending",
        "attached_unverified",
    }:
        return RuntimeError(
            f"allocation request {existing.get('request_name')!r} at {path} "
            "requires reconciliation; rerun this command with --resume"
        )
    if existing.get("pod_id"):
        return RuntimeError(
            f"claim handle at {path} already identifies pod {existing['pod_id']}; "
            "reconcile that pod before another launch"
        )
    return RuntimeError(f"claim custody already exists at {path}; inspect it before retrying")


def _canonical_digest(value: Mapping[str, Any]) -> str:
    payload = json.dumps(value, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return f"sha256:{hashlib.sha256(payload).hexdigest()}"


def _receipt_dir_for_handle(path: Path) -> Path:
    if path.parent.name == "runpod":
        return path.parent.parent / "receipts"
    return path.parent / "receipts"


def _matching_terminal_cleanup(
    path: Path, existing: Mapping[str, Any]
) -> dict[str, str] | None:
    """Find exact, identity-bound evidence that an old claimed pod is terminal."""
    pod_id = str(existing.get("pod_id") or "").strip()
    volume_id = str(existing.get("network_volume_id") or "").strip()
    if not pod_id or not volume_id:
        return None
    expected_digest = _canonical_digest(existing)
    matches: list[tuple[int, Path, str]] = []
    receipt_dir = _receipt_dir_for_handle(path)
    if not receipt_dir.is_dir():
        return None
    for receipt_path in receipt_dir.glob("*.json"):
        try:
            receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        if not isinstance(receipt, dict):
            continue
        cleanup = receipt.get("cleanup")
        if not isinstance(cleanup, dict):
            continue
        if receipt.get("claim_handle_digest") != expected_digest:
            continue
        if str(receipt.get("pod_id") or "").strip() != pod_id:
            continue
        if str(cleanup.get("pod_id") or "").strip() != pod_id:
            continue
        if receipt.get("cleanup_status") != "complete":
            continue
        status = str(cleanup.get("status") or "").strip()
        if status not in {"terminated", "already_gone", "absent"}:
            continue
        if str(cleanup.get("backup_volume_id") or "").strip() != volume_id:
            continue
        if cleanup.get("backup_volume_preserved") is not True:
            continue
        matches.append((receipt_path.stat().st_mtime_ns, receipt_path, status))
    if not matches:
        return None
    _mtime, receipt_path, status = max(matches, key=lambda item: item[0])
    return {
        "pod_id": pod_id,
        "handle_digest": expected_digest,
        "cleanup_status": status,
        "cleanup_receipt_path": str(receipt_path.resolve()),
    }


def _rollover_candidate(path: Path) -> dict[str, str] | None:
    if not path.exists():
        return None
    try:
        existing = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise _existing_handle_error(path) from exc
    if not isinstance(existing, dict):
        raise _existing_handle_error(path)
    terminal = _matching_terminal_cleanup(path, existing)
    if terminal is None:
        raise _existing_handle_error(path)
    return terminal


def _archive_path(path: Path, rollover: Mapping[str, str]) -> Path:
    digest = rollover["handle_digest"].removeprefix("sha256:")[:16]
    pod_id = "".join(
        character
        for character in rollover["pod_id"]
        if character.isalnum() or character in "-_"
    )
    return path.with_name(f"{path.stem}.archived-{pod_id}-{digest}{path.suffix}")


def _archive_bytes(path: Path, data: bytes) -> None:
    try:
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except FileExistsError:
        if path.read_bytes() != data:
            raise RuntimeError(f"claim archive collision at {path}; refusing rollover")
        return
    try:
        with os.fdopen(fd, "wb") as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
    except BaseException:
        path.unlink(missing_ok=True)
        raise


def _start_allocation_attempt(
    path: Path,
    *,
    args: argparse.Namespace,
    volume: dict[str, Any],
    rollover: dict[str, str] | None = None,
    pod_id: str | None = None,
) -> dict[str, Any]:
    operation_id = uuid.uuid4().hex
    request_name = f"{args.name_prefix}-{operation_id}" if pod_id is None else None
    attempt = {
        "schema_version": "astrid.runpod.allocation-attempt.v1",
        "state": "allocation_ready" if pod_id is None else "attachment_pending",
        "operation_id": operation_id,
        "request_name": request_name,
        "requested_at": utc_now_iso(),
        "reconciliation_required": True,
        "api_key_ref": "RUNPOD_API_KEY",
        "provider_account_ref": args.provider_account_ref,
        "acquisition_mode": "allocate" if pod_id is None else "attach",
        "release_authorized": (
            True if pod_id is None else bool(getattr(args, "allow_pod_release", False))
        ),
        "gpu_type": args.gpu_type,
        "storage_name": args.storage_name,
        "network_volume_id": volume.get("id"),
        "network_volume_name": volume.get("name") or args.storage_name,
        "network_volume_size_gb": int(volume["size"]),
        "network_volume_datacenter_id": volume.get("dataCenterId"),
        "attach_only": True,
        "container_disk_gb": args.container_disk_gb,
        "worker_image": args.worker_image,
        "template_id": args.template_id,
        "allowed_cuda_versions": [
            part.strip() for part in args.allowed_cuda_versions.split(",") if part.strip()
        ],
        "max_wait_seconds": args.max_wait_seconds,
        "poll_seconds": args.poll_seconds,
        "lifecycle": {"mode": args.lifecycle_policy},
    }
    if pod_id is None:
        attempt["create_attempts"] = 0
    if pod_id is not None:
        attempt["requested_pod_id"] = pod_id
    if rollover is not None:
        archive_path = _archive_path(path, rollover)
        attempt["previous_claim"] = {
            **rollover,
            "archive_path": str(archive_path.resolve()),
        }
        try:
            with path.open("rb") as stream:
                platform.lock_file(stream.fileno())
                opened = os.fstat(stream.fileno())
                current_path = path.stat()
                if (opened.st_dev, opened.st_ino) != (
                    current_path.st_dev,
                    current_path.st_ino,
                ):
                    raise RuntimeError(
                        f"claim custody at {path} changed during rollover; refusing to overwrite it"
                    )
                current_bytes = stream.read()
                try:
                    current = json.loads(current_bytes)
                except json.JSONDecodeError as exc:
                    raise RuntimeError(
                        f"cannot verify stale claim custody at {path}: {exc}"
                    ) from exc
                if (
                    not isinstance(current, dict)
                    or _canonical_digest(current) != rollover["handle_digest"]
                ):
                    raise RuntimeError(
                        f"claim custody at {path} changed during rollover; refusing to overwrite it"
                    )
                refreshed = _matching_terminal_cleanup(path, current)
                if refreshed != rollover:
                    raise RuntimeError(
                        f"terminal cleanup evidence for {rollover['pod_id']} changed during rollover"
                    )
                _archive_bytes(archive_path, current_bytes)
                _write_json(path, attempt)
        except FileNotFoundError as exc:
            raise RuntimeError(f"claim custody at {path} changed during rollover") from exc
        return attempt
    try:
        _create_json_exclusive(path, attempt)
    except FileExistsError as exc:
        raise _existing_handle_error(path) from exc
    return attempt


def _read_resumable_attempt(path: Path) -> dict[str, Any]:
    try:
        existing = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise RuntimeError(f"cannot read resumable RunPod custody at {path}: {exc}") from exc
    if not isinstance(existing, dict) or not str(existing.get("schema_version", "")).startswith(
        "astrid.runpod.allocation-attempt."
    ):
        raise RuntimeError(f"{path} does not contain a resumable RunPod allocation attempt")
    if not existing.get("operation_id"):
        raise RuntimeError(f"RunPod allocation attempt at {path} has no operation ID")
    if existing.get("state") not in {
        "allocation_ready",
        "capacity_retry_ready",
        "capacity_exhausted",
        "allocation_pending",
        "allocation_unknown",
        "allocated_unverified",
        "attachment_pending",
        "attached_unverified",
    }:
        raise RuntimeError(
            f"RunPod custody at {path} is not resumable (state={existing.get('state')!r})"
        )
    return existing


def _validate_resume_request(
    attempt: Mapping[str, Any],
    *,
    args: argparse.Namespace,
    volume: Mapping[str, Any],
) -> None:
    expected = {
        "provider_account_ref": args.provider_account_ref,
        "gpu_type": args.gpu_type,
        "storage_name": args.storage_name,
        "network_volume_id": volume.get("id"),
        "network_volume_name": volume.get("name") or args.storage_name,
        "network_volume_size_gb": int(volume["size"]),
        "network_volume_datacenter_id": volume.get("dataCenterId"),
        "container_disk_gb": args.container_disk_gb,
        "worker_image": args.worker_image,
        "template_id": args.template_id,
        "allowed_cuda_versions": [
            part.strip() for part in args.allowed_cuda_versions.split(",") if part.strip()
        ],
    }
    mismatches = [
        key for key, value in expected.items()
        if attempt.get(key) != value
    ]
    expected_mode = "attach" if getattr(args, "pod_id", None) is not None else "allocate"
    if attempt.get("acquisition_mode", "allocate") != expected_mode:
        mismatches.append("acquisition_mode")
    if expected_mode == "attach" and attempt.get("requested_pod_id") != args.pod_id:
        mismatches.append("requested_pod_id")
    if mismatches:
        raise RuntimeError(
            "resume request differs from the original RunPod intent in: "
            + ", ".join(sorted(set(mismatches)))
        )


def _replace_allocation_attempt(
    path: Path,
    value: dict[str, Any],
    *,
    operation_id: str,
) -> None:
    replace_target_record(path, value, operation_id=operation_id)


async def _require_existing_volume(config: RunPodConfig, name_or_id: str) -> dict[str, Any]:
    return await resolve_existing_volume(config.api_key, name_or_id)


def _handle(
    *,
    pod: Any,
    ssh: dict[str, Any],
    config: RunPodConfig,
    volume: dict[str, Any],
    claimed_at: str,
    preflight: dict[str, str],
    attempt: dict[str, Any],
) -> dict[str, Any]:
    selected_gpu = getattr(pod, "_gpu_type", None) or config.gpu_type
    selected_storage = getattr(pod, "_storage_name", None) or config.storage_name
    return {
        "schema_version": "astrid.runpod.claim.v1",
        "state": "claimed",
        "operation_id": attempt["operation_id"],
        "request_name": attempt.get("request_name") or str(pod.name),
        "pod_id": str(pod.id),
        "name": str(pod.name),
        "ssh": f"root@{ssh['ip']} -p {ssh['port']}",
        "claimed_at": claimed_at,
        "gpu_type": selected_gpu,
        "storage_name": selected_storage,
        # The exact account inventory check is authoritative for this identity;
        # SDK-local storage metadata is not a substitute for its pinned ID.
        "network_volume_id": volume.get("id"),
        "network_volume_name": str(volume.get("name") or ""),
        "network_volume_size_gb": int(volume["size"]),
        "network_volume_datacenter_id": volume.get("dataCenterId"),
        "attach_only": True,
        "container_disk_gb": config.container_disk_gb,
        "volume_mount_path": config.volume_mount_path,
        "allowed_cuda_versions": list(config.allowed_cuda_versions),
        "worker_image": config.worker_image,
        "template_id": config.template_id,
        "name_prefix": config.name_prefix,
        "api_key_ref": attempt.get("api_key_ref", "RUNPOD_API_KEY"),
        "provider_account_ref": attempt.get("provider_account_ref"),
        "acquisition_mode": attempt.get("acquisition_mode", "allocate"),
        "release_authorized": attempt.get("release_authorized", False),
        "provider_observation": attempt.get("provider_observation"),
        "lifecycle": {"mode": DEFAULT_LIFECYCLE_POLICY},
        "reconciliation_required": False,
        "runtime_preflight": preflight,
        **(
            {"previous_claim": attempt["previous_claim"]}
            if "previous_claim" in attempt
            else {}
        ),
    }


async def _finish_claim(
    *,
    args: argparse.Namespace,
    handle_path: Path,
    config: RunPodConfig,
    acquired: AcquiredRunPodTarget,
) -> dict[str, Any]:
    pod = acquired.pod
    volume = acquired.volume
    attempt = acquired.attempt
    operation_id = str(attempt["operation_id"])
    pod_id = str(pod.id)
    allocated = dict(attempt)
    observation = acquired.observation

    failure_phase = "ssh"
    try:
        ssh = await pod._ensure_ssh_details()
        failure_phase = "release_preflight"
        preflight = await _preflight_release(pod, args.release_root)
        final_attempt = {
            **attempt,
            "acquisition_mode": acquired.acquisition_mode,
            "provider_observation": observation.to_dict(),
        }
        result = _handle(
            pod=pod,
            ssh=ssh,
            config=config,
            volume=volume,
            claimed_at=utc_now_iso(),
            preflight=preflight,
            attempt=final_attempt,
        )
        result["handle_path"] = str(handle_path)
        _replace_allocation_attempt(
            handle_path, result, operation_id=operation_id
        )
        return result
    except BaseException as exc:
        failed = {
            **allocated,
            "state": "allocated_unverified",
            "failure_phase": failure_phase,
            "failure_type": type(exc).__name__,
            "failed_at": utc_now_iso(),
            "reconciliation_required": True,
        }
        _replace_allocation_attempt(handle_path, failed, operation_id=operation_id)
        _log(
            f"pod {pod_id} failed during {failure_phase}: {type(exc).__name__}; "
            "leave_running keeps the exact pod running"
        )
        raise


async def _claim(args: argparse.Namespace) -> dict[str, Any]:
    provider_account_ref = getattr(args, "provider_account_ref", None)
    if not isinstance(provider_account_ref, str) or not provider_account_ref.strip():
        raise RuntimeError(
            "a stable non-secret --provider-account-ref (or ASTRID_RUNPOD_ACCOUNT_REF) is required"
        )
    if provider_account_ref.startswith(("rpa_", "sk-")):
        raise RuntimeError("provider_account_ref must be a non-secret account/profile label")
    handle_path = Path(args.handle_path).expanduser().resolve()
    resuming = bool(getattr(args, "resume", False))
    if resuming:
        if not handle_path.is_file():
            raise RuntimeError(f"no RunPod custody exists to resume at {handle_path}")
        rollover = None
        prior_attempt = _read_resumable_attempt(handle_path)
    else:
        rollover = _rollover_candidate(handle_path)
        prior_attempt = None

    # RunPodConfig.from_env() is the shared Astrid credential/config boundary;
    # explicit values below prevent ambient GPU/storage fallbacks from changing
    # the request this waiter is intended to claim.
    config = RunPodConfig.from_env(
        gpu_type=args.gpu_type,
        storage_name=args.storage_name,
        storage_volumes=(args.storage_name,),
        container_disk_gb=args.container_disk_gb,
        min_memory_gb=args.min_memory_gb,
        name_prefix=args.name_prefix,
        worker_image=args.worker_image,
        # The selected H3 image is authoritative; do not let the generic
        # runpod-torch-v240 template silently replace it.
        template_id=args.template_id,
        allowed_cuda_versions=tuple(args.allowed_cuda_versions.split(",")),
        # The existing backup volume is attachment-only. A zero volume request
        # plus attach_only prevents lifecycle from expanding or replacing it.
        disk_size_gb=0,
        attach_only=True,
    )
    volume = await _require_existing_volume(config, args.storage_name)
    if prior_attempt is not None:
        _validate_resume_request(prior_attempt, args=args, volume=volume)
        attempt = prior_attempt
    else:
        attempt = _start_allocation_attempt(
            handle_path,
            args=args,
            volume=volume,
            rollover=rollover,
            pod_id=getattr(args, "pod_id", None),
        )
    operation_id = str(attempt["operation_id"])
    request_name = str(attempt.get("request_name") or "")
    _log(
        f"operation_id={operation_id} request_name={request_name!r} "
        f"watching gpu={args.gpu_type!r} storage={args.storage_name!r} "
        f"volume_id={volume.get('id')!r} volume_size_gb={int(volume['size'])} "
        f"container_disk_gb={args.container_disk_gb} image={args.worker_image!r} "
        f"allowed_cuda={config.allowed_cuda_versions!r} release={args.release_root!r} "
        f"poll_seconds={args.poll_seconds} max_wait_seconds={args.max_wait_seconds} "
        f"lifecycle={args.lifecycle_policy!r} handle_path={str(handle_path)!r}"
    )
    if resuming and attempt.get("state") not in {
        "capacity_retry_ready",
        "capacity_exhausted",
    }:
        acquired = await reconcile_target(
            config,
            handle_path=handle_path,
            attempt=attempt,
            volume=volume,
            ready_timeout_seconds=args.ready_timeout_seconds,
        )
    else:
        acquired = await acquire_target(
            config,
            handle_path=handle_path,
            attempt=attempt,
            volume=volume,
            pod_id=getattr(args, "pod_id", None),
            max_wait_seconds=args.max_wait_seconds,
            capacity_window_seconds=args.capacity_window_seconds,
            retry_interval_seconds=args.poll_seconds,
            ready_timeout_seconds=args.ready_timeout_seconds,
            allow_capacity_retry=resuming,
        )
    return await _finish_claim(
        args=args,
        handle_path=handle_path,
        config=config,
        acquired=acquired,
    )


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--gpu-type", default=DEFAULT_GPU)
    parser.add_argument("--storage-name", default=DEFAULT_STORAGE)
    parser.add_argument(
        "--provider-account-ref",
        default=os.environ.get("ASTRID_RUNPOD_ACCOUNT_REF"),
        help="Stable non-secret account/profile label shared with Runtime placement (or set ASTRID_RUNPOD_ACCOUNT_REF).",
    )
    parser.add_argument("--container-disk-gb", type=int, default=DEFAULT_CONTAINER_DISK_GB)
    parser.add_argument("--image", dest="worker_image", default=DEFAULT_WORKER_IMAGE)
    parser.add_argument(
        "--allowed-cuda-versions",
        default=",".join(DEFAULT_ALLOWED_CUDA_VERSIONS),
        help="Provider host CUDA versions compatible with the selected release.",
    )
    parser.add_argument(
        "--template-id",
        default=None,
        help="Optional provider template; defaults to none so the selected image is authoritative.",
    )
    parser.add_argument("--release-root", default=DEFAULT_RELEASE_ROOT)
    parser.add_argument("--min-memory-gb", type=int, default=32)
    parser.add_argument(
        "--pod-id",
        default=None,
        help="Attach to this exact account-visible pod instead of allocating a new one.",
    )
    parser.add_argument(
        "--resume",
        action="store_true",
        help="Reconcile the existing secret-free custody record; only issue a create when its journal proves no create is in flight.",
    )
    parser.add_argument(
        "--allow-pod-release",
        action="store_true",
        help="Record exclusive release authority for an exact attached pod.",
    )
    parser.add_argument("--poll-seconds", type=int, default=DEFAULT_POLL_SECONDS)
    parser.add_argument(
        "--capacity-window-seconds",
        type=int,
        default=DEFAULT_CAPACITY_WINDOW_SECONDS,
        help="Bound each launch_when_available call; the outer waiter starts another window.",
    )
    parser.add_argument(
        "--max-wait-seconds",
        type=int,
        default=DEFAULT_MAX_WAIT_SECONDS,
        help="Overall capacity-wait limit (default: 43200 / 12 hours); 0 waits indefinitely.",
    )
    parser.add_argument("--ready-timeout-seconds", type=int, default=DEFAULT_READY_TIMEOUT_SECONDS)
    parser.add_argument("--name-prefix", default="astrid-claim-5090-backup")
    parser.add_argument(
        "--handle-path",
        default=str(DEFAULT_HANDLE_PATH),
        help=f"Secret-free custody marker/final handle (default: {DEFAULT_HANDLE_PATH}).",
    )
    parser.add_argument(
        "--lifecycle-policy",
        choices=(DEFAULT_LIFECYCLE_POLICY,),
        default=DEFAULT_LIFECYCLE_POLICY,
        help="Claim postcondition; leave_running never terminates an allocated pod.",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    for name in (
        "container_disk_gb",
        "min_memory_gb",
        "poll_seconds",
        "capacity_window_seconds",
        "ready_timeout_seconds",
    ):
        if getattr(args, name) <= 0:
            raise SystemExit(f"--{name.replace('_', '-')} must be positive")
    if args.max_wait_seconds < 0:
        raise SystemExit("--max-wait-seconds must be zero or positive")
    if args.pod_id is not None and not args.pod_id.strip():
        raise SystemExit("--pod-id must be nonempty when supplied")
    if not any(part.strip() for part in args.allowed_cuda_versions.split(",")):
        raise SystemExit("--allowed-cuda-versions must contain at least one version")

    try:
        result = asyncio.run(_claim(args))
    except KeyboardInterrupt:
        _log("interrupted; inspect the claim custody handle before any retry")
        return 130
    print(json.dumps(result, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
