#!/usr/bin/env python3
"""Materialize and execute the hosted Linux R-24 vertical slice."""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import signal
import socket
import subprocess
import sys
import time
import urllib.request
import uuid
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

try:
    from . import ci_journey_provider as provider
    from . import pinokio_linux_mode_actions as mode_actions
    from .ci_journey_r24 import PUBLISHED_CANDIDATE
except ImportError:
    import ci_journey_provider as provider
    import pinokio_linux_mode_actions as mode_actions
    from ci_journey_r24 import PUBLISHED_CANDIDATE


PINOKIOD_REF = "add4a674ad1ba95bb62ec427fce519fe5bf0e9bf"
PTERM_REF = "5be43718701c448b43daf0ce51d8935f170065bb"
CONFIRMATION = "R24_LIVE_CURRENT_TUPLE"
PORT = 42000
TASK_DURATION_SECONDS = 300
HEX40 = re.compile(r"[0-9a-f]{40}\Z")
HEX32 = re.compile(r"[0-9a-f]{32}\Z")
HEX64 = re.compile(r"[0-9a-f]{64}\Z")


class MaterializerError(RuntimeError):
    pass


def _sha(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _canonical(value: Any) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":"),
                      ensure_ascii=True, allow_nan=False).encode("ascii")


def _write(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(json.dumps(value, indent=2, sort_keys=True,
                                allow_nan=False).encode("ascii") + b"\n")


def _read(path: Path) -> Any:
    if path.is_symlink() or not path.is_file():
        raise MaterializerError(f"missing regular file: {path}")
    return json.loads(path.read_bytes())


def _git(root: Path, *args: str) -> str:
    return subprocess.check_output(
        ["git", "-C", str(root), *args], stderr=subprocess.DEVNULL,
        text=True, timeout=30,
    ).strip()


def validate_checkout(root: Path, ref: str, *, tracked_only: bool = False) -> Path:
    if not root.is_absolute() or root.is_symlink() or not root.is_dir():
        raise MaterializerError("absolute nonsymlink checkout required")
    selected = root.resolve(strict=True)
    dirty_args = ["status", "--porcelain=v1",
                  "--untracked-files=no" if tracked_only else "--untracked-files=all"]
    if (Path(_git(selected, "rev-parse", "--show-toplevel")).resolve() != selected
            or _git(selected, "rev-parse", "HEAD") != ref
            or _git(selected, *dirty_args)):
        raise MaterializerError(f"checkout identity mismatch: {root.name}")
    return selected


def pterm_argv(node: Path, pterm: Path, script: str) -> list[str]:
    return [str(node), str(pterm / "index.js"), "start", script,
            "--ref", f"pinokio://127.0.0.1:{PORT}/api/astrid"]


def route_argv(runner: Path, run_root: Path, candidate: Path, config: Path,
               producer_root: Path, checkouts: dict[str, Path], *,
               mode: str = "r24-only",
               mode_producer_root: Path | None = None) -> list[str]:
    route = "r24-live" if mode == "r24-only" else "r25-linux-mode-actions"
    argv = [sys.executable, "-B", str(runner / "scripts/ci_journey.py"),
            "--shared-executor", route,
            "--run-root", str(run_root), "--candidate", str(candidate),
            "--config", str(config), "--producer-checkout", str(producer_root),
            "--app-checkout", str(checkouts["app"]),
            "--agent-checkout", str(checkouts["agent"]),
            "--runtime-checkout", str(checkouts["runtime"]),
            "--pinokio-checkout", str(checkouts["pinokio"])]
    if mode != "r24-only":
        if mode != "backend" or mode_producer_root is None:
            raise MaterializerError("only the bounded backend mode has a hosted materializer")
        argv.extend(["--mode", mode,
                     "--mode-producer-checkout", str(mode_producer_root),
                     "--output", str(run_root / "evidence/r25-linux-mode-actions")])
    return argv


def _run(argv: list[str], *, log: Path, receipt: dict[str, Any], label: str,
         env: dict[str, str] | None = None, cwd: Path | None = None,
         timeout: int = 3600, check: bool = True,
         stderr_log: Path | None = None) -> subprocess.CompletedProcess[bytes]:
    started = time.monotonic()
    try:
        with log.open("wb") as stream:
            if stderr_log is None:
                process = subprocess.run(argv, stdout=stream, stderr=subprocess.STDOUT,
                                         cwd=cwd, env=env, timeout=timeout)
            else:
                with stderr_log.open("wb") as errors:
                    process = subprocess.run(argv, stdout=stream, stderr=errors,
                                             cwd=cwd, env=env, timeout=timeout)
    except (OSError, subprocess.TimeoutExpired) as exc:
        record = {
            "label": label, "argv": argv, "exit": None,
            "elapsed_seconds": round(time.monotonic() - started, 3),
            "timeout_seconds": timeout,
            "timed_out": isinstance(exc, subprocess.TimeoutExpired),
            "error": type(exc).__name__,
            "log": str(log), "log_sha256": _sha(log),
        }
        if stderr_log is not None:
            record.update(stderr_log=str(stderr_log), stderr_sha256=_sha(stderr_log))
        receipt.setdefault("commands", []).append(record)
        if isinstance(exc, subprocess.TimeoutExpired):
            raise MaterializerError(f"{label} timed out after {timeout} seconds") from exc
        raise MaterializerError(f"{label} could not start: {type(exc).__name__}") from exc
    record = {
        "label": label, "argv": argv, "exit": process.returncode,
        "elapsed_seconds": round(time.monotonic() - started, 3),
        "log": str(log), "log_sha256": _sha(log),
    }
    if stderr_log is not None:
        record.update(stderr_log=str(stderr_log), stderr_sha256=_sha(stderr_log))
    receipt.setdefault("commands", []).append(record)
    if check and process.returncode:
        raise MaterializerError(f"{label} exited {process.returncode}")
    return process


def _launch(argv: list[str], *, log: Path, receipt: dict[str, Any], label: str,
            env: dict[str, str], cwd: Path,
            start_new_session: bool = False) -> tuple[
                subprocess.Popen[bytes], Any, dict[str, Any], float]:
    started = time.monotonic()
    stream = log.open("wb")
    try:
        process = subprocess.Popen(
            argv, cwd=cwd, env=env, stdout=stream, stderr=subprocess.STDOUT,
            start_new_session=start_new_session,
        )
    except OSError as exc:
        stream.close()
        receipt.setdefault("commands", []).append({
            "label": label, "argv": argv, "exit": None,
            "elapsed_seconds": round(time.monotonic() - started, 3),
            "error": type(exc).__name__, "log": str(log), "log_sha256": _sha(log),
        })
        raise MaterializerError(f"{label} could not start: {type(exc).__name__}") from exc
    record = {
        "label": label, "argv": argv, "pid": process.pid, "exit": None,
        "status": "running", "log": str(log),
    }
    receipt.setdefault("commands", []).append(record)
    return process, stream, record, started


def _finish_launch(process: subprocess.Popen[bytes] | None, stream: Any,
                   record: dict[str, Any] | None, started: float | None) -> None:
    if process is None or stream is None or record is None or started is None:
        return
    if not stream.closed:
        stream.close()
    record.update(
        exit=process.poll(),
        status="exited" if process.poll() is not None else "exit-unavailable",
        elapsed_seconds=round(time.monotonic() - started, 3),
        log_sha256=_sha(Path(record["log"])),
    )


def materialize(resource: Path, pinokio: Path, producer_root: Path,
                scratch: Path, receipt: dict[str, Any]) -> None:
    if resource.exists() or resource.is_symlink():
        raise MaterializerError("materialized resource path already exists")
    resource.mkdir(parents=True)
    archive = scratch / "candidate-pinokio.tar"
    _run(["git", "-C", str(pinokio), "archive", "--format=tar",
          "--output", str(archive), PUBLISHED_CANDIDATE["pinokio"]],
         log=scratch / "git-archive.log", receipt=receipt, label="materialize-archive")
    _run(["tar", "-xf", str(archive), "-C", str(resource)],
         log=scratch / "tar-extract.log", receipt=receipt, label="materialize-extract")
    archive.unlink()
    for relative in ("pinokio/providers/r24-action.cjs",
                     "pinokio/providers/r24-live-action.js"):
        source = producer_root / relative
        expected = provider.LIVE_PRODUCER_FILES[relative]
        if _sha(source) != expected:
            raise MaterializerError("producer action digest mismatch")
        target = resource / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(source, target)
    environment = resource / "_ENVIRONMENT"
    raw = environment.read_text(encoding="utf-8")
    changed, count = re.subn(r"(?m)^ASTRID_APP_REF=.*$",
                             f"ASTRID_APP_REF={PUBLISHED_CANDIDATE['app']}", raw)
    if count != 1:
        raise MaterializerError("candidate App ref setting unavailable")
    environment.write_text(changed, encoding="utf-8")


def _runner_local_home(run_root: Path, pinokio_home: Path) -> Path:
    """Create the login home that must survive Pinokio's child-env filter.

    Pinokio deliberately removes values rooted under ``PINOKIO_HOME`` before it
    starts an app shell.  Its daemon may therefore have a HOME value which the
    installer child cannot see.  Keep the disposable login home in the same
    runner-local run root, but as a sibling (not a descendant) of the daemon's
    managed home.
    """
    runner_home = run_root / "installer-home"
    if runner_home.exists() or runner_home.is_symlink():
        raise MaterializerError("fresh runner-local installer home required")
    runner_home.mkdir(mode=0o700)
    selected_home = pinokio_home.resolve(strict=True)
    selected_runner_home = runner_home.resolve(strict=True)
    if selected_runner_home == selected_home or selected_home in selected_runner_home.parents:
        raise MaterializerError("installer HOME must be outside PINOKIO_HOME")
    return selected_runner_home


def _engine_environment(home: Path, runner_home: Path) -> dict[str, str]:
    selected_home = home.resolve(strict=True)
    selected_runner_home = runner_home.resolve(strict=True)
    if selected_runner_home == selected_home or selected_home in selected_runner_home.parents:
        raise MaterializerError("installer HOME must be outside PINOKIO_HOME")
    env = dict(os.environ)
    env.update({
        # pterm is a control-plane client.  pinokiod owns the actual shell
        # child environment, which filters values under PINOKIO_HOME.  This
        # sibling directory therefore has to be present in the daemon env.
        "HOME": str(selected_runner_home),
        "XDG_DATA_HOME": str(selected_runner_home / "xdg-data"),
        "PINOKIO_HOME": str(home),
        "PINOKIO_HTTPS_ACTIVE": "0",
        "PINOKIO_NETWORK_ACTIVE": "0",
        "ASTRID_APP_REF": PUBLISHED_CANDIDATE["app"],
    })
    return env


def _install_candidate(*, node: Path, pterm: Path, resource: Path, manifest: Path,
                       home: Path, env: dict[str, str], evidence: Path,
                       receipt: dict[str, Any]) -> tuple[dict[str, Any], Path]:
    """Run the real PTerm route and require its genuine installer result.

    PTerm's terminal disconnect is a transport completion and can return zero
    even when the nested shell command failed.  A zero PTerm exit is therefore
    never installation success: the marker and installed identity must verify
    before this function returns.
    """
    _run(pterm_argv(node, pterm, "pinokio/install.js"),
         log=evidence / "install.log", receipt=receipt, label="pinokio-install",
         env=env, cwd=home, timeout=3600)
    try:
        return _installed(resource / "app/astrid", manifest)
    except MaterializerError as exc:
        raise MaterializerError(
            "pinokio-install transport completed without a valid installed candidate: "
            f"{exc}"
        ) from exc


def _require_port_free(port: int = PORT) -> None:
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
            probe.bind(("127.0.0.1", port))
    except OSError as exc:
        raise MaterializerError(f"loopback port {port} is unavailable before pinokiod launch") from exc


def _loopback_endpoint(value: Any) -> str:
    if not isinstance(value, str):
        raise MaterializerError("Runtime discovery endpoint mismatch")
    try:
        parsed = urlsplit(value)
        port = parsed.port
    except ValueError as exc:
        raise MaterializerError("Runtime discovery endpoint mismatch") from exc
    if (parsed.scheme != "http" or parsed.hostname != "127.0.0.1"
            or port is None or not 1 <= port <= 65535
            or parsed.username is not None or parsed.password is not None
            or parsed.query or parsed.fragment or parsed.path not in {"", "/"}):
        raise MaterializerError("Runtime discovery endpoint mismatch")
    return f"http://127.0.0.1:{port}"


def _wait_engine(process: subprocess.Popen[bytes], timeout: int = 240) -> None:
    deadline = time.monotonic() + timeout
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    while time.monotonic() < deadline:
        if process.poll() is not None:
            raise MaterializerError(f"pinokiod exited before readiness: {process.returncode}")
        try:
            with opener.open(f"http://127.0.0.1:{PORT}/pinokio/version", timeout=2) as response:
                if response.status == 200:
                    return
        except OSError:
            pass
        time.sleep(1)
    raise MaterializerError("pinokiod readiness timeout")


def _wait_runtime(data: Path, start: subprocess.Popen[bytes], timeout: int = 900) -> dict[str, Any]:
    discovery = data / "runtime/discovery.json"
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if start.poll() is not None:
            raise MaterializerError(f"Pinokio Start client exited early: {start.returncode}")
        try:
            value = _read(discovery)
            endpoint = _loopback_endpoint(value.get("endpoint"))
            opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
            request = urllib.request.Request(endpoint + "/v1/health")
            with opener.open(request, timeout=3) as response:
                health = json.load(response)
            if (response.status == 200 and health.get("status") == "ok"
                    and health.get("runtime_instance_id") == value.get("runtime_instance_id")):
                return value
        except (OSError, ValueError, KeyError):
            pass
        time.sleep(2)
    raise MaterializerError("installed Runtime readiness timeout")


def _proc_start_ticks(pid: int) -> str:
    raw = (Path("/proc") / str(pid) / "stat").read_text(encoding="ascii")
    fields = raw[raw.rfind(")") + 2:].split()
    if len(fields) < 20:
        raise MaterializerError("pinokiod process identity unavailable")
    return fields[19]


def build_config(*, checkouts: dict[str, Path], pterm: Path, pinokiod: Path,
                 node: Path, daemon_pid: int, home: Path, scratch: Path) -> dict[str, Any]:
    return {
        "checkouts": {key: str(value) for key, value in checkouts.items()},
        "pterm": str(pterm),
        "pinokiod": str(pinokiod),
        "node": str(node),
        "node_sha256": _sha(node),
        "pterm_entry_sha256": _sha(pterm / "index.js"),
        "unws_sha256": _sha(pterm / "node_modules/unws/src/node.cjs"),
        "engine": {
            "pid": daemon_pid,
            "boot_id": Path("/proc/sys/kernel/random/boot_id").read_text().strip(),
            "start_ticks": _proc_start_ticks(daemon_pid),
            "pinokio_home": str(home),
            "port": PORT,
        },
        "scratch_root": str(scratch),
    }


def _installed(backend: Path, candidate_manifest: Path) -> tuple[dict[str, Any], Path]:
    install = _read(backend / "install.json")
    if (install.get("owner") != "haus.astrid.runtime"
            or install.get("managed_by") != "pinokio"):
        raise MaterializerError("genuine Pinokio install marker unavailable")
    selected_data = Path(str(install.get("data_root", "")))
    if (not selected_data.is_absolute() or selected_data.is_symlink()
            or not selected_data.is_dir()):
        raise MaterializerError("installed data root mismatch")
    data = selected_data.resolve(strict=True)
    current_path = backend / "current"
    if current_path.is_symlink() or not current_path.is_file():
        raise MaterializerError("installed current selection mismatch")
    current = current_path.read_text(encoding="utf-8").strip()
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,63}", current):
        raise MaterializerError("installed current selection mismatch")
    version_path = backend / "versions" / current
    if version_path.is_symlink() or not version_path.is_dir():
        raise MaterializerError("installed candidate identity mismatch")
    version_root = version_path.resolve(strict=True)
    version = _read(version_root / "version.json")
    python = Path(str(version.get("python", "")))
    profile = Path(str(version.get("profile", "")))
    if (Path(str(version.get("root", ""))).resolve(strict=True) != version_root
            or version.get("identity") != {"release": 1, "managed_by": "pinokio"}
            or not python.is_absolute() or not python.is_file()
            or python.parent.is_symlink() or not python.parent.is_dir()
            or profile.is_symlink() or not profile.is_absolute() or not profile.is_file()
            or version_root not in python.parent.resolve(strict=True).parents
            or version_root not in profile.resolve(strict=True).parents
            or _read(version_root / "manifest.json") != _read(candidate_manifest)):
        raise MaterializerError("installed candidate identity mismatch")
    # Keep the lexical venv interpreter path: on POSIX the genuine installer may
    # create it as a symlink, and resolving it can escape the venv semantics.
    version["python"] = str(python)
    version["profile"] = str(profile.resolve(strict=True))
    return version, data


