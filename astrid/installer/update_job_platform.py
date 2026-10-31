"""Same-user transient OS jobs for installer updates."""
from __future__ import annotations

import base64
import os
import re
import subprocess
import sys
import xml.etree.ElementTree as ET
from pathlib import Path

_NAME_RE = re.compile(r"^[A-Za-z0-9_.-]{1,180}$")
_UUID_RE = re.compile(r"^[0-9a-f]{32}$")


class UpdateJobLaunchError(RuntimeError):
    """The OS manager definitively rejected registration or start."""


def _system(system: str | None) -> str:
    value = system or sys.platform
    if value.startswith("linux"):
        return "linux"
    if value == "darwin":
        return "darwin"
    if value in {"win32", "cygwin"}:
        return "win32"
    raise ValueError(f"Unsupported update-job platform: {value}")


def _argv(command: list[str]) -> list[str]:
    if (not isinstance(command, list) or not command
            or any(not isinstance(part, str) or not part or "\x00" in part for part in command)):
        raise ValueError("An update job requires a non-empty argv of non-empty strings.")
    return list(command)


def _job_name(job: dict) -> tuple[str, str]:
    if not isinstance(job, dict):
        raise ValueError("An accepted update job record is required.")
    name, operation = job.get("job_name"), job.get("operation_id")
    if (not isinstance(name, str) or not _NAME_RE.fullmatch(name)
            or not isinstance(operation, str) or not _UUID_RE.fullmatch(operation)):
        raise ValueError("The update job name or operation identifier is invalid.")
    if name != f"astrid-update-{operation}":
        raise ValueError("The update job name does not match its operation identifier.")
    return name, operation


def _private_path(job: dict, leaf: str) -> Path:
    log_path, root = Path(job.get("log_path", "")), Path(job.get("program_root", ""))
    if not log_path.is_absolute() or not root.is_absolute() or not log_path.is_relative_to(root):
        raise ValueError("The update job log must be an absolute path inside its installation.")
    return log_path.parent / leaf


def _job_paths(job: dict) -> tuple[Path, Path]:
    log_path, root = Path(job.get("log_path", "")), Path(job.get("program_root", ""))
    if not log_path.is_absolute() or not root.is_absolute() or not log_path.is_relative_to(root):
        raise ValueError("The update job log must be an absolute path inside its installation.")
    return root, log_path


def _run(args: list[str]) -> subprocess.CompletedProcess:
    return subprocess.run(args, text=True, capture_output=True, check=False,
                          stdin=subprocess.DEVNULL, timeout=30)


def _windows_sid() -> str:
    result = _run(_encoded_ps("[System.Security.Principal.WindowsIdentity]::GetCurrent().User.Value"))
    if result.returncode:
        raise RuntimeError(f"Could not identify the current Windows user: {result.stderr.strip()}")
    match = re.search(r"S-1-(?:[0-9]+-)+[0-9]+", result.stdout)
    if not match:
        raise RuntimeError("Could not identify the current Windows user SID.")
    return match.group(0)


def _systemd_path(value: Path) -> str:
    # systemd interprets percent specifiers in StandardOutput paths.
    return str(value).replace("%", "%%")


def describe(job: dict, command: list[str], system: str | None = None) -> dict:
    """Return a durable ownership descriptor without registering an OS object."""
    system, command = _system(system), _argv(command)
    _, operation = _job_name(job)
    root, log_path = _job_paths(job)
    if system == "linux":
        uid = os.getuid()
        unit = f"astrid-update-{operation}.service"
        return {"format": "astrid-update-platform/v1", "system": system,
                "kind": "systemd-user-transient", "name": unit, "unit": unit,
                "user": str(uid), "operation_id": operation, "program_root": str(root),
                "log_path": str(log_path), "command": command}
    if system == "darwin":
        uid = os.getuid()
        label = f"astrid-update-{operation}"
        return {"format": "astrid-update-platform/v1", "system": system,
                "kind": "launchd-gui-one-shot", "name": label, "label": label,
                "domain": f"gui/{uid}", "user": str(uid), "operation_id": operation,
                "log_path": str(log_path), "program_root": str(root),
                "plist_path": str(_private_path(job, f".{label}.plist")), "command": command}
    user_sid = _windows_sid()
    task_name = f"astrid-update-{operation}"
    return {"format": "astrid-update-platform/v1", "system": system,
            "kind": "scheduled-task-on-demand", "name": task_name, "task_name": task_name,
            "user": user_sid, "operation_id": operation,
            "log_path": str(log_path), "program_root": str(root),
            "command": command}


