"""Windows R1.1 Pinokio stage evidence, independent of the shared gate.

The caller must execute and observe the real launcher. This module only validates
and persists the observations; it does not run Pinokio or certify a hosted run.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
from pathlib import Path
from typing import Any, Mapping

from scripts.make_standin_release import canonical_manifest_bytes, make_standin_release

STAGES = ("install", "start_task", "stop_start", "busy_stop", "update", "updated_task", "uninstall")
_HEX = re.compile(r"^[0-9a-f]{64}$")


class EvidenceError(ValueError):
    pass


def standin_manifest(candidate: Mapping[str, Any], destination: Path) -> str:
    """Persist the canonical stand-in that the launcher must actually install."""
    if candidate.get("release") != 1 or isinstance(candidate.get("release"), bool):
        raise EvidenceError("Windows Pinokio baseline must be release 1")
    raw = canonical_manifest_bytes(make_standin_release(candidate))
    destination = Path(destination)
    if destination.exists() or destination.is_symlink():
        raise EvidenceError("stand-in destination already exists")
    destination.parent.mkdir(parents=True, exist_ok=True)
    with destination.open("xb") as stream:
        stream.write(raw)
    return hashlib.sha256(raw).hexdigest()


def _true(row: Mapping[str, Any], *names: str) -> None:
    for name in names:
        if row.get(name) is not True:
            raise EvidenceError(f"{name} must be observed true")


def _text(row: Mapping[str, Any], name: str) -> str:
    value = row.get(name)
    if not isinstance(value, str) or not value.strip():
        raise EvidenceError(f"{name} must be observed")
    return value


def _digest(row: Mapping[str, Any], name: str) -> str:
    value = _text(row, name)
    if not _HEX.fullmatch(value):
        raise EvidenceError(f"{name} must be a SHA-256 digest")
    return value


def validate_stage(name: str, row: Mapping[str, Any], earlier: Mapping[str, Mapping[str, Any]]) -> None:
    """Validate observations and cross-stage bindings; missing values never pass."""
    if name not in STAGES or not isinstance(row, Mapping) or row.get("status") != "pass":
        raise EvidenceError(f"{name}: missing or non-passing stage")
    if name == "install":
        _true(row, "launcher_install", "managed_by_pinokio")
        if row.get("release") != 1 or isinstance(row.get("release"), bool):
            raise EvidenceError("install must select release 1")
        _text(row, "install_id")
    elif name == "start_task":
        _true(row, "launcher_start", "healthy", "task_succeeded", "materialized_output")
        _text(row, "runtime_instance_id")
        _text(row, "task_id")
        _digest(row, "data_digest")
    elif name == "stop_start":
        _true(row, "launcher_stop", "owned_runtime_stopped", "unrelated_sentinel_alive", "launcher_start", "healthy")
        before = _text(row, "before_instance_id")
        after = _text(row, "after_instance_id")
        if before != earlier["start_task"]["runtime_instance_id"] or before == after:
            raise EvidenceError("restart must replace the observed initial Runtime instance")
    elif name == "busy_stop":
        _true(row, "task_observed_busy", "launcher_stop_attempted", "data_intact", "unrelated_sentinel_alive")
        if row.get("stop_outcome") not in {"refused", "stopped"}:
            raise EvidenceError("busy Stop outcome is unknown")
        if row.get("task_state") not in {"succeeded", "failed"}:
            raise EvidenceError("busy task did not finish or cleanly fail")
        _true(row, "task_terminal_observed")
        if row["task_state"] == "failed":
            _true(row, "task_clean_failure")
        if _text(row, "task_id") == earlier["start_task"]["task_id"]:
            raise EvidenceError("busy task must be distinct from the initial task")
        if _digest(row, "data_digest") != earlier["start_task"]["data_digest"]:
            raise EvidenceError("workspace data changed during busy Stop")
    elif name == "update":
        _true(row, "launcher_update", "healthy", "managed_by_pinokio", "data_intact")
        if row.get("release") != 2 or isinstance(row.get("release"), bool):
            raise EvidenceError("update must select release 2")
        if _digest(row, "installed_manifest_sha256") != _digest(row, "standin_manifest_sha256"):
            raise EvidenceError("installed manifest differs from the generated stand-in")
        _text(row, "runtime_instance_id")
        if _digest(row, "data_digest") != earlier["start_task"]["data_digest"]:
            raise EvidenceError("workspace data changed during update")
    elif name == "updated_task":
        _true(row, "healthy", "task_succeeded", "materialized_output")
        if row.get("release") != 2 or isinstance(row.get("release"), bool) or row.get("runtime_instance_id") != earlier["update"]["runtime_instance_id"]:
            raise EvidenceError("updated task is not bound to the release 2 Runtime")
        _text(row, "task_id")
        if row["task_id"] in {earlier["start_task"]["task_id"], earlier["busy_stop"]["task_id"]}:
            raise EvidenceError("updated task must be a distinct task")
    elif name == "uninstall":
        _true(row, "launcher_uninstall", "program_removed", "owned_runtime_stopped", "unrelated_sentinel_alive", "data_intact")
        if _digest(row, "data_digest") != earlier["start_task"]["data_digest"]:
            raise EvidenceError("uninstall did not preserve workspace data")


class WindowsPinokioEvidence:
    """Write an incremental, ordered evidence ledger, failing closed on errors."""

    def __init__(self, path: Path, standin_sha256: str):
        if not isinstance(standin_sha256, str) or not _HEX.fullmatch(standin_sha256):
            raise EvidenceError("generated stand-in digest is required")
        self.standin_sha256 = standin_sha256
        self.path = Path(path)
        if self.path.exists() or self.path.is_symlink():
            raise EvidenceError("evidence destination already exists")
        self.rows: list[dict[str, Any]] = []
        self._save("incomplete")

    def _save(self, status: str) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temp = self.path.with_name(self.path.name + ".tmp")
        if temp.exists() or temp.is_symlink():
            raise EvidenceError("evidence temporary path already exists")
        payload = {"schema": "astrid.pinokio-windows-r1.1-stages.v1", "status": status, "standin_sha256": self.standin_sha256, "stages": self.rows}
        try:
            with temp.open("x", encoding="utf-8", newline="\n") as stream:
                json.dump(payload, stream, indent=2, sort_keys=True, allow_nan=False)
                stream.write("\n")
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temp, self.path)
        finally:
            temp.unlink(missing_ok=True)

    def record(self, name: str, observation: Mapping[str, Any]) -> None:
        if len(self.rows) >= len(STAGES) or name != STAGES[len(self.rows)]:
            raise EvidenceError("stage is missing, duplicated, or out of order")
        previous = {row["stage"]: row["observation"] for row in self.rows}
        validate_stage(name, observation, previous)
        if name == "update" and observation["standin_manifest_sha256"] != self.standin_sha256:
            raise EvidenceError("update does not match the generated stand-in")
        # Round-trip to avoid mutable aliases and reject values that are not JSON.
        copied = json.loads(json.dumps(dict(observation), allow_nan=False))
        self.rows.append({"stage": name, "observation": copied})
        try:
            self._save("pass" if len(self.rows) == len(STAGES) else "incomplete")
        except Exception:
            self.rows.pop()
            raise


def validate_ledger(value: Mapping[str, Any], *, standin_sha256: str) -> None:
    """Gate-side validation: a pass label alone cannot satisfy the contract."""
    if not isinstance(value, Mapping) or value.get("schema") != "astrid.pinokio-windows-r1.1-stages.v1" or value.get("status") != "pass":
        raise EvidenceError("Windows Pinokio stage ledger is absent or incomplete")
    if not isinstance(standin_sha256, str) or not _HEX.fullmatch(standin_sha256) or value.get("standin_sha256") != standin_sha256:
        raise EvidenceError("generated stand-in digest does not match the ledger")
    rows = value.get("stages")
    if not isinstance(rows, list) or len(rows) != len(STAGES):
        raise EvidenceError("Windows Pinokio stages are missing")
    earlier: dict[str, Mapping[str, Any]] = {}
    for name, row in zip(STAGES, rows):
        if not isinstance(row, Mapping) or row.get("stage") != name:
            raise EvidenceError("Windows Pinokio stages are out of order")
        observation = row.get("observation")
        validate_stage(name, observation, earlier)
        if name == "update" and observation["standin_manifest_sha256"] != standin_sha256:
            raise EvidenceError("update does not match the generated stand-in")
        earlier[name] = observation
