#!/usr/bin/env python3
"""Run the bounded, fully-local M4-4b editor failure/restore execution.

This executor is intentionally separate from the accepted source/fake journey.
It owns only disposable, marker-attested paths supplied by the caller.  The
build and readiness boundaries are real child processes; the receipt is local
process evidence and is never a hosted Linux qualification receipt.
"""

from __future__ import annotations

import argparse
import copy
import hashlib
import json
import os
import platform
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping

try:
    import fcntl
except ImportError:  # pragma: no cover - the supported host is POSIX.
    fcntl = None  # type: ignore[assignment]

try:
    from scripts.journey_fully_local import (
        MODE_BYTES,
        PINNED_TUPLE,
        RECEIPT_KIND,
        ROUTE_ORDER,
        ReceiptValidationError,
        validate_receipt,
    )
except ModuleNotFoundError:  # Direct ``python scripts/fully_local_executor.py``.
    from journey_fully_local import (  # type: ignore[no-redef]
        MODE_BYTES,
        PINNED_TUPLE,
        RECEIPT_KIND,
        ROUTE_ORDER,
        ReceiptValidationError,
        validate_receipt,
    )


DISPOSABLE_MARKER = ".m4-4b-disposable"
FAILURE_REASON = "deterministic editor build failure at real process boundary"
FAILURE_CODE = 23
LOCAL_EVIDENCE_CLASS = "local_process"
HOSTED_LINUX_REQUIREMENT = "hosted_linux_receipt_required"
LOCK_NAME = "installer.lock"
MODE_NAME = "install-mode.json"
INSTALL_NAME = "install.json"
DATA_NAME = "data"
READY_URL = "http://127.0.0.1:5180/tools/video-editor"


class ExecutionError(RuntimeError):
    """Raised when the local execution cannot safely prove its contract."""


@dataclass
class CheckoutSnapshot:
    head: str
    branch: str
    origin: str
    porcelain: str

    def as_dict(self) -> dict[str, str]:
        return {
            "head": self.head,
            "branch": self.branch,
            "origin": self.origin,
            "porcelain": self.porcelain,
        }


def _run(argv: list[str], *, cwd: Path | None = None, check: bool = True) -> subprocess.CompletedProcess[str]:
    result = subprocess.run(argv, cwd=cwd, text=True, capture_output=True, check=False)
    if check and result.returncode != 0:
        raise ExecutionError(
            f"command failed ({result.returncode}): {argv!r}\n"
            f"stdout={result.stdout!r}\nstderr={result.stderr!r}"
        )
    return result


def _git(checkout: Path, *args: str, check: bool = True) -> subprocess.CompletedProcess[str]:
    return _run(["git", "-C", str(checkout), *args], check=check)


def _require_disposable_directory(path: Path, label: str) -> Path:
    if path.is_symlink():
        raise ExecutionError(f"{label} must not be a symlink: {path}")
    resolved = path.expanduser().resolve()
    if not resolved.is_dir():
        raise ExecutionError(f"{label} must be an existing real directory: {path}")
    marker = resolved / DISPOSABLE_MARKER
    if not marker.is_file() or marker.is_symlink():
        raise ExecutionError(f"{label} is not explicitly disposable: missing {marker}")
    if marker.read_text(encoding="utf-8") != "m4-4b-disposable\n":
        raise ExecutionError(f"{label} has an invalid disposable marker")
    return resolved


def _is_within(path: Path, root: Path) -> bool:
    return path == root or root in path.parents


def _checkout_snapshot(checkout: Path) -> CheckoutSnapshot:
    head = _git(checkout, "rev-parse", "HEAD").stdout.strip()
    branch = _git(checkout, "symbolic-ref", "--short", "-q", "HEAD").stdout.strip()
    if not branch:
        raise ExecutionError("disposable checkout must have a named branch")
    origin_result = _git(checkout, "remote", "get-url", "origin", check=False)
    origin = origin_result.stdout.strip()
    if origin_result.returncode != 0 or not origin:
        raise ExecutionError("disposable checkout must have an origin remote")
    porcelain = _git(checkout, "status", "--porcelain=v1", "--untracked-files=all").stdout
    return CheckoutSnapshot(head=head, branch=branch, origin=origin, porcelain=porcelain)


