#!/usr/bin/env python3
"""Installed-client control boundary for the hosted R-24 journey.

This file is executed by the installed candidate Python.  It authenticates the
live Runtime owner, starts the configured local Worker only when absent, and
observes or settles the task admitted through ``astrid.omp_invoke``.
"""
from __future__ import annotations

import argparse
import dataclasses
import hashlib
import json
import re
import sys
import time
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

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


def _loopback_endpoint(value: Any) -> str:
    if not isinstance(value, str):
        raise TaskBoundaryError("Runtime owner discovery mismatch")
    try:
        parsed = urlsplit(value)
        port = parsed.port
    except ValueError as exc:
        raise TaskBoundaryError("Runtime owner discovery mismatch") from exc
    if (parsed.scheme != "http" or parsed.hostname != "127.0.0.1"
            or port is None or not 1 <= port <= 65535
            or parsed.username is not None or parsed.password is not None
            or parsed.query or parsed.fragment or parsed.path not in {"", "/"}):
        raise TaskBoundaryError("Runtime owner discovery mismatch")
    return f"http://127.0.0.1:{port}"


def _write_owner_discovery_diagnostic(
    path: Path | None, *, data_root: Path, expected_credential: Path,
    credential_regular_nonsymlink: bool, credential_matches: bool,
    discovery: dict[str, Any],
) -> None:
    if path is None:
        return
    observed_credential = discovery.get("credential_file")
    observed_endpoint = discovery.get("endpoint")
    parsed_endpoint = None
    endpoint_parse_succeeded = False
    if isinstance(observed_endpoint, str):
        try:
            parsed = urlsplit(observed_endpoint)
            port = parsed.port
            endpoint_parse_succeeded = True
            if (parsed.scheme == "http" and parsed.hostname == "127.0.0.1"
                    and port is not None and 1 <= port <= 65535
                    and parsed.username is None and parsed.password is None
                    and not parsed.query and not parsed.fragment
                    and parsed.path in {"", "/"}):
                parsed_endpoint = f"http://127.0.0.1:{port}"
        except ValueError:
            pass
    # Discovery is not trusted to be secret-free. Preserve ordinary endpoint
    # strings exactly, but redact URL components that can carry credentials or
    # arbitrary values when the parser can identify them.
    safe_endpoint = observed_endpoint
    endpoint_redacted = False
    if isinstance(observed_endpoint, str):
        try:
            parsed = urlsplit(observed_endpoint)
            if (parsed.username is not None or parsed.password is not None
                    or parsed.query or parsed.fragment
                    or parsed.scheme not in {"http", "https"}
                    or parsed.hostname not in {"127.0.0.1", "localhost"}
                    or parsed.path not in {"", "/"}):
                safe_endpoint = "<redacted-endpoint-components>"
                endpoint_redacted = True
        except ValueError:
            safe_endpoint = "<unparseable-endpoint>"
            endpoint_redacted = True
    diagnostic = {
        "schema": "astrid.r24.owner-discovery-diagnostic.v1",
        "selected_data_root": str(data_root),
        "resolved_expected_owner_token_path": str(expected_credential),
        "expected_owner_token_regular_nonsymlink": credential_regular_nonsymlink,
        "discovery_credential_file": observed_credential,
        "discovery_credential_path_equals_expected": credential_matches,
        "discovery_endpoint": safe_endpoint,
        "endpoint_redacted": endpoint_redacted,
        "endpoint_parse_succeeded": endpoint_parse_succeeded,
        "canonical_endpoint": parsed_endpoint,
        "endpoint_equals_canonical": (
            parsed_endpoint is not None and parsed_endpoint == observed_endpoint
        ),
    }
    _write(path, diagnostic)


def _installed(
    backend: Path, owner_diagnostic: Path | None = None,
) -> tuple[dict[str, Any], Path, dict[str, Any], Path]:
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
    credential_path = data / "runtime/credentials/owner.token"
    credential_regular_nonsymlink = (
        not credential_path.is_symlink() and credential_path.is_file()
    )
    if not credential_regular_nonsymlink:
        raise TaskBoundaryError("Runtime owner credential mismatch")
    credential = credential_path.resolve(strict=True)
    credential_matches = (
        Path(str(discovery.get("credential_file", ""))) == credential
    )
    endpoint = discovery.get("endpoint")
    try:
        canonical_endpoint = _loopback_endpoint(endpoint)
        endpoint_matches = canonical_endpoint == endpoint
    except TaskBoundaryError:
        canonical_endpoint = None
        endpoint_matches = False
    if not credential_matches or not endpoint_matches:
        _write_owner_discovery_diagnostic(
            owner_diagnostic, data_root=data, expected_credential=credential,
            credential_regular_nonsymlink=credential_regular_nonsymlink,
            credential_matches=credential_matches,
            discovery=discovery,
        )
        raise TaskBoundaryError("Runtime owner discovery mismatch")
    return install, data.resolve(), discovery, credential


