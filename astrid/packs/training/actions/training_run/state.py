"""Durable state helpers for ``training.training_run``."""

from __future__ import annotations

import copy
from pathlib import Path
from typing import Any, Mapping

from astrid.pack_api import read_json, write_json_atomic

LAST_RUN_FILENAME = "last_run.json"

RUN_PHASES = (
    "initialized",
    "preflight_ready",
    "provisioning",
    "pod_ready",
    "staging",
    "training",
    "pulling_artifacts",
    "review_ready",
    "registering",
    "tearing_down",
    "completed",
    "failed",
)


class TrainingRunStateError(ValueError):
    """Raised when training-run state cannot be read or written safely."""


def utc_now_iso() -> str:
    from astrid.pack_api import utc_now_iso as _utc_now_iso

    return _utc_now_iso()


def last_run_path(run_dir_or_path: str | Path) -> Path:
    path = Path(run_dir_or_path).expanduser()
    if path.name == LAST_RUN_FILENAME:
        return path.resolve()
    return (path / LAST_RUN_FILENAME).resolve()


def resume_command(run_dir: str | Path) -> str:
    return f"python3 -m astrid.packs.training.actions.training_run.run resume --out {Path(run_dir).expanduser().resolve()}"


def make_initial_state(
    *,
    run_dir: str | Path,
    config_path: str | Path,
    mode: str,
    manifest: Mapping[str, Any] | None = None,
    secrets: Mapping[str, Any] | None = None,
    budget: Mapping[str, Any] | None = None,
    now: str | None = None,
) -> dict[str, Any]:
    timestamp = now or utc_now_iso()
    resolved_run_dir = Path(run_dir).expanduser().resolve()
    state: dict[str, Any] = {
        "schema_version": 1,
        "state_version": 0,
        "status": "initialized",
        "phase": "initialized",
        "mode": mode,
        "created_at": timestamp,
        "updated_at": timestamp,
        "run_dir": str(resolved_run_dir),
        "config_path": str(Path(config_path).expanduser().resolve()),
        "recoverability": {
            "resume_command": resume_command(resolved_run_dir),
            "teardown_guard": {
                "required": False,
                "pod_id": None,
                "handle_path": None,
            },
        },
    }
    if manifest is not None:
        state["manifest"] = dict(manifest)
    if secrets is not None:
        state["secrets"] = dict(secrets)
    if budget is not None:
        state["budget"] = dict(budget)
    _validate_state(state)
    return state


def read_last_run_state(run_dir_or_path: str | Path) -> dict[str, Any]:
    state = read_json(last_run_path(run_dir_or_path))
    if not isinstance(state, dict):
        raise TrainingRunStateError("last_run.json must contain a JSON object")
    _validate_state(state)
    return state


def write_last_run_state(run_dir_or_path: str | Path, state: Mapping[str, Any], *, now: str | None = None) -> dict[str, Any]:
    next_state = copy.deepcopy(dict(state))
    next_state["state_version"] = int(next_state.get("state_version", 0)) + 1
    next_state["updated_at"] = now or utc_now_iso()
    _validate_state(next_state)
    write_json_atomic(last_run_path(run_dir_or_path), next_state)
    return next_state


def record_phase(
    run_dir_or_path: str | Path,
    phase: str,
    *,
    pod_id: str | None = None,
    handle_path: str | Path | None = None,
    extra: Mapping[str, Any] | None = None,
    now: str | None = None,
) -> dict[str, Any]:
    state = read_last_run_state(run_dir_or_path)
    state["phase"] = phase
    state["status"] = "failed" if phase == "failed" else phase
    if extra:
        state.update(dict(extra))
    if pod_id is not None or handle_path is not None:
        _set_pod_recoverability(state, pod_id=pod_id, handle_path=handle_path)
    return write_last_run_state(run_dir_or_path, state, now=now)


def record_pod_ready(
    run_dir_or_path: str | Path,
    *,
    pod_id: str,
    handle_path: str | Path,
    now: str | None = None,
) -> dict[str, Any]:
    return record_phase(run_dir_or_path, "pod_ready", pod_id=pod_id, handle_path=handle_path, now=now)


