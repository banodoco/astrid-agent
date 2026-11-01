"""Shared implementation for all runpod.* action subcommands.

All five action run.py files (provision, session, exec, teardown, pull) are
thin wrappers that call ``guard_canonical_entrypoint`` with their pack-action
name and then re-export everything from this module.

Do NOT import from individual action run.py modules in production code —
import from this module or from the specific action run.py for test-compat.
"""

from __future__ import annotations

import argparse
import asyncio
import inspect
import json
import os
import re
import shutil
import subprocess
import tempfile
import time
import uuid
from collections.abc import Mapping
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Literal, cast

from astrid.core.cli_choices import add_choice_arg
from astrid.core.contracts.errors import AstridError
from astrid.core.util.log_and_swallow import log_and_swallow
from astrid.core.util.time import utc_now_milliseconds

# ---------------------------------------------------------------------------
# __all__ — declare every name this module exports via ``import *``.
# Includes private (_-prefixed) names because tests import them directly
# from astrid.packs.runpod.actions.provision.run (the canonical action wrapper)
# and that module re-exports from this shared common module.
# ---------------------------------------------------------------------------

__all__ = [
    # stdlib modules exposed for monkeypatching in tests
    "subprocess",
    # constants
    "_PRICING_TABLE",
    # private helpers
    "_get_hourly_rate",
    "_write_json",
    "_cost_entry",
    "_cost_amount",
    "_write_cost_sidecar",
    "_artifact_records",
    "_copy_detached_artifact_root",
    "_termination_status",
    "_detached_exec_result",
    "_host_hf_token_env_vars",
    "_resolve_compute_profile",
    "_storage_required",
    "_preflight_storage",
    "_build_pod_handle",
    "_build_provisional_pod_handle",
    "_terminate_pod_id",
    "_ssh_target_from_handle",
    "_build_scp_pull_command",
    "_load_handle_and_config",
    # public commands
    "cmd_provision",
    "cmd_exec",
    "cmd_pull",
    "cmd_teardown",
    "cmd_session",
    "build_parser",
    "main",
]

# ---------------------------------------------------------------------------
# Pinned GPU pricing fallback (USD/hr).
# Used when the RunPod pricing API is unreachable.
# ---------------------------------------------------------------------------

_PRICING_TABLE: dict[str, float] = {
    "NVIDIA GeForce RTX 4090": 0.34,
    "NVIDIA RTX 4090": 0.34,
    "NVIDIA A100-SXM4-80GB": 1.89,
    "NVIDIA A100 80GB SXM4": 1.89,
    "NVIDIA A40": 0.79,
    "NVIDIA A6000": 0.79,
    "NVIDIA RTX 6000 Ada": 0.79,
    "NVIDIA L40S": 1.14,
    "NVIDIA L40": 1.14,
    "NVIDIA H100-SXM-80GB": 2.99,
    "NVIDIA H100 80GB HBM3": 2.99,
}


def _get_hourly_rate(api_key: str, gpu_type) -> float:
    """Resolve the hourly rate for *gpu_type*.

    Accepts str or list[str]; for a list, uses the first element as the rate estimate
    (auto-fallback's actual selection only known post-launch).
    Tries the RunPod GPU listing first; falls back to the pinned table.
    """
    if isinstance(gpu_type, (list, tuple)):
        gpu_type = gpu_type[0] if gpu_type else ""
    try:
        from runpod_lifecycle.api import find_gpu_type

        gpu_info = find_gpu_type(gpu_type, api_key)
        if gpu_info:
            for field in ("securePrice", "price", "costPerHr", "minPrice"):
                rate = gpu_info.get(field)
                if rate is not None:
                    return float(rate)
    except Exception as exc:  # noqa: BLE001
        log_and_swallow(exc, context="runpod.exec.resolve_gpu_rate")

    # Fallback to pinned table.
    rate = _PRICING_TABLE.get(gpu_type)
    if rate is not None:
        return rate

    # Last-resort: partial match on common prefixes.
    for known_name, known_rate in _PRICING_TABLE.items():
        if gpu_type.lower() in known_name.lower() or known_name.lower() in gpu_type.lower():
            return known_rate

    return 0.50  # Sensible default for unknown GPUs.


def _write_json(path: Path, payload: dict[str, Any]) -> None:
    """Atomically replace *path* with an indented JSON document."""
    path.parent.mkdir(parents=True, exist_ok=True)
    data = (json.dumps(payload, indent=2, default=str) + "\n").encode("utf-8")
    fd, temporary = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
    temporary_path = Path(temporary)
    try:
        with os.fdopen(fd, "wb") as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary_path, path)
    finally:
        temporary_path.unlink(missing_ok=True)


def _create_json_exclusive(path: Path, payload: dict[str, Any]) -> None:
    """Create one custody document without replacing an existing owner."""
    path.parent.mkdir(parents=True, exist_ok=True)
    data = (json.dumps(payload, indent=2, default=str) + "\n").encode("utf-8")
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(fd, "wb") as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
    except BaseException:
        path.unlink(missing_ok=True)
        raise


def _cost_entry(amount: float, source: str, basis: str) -> dict[str, Any]:
    """Build a cost sidecar dict matching the CostEntry shape."""
    return {
        "amount": round(amount, 6),
        "currency": "USD",
        "source": source,
        "basis": basis,
    }


def _cost_amount(duration_seconds: float, hourly_rate: float) -> float:
    """Compute cost from wallclock-seconds and hourly rate."""
    return duration_seconds * hourly_rate / 3600.0


def _write_cost_sidecar(produces_dir: Path, *, duration_seconds: float, hourly_rate: float, basis_prefix: str) -> None:
    _write_json(
        produces_dir / "cost.json",
        _cost_entry(
            _cost_amount(duration_seconds, hourly_rate),
            "runpod",
            f"{basis_prefix}: {duration_seconds:.1f}s * ${hourly_rate}/hr",
        ),
    )


def _artifact_records(root: Path) -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []
    if not root.is_dir():
        return records
    for path in sorted(p for p in root.rglob("*") if p.is_file()):
        records.append(
            {
                "path": str(path.relative_to(root)),
                "size_bytes": path.stat().st_size,
            }
        )
    return records


def _copy_detached_artifact_root(artifact_root: str | None, produces_dir: Path) -> tuple[str | None, list[dict[str, Any]]]:
    """Copy only the substrate-returned artifact root into produces/artifact_dir."""
    artifact_dst = produces_dir / "artifact_dir"
    if artifact_dst.exists():
        shutil.rmtree(artifact_dst)
    artifact_dst.mkdir(parents=True, exist_ok=True)

    if not artifact_root:
        return str(artifact_dst), []

    artifact_src = Path(artifact_root)
    if not artifact_src.is_dir():
        return str(artifact_dst), []

    for item in artifact_src.iterdir():
        dst = artifact_dst / item.name
        if item.is_dir():
            shutil.copytree(item, dst)
        else:
            shutil.copy2(item, dst)
    return str(artifact_dst), _artifact_records(artifact_dst)


def _termination_status(returncode: int, terminated: bool) -> str:
    if terminated:
        return "terminated"
    if returncode == 0:
        return "completed"
    return "remote_failed"


def _detached_exec_result(
    result: Any,
    *,
    produces_dir: Path,
    pod_id: str,
    name_prefix: str,
    remote_root: str,
    upload_mode: str,
    timeout: int,
) -> dict[str, Any]:
    artifact_root = str(result.artifact_root) if result.artifact_root else None
    artifact_dir, artifact_paths = _copy_detached_artifact_root(artifact_root, produces_dir)
    payload: dict[str, Any] = {
        "returncode": int(result.returncode),
        "stdout": str(result.stdout or ""),
        "stderr": str(result.stderr or ""),
        "terminated": bool(result.terminated),
        "termination_status": _termination_status(int(result.returncode), bool(result.terminated)),
        "artifact_root": artifact_root,
        "artifact_dir": artifact_dir,
        "artifact_paths": artifact_paths,
        "breach_log": result.breach_log,
        "breadcrumbs": {
            "pod_id": pod_id,
            "name_prefix": name_prefix,
            "remote_root": remote_root,
            "upload_mode": upload_mode,
            "timeout": timeout,
        },
    }
    if payload["returncode"] != 0:
        diagnostics_dir = produces_dir / "diagnostics"
        diagnostics_dir.mkdir(parents=True, exist_ok=True)
        diagnostics_path = diagnostics_dir / "remote_exit.json"
        _write_json(
            diagnostics_path,
            {
                "returncode": payload["returncode"],
                "termination_status": payload["termination_status"],
                "stdout_tail": payload["stdout"][-4000:],
                "stderr_tail": payload["stderr"][-4000:],
                "artifact_dir": artifact_dir,
                "artifact_paths": artifact_paths,
                "breadcrumbs": payload["breadcrumbs"],
            },
        )
        payload["diagnostics_path"] = str(diagnostics_path)
    return payload


def _host_hf_token_env_vars(profile: Mapping[str, Any] | None = None) -> dict[str, str]:
    """Return pod credential env vars sourced from the host, never literals from disk."""
    from astrid.core.compute_profile import credential_env_ref
    from astrid.core.util.credentials_scope import CredentialsScope

    token_ref = credential_env_ref(profile or {}, "hf_token", "HF_TOKEN")
    if not token_ref:
        return {}
    try:
        token = CredentialsScope.get_local("huggingface", env_var=token_ref)
    except AstridError:
        return {}
    return {token_ref: token} if token else {}


