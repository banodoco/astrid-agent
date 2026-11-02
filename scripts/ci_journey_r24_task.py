#!/usr/bin/env python3
"""Installed-client control boundary for the hosted R-24 journey.

This file is executed by the installed candidate Python.  It authenticates the
live Runtime owner, starts the configured local Worker only when absent, and
observes or settles the task admitted through ``astrid.omp_invoke``.
"""
from __future__ import annotations

import argparse
import dataclasses
import json
import os
import re
import time
from pathlib import Path
from typing import Any


TERMINAL = frozenset({"succeeded", "failed", "cancelled"})
HEX32 = re.compile(r"[0-9a-f]{32}\Z")


class TaskBoundaryError(RuntimeError):
    pass


def _read(path: Path) -> Any:
    if path.is_symlink() or not path.is_file():
        raise TaskBoundaryError(f"missing regular file: {path.name}")
    return json.loads(path.read_bytes())


def _write(path: Path, value: Any) -> None:
    path.write_bytes(json.dumps(value, indent=2, sort_keys=True,
                                allow_nan=False).encode("ascii") + b"\n")


def _installed(backend: Path) -> tuple[dict[str, Any], Path, dict[str, Any], Path]:
    if not backend.is_absolute() or backend.is_symlink() or not backend.is_dir():
        raise TaskBoundaryError("absolute installed backend required")
    install = _read(backend / "install.json")
    if (install.get("owner") != "haus.astrid.runtime"
            or install.get("managed_by") != "pinokio"):
        raise TaskBoundaryError("installed owner mismatch")
    data = Path(str(install.get("data_root", "")))
    if not data.is_absolute() or data.is_symlink() or not data.is_dir():
        raise TaskBoundaryError("installed data root mismatch")
    current = (backend / "current").read_text(encoding="utf-8").strip()
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,63}", current):
        raise TaskBoundaryError("installed version selection mismatch")
    version_root = (backend / "versions" / current).resolve(strict=True)
    version = _read(version_root / "version.json")
    if (Path(str(version.get("root", ""))).resolve(strict=True) != version_root
            or version.get("identity") != {"release": 1, "managed_by": "pinokio"}):
        raise TaskBoundaryError("installed version identity mismatch")
    discovery = _read(data / "runtime/discovery.json")
    credential = (data / "runtime/credentials/owner.token").resolve(strict=True)
    if (Path(str(discovery.get("credential_file", ""))).resolve(strict=True) != credential
            or not str(discovery.get("endpoint", "")).startswith("http://127.0.0.1:")):
        raise TaskBoundaryError("Runtime owner discovery mismatch")
    return install, data.resolve(), discovery, credential


def _client(backend: Path):
    from astrid_runtime_client.generated import WorkspaceClient

    install, data, discovery, credential = _installed(backend)
    token = credential.read_text(encoding="utf-8").strip()
    if not token:
        raise TaskBoundaryError("Runtime owner credential is empty")
    return (WorkspaceClient(discovery["endpoint"], token, timeout=15),
            install, data, discovery)


def _health(client: Any, discovery: dict[str, Any]) -> tuple[Any, Any]:
    health = client.health()
    if (health.status != "ok" or health.protocol != "workspace.v1"
            or health.runtime_instance_id != discovery.get("runtime_instance_id")
            or health.identity != {"release": 1, "managed_by": "pinokio"}
            or not health.runtime_session_id or health.runtime_epoch < 1):
        raise TaskBoundaryError("authenticated Runtime health mismatch")
    handshake = client.handshake(
        "astrid-r24-materializer", "1",
        ["projects:write", "tasks:read", "tasks:write", "worker:register"],
    )
    if handshake.protocol != "workspace.v1" or handshake.realm_id != discovery.get("active_realm"):
        raise TaskBoundaryError("authenticated Runtime handshake mismatch")
    return health, handshake


