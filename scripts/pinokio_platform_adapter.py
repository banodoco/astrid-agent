"""Fail-closed OS process and readiness adapter for the Pinokio backend executor.

This module supplies concrete platform observations only. It does not build,
rewrite, or interpret Pinokio action arrays; those remain owned by the backend
executor and its immutable launcher provenance checks.

Local executions are implementation evidence, never hosted-lifecycle receipts.
"""

from __future__ import annotations

import argparse
import copy
import ctypes
import errno
import hashlib
import ipaddress
import json
import os
import re
import signal
import socket
import struct
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
import uuid
from pathlib import Path
from typing import Any, Mapping

HOSTED_RUNNERS = {
    "linux": ("ubuntu-24.04", "Linux", "ubuntu24"),
    "macos": ("macos-14", "macOS", "macos14"),
    "windows": ("windows-2022", "Windows", "win22"),
}
PLATFORM_NAMES = {"linux": "linux", "darwin": "macos", "win32": "windows"}
OPERATIONS = (
    "ready",
    "status",
    "health",
    "menu",
    "processes",
    "stop",
    "start",
    "busy-refusal",
    "unknown-refusal",
    "recovery",
    "identity-mismatch",
    "instance-mismatch",
    "adopt",
)
SHA256 = re.compile(r"^[0-9a-f]{64}$")
RUNNER_IDENTITY_ENV = ("RUNNER_OS", "ImageOS", "ImageVersion")
PROCESS_ENV = ("PATH", "SYSTEMROOT", "WINDIR", "TEMP", "TMP", "HOME", "USERPROFILE", "LANG")


class AdapterError(RuntimeError):
    """Raised whenever a required platform fact cannot be observed safely."""