_RUNPOD_COMPUTE_DEFAULTS: dict[str, Any] = {
    "gpu_type": "NVIDIA GeForce RTX 4090",
    "allowed_cuda_versions": None,
    "name_prefix": "pod",
    "image": "runpod/pytorch:2.4.0-py3.11-cuda12.4.1-devel-ubuntu22.04",
    "container_disk_gb": 200,
    "volume_in_gb": 0,
    "volume_mount_path": "/workspace",
    "max_runtime_seconds": 7200,
    "remote_root": "/workspace",
    "timeout": 3600,
    "upload_mode": "sftp_walk",
    "ports": "8888/http,22/tcp",
    "credentials": {
        "runpod_api_key": "RUNPOD_API_KEY",
        "hf_token": "HF_TOKEN",
    },
}

_DEFAULT_RUNPOD_IMAGE = str(_RUNPOD_COMPUTE_DEFAULTS["image"])
_CUDA_13_GPU_MARKER = "5090"


def _is_cuda_13_route(gpu_type: str | list[str] | tuple[str, ...]) -> bool:
    candidates = (gpu_type,) if isinstance(gpu_type, str) else tuple(gpu_type)
    return any(_CUDA_13_GPU_MARKER in candidate for candidate in candidates)


def _launch_contract(
    *,
    gpu_type: str | list[str] | tuple[str, ...],
    image: Any,
    allowed_cuda_versions: Any,
) -> tuple[str, tuple[str, ...], str | None]:
    """Return an explicit image/CUDA/template contract for one launch."""
    normalized_image = str(image or "").strip()
    versions = tuple(str(version).strip() for version in (allowed_cuda_versions or ()) if str(version).strip())
    if not _is_cuda_13_route(gpu_type):
        return normalized_image, versions, None
    if not normalized_image or normalized_image == _DEFAULT_RUNPOD_IMAGE:
        raise AstridError(
            "RTX 5090 provisioning requires an explicit CUDA-13-compatible worker image",
            recovery_command="supply the validated CUDA 13 image together with --allowed-cuda-versions 13.0",
        )
    if versions != ("13.0",):
        raise AstridError(
            "RTX 5090 provisioning requires the explicit allowed CUDA version 13.0",
            recovery_command="set --allowed-cuda-versions 13.0 for the validated 5090 route",
        )
    # An empty template ID is intentional: runpod-lifecycle forwards only
    # truthy template IDs, so the explicit image cannot inherit its legacy
    # runpod-torch-v240 default.
    return normalized_image, versions, ""


def _new_runpod_config(
    config_type: Any,
    *,
    datacenter_id: str | None,
    template_id: str | None,
    **kwargs: Any,
) -> Any:
    """Construct the installed lifecycle config without dropping constraints."""
    parameters = inspect.signature(config_type).parameters.values()
    accepts_extra = any(parameter.kind == inspect.Parameter.VAR_KEYWORD for parameter in parameters)
    parameter_names = {parameter.name for parameter in parameters}
    if datacenter_id:
        if "datacenter_id" not in parameter_names and not accepts_extra:
            raise AstridError(
                "the installed runpod-lifecycle does not support datacenter-constrained allocation",
                recovery_command="install a lifecycle revision whose RunPodConfig accepts datacenter_id before using this profile",
            )
        kwargs["datacenter_id"] = datacenter_id
    if template_id is not None:
        kwargs["template_id"] = template_id
    return config_type(**kwargs)


def _resolve_compute_profile(args: argparse.Namespace, produces_dir: Path) -> dict[str, Any]:
    """Resolve RunPod settings and persist a secret-free execution snapshot.

    Existing ``RUNPOD_*`` environment settings remain supported as legacy
    executor defaults.  A user profile selected through
    ``ASTRID_COMPUTE_PROFILE`` (or ``--compute-profile``/``default.json``)
    takes precedence over those settings, while explicit command inputs win
    over the profile.
    """

    from astrid.core.compute_profile import resolve_compute_profile, write_resolved_snapshot

    env = os.environ
    explicit: dict[str, Any] = {}
    profile_fields = (
        "gpu_type", "allowed_cuda_versions", "storage_name", "max_runtime_seconds", "name_prefix", "image",
        "container_disk_gb", "datacenter_id", "ports", "local_root", "remote_root",
        "volume_in_gb", "volume_mount_path",
        "remote_script", "timeout", "upload_mode", "excludes", "require_storage",
    )
    for field in profile_fields:
        value = getattr(args, field, None)
        # Some callers (and legacy tests) pass lightweight mocked namespaces.
        # Do not let mock sentinels become profile values and later break the
        # JSON-safe resolved snapshot. argparse supplies these two fields as
        # int/string respectively in the real executor path.
        if field == "volume_in_gb" and (isinstance(value, bool) or not isinstance(value, int)):
            continue
        if field == "volume_mount_path" and not isinstance(value, str):
            continue
        if field == "allowed_cuda_versions" and not isinstance(value, (str, list, tuple)):
            continue
        if field == "allowed_cuda_versions" and isinstance(value, str) and not value.strip():
            raise AstridError("--allowed-cuda-versions must contain at least one version")
        if field == "allowed_cuda_versions" and isinstance(value, str):
            value = [part.strip() for part in value.split(",") if part.strip()]
            if not value:
                raise AstridError("--allowed-cuda-versions must contain at least one version")
        if field == "allowed_cuda_versions" and isinstance(value, (list, tuple)) and not value:
            raise AstridError("allowed_cuda_versions must contain at least one version")
        if value is not None and not (field == "require_storage" and value is False):
            explicit[field] = value
    profile_arg = getattr(args, "compute_profile", None)
    if isinstance(profile_arg, str) and profile_arg.strip():
        explicit["compute_profile"] = profile_arg.strip()

    # Preserve the long-standing RUNPOD_* knobs, but classify them as the
    # lowest executor-default tier so they cannot override a profile.
    defaults = dict(_RUNPOD_COMPUTE_DEFAULTS)
    defaults["credentials"] = dict(_RUNPOD_COMPUTE_DEFAULTS["credentials"])
    env_fields = {
        "gpu_type": "RUNPOD_GPU_TYPE",
        "allowed_cuda_versions": "RUNPOD_ALLOWED_CUDA_VERSIONS",
        "storage_name": "RUNPOD_STORAGE_NAME",
        "name_prefix": "RUNPOD_NAME_PREFIX",
        "image": "RUNPOD_WORKER_IMAGE",
        "datacenter_id": "RUNPOD_DATACENTER_ID",
        "ports": "RUNPOD_PORTS",
        "remote_root": "RUNPOD_REMOTE_ROOT",
        "remote_script": "RUNPOD_REMOTE_SCRIPT",
        "volume_mount_path": "RUNPOD_VOLUME_MOUNT_PATH",
        "upload_mode": "RUNPOD_UPLOAD_MODE",
        "excludes": "RUNPOD_EXCLUDES",
    }
    for field, env_name in env_fields.items():
        if env.get(env_name):
            defaults[field] = env[env_name]
    if "RUNPOD_ALLOWED_CUDA_VERSIONS" in env and not env.get("RUNPOD_ALLOWED_CUDA_VERSIONS", "").strip():
        raise AstridError("RUNPOD_ALLOWED_CUDA_VERSIONS must contain at least one version")
    for field, env_name in (
        ("max_runtime_seconds", "RUNPOD_MAX_RUNTIME_SECONDS"),
        ("container_disk_gb", "RUNPOD_CONTAINER_DISK_GB"),
        ("volume_in_gb", "RUNPOD_VOLUME_IN_GB"),
        ("timeout", "RUNPOD_TIMEOUT"),
    ):
        if env.get(env_name):
            try:
                defaults[field] = int(env[env_name])
            except ValueError as exc:
                raise AstridError(
                    f"{env_name} must be an integer",
                    recovery_command=f"set {env_name} to a valid integer and retry",
                ) from exc
    if env.get("RUNPOD_REQUIRE_STORAGE", "").lower() in {"1", "true", "yes", "on"}:
        defaults["require_storage"] = True

    resolved = resolve_compute_profile(
        explicit=explicit,
        env=env,
        profile_id=profile_arg if isinstance(profile_arg, str) and profile_arg.strip() else None,
        executor_defaults=defaults,
    )
    write_resolved_snapshot(produces_dir, resolved)
    return resolved


def _storage_required(args: argparse.Namespace) -> bool:
    value = os.environ.get("RUNPOD_REQUIRE_STORAGE", "")
    env_required = value.lower() in {"1", "true", "yes", "on"}
    return bool(getattr(args, "require_storage", False) or env_required)


def _preflight_storage(
    storage_name: str | None,
    *,
    required: bool,
    context: str,
    api_key: str,
) -> int:
    if not required and not storage_name:
        return 0

    from astrid.core.integrations.runpod.storage import require_existing_storage

    try:
        asyncio.run(require_existing_storage(storage_name, context=context, api_key=api_key))
    except Exception as exc:
        raise AstridError(
            str(exc),
            recovery_command="verify the storage name exists in your RunPod account and is accessible, then retry",
        ) from exc
    return 0