def ensure_worker(backend: Path) -> dict[str, Any]:
    from astrid_runtime_client.generated import ApiError

    client, _install, _data, discovery = _client(backend)
    health, handshake = _health(client, discovery)
    started = False
    try:
        generation = client.get_local_worker_generation()
    except ApiError as exc:
        if exc.status != 404 and exc.code != "not_found":
            raise
        generation = client.start_local_worker("astrid", discovery["active_realm"])
        started = True
    if (generation.get("state") != "active"
            or generation.get("profile_id") != "astrid"
            or generation.get("workspace_uuid") != discovery.get("active_realm")
            or not generation.get("executor_incarnation")):
        raise TaskBoundaryError("local Worker generation mismatch")
    return {
        "schema": "astrid.r24.worker-ready.v1",
        "authenticated": True,
        "started": started,
        "runtime": {
            "instance_id": health.runtime_instance_id,
            "session_id": health.runtime_session_id,
            "epoch": health.runtime_epoch,
            "realm_id": handshake.realm_id,
        },
        "generation": dict(generation),
    }


def _task_projection(task: Any) -> dict[str, Any]:
    return dataclasses.asdict(task)


def observe_task(backend: Path, task_id: str, timeout: int) -> dict[str, Any]:
    client, _install, _data, discovery = _client(backend)
    health, _handshake = _health(client, discovery)
    deadline = time.monotonic() + timeout
    observations = []
    while True:
        task = client.get_task(task_id)
        observations.append({"state": task.state, "version": task.version,
                             "binding_status": (task.execution_binding or {}).get("status")})
        binding = task.execution_binding or {}
        expected_target = {"kind": "profile", "id": "astrid"}
        if task.state == "running" and binding.get("status") == "claimed":
            if ((task.execution_request or {}).get("target") != expected_target
                    or binding.get("session_id") != health.runtime_session_id
                    or binding.get("runtime_epoch") != health.runtime_epoch
                    or not binding.get("executor_id")
                    or not binding.get("binding_id")):
                raise TaskBoundaryError("running task execution binding mismatch")
            return {
                "schema": "astrid.r24.task-running.v1",
                "authenticated": True,
                "runtime": {"instance_id": health.runtime_instance_id,
                            "session_id": health.runtime_session_id,
                            "epoch": health.runtime_epoch},
                "observations": observations,
                "task": _task_projection(task),
            }
        if task.state in TERMINAL:
            raise TaskBoundaryError(f"task became terminal before provider invocation: {task.state}")
        if time.monotonic() >= deadline:
            raise TaskBoundaryError("task did not reach claimed running state")
        time.sleep(1)


def settle_task(backend: Path, task_id: str, timeout: int) -> dict[str, Any]:
    client, _install, _data, discovery = _client(backend)
    health, _handshake = _health(client, discovery)
    task = client.get_task(task_id)
    cancel = None
    if task.state not in TERMINAL:
        cancel = dict(client.cancel_task(
            task_id, idempotency_key=f"r24-cancel-{task_id}",
            expected_version=task.version,
        ))
    deadline = time.monotonic() + timeout
    states = []
    while True:
        task = client.get_task(task_id)
        states.append({"state": task.state, "version": task.version})
        if task.state in TERMINAL:
            return {
                "schema": "astrid.r24.task-settled.v1",
                "authenticated": True,
                "runtime": {"instance_id": health.runtime_instance_id,
                            "session_id": health.runtime_session_id,
                            "epoch": health.runtime_epoch},
                "cancel": cancel,
                "states": states,
                "task": _task_projection(task),
            }
        if time.monotonic() >= deadline:
            raise TaskBoundaryError("task remained nonterminal after cancellation")
        time.sleep(1)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("mode", choices=("ensure-worker", "observe", "settle"))
    parser.add_argument("--backend", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--task-id")
    parser.add_argument("--timeout", type=int, default=180)
    args = parser.parse_args(argv)
    if args.mode == "ensure-worker":
        value = ensure_worker(args.backend)
    else:
        if not args.task_id or not HEX32.fullmatch(args.task_id):
            raise TaskBoundaryError("32-hex task id required")
        value = (observe_task(args.backend, args.task_id, args.timeout)
                 if args.mode == "observe"
                 else settle_task(args.backend, args.task_id, args.timeout))
    _write(args.output, value)
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as exc:
        print(f"{type(exc).__name__}: {exc}", file=__import__("sys").stderr)
        raise SystemExit(1)