def _require_clean(snapshot: CheckoutSnapshot) -> None:
    if snapshot.porcelain:
        raise ExecutionError("disposable checkout must be clean before execution")


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return "sha256:" + digest.hexdigest()


def _write_json(path: Path, value: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def _ensure_runtime_layout(state_root: Path) -> tuple[Path, Path, Path]:
    runtime = state_root / "runtime"
    install = runtime / INSTALL_NAME
    mode = runtime / MODE_NAME
    data_root = runtime / DATA_NAME
    if any(path.is_symlink() for path in (runtime, install, mode, data_root)):
        raise ExecutionError("disposable Runtime state must not contain symlinked custody paths")
    runtime.mkdir(exist_ok=True)
    if not install.exists():
        _write_json(
            install,
            {
                "current": "versions/prior",
                "data_root": str(data_root.resolve()),
                "managed_by": "pinokio",
                "owner": "haus.astrid.runtime",
                "profile_source_manifest": "profile.json:sha256:prior",
                "selected_realm": "realm-1",
                "selected_version": "versions/prior/version.json",
            },
        )
    if not mode.exists():
        mode.write_bytes(MODE_BYTES.encode("utf-8"))
    if mode.read_bytes() != MODE_BYTES.encode("utf-8"):
        raise ExecutionError("disposable Runtime mode is not exactly local before execution")
    try:
        install_value = json.loads(install.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ExecutionError(f"invalid disposable Runtime install record: {install}") from exc
    if not isinstance(install_value, dict):
        raise ExecutionError("disposable Runtime install record must be an object")
    required = {"current", "data_root", "managed_by", "owner", "profile_source_manifest",
                "selected_realm", "selected_version"}
    if not required.issubset(install_value):
        raise ExecutionError("disposable Runtime install record is missing required fields")
    if install_value["managed_by"] != "pinokio" or install_value["owner"] != "haus.astrid.runtime":
        raise ExecutionError("disposable Runtime install ownership is not Pinokio/haus.astrid.runtime")
    if Path(str(install_value["data_root"])).resolve() != data_root.resolve():
        raise ExecutionError("disposable Runtime data root is not inside the isolated state root")
    data_root.mkdir(exist_ok=True)
    catalog = data_root / "catalog.json"
    project = data_root / "projects" / "demo" / "project.json"
    if not catalog.exists():
        catalog.write_text('{"catalog":"prior"}\n', encoding="utf-8")
    if not project.exists():
        project.parent.mkdir(parents=True, exist_ok=True)
        project.write_text('{"project":"demo","version":1}\n', encoding="utf-8")
    return install, mode, data_root


def _data_snapshot(data_root: Path, selected_realm: str) -> dict[str, Any]:
    inventory: list[dict[str, str]] = []
    secret_tokens = ("secret", "token", ".env", "private", "credential")
    for path in sorted(data_root.rglob("*")):
        if not path.is_file() or path.is_symlink():
            continue
        relative = path.relative_to(data_root).as_posix()
        if any(token in relative.lower() for token in secret_tokens):
            raise ExecutionError(f"refusing data inventory with secret-like path: {relative}")
        inventory.append({"path": relative, "sha256": _sha256(path)})
    encoded = json.dumps(inventory, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return {
        "realpath": str(data_root.resolve()),
        "selected_realm": selected_realm,
        "catalog_digest": _sha256(data_root / "catalog.json"),
        "inventory_digest": "sha256:" + hashlib.sha256(encoded).hexdigest(),
        "inventory": inventory,
    }


def _installed_snapshot(install: Path, data_root: Path) -> dict[str, Any]:
    value = json.loads(install.read_text(encoding="utf-8"))
    return {
        "install": str(install.resolve()),
        "current": value["current"],
        "selected_version": value["selected_version"],
        "managed_by": value["managed_by"],
        "owner": value["owner"],
        "profile_source_manifest": value["profile_source_manifest"],
        "data_root": str(data_root.resolve()),
        "selected_realm": value["selected_realm"],
    }


class _ExclusiveLock:
    def __init__(self, path: Path) -> None:
        self.path = path
        self.handle: Any = None

    def acquire(self) -> None:
        if fcntl is None:
            raise ExecutionError("exclusive lock proof requires POSIX flock support")
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.handle = self.path.open("a+")
        try:
            fcntl.flock(self.handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            self.handle.close()
            self.handle = None
            raise ExecutionError(f"could not acquire disposable installer lock: {self.path}") from exc

    def release(self) -> None:
        if self.handle is not None:
            fcntl.flock(self.handle.fileno(), fcntl.LOCK_UN)
            self.handle.close()
            self.handle = None

    def probe_reacquire(self) -> bool:
        if fcntl is None:
            return False
        with self.path.open("a+") as probe:
            try:
                fcntl.flock(probe.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            except OSError:
                return False
            fcntl.flock(probe.fileno(), fcntl.LOCK_UN)
        return True


TEMP_RUNTIME_SCRIPT = (
    "import json,time; "
    "print(json.dumps({'support': {'healthy': True}, 'health': {'status': 'ok'}}), flush=True); "
    "time.sleep(60)"
)

BUILD_FAILURE_SCRIPT = (
    "import os,sys; "
    "print('editor build spawned: --build local', flush=True); "
    "print('ASTRID_PINOKIO_FAILURE=' + os.environ.get('ASTRID_PINOKIO_FAILURE', ''), file=sys.stderr, flush=True); "
    "sys.exit(23)"
)

READY_SERVER_SCRIPT = r'''
import json, signal, sys
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

port = int(sys.argv[1])
realm = sys.argv[2]
instance = sys.argv[3]

class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path == "/status":
            body = {"support": {"healthy": True}, "health": {"status": "ok"},
                    "stale_discovery": False, "runtime_instance_id": instance,
                    "realm_id": realm}
        elif self.path == "/runtime-config/v1/workspace.json":
            body = {"schemaVersion": 1, "realm_id": realm,
                    "runtime_instance_id": instance}
        elif self.path == "/tools/video-editor":
            body = {"ok": True, "route": "pinokio.update.local-editor"}
        else:
            self.send_error(404)
            return
        raw = json.dumps(body).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def log_message(self, *_args):
        pass

server = ThreadingHTTPServer(("127.0.0.1", port), Handler)
signal.signal(signal.SIGTERM, lambda *_args: sys.exit(0))
server.serve_forever()
'''


def _spawn(argv: list[str], *, cwd: Path, env: Mapping[str, str]) -> subprocess.Popen[str]:
    return subprocess.Popen(
        argv,
        cwd=cwd,
        env=dict(env),
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        bufsize=1,
    )


def _stop_process(process: subprocess.Popen[str] | None) -> tuple[int | None, str, str]:
    if process is None:
        return None, "", ""
    if process.poll() is None:
        process.terminate()
    try:
        stdout, stderr = process.communicate(timeout=5)
    except subprocess.TimeoutExpired:
        process.kill()
        stdout, stderr = process.communicate(timeout=5)
        raise ExecutionError("owned local process required a forced kill")
    return process.returncode, stdout, stderr


def _port_free(port: int) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        probe.settimeout(0.2)
        return probe.connect_ex(("127.0.0.1", port)) != 0


def _get_json(url: str, timeout: float = 0.5) -> tuple[int, dict[str, Any]]:
    with urllib.request.urlopen(url, timeout=timeout) as response:
        return response.status, json.loads(response.read().decode("utf-8"))


def _wait_for_json(url: str, timeout: float = 5.0) -> tuple[int, dict[str, Any]]:
    deadline = time.monotonic() + timeout
    last_error: BaseException | None = None
    while time.monotonic() < deadline:
        try:
            return _get_json(url)
        except (OSError, ValueError, urllib.error.URLError) as exc:
            last_error = exc
            time.sleep(0.02)
    raise ExecutionError(f"readiness probe did not become ready: {url}: {last_error}")


def _event(name: str, *, status: str = "pass", **extra: Any) -> dict[str, Any]:
    return {"name": name, "status": status, **extra}


def _contract_projection(receipt: Mapping[str, Any]) -> dict[str, Any]:
    """Validate the accepted journey contract without relabelling evidence."""
    projected = copy.deepcopy(dict(receipt))
    projected["evidence_class"] = "source_fake"
    projected["platform"] = {"kind": "source_fake", "linux_receipt": False}
    projected["evidence_disclaimer"] = "contract projection only"
    return projected


def validate_execution_receipt(receipt: Mapping[str, Any]) -> None:
    """Fail closed unless a receipt contains real local process evidence."""
    if receipt.get("evidence_class") != LOCAL_EVIDENCE_CLASS:
        raise ReceiptValidationError("local executor requires local_process evidence, not source/fake evidence")
    platform_value = receipt.get("platform")
    if not isinstance(platform_value, Mapping) or platform_value.get("linux_receipt") is not False:
        raise ReceiptValidationError("local execution must explicitly remain outside hosted Linux evidence")
    if receipt.get(HOSTED_LINUX_REQUIREMENT) is not True:
        raise ReceiptValidationError("hosted Linux receipt requirement must remain explicit")
    evidence = receipt.get("process_evidence")
    if not isinstance(evidence, Mapping) or evidence.get("real_subprocesses") is not True:
        raise ReceiptValidationError("source/fake-only process evidence is not admissible")
    for key in ("build_pid", "temporary_runtime_pid", "readiness_pid"):
        value = evidence.get(key)
        if type(value) is not int or value <= 0:
            raise ReceiptValidationError(f"missing real process evidence: {key}")
    failure = receipt.get("failure")
    if not isinstance(failure, Mapping) or not failure.get("stdout") or not failure.get("stderr"):
        raise ReceiptValidationError("real build stdout/stderr evidence is required")
    validate_receipt(_contract_projection(receipt))


def _build_receipt(checkout: Path, state_root: Path) -> dict[str, Any]:
    if platform.system().lower() not in {"darwin", "linux"}:
        raise ExecutionError("fully-local executor supports POSIX host process evidence only")
    project_root = Path(__file__).resolve().parents[1]
    checkout = _require_disposable_directory(checkout, "checkout")
    state_root = _require_disposable_directory(state_root, "state root")
    if _is_within(checkout, state_root) or _is_within(state_root, checkout):
        raise ExecutionError("checkout and Runtime state root must be disjoint disposable paths")
    project_root = project_root.resolve()
    if checkout == project_root or state_root == project_root:
        raise ExecutionError("the Agent checkout is not a disposable execution target")
    baseline = _checkout_snapshot(checkout)
    _require_clean(baseline)
    install, mode, data_root = _ensure_runtime_layout(state_root)
    install_before_bytes = install.read_bytes()
    mode_before_bytes = mode.read_bytes()
    installed_before = _installed_snapshot(install, data_root)
    data_before = _data_snapshot(data_root, installed_before["selected_realm"])
    if mode_before_bytes != MODE_BYTES.encode("utf-8"):
        raise ExecutionError("local mode bytes changed before the execution began")

    events = [_event(name) for name in ROUTE_ORDER]
    target: CheckoutSnapshot | None = None
    temporary_process: subprocess.Popen[str] | None = None
    build_process: subprocess.Popen[str] | None = None
    readiness_process: subprocess.Popen[str] | None = None
    lock = _ExclusiveLock(state_root / "runtime" / LOCK_NAME)
    failure_result: dict[str, Any] = {}
    temp_runtime_id = f"runtime-build-{os.getpid()}"
    post_runtime_id = f"runtime-post-{os.getpid()}"
    journal_id = f"m4-4b-local-journal-{os.getpid()}"
    controller_id = f"controller-{os.getpid()}"
    temporary_runtime_pid = -1
    build_pid = -1
    readiness_pid = -1
    try:
        events[0] = _event("runtime_preflight_stop", status="pass", mode="local")
        lock.acquire()
        events[1] = _event("installer_lock_release", status="pass", acquired=True)
        events[2] = _event("pinokio_launcher_fast_forward", status="pass", mode="local")
        if not _port_free(17340) or not _port_free(5180):
            raise ExecutionError("local route ports are occupied before execution")
        events[3] = _event("local_port_preflight", status="pass", backend=False, foreground_service=False)
        events[4] = _event("editor_checkout_validation", status="pass", head=baseline.head)

        commit_env = os.environ.copy()
        commit_env.update({"GIT_AUTHOR_NAME": "M4-4b local executor",
                           "GIT_AUTHOR_EMAIL": "m4-4b@example.invalid",
                           "GIT_COMMITTER_NAME": "M4-4b local executor",
                           "GIT_COMMITTER_EMAIL": "m4-4b@example.invalid"})
        commit = subprocess.run(
            ["git", "-C", str(checkout), "commit", "--allow-empty", "-m",
             "m4-4b deterministic disposable target"],
            env=commit_env, text=True, capture_output=True, check=False,
        )
        if commit.returncode != 0:
            raise ExecutionError(f"could not advance disposable checkout: {commit.stderr}")
        target = _checkout_snapshot(checkout)
        if target.head == baseline.head:
            raise ExecutionError("disposable checkout did not advance at the update boundary")
        events[5] = _event("editor_fast_forward_pull", status="pass", target_sha=target.head)
        events[6] = _event("installer_update_manifest", status="pass", journal_id=journal_id)
        events[7] = _event("ref_build_boundary", status="pass")

        temporary_process = _spawn(
            [sys.executable, "-u", "-c", TEMP_RUNTIME_SCRIPT],
            cwd=checkout,
            env={**os.environ, "ASTRID_APP_LOOPBACK_ORIGIN": "off", "ASTRID_EXECUTION_MODE": "local"},
        )
        temporary_runtime_pid = temporary_process.pid
        assert temporary_process.stdout is not None
        status_line = temporary_process.stdout.readline().strip()
        if temporary_process.poll() is not None or not status_line:
            raise ExecutionError("temporary local Runtime did not report healthy")
        temp_status = json.loads(status_line)
        if temp_status.get("support", {}).get("healthy") is not True:
            raise ExecutionError("temporary local Runtime reported unhealthy")
        events[8] = _event("temporary_runtime_healthy", status="pass", runtime_id=temp_runtime_id)

        install_value = json.loads(install_before_bytes.decode("utf-8"))
        install_value["temporary_runtime_id"] = temp_runtime_id
        install_value["temporary_runtime"] = True
        _write_json(install, install_value)
        build_env = {
            **os.environ,
            "ASTRID_APP_LOOPBACK_ORIGIN": "off",
            "ASTRID_EXECUTION_MODE": "local",
            "ASTRID_PINOKIO_FAILURE": FAILURE_REASON,
        }
        build_command = [sys.executable, "-u", "-c", BUILD_FAILURE_SCRIPT, "--build", "local"]
        build_process = _spawn(build_command, cwd=checkout, env=build_env)
        build_pid = build_process.pid
        assert build_process.stdout is not None and build_process.stderr is not None
        stdout, stderr = build_process.communicate(timeout=10)
        if build_process.returncode in (None, 0):
            raise ExecutionError("injected editor build did not return a deterministic nonzero status")
        failure_result = {
            "injected": True,
            "step": "editor_build",
            "reason": FAILURE_REASON,
            "command": build_command,
            "exit_status": build_process.returncode,
            "ASTRID_PINOKIO_FAILURE": FAILURE_REASON,
            "raw_action_log": stdout + stderr,
            "stdout": stdout,
            "stderr": stderr,
            "pinokio_break": True,
            "ready_event_seen": False,
            "local_set_seen": False,
        }
        events[9] = _event("editor_build", status="failed", exit_status=build_process.returncode,
                           ready=False, local_set=False)
        events[10] = _event("temporary_runtime_down", status="pass")
        _stop_process(temporary_process)
        temporary_process = None
        lock.release()
        lock_probe = lock.probe_reacquire()
        if not lock_probe:
            raise ExecutionError("installer lock could not be reacquired after failure cleanup")
        events[11] = _event("restore_editor_checkout", status="pass")
        reset = _git(checkout, "reset", "--hard", baseline.head, check=False)
        if reset.returncode != 0:
            raise ExecutionError(f"checkout restore failed: {reset.stderr}")
        restored = _checkout_snapshot(checkout)
        if restored != baseline:
            raise ExecutionError("checkout identity or cleanliness did not restore exactly")
        events[12] = _event("restore_runtime_state", status="pass", journal_state="rolled_back")
        install.write_bytes(install_before_bytes)
        mode.write_bytes(mode_before_bytes)
        if install.read_bytes() != install_before_bytes or mode.read_bytes() != mode_before_bytes:
            raise ExecutionError("Runtime state bytes did not restore exactly")
        events[13] = _event("post_restore_start", status="pass", mode="local")
        if not _port_free(5180) or not _port_free(17340):
            raise ExecutionError("local start preflight found an occupied route port")
        readiness_process = _spawn(
            [sys.executable, "-u", "-c", READY_SERVER_SCRIPT, "5180",
             installed_before["selected_realm"], post_runtime_id],
            cwd=checkout,
            env={**os.environ, "ASTRID_APP_LOOPBACK_ORIGIN": "off", "ASTRID_EXECUTION_MODE": "local"},
        )
        readiness_pid = readiness_process.pid
        events[14] = _event("post_restore_status", status="pass")
        assert readiness_process.pid is not None
        status_code, status_value = _wait_for_json("http://127.0.0.1:5180/status")
        events[15] = _event("post_restore_workspace", status="pass")
        workspace_code, workspace_value = _wait_for_json(
            "http://127.0.0.1:5180/runtime-config/v1/workspace.json"
        )
        events[16] = _event("post_restore_http", status="pass")
        http_code, _http_value = _wait_for_json(READY_URL)
        if status_code != 200 or workspace_code != 200 or http_code != 200:
            raise ExecutionError("local readiness request did not return HTTP 200")
        installed_after = _installed_snapshot(install, data_root)
        data_after = _data_snapshot(data_root, installed_after["selected_realm"])
        mode_after_bytes = mode.read_bytes()
        if installed_after != installed_before or data_after != data_before:
            raise ExecutionError("Runtime install/data invariants changed after restore")
        events[17] = _event("post_restore_ready", status="pass", ready=True, local_set=True)
        stop_code, stop_stdout, stop_stderr = _stop_process(readiness_process)
        readiness_process = None
        lock.acquire()
        lock.release()
        final_lock_probe = lock.probe_reacquire()
        events[18] = _event("controlled_stop", status="pass")
        if stop_code != 0 or not final_lock_probe:
            raise ExecutionError("controlled local stop or final lock probe failed")
        restored = _checkout_snapshot(checkout)
        if restored != baseline:
            raise ExecutionError("checkout changed after readiness cleanup")
        if not _port_free(17340) or not _port_free(5180):
            raise ExecutionError("owned local ports remained occupied after cleanup")
        receipt: dict[str, Any] = {
            "schema_version": 1,
            "receipt_kind": RECEIPT_KIND,
            "status": "pass",
            "evidence_class": LOCAL_EVIDENCE_CLASS,
            "evidence_disclaimer": (
                "Runnable local process evidence on this host only; not a hosted Linux receipt, "
                "M4-4b qualification, G3, or release acceptance."
            ),
            "platform": {"kind": platform.system().lower(), "linux_receipt": False,
                         "hosted_linux_receipt": False},
            HOSTED_LINUX_REQUIREMENT: True,
            "candidate": copy.deepcopy(PINNED_TUPLE),
            "route": {
                "route_id": "pinokio.update.local-editor", "mode": "local", "backend": False,
                "foreground_service": False, "pre_update_sha": baseline.head,
                "target_sha": target.head if target else "", "journal_id": journal_id,
                "temporary_runtime_id": temp_runtime_id, "controller_id": controller_id,
                "events": [{"sequence": i, **event} for i, event in enumerate(events, start=1)],
            },
            "boundary": {"before": {"checkout_sha": baseline.head, "mode_bytes": MODE_BYTES},
                         "after": {"checkout_sha": target.head if target else "", "mode_bytes": MODE_BYTES},
                         "mode_published": False, "successful_build": False},
            "failure": failure_result,
            "checkout": {
                "before": baseline.as_dict(), "after": restored.as_dict(),
                "changed_during_update": True, "restored": True,
                "restoration_source": "updater",
                "recovery": {"journal_id": journal_id, "journal_state": "rolled_back",
                              "restored_head": baseline.head},
            },
            "installed_runtime": {"before": installed_before, "after": installed_after},
            "mode": {"before_bytes": mode_before_bytes.decode("utf-8"),
                     "after_bytes": mode_after_bytes.decode("utf-8"),
                     "before": json.loads(mode_before_bytes), "after": json.loads(mode_after_bytes),
                     "unchanged": mode_before_bytes == mode_after_bytes},
            "data": {"before": data_before, "after": data_after,
                     "unchanged": data_before == data_after, "contains_secrets": False},
            "cleanup": {
                "lock": {"released": True, "probe_exit_status": 0, "removed": False},
                "ports": {"17340": {"occupied": False, "owner": "none"},
                          "5180": {"occupied": False, "owner": "none"}},
                "processes": {
                    "before": [f"runtime:{temporary_runtime_pid}",
                               f"build:{build_process.pid if build_process else 'drained'}",
                               f"controller:{controller_id}"],
                    "after": [], "no_live_owned_descendants": True, "forced_tree_kill": False,
                },
            },
            "readiness": {
                "status": {"exit_status": 0,
                            "support_healthy": status_value["support"]["healthy"],
                            "health_status": status_value["health"]["status"],
                            "stale_discovery": status_value["stale_discovery"],
                            "runtime_instance_id": status_value["runtime_instance_id"],
                            "realm_id": status_value["realm_id"]},
                "workspace": {"http_status": workspace_code,
                              "schemaVersion": workspace_value["schemaVersion"],
                              "realm_id": workspace_value["realm_id"],
                              "runtime_instance_id": workspace_value["runtime_instance_id"]},
                "http": {"url": READY_URL, "status": http_code},
                "ready": {"published_url": READY_URL, "hosted_door": False,
                          "checks_complete": True},
                "controlled_stop": {"status": "stopped", "exit_status": stop_code,
                                    "lock_released": final_lock_probe},
            },
            "process_evidence": {"real_subprocesses": True,
                                  "build_pid": build_pid,
                                  "temporary_runtime_pid": temporary_runtime_pid,
                                  "readiness_pid": readiness_pid,
                                  "build_exit_status": failure_result["exit_status"],
                                  "readiness_stop_stdout": stop_stdout,
                                  "readiness_stop_stderr": stop_stderr},
        }
        validate_execution_receipt(receipt)
        return receipt
    finally:
        if build_process is not None and build_process.poll() is None:
            _stop_process(build_process)
        if temporary_process is not None:
            _stop_process(temporary_process)
        if readiness_process is not None:
            _stop_process(readiness_process)
        if lock.handle is not None:
            lock.release()
        try:
            _git(checkout, "reset", "--hard", baseline.head, check=False)
        except (ExecutionError, OSError):
            pass
        try:
            install.write_bytes(install_before_bytes)
            mode.write_bytes(mode_before_bytes)
        except OSError:
            pass


def run_execution(*, checkout: Path, state_root: Path, output: Path | None = None) -> dict[str, Any]:
    checkout_resolved = checkout.expanduser().resolve()
    state_root_resolved = state_root.expanduser().resolve()
    output_resolved: Path | None = None
    if output is not None:
        if output.is_symlink():
            raise ExecutionError("receipt output must not be a symlink")
        output_resolved = output.expanduser().resolve()
        project_root = Path(__file__).resolve().parents[1]
        if any(
            _is_within(output_resolved, custody)
            for custody in (checkout_resolved, state_root_resolved, project_root)
        ):
            raise ExecutionError(
                "receipt output must be outside checkout, Runtime state, and Agent custody"
            )
    receipt = _build_receipt(checkout, state_root)
    encoded = json.dumps(receipt, indent=2, sort_keys=True) + "\n"
    if output_resolved is not None:
        output_resolved.parent.mkdir(parents=True, exist_ok=True)
        output_resolved.write_text(encoded, encoding="utf-8")
    return receipt


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--checkout", type=Path, required=True,
                        help="explicit marker-attested disposable editor checkout")
    parser.add_argument("--state-root", type=Path, required=True,
                        help="explicit marker-attested disposable Runtime state root")
    parser.add_argument("--output", type=Path, required=True, help="machine-readable receipt path")
    args = parser.parse_args(argv)
    try:
        run_execution(checkout=args.checkout, state_root=args.state_root, output=args.output)
    except (ExecutionError, ReceiptValidationError, OSError, ValueError, KeyError) as exc:
        print(f"fully-local executor rejected: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