def _pinokio_install_mode_receipt(resource: Path, expected_mode: str) -> dict[str, Any]:
    path = resource / "app/install-mode.json"
    if path.is_symlink() or not path.is_file():
        raise MaterializerError("installed Pinokio mode receipt is unavailable")

    def closed_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
        value: dict[str, Any] = {}
        for key, item in pairs:
            if key in value:
                raise MaterializerError("installed Pinokio mode receipt has duplicate fields")
            value[key] = item
        return value

    try:
        value = json.loads(path.read_bytes(), object_pairs_hook=closed_object)
    except (UnicodeError, json.JSONDecodeError) as exc:
        raise MaterializerError("installed Pinokio mode receipt is malformed") from exc
    if (not isinstance(value, dict)
            or set(value) != {"schema_version", "mode"}
            or type(value.get("schema_version")) is not int
            or value["schema_version"] != 1
            or value.get("mode") not in {"backend", "local"}
            or value["mode"] != expected_mode):
        raise MaterializerError("installed Pinokio mode receipt differs from the selected route")
    return {"schema_version": value["schema_version"], "mode": value["mode"]}


def _installed_env(version: dict[str, Any], data: Path) -> dict[str, str]:
    env = {key: value for key, value in os.environ.items()
           if key not in {"PYTHONPATH", "PYTHONHOME"}}
    selected = version.get("env") or {}
    if not isinstance(selected, dict) or any(not isinstance(key, str) or not isinstance(value, str)
                                            for key, value in selected.items()):
        raise MaterializerError("installed environment mismatch")
    env.update(selected)
    env["ASTRID_RUNTIME_DATA_ROOT"] = str(data)
    env["ASTRID_INSTALL_MANAGED_BY"] = "pinokio"
    env["ASTRID_HOST_EXECUTION_PROFILE"] = "installed"
    return env