def _build_pod_handle(
    *,
    pod: Any,
    ssh: dict[str, Any],
    name_prefix: str,
    terminate_at: str,
    gpu_type: Any,
    hourly_rate: float,
    provisioned_at: str,
    datacenter_id: str | None,
    image: str,
    container_disk_gb: int,
    volume_in_gb: int,
    storage_name: str | None,
    network_volume_id: Any,
    ports: str | None,
    api_key_ref: str = "RUNPOD_API_KEY",
    volume_mount_path: str = "/workspace",
    allowed_cuda_versions: Any = (),
    template_id: str | None = None,
    operation_id: str | None = None,
    request_name: str | None = None,
) -> dict[str, Any]:
    handle = {
        "pod_id": pod.id,
        "ssh": f"root@{ssh['ip']} -p {ssh['port']}",
        "name": pod.name,
        "name_prefix": name_prefix,
        "terminate_at": terminate_at,
        "gpu_type": gpu_type,
        "hourly_rate": hourly_rate,
        "provisioned_at": provisioned_at,
        "config_snapshot": {
            "api_key_ref": api_key_ref,
            "datacenter_id": datacenter_id,
            "image": image,
            "container_disk_in_gb": container_disk_gb,
            "volume_in_gb": volume_in_gb,
            "volume_mount_path": volume_mount_path,
            "storage_name": storage_name,
            "network_volume_id": network_volume_id,
            "ports": ports or "8888/http,22/tcp",
            "allowed_cuda_versions": list(allowed_cuda_versions or ()),
            "template_id": template_id,
        },
    }
    if operation_id:
        handle["operation_id"] = operation_id
    if request_name:
        handle["request_name"] = request_name
    return handle


def _build_provisional_pod_handle(
    *,
    pod: Any,
    name_prefix: str,
    terminate_at: str,
    provisioned_at: str,
    gpu_type: Any,
    hourly_rate: float,
    datacenter_id: str | None,
    image: str | None,
    container_disk_gb: int,
    volume_in_gb: int,
    storage_name: str | None,
    network_volume_id: Any,
    ports: str | None,
    api_key_ref: str,
    volume_mount_path: str,
    allowed_cuda_versions: Any,
    template_id: str | None,
    operation_id: str,
    request_name: str,
) -> dict[str, Any]:
    """Build a reloadable, secret-safe pre-readiness ownership breadcrumb."""
    return {
        "schema_version": "astrid.runpod.provisional-handle.v1",
        "pod_id": str(pod.id),
        "name": str(getattr(pod, "name", name_prefix) or name_prefix),
        "name_prefix": name_prefix,
        "terminate_at": terminate_at,
        "gpu_type": gpu_type,
        "hourly_rate": hourly_rate,
        "provisioned_at": provisioned_at,
        "operation_id": operation_id,
        "request_name": request_name,
        "config_snapshot": {
            "api_key_ref": api_key_ref or "RUNPOD_API_KEY",
            "datacenter_id": datacenter_id,
            "image": image,
            "container_disk_in_gb": container_disk_gb,
            "volume_in_gb": volume_in_gb,
            "volume_mount_path": volume_mount_path,
            "storage_name": storage_name,
            "network_volume_id": network_volume_id,
            "ports": ports or "8888/http,22/tcp",
            "allowed_cuda_versions": list(allowed_cuda_versions or ()),
            "template_id": template_id,
        },
        "state": "provisioning",
    }


def _mark_cleanup_pending(
    handle_path: Path,
    handle: dict[str, Any] | None,
    *,
    reason: str,
    phase: str,
) -> None:
    """Persist that a provider handle still needs cleanup/reconciliation."""
    if handle is None:
        return
    handle.update(
        {
            "state": "cleanup_pending",
            "cleanup_pending": True,
            "cleanup_phase": phase,
            "cleanup_error": reason,
            "cleanup_pending_at": utc_now_milliseconds(),
        }
    )
    operation_id = handle.get("operation_id")
    if operation_id:
        _replace_custody_record(handle_path, handle, operation_id=str(operation_id))
    else:
        _write_json(handle_path, handle)