def _validate(descriptor: dict, system: str | None = None) -> tuple[str, str]:
    if not isinstance(descriptor, dict) or descriptor.get("format") != "astrid-update-platform/v1":
        raise ValueError("The update platform descriptor is invalid.")
    actual, name = _system(descriptor.get("system")), descriptor.get("name")
    if system is not None and actual != _system(system):
        raise ValueError("The update platform descriptor belongs to another system.")
    if not isinstance(name, str) or not name or any(ord(c) < 32 for c in name):
        raise ValueError("The update platform object name is invalid.")
    expected = {"linux": ("systemd-user-transient", "unit"),
                "darwin": ("launchd-gui-one-shot", "label"),
                "win32": ("scheduled-task-on-demand", "task_name")}[actual]
    operation = descriptor.get("operation_id")
    if not isinstance(operation, str) or not _UUID_RE.fullmatch(operation):
        raise ValueError("The update platform operation identifier is invalid.")
    expected_name = {"linux": f"astrid-update-{operation}.service",
                     "darwin": f"astrid-update-{operation}",
                     "win32": f"astrid-update-{operation}"}[actual]
    if (descriptor.get("kind") != expected[0] or descriptor.get(expected[1]) != name
            or name != expected_name):
        raise ValueError("The update platform descriptor does not own the named object.")
    root, log = Path(descriptor.get("program_root", "")), Path(descriptor.get("log_path", ""))
    if not root.is_absolute() or not log.is_absolute() or not log.is_relative_to(root):
        raise ValueError("The update platform log path is invalid.")
    if actual in {"linux", "darwin"} and descriptor.get("user") != str(os.getuid()):
        raise ValueError("The update platform object belongs to another user.")
    if actual == "darwin":
        if descriptor.get("domain") != f"gui/{os.getuid()}":
            raise ValueError("The launchd update object is outside the current GUI domain.")
        expected_path = log.parent / f".{name}.plist"
        if Path(descriptor.get("plist_path", "")) != expected_path:
            raise ValueError("The launchd plist path is not the owned private path.")
    if actual == "win32":
        sid = descriptor.get("user")
        if not isinstance(sid, str) or not re.fullmatch(r"S-1-(?:[0-9]+-)+[0-9]+", sid):
            raise ValueError("The Scheduled Task user SID is invalid.")
        if _windows_sid() != sid:
            raise ValueError("The Scheduled Task belongs to another Windows user.")
    path_key = {"darwin": "plist_path"}.get(actual)
    if path_key and not Path(descriptor.get(path_key, "")).is_absolute():
        raise ValueError("The update platform descriptor path is invalid.")
    return actual, name


def _failure(action: str, result: subprocess.CompletedProcess) -> RuntimeError:
    detail = (result.stderr or result.stdout or "OS command failed").strip()
    return RuntimeError(f"Could not {action} update job: {detail}")


def _launch_failure(result: subprocess.CompletedProcess) -> UpdateJobLaunchError:
    detail = (result.stderr or result.stdout or "OS command failed").strip()
    return UpdateJobLaunchError(f"Could not launch update job: {detail}")


def _plist(descriptor: dict, command: list[str], log_path: Path) -> bytes:
    root = ET.Element("plist", version="1.0")
    dictionary = ET.SubElement(root, "dict")
    values = [("Label", descriptor["label"]), ("ProgramArguments", command),
              ("RunAtLoad", True),
              ("StandardOutPath", str(log_path)), ("StandardErrorPath", str(log_path))]
    for key, value in values:
        ET.SubElement(dictionary, "key").text = key
        if isinstance(value, bool):
            ET.SubElement(dictionary, "true" if value else "false")
        elif isinstance(value, list):
            array = ET.SubElement(dictionary, "array")
            for item in value:
                ET.SubElement(array, "string").text = item
        else:
            ET.SubElement(dictionary, "string").text = value
    return ET.tostring(root, encoding="utf-8", xml_declaration=True)