def _task_ref(invocation: Path) -> dict[str, str]:
    value = _read(invocation)
    refs = (value.get("details") or {}).get("astridTaskRefs") or []
    if (len(refs) != 1 or set(refs[0]) != {
            "project_id", "run_id", "task_id", "capability_id"}
            or not HEX32.fullmatch(str(refs[0].get("task_id", "")))
            or not HEX32.fullmatch(str(refs[0].get("run_id", "")))
            or not isinstance(refs[0].get("project_id"), str) or not refs[0]["project_id"]
            or refs[0].get("capability_id") != "chiptune.compose"):
        raise MaterializerError("installed Agent did not return one task identity")
    return {key: str(value) for key, value in refs[0].items()}


def _bind_task_ref(task_ref: dict[str, str], observed: dict[str, Any]) -> None:
    task = observed.get("task") or {}
    if any(task.get(key) != task_ref[key]
           for key in ("task_id", "run_id", "project_id", "capability_id")):
        raise MaterializerError("observed task differs from installed Agent admission")


def _terminate(process: subprocess.Popen[bytes] | None, timeout: int = 10) -> None:
    if process is None or process.poll() is not None:
        return
    process.terminate()
    try:
        process.wait(timeout=timeout)
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait(timeout=timeout)


def _hosted_binding(receipt: dict[str, Any], task: dict[str, Any],
                    busy: dict[str, Any], settled: dict[str, Any], *,
                    task_sha: str, busy_sha: str, settled_sha: str) -> dict[str, Any]:
    workflow = {
        "repository": os.environ.get("GITHUB_REPOSITORY"),
        "workflow": os.environ.get("GITHUB_WORKFLOW"),
        "workflow_ref": os.environ.get("GITHUB_WORKFLOW_REF"),
        "workflow_sha": os.environ.get("GITHUB_WORKFLOW_SHA"),
        "run_id": os.environ.get("GITHUB_RUN_ID"),
        "run_attempt": os.environ.get("GITHUB_RUN_ATTEMPT"),
        "job": os.environ.get("GITHUB_JOB"),
        "sha": os.environ.get("GITHUB_SHA"),
    }
    if any(value in (None, "") for value in workflow.values()):
        raise MaterializerError("hosted Actions binding unavailable")
    if (task.get("authenticated") is not True
            or not all(HEX64.fullmatch(value) for value in (task_sha, busy_sha, settled_sha))):
        raise MaterializerError("authenticated task evidence binding unavailable")
    selected = task.get("task") or {}
    binding = selected.get("execution_binding") or {}
    runtime = task.get("runtime") or {}
    final = settled.get("task") or {}
    final_runtime = settled.get("runtime") or {}
    before = busy.get("protected_before")
    after = busy.get("protected_after")
    active = before.get("active_tasks") if isinstance(before, dict) else None
    blockers = (busy.get("busy") or {}).get("blockers")
    if (before != after or not isinstance(active, list) or len(active) != 1
            or not isinstance(blockers, list) or not blockers):
        raise MaterializerError("provider busy census differs from admitted task")
    row = active[0]
    expected = {
        "task_id": selected.get("task_id"),
        "run_id": selected.get("run_id"),
        "status": "running",
        "binding_id": binding.get("binding_id"),
        "execution_session_id": runtime.get("session_id"),
        "runtime_epoch": runtime.get("epoch"),
        "executor_id": binding.get("executor_id"),
        "target_kind": "profile",
        "target_id": "astrid",
        "binding_status": "claimed",
    }
    if (selected.get("state") != "running"
            or binding.get("status") != "claimed"
            or binding.get("target_kind") != "profile" or binding.get("target_id") != "astrid"
            or any(value in (None, "") for value in expected.values())
            or not isinstance(row, dict) or any(row.get(key) != value for key, value in expected.items())
            or row.get("capability") != selected.get("capability_id")
            or {item.get("task_id") for item in blockers} != {selected.get("task_id")}
            or not any(item.get("blocker_kind") == "running_task"
                       and str(item.get("state", "")).lower() == "running"
                       for item in blockers)
            or (busy.get("runtime") or {}).get("runtime_instance_id") != runtime.get("instance_id")
            or (busy.get("runtime") or {}).get("runtime_session_id") != runtime.get("session_id")
            or (busy.get("runtime") or {}).get("runtime_epoch") != runtime.get("epoch")):
        raise MaterializerError("provider busy census is not the admitted running task")
    inventory = ((receipt.get("export") or {}).get("files") or [])
    rows = [item for item in inventory
            if isinstance(item, dict) and item.get("path") == "observation-busy.json"]
    if len(rows) != 1 or rows[0].get("sha256") != busy_sha:
        raise MaterializerError("provider busy observation digest mismatch")
    if (settled.get("authenticated") is not True
            or final.get("task_id") != selected.get("task_id")
            or final.get("run_id") != selected.get("run_id")
            or final.get("state") not in {"succeeded", "failed", "cancelled"}
            or final_runtime.get("instance_id") != runtime.get("instance_id")
            or final_runtime.get("session_id") != runtime.get("session_id")
            or final_runtime.get("epoch") != runtime.get("epoch")
            or settled.get("installed_client") != task.get("installed_client")):
        raise MaterializerError("authenticated task settlement differs from running task")
    return {
        "schema": "astrid.r24.hosted-observation-binding.v1",
        "workflow": workflow,
        "run_id": receipt["run_id"],
        "session": receipt["session"],
        "request_sha256": receipt["request_sha256"],
        "task": {
            "task_id": selected["task_id"],
            "run_id": selected["run_id"],
            "state": selected["state"],
            "execution_binding": binding,
        },
        "candidate": PUBLISHED_CANDIDATE,
        "provider_ref": provider.LIVE_PRODUCER_REF,
        "runtime_execution_sources": receipt["runtime_execution_sources"],
        "task_running_sha256": task_sha,
        "busy_observation_sha256": busy_sha,
        "task_settled_sha256": settled_sha,
        "settlement": {
            "state": final["state"],
            "runtime": final_runtime,
        },
        "export": receipt["export"],
        "qualification": "not-evaluated",
    }