def _sha(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _need_text(value: Any, label: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise AdapterError(f"{label} must be a non-empty string")
    return value


def _load_spec(path: Path) -> dict[str, Any]:
    if not path.is_absolute() or path.is_symlink() or not path.is_file():
        raise AdapterError("spec path must be an absolute regular file")
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise AdapterError(f"cannot read adapter spec: {exc}") from exc
    if not isinstance(value, dict):
        raise AdapterError("adapter spec must be a JSON object")
    return value


def validate_runner_identity(
    platform: Mapping[str, Any],
    *,
    observed_platform: str | None = None,
    env: Mapping[str, str] | None = None,
    powershell_version: str | None = None,
) -> dict[str, Any]:
    """Validate declared OS and hosted runner against independently observed values."""
    env = os.environ if env is None else env
    os_name = _need_text(platform.get("os"), "platform.os").lower()
    if os_name not in HOSTED_RUNNERS:
        raise AdapterError("platform.os must be linux, macos, or windows")
    observed_platform = sys.platform if observed_platform is None else observed_platform
    actual = PLATFORM_NAMES.get(observed_platform)
    if actual != os_name:
        raise AdapterError(f"declared OS {os_name} does not match observed platform {actual}")
    hosted = platform.get("hosted")
    if not isinstance(hosted, bool):
        raise AdapterError("platform.hosted must be boolean")
    runner = _need_text(platform.get("runner"), "platform.runner")
    provenance: dict[str, str] = {}
    if hosted:
        expected_runner, expected_os, expected_image = HOSTED_RUNNERS[os_name]
        if runner != expected_runner:
            raise AdapterError(f"hosted {os_name} requires runner {expected_runner}")
        provenance = {
            key: str(env.get(key, "")) for key in ("RUNNER_OS", "ImageOS", "ImageVersion")
        }
        if (
            provenance["RUNNER_OS"] != expected_os
            or provenance["ImageOS"] != expected_image
            or not provenance["ImageVersion"].strip()
        ):
            raise AdapterError("hosted runner provenance is absent or mismatched")
    provenance["observed_platform"] = observed_platform
    result: dict[str, Any] = {
        "os": os_name,
        "runner": runner,
        "hosted": hosted,
        "evidence_class": "hosted-lifecycle" if hosted else "executable-boundary",
        "runner_provenance": provenance,
    }
    if os_name == "windows":
        windows = platform.get("windows_powershell")
        if windows is not None:
            if not isinstance(windows, Mapping):
                raise AdapterError("platform.windows_powershell must be an object")
            required = windows.get("required_version_prefix")
            if hosted and required is None:
                raise AdapterError("hosted Windows requires a PowerShell 5.1 version requirement")
            if required is not None:
                required = _need_text(required, "windows_powershell.required_version_prefix")
                executable = _need_text(
                    windows.get("executable", "powershell.exe"), "windows_powershell.executable"
                )
                if hosted and (
                    required != "5.1" or Path(executable).name.lower() != "powershell.exe"
                ):
                    raise AdapterError(
                        "hosted Windows requires Windows PowerShell 5.1 via powershell.exe"
                    )
                if not powershell_version:
                    powershell_version = observe_powershell_version(executable)
                if not powershell_version.startswith(required):
                    raise AdapterError("observed PowerShell version does not meet required prefix")
            if powershell_version:
                result["powershell_version"] = powershell_version
                provenance["powershell_version"] = powershell_version
        elif hosted:
            raise AdapterError(
                "hosted Windows requires an expressible PowerShell version requirement"
            )
    return result


def observe_powershell_version(executable: str = "powershell.exe") -> str:
    """Observe Windows PowerShell version using explicit argv and no shell wrapper."""
    if sys.platform != "win32":
        raise AdapterError("PowerShell version can only be observed on Windows")
    argv = [
        executable,
        "-NoLogo",
        "-NoProfile",
        "-NonInteractive",
        "-Command",
        "$PSVersionTable.PSVersion.ToString()",
    ]
    try:
        result = subprocess.run(
            argv, capture_output=True, text=True, timeout=15, shell=False, check=False
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise AdapterError(f"PowerShell version observation failed: {exc}") from exc
    version = result.stdout.strip()
    if result.returncode or not version:
        raise AdapterError("PowerShell version observation was empty or unsuccessful")
    return version


def build_executor_commands(
    spec_path: Path | str,
    *,
    cwd: Path | str | None = None,
    env: Mapping[str, str] | None = None,
    timeout_seconds: int = 30,
) -> dict[str, Any]:
    """Return backend-executor-compatible runtime and negative-control commands."""
    spec_path = Path(spec_path)
    if spec_path.is_symlink():
        raise AdapterError("spec_path must not be a symlink")
    spec_path = spec_path.resolve(strict=True)
    if spec_path.is_symlink() or not spec_path.is_file():
        raise AdapterError("spec_path must resolve to a regular file")
    cwd_path = Path(cwd).resolve(strict=True) if cwd is not None else spec_path.parent
    if not cwd_path.is_dir():
        raise AdapterError("cwd must be a directory")
    if timeout_seconds <= 0:
        raise AdapterError("timeout_seconds must be positive")
    raw_spec = _load_spec(spec_path)
    platform_spec = raw_spec.get("platform")
    instances = raw_spec.get("instance_ids")
    identity = _need_text(raw_spec.get("identity"), "identity")
    if (
        not isinstance(platform_spec, Mapping)
        or platform_spec.get("os") not in HOSTED_RUNNERS
        or not isinstance(instances, list)
        or len(instances) < 2
        or not all(isinstance(item, str) and item.strip() for item in instances)
        or len(set(instances)) != len(instances)
    ):
        raise AdapterError("spec must declare platform.os, identity, and two distinct instance_ids")
    guard_paths = [str(spec_path)]
    explicit_guards = raw_spec.get("guard_paths", [])
    if not isinstance(explicit_guards, list):
        raise AdapterError("guard_paths must be a list of explicit absolute paths")
    for raw_path in explicit_guards:
        if not isinstance(raw_path, str) or not raw_path:
            raise AdapterError("guard_paths entries must be non-empty absolute paths")
        path = Path(raw_path)
        if not path.is_absolute() or path.is_symlink():
            raise AdapterError("guard_paths entries must be absolute non-symlink paths")
        guard_paths.append(str(path.resolve(strict=True)))
    explicit_env = dict(env or {})
    if not all(isinstance(k, str) and isinstance(v, str) for k, v in explicit_env.items()):
        raise AdapterError("env must contain string keys and values")
    identity_overrides = sorted(set(explicit_env) & set(RUNNER_IDENTITY_ENV))
    if identity_overrides:
        raise AdapterError(
            "runner identity variables must be inherited from the observed runner, not overridden: "
            + ", ".join(identity_overrides)
        )
    module = Path(__file__).resolve(strict=True)

    def command(operation: str) -> dict[str, Any]:
        if operation not in OPERATIONS:
            raise AdapterError(f"unsupported operation: {operation}")
        return {
            "id": f"pinokio-platform.{operation}",
            "argv": [sys.executable, str(module), operation, "--spec", str(spec_path)],
            "cwd": str(cwd_path),
            "env": explicit_env,
            "inherit_env": [*RUNNER_IDENTITY_ENV, *PROCESS_ENV],
            "redact_env": [],
            "redact_argv_indices": [],
            "timeout_seconds": timeout_seconds,
        }

    commands = {
        name: command(name)
        for name in ("ready", "status", "health", "menu", "processes", "stop", "start")
    }
    controls = {
        "busy_stop": {
            "refusal": command("busy-refusal"),
            "recovery": command("recovery"),
            "expected_reason": "busy",
            "guard_paths": guard_paths,
        },
        "unknown_stop": {
            "refusal": command("unknown-refusal"),
            "recovery": command("recovery"),
            "expected_reason": "unknown",
            "guard_paths": guard_paths,
        },
        "identity_mismatch": {"probe": command("identity-mismatch")},
        "instance_mismatch": {"probe": command("instance-mismatch")},
    }
    return {
        "runtime": {
            **commands,
            "expected_identity": identity,
            "expected_instance_id": instances[0],
            "expected_restart_instance_id": instances[1],
        },
        "negative_controls": controls,
    }


def _validate_spec(spec: Mapping[str, Any]) -> dict[str, Any]:
    identity = _need_text(spec.get("identity"), "identity")
    instances = spec.get("instance_ids")
    if (
        not isinstance(instances, list)
        or len(instances) < 2
        or not all(isinstance(item, str) and item.strip() for item in instances)
    ):
        raise AdapterError("instance_ids must contain at least two distinct non-empty strings")
    if len(set(instances)) != len(instances):
        raise AdapterError("instance_ids must be distinct")
    allowed_sessions = {(identity, item) for item in instances}
    preexisting = spec.get("preexisting_session")
    if preexisting is not None:
        if not isinstance(preexisting, Mapping):
            raise AdapterError("preexisting_session must be an object")
        preexisting_identity = _need_text(
            preexisting.get("identity"), "preexisting_session.identity"
        )
        preexisting_instance = _need_text(
            preexisting.get("instance_id"), "preexisting_session.instance_id"
        )
        if (preexisting_identity, preexisting_instance) in allowed_sessions:
            raise AdapterError("preexisting_session must be distinct from candidate sessions")
        allowed_sessions.add((preexisting_identity, preexisting_instance))
    argv = spec.get("runtime_argv")
    if (
        not isinstance(argv, list)
        or not argv
        or not all(isinstance(item, str) and item for item in argv)
    ):
        raise AdapterError("runtime_argv must be a non-empty explicit argv list")
    executable = Path(argv[0])
    if not executable.is_absolute() or executable.is_symlink() or not executable.is_file():
        raise AdapterError("runtime_argv[0] must be an absolute regular non-symlink executable")
    runtime_executable = Path(spec.get("runtime_executable_path", argv[0]))
    if (
        not runtime_executable.is_absolute()
        or runtime_executable.is_symlink()
        or not runtime_executable.is_file()
    ):
        raise AdapterError("runtime_executable_path must be an absolute regular non-symlink file")
    cwd = Path(_need_text(spec.get("runtime_cwd"), "runtime_cwd"))
    if not cwd.is_absolute() or not cwd.is_dir() or cwd.is_symlink():
        raise AdapterError("runtime_cwd must be an existing absolute non-symlink directory")
    host = _need_text(spec.get("host", "127.0.0.1"), "host")
    try:
        if not ipaddress.ip_address(host).is_loopback:
            raise AdapterError("host must be a loopback IP address")
    except ValueError as exc:
        raise AdapterError("host must be a loopback IP address") from exc
    port = spec.get("port")
    if not isinstance(port, int) or isinstance(port, bool) or not 1 <= port <= 65535:
        raise AdapterError("port must be an integer in 1..65535")
    paths: dict[str, str] = {}
    for key in ("status_path", "health_path", "menu_path"):
        path = _need_text(spec.get(key), key)
        if not path.startswith("/") or path.startswith("//") or "\r" in path or "\n" in path:
            raise AdapterError(f"{key} must be an absolute HTTP path")
        paths[key] = path
    negative_paths = spec.get("negative_control_paths")
    if (
        not isinstance(negative_paths, Mapping)
        or not all(
            isinstance(negative_paths.get(key), str)
            and negative_paths[key].startswith("/")
            and not negative_paths[key].startswith("//")
            and "\r" not in negative_paths[key]
            and "\n" not in negative_paths[key]
            for key in ("identity_mismatch", "instance_mismatch")
        )
        or negative_paths["identity_mismatch"] == negative_paths["instance_mismatch"]
    ):
        raise AdapterError(
            "negative_control_paths must provide distinct observed HTTP paths for both mismatches"
        )
    state_path = Path(_need_text(spec.get("state_path"), "state_path"))
    if not state_path.is_absolute() or state_path.is_symlink():
        raise AdapterError("state_path must be absolute and non-symlinked")
    platform = spec.get("platform")
    if not isinstance(platform, Mapping):
        raise AdapterError("platform is required")
    provenance = validate_runner_identity(platform)
    env = spec.get("runtime_env", {})
    if not isinstance(env, Mapping) or not all(
        isinstance(k, str) and isinstance(v, str) for k, v in env.items()
    ):
        raise AdapterError("runtime_env must be a string-to-string object")
    controls = spec.get("stop_controls")
    if not isinstance(controls, Mapping):
        raise AdapterError("stop_controls must configure busy and unknown commands")
    parsed_controls: dict[str, dict[str, Any]] = {}
    for reason in ("busy", "unknown"):
        control = controls.get(reason)
        if not isinstance(control, Mapping):
            raise AdapterError(f"stop_controls.{reason} must be an object")
        control_argv = control.get("argv")
        if (
            not isinstance(control_argv, list)
            or not control_argv
            or not all(isinstance(part, str) and part for part in control_argv)
        ):
            raise AdapterError(f"stop_controls.{reason}.argv must be an explicit argv list")
        control_exe = Path(control_argv[0])
        if not control_exe.is_absolute() or control_exe.is_symlink() or not control_exe.is_file():
            raise AdapterError(
                f"stop_controls.{reason}.argv[0] must be an absolute regular non-symlink file"
            )
        control_cwd = Path(_need_text(control.get("cwd"), f"stop_controls.{reason}.cwd"))
        if not control_cwd.is_absolute() or not control_cwd.is_dir() or control_cwd.is_symlink():
            raise AdapterError(
                f"stop_controls.{reason}.cwd must be an absolute non-symlink directory"
            )
        control_env = control.get("env", {})
        if not isinstance(control_env, Mapping) or not all(
            isinstance(k, str) and isinstance(v, str) for k, v in control_env.items()
        ):
            raise AdapterError(f"stop_controls.{reason}.env must contain string keys and values")
        expected = control.get("expected_exit_code")
        if not isinstance(expected, int) or isinstance(expected, bool) or expected == 0:
            raise AdapterError(
                f"stop_controls.{reason}.expected_exit_code must be a nonzero integer"
            )
        parsed_controls[reason] = {
            "argv": list(control_argv),
            "cwd": control_cwd,
            "env": dict(control_env),
            "expected_exit_code": expected,
            "reason": _need_text(control.get("reason"), f"stop_controls.{reason}.reason"),
            "stage": _need_text(control.get("stage"), f"stop_controls.{reason}.stage"),
        }
        if parsed_controls[reason]["reason"] != reason:
            raise AdapterError(f"stop_controls.{reason}.reason must be exactly {reason!r}")
    guard_paths = spec.get("guard_paths", [])
    if not isinstance(guard_paths, list):
        raise AdapterError("guard_paths must be a list")
    checked_guards: list[Path] = []
    for raw_path in guard_paths:
        if not isinstance(raw_path, str) or not raw_path:
            raise AdapterError("guard_paths entries must be non-empty absolute paths")
        guard = Path(raw_path)
        if not guard.is_absolute() or guard.is_symlink():
            raise AdapterError("guard_paths entries must be absolute non-symlink paths")
        checked_guards.append(guard.resolve(strict=True))
    start_timeout = spec.get("start_timeout_seconds", 10)
    if (
        not isinstance(start_timeout, (int, float))
        or isinstance(start_timeout, bool)
        or start_timeout <= 0
    ):
        raise AdapterError("start_timeout_seconds must be positive")
    return {
        "identity": identity,
        "instance_ids": list(instances),
        "allowed_sessions": allowed_sessions,
        "runtime_argv": list(argv),
        "runtime_executable_path": os.path.normcase(os.path.realpath(runtime_executable)),
        "runtime_cwd": cwd,
        "host": host,
        "port": port,
        **paths,
        "stop_controls": parsed_controls,
        "guard_paths": checked_guards,
        "negative_control_paths": dict(negative_paths),
        "state_path": state_path,
        "platform": provenance,
        "runtime_env": dict(env),
        "start_timeout_seconds": float(start_timeout),
    }


def _write_state(path: Path, state: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = json.dumps(dict(state), sort_keys=True, separators=(",", ":")) + "\n"
    fd, temporary = tempfile.mkstemp(prefix=path.name + ".", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
        if os.name == "posix":
            os.chmod(temporary, 0o600)
        os.replace(temporary, path)
    except BaseException:
        try:
            os.unlink(temporary)
        except OSError:
            pass
        raise


def _read_state(path: Path) -> dict[str, Any] | None:
    if not path.exists():
        return None
    if path.is_symlink() or not path.is_file():
        raise AdapterError("state file is not a regular non-symlink file")
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise AdapterError(f"state file is malformed or unreadable: {exc}") from exc
    required = {
        "active",
        "pid",
        "birth",
        "pgid",
        "executable_path",
        "identity",
        "runtime_instance_id",
        "argv_digest",
        "controller_token_digest",
        "host",
        "port",
        "platform_identity",
    }
    if not isinstance(value, dict) or not required.issubset(value):
        raise AdapterError("state file lacks required process identity fields")
    if not isinstance(value["active"], bool):
        raise AdapterError("state active field is malformed")
    return value


def _validate_state_for_spec(state: Mapping[str, Any] | None, spec: Mapping[str, Any]) -> None:
    if state is None:
        return
    expected_argv_digest = _sha(json.dumps(spec["runtime_argv"], separators=(",", ":")).encode())
    expected_executable = spec["runtime_executable_path"]
    if (
        not isinstance(state.get("pid"), int)
        or isinstance(state.get("pid"), bool)
        or state["pid"] <= 1
        or not isinstance(state.get("birth"), str)
        or not state["birth"]
        or state.get("executable_path") != expected_executable
        or (state.get("identity"), state.get("runtime_instance_id")) not in spec["allowed_sessions"]
        or state.get("argv_digest") != expected_argv_digest
        or state.get("controller_token_digest") is None
        or not isinstance(state.get("controller_token_digest"), str)
        or not SHA256.fullmatch(state["controller_token_digest"])
        or state.get("host") != spec["host"]
        or state.get("port") != spec["port"]
        or state.get("platform_identity") != spec["platform"]
        or state.get("evidence_class") != spec["platform"]["evidence_class"]
        or not isinstance(state.get("start_count"), int)
        or state["start_count"] < 1
    ):
        raise AdapterError(
            "state file does not authenticate to this candidate, argv, runner, and endpoint"
        )
    if spec["platform"]["os"] == "windows":
        if state.get("pgid") is not None:
            raise AdapterError("Windows process state has an invalid process-group identity")
    elif state.get("pgid") != state["pid"]:
        raise AdapterError("POSIX process group does not match the recorded owned process")


def _birth(pid: int, os_name: str) -> str:
    if os_name == "linux":
        try:
            raw = Path(f"/proc/{pid}/stat").read_text(encoding="ascii")
        except OSError as exc:
            raise AdapterError(f"cannot observe Linux process incarnation: {exc}") from exc
        birth, _pgid = _parse_linux_process_info(raw)
        return birth
    if os_name == "macos":
        _observed_pid, _pgid, seconds, micros = _mac_process_info(pid)
        return f"{seconds}.{micros:06d}"
    script = (
        "$p=Get-CimInstance Win32_Process -Filter 'ProcessId = " + str(pid) + "'; "
        "if ($p) { $p.CreationDate.ToUniversalTime().ToString('o') }"
    )
    result = _run_observation(
        ["powershell.exe", "-NoLogo", "-NoProfile", "-NonInteractive", "-Command", script],
        timeout=15,
    )
    value = result.stdout.strip()
    if result.returncode or not value:
        raise AdapterError("Windows CIM process birth observation failed")
    return value


def _parse_mac_process_info(raw: bytes, pid: int) -> tuple[int, int, int, int]:
    if len(raw) < 136:
        raise AdapterError("macOS libproc process info buffer is too small")
    observed_pid = struct.unpack_from("=I", raw, 12)[0]
    pgid = struct.unpack_from("=I", raw, 100)[0]
    seconds = struct.unpack_from("=Q", raw, 120)[0]
    micros = struct.unpack_from("=Q", raw, 128)[0]
    if observed_pid != pid or seconds <= 0 or micros >= 1_000_000:
        raise AdapterError("macOS libproc returned inconsistent process identity")
    return observed_pid, pgid, seconds, micros


def _mac_process_info(pid: int) -> tuple[int, int, int, int]:
    """Read PID, process group, and birth time through libproc (no shell/tool)."""
    try:
        libproc = ctypes.CDLL("/usr/lib/libproc.dylib", use_errno=True)
        proc_pidinfo = libproc.proc_pidinfo
    except (OSError, AttributeError) as exc:
        raise AdapterError(f"macOS libproc process observation unavailable: {exc}") from exc
    proc_pidinfo.argtypes = [
        ctypes.c_int,
        ctypes.c_int,
        ctypes.c_uint64,
        ctypes.c_void_p,
        ctypes.c_int,
    ]
    proc_pidinfo.restype = ctypes.c_int
    buffer = ctypes.create_string_buffer(256)
    ctypes.set_errno(0)
    size = proc_pidinfo(pid, 3, 0, buffer, len(buffer))  # PROC_PIDTBSDINFO
    if size < 136:
        if ctypes.get_errno() == 3:  # ESRCH: the kernel confirms this process is gone
            raise AdapterError("macOS process is absent")
        raise AdapterError("macOS libproc process birth/group observation failed")
    return _parse_mac_process_info(buffer.raw[:size], pid)


def _linux_process_info(pid: int) -> tuple[str, int]:
    try:
        raw = Path(f"/proc/{pid}/stat").read_text(encoding="ascii")
    except OSError as exc:
        raise AdapterError(f"cannot observe Linux process identity: {exc}") from exc
    return _parse_linux_process_info(raw)


def _parse_linux_process_info(raw: str) -> tuple[str, int]:
    """Return Linux proc start time and process group from one stat row."""
    closing = raw.rfind(")")
    fields = raw[closing + 2 :].split() if closing >= 0 else []
    if len(fields) < 20:
        raise AdapterError("Linux process stat was malformed")
    try:
        return fields[19], int(fields[2])  # starttime (field 22), pgrp (field 5)
    except (ValueError, IndexError) as exc:
        raise AdapterError("Linux process stat identity fields were malformed") from exc


def _observed_executable(pid: int, os_name: str) -> str:
    """Observe a live process executable without trusting configured argv."""
    if os_name == "linux":
        try:
            value = os.readlink(f"/proc/{pid}/exe")
        except OSError as exc:
            raise AdapterError(f"cannot observe Linux process executable: {exc}") from exc
    elif os_name == "macos":
        try:
            libproc = ctypes.CDLL("/usr/lib/libproc.dylib", use_errno=True)
            proc_pidpath = libproc.proc_pidpath
        except (OSError, AttributeError) as exc:
            raise AdapterError(f"macOS executable observation unavailable: {exc}") from exc
        proc_pidpath.argtypes = [ctypes.c_int, ctypes.c_void_p, ctypes.c_uint32]
        proc_pidpath.restype = ctypes.c_int
        buffer = ctypes.create_string_buffer(4096)
        ctypes.set_errno(0)
        size = proc_pidpath(pid, buffer, len(buffer))
        if size <= 0:
            raise AdapterError("macOS process executable observation failed")
        value = os.fsdecode(buffer.value)
    else:
        script = (
            "$p=Get-CimInstance Win32_Process -Filter 'ProcessId = " + str(pid) + "'; "
            "if ($p) { $p.ExecutablePath }"
        )
        result = _run_observation(
            [
                "powershell.exe",
                "-NoLogo",
                "-NoProfile",
                "-NonInteractive",
                "-Command",
                script,
            ],
            timeout=15,
        )
        value = result.stdout.strip()
        if result.returncode or not value:
            raise AdapterError("Windows process executable observation failed")
    if not value:
        raise AdapterError("process executable observation was empty")
    return os.path.normcase(os.path.realpath(value))


def _wait_expected_executable(pid: int, os_name: str, expected: str, timeout: float = 3.0) -> str:
    """Wait for a just-spawned interpreter or launcher to finish its exec."""
    deadline = time.monotonic() + timeout
    last_observed: str | None = None
    last_error: AdapterError | None = None
    while True:
        if not _pid_alive(pid):
            raise AdapterError("Runtime exited before its executable could be authenticated")
        try:
            last_observed = _observed_executable(pid, os_name)
            last_error = None
            if last_observed == expected:
                return last_observed
        except AdapterError as exc:
            last_error = exc
        if time.monotonic() >= deadline:
            if last_error is not None:
                raise AdapterError(
                    f"Runtime executable could not be authenticated: {last_error}"
                ) from last_error
            raise AdapterError(
                f"Runtime executable does not match configured path (observed {last_observed!r})"
            )
        time.sleep(0.02)


def _run_observation(argv: list[str], *, timeout: int) -> subprocess.CompletedProcess[str]:
    if not argv or not all(isinstance(part, str) and part for part in argv):
        raise AdapterError("platform observation argv is invalid")
    try:
        return subprocess.run(
            argv, capture_output=True, text=True, timeout=timeout, shell=False, check=False
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise AdapterError(f"platform observation failed: {exc}") from exc


def _pid_alive(pid: int) -> bool:
    if not isinstance(pid, int) or pid <= 1:
        raise AdapterError("recorded PID is invalid")
    if sys.platform == "darwin":
        try:
            _mac_process_info(pid)
            return True
        except AdapterError as exc:
            if "process is absent" in str(exc):
                return False
            raise
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError as exc:
        raise AdapterError("cannot observe recorded process: permission denied") from exc
    except OSError as exc:
        # Windows reports a missing PID through WinError 87/6 in some Python builds.
        if sys.platform == "win32" and getattr(exc, "winerror", None) in {6, 87}:
            return False
        raise AdapterError(f"cannot observe recorded process: {exc}") from exc


def _process_matches(state: Mapping[str, Any], os_name: str) -> bool:
    alive = _pid_alive(state["pid"])
    if not alive:
        return False
    if _birth(state["pid"], os_name) != state["birth"]:
        raise AdapterError("recorded PID has been reused or its birth identity changed")
    if _observed_executable(state["pid"], os_name) != state["executable_path"]:
        raise AdapterError("recorded process executable changed or does not match configured argv")
    if os_name == "linux":
        _birth_token, pgid = _linux_process_info(state["pid"])
        if pgid != state["pgid"]:
            raise AdapterError("cannot authenticate the recorded process group")
    elif os_name == "macos":
        _observed_pid, pgid, _seconds, _micros = _mac_process_info(state["pid"])
        if pgid != state["pgid"]:
            raise AdapterError("cannot authenticate the recorded process group")
    return True


def _parse_linux_listening_inodes(raw: str, port: int) -> set[str]:
    """Extract LISTEN socket inodes for a TCP port from one proc net table."""
    expected_port = f"{port:04X}"
    result: set[str] = set()
    for line in raw.splitlines()[1:]:
        fields = line.split()
        if len(fields) < 10:
            continue
        local = fields[1].rsplit(":", 1)
        if len(local) == 2 and local[1].upper() == expected_port and fields[3] == "0A":
            result.add(fields[9])
    return result


def _linux_listener_owned_by(pid: int, port: int) -> bool:
    try:
        socket_inodes = {
            match.group(1)
            for fd in Path(f"/proc/{pid}/fd").iterdir()
            if (match := re.fullmatch(r"socket:\[(\d+)\]", os.readlink(fd))) is not None
        }
        listening: set[str] = set()
        for table in (Path("/proc/net/tcp"), Path("/proc/net/tcp6")):
            listening.update(_parse_linux_listening_inodes(table.read_text(encoding="ascii"), port))
    except FileNotFoundError:
        return False
    except (OSError, PermissionError) as exc:
        raise AdapterError(f"cannot observe Linux listener ownership: {exc}") from exc
    return bool(socket_inodes & listening)


def _listener_owned_by(pid: int, host: str, port: int, os_name: str) -> bool:
    """Prove the configured listener belongs to the recorded process PID."""
    if os_name == "linux":
        return _linux_listener_owned_by(pid, port)
    if os_name == "macos":
        executable = Path("/usr/sbin/lsof")
        if not executable.is_file() or executable.is_symlink():
            raise AdapterError("macOS lsof listener-owner observation is unavailable")
        result = _run_observation(
            [
                str(executable),
                "-nP",
                "-a",
                "-p",
                str(pid),
                f"-iTCP:{port}",
                "-sTCP:LISTEN",
                "-Fpn",
            ],
            timeout=15,
        )
        if result.returncode == 1 and not result.stdout.strip():
            return False
        if result.returncode:
            raise AdapterError(f"macOS listener-owner observation failed: {result.stderr.strip()}")
        rows = result.stdout.splitlines()
        return f"p{pid}" in rows and any(
            row.startswith("n") and row.rsplit(":", 1)[-1] == str(port) for row in rows
        )
    script = (
        "$c=Get-NetTCPConnection -State Listen -LocalPort "
        + str(port)
        + " -ErrorAction Stop | Where-Object { $_.OwningProcess -eq "
        + str(pid)
        + " }; "
        "if ($c) { $c | Select-Object OwningProcess,LocalAddress,LocalPort,State "
        "| ConvertTo-Json -Compress }"
    )
    result = _run_observation(
        [
            "powershell.exe",
            "-NoLogo",
            "-NoProfile",
            "-NonInteractive",
            "-Command",
            script,
        ],
        timeout=15,
    )
    if result.returncode:
        raise AdapterError(f"Windows listener-owner observation failed: {result.stderr.strip()}")
    if not result.stdout.strip():
        return False
    try:
        payload = json.loads(result.stdout)
    except json.JSONDecodeError as exc:
        raise AdapterError("Windows listener-owner observation returned malformed JSON") from exc
    rows = payload if isinstance(payload, list) else [payload]
    return any(
        isinstance(row, Mapping)
        and row.get("OwningProcess") == pid
        and row.get("LocalPort") == port
        and str(row.get("State", "")).lower() == "listen"
        for row in rows
    )


def _port_free(host: str, port: int) -> bool:
    family = socket.AF_INET6 if ":" in host else socket.AF_INET
    sock = socket.socket(family, socket.SOCK_STREAM)
    try:
        # Permit rebinding an exited listener's TIME_WAIT tuple while still
        # refusing an active bind. The disposable and Runtime listeners set
        # SO_REUSEADDR too; SO_REUSEPORT is intentionally never enabled.
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        sock.bind((host, port))
        return True
    except OSError as exc:
        if exc.errno in {98, 48, 10048}:
            # An exited TCP listener can retain its accepted connection in
            # TIME_WAIT even though no service owns the port. A refused local
            # connect proves there is no listening endpoint; a responding or
            # unobservable endpoint remains occupied/fail-closed.
            probe = socket.socket(family, socket.SOCK_STREAM)
            probe.settimeout(0.5)
            try:
                code = probe.connect_ex((host, port))
            except OSError as probe_error:
                raise AdapterError(
                    f"cannot observe occupied port {host}:{port}: {probe_error}"
                ) from probe_error
            finally:
                probe.close()
            if code in {errno.ECONNREFUSED, 10061}:
                return True
            if code == 0:
                return False
            raise AdapterError(
                f"cannot prove port {host}:{port} free (connect error {code})"
            ) from exc
        raise AdapterError(f"cannot observe port {host}:{port} (errno {exc.errno}): {exc}") from exc
    finally:
        sock.close()


def _wait_port_free(host: str, port: int, timeout: float = 3.0) -> bool:
    deadline = time.monotonic() + timeout
    while True:
        if _port_free(host, port):
            return True
        if time.monotonic() >= deadline:
            return False
        time.sleep(0.05)


def _http(
    spec: Mapping[str, Any], state: Mapping[str, Any], path: str | None = None
) -> tuple[int, dict[str, Any]]:
    path = spec["health_path"] if path is None else path
    url = f"http://{spec['host']}:{spec['port']}{path}"
    request = urllib.request.Request(url, headers={"Accept": "application/json"})
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    try:
        with opener.open(request, timeout=3) as response:
            status = response.status
            payload = json.loads(response.read().decode("utf-8"))
    except (
        OSError,
        urllib.error.URLError,
        urllib.error.HTTPError,
        json.JSONDecodeError,
        UnicodeDecodeError,
    ) as exc:
        raise AdapterError(f"HTTP readiness observation failed: {exc}") from exc
    if not isinstance(payload, dict):
        raise AdapterError("HTTP health response must be a JSON object")
    if payload.get("runtime_instance_id") is None and payload.get("instance_id") is not None:
        payload["runtime_instance_id"] = payload["instance_id"]
    return status, payload


def _observe_endpoint(
    spec: Mapping[str, Any], state: Mapping[str, Any], operation: str
) -> tuple[int, dict[str, Any]]:
    path_key = {"status": "status_path", "health": "health_path", "menu": "menu_path"}[operation]
    status, payload = _http(spec, state, spec[path_key])
    if (
        status != 200
        or payload.get("identity") != state["identity"]
        or payload.get("runtime_instance_id") != state["runtime_instance_id"]
    ):
        raise AdapterError(
            f"{operation} endpoint did not observe the recorded identity and instance"
        )
    if operation == "status":
        support = payload.get("support")
        if not isinstance(support, Mapping) or support.get("healthy") is not True:
            raise AdapterError("status endpoint did not observe support.healthy true")
    elif operation == "health":
        install = payload.get("install")
        if (
            payload.get("status") not in {"ok", "healthy"}
            or payload.get("protocol") != "workspace.v1"
            or not isinstance(install, Mapping)
            or install.get("managed_by") != "pinokio"
        ):
            raise AdapterError("health endpoint did not observe required runtime health fields")
    elif payload.get("open_astrid") is not True:
        raise AdapterError("menu endpoint did not observe open_astrid true")
    return status, payload


def _run_stop_control(
    spec: Mapping[str, Any], state: Mapping[str, Any], reason: str
) -> tuple[dict[str, Any], int]:
    control = spec["stop_controls"][reason]
    before_state = spec["state_path"].read_bytes()
    before_digest = _sha(before_state)
    if not state["active"] or not _process_matches(state, spec["platform"]["os"]):
        raise AdapterError(f"cannot prove a live same-session refusal for {reason}")
    if not _listener_owned_by(state["pid"], state["host"], state["port"], spec["platform"]["os"]):
        raise AdapterError(f"cannot bind {reason} refusal session to its listener owner")
    _observe_endpoint(spec, state, "health")
    inherited_names = ("PATH", "SYSTEMROOT", "WINDIR", "TEMP", "TMP", "HOME", "USERPROFILE", "LANG")
    env = {key: os.environ[key] for key in inherited_names if key in os.environ}
    env.update(control["env"])
    try:
        result = subprocess.run(
            control["argv"],
            cwd=str(control["cwd"]),
            env=env,
            capture_output=True,
            text=True,
            timeout=30,
            shell=False,
            check=False,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise AdapterError(
            f"configured {reason} refusal command could not be observed: {exc}"
        ) from exc
    captured = (result.stdout or "") + "\n" + (result.stderr or "")
    if result.returncode != control["expected_exit_code"] or control["reason"] not in captured:
        raise AdapterError(
            f"configured {reason} refusal did not produce its declared nonzero code and reason"
        )
    after_state = spec["state_path"].read_bytes()
    if _sha(after_state) != before_digest or after_state != before_state:
        raise AdapterError(f"configured {reason} refusal mutated adapter state")
    current = _read_state(spec["state_path"])
    _validate_state_for_spec(current, spec)
    if current != state or not _process_matches(current, spec["platform"]["os"]):
        raise AdapterError(f"configured {reason} refusal changed the owned process incarnation")
    if not _listener_owned_by(
        current["pid"], current["host"], current["port"], spec["platform"]["os"]
    ):
        raise AdapterError(f"configured {reason} refusal changed listener ownership")
    _observe_endpoint(spec, current, "health")
    command_digest = _sha(
        json.dumps(
            {"argv": control["argv"], "cwd": str(control["cwd"]), "env": control["env"]},
            sort_keys=True,
            separators=(",", ":"),
        ).encode()
    )
    return {
        "outcome": "refused",
        "reason": reason,
        "stage": control["stage"],
        "expected_exit_code": control["expected_exit_code"],
        "observed_exit_code": result.returncode,
        "command_digest": command_digest,
        "cwd": str(control["cwd"]),
        "runtime_instance_id": current["runtime_instance_id"],
        "process_birth": current["birth"],
        "state_digest": before_digest,
    }, result.returncode


def _ready(
    spec: Mapping[str, Any],
    state: Mapping[str, Any],
    *,
    expected_identity: str,
    expected_instance: str,
) -> dict[str, Any]:
    if not state["active"] or not _process_matches(state, spec["platform"]["os"]):
        raise AdapterError("owned Runtime process is not alive")
    if not _listener_owned_by(state["pid"], state["host"], state["port"], spec["platform"]["os"]):
        raise AdapterError("owned Runtime process is not the configured listener owner")
    status, payload = _http(spec, state)
    identity = payload.get("identity")
    instance = payload.get("runtime_instance_id")
    if status != 200 or identity != expected_identity or instance != expected_instance:
        raise AdapterError(
            "HTTP readiness identity or instance does not match the expected candidate"
        )
    return {
        "ready": True,
        "http_status": status,
        "identity": identity,
        "runtime_instance_id": instance,
    }


def adopt_process(
    spec: Mapping[str, Any],
    *,
    pid: int,
    identity: str,
    instance_id: str,
    controller_token_digest: str,
    start_count: int = 1,
) -> dict[str, Any]:
    """Bind a Runtime launched by a real Pinokio action to adapter observations.

    The caller supplies only a token digest. The raw controller bearer must
    never cross this boundary or appear in the state/receipt.
    """
    data = _validate_spec(spec)
    if not isinstance(pid, int) or isinstance(pid, bool) or pid <= 1:
        raise AdapterError("adopted pid must be an integer greater than one")
    identity = _need_text(identity, "adopt.identity")
    instance_id = _need_text(instance_id, "adopt.instance_id")
    if (identity, instance_id) not in data["allowed_sessions"]:
        raise AdapterError("adopted identity and instance are not an allowed session")
    if not isinstance(controller_token_digest, str) or not SHA256.fullmatch(
        controller_token_digest
    ):
        raise AdapterError("adopt.controller_token_digest must be a SHA-256 digest")
    if not isinstance(start_count, int) or isinstance(start_count, bool) or start_count < 1:
        raise AdapterError("adopt.start_count must be a positive integer")

    previous = _read_state(data["state_path"])
    _validate_state_for_spec(previous, data)
    if previous is not None and previous["active"]:
        if _process_matches(previous, data["platform"]["os"]):
            raise AdapterError("cannot replace a still-live recorded session during adoption")
        # A successful Pinokio update may replace the prior recorded process.
        # PID reuse remains an error because _process_matches checks birth.

    birth = _birth(pid, data["platform"]["os"])
    if data["platform"]["os"] == "linux":
        observed_birth, pgid = _linux_process_info(pid)
        if observed_birth != birth:
            raise AdapterError("Linux adopted process birth changed during observation")
    elif data["platform"]["os"] == "macos":
        _observed_pid, pgid, seconds, micros = _mac_process_info(pid)
        if f"{seconds}.{micros:06d}" != birth:
            raise AdapterError("macOS adopted process birth changed during observation")
    else:
        pgid = None
    if data["platform"]["os"] != "windows" and pgid != pid:
        raise AdapterError("adopted POSIX Runtime must be the leader of its owned process group")

    expected_executable = data["runtime_executable_path"]
    executable_path = _wait_expected_executable(
        pid,
        data["platform"]["os"],
        expected_executable,
    )
    state = {
        "active": True,
        "pid": pid,
        "birth": birth,
        "pgid": pgid,
        "executable_path": executable_path,
        "identity": identity,
        "runtime_instance_id": instance_id,
        "argv_digest": _sha(json.dumps(data["runtime_argv"], separators=(",", ":")).encode()),
        "controller_token_digest": controller_token_digest,
        "host": data["host"],
        "port": data["port"],
        "platform_identity": copy.deepcopy(data["platform"]),
        "start_count": start_count,
        "evidence_class": data["platform"]["evidence_class"],
    }
    if _port_free(data["host"], data["port"]):
        raise AdapterError("adopted Runtime does not own the configured port")
    readiness = _ready(data, state, expected_identity=identity, expected_instance=instance_id)
    _observe_endpoint(data, state, "health")
    _write_state(data["state_path"], state)
    return {
        "result": "adopted",
        "pid": pid,
        "process_birth": birth,
        "identity": identity,
        "runtime_instance_id": instance_id,
        "controller_token_representation": "sha256",
        "readiness": readiness,
    }


def _stop(spec: Mapping[str, Any], state: dict[str, Any]) -> dict[str, Any]:
    if not state["active"]:
        raise AdapterError("stop requires an active recorded exact session")
    os_name = spec["platform"]["os"]
    if not _process_matches(state, os_name):
        raise AdapterError("recorded active process is absent; refusing unverified cleanup")
    # Re-observe incarnation immediately before termination. POSIX children were
    # created in their own session; Windows uses taskkill /T against the exact PID.
    if _birth(state["pid"], os_name) != state["birth"]:
        raise AdapterError("recorded PID changed incarnation before stop")
    try:
        if os_name == "windows":
            result = _run_observation(
                ["taskkill.exe", "/PID", str(state["pid"]), "/T", "/F"], timeout=20
            )
            if result.returncode:
                raise AdapterError(f"taskkill failed: {result.stderr.strip()}")
        else:
            os.killpg(state["pgid"], signal.SIGTERM)
            deadline = time.monotonic() + 8
            while time.monotonic() < deadline and _pid_alive(state["pid"]):
                time.sleep(0.05)
            if _pid_alive(state["pid"]):
                if _birth(state["pid"], os_name) != state["birth"]:
                    raise AdapterError("recorded PID changed incarnation during stop")
                os.killpg(state["pgid"], signal.SIGKILL)
    except (OSError, ProcessLookupError, PermissionError) as exc:
        raise AdapterError(f"owned process stop could not be observed: {exc}") from exc
    deadline = time.monotonic() + 8
    while time.monotonic() < deadline and _pid_alive(state["pid"]):
        time.sleep(0.05)
    if _pid_alive(state["pid"]):
        raise AdapterError("owned Runtime process remained alive after stop")
    if not _wait_port_free(state["host"], state["port"]):
        raise AdapterError("Runtime process exited but its port is still occupied")
    state["active"] = False
    state["stopped_at"] = time.time()
    _write_state(spec["state_path"], state)
    return {"result": "stopped", "runtime_instance_id": state["runtime_instance_id"]}


def run_operation(operation: str, spec: Mapping[str, Any]) -> tuple[dict[str, Any], int]:
    """Execute one adapter operation and return JSON plus process exit code."""
    if operation not in OPERATIONS:
        raise AdapterError(f"unsupported operation: {operation}")
    data = _validate_spec(spec)
    state = _read_state(data["state_path"])
    _validate_state_for_spec(state, data)
    if operation == "start":
        if state is not None and state["active"]:
            if _process_matches(state, data["platform"]["os"]):
                raise AdapterError("Runtime is already active; refusing duplicate start")
            raise AdapterError("active state points to an absent process; refusing ambiguous start")
        if not _port_free(data["host"], data["port"]):
            raise AdapterError("requested Runtime port is already occupied")
        start_count = 0 if state is None else state.get("start_count", 1)
        if (
            not isinstance(start_count, int)
            or isinstance(start_count, bool)
            or (state is not None and start_count < 1)
        ):
            raise AdapterError("state has an invalid runtime start count")
        # The backend contract publishes one update instance and one restart
        # instance; subsequent refusal-control re-arms are checked against that
        # restart identity by its existing _readiness(stage) contract.
        instance = data["instance_ids"][0 if state is None else 1]
        token = uuid.uuid4().hex
        inherited_names = (
            "PATH",
            "SYSTEMROOT",
            "WINDIR",
            "TEMP",
            "TMP",
            "HOME",
            "USERPROFILE",
            "LANG",
        )
        env = {key: os.environ[key] for key in inherited_names if key in os.environ}
        env.update(data["runtime_env"])
        env.update(
            {
                "ASTRID_RUNTIME_IDENTITY": data["identity"],
                "ASTRID_RUNTIME_INSTANCE_ID": instance,
                "ASTRID_CONTROLLER_TOKEN": token,
            }
        )
        kwargs: dict[str, Any] = {
            "cwd": str(data["runtime_cwd"]),
            "env": env,
            "stdin": subprocess.DEVNULL,
            "stdout": subprocess.DEVNULL,
            "stderr": subprocess.DEVNULL,
            "shell": False,
            "close_fds": True,
        }
        if data["platform"]["os"] == "windows":
            kwargs["creationflags"] = (
                subprocess.CREATE_NEW_PROCESS_GROUP | subprocess.DETACHED_PROCESS
            )
        else:
            kwargs["start_new_session"] = True
        try:
            process = subprocess.Popen(data["runtime_argv"], **kwargs)
        except OSError as exc:
            raise AdapterError(f"Runtime launch failed: {exc}") from exc
        launched_state: dict[str, Any] | None = None
        try:
            birth = _birth(process.pid, data["platform"]["os"])
            pgid = process.pid if data["platform"]["os"] != "windows" else None
            expected_executable = data["runtime_executable_path"]
            executable_path = _wait_expected_executable(
                process.pid,
                data["platform"]["os"],
                expected_executable,
            )
            launched_state = {
                "active": True,
                "pid": process.pid,
                "birth": birth,
                "pgid": pgid,
                "executable_path": executable_path,
                "identity": data["identity"],
                "runtime_instance_id": instance,
                "argv_digest": _sha(
                    json.dumps(
                        data["runtime_argv"],
                        separators=(",", ":"),
                    ).encode()
                ),
                "controller_token_digest": _sha(token.encode()),
                "host": data["host"],
                "port": data["port"],
                "platform_identity": copy.deepcopy(data["platform"]),
                "start_count": start_count + 1,
                "evidence_class": data["platform"]["evidence_class"],
            }
            state = launched_state
            _write_state(data["state_path"], launched_state)
            deadline = time.monotonic() + data["start_timeout_seconds"]
            while time.monotonic() < deadline:
                if process.poll() is not None:
                    raise AdapterError(
                        f"Runtime exited during startup with code {process.returncode}"
                    )
                try:
                    ready = _ready(
                        data, state, expected_identity=data["identity"], expected_instance=instance
                    )
                    return {"result": "ready", **ready}, 0
                except AdapterError:
                    time.sleep(0.05)
            raise AdapterError("Runtime did not become HTTP ready before timeout")
        except BaseException as startup_error:
            # Only clean up the exact process incarnation we launched. A cleanup
            # failure is material and must not be hidden behind the startup error.
            try:
                if launched_state is not None:
                    if process.poll() is not None:
                        if _pid_alive(process.pid):
                            raise AdapterError(
                                "launched PID is still present after its process handle exited"
                            )
                        if not _wait_port_free(launched_state["host"], launched_state["port"]):
                            raise AdapterError(
                                "launched process exited but its port remains occupied"
                            )
                        launched_state["active"] = False
                        launched_state["stopped_at"] = time.time()
                        _write_state(data["state_path"], launched_state)
                    else:
                        _stop(data, launched_state)
                elif process.poll() is None:
                    if data["platform"]["os"] != "windows":
                        os.killpg(process.pid, signal.SIGKILL)
                    else:
                        process.kill()
                    process.wait(timeout=5)
                    if process.poll() is None:
                        raise AdapterError("launched child remained alive after startup cleanup")
            except BaseException as cleanup_error:
                raise AdapterError(
                    f"startup failed ({startup_error}); exact-process cleanup failed ({cleanup_error})"
                ) from cleanup_error
            raise
    if operation == "stop":
        if state is None or not state["active"]:
            raise AdapterError("stop requires an active recorded exact session")
        return _stop(data, state), 0
    if operation in {"busy-refusal", "unknown-refusal"}:
        reason = "busy" if operation == "busy-refusal" else "unknown"
        if state is None:
            raise AdapterError(f"cannot prove a live same-session refusal for {reason}")
        return _run_stop_control(data, state, reason)
    if operation == "recovery":
        if state is None or not state["active"]:
            raise AdapterError("same-session recovery requires an active recorded process")
        session = {
            key: state.get(key)
            for key in ("pid", "birth", "runtime_instance_id", "controller_token_digest")
        }
        result = _stop(data, state)
        return {"outcome": "stopped", "same_session": True, "session": session, "result": result}, 0
    if operation == "processes":
        active = bool(state and state["active"])
        if active:
            alive = _process_matches(state, data["platform"]["os"])
            if not alive:
                raise AdapterError("active state has no matching live process")
            ports_free = _port_free(state["host"], state["port"])
            if ports_free:
                raise AdapterError("active process does not own its configured port")
            if not _listener_owned_by(
                state["pid"], state["host"], state["port"], data["platform"]["os"]
            ):
                raise AdapterError("active process is not the configured listener owner")
            status, payload = _observe_endpoint(data, state, "health")
            if (
                status != 200
                or payload.get("identity") != state["identity"]
                or payload.get("runtime_instance_id", payload.get("instance_id"))
                != state["runtime_instance_id"]
            ):
                raise AdapterError(
                    "active process port does not serve the recorded runtime identity"
                )
        else:
            ports_free = _port_free(data["host"], data["port"])
            if not ports_free:
                raise AdapterError("stopped snapshot cannot prove configured port free")
        return {
            "controller_pid": state["pid"] if active else None,
            "runtime_instance_id": state["runtime_instance_id"] if active else None,
            "running": active,
            "owned_processes": [state["pid"]] if active else [],
            "owned_ports_free": ports_free,
            # The value is already a digest, never the bearer. The accepted
            # executor sanitizes this token-named field once more into its
            # stable controller_token_digest identity field.
            "controller_token": state["controller_token_digest"] if state else None,
            "controller_token_representation": "sha256" if state else None,
            "evidence_class": data["platform"]["evidence_class"],
        }, 0
    if operation in {"ready", "status", "health", "menu", "identity-mismatch", "instance-mismatch"}:
        if state is None or not state["active"]:
            raise AdapterError("Runtime readiness observation requires an active owned process")
        if not _process_matches(state, data["platform"]["os"]):
            raise AdapterError(
                "Runtime readiness observation lost the recorded process incarnation"
            )
        if not _listener_owned_by(
            state["pid"], state["host"], state["port"], data["platform"]["os"]
        ):
            raise AdapterError("Runtime readiness observation lost its listener owner")
        if operation == "identity-mismatch":
            _observe_endpoint(data, state, "health")
            mismatch = _need_text(spec.get("mismatch_identity"), "mismatch_identity")
            if mismatch == data["identity"]:
                raise AdapterError("mismatch_identity must differ from expected identity")
            status, payload = _http(
                data, state, data["negative_control_paths"]["identity_mismatch"]
            )
            identity = payload.get("identity")
            instance = payload.get("runtime_instance_id", payload.get("instance_id"))
            if status != 200 or identity != mismatch or instance != state["runtime_instance_id"]:
                raise AdapterError(
                    "identity mismatch control did not observe the configured wrong identity"
                )
            suppressed = {
                key: payload.get(key)
                for key in ("ready", "ready_sentinel", "local_set", "open_menu")
            }
            if suppressed != {
                "ready": False,
                "ready_sentinel": False,
                "local_set": False,
                "open_menu": False,
            }:
                raise AdapterError(
                    "identity mismatch did not observe all readiness suppression fields"
                )
            return {
                "http_status": status,
                "identity": identity,
                "runtime_instance_id": instance,
                **suppressed,
            }, 0
        if operation == "instance-mismatch":
            _observe_endpoint(data, state, "health")
            mismatch = _need_text(spec.get("mismatch_instance_id"), "mismatch_instance_id")
            if mismatch in data["instance_ids"]:
                raise AdapterError("mismatch_instance_id must differ from configured instances")
            status, payload = _http(
                data, state, data["negative_control_paths"]["instance_mismatch"]
            )
            identity = payload.get("identity")
            instance = payload.get("runtime_instance_id", payload.get("instance_id"))
            if status != 200 or identity != state["identity"] or instance != mismatch:
                raise AdapterError(
                    "instance mismatch control did not observe the configured wrong instance"
                )
            suppressed = {
                key: payload.get(key)
                for key in ("ready", "ready_sentinel", "local_set", "open_menu")
            }
            if suppressed != {
                "ready": False,
                "ready_sentinel": False,
                "local_set": False,
                "open_menu": False,
            }:
                raise AdapterError(
                    "instance mismatch did not observe all readiness suppression fields"
                )
            return {
                "http_status": status,
                "identity": identity,
                "runtime_instance_id": instance,
                **suppressed,
            }, 0
        if operation == "ready":
            status, payload = _http(data, state)
            identity = payload.get("identity")
            instance = payload.get("runtime_instance_id")
            ready = (
                status == 200
                and identity == state["identity"]
                and instance == state["runtime_instance_id"]
            )
            if not ready:
                raise AdapterError(
                    "Runtime failed process, HTTP, candidate identity, or instance readiness"
                )
            return {
                "ready": True,
                "http_status": status,
                "identity": identity,
                "runtime_instance_id": instance,
            }, 0
        _status, observed = _observe_endpoint(data, state, operation)
        return observed, 0
    raise AdapterError(f"unsupported operation: {operation}")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("operation", choices=OPERATIONS)
    parser.add_argument("--spec", required=True, type=Path)
    parser.add_argument("--pid", type=int, help="existing Pinokio-launched Runtime PID for adopt")
    parser.add_argument("--identity", help="existing Runtime identity for adopt")
    parser.add_argument("--instance-id", help="existing Runtime instance for adopt")
    parser.add_argument("--controller-token-digest", help="SHA-256 only; never pass the raw bearer")
    parser.add_argument(
        "--start-count", type=int, default=1, help="positive lifecycle count for adopt"
    )
    args = parser.parse_args(argv)
    try:
        spec = _load_spec(args.spec)
        if args.operation == "adopt":
            if (
                args.pid is None
                or args.identity is None
                or args.instance_id is None
                or args.controller_token_digest is None
            ):
                raise AdapterError(
                    "adopt requires --pid, --identity, --instance-id, and --controller-token-digest"
                )
            value = adopt_process(
                spec,
                pid=args.pid,
                identity=args.identity,
                instance_id=args.instance_id,
                controller_token_digest=args.controller_token_digest,
                start_count=args.start_count,
            )
            code = 0
        else:
            value, code = run_operation(args.operation, spec)
        print(json.dumps(value, sort_keys=True))
        return code
    except (AdapterError, OSError, ValueError, subprocess.SubprocessError) as exc:
        print(json.dumps({"error": str(exc)}, sort_keys=True), file=sys.stderr)
        return 2


if __name__ == "__main__":  # pragma: no cover - exercised through CLI
    raise SystemExit(main())
