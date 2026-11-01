"""Preparation checkpoints in P1's claim journal, under its existing owner lock.

This is process/job custody, never a task, activation or second journal. Old
published claims without these write-ahead checkpoints remain unreleasable by
the preparation gate.
"""
from __future__ import annotations

import hashlib
import json
import re
import shlex
import uuid
from pathlib import Path
from typing import Any, Mapping

from .worker_preparation import TargetObservationError, replace_target_record

SCHEMA = "astrid.runpod.preparation-custody.v1"
QUIESCENCE = "astrid.runpod.preparation-quiescence.v1"
IDENTITY = ("operation_id", "pod_id", "provider_account_ref")


def _read(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise TargetObservationError("preparation custody is unreadable")
    return value


def _supervisor_identity(root: str) -> dict[str, Any]:
    from astrid.core.generation.engine_recipe import load_recipe, recipe_digest
    from astrid.core.generation.engine_runner import _sha, helper_manifest

    recipe = load_recipe("vibecomfy-h3", "linux-x86_64-cu130")
    return {"root": root, "engine": recipe.engine, "platform": recipe.platform,
            "recipe_sha256": recipe_digest(recipe), "helper_sha256": _sha(helper_manifest(recipe))}


def _command(python: str, module: str, function: str, *args: Any) -> str:
    program = f"import json; from {module} import {function}; print(json.dumps({function}(*{args!r})))"
    return shlex.join([python, "-c", program])


def control_preparation_supervisor(supervisor: Mapping[str, Any], action: str) -> dict[str, Any]:
    """Target-side operation; no new owner or PID adoption during rollback."""
    import time

    from astrid.core.generation.engine_custody import LinuxEngineServiceClient

    identity = supervisor["identity"]
    if identity != _supervisor_identity(identity["root"]):
        raise TargetObservationError("preparation supervisor code/recipe changed")
    client = LinuxEngineServiceClient(**identity, owner_id=supervisor["owner_id"])
    deadline = time.monotonic() + 30
    if action == "reserve":
        return client.reserve_preparation(deadline=deadline)
    if action != "stop":
        raise ValueError("unsupported preparation supervisor operation")
    # All launch jobs have already positively stopped under the claim lock.
    # A reservation that never started has no control directory or owner lock.
    # Unsafe/symlink paths and a lost live owner raise, never imply absence.
    try:
        return client.stop_preparation(deadline=deadline)
    except FileNotFoundError:
        from astrid.core.generation.engine_custody import _directory, _exists
        try:
            with _directory(client.control, create=False) as fd:
                if any(_exists(fd, name) is not None for name in
                       ("owner.lock", "session.json", "control.sock")):
                    raise TargetObservationError("preparation supervisor stop is unresolved")
        except FileNotFoundError:
            pass
        return {"identity": client.identity, "owner_id": supervisor["owner_id"],
                "stopped": True, "supervisor_stop": "original_preparation_owner"}


def observe_preparation_job(root: str, job_id: str) -> dict[str, Any]:
    """Positive terminal status AND no live member of the recorded job group.

    Lifecycle's cancelled status alone is insufficient: its cancellation may
    time out after sending signals. Missing status/PID or unreadable /proc is
    uncertainty. Never use this observation to signal an adopted process.
    """
    if not re.fullmatch(r"[0-9a-f]{32}", job_id):
        raise TargetObservationError("invalid preparation job ID")
    directory = Path(root, job_id)
    state = (directory / "status").read_text().strip()
    pid_text = (directory / "pid").read_text().strip()
    if state not in {"exited", "cancelled"} or not pid_text.isdecimal() or int(pid_text) <= 1:
        raise TargetObservationError("exact preparation job is not positively terminal")
    group = int(pid_text)
    for process in Path("/proc").iterdir():
        if not process.name.isdecimal():
            continue
        try:
            fields = (process / "stat").read_text().rsplit(")", 1)[1].split()
        except FileNotFoundError:
            continue
        if int(fields[2]) == group and fields[0] not in {"Z", "X"}:
            raise TargetObservationError("exact preparation job group is still live")
    return {"job_id": job_id, "state": state, "stopped": True, "process_group_absent": True}


def observe_preparation_job_command(root: str, job_id: str, *, python: str = "python3") -> str:
    return _command(python, __name__, "observe_preparation_job", root, job_id)


def validate_preparation_custody(attempt: Mapping[str, Any]) -> Mapping[str, Any]:
    custody = attempt.get("preparation_custody")
    ordinary = attempt.get("ordinary_preparation")
    if (attempt.get("schema_version") != "astrid.runpod.claim.v1"
            or attempt.get("acquisition_mode") != "allocate"
            or attempt.get("release_authorized") is not True
            or attempt.get("target_identity_verified") is not True
            or attempt.get("worker_activation") not in (None, "not_started")
            or attempt.get("request_name") != "astrid-product-" + str(attempt.get("operation_id"))
            or attempt.get("name") != attempt.get("request_name")
            or attempt.get("state") not in {"claimed", "cleanup_pending", "release_pending", "cleanup_complete"}
            or any(attempt.get(key) is not None for key in
                   ("managed_worker", "managed_session", "task_id", "activation_id"))
            or not isinstance(ordinary, Mapping)
            or ordinary.get("schema") != "astrid.runpod.ordinary-preparation.v1"
            or ordinary.get("state") not in {"preparing", "prepared", "unresolved"}
            or ordinary.get("claim_operation_id") != attempt.get("operation_id")
            or any(ordinary.get(key) != attempt.get(key) for key in ("pod_id", "provider_account_ref"))
            or not isinstance(custody, Mapping) or custody.get("schema") != SCHEMA
            or any(not custody.get(key) or custody[key] != attempt.get(key) for key in IDENTITY)
            or custody.get("admission") is not None
            or custody.get("state") not in {"preparing", "prepared"}):
        raise TargetObservationError("release lacks exclusive never-admitted preparation custody")
    supervisor = custody.get("supervisor")
    jobs = custody.get("jobs")
    if (not isinstance(supervisor, Mapping)
            or set(supervisor) != {"identity", "owner_id"}
            or not isinstance(supervisor["identity"], Mapping)
            or set(supervisor["identity"]) != {"root", "engine", "platform", "recipe_sha256", "helper_sha256"}
            or supervisor["identity"].get("engine") != "vibecomfy-h3"
            or supervisor["identity"].get("platform") != "linux-x86_64-cu130"
            or any(not isinstance(supervisor["identity"].get(key), str)
                   or not re.fullmatch(r"[0-9a-f]{64}", supervisor["identity"][key])
                   for key in ("recipe_sha256", "helper_sha256"))
            or not isinstance(supervisor.get("owner_id"), str)
            or not re.fullmatch(r"[0-9a-f]{32}", supervisor["owner_id"])
            or not isinstance(jobs, list)):
        raise TargetObservationError("preparation job/supervisor custody is invalid")
    root = supervisor["identity"].get("root")
    mount = attempt.get("volume_mount_path")
    if (not isinstance(root, str) or not isinstance(mount, str)
            or not Path(root).is_absolute() or ".." in Path(root).parts
            or str(Path(root)) != root or not Path(root).is_relative_to(mount) or root == mount):
        raise TargetObservationError("preparation supervisor is outside the exact claim volume")
    for field in ("python", "job_root"):
        path = custody.get(field)
        if (not isinstance(path, str) or not Path(path).is_absolute()
                or str(Path(path)) != path or ".." in Path(path).parts):
            raise TargetObservationError("preparation has invalid command/job paths")
    if not Path(custody["job_root"]).is_relative_to(mount):
        raise TargetObservationError("preparation jobs are outside the exact claim volume")
    seen = set()
    for job in jobs:
        if (not isinstance(job, Mapping) or set(job) != {"job_id", "command_sha256"}
                or not isinstance(job.get("job_id"), str)
                or not re.fullmatch(r"[0-9a-f]{32}", job["job_id"])
                or job["job_id"] in seen
                or not isinstance(job.get("command_sha256"), str)
                or not re.fullmatch(r"sha256:[0-9a-f]{64}", job["command_sha256"])):
            raise TargetObservationError("preparation jobs are not exact write-ahead identities")
        seen.add(job["job_id"])
    return custody


def validate_preparation_quiescence(value: Mapping[str, Any], attempt: Mapping[str, Any]) -> dict[str, Any]:
    custody = validate_preparation_custody(attempt)
    progress = attempt.get("release_progress", {}).get("preparation")
    if (value.get("schema") != QUIESCENCE
            or attempt.get("state") not in {"release_pending", "cleanup_pending", "cleanup_complete"}
            or set(value) != {"schema", *IDENTITY, "jobs", "supervisor_stop"}
            or any(value.get(key) != attempt.get(key) for key in IDENTITY)
            or not isinstance(progress, Mapping)
            or dict(progress) != {"jobs": value.get("jobs"), "supervisor_stop": value.get("supervisor_stop")}):
        raise TargetObservationError("preparation quiescence lacks its lock-held stop checkpoints")
    jobs = value.get("jobs")
    if not isinstance(jobs, list) or len(jobs) != len(custody["jobs"]):
        raise TargetObservationError("preparation quiescence does not cover every exact job")
    for job, stop in zip(custody["jobs"], jobs):
        if (not isinstance(stop, Mapping)
                or set(stop) != {"job_id", "state", "stopped", "process_group_absent"}
                or stop.get("job_id") != job["job_id"] or stop.get("state") not in {"exited", "cancelled"}
                or stop.get("stopped") is not True or stop.get("process_group_absent") is not True):
            raise TargetObservationError("preparation quiescence lacks positive exact-job stop")
    if (not isinstance(value.get("supervisor_stop"), Mapping)
            or value["supervisor_stop"].get("stopped") is not True
            or value.get("supervisor_stop") != {**custody["supervisor"], "stopped": True,
                                        "supervisor_stop": "original_preparation_owner"}):
        raise TargetObservationError("preparation quiescence lacks original supervisor stop")
    return json.loads(json.dumps(dict(value), allow_nan=False))


class JournalPreparationOps:
    """Write-ahead proxy; its caller already holds P1's target_owner lock."""

    def __init__(self, path: Path, ops: Any) -> None:
        self.path, self.ops = path, ops
        custody = validate_preparation_custody(_read(path))
        if getattr(ops, "job_root", "/workspace/.astrid-jobs") != custody["job_root"]:
            raise TargetObservationError("preparation job transport root changed")

    @classmethod
    def begin(cls, path: Path, ops: Any, *, engine_root: str, python: str) -> JournalPreparationOps:
        current = _read(path)
        if current.get("state") != "claimed" or current.get("preparation_custody") is not None:
            raise TargetObservationError("preparation already has custody or release has begun")
        custody = {"schema": SCHEMA, **{key: current[key] for key in IDENTITY},
                   "state": "preparing", "jobs": [], "python": python,
                   "job_root": getattr(ops, "job_root", "/workspace/.astrid-jobs"),
                   "supervisor": {"identity": _supervisor_identity(engine_root), "owner_id": uuid.uuid4().hex}}
        current = {**current, "preparation_custody": custody}
        validate_preparation_custody(current)
        replace_target_record(path, current, operation_id=current["operation_id"])
        return cls(path, ops)

    def __getattr__(self, name: str) -> Any:
        return getattr(self.ops, name)

    def job_start(self, command: str) -> Mapping[str, Any]:
        current = _read(self.path)
        custody = validate_preparation_custody(current)
        if current["state"] != "claimed" or custody["state"] != "preparing":
            raise TargetObservationError("preparation jobs are excluded during release or admission")
        job_id = uuid.uuid4().hex
        jobs = [*custody["jobs"], {"job_id": job_id,
                "command_sha256": "sha256:" + hashlib.sha256(command.encode()).hexdigest()}]
        replace_target_record(self.path, {**current, "preparation_custody": {**custody, "jobs": jobs}},
                              operation_id=current["operation_id"])
        result = self.ops.job_start(command, job_id=job_id)
        if result.get("id") != job_id:
            raise TargetObservationError("preparation launch returned a foreign job identity")
        return result

    def supervisor_command(self, action: str) -> str:
        custody = validate_preparation_custody(_read(self.path))
        return _command(custody["python"], __name__, "control_preparation_supervisor", custody["supervisor"], action)

    async def quiesce(self, attempt: Mapping[str, Any]) -> Mapping[str, Any]:
        from .worker_preparation import _checkpoint_release_progress, _read_release_checkpoint

        custody = validate_preparation_custody(attempt)
        progress = dict(attempt.get("release_progress", {}))
        saved = dict(progress.get("preparation", {"jobs": []}))

        def checkpoint() -> None:
            progress["preparation"] = saved
            _checkpoint_release_progress(self.path, attempt, progress)

        # Retrying cancellation/observation is safe for these exact job IDs.
        # Never substitute cancellation acknowledgements for positive stops.
        saved["jobs"] = []
        for job in custody["jobs"]:
            self.ops.job_cancel(job["job_id"])
            stop = self.ops.job_observe_stopped(job["job_id"], python=custody["python"])
            saved["jobs"].append(dict(stop))
            checkpoint()
        if "supervisor_stop" not in saved:
            code, stdout, _ = self.ops.exec(self.supervisor_command("stop"), 45)
            if code != 0:
                raise TargetObservationError("original preparation supervisor stop remains unresolved")
            saved["supervisor_stop"] = json.loads(stdout)
            checkpoint()
        value = {"schema": QUIESCENCE, **{key: attempt[key] for key in IDENTITY}, **saved}
        validate_preparation_quiescence(value, _read_release_checkpoint(self.path, attempt))
        return value