def record_failure(
    run_dir_or_path: str | Path,
    *,
    phase: str,
    error: BaseException | str | Mapping[str, Any],
    pod_id: str | None = None,
    handle_path: str | Path | None = None,
    now: str | None = None,
) -> dict[str, Any]:
    state = read_last_run_state(run_dir_or_path)
    state["status"] = "failed"
    state["phase"] = phase
    state["final_error"] = _error_payload(error, phase=phase)
    if pod_id is not None or handle_path is not None:
        _set_pod_recoverability(state, pod_id=pod_id, handle_path=handle_path)
    return write_last_run_state(run_dir_or_path, state, now=now)


def mark_teardown_complete(run_dir_or_path: str | Path, *, now: str | None = None) -> dict[str, Any]:
    state = read_last_run_state(run_dir_or_path)
    guard = state.setdefault("recoverability", {}).setdefault("teardown_guard", {})
    guard["required"] = False
    state["phase"] = "tearing_down"
    state["status"] = "tearing_down"
    return write_last_run_state(run_dir_or_path, state, now=now)


def _set_pod_recoverability(
    state: dict[str, Any],
    *,
    pod_id: str | None,
    handle_path: str | Path | None,
) -> None:
    existing = state.get("pod") if isinstance(state.get("pod"), Mapping) else {}
    resolved_pod_id = str(pod_id or existing.get("id") or "")
    resolved_handle = str(Path(handle_path).expanduser().resolve()) if handle_path is not None else str(existing.get("handle_path") or "")
    if not resolved_pod_id:
        raise TrainingRunStateError("pod_id is required once a pod exists")
    if not resolved_handle:
        raise TrainingRunStateError("handle_path is required once a pod exists")
    state["pod"] = {"id": resolved_pod_id, "handle_path": resolved_handle}
    recoverability = state.setdefault("recoverability", {})
    recoverability["resume_command"] = resume_command(state["run_dir"])
    recoverability["teardown_guard"] = {
        "required": True,
        "pod_id": resolved_pod_id,
        "handle_path": resolved_handle,
    }


def _error_payload(error: BaseException | str | Mapping[str, Any], *, phase: str) -> dict[str, Any]:
    if isinstance(error, Mapping):
        payload = dict(error)
        payload.setdefault("phase", phase)
        return payload
    if isinstance(error, BaseException):
        return {"phase": phase, "type": type(error).__name__, "message": str(error)}
    return {"phase": phase, "type": "error", "message": str(error)}


def _validate_state(state: Mapping[str, Any]) -> None:
    for key in ("schema_version", "state_version", "status", "phase", "run_dir", "config_path", "recoverability"):
        if key not in state:
            raise TrainingRunStateError(f"last_run.json missing required field: {key}")
    if state["schema_version"] != 1:
        raise TrainingRunStateError("last_run.json schema_version must be 1")
    if state["phase"] not in RUN_PHASES:
        raise TrainingRunStateError(f"unsupported training-run phase: {state['phase']}")
    recoverability = state.get("recoverability")
    if not isinstance(recoverability, Mapping):
        raise TrainingRunStateError("recoverability must be an object")
    if not recoverability.get("resume_command"):
        raise TrainingRunStateError("recoverability.resume_command is required")
    guard = recoverability.get("teardown_guard")
    if not isinstance(guard, Mapping):
        raise TrainingRunStateError("recoverability.teardown_guard is required")
    pod = state.get("pod")
    if pod is not None:
        if not isinstance(pod, Mapping) or not pod.get("id") or not pod.get("handle_path"):
            raise TrainingRunStateError("pod state requires id and handle_path")
        if guard.get("required") is not True:
            raise TrainingRunStateError("teardown guard must be required while pod exists")
        if guard.get("pod_id") != pod.get("id") or guard.get("handle_path") != pod.get("handle_path"):
            raise TrainingRunStateError("teardown guard must mirror pod id and handle_path")
