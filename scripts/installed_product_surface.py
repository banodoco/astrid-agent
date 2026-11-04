#!/usr/bin/env python3
"""Observe a real installed Astrid for the M4-5 installed/browser boundary.

This command is deliberately read-only.  It binds the four candidate checkouts,
the selected installation record, the installed executable, the native service
manager, Runtime status, a canonical semantic snapshot supplied by the host,
and an unrelated process sentinel.  It never constructs an installed identity
from source checkout metadata.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import platform
import plistlib
import re
import shlex
import subprocess
import sys
import tempfile
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

PROTOCOL = "astrid-installed-browser-interface/v1"
JOURNAL_FORMAT = "astrid-installed-product-surface/v1"
SHA = re.compile(r"[0-9a-f]{40}")
SAFE_VERSION = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,63}")
OWNER = "haus.astrid.runtime"
TERMINAL_STEPS = {"complete", "rolled_back"}
AMBIENT_ENV_ALLOWLIST = frozenset(
    {
        "APPDATA",
        "COMSPEC",
        "DBUS_SESSION_BUS_ADDRESS",
        "DISPLAY",
        "HOME",
        "LANG",
        "LC_ALL",
        "LC_CTYPE",
        "LOCALAPPDATA",
        "LOGNAME",
        "PATH",
        "PATHEXT",
        "PROGRAMDATA",
        "ProgramFiles",
        "ProgramFiles(x86)",
        "SHELL",
        "SystemRoot",
        "TEMP",
        "TMP",
        "TMPDIR",
        "USER",
        "USERPROFILE",
        "WAYLAND_DISPLAY",
        "WINDIR",
        "XDG_CACHE_HOME",
        "XDG_CONFIG_HOME",
        "XDG_DATA_HOME",
        "XDG_RUNTIME_DIR",
    }
)
SENSITIVE_ENV_PARTS = (
    "ACCESS_KEY",
    "API_KEY",
    "CREDENTIAL",
    "PASSWORD",
    "PASSWD",
    "PRIVATE_KEY",
    "SECRET",
    "TOKEN",
)


class SurfaceError(RuntimeError):
    """The installed product cannot prove the requested observation."""


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _canonical(value: object) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":")).encode("utf-8")


def _mapping(value: object, label: str) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        raise SurfaceError(f"{label} must be an object")
    return dict(value)


def _child_env(installed: Mapping[str, Any] | None = None) -> dict[str, str]:
    env = {
        key: os.environ[key]
        for key in AMBIENT_ENV_ALLOWLIST
        if key in os.environ and not any(part in key.upper() for part in SENSITIVE_ENV_PARTS)
    }
    env.setdefault("PATH", os.defpath)
    for key, value in (installed or {}).items():
        name = str(key)
        if not any(part in name.upper() for part in SENSITIVE_ENV_PARTS):
            env[name] = str(value)
    return env


def _read_json(path: Path, label: str) -> dict[str, Any]:
    if path.is_symlink() or not path.is_file():
        raise SurfaceError(f"{label} must be a non-symlink regular file: {path}")
    try:
        return _mapping(json.loads(path.read_text(encoding="utf-8")), label)
    except (OSError, json.JSONDecodeError) as exc:
        raise SurfaceError(f"{label} is unavailable or malformed: {path}") from exc


def _write_json(path: Path, value: Mapping[str, Any]) -> None:
    if path.is_symlink():
        raise SurfaceError(f"refusing symlinked surface journal: {path}")
    descriptor, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        os.chmod(temporary, 0o600)
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            json.dump(value, handle, indent=2, sort_keys=True)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def _command(argv: Sequence[str], label: str, *, timeout: float = 30) -> str:
    try:
        result = subprocess.run(
            list(argv), capture_output=True, text=True, timeout=timeout, check=False
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise SurfaceError(f"{label} is unavailable: {exc}") from exc
    if result.returncode:
        detail = (result.stderr or result.stdout).strip().splitlines()
        suffix = f": {detail[0]}" if detail else ""
        raise SurfaceError(f"{label} failed with exit {result.returncode}{suffix}")
    return result.stdout if result.stdout.strip() else result.stderr


def _git_head(path: Path, label: str) -> str:
    if not path.is_absolute() or path.is_symlink() or not path.is_dir():
        raise SurfaceError(f"{label} checkout must be an absolute non-symlink directory")
    head = _command(["git", "-C", str(path), "rev-parse", "HEAD"], f"{label} checkout").strip()
    if SHA.fullmatch(head) is None:
        raise SurfaceError(f"{label} checkout did not resolve to a full commit")
    dirty = _command(
        ["git", "-C", str(path), "status", "--porcelain", "--untracked-files=no"],
        f"{label} checkout status",
    )
    if dirty.strip():
        raise SurfaceError(f"{label} checkout has tracked modifications")
    return head


def _checkouts(args: argparse.Namespace, envelope: Mapping[str, Any]) -> dict[str, dict[str, str]]:
    candidate = _mapping(_mapping(envelope["request"], "request")["inputs"], "inputs")["candidate"]
    candidate = _mapping(candidate, "candidate")
    paths = {
        "app": args.app_checkout,
        "agent": args.agent_checkout,
        "runtime": args.runtime_checkout,
        "pinokio": args.pinokio_checkout,
    }
    if set(candidate) != set(paths):
        raise SurfaceError("candidate must contain app, agent, runtime, and pinokio")
    observed: dict[str, dict[str, str]] = {}
    for name, path in paths.items():
        head = _git_head(path, name)
        if head != candidate[name]:
            raise SurfaceError(f"{name} checkout differs from the requested candidate")
        observed[name] = {"sha": head}
    return observed


def _roots(envelope: Mapping[str, Any]) -> tuple[Path, Path]:
    inputs = _mapping(_mapping(envelope["request"], "request")["inputs"], "inputs")
    root = Path(inputs.get("root", ""))
    data = Path(inputs.get("data_root", ""))
    if not root.is_absolute() or not data.is_absolute() or root == data:
        raise SurfaceError("install and data roots must be distinct absolute paths")
    if root.is_symlink() or data.is_symlink():
        raise SurfaceError("install and data roots must not be symlinks")
    if not data.is_dir():
        raise SurfaceError("data root is unavailable")
    return root.resolve(strict=False), data.resolve(strict=True)


def _installed(root: Path, data: Path) -> dict[str, Any]:
    marker = _read_json(root / "install.json", "install marker")
    if marker.get("owner") != OWNER or Path(str(marker.get("data_root", ""))).resolve() != data:
        raise SurfaceError("install marker does not bind the requested data root")
    current_path = root / "current"
    if current_path.is_symlink() or not current_path.is_file():
        raise SurfaceError("installed current selection is unavailable")
    version_name = current_path.read_text(encoding="utf-8").strip()
    if SAFE_VERSION.fullmatch(version_name) is None:
        raise SurfaceError("installed current selection is invalid")
    version_root = (root / "versions" / version_name).resolve(strict=True)
    versions_root = (root / "versions").resolve(strict=True)
    if not version_root.is_relative_to(versions_root):
        raise SurfaceError("installed version escapes the program root")
    version = _read_json(version_root / "version.json", "installed version record")
    if Path(str(version.get("root", ""))).resolve() != version_root:
        raise SurfaceError("installed version record points outside the selected version")
    python = Path(os.path.abspath(str(version.get("python", ""))))
    if (
        not python.is_absolute()
        or not python.is_relative_to(version_root)
        or not python.is_file()
        or not os.access(python, os.X_OK)
    ):
        raise SurfaceError("installed Python executable is unavailable")
    profile = Path(str(version.get("profile", "")))
    if (
        profile.is_symlink()
        or not profile.is_file()
        or not profile.resolve().is_relative_to(version_root)
    ):
        raise SurfaceError("installed profile is outside the selected version")
    manifest_path = version_root / "manifest.json"
    manifest = _read_json(manifest_path, "installed manifest")
    release = manifest.get("release", 0)
    identity = _mapping(version.get("identity"), "installed version identity")
    if type(release) is not int or release < 0 or identity.get("release") != release:
        raise SurfaceError("installed version and manifest release identities differ")
    managed_by = marker.get("managed_by")
    if managed_by not in {None, "pinokio"} or identity.get("managed_by") != managed_by:
        raise SurfaceError("installed manager identity differs across durable records")
    return {
        "marker": marker,
        "version": version,
        "version_root": version_root,
        "manifest": manifest,
        "manifest_path": manifest_path,
        # Keep the selected venv path. POSIX venv interpreters are normally
        # symlinks; resolving it would lose the sibling astrid-runtime script.
        "python": python,
        "managed_by": managed_by,
    }


def _relative_file(data: Path, relative: str, label: str) -> Path:
    supplied = Path(relative)
    if supplied.is_absolute() or ".." in supplied.parts or not supplied.parts:
        raise SurfaceError(f"{label} must be a safe path relative to the data root")
    path = data.joinpath(*supplied.parts)
    if path.is_symlink() or not path.is_file():
        raise SurfaceError(f"{label} must be a non-symlink regular file")
    resolved = path.resolve(strict=True)
    if not resolved.is_relative_to(data):
        raise SurfaceError(f"{label} escapes the data root")
    return resolved


def _actual_os(expected: str) -> str:
    actual = {"Darwin": "macos", "Linux": "linux", "Windows": "windows"}.get(platform.system())
    if actual != expected:
        raise SurfaceError(f"scenario OS {expected!r} does not match this host ({actual!r})")
    return actual


def _powershell(script: str, label: str) -> str:
    executable = "powershell.exe" if sys.platform == "win32" else "pwsh"
    return _command([executable, "-NoProfile", "-NonInteractive", "-Command", script], label)


def _manager_version(os_name: str) -> dict[str, Any]:
    if os_name == "linux":
        version = _command(["systemctl", "--user", "--version"], "systemd --user").splitlines()[0]
        _command(["systemctl", "--user", "show-environment"], "systemd --user session")
        manager = "systemd-user"
    elif os_name == "macos":
        version = _command(["launchctl", "version"], "launchd").strip().splitlines()[0]
        _command(["launchctl", "print", f"gui/{os.getuid()}"], "launchd GUI domain")
        manager = "launchd"
    else:
        version = _powershell(
            "$ErrorActionPreference='Stop'; Get-Command Get-ScheduledTask | Out-Null; "
            "$PSVersionTable.PSVersion.ToString()",
            "Task Scheduler",
        ).strip()
        manager = "task-scheduler"
    if not version:
        raise SurfaceError("native service manager returned no version")
    return {"manager": manager, "version": version, "foreground_fallback": False}


def _canonical_service_argv(installed: Mapping[str, Any], data: Path, os_name: str) -> list[str]:
    helper = (
        "import json,sys; from pathlib import Path; "
        "from runtime_protocol import platform; "
        "from astrid.installer.core import Version; "
        "from astrid.installer.service import command; "
        "version=Version.load(json.loads(Path(sys.argv[1]).read_text(encoding='utf-8'))); "
        "root=version.root.parent.parent; log=platform.log_dir()/'install.log'; "
        "print(json.dumps(command(version,Path(sys.argv[2]),windows=sys.argv[3]=='windows',"
        "log=log,env_file=root/'service-env.json')))"
    )
    python = Path(installed["python"])
    raw_env = _mapping(_mapping(installed["version"], "version").get("env"), "installed env")
    try:
        result = subprocess.run(
            [
                str(python),
                "-X",
                "utf8",
                "-I",
                "-c",
                helper,
                str(Path(installed["version_root"]) / "version.json"),
                str(data),
                os_name,
            ],
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
            env=_child_env(raw_env),
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise SurfaceError(f"installed service-command derivation is unavailable: {exc}") from exc
    if result.returncode:
        raise SurfaceError("installed service-command derivation failed")
    try:
        argv = json.loads(result.stdout)
    except json.JSONDecodeError as exc:
        raise SurfaceError("installed service-command derivation returned invalid JSON") from exc
    if not isinstance(argv, list) or not argv or any(not isinstance(value, str) for value in argv):
        raise SurfaceError("installed service-command derivation returned malformed argv")
    return argv


def _service_argv(expected: Sequence[str], observed: Sequence[str]) -> None:
    if list(observed) != list(expected):
        raise SurfaceError(
            "native service definition differs from the exact installed Runtime command"
        )


def _process_executable(pid: int) -> Path:
    if sys.platform.startswith("linux"):
        return Path(f"/proc/{pid}/exe").resolve(strict=True)
    if sys.platform == "darwin":
        value = _command(
            ["ps", "-p", str(pid), "-o", "comm="], "Runtime process executable"
        ).strip()
        if not value:
            raise SurfaceError("Runtime process executable is unavailable")
        return Path(value).resolve(strict=True)
    value = _powershell(
        f'$p=Get-CimInstance Win32_Process -Filter "ProcessId={pid}"; '
        "if (-not $p) { throw 'missing Runtime process' }; $p.ExecutablePath",
        "Runtime process executable",
    ).strip()
    return Path(value).resolve(strict=True)


def _native_service(
    installed: Mapping[str, Any], data: Path, os_name: str, runtime_pid: int
) -> None:
    python = Path(installed["python"])
    expected_process = python.with_name("pythonw.exe") if os_name == "windows" else python
    if runtime_pid <= 0 or _process_executable(runtime_pid) != expected_process.resolve():
        raise SurfaceError(
            "native service process is not the selected installed Runtime executable"
        )
    expected_argv = _canonical_service_argv(installed, data, os_name)
    if os_name == "linux":
        output = _command(
            [
                "systemctl",
                "--user",
                "show",
                "astrid.service",
                "--property=LoadState,ActiveState,FragmentPath,MainPID",
            ],
            "Astrid systemd user service",
        )
        fields = dict(line.split("=", 1) for line in output.splitlines() if "=" in line)
        fragment = Path(fields.get("FragmentPath", ""))
        if (
            fields.get("LoadState") == "not-found"
            or fields.get("ActiveState") != "active"
            or fields.get("MainPID") != str(runtime_pid)
            or fragment.is_symlink()
            or not fragment.is_file()
        ):
            raise SurfaceError("active systemd unit is not bound to the observed Runtime PID")
        exec_lines = [
            line.split("=", 1)[1]
            for line in fragment.read_text(encoding="utf-8", errors="strict").splitlines()
            if line.startswith("ExecStart=")
        ]
        if len(exec_lines) != 1:
            raise SurfaceError("systemd unit has no unique Runtime command")
        encoded_argv = exec_lines[0].replace("%%", "%").replace("$$", "$")
        _service_argv(expected_argv, shlex.split(encoded_argv))
    elif os_name == "macos":
        plist = Path.home() / "Library" / "LaunchAgents" / "haus.astrid.runtime.plist"
        if plist.is_symlink() or not plist.is_file():
            raise SurfaceError("LaunchAgent definition is unavailable")
        with plist.open("rb") as handle:
            definition = plistlib.load(handle)
        argv = definition.get("ProgramArguments") if isinstance(definition, dict) else None
        if not isinstance(argv, list) or any(not isinstance(value, str) for value in argv):
            raise SurfaceError("LaunchAgent Runtime command is malformed")
        _service_argv(expected_argv, argv)
        state = _command(
            ["launchctl", "print", f"gui/{os.getuid()}/haus.astrid.runtime"],
            "Astrid LaunchAgent",
        )
        match = re.search(r"^\s*pid\s*=\s*(\d+)\s*$", state, re.MULTILINE)
        if match is None or int(match.group(1)) != runtime_pid:
            raise SurfaceError("LaunchAgent is not bound to the observed Runtime PID")
    else:
        encoded = _powershell(
            "$ErrorActionPreference='Stop'; $t=Get-ScheduledTask -TaskName 'Astrid Runtime' "
            "-TaskPath '\\'; if ($t.State -ne 'Running') { throw 'task not running' }; "
            "$t.Actions | Select-Object Execute,Arguments | ConvertTo-Json -Compress",
            "Astrid Scheduled Task",
        ).strip()
        action = _mapping(json.loads(encoded), "Scheduled Task action")
        execute = action.get("Execute")
        arguments = action.get("Arguments")
        if (
            not isinstance(execute, str)
            or not isinstance(arguments, str)
            or execute != expected_argv[0]
            or arguments != subprocess.list2cmdline(expected_argv[1:])
        ):
            raise SurfaceError(
                "Scheduled Task definition differs from the exact installed Runtime command"
            )
        try:
            instances = _powershell(
                "$ErrorActionPreference='Stop'; $service=New-Object -ComObject 'Schedule.Service'; "
                "$service.Connect(); $task=$service.GetFolder('\\').GetTask('Astrid Runtime'); "
                "@($task.GetInstances(0) | ForEach-Object { [int]$_.EnginePID }) | "
                "ConvertTo-Json -Compress",
                "Astrid Scheduled Task running instances",
            ).strip()
        except SurfaceError as exc:
            raise SurfaceError(
                "NEEDS: Windows Task Scheduler must expose the Astrid Runtime EnginePID for native service proof"
            ) from exc
        values = json.loads(instances)
        pids = values if isinstance(values, list) else [values]
        if runtime_pid not in pids:
            raise SurfaceError("Scheduled Task is not bound to the observed Runtime PID")


def _runtime_executable(installed: Mapping[str, Any]) -> Path:
    python = Path(installed["python"])
    version_root = Path(installed["version_root"])
    names = (
        ("astrid-runtime.exe", "astrid-runtime") if sys.platform == "win32" else ("astrid-runtime",)
    )
    for name in names:
        candidate = python.parent / name
        lexical = Path(os.path.abspath(candidate))
        if (
            lexical.is_relative_to(version_root)
            and not lexical.is_symlink()
            and lexical.is_file()
            and os.access(lexical, os.X_OK)
        ):
            return lexical
    raise SurfaceError("installed astrid-runtime executable is unavailable")


def _runtime_status(installed: Mapping[str, Any], data: Path) -> tuple[dict[str, Any], Path]:
    executable = _runtime_executable(installed)
    raw_env = _mapping(_mapping(installed["version"], "version").get("env"), "installed env")
    env = _child_env(raw_env)
    try:
        result = subprocess.run(
            [str(executable), "status", "--data-root", str(data), "--json"],
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
            env=env,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise SurfaceError(f"installed Runtime status is unavailable: {exc}") from exc
    if result.returncode:
        raise SurfaceError("installed Runtime status command failed")
    try:
        status = _mapping(json.loads(result.stdout), "installed Runtime status")
    except json.JSONDecodeError as exc:
        raise SurfaceError("installed Runtime status returned invalid JSON") from exc
    health = _mapping(status.get("health"), "Runtime health")
    discovery = _mapping(status.get("discovery"), "Runtime discovery")
    support = _mapping(status.get("support"), "Runtime support")
    instance = discovery.get("runtime_instance_id")
    if (
        health.get("status") not in {"ok", "healthy"}
        or not isinstance(instance, str)
        or not instance
        or health.get("runtime_instance_id") != instance
        or support.get("pid_alive") is not True
    ):
        raise SurfaceError("installed Runtime status is not the selected healthy owner")
    return status, executable


def _runtime_owner_pids() -> list[int]:
    if sys.platform == "win32":
        raw = _powershell(
            "@(Get-CimInstance Win32_Process | Select-Object ProcessId,CommandLine) | "
            "ConvertTo-Json -Compress",
            "Runtime process census",
        )
        rows = json.loads(raw)
        rows = rows if isinstance(rows, list) else [rows]
        return sorted(
            int(row["ProcessId"])
            for row in rows
            if isinstance(row, dict)
            and "-m runtime_protocol start" in str(row.get("CommandLine") or "")
        )
    output = _command(["ps", "-axo", "pid=,command="], "Runtime process census")
    found = []
    for line in output.splitlines():
        pid, _, command = line.strip().partition(" ")
        if pid.isdigit() and "-m runtime_protocol start" in command:
            found.append(int(pid))
    return sorted(found)


def _process_birth(pid: int) -> str | None:
    if pid <= 0:
        return None
    if sys.platform.startswith("linux"):
        path = Path(f"/proc/{pid}/stat")
        if not path.is_file():
            return None
        fields = path.read_text(encoding="utf-8").split()
        return fields[21] if len(fields) > 21 else None
    if sys.platform == "darwin":
        result = subprocess.run(
            ["ps", "-p", str(pid), "-o", "lstart="],
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
        return result.stdout.strip() or None if result.returncode == 0 else None
    try:
        value = _powershell(
            f'(Get-CimInstance Win32_Process -Filter "ProcessId={pid}").CreationDate',
            "sentinel process identity",
        ).strip()
    except SurfaceError:
        return None
    return value or None


def _sentinel(data: Path, relative: str) -> None:
    record = _read_json(_relative_file(data, relative, "sentinel record"), "sentinel record")
    pid = record.get("pid")
    birth = record.get("birth")
    if not isinstance(pid, int) or isinstance(pid, bool) or not isinstance(birth, str) or not birth:
        raise SurfaceError("sentinel record must contain a positive pid and birth identity")
    if _process_birth(pid) != birth:
        raise SurfaceError("unrelated sentinel process is absent or was replaced")


def _settled(root: Path) -> None:
    transaction_path = root / "update-transaction.json"
    if transaction_path.exists():
        transaction = _read_json(transaction_path, "update transaction")
        if transaction.get("step") not in TERMINAL_STEPS:
            raise SurfaceError("installer update transaction is not settled")
    marker = _read_json(root / "install.json", "install marker")
    job = marker.get("update_job")
    if isinstance(job, Mapping) and job.get("state") not in {"updated", "rolled_back", "failed"}:
        raise SurfaceError("owned update job is not settled")


def _journal(
    data: Path, args: argparse.Namespace, envelope: Mapping[str, Any], details: Mapping[str, Any]
) -> None:
    if Path(args.journal_name).name != args.journal_name or args.journal_name in {".", ".."}:
        raise SurfaceError("journal name must be one safe file name")
    path = data / args.journal_name
    value: dict[str, Any]
    if path.exists():
        value = _read_json(path, "installed surface journal")
        if value.get("format") != JOURNAL_FORMAT:
            raise SurfaceError("installed surface journal format differs")
    else:
        value = {"format": JOURNAL_FORMAT, "actions": []}
    actions = value.get("actions")
    if not isinstance(actions, list):
        raise SurfaceError("installed surface journal actions are malformed")
    handoff = envelope["handoff_id"]
    if any(isinstance(item, dict) and item.get("handoff_id") == handoff for item in actions):
        raise SurfaceError("installed surface handoff was already observed")
    actions.append(
        {
            "handoff_id": handoff,
            "action": envelope["action"],
            "details_sha256": hashlib.sha256(_canonical(details)).hexdigest(),
        }
    )
    _write_json(path, value)


def _validate_envelope(value: object) -> dict[str, Any]:
    envelope = _mapping(value, "interface envelope")
    if envelope.get("protocol") != PROTOCOL or envelope.get("surface") != "installed":
        raise SurfaceError("installed interface protocol or surface differs")
    action = envelope.get("action")
    if action not in {"inspect-release1", "inspect-release2", "cleanup"}:
        raise SurfaceError(f"installed surface does not own action {action!r}")
    request = _mapping(envelope.get("request"), "request")
    if request.get("action") != action:
        raise SurfaceError("request and interface action differ")
    scenario = _mapping(request.get("scenario"), "scenario")
    _actual_os(str(scenario.get("os")))
    handoff = envelope.get("handoff_id")
    if not isinstance(handoff, str) or len(handoff) != 64:
        raise SurfaceError("interface handoff identity is invalid")
    return envelope


def execute(
    args: argparse.Namespace, envelope: Mapping[str, Any]
) -> tuple[dict[str, Any], dict[str, Any]]:
    request = _mapping(envelope["request"], "request")
    scenario = _mapping(request["scenario"], "scenario")
    inputs = _mapping(request["inputs"], "inputs")
    root, data = _roots(envelope)
    action = str(envelope["action"])
    checkouts = _checkouts(args, envelope)
    if action == "cleanup":
        if os.path.lexists(root) or _runtime_owner_pids():
            raise SurfaceError("owned product cleanup left the program root or Runtime owner")
        _sentinel(data, args.sentinel_path)
        observation = {"cleanup": "pass"}
        evidence = {
            "installed_executable": {"state": "uninstalled"},
            "candidate_checkouts": checkouts,
        }
        _journal(data, args, envelope, evidence)
        return observation, evidence

    installed = _installed(root, data)
    manifest = _mapping(installed["manifest"], "installed manifest")
    release = manifest.get("release", 0)
    semantic = _relative_file(data, args.semantic_path, "semantic snapshot")
    semantic_sha = _sha256(semantic)
    executable_evidence = {
        "path": str(installed["python"]),
        "resolved_path": str(Path(installed["python"]).resolve()),
        "sha256": _sha256(Path(installed["python"])),
        "version_record_sha256": _sha256(Path(installed["version_root"]) / "version.json"),
        "manifest_sha256": _sha256(Path(installed["manifest_path"])),
    }
    if action == "inspect-release1":
        if release != 1:
            raise SurfaceError("selected installation is not Release 1")
        for name in ("agent", "runtime"):
            source = _mapping(manifest.get(name), f"installed {name} source")
            if source.get("sha") != inputs["candidate"][name]:
                raise SurfaceError(f"installed Release-1 {name} differs from the candidate")
        observation: dict[str, Any] = {
            "release": 1,
            "candidate_checkouts": checkouts,
            "semantic_data": {"sha256": semantic_sha},
            "os_build": {"macos": "macOS", "linux": "Linux", "windows": "Windows"}[scenario["os"]]
            + "-"
            + platform.platform(),
        }
        if scenario.get("mode") == "direct":
            if installed["managed_by"] is not None:
                raise SurfaceError("direct installation is marked as Pinokio-managed")
            selection = root.parent / "astrid-install.json"
            if selection.exists() and _read_json(selection, "install selection").get("foreground"):
                raise SurfaceError("direct installation selected a foreground fallback")
            observation["service_manager"] = _manager_version(str(scenario["os"]))
        elif installed["managed_by"] != "pinokio":
            raise SurfaceError("Pinokio scenario is not marked as Pinokio-managed")
        evidence = {"installed_executable": executable_evidence, "candidate_checkouts": checkouts}
    else:
        stand_in = _mapping(inputs.get("stand_in"), "stand-in")
        target = _mapping(stand_in.get("target"), "stand-in target")
        runtime = _mapping(manifest.get("runtime"), "installed Runtime source")
        installed_manifest_sha = _sha256(Path(installed["manifest_path"]))
        if (
            release != 2
            or manifest.get("qualification_marker") != inputs.get("stand_in_marker")
            or installed_manifest_sha != stand_in.get("manifest_sha256")
            or _mapping(manifest.get("agent"), "installed Agent source").get("sha")
            != target.get("agent")
            or runtime.get("sha") != target.get("runtime")
            or runtime.get("schema_digest") != target.get("schema_digest")
        ):
            raise SurfaceError("installed Release-2 manifest differs from the bound stand-in")
        status, runtime_executable = _runtime_status(installed, data)
        discovery = _mapping(status.get("discovery"), "Runtime discovery")
        pid = discovery.get("pid")
        if not isinstance(pid, int) or isinstance(pid, bool) or _runtime_owner_pids() != [pid]:
            raise SurfaceError("exactly one selected Runtime owner was not observed")
        if scenario.get("mode") == "direct":
            _native_service(installed, data, str(scenario["os"]), pid)
        elif installed["managed_by"] != "pinokio":
            raise SurfaceError("Pinokio scenario lost Pinokio ownership")
        _settled(root)
        _sentinel(data, args.sentinel_path)
        observation = {
            "installed": {
                "release": 2,
                "qualification_marker": inputs["stand_in_marker"],
                "manifest_sha256": installed_manifest_sha,
                "agent": target["agent"],
                "runtime": target["runtime"],
                "schema_digest": target["schema_digest"],
            },
            "semantic_data": {"sha256": semantic_sha},
            "service": "pass",
            "health": "pass",
            "task_settled": "pass",
            "one_runtime": "pass",
            "unrelated_sentinel": "pass",
        }
        evidence = {
            "installed_executable": executable_evidence,
            "runtime_executable": {
                "path": str(runtime_executable),
                "sha256": _sha256(runtime_executable),
            },
            "runtime_instance_id": discovery["runtime_instance_id"],
        }
    _journal(data, args, envelope, evidence)
    return observation, evidence


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--app-checkout", type=Path, required=True)
    parser.add_argument("--agent-checkout", type=Path, required=True)
    parser.add_argument("--runtime-checkout", type=Path, required=True)
    parser.add_argument("--pinokio-checkout", type=Path, required=True)
    parser.add_argument("--semantic-path", default="qualification/semantic-snapshot.json")
    parser.add_argument("--sentinel-path", default="qualification/unrelated-sentinel.json")
    parser.add_argument("--journal-name", default=".astrid-m4-5-installed-surface.json")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        envelope = _validate_envelope(json.load(sys.stdin))
        observation, extra_evidence = execute(args, envelope)
        response = {
            "protocol": PROTOCOL,
            "handoff_id": envelope["handoff_id"],
            "surface": "installed",
            "action": envelope["action"],
            "status": "pass",
            "evidence": {
                "kind": "installed-process",
                "pid": os.getpid(),
                "synthetic": False,
                "source_only": False,
                **extra_evidence,
            },
            "observation": observation,
        }
    except (SurfaceError, OSError, ValueError, subprocess.SubprocessError) as exc:
        print(f"installed_product_surface: {exc}", file=sys.stderr)
        return 1
    print(json.dumps(response, sort_keys=True, separators=(",", ":")))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