def _existing_custody_error(handle_path: Path) -> AstridError:
    try:
        previous = json.loads(handle_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        return AstridError(
            f"RunPod custody record at {handle_path} already exists and cannot be read safely: {exc}",
            recovery_command="inspect and reconcile the existing custody record before retrying",
        )
    if previous.get("state") in {"allocation_pending", "allocation_unknown"}:
        return AstridError(
            f"RunPod allocation request {previous.get('request_name')!r} requires reconciliation before retry",
            recovery_command="reconcile this request name with the provider account; use the exact pod ID for teardown if one exists",
            code="allocation_unknown",
            state_snapshot={
                "handle_path": str(handle_path),
                "operation_id": previous.get("operation_id"),
                "request_name": previous.get("request_name"),
                "reconciliation_required": True,
            },
        )
    if previous.get("pod_id"):
        return AstridError(
            f"RunPod handle at {handle_path} already identifies pod {previous['pod_id']}",
            recovery_command="teardown or move the existing handle before starting another allocation in this output directory",
            state_snapshot={
                "handle_path": str(handle_path),
                "operation_id": previous.get("operation_id"),
                "pod_id": previous["pod_id"],
            },
        )
    return AstridError(
        f"RunPod custody record already exists at {handle_path}",
        recovery_command="inspect and reconcile the existing record before retrying",
        state_snapshot={"handle_path": str(handle_path)},
    )


def _replace_custody_record(
    handle_path: Path,
    payload: dict[str, Any],
    *,
    operation_id: str,
) -> None:
    """Atomically transition a custody record owned by *operation_id*."""
    try:
        current = json.loads(handle_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise AstridError(
            f"cannot verify RunPod custody at {handle_path}: {exc}",
            recovery_command="reconcile the existing custody record before continuing",
        ) from exc
    if current.get("operation_id") != operation_id:
        raise AstridError(
            f"RunPod custody at {handle_path} belongs to another operation",
            recovery_command="do not overwrite the record; reconcile its operation_id before retrying",
            state_snapshot={
                "handle_path": str(handle_path),
                "expected_operation_id": operation_id,
                "actual_operation_id": current.get("operation_id"),
            },
        )
    if payload.get("operation_id") != operation_id:
        raise AstridError("replacement RunPod custody record changed operation identity")
    _write_json(handle_path, payload)


def _remove_custody_record(handle_path: Path, *, operation_id: str) -> None:
    """Remove only the custody record owned by *operation_id*."""
    current = json.loads(handle_path.read_text(encoding="utf-8"))
    if current.get("operation_id") != operation_id:
        raise AstridError(
            f"refusing to remove RunPod custody for another operation at {handle_path}",
            state_snapshot={
                "handle_path": str(handle_path),
                "expected_operation_id": operation_id,
                "actual_operation_id": current.get("operation_id"),
            },
        )
    handle_path.unlink()


def _start_allocation_attempt(
    handle_path: Path, *, name_prefix: str, api_key_ref: str,
    gpu_type: str | list[str], storage_name: str | None,
) -> dict[str, Any]:
    """Claim exclusive custody before a potentially billable create."""
    operation_id = uuid.uuid4().hex
    request_name = f"{name_prefix}-{operation_id[:12]}"
    attempt = {
        "schema_version": "astrid.runpod.allocation-attempt.v1",
        "state": "allocation_pending",
        "operation_id": operation_id,
        "request_name": request_name,
        "api_key_ref": api_key_ref,
        "gpu_type": gpu_type,
        "storage_name": storage_name,
        "requested_at": utc_now_milliseconds(),
        "reconciliation_required": True,
    }
    try:
        _create_json_exclusive(handle_path, attempt)
    except FileExistsError as exc:
        raise _existing_custody_error(handle_path) from exc
    return attempt


def _record_allocation_unknown(
    handle_path: Path, attempt: dict[str, Any], exc: Any,
) -> AstridError:
    attempt.update({
        "state": "allocation_unknown",
        "gpu_type_attempted": exc.gpu_type,
        "ram_tier_attempted": exc.ram_tier,
        "storage_name_attempted": exc.storage_name,
        "storage_volume_id_attempted": exc.storage_volume_id,
        "reconciliation_required": True,
        "unresolved_at": utc_now_milliseconds(),
    })
    _replace_custody_record(
        handle_path,
        attempt,
        operation_id=str(attempt["operation_id"]),
    )
    return AstridError(
        str(exc),
        recovery_command="reconcile the request name with the provider account; terminate by exact pod ID if allocated, then clear the marker before retry",
        code="allocation_unknown",
        state_snapshot={
            "handle_path": str(handle_path),
            "operation_id": attempt["operation_id"],
            "request_name": exc.request_name,
            "allocation_unknown": True,
            "reconciliation_required": True,
        },
    )


def _record_nonretryable_launch_failure(
    handle_path: Path, attempt: dict[str, Any], exc: Any,
) -> AstridError:
    """Retain custody when a fatal launch error may have created a known pod."""
    pod_id = getattr(exc, "pod_id", None)
    failed_at = utc_now_milliseconds()
    attempt.update({
        "state": "allocated_unverified" if pod_id else "allocation_unknown",
        "failure_type": type(exc).__name__,
        "failed_at": failed_at,
        "reconciliation_required": True,
    })
    if pod_id:
        attempt["pod_id"] = str(pod_id)
    else:
        attempt["unresolved_at"] = failed_at
    _replace_custody_record(
        handle_path,
        attempt,
        operation_id=str(attempt["operation_id"]),
    )
    return AstridError(
        str(exc),
        recovery_command=(
            f"reconcile known pod {pod_id} by exact ID and the recorded request before retrying"
            if pod_id
            else "reconcile the recorded request with the provider account before retrying"
        ),
        code="allocation_known" if pod_id else "allocation_unknown",
        state_snapshot={
            "handle_path": str(handle_path),
            "operation_id": attempt["operation_id"],
            "request_name": attempt["request_name"],
            "pod_id": str(pod_id) if pod_id else None,
            "reconciliation_required": True,
        },
    )


async def _terminate_pod_id(pod_id: str, config: Any, *, name: str | None = None) -> bool:
    from runpod_lifecycle import get_pod

    try:
        pod = await get_pod(pod_id, config, name=name)
        await pod.terminate()
        return True
    except Exception as exc:
        msg = str(exc).lower()
        if "not found" in msg or "404" in msg or "does not exist" in msg:
            return True
        log_and_swallow(exc, context="runpod.exec.session_teardown")
        return False


def _ssh_target_from_handle(handle: dict[str, Any]) -> tuple[str, str]:
    """Return ``(user_host, port)`` from the persisted RunPod ssh field."""
    ssh = str(handle.get("ssh") or "")
    match = re.match(r"(\S+)\s+-p\s+(\d+)", ssh)
    if not match:
        raise AstridError(
            f"pod_handle ssh field is missing or invalid: {ssh!r}",
            recovery_command="verify the pod_handle.json has a valid ssh field (e.g. 'root@1.2.3.4 -p 22') and the pod is still running",
        )
    return match.group(1), match.group(2)


def _build_scp_pull_command(
    handle: dict[str, Any],
    *,
    remote_path: str,
    local_dir: Path,
    ssh_key: str | None = None,
) -> list[str]:
    """Build the SCP command used by ``runpod.run pull``."""
    target, port = _ssh_target_from_handle(handle)
    cmd = [
        "scp",
        "-r",
        "-P",
        port,
        "-o",
        "StrictHostKeyChecking=no",
        "-o",
        "IdentitiesOnly=yes",
    ]
    if ssh_key:
        cmd.extend(["-i", ssh_key])
    cmd.extend([f"{target}:{remote_path}", str(local_dir)])
    return cmd


# ---------------------------------------------------------------------------
# Shared helper: load pod_handle and rebuild config
# ---------------------------------------------------------------------------


def _load_handle_and_config(handle_path: Path) -> tuple[dict[str, Any], Any]:
    """Return ``(handle_dict, RunPodConfig)`` from a pod_handle.json path."""
    from runpod_lifecycle import RunPodConfig

    handle = json.loads(handle_path.read_text(encoding="utf-8"))
    if handle.get("state") in {"allocation_pending", "allocation_unknown"}:
        raise AstridError(
            f"allocation request {handle.get('request_name')!r} has no confirmed pod ID",
            recovery_command="reconcile the request name with the provider account before any exact-ID teardown",
            code="allocation_unknown",
            state_snapshot={"handle_path": str(handle_path), "request_name": handle.get("request_name"), "reconciliation_required": True},
        )
    if str(handle.get("schema_version", "")).startswith(
        "astrid.runpod.allocation-attempt."
    ):
        raise AstridError(
            f"RunPod acquisition journal is not an executable pod handle (state={handle.get('state')!r})",
            recovery_command=(
                "resume the original claim command to reconcile its exact provider target, "
                "then pass the resulting claimed handle to RunPod exec/pull"
            ),
            code="allocation_journal_not_claimed",
            state_snapshot={
                "handle_path": str(handle_path),
                "operation_id": handle.get("operation_id"),
                "state": handle.get("state"),
                "reconciliation_required": True,
            },
        )
    if handle.get("schema_version") == "astrid.runpod.claim.v1":
        # Claim waiters deliberately emit an operator/lifecycle handle rather
        # than pretending that a reused pod was provisioned by Astrid.  The
        # canonical executor can still reattach safely: resolve the exact pod
        # by id, verify its live provider state, and enrich only the in-memory
        # view with the fields the provision-handle contract requires.
        pod_id = str(handle.get("pod_id") or "").strip()
        if not pod_id:
            raise AstridError(
                "claim handle is missing pod_id",
                recovery_command="rerun the claim waiter to produce a valid lifecycle handle",
            )
        api_key_ref = str(handle.get("api_key_ref") or "RUNPOD_API_KEY")
        from astrid.core.util.credentials_scope import CredentialsScope

        credential = CredentialsScope.resolve_local("runpod", env_var=api_key_ref)
        api_key = credential.value
        from astrid.packs.runpod.worker_preparation import (
            RunPodTargetContract,
            TargetObservationError,
            observe_target,
        )

        network_volume_id = str(handle.get("network_volume_id") or "")
        network_volume_name = str(handle.get("network_volume_name") or "")
        if (
            not network_volume_name
            and str(handle.get("storage_name") or "") != network_volume_id
        ):
            network_volume_name = str(handle.get("storage_name") or "")

        try:
            observe_target(
                api_key,
                RunPodTargetContract(
                    pod_id=pod_id,
                    expected_pod_name=str(handle.get("name") or "") or None,
                    gpu_type=str(handle.get("gpu_type") or ""),
                    worker_image=str(handle.get("worker_image") or ""),
                    network_volume_id=network_volume_id,
                    network_volume_name=network_volume_name or None,
                    network_volume_size_gb=int(handle.get("network_volume_size_gb") or 0),
                ),
            )
        except (TargetObservationError, TypeError, ValueError) as exc:
            raise AstridError(
                f"claimed RunPod pod {pod_id} does not match its recorded provider identity: {exc}",
                recovery_command="verify the provider account and reconcile pod/image/GPU/network-volume identity before continuing",
                code="provider_target_unverified",
                state_snapshot={
                    "pod_id": pod_id,
                    "handle_path": str(handle_path),
                    "reconciliation_required": True,
                },
            ) from exc
        from runpod_lifecycle.api import get_pod_status

        status = get_pod_status(pod_id, api_key)
        if not isinstance(status, dict):
            raise AstridError(
                f"claimed RunPod pod {pod_id} was not found under the configured account",
                recovery_command="verify the claim handle and RunPod account, then retry",
            )
        if status.get("desired_status") not in {"RUNNING", "PROVISIONING"}:
            raise AstridError(
                f"claimed RunPod pod {pod_id} is not attachable (status={status.get('desired_status')!r})",
                recovery_command="wait for the claimed pod to return to RUNNING or reclaim a compatible pod",
            )
        hourly_rate = status.get("cost_per_hr")
        if not isinstance(hourly_rate, (int, float)) or isinstance(hourly_rate, bool):
            raise AstridError(
                f"RunPod did not return live pricing for claimed pod {pod_id}; refusing to fabricate a cost",
                recovery_command="recheck the provider status and retry once cost_per_hr is available",
            )
        snapshot = {
            "api_key_ref": api_key_ref,
            "datacenter_id": None,
            "image": handle.get("worker_image"),
            "template_id": handle.get("template_id"),
            "container_disk_in_gb": int(handle.get("container_disk_gb", 200)),
            "volume_in_gb": int(handle.get("network_volume_size_gb", 0)),
            "volume_mount_path": str(handle.get("volume_mount_path") or "/workspace"),
            "storage_name": handle.get("storage_name"),
            "network_volume_id": handle.get("network_volume_id"),
            "ports": "8888/http,22/tcp",
            "allowed_cuda_versions": list(handle.get("allowed_cuda_versions") or ()),
        }
        if not isinstance(snapshot["image"], str) or not snapshot["image"].strip():
            raise AstridError(
                f"claim handle for pod {pod_id} is missing worker_image",
                recovery_command="re-run the CUDA-qualified claim waiter",
            )
        handle = {
            **handle,
            "hourly_rate": float(hourly_rate),
            "config_snapshot": snapshot,
        }
    api_key_ref = handle["config_snapshot"]["api_key_ref"]
    from astrid.core.util.credentials_scope import CredentialsScope

    credential = CredentialsScope.resolve_local("runpod", env_var=api_key_ref)
    api_key = credential.value

    snap = handle["config_snapshot"]
    gpu_type = handle.get("gpu_type", "NVIDIA GeForce RTX 4090")
    template_id = snap.get("template_id")
    if _is_cuda_13_route(gpu_type) and template_id is None:
        template_id = ""
    config = _new_runpod_config(
        RunPodConfig,
        datacenter_id=snap.get("datacenter_id"),
        template_id=template_id,
        api_key=api_key,
        gpu_type=gpu_type,
        worker_image=snap.get("image") or "runpod/pytorch:2.4.0-py3.11-cuda12.4.1-devel-ubuntu22.04",
        container_disk_gb=snap.get("container_disk_in_gb", 200),
        disk_size_gb=snap.get("volume_in_gb", 0),
        volume_mount_path=snap.get("volume_mount_path", "/workspace"),
        allowed_cuda_versions=snap.get("allowed_cuda_versions") or (),
        storage_name=snap.get("storage_name") or snap.get("network_volume_id"),
        ssh_public_key=os.environ.get("RUNPOD_SSH_PUBLIC_KEY"),
        ssh_private_key=os.environ.get("RUNPOD_SSH_PRIVATE_KEY"),
        ssh_public_key_path=(
            os.environ.get("RUNPOD_SSH_PUBLIC_KEY_PATH")
            or os.environ.get("RUNPOD_SSH_IDENTITY_PUBLIC_PATH")
        ),
        ssh_private_key_path=(
            os.environ.get("RUNPOD_SSH_PRIVATE_KEY_PATH")
            or os.environ.get("RUNPOD_SSH_IDENTITY_PATH")
        ),
        env_vars=_host_hf_token_env_vars(),
    )
    return handle, config


# ---------------------------------------------------------------------------
# 1. provision
# ---------------------------------------------------------------------------


def cmd_provision(args: argparse.Namespace, produces_dir: Path) -> int:
    """Provision a RunPod GPU pod → pod_handle.json + cost.json."""
    from runpod_lifecycle import AllocationUnknown, LaunchFailure, RunPodConfig, launch

    from astrid.core.compute_profile import credential_env_ref

    resolved = _resolve_compute_profile(args, produces_dir)
    api_key_ref = credential_env_ref(resolved, "runpod_api_key", "RUNPOD_API_KEY") or "RUNPOD_API_KEY"

    from astrid.core.util.credentials_scope import CredentialsScope

    credential = CredentialsScope.resolve_local("runpod", env_var=api_key_ref)
    api_key = credential.value

    gpu_type = resolved["gpu_type"]
    if isinstance(gpu_type, str) and "," in gpu_type:
        gpu_type = [g.strip() for g in gpu_type.split(",") if g.strip()]
    allowed_cuda_versions = resolved.get("allowed_cuda_versions") or ()
    if isinstance(allowed_cuda_versions, str):
        allowed_cuda_versions = [v.strip() for v in allowed_cuda_versions.split(",") if v.strip()]
    name_prefix = resolved.get("name_prefix")
    if not isinstance(name_prefix, str):
        raise AstridError("RunPod name_prefix must be a string")
    image = resolved.get("image")
    container_disk_gb = int(resolved["container_disk_gb"])
    volume_in_gb = int(resolved.get("volume_in_gb", 0))
    volume_mount_path = str(resolved.get("volume_mount_path") or "/workspace")
    datacenter_id = resolved.get("datacenter_id")
    storage_name = resolved.get("storage_name")
    storage_required = bool(resolved.get("require_storage"))
    max_runtime = int(resolved["max_runtime_seconds"])
    ports = resolved.get("ports")

    image, allowed_cuda_versions, template_id = _launch_contract(
        gpu_type=gpu_type,
        image=image,
        allowed_cuda_versions=allowed_cuda_versions,
    )

    config = _new_runpod_config(
        RunPodConfig,
        datacenter_id=datacenter_id,
        template_id=template_id,
        api_key=api_key,
        gpu_type=gpu_type,
        worker_image=image,
        container_disk_gb=container_disk_gb,
        disk_size_gb=volume_in_gb,
        volume_mount_path=volume_mount_path,
        storage_name=storage_name,
        attach_only=bool(storage_name and volume_in_gb == 0),
        name_prefix=name_prefix,
        ports=ports,
        allowed_cuda_versions=allowed_cuda_versions,
        ssh_public_key=os.environ.get("RUNPOD_SSH_PUBLIC_KEY"),
        ssh_private_key=os.environ.get("RUNPOD_SSH_PRIVATE_KEY"),
        ssh_public_key_path=(
            os.environ.get("RUNPOD_SSH_PUBLIC_KEY_PATH")
            or os.environ.get("RUNPOD_SSH_IDENTITY_PUBLIC_PATH")
        ),
        ssh_private_key_path=(
            os.environ.get("RUNPOD_SSH_PRIVATE_KEY_PATH")
            or os.environ.get("RUNPOD_SSH_IDENTITY_PATH")
        ),
        env_vars=_host_hf_token_env_vars(resolved),
    )

    _preflight_storage(
        storage_name,
        required=storage_required,
        context="RunPod provision",
        api_key=api_key,
    )

    hourly_rate = _get_hourly_rate(api_key, gpu_type)
    provisioned_at = utc_now_milliseconds()
    t0 = time.monotonic()

    handle_path = produces_dir / "pod_handle.json"
    attempt = _start_allocation_attempt(
        handle_path, name_prefix=name_prefix, api_key_ref=api_key_ref,
        gpu_type=gpu_type, storage_name=storage_name,
    )
    operation_id = str(attempt["operation_id"])
    request_name = str(attempt["request_name"])
    pod_id: str | None = None
    handle: dict[str, Any] | None = None
    failure_phase = "allocation"

    async def _provision() -> tuple[Any, dict[str, Any]]:
        nonlocal pod_id, handle, failure_phase
        pod = await launch(config, name=request_name)
        # Provider allocation is already billable and must be recoverable even
        # when readiness or SSH discovery fails immediately afterwards.
        pod_id = str(pod.id)
        terminate_at_dt = datetime.now(timezone.utc).timestamp() + max_runtime
        terminate_at = datetime.fromtimestamp(terminate_at_dt, tz=timezone.utc).isoformat()
        handle = _build_provisional_pod_handle(
            pod=pod,
            name_prefix=name_prefix,
            terminate_at=terminate_at,
            provisioned_at=provisioned_at,
            gpu_type=gpu_type,
            hourly_rate=hourly_rate,
            datacenter_id=datacenter_id,
            image=image,
            container_disk_gb=container_disk_gb,
            volume_in_gb=volume_in_gb,
            storage_name=storage_name,
            network_volume_id=getattr(pod, "_storage_volume", None),
            ports=ports,
            api_key_ref=api_key_ref,
            volume_mount_path=volume_mount_path,
            allowed_cuda_versions=allowed_cuda_versions,
            template_id=template_id,
            operation_id=operation_id,
            request_name=request_name,
        )
        _replace_custody_record(handle_path, handle, operation_id=operation_id)

        failure_phase = "readiness"
        await pod.wait_ready(timeout=900)
        failure_phase = "ssh"
        ssh = await pod._ensure_ssh_details()
        return pod, ssh

    try:
        pod, ssh = asyncio.run(_provision())
    except AllocationUnknown as exc:
        raise _record_allocation_unknown(handle_path, attempt, exc) from exc
    except LaunchFailure as exc:
        if pod_id is None and not getattr(exc, "retryable", True):
            raise _record_nonretryable_launch_failure(handle_path, attempt, exc) from exc
        if pod_id is None:
            _remove_custody_record(handle_path, operation_id=operation_id)
        else:
            _mark_cleanup_pending(handle_path, handle, reason=str(exc), phase=failure_phase)
        raise AstridError(str(exc), recovery_command="check RunPod capacity and configuration before retry") from exc
    except Exception as exc:
        if pod_id is None:
            attempt.update({"state": "allocation_unknown", "unresolved_at": utc_now_milliseconds()})
            _replace_custody_record(handle_path, attempt, operation_id=operation_id)
        _mark_cleanup_pending(
            handle_path,
            handle,
            reason=str(exc),
            phase=failure_phase,
        )
        raise AstridError(
            str(exc),
            recovery_command=(
                f"retry teardown using the cleanup_pending handle at {handle_path}"
                if pod_id
                else "reconcile the request name with the provider account before retrying"
            ),
            state_snapshot={
                "pod_id": pod_id,
                "handle_path": handle_path,
                "cleanup_pending": bool(pod_id),
                "cleanup_phase": failure_phase if pod_id else None,
                "request_name": request_name if pod_id is None else None,
                "reconciliation_required": pod_id is None,
            },
            code="allocation_unknown" if pod_id is None else None,
        ) from exc

    terminate_at_dt = datetime.now(timezone.utc).timestamp() + max_runtime
    terminate_at = datetime.fromtimestamp(terminate_at_dt, tz=timezone.utc).isoformat()

    handle = _build_pod_handle(
        pod=pod,
        ssh=ssh,
        name_prefix=name_prefix,
        terminate_at=terminate_at,
        gpu_type=gpu_type,
        hourly_rate=hourly_rate,
        provisioned_at=provisioned_at,
        datacenter_id=datacenter_id,
        image=image,
        container_disk_gb=container_disk_gb,
        volume_in_gb=volume_in_gb,
        storage_name=storage_name,
        network_volume_id=pod._storage_volume,
        ports=ports,
        api_key_ref=api_key_ref or "RUNPOD_API_KEY",
        volume_mount_path=volume_mount_path,
        allowed_cuda_versions=allowed_cuda_versions,
        template_id=template_id,
        operation_id=operation_id,
        request_name=request_name,
    )

    _replace_custody_record(handle_path, handle, operation_id=operation_id)

    duration = time.monotonic() - t0
    _write_cost_sidecar(produces_dir, duration_seconds=duration, hourly_rate=hourly_rate, basis_prefix="provision")

    ssh_str = handle["ssh"]
    print(f"Provisioned pod {pod.id} ({gpu_type}) — ssh: {ssh_str}")
    return 0


# ---------------------------------------------------------------------------
# 2. exec
# ---------------------------------------------------------------------------


def cmd_exec(args: argparse.Namespace, produces_dir: Path) -> int:
    """Reattach to a provisioned pod, ship + run + download → exec_result.json + cost.json."""
    pod_handle_path = Path(args.pod_handle) if args.pod_handle else produces_dir / "pod_handle.json"
    if not pod_handle_path.is_file():
        raise AstridError(
            f"pod_handle.json not found at {pod_handle_path}",
            recovery_command="run 'astrid runpod provision' first to create a pod, then retry exec",
        )

    handle, config = _load_handle_and_config(pod_handle_path)
    hourly_rate = handle["hourly_rate"]

    remote_root = args.remote_root or "/workspace"
    remote_script = args.remote_script or (produces_dir.parent / "remote_script.sh")
    local_root = Path(args.local_root) if args.local_root else Path.cwd()
    timeout = args.timeout or 3600
    upload_mode: Literal["sftp_walk", "tarball"] = (
        cast(Literal["sftp_walk", "tarball"], args.upload_mode)
        if args.upload_mode in ("sftp_walk", "tarball")
        else "sftp_walk"
    )
    excludes = set(args.excludes.split(",")) if args.excludes else set()

    # The remote script can be either a path to a file or an inline command.
    if not isinstance(remote_script, str):
        remote_script = str(remote_script)
    if Path(remote_script).is_file():
        remote_script = Path(remote_script).read_text(encoding="utf-8").strip()

    async def _exec() -> dict[str, Any]:
        from runpod_lifecycle import get_pod, ship_and_run_detached

        pod = await get_pod(handle["pod_id"], config, name=handle.get("name"))

        result = await ship_and_run_detached(
            remote_script=remote_script,
            pod=pod,
            local_root=local_root,
            remote_root=remote_root,
            exclude=excludes,
            upload_mode=upload_mode,
            timeout=timeout,
            name_prefix=handle["name_prefix"],
            terminate_after_exec=False,
            poll_interval=30,
        )
        return _detached_exec_result(
            result,
            produces_dir=produces_dir,
            pod_id=str(handle["pod_id"]),
            name_prefix=str(handle["name_prefix"]),
            remote_root=remote_root,
            upload_mode=upload_mode,
            timeout=timeout,
        )

    t0 = time.monotonic()
    try:
        result = asyncio.run(_exec())
    except Exception as exc:
        raise AstridError(
            str(exc),
            recovery_command="check pod connectivity and remote script syntax, then retry",
        ) from exc

    duration = time.monotonic() - t0

    _write_json(produces_dir / "exec_result.json", result)
    _write_cost_sidecar(produces_dir, duration_seconds=duration, hourly_rate=hourly_rate, basis_prefix="exec")

    print(f"Exec complete: returncode={result['returncode']}, artifacts={result['artifact_dir']}")
    return int(result["returncode"])


# ---------------------------------------------------------------------------
# 3. pull artifacts
# ---------------------------------------------------------------------------


def cmd_pull(args: argparse.Namespace, produces_dir: Path) -> int:
    """Pull files or directories from a provisioned pod using the saved handle."""
    pod_handle_path = Path(args.pod_handle) if args.pod_handle else produces_dir / "pod_handle.json"
    if not pod_handle_path.is_file():
        raise AstridError(
            f"pod_handle.json not found at {pod_handle_path}",
            recovery_command="run 'astrid runpod provision' first to create a pod, then retry pull",
        )

    handle = json.loads(pod_handle_path.read_text(encoding="utf-8"))
    local_dir = Path(args.local_dir) if args.local_dir else produces_dir / "artifact_dir"
    local_dir.mkdir(parents=True, exist_ok=True)
    remote_paths = list(args.remote_path or [])
    if not remote_paths:
        raise AstridError(
            "at least one --remote-path is required",
            recovery_command="specify --remote-path for each file or directory to pull",
        )

    artifacts: list[dict[str, Any]] = []
    for remote_path in remote_paths:
        cmd = _build_scp_pull_command(
            handle,
            remote_path=remote_path,
            local_dir=local_dir,
            ssh_key=args.ssh_key,
        )
        print(f"$ {' '.join(cmd)}")
        rv = subprocess.run(cmd)
        local_path = local_dir if remote_path.rstrip().endswith("/.") else local_dir / Path(remote_path.rstrip("/")).name
        exists = local_path.exists()
        artifacts.append(
            {
                "remote_path": remote_path,
                "local_path": str(local_path),
                "exists": exists,
                "returncode": rv.returncode,
                "command": cmd,
            }
        )
        if rv.returncode != 0:
            # Structured error reporting is the artifact_pull.json manifest +
            # exit code 3 (the executor protocol's error channel); not a raise.
            _write_json(produces_dir / "artifact_pull.json", {"status": "failed", "artifacts": artifacts})
            return 3
        if not exists:
            _write_json(produces_dir / "artifact_pull.json", {"status": "missing_local", "artifacts": artifacts})
            return 3

    _write_json(produces_dir / "artifact_pull.json", {"status": "ok", "artifacts": artifacts})
    print(f"Pulled {len(artifacts)} artifact(s) into {local_dir}")
    return 0


# ---------------------------------------------------------------------------
# 4. teardown
# ---------------------------------------------------------------------------


def cmd_teardown(args: argparse.Namespace, produces_dir: Path) -> int:
    """Terminate a pod by pod_handle. Idempotent — 'not found' is a no-op."""
    pod_handle_path = Path(args.pod_handle) if args.pod_handle else produces_dir / "pod_handle.json"
    if not pod_handle_path.is_file():
        raise AstridError(
            f"pod_handle.json not found at {pod_handle_path}",
            recovery_command="run 'astrid runpod provision' first to create a pod, then retry teardown",
        )

    handle, config = _load_handle_and_config(pod_handle_path)
    if (
        handle.get("schema_version") == "astrid.runpod.claim.v1"
        and handle.get("acquisition_mode") == "attach"
        and handle.get("release_authorized") is not True
    ):
        raise AstridError(
            f"pod {handle.get('pod_id')} was attached without exclusive release authority",
            recovery_command=(
                "retain the pod, or create an explicit exclusive-owner claim before requesting release"
            ),
            code="release_not_authorized",
            state_snapshot={
                "pod_id": handle.get("pod_id"),
                "handle_path": str(pod_handle_path),
            },
        )
    hourly_rate = handle["hourly_rate"]

    t0 = time.monotonic()
    receipt: dict[str, Any] = {"pod_id": handle["pod_id"], "action": "terminate", "status": "unknown"}
    try:

        async def _teardown() -> None:
            from runpod_lifecycle import get_pod

            try:
                pod = await get_pod(handle["pod_id"], config, name=handle.get("name"))
                await pod.terminate()
            except Exception as exc:
                msg = str(exc).lower()
                if "not found" in msg or "404" in msg or "does not exist" in msg:
                    receipt["status"] = "already_gone"
                    receipt["reason"] = f"pod already terminated or not found: {exc}"
                    return
                raise

        asyncio.run(_teardown())
        if receipt["status"] == "unknown":
            receipt["status"] = "terminated"
    except Exception as exc:
        receipt["status"] = "error"
        receipt["reason"] = str(exc)

    duration = time.monotonic() - t0

    receipt["terminated_at"] = utc_now_milliseconds()
    _write_json(produces_dir / "teardown_receipt.json", receipt)
    _write_cost_sidecar(produces_dir, duration_seconds=duration, hourly_rate=hourly_rate, basis_prefix="teardown")

    status = receipt["status"]
    if status == "terminated":
        print(f"Teardown: pod {handle['pod_id']} terminated")
    elif status == "already_gone":
        print(f"Teardown: pod {handle['pod_id']} already gone (idempotent no-op)")
    else:
        print(f"Teardown: pod {handle['pod_id']} — {status}: {receipt.get('reason', '')}")
        raise AstridError(
            f"teardown failed: {receipt['reason']}",
            recovery_command="verify the pod still exists and your API key is valid, then retry",
            state_snapshot={"pod_id": handle["pod_id"]},
        )
    return 0


# ---------------------------------------------------------------------------
# 5. session  (provision → exec → teardown with try/finally)
# ---------------------------------------------------------------------------


def cmd_session(args: argparse.Namespace, produces_dir: Path) -> int:
    """Composite session: provision → exec+download → finally terminate.

    Writes ``pod_handle.json`` immediately after provision so the sweeper
    can recover orphaned pods on crash.  Deletes the handle on graceful
    teardown.
    """
    from runpod_lifecycle import AllocationUnknown, LaunchFailure, RunPodConfig, launch

    from astrid.core.compute_profile import credential_env_ref

    resolved = _resolve_compute_profile(args, produces_dir)
    api_key_ref = credential_env_ref(resolved, "runpod_api_key", "RUNPOD_API_KEY") or "RUNPOD_API_KEY"

    from astrid.core.util.credentials_scope import CredentialsScope

    credential = CredentialsScope.resolve_local("runpod", env_var=api_key_ref)
    api_key = credential.value

    gpu_type = resolved["gpu_type"]
    if isinstance(gpu_type, str) and "," in gpu_type:
        gpu_type = [g.strip() for g in gpu_type.split(",") if g.strip()]
    allowed_cuda_versions = resolved.get("allowed_cuda_versions") or ()
    if isinstance(allowed_cuda_versions, str):
        allowed_cuda_versions = [v.strip() for v in allowed_cuda_versions.split(",") if v.strip()]
    name_prefix = resolved.get("name_prefix")
    if not isinstance(name_prefix, str):
        raise AstridError("RunPod name_prefix must be a string")
    image = resolved.get("image")
    container_disk_gb = int(resolved["container_disk_gb"])
    volume_in_gb = int(resolved.get("volume_in_gb", 0))
    volume_mount_path = str(resolved.get("volume_mount_path") or "/workspace")
    datacenter_id = resolved.get("datacenter_id")
    storage_name = resolved.get("storage_name")
    storage_required = bool(resolved.get("require_storage"))
    max_runtime = int(resolved["max_runtime_seconds"])
    ports = resolved.get("ports")
    remote_root = resolved.get("remote_root") or "/workspace"
    remote_script = resolved.get("remote_script") or ""
    local_root = Path(resolved["local_root"]) if resolved.get("local_root") else Path.cwd()
    timeout = int(resolved["timeout"])
    upload_mode: Literal["sftp_walk", "tarball"] = (
        cast(Literal["sftp_walk", "tarball"], resolved["upload_mode"])
        if resolved.get("upload_mode") in ("sftp_walk", "tarball")
        else "sftp_walk"
    )
    excludes = set(str(resolved["excludes"]).split(",")) if resolved.get("excludes") else set()

    image, allowed_cuda_versions, template_id = _launch_contract(
        gpu_type=gpu_type,
        image=image,
        allowed_cuda_versions=allowed_cuda_versions,
    )

    config = _new_runpod_config(
        RunPodConfig,
        datacenter_id=datacenter_id,
        template_id=template_id,
        api_key=api_key,
        gpu_type=gpu_type,
        worker_image=image,
        container_disk_gb=container_disk_gb,
        disk_size_gb=volume_in_gb,
        volume_mount_path=volume_mount_path,
        storage_name=storage_name,
        attach_only=bool(storage_name and volume_in_gb == 0),
        name_prefix=name_prefix,
        ports=ports,
        allowed_cuda_versions=allowed_cuda_versions,
        ssh_public_key=os.environ.get("RUNPOD_SSH_PUBLIC_KEY"),
        ssh_private_key=os.environ.get("RUNPOD_SSH_PRIVATE_KEY"),
        ssh_public_key_path=(
            os.environ.get("RUNPOD_SSH_PUBLIC_KEY_PATH")
            or os.environ.get("RUNPOD_SSH_IDENTITY_PUBLIC_PATH")
        ),
        ssh_private_key_path=(
            os.environ.get("RUNPOD_SSH_PRIVATE_KEY_PATH")
            or os.environ.get("RUNPOD_SSH_IDENTITY_PATH")
        ),
        env_vars=_host_hf_token_env_vars(resolved),
    )

    _preflight_storage(
        storage_name,
        required=storage_required,
        context="RunPod session",
        api_key=api_key,
    )

    hourly_rate = _get_hourly_rate(api_key, gpu_type)
    provisioned_at = utc_now_milliseconds()

    t0 = time.monotonic()
    pod_id: str | None = None
    handle: dict[str, Any] | None = None
    handle_path = produces_dir / "pod_handle.json"
    attempt = _start_allocation_attempt(
        handle_path, name_prefix=name_prefix, api_key_ref=api_key_ref,
        gpu_type=gpu_type, storage_name=storage_name,
    )
    operation_id = str(attempt["operation_id"])
    request_name = str(attempt["request_name"])
    exit_code = 99  # sentinel for crash-before-exec
    cleanup_pending = False
    cleanup_error: str | None = None

    try:
        # ---- provision -------------------------------------------------
        async def _provision() -> tuple[Any, dict[str, Any]]:
            nonlocal pod_id, handle
            pod = await launch(config, name=request_name)
            # Capture custody immediately after the provider returns a pod.
            # Readiness and SSH discovery can fail after allocation; delaying
            # pod_id assignment until both succeed leaves the finally block
            # unable to terminate that orphan.
            pod_id = str(pod.id)
            provisional_terminate_at = datetime.fromtimestamp(
                datetime.now(timezone.utc).timestamp() + max_runtime,
                tz=timezone.utc,
            ).isoformat()
            handle = _build_provisional_pod_handle(
                pod=pod,
                name_prefix=name_prefix,
                terminate_at=provisional_terminate_at,
                provisioned_at=provisioned_at,
                gpu_type=gpu_type,
                hourly_rate=hourly_rate,
                datacenter_id=datacenter_id,
                image=image,
                container_disk_gb=container_disk_gb,
                volume_in_gb=volume_in_gb,
                storage_name=storage_name,
                network_volume_id=getattr(pod, "_storage_volume", None),
                ports=ports,
                api_key_ref=api_key_ref,
                volume_mount_path=volume_mount_path,
                allowed_cuda_versions=allowed_cuda_versions,
                template_id=template_id,
                operation_id=operation_id,
                request_name=request_name,
            )
            _replace_custody_record(handle_path, handle, operation_id=operation_id)
            await pod.wait_ready(timeout=900)
            ssh = await pod._ensure_ssh_details()
            return pod, ssh

        pod, ssh = asyncio.run(_provision())
        pod_id = pod.id
        terminate_at_dt = datetime.now(timezone.utc).timestamp() + max_runtime
        terminate_at = datetime.fromtimestamp(terminate_at_dt, tz=timezone.utc).isoformat()

        handle = _build_pod_handle(
            pod=pod,
            ssh=ssh,
            name_prefix=name_prefix,
            terminate_at=terminate_at,
            gpu_type=gpu_type,
            hourly_rate=hourly_rate,
            provisioned_at=provisioned_at,
            datacenter_id=datacenter_id,
            image=image,
            container_disk_gb=container_disk_gb,
            volume_in_gb=volume_in_gb,
            storage_name=storage_name,
            network_volume_id=pod._storage_volume,
            ports=ports,
            api_key_ref=api_key_ref or "RUNPOD_API_KEY",
            volume_mount_path=volume_mount_path,
            allowed_cuda_versions=allowed_cuda_versions,
            template_id=template_id,
            operation_id=operation_id,
            request_name=request_name,
        )

        # *** Write pod_handle.json IMMEDIATELY (sweeper breadcrumb) ***
        _replace_custody_record(handle_path, handle, operation_id=operation_id)

        # ---- exec ------------------------------------------------------
        if remote_script:
            if Path(remote_script).is_file():
                remote_script = Path(remote_script).read_text(encoding="utf-8").strip()

            async def _exec() -> dict[str, Any]:
                from runpod_lifecycle import get_pod, ship_and_run_detached

                pod_handle = await get_pod(pod_id, config, name=handle.get("name"))  # type: ignore[arg-type]
                result = await ship_and_run_detached(
                    remote_script=remote_script,
                    pod=pod_handle,
                    local_root=local_root,
                    remote_root=remote_root,
                    exclude=excludes,
                    upload_mode=upload_mode,
                    timeout=timeout,
                    name_prefix=name_prefix,
                    terminate_after_exec=False,
                    poll_interval=30,
                )
                return _detached_exec_result(
                    result,
                    produces_dir=produces_dir,
                    pod_id=str(pod_id),
                    name_prefix=name_prefix,
                    remote_root=remote_root,
                    upload_mode=upload_mode,
                    timeout=timeout,
                )

            exec_result = asyncio.run(_exec())
            exit_code = exec_result["returncode"]

            _write_json(produces_dir / "exec_result.json", exec_result)
        else:
            # No script to execute — just an empty exec_result.
            exec_result = {
                "returncode": 0,
                "stdout": "",
                "stderr": "",
                "terminated": False,
                "termination_status": "completed",
                "artifact_root": None,
                "artifact_dir": str(produces_dir / "artifact_dir"),
                "artifact_paths": [],
                "breadcrumbs": {
                    "pod_id": pod_id,
                    "name_prefix": name_prefix,
                    "remote_root": remote_root,
                    "upload_mode": upload_mode,
                    "timeout": timeout,
                },
            }
            (produces_dir / "artifact_dir").mkdir(parents=True, exist_ok=True)
            _write_json(produces_dir / "exec_result.json", exec_result)
            exit_code = 0

        total_duration = time.monotonic() - t0
        _write_cost_sidecar(produces_dir, duration_seconds=total_duration, hourly_rate=hourly_rate, basis_prefix="session")

    except AllocationUnknown as exc:
        total_duration = time.monotonic() - t0
        _write_cost_sidecar(produces_dir, duration_seconds=total_duration, hourly_rate=hourly_rate, basis_prefix="session (allocation unknown)")
        raise _record_allocation_unknown(handle_path, attempt, exc) from exc
    except Exception as exc:
        known_pod_id = getattr(exc, "pod_id", None) if isinstance(exc, LaunchFailure) else None
        recorded_launch_error: AstridError | None = None
        if isinstance(exc, LaunchFailure) and pod_id is None:
            if not getattr(exc, "retryable", True):
                recorded_launch_error = _record_nonretryable_launch_failure(
                    handle_path, attempt, exc
                )
            else:
                _remove_custody_record(handle_path, operation_id=operation_id)
        elif pod_id is None:
            attempt.update({"state": "allocation_unknown", "unresolved_at": utc_now_milliseconds()})
            _replace_custody_record(handle_path, attempt, operation_id=operation_id)
        total_duration = time.monotonic() - t0
        _write_cost_sidecar(produces_dir, duration_seconds=total_duration, hourly_rate=hourly_rate, basis_prefix="session (failed)")
        if recorded_launch_error is not None:
            raise recorded_launch_error from exc
        raise AstridError(
            str(exc),
            recovery_command=(
                "reconcile the request name with the provider account before retrying"
                if pod_id is None and (
                    not isinstance(exc, LaunchFailure)
                    or not getattr(exc, "retryable", True)
                )
                else "check your RunPod API key, GPU availability, and remote script syntax, then retry"
            ),
            code=(
                "allocation_known" if known_pod_id
                else "allocation_unknown"
                if pod_id is None and (
                    not isinstance(exc, LaunchFailure)
                    or not getattr(exc, "retryable", True)
                )
                else None
            ),
            state_snapshot=(
                {
                    "pod_id": str(known_pod_id),
                    "handle_path": str(handle_path),
                    "reconciliation_required": True,
                }
                if known_pod_id
                else None
            ),
        ) from exc

    finally:
        # ---- teardown (guaranteed) ------------------------------------
        if pod_id:
            teardown_ok = False
            try:
                teardown_ok = asyncio.run(
                    _terminate_pod_id(pod_id, config, name=handle.get("name") if handle else None)
                )
                if not teardown_ok:
                    cleanup_error = "provider termination did not confirm cleanup"
            except Exception as exc:
                cleanup_error = str(exc)
                log_and_swallow(exc, context="runpod.exec.session_teardown_failed")
            if teardown_ok:
                try:
                    _remove_custody_record(handle_path, operation_id=operation_id)
                except Exception as exc:  # noqa: BLE001
                    log_and_swallow(exc, context="runpod.exec.handle_cleanup")
            else:
                cleanup_pending = True
                _mark_cleanup_pending(
                    handle_path,
                    handle,
                    reason=cleanup_error or "provider termination did not confirm cleanup",
                    phase="session_teardown",
                )

    if cleanup_pending and exit_code == 0:
        raise AstridError(
            f"session completed but cleanup is pending for pod {pod_id}: {cleanup_error}",
            recovery_command=f"retry teardown using the cleanup_pending handle at {handle_path}",
            state_snapshot={
                "pod_id": pod_id,
                "handle_path": handle_path,
                "cleanup_pending": True,
                "cleanup_phase": "session_teardown",
            },
        )
    return exit_code


# ---------------------------------------------------------------------------
# CLI entrypoint
# ---------------------------------------------------------------------------


def build_parser() -> argparse.ArgumentParser:
    """Build the argument parser for all action subcommands."""
    parser = argparse.ArgumentParser(description="RunPod executor commands.")
    sub = parser.add_subparsers(dest="command", required=True)

    # --- provision ---
    p_prov = sub.add_parser("provision", help="Provision a RunPod GPU pod.")
    p_prov.add_argument("--produces-dir", type=Path, required=True, help="Produces output directory.")
    p_prov.add_argument("--compute-profile", help="Named user compute profile (ASTRID_COMPUTE_PROFILE takes precedence).")
    p_prov.add_argument("--gpu-type", help="GPU type (e.g. 'NVIDIA GeForce RTX 4090').")
    p_prov.add_argument("--allowed-cuda-versions", help="Comma-separated CUDA host versions (e.g. 12.4,12.6).")
    p_prov.add_argument("--storage-name", help="Network storage volume name.")
    p_prov.add_argument("--max-runtime-seconds", type=int, help="Maximum pod lifetime in seconds.")
    p_prov.add_argument("--name-prefix", help="Pod name prefix for grouping.")
    p_prov.add_argument("--image", help="Docker image for the pod.")
    p_prov.add_argument("--container-disk-gb", type=int, help="Container disk size in GB.")
    p_prov.add_argument("--volume-in-gb", type=int, help="Local or network volume size in GB.")
    p_prov.add_argument("--volume-mount-path", help="Mount path for the local or network volume.")
    p_prov.add_argument("--datacenter-id", help="RunPod datacenter ID.")
    p_prov.add_argument("--ports", help="Comma-separated port spec for the pod (default: '8888/http,22/tcp').")
    p_prov.add_argument("--require-storage", action="store_true", help="Require --storage-name to resolve before launch.")

    # --- exec ---
    p_exec = sub.add_parser("exec", help="Execute a script on an existing pod.")
    p_exec.add_argument("--produces-dir", type=Path, required=True, help="Produces output directory.")
    p_exec.add_argument("--pod-handle", help="Path to pod_handle.json (default: <produces-dir>/pod_handle.json).")
    p_exec.add_argument("--local-root", help="Local directory to upload.")
    p_exec.add_argument("--remote-root", help="Remote path on the pod.")
    p_exec.add_argument("--remote-script", help="Script file path or inline command.")
    p_exec.add_argument("--timeout", type=int, help="Execution timeout in seconds.")
    add_choice_arg(p_exec, "--upload-mode", values=("sftp_walk", "tarball"), help="Upload mode.")
    p_exec.add_argument("--excludes", help="Comma-separated glob patterns to exclude from upload.")

    # --- pull ---
    p_pull = sub.add_parser("pull", help="Pull artifacts from an existing pod.")
    p_pull.add_argument("--produces-dir", type=Path, required=True, help="Produces output directory.")
    p_pull.add_argument("--pod-handle", help="Path to pod_handle.json (default: <produces-dir>/pod_handle.json).")
    p_pull.add_argument("--remote-path", action="append", help="Remote file or directory to pull. Repeatable.")
    p_pull.add_argument("--local-dir", help="Local destination directory.")
    p_pull.add_argument("--ssh-key", help="Private SSH key path. Omit to use ssh-agent/default keys.")

    # --- teardown ---
    p_tear = sub.add_parser("teardown", help="Terminate a pod (idempotent).")
    p_tear.add_argument("--produces-dir", type=Path, required=True, help="Produces output directory.")
    p_tear.add_argument("--pod-handle", help="Path to pod_handle.json (default: <produces-dir>/pod_handle.json).")

    # --- session ---
    p_sess = sub.add_parser("session", help="Provision → exec → teardown composite session.")
    p_sess.add_argument("--produces-dir", type=Path, required=True, help="Produces output directory.")
    p_sess.add_argument("--compute-profile", help="Named user compute profile (ASTRID_COMPUTE_PROFILE takes precedence).")
    p_sess.add_argument("--gpu-type", help="GPU type.")
    p_sess.add_argument("--allowed-cuda-versions", help="Comma-separated CUDA host versions (e.g. 12.4,12.6).")
    p_sess.add_argument("--storage-name", help="Network storage volume name.")
    p_sess.add_argument("--max-runtime-seconds", type=int, help="Maximum pod lifetime in seconds.")
    p_sess.add_argument("--name-prefix", help="Pod name prefix for grouping.")
    p_sess.add_argument("--image", help="Docker image for the pod.")
    p_sess.add_argument("--container-disk-gb", type=int, help="Container disk size in GB.")
    p_sess.add_argument("--volume-in-gb", type=int, help="Local or network volume size in GB.")
    p_sess.add_argument("--volume-mount-path", help="Mount path for the local or network volume.")
    p_sess.add_argument("--datacenter-id", help="RunPod datacenter ID.")
    p_sess.add_argument("--ports", help="Comma-separated port spec for the pod (default: '8888/http,22/tcp').")
    p_sess.add_argument("--require-storage", action="store_true", help="Require --storage-name to resolve before launch.")
    p_sess.add_argument("--local-root", help="Local directory to upload.")
    p_sess.add_argument("--remote-root", help="Remote path on the pod.")
    p_sess.add_argument("--remote-script", help="Script file path or inline command.")
    p_sess.add_argument("--timeout", type=int, help="Execution timeout in seconds.")
    add_choice_arg(p_sess, "--upload-mode", values=("sftp_walk", "tarball"), help="Upload mode.")
    p_sess.add_argument("--excludes", help="Comma-separated glob patterns to exclude from upload.")

    return parser


def main(argv: list[str] | None = None) -> int:
    """Dispatch to the appropriate subcommand handler."""
    args = build_parser().parse_args(argv)

    produces_dir = Path(args.produces_dir)
    produces_dir.mkdir(parents=True, exist_ok=True)

    if args.command == "provision":
        return cmd_provision(args, produces_dir)
    elif args.command == "exec":
        return cmd_exec(args, produces_dir)
    elif args.command == "pull":
        return cmd_pull(args, produces_dir)
    elif args.command == "teardown":
        return cmd_teardown(args, produces_dir)
    elif args.command == "session":
        return cmd_session(args, produces_dir)
    else:
        raise AstridError(
            f"unknown command {args.command!r}",
            valid_options=["provision", "exec", "pull", "teardown", "session"],
            recovery_command="use one of: provision, exec, pull, teardown, session",
        )