def execute(args: argparse.Namespace) -> int:
    run_root = args.run_root.resolve()
    if run_root.exists() or run_root.is_symlink():
        raise MaterializerError("fresh run root required")
    run_root.mkdir(parents=True)
    evidence = run_root / "evidence/materializer"
    evidence.mkdir(parents=True)
    receipt_path = evidence / "r24-materializer.json"
    selected_mode = getattr(args, "mode", "r24-only")
    receipt: dict[str, Any] = {
        "schema": "astrid.r24.materializer.v1", "status": "running",
        "stage": "preflight", "first_stop": None, "commands": [],
        "mode": selected_mode,
        "candidate": PUBLISHED_CANDIDATE,
        "provider_ref": provider.LIVE_PRODUCER_REF,
        "hosted": {key: os.environ.get(key) for key in
                   ("GITHUB_REPOSITORY", "GITHUB_RUN_ID", "GITHUB_RUN_ATTEMPT",
                    "GITHUB_JOB", "GITHUB_WORKFLOW_REF", "GITHUB_WORKFLOW_SHA", "GITHUB_SHA")},
    }
    _write(receipt_path, receipt)
    daemon = None
    start = None
    daemon_log = None
    start_log = None
    daemon_record = None
    start_record = None
    daemon_started = None
    start_started = None
    backend = None
    task_id = None
    installed_env = None
    version = None
    node = None
    pterm = None
    engine_env = None
    try:
        if sys.platform != "linux" or args.confirmation != CONFIRMATION:
            raise MaterializerError("Linux and exact confirmation are required")
        runner = validate_checkout(args.runner, args.runner_ref)
        if args.producer_ref != provider.LIVE_PRODUCER_REF:
            raise MaterializerError("producer ref differs from audited live producer")
        producer_root = validate_checkout(args.producer, args.producer_ref)
        mode_producer_root = None
        if selected_mode not in {"r24-only", "backend"}:
            raise MaterializerError("only r24-only and backend modes are materialized here")
        if selected_mode == "backend":
            if (getattr(args, "mode_producer", None) is None
                    or getattr(args, "mode_producer_ref", None)
                    != mode_actions.MODE_PRODUCER_REF):
                raise MaterializerError("mode producer ref differs from audited R-25 producer")
            mode_producer_root = validate_checkout(
                args.mode_producer, args.mode_producer_ref)
        checkouts = {
            name: validate_checkout(getattr(args, name), PUBLISHED_CANDIDATE[name])
            for name in ("app", "agent", "runtime", "pinokio")
        }
        pterm = validate_checkout(args.pterm, PTERM_REF, tracked_only=True)
        pinokiod = validate_checkout(args.pinokiod, PINOKIOD_REF)
        roots = {runner, producer_root, pterm, pinokiod, *checkouts.values()}
        if mode_producer_root is not None:
            roots.add(mode_producer_root)
        expected_roots = 9 if mode_producer_root is not None else 8
        if len(roots) != expected_roots:
            raise MaterializerError("all source checkouts must be distinct")
        manifest = checkouts["app"] / "public/install-manifest.json"
        if _sha(manifest) != PUBLISHED_CANDIDATE["manifest_sha256"]:
            raise MaterializerError("published manifest digest mismatch")
        selected_node = shutil.which("node")
        if selected_node is None:
            raise MaterializerError("Node executable is unavailable")
        node = Path(selected_node).resolve(strict=True)
        scratch = run_root / "scratch"
        scratch.mkdir()
        home = run_root / "pinokio-home"
        for path in (home, home / "api"):
            path.mkdir(mode=0o700)
        runner_home = _runner_local_home(run_root, home)
        (runner_home / "xdg-data").mkdir(mode=0o700)
        receipt["environment_boundary"] = {
            "pinokio_home": str(home.resolve(strict=True)),
            "installer_home": str(runner_home),
            "installer_home_inside_pinokio_home": False,
        }
        resource = home / "api/astrid"
        receipt["stage"] = "materialize-candidate"
        _write(receipt_path, receipt)
        materialize(resource, checkouts["pinokio"], producer_root, scratch, receipt)

        receipt["stage"] = "start-pinokiod"
        _write(receipt_path, receipt)
        engine_env = _engine_environment(home, runner_home)
        _require_port_free()
        daemon, daemon_log, daemon_record, daemon_started = _launch(
            [str(node), str(pinokiod / "script/index.js")],
            log=evidence / "pinokiod.log", receipt=receipt, label="start-pinokiod",
            cwd=pinokiod, env=engine_env, start_new_session=True,
        )
        _write(receipt_path, receipt)
        _wait_engine(daemon)

        receipt["stage"] = "install-candidate"
        _write(receipt_path, receipt)
        version, data = _install_candidate(
            node=node, pterm=pterm, resource=resource, manifest=manifest,
            home=home, env=engine_env, evidence=evidence, receipt=receipt,
        )
        mode_path = resource / "app/install-mode.json"
        receipt["pinokio_install_mode_receipt"] = _pinokio_install_mode_receipt(
            resource, "backend")
        receipt["pinokio_install_mode_binding"] = {
            "source": "installed-app/install-mode.json",
            "sha256": _sha(mode_path),
            "slot_id": mode_actions.MODES["backend"],
            "candidate": PUBLISHED_CANDIDATE,
        }
        _write(receipt_path, receipt)
        backend = resource / "app/astrid"
        owner = {"schema_version": 1, "run_id": uuid.uuid4().hex,
                 "root": str(backend.resolve()), "data_root": str(data)}
        _write(run_root / "tst-b1-owner.json", owner)

        receipt["stage"] = "start-installed-runtime"
        _write(receipt_path, receipt)
        start, start_log, start_record, start_started = _launch(
            pterm_argv(node, pterm, "pinokio/start.js"),
            log=evidence / "start.log", receipt=receipt,
            label="start-installed-runtime", cwd=home, env=engine_env,
        )
        _write(receipt_path, receipt)
        discovery = _wait_runtime(data, start)
        installed_env = _installed_env(version, data)

        receipt["stage"] = "authenticate-and-start-worker"
        _write(receipt_path, receipt)
        helper = runner / "scripts/ci_journey_r24_task.py"
        python = str(version["python"])
        agent_client = checkouts["agent"] / "astrid_runtime_client/generated.py"
        worker_file = evidence / "worker-ready.json"
        _run([python, "-I", str(helper), "ensure-worker", "--backend", str(backend),
              "--agent-client", str(agent_client),
              "--output", str(worker_file)], log=evidence / "worker-ready.log",
             receipt=receipt, label="authenticate-health-start-local-worker",
             env=installed_env, timeout=900)

        receipt["stage"] = "admit-installed-agent-task"
        _write(receipt_path, receipt)
        project = f"r24-live-{owner['run_id'][:12]}"
        _run([python, "-I", "-m", "astrid", "projects", "create", project,
              "--name", "R-24 hosted live task", "--json"],
             log=evidence / "project.json", receipt=receipt,
             label="installed-agent-create-project", env=installed_env, timeout=120)
        invocation = evidence / "omp-invocation.json"
        omp_request = {
            "capability_id": "chiptune.compose", "kind": "executor", "project": project,
            "inputs": {"duration_s": TASK_DURATION_SECONDS, "bpm": 132,
                       "key": "A minor", "seed": 24,
                       "sections": [{"start_s": 0, "end_s": TASK_DURATION_SECONDS,
                                     "energy": 0.8, "mood": "tense"}]},
            "execution_request": {"target": {"kind": "profile", "id": "astrid"}},
        }
        _run([python, "-I", "-m", "astrid.omp_invoke",
              json.dumps(omp_request, sort_keys=True, separators=(",", ":"))],
             log=invocation, receipt=receipt, label="installed-agent-admit-task-target",
             env=installed_env, timeout=180,
             stderr_log=evidence / "omp-invocation.stderr.log")
        task_ref = _task_ref(invocation)
        task_id = task_ref["task_id"]
        running_file = evidence / "task-running.json"
        _run([python, "-I", str(helper), "observe", "--backend", str(backend),
              "--agent-client", str(agent_client),
              "--task-id", task_id, "--timeout", "180", "--output", str(running_file)],
             log=evidence / "task-running.log", receipt=receipt,
             label="installed-client-get-task-running", env=installed_env, timeout=240)
        _bind_task_ref(task_ref, _read(running_file))

        receipt["stage"] = "invoke-integrated-provider"
        _write(receipt_path, receipt)
        candidate_file = run_root / "candidate.json"
        config_file = run_root / "config.json"
        _write(candidate_file, PUBLISHED_CANDIDATE)
        _write(config_file, build_config(
            checkouts=checkouts, pterm=pterm, pinokiod=pinokiod, node=node,
            daemon_pid=daemon.pid, home=home, scratch=scratch))
        _run(route_argv(runner, run_root, candidate_file, config_file,
                        producer_root, checkouts, mode=selected_mode,
                        mode_producer_root=mode_producer_root),
             log=evidence / "r24-live-route.log", receipt=receipt,
             label="documented-r24-live-route", env=dict(os.environ), timeout=300)
        provider_receipt = _read(run_root / "evidence/r24-live/r24-invocation.json")
        if selected_mode == "backend":
            mode_record_path = (run_root /
                                "evidence/r25-linux-mode-actions/mode-action-seam.json")
            mode_record = _read(mode_record_path)
            if (mode_record.get("schema") != mode_actions.SCHEMA
                    or mode_record.get("slot_id") != mode_actions.MODES["backend"]
                    or mode_record.get("boundary", {}).get("m4_6_receipt") is not False):
                raise MaterializerError("bounded backend mode-action record differs")
            receipt["mode_action_observation"] = {
                "path": str(mode_record_path),
                "sha256": _sha(mode_record_path),
                "slot_id": mode_record["slot_id"],
                "qualification": "not-evaluated",
                "m4_6_receipt": False,
            }
        task_running = _read(running_file)
        busy_observation = _read(
            run_root / "evidence/r24-live/provider-export/observation-busy.json")
        receipt.update(status="captured-untrusted", stage="settle-task",
                       run_id=owner["run_id"], task_id=task_id,
                       provider_session=provider_receipt["session"],
                       request_sha256=provider_receipt["request_sha256"],
                       runtime={"discovery": discovery,
                                "worker": _read(worker_file)["generation"]})
        _write(receipt_path, receipt)

        settled = evidence / "task-settled.json"
        _run([python, "-I", str(helper), "settle", "--backend", str(backend),
              "--agent-client", str(agent_client),
              "--task-id", task_id, "--timeout", "300", "--output", str(settled)],
             log=evidence / "task-settled.log", receipt=receipt,
             label="installed-client-cancel-settle-task", env=installed_env, timeout=360)
        settled_observation = _read(settled)
        busy_path = run_root / "evidence/r24-live/provider-export/observation-busy.json"
        hosted = _hosted_binding(
            provider_receipt, task_running, busy_observation, settled_observation,
            task_sha=_sha(running_file), busy_sha=_sha(busy_path), settled_sha=_sha(settled),
        )
        _write(run_root / "evidence/r24-live/hosted-binding.json", hosted)
        task_id = None
        receipt["stage"] = "stop-installed-runtime"
        _write(receipt_path, receipt)
        _run(pterm_argv(node, pterm, "pinokio/stop.js"),
             log=evidence / "stop.log", receipt=receipt, label="pinokio-stop",
             env=engine_env, cwd=home, timeout=300)
        receipt.update(status="complete", stage="complete")
        _write(receipt_path, receipt)
        return 0
    except Exception as exc:  # noqa: BLE001 - retain the exact first hosted stop
        receipt.update(status="blocked", first_stop=receipt.get("stage"),
                       error=f"{type(exc).__name__}: {exc}")
        _write(receipt_path, receipt)
        return 1
    finally:
        if (task_id and backend is not None and installed_env is not None
                and version is not None):
            try:
                _run([str(version["python"]), "-I",
                      str(args.runner / "scripts/ci_journey_r24_task.py"), "settle",
                      "--backend", str(backend), "--agent-client",
                      str(args.agent / "astrid_runtime_client/generated.py"), "--task-id", task_id,
                      "--timeout", "120", "--output", str(evidence / "task-cleanup.json")],
                     log=evidence / "task-cleanup.log", receipt=receipt,
                     label="failure-cleanup-task", env=installed_env,
                     timeout=180, check=False)
            except Exception:  # noqa: BLE001 - best-effort bounded failure cleanup
                pass
        if (start is not None and start.poll() is None and daemon is not None
                and daemon.poll() is None and node is not None and pterm is not None
                and engine_env is not None):
            try:
                _run(pterm_argv(node, pterm, "pinokio/stop.js"),
                     log=evidence / "failure-stop.log", receipt=receipt,
                     label="failure-cleanup-pinokio-stop", env=engine_env,
                     cwd=Path(engine_env["PINOKIO_HOME"]), timeout=180, check=False)
            except Exception:  # noqa: BLE001 - best-effort bounded failure cleanup
                pass
        _terminate(start)
        _finish_launch(start, start_log, start_record, start_started)
        if daemon is not None and daemon.poll() is None:
            try:
                os.killpg(daemon.pid, signal.SIGTERM)
                daemon.wait(timeout=10)
            except (OSError, subprocess.TimeoutExpired):
                try:
                    os.killpg(daemon.pid, signal.SIGKILL)
                except OSError:
                    pass
                try:
                    daemon.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    pass
        _finish_launch(daemon, daemon_log, daemon_record, daemon_started)
        _write(receipt_path, receipt)


def parser() -> argparse.ArgumentParser:
    value = argparse.ArgumentParser()
    for name in ("runner", "producer", "app", "agent", "runtime", "pinokio",
                 "pterm", "pinokiod", "run-root"):
        value.add_argument("--" + name, type=Path, required=True)
    value.add_argument("--runner-ref", required=True)
    value.add_argument("--producer-ref", required=True)
    value.add_argument("--mode", choices=("r24-only", "backend"), default="r24-only")
    value.add_argument("--mode-producer", type=Path)
    value.add_argument("--mode-producer-ref")
    value.add_argument("--confirmation", required=True)
    return value


def main(argv: list[str] | None = None) -> int:
    args = parser().parse_args(argv)
    if not HEX40.fullmatch(args.runner_ref) or not HEX40.fullmatch(args.producer_ref):
        raise MaterializerError("full runner and producer commit SHAs required")
    if args.mode == "backend" and (not isinstance(args.mode_producer_ref, str)
                                    or not HEX40.fullmatch(args.mode_producer_ref)):
        raise MaterializerError("full mode producer commit SHA required")
    return execute(args)


if __name__ == "__main__":
    raise SystemExit(main())