def _source_binding(version_root: Path, expected: Path, observed: Path) -> dict[str, str]:
    if (expected.is_symlink() or not expected.is_file()
            or observed.is_symlink() or not observed.is_file()
            or version_root not in observed.resolve(strict=True).parents):
        raise TaskBoundaryError("installed Agent client source mismatch")
    expected_sha = hashlib.sha256(expected.read_bytes()).hexdigest()
    observed_sha = hashlib.sha256(observed.read_bytes()).hexdigest()
    if observed_sha != expected_sha:
        raise TaskBoundaryError("installed Agent client source mismatch")
    return {"path": str(observed.resolve()), "sha256": observed_sha,
            "expected_path": str(expected.resolve())}


def _client(backend: Path, agent_client: Path,
            owner_diagnostic: Path | None = None):
    from astrid_runtime_client import generated

    install, data, discovery, credential = _installed(backend, owner_diagnostic)
    current = (backend / "current").read_text(encoding="utf-8").strip()
    version_root = (backend / "versions" / current).resolve(strict=True)
    source = _source_binding(
        version_root, agent_client, Path(str(generated.__file__)))
    token = credential.read_text(encoding="utf-8").strip()
    if not token:
        raise TaskBoundaryError("Runtime owner credential is empty")
    return (generated.WorkspaceClient(discovery["endpoint"], token, timeout=15),
            install, data, discovery, source)


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


def ensure_worker(backend: Path, agent_client: Path,
                  owner_diagnostic: Path | None = None) -> dict[str, Any]:
    from astrid_runtime_client.generated import ApiError

    client, _install, _data, discovery, source = _client(
        backend, agent_client, owner_diagnostic)
    health, handshake = _health(client, discovery)
    started = False
    try:
        generation = client.get_local_worker_generation()
    except ApiError as exc:
        if exc.status != 404 or exc.code != "not_found":
            raise
        client.start_local_worker("astrid", discovery["active_realm"])
        generation = client.get_local_worker_generation()
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
        "installed_client": source,
    }


def _task_projection(task: Any) -> dict[str, Any]:
    return dataclasses.asdict(task)


def observe_task(backend: Path, agent_client: Path, task_id: str, timeout: int) -> dict[str, Any]:
    client, _install, _data, discovery, source = _client(backend, agent_client)
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
            if (task.task_id != task_id or task.runtime_epoch != health.runtime_epoch
                    or (task.execution_request or {}).get("target") != expected_target
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
                "installed_client": source,
            }
        if task.state in TERMINAL:
            raise TaskBoundaryError(f"task became terminal before provider invocation: {task.state}")
        if time.monotonic() >= deadline:
            raise TaskBoundaryError("task did not reach claimed running state")
        time.sleep(1)


def settle_task(backend: Path, agent_client: Path, task_id: str, timeout: int) -> dict[str, Any]:
    client, _install, _data, discovery, source = _client(backend, agent_client)
    health, _handshake = _health(client, discovery)
    task = client.get_task(task_id)
    if (task.task_id != task_id or task.runtime_epoch != health.runtime_epoch):
        raise TaskBoundaryError("task selected for settlement differs from authenticated request")
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
                "installed_client": source,
            }
        if time.monotonic() >= deadline:
            raise TaskBoundaryError("task remained nonterminal after cancellation")
        time.sleep(1)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("mode", choices=("ensure-worker", "observe", "settle"))
    parser.add_argument("--backend", type=Path, required=True)
    parser.add_argument("--agent-client", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--owner-diagnostic", type=Path)
    parser.add_argument("--task-id")
    parser.add_argument("--timeout", type=int, default=180)
    args = parser.parse_args(argv)
    if args.mode == "ensure-worker":
        value = ensure_worker(args.backend, args.agent_client, args.owner_diagnostic)
    else:
        if not args.task_id or not HEX32.fullmatch(args.task_id):
            raise TaskBoundaryError("32-hex task id required")
        value = (observe_task(args.backend, args.agent_client, args.task_id, args.timeout)
                 if args.mode == "observe"
                 else settle_task(args.backend, args.agent_client, args.task_id, args.timeout))
    _write(args.output, value)
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as exc:  # noqa: BLE001 - executable boundary reports one bounded error
        print(f"{type(exc).__name__}: {exc}", file=sys.stderr)
        raise SystemExit(1)