def _ps_quote(value: str) -> str:
    return "'" + value.replace("'", "''") + "'"


def _encoded_ps(script: str) -> list[str]:
    encoded = base64.b64encode(script.encode("utf-16le")).decode("ascii")
    return ["powershell.exe", "-NoProfile", "-NonInteractive", "-EncodedCommand", encoded]


def _write_private(path: Path, content: bytes) -> None:
    if path.exists() or path.is_symlink():
        raise RuntimeError(f"Refusing to overwrite update job input: {path}")
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "wb") as stream:
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
    except BaseException:
        path.unlink(missing_ok=True)
        raise


def launch(descriptor: dict, command: list[str], log_path: str | os.PathLike,
           system: str | None = None) -> None:
    """Register and start one transient job as the current user."""
    actual, name = _validate(descriptor, system)
    command, log = _argv(command), Path(log_path)
    if not log.is_absolute():
        raise ValueError("The update job log path must be absolute.")
    if log != Path(descriptor["log_path"]):
        raise ValueError("The update job log path differs from its durable descriptor.")
    if actual == "linux":
        if descriptor.get("user") != str(os.getuid()):
            raise ValueError("The systemd update job belongs to another user.")
        args = ["systemd-run", "--user", "--no-block", "--expand-environment=no",
                f"--unit={name}", "--collect", "--property=Type=oneshot",
                "--property=Restart=no",
                f"--property=StandardOutput=append:{_systemd_path(log)}",
                f"--property=StandardError=append:{_systemd_path(log)}",
                "--", *command]
        result = _run(args)
    elif actual == "darwin":
        if descriptor.get("user") != str(os.getuid()):
            raise ValueError("The launchd update job belongs to another user.")
        plist = Path(descriptor["plist_path"])
        _write_private(plist, _plist(descriptor, command, log))
        result = _run(["launchctl", "bootstrap", descriptor["domain"], str(plist)])
        if result.returncode:
            plist.unlink(missing_ok=True)
    else:
        sid = descriptor.get("user", "")
        if not isinstance(sid, str) or not sid.startswith("S-1-"):
            raise ValueError("The Scheduled Task update job has no current-user SID.")
        worker_script = ("$ErrorActionPreference = 'Stop'\n"
                         f"$exe = {_ps_quote(command[0])}\n"
                         f"$workerArgs = @({', '.join(_ps_quote(x) for x in command[1:])})\n"
                         f"$log = {_ps_quote(str(log))}\n"
                         "try { & $exe @workerArgs *>> $log; exit $LASTEXITCODE } "
                         "catch { $_ | Out-String | Add-Content -LiteralPath $log; exit 1 }\n")
        worker_encoded = base64.b64encode(worker_script.encode("utf-16le")).decode("ascii")
        powershell_arguments = subprocess.list2cmdline(
            ["-NoProfile", "-NonInteractive", "-EncodedCommand", worker_encoded])
        task_action = ("$a = New-ScheduledTaskAction -Execute 'powershell.exe' -Argument "
                       + _ps_quote(powershell_arguments) + "\n")
        task_principal = ("$p = New-ScheduledTaskPrincipal -UserId " + _ps_quote(sid)
                          + " -LogonType Interactive -RunLevel Limited\n")
        task_script = ("$ErrorActionPreference = 'Stop'\n" + task_action + task_principal
                       + "$s = New-ScheduledTaskSettingsSet -MultipleInstances IgnoreNew "
                       + "-ExecutionTimeLimit ([TimeSpan]::Zero) -AllowStartIfOnBatteries "
                       + "-DontStopIfGoingOnBatteries\n"
                       + "Register-ScheduledTask -TaskName " + _ps_quote(name)
                       + " -TaskPath '\\' -Action $a -Principal $p -Settings $s | Out-Null\n"
                       + "Start-ScheduledTask -TaskName " + _ps_quote(name) + " -TaskPath '\\'\n")
        result = _run(_encoded_ps(task_script))
    if result.returncode:
        raise _launch_failure(result)


def is_active(descriptor: dict, system: str | None = None) -> bool:
    """Query only the recorded object; raise on errors and unknown states."""
    actual, name = _validate(descriptor, system)
    if actual == "linux":
        if descriptor.get("user") != str(os.getuid()):
            raise ValueError("The systemd update job belongs to another user.")
        result = _run(["systemctl", "--user", "show", name, "--property=ActiveState", "--value"])
        if result.returncode:
            if result.returncode == 4 or "not found" in (result.stderr + result.stdout).lower():
                return False
            raise _failure("query", result)
        state = result.stdout.strip()
        if state in {"", "inactive", "failed", "dead", "not-found"}:
            return False
        if state in {"active", "activating", "reloading"}:
            return True
        raise RuntimeError(f"Unknown systemd update job state: {state}")
    if actual == "darwin":
        if descriptor.get("user") != str(os.getuid()):
            raise ValueError("The launchd update job belongs to another user.")
        result = _run(["launchctl", "print", f"{descriptor['domain']}/{name}"])
        if result.returncode != 0:
            output = (result.stderr + result.stdout).lower()
            if any(message in output for message in (
                    "could not find service", "could not find specified service", "no such process")):
                return False
            raise _failure("query", result)
        output = result.stdout.lower()
        if re.search(r"\bpid\s*=\s*[1-9][0-9]*", output) or "state = running" in output:
            return True
        if re.search(r"state\s*=\s*(?:not running|waiting|inactive|exited)\b", output):
            return False
        raise RuntimeError("Could not determine whether the launchd update job is active.")
    script = ("$ErrorActionPreference = 'Stop'\n$t = Get-ScheduledTask -TaskPath '\\' -ErrorAction Stop | Where-Object { $_.TaskName -eq "
              + _ps_quote(name) + " }; if ($t) { Write-Output $t.State } else { Write-Output 'ASTRID_NOT_FOUND' }\n")
    result = _run(_encoded_ps(script))
    if result.returncode:
        raise _failure("query", result)
    state = result.stdout.strip().splitlines()[-1] if result.stdout.strip() else ""
    if state == "ASTRID_NOT_FOUND" or state in {"Ready", "Disabled"}:
        return False
    if state == "Running":
        return True
    raise RuntimeError(f"Unknown Scheduled Task update job state: {state}")


def cleanup(descriptor: dict, system: str | None = None, *, finishing: bool = False) -> None:
    """Remove the recorded object; finishing=True is for its claimed worker."""
    actual, name = _validate(descriptor, system)
    if actual == "linux" and is_active(descriptor, system=actual):
        # The transient unit self-collects when this active worker exits.
        return
    if not finishing and is_active(descriptor, system=actual):
        raise RuntimeError("The update job is still active; owned OS object retained.")
    if actual == "linux":
        # --collect removes the unit on exit; never stop the worker from itself.
        result = _run(["systemctl", "--user", "reset-failed", name])
        output = (result.stderr + result.stdout).lower()
        absent = "not found" in output or "not loaded" in output
        if result.returncode and not absent:
            raise _failure("clean up", result)
    elif actual == "darwin":
        # Remove first: bootout can terminate the worker if it calls cleanup.
        Path(descriptor["plist_path"]).unlink(missing_ok=True)
        result = _run(["launchctl", "bootout", f"{descriptor['domain']}/{name}"])
        output = (result.stderr + result.stdout).lower()
        absent = any(message in output for message in (
            "could not find service", "could not find specified service", "no such process"))
        if result.returncode and not absent:
            raise _failure("clean up", result)
    else:
        script = ("$ErrorActionPreference = 'Stop'\n$t = Get-ScheduledTask -TaskPath '\\' -ErrorAction Stop | Where-Object { $_.TaskName -eq "
                  + _ps_quote(name) + " }; if ($t) { Unregister-ScheduledTask -TaskName "
                  + _ps_quote(name) + " -TaskPath '\\' -Confirm:$false -ErrorAction Stop }\n")
        result = _run(_encoded_ps(script))
        if result.returncode:
            raise _failure("clean up", result)
