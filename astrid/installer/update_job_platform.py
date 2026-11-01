"""Same-user transient OS jobs for installer updates."""
from __future__ import annotations

import base64
import os
import re
import shlex
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


def _environment(job: dict) -> dict[str, str]:
    """Load the selected version's canonical environment for one transient job."""
    from .core import Version
    from .environment import canonical_environment

    old = Version.load(job["old_version"])
    return canonical_environment(old.env, Path(job["data_root"]))


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
    environment = _environment(job)
    if system == "linux":
        uid = os.getuid()
        unit = f"astrid-update-{operation}.service"
        return {"format": "astrid-update-platform/v1", "system": system,
                "kind": "systemd-user-transient", "name": unit, "unit": unit,
                "user": str(uid), "operation_id": operation, "program_root": str(root),
                "log_path": str(log_path), "command": command, "environment": environment}
    if system == "darwin":
        uid = os.getuid()
        label = f"astrid-update-{operation}"
        return {"format": "astrid-update-platform/v1", "system": system,
                "kind": "launchd-gui-one-shot", "name": label, "label": label,
                "domain": f"gui/{uid}", "user": str(uid), "operation_id": operation,
                "log_path": str(log_path), "program_root": str(root),
                "plist_path": str(_private_path(job, f".{label}.plist")), "command": command,
                "environment": environment}
    user_sid = _windows_sid()
    task_name = f"astrid-update-{operation}"
    return {"format": "astrid-update-platform/v1", "system": system,
            "kind": "scheduled-task-on-demand", "name": task_name, "task_name": task_name,
            "user": user_sid, "operation_id": operation,
            "log_path": str(log_path), "program_root": str(root),
            "command": command, "environment": environment}


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
    environment = descriptor.get("environment")
    if environment is not None and (not isinstance(environment, dict)
                                    or any(not isinstance(key, str) or not key or "=" in key
                                           or not isinstance(value, str) or "\x00" in value
                                           for key, value in environment.items())):
        raise ValueError("The update platform environment is invalid.")
    return actual, name


def _failure(action: str, result: subprocess.CompletedProcess) -> RuntimeError:
    detail = (result.stderr or result.stdout or "OS command failed").strip()
    return RuntimeError(f"Could not {action} update job: {detail}")


def _launch_failure(result: subprocess.CompletedProcess) -> UpdateJobLaunchError:
    detail = (result.stderr or result.stdout or "OS command failed").strip()
    return UpdateJobLaunchError(f"Could not launch update job: {detail}")


def _plist(descriptor: dict, command: list[str], log_path: Path, environment: dict[str, str]) -> bytes:
    root = ET.Element("plist", version="1.0")
    dictionary = ET.SubElement(root, "dict")
    values = [("Label", descriptor["label"]), ("ProgramArguments", command),
              ("RunAtLoad", True), ("EnvironmentVariables", environment),
              ("StandardOutPath", str(log_path)), ("StandardErrorPath", str(log_path))]
    for key, value in values:
        ET.SubElement(dictionary, "key").text = key
        if isinstance(value, bool):
            ET.SubElement(dictionary, "true" if value else "false")
        elif isinstance(value, list):
            array = ET.SubElement(dictionary, "array")
            for item in value:
                ET.SubElement(array, "string").text = item
        elif isinstance(value, dict):
            nested = ET.SubElement(dictionary, "dict")
            for nested_key, nested_value in sorted(value.items()):
                ET.SubElement(nested, "key").text = nested_key
                ET.SubElement(nested, "string").text = nested_value
        else:
            ET.SubElement(dictionary, "string").text = value
    return ET.tostring(root, encoding="utf-8", xml_declaration=True)


def _ps_quote(value: str) -> str:
    return "'" + value.replace("'", "''") + "'"


def _encoded_ps(script: str) -> list[str]:
    encoded = base64.b64encode(script.encode("utf-16le")).decode("ascii")
    return ["powershell.exe", "-NoProfile", "-NonInteractive", "-EncodedCommand", encoded]


def _windows_worker_script(command: list[str], log: Path, environment: dict[str, str]) -> str:
    """Return the exact Scheduled Task child wrapper, including its environment."""
    return ("$ErrorActionPreference = 'Stop'\n"
            + "".join(f"$env:{key} = {_ps_quote(value)}\n"
                      for key, value in sorted(environment.items()))
            + f"$exe = {_ps_quote(command[0])}\n"
            f"$workerArgs = @({', '.join(_ps_quote(x) for x in command[1:])})\n"
            f"$log = {_ps_quote(str(log))}\n"
            "try { & $exe @workerArgs *>> $log; exit $LASTEXITCODE } "
            "catch { $_ | Out-String | Add-Content -LiteralPath $log; exit 1 }\n")


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
    environment = dict(descriptor.get("environment") or {})
    if actual == "linux":
        if descriptor.get("user") != str(os.getuid()):
            raise ValueError("The systemd update job belongs to another user.")
        args = ["systemd-run", "--user", "--no-block", "--expand-environment=no",
                f"--unit={name}", "--collect", "--property=Type=oneshot",
                "--property=Restart=no",
                f"--property=StandardOutput=append:{_systemd_path(log)}",
                f"--property=StandardError=append:{_systemd_path(log)}",
                *(f"--setenv={key}={value}" for key, value in sorted(environment.items())),
                "--", *command]
        result = _run(args)
    elif actual == "darwin":
        if descriptor.get("user") != str(os.getuid()):
            raise ValueError("The launchd update job belongs to another user.")
        plist = Path(descriptor["plist_path"])
        _write_private(plist, _plist(descriptor, command, log, environment))
        result = _run(["launchctl", "bootstrap", descriptor["domain"], str(plist)])
        if result.returncode:
            plist.unlink(missing_ok=True)
    else:
        sid = descriptor.get("user", "")
        if not isinstance(sid, str) or not sid.startswith("S-1-"):
            raise ValueError("The Scheduled Task update job has no current-user SID.")
        worker_script = _windows_worker_script(command, log, environment)
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
            if result.returncode == 4:
                return False
            raise _failure("query", result)
        state = result.stdout.strip()
        if state in {"inactive", "failed", "dead", "not-found"}:
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


def is_registered(descriptor: dict, command: list[str], system: str | None = None) -> bool:
    """Prove absence or exact native action ownership for a named update job.

    Return False only for a positively absent object. Existing objects with an
    action that cannot be parsed or does not match raise, so callers never
    retire a same-name registration based on the descriptor alone.
    """
    actual, name = _validate(descriptor, system)
    command = _argv(command)
    if actual == "linux":
        load = _run(["systemctl", "--user", "show", "--property=LoadState", "--value", name])
        if load.returncode:
            if load.returncode == 4:
                return False
            raise _failure("inspect", load)
        load_state = load.stdout.strip()
        if load_state == "not-found":
            return False
        if load_state != "loaded":
            raise RuntimeError(f"Unknown systemd update unit load state: {load_state}")
        object_path = "/org/freedesktop/systemd1/unit/" + "".join(
            char if char.isascii() and char.isalnum() else f"_{ord(char):02x}" for char in name)
        result = _run(["busctl", "--user", "--no-pager", "get-property",
                       "org.freedesktop.systemd1", object_path,
                       "org.freedesktop.systemd1.Service", "ExecStart"])
        if result.returncode:
            raise _failure("inspect", result)
        try:
            fields = shlex.split(result.stdout.strip(), posix=True)
            if len(fields) < 3 or fields[0] != "a(sasbttttuii)" or fields[1] != "1":
                raise ValueError("unexpected ExecStart signature or count")
            _type, _count, executable, argc, *tail = fields
            argc_value = int(argc)
            native_command = tail[:argc_value]
            remaining = tail[argc_value:]
            if (argc_value != len(command) or len(native_command) != argc_value
                    or len(remaining) != 8 or remaining[0] not in {"true", "false"}
                    or any(not item.isdecimal() for item in remaining[1:])):
                raise ValueError("unexpected ExecStart struct")
        except (ValueError, IndexError) as failure:
            raise RuntimeError("The systemd update unit command is ambiguous.") from failure
        if executable != command[0] or native_command != command:
            raise RuntimeError("The systemd update unit command does not match its accepted operation.")
        return True
    if actual == "darwin":
        result = _run(["launchctl", "print", f"{descriptor['domain']}/{name}"])
        if result.returncode:
            output = (result.stderr + result.stdout).lower()
            if result.returncode in {3, 113} and any(message in output for message in (
                    "could not find service", "could not find specified service", "no such process")):
                return False
            raise _failure("inspect", result)
        plist_path = Path(descriptor["plist_path"])
        from runtime_protocol import platform as runtime_platform
        if (plist_path.is_symlink() or not plist_path.is_file()
                or not runtime_platform.is_owner_only(plist_path)):
            raise RuntimeError("The launchd update service has no owned command descriptor.")
        expected_plist = _plist(descriptor, command, Path(descriptor["log_path"]),
                                dict(descriptor.get("environment") or {}))
        if plist_path.read_bytes() != expected_plist:
            raise RuntimeError("The launchd update plist does not match its accepted command.")
        # launchctl print exposes the loaded executable and arguments. Require
        # both fields so a same-label foreign service is never retired.
        output = result.stdout
        program = re.search(r"(?m)^\s*program\s*=\s*(.+?)\s*$", output)
        arguments = re.search(r"(?ms)^\s*arguments\s*=\s*\{(.*?)^\s*\}", output)
        if any("\n" in part or "\r" in part for part in command):
            raise RuntimeError("The launchd update service command is ambiguous.")
        try:
            native_program = program.group(1).strip() if program else ""
            program_values = [native_program] if native_program == command[0] else shlex.split(native_program)
        except ValueError:
            program_values = []
        if not program_values or len(program_values) != 1 or program_values[0] != command[0] or not arguments:
            raise RuntimeError("The launchd update service command is ambiguous.")
        argument_lines = [line.strip() for line in arguments.group(1).splitlines() if line.strip()]
        # launchctl prints each literal argument on its own line, including
        # spaces and the Python -c source. Compare that complete representation
        # before handling the quoted form; shell splitting loses raw arguments.
        if argument_lines == command:
            return True
        parsed = []
        try:
            for line in argument_lines:
                if line.endswith(","):
                    line = line[:-1].rstrip()
                values = shlex.split(line, posix=True)
                if len(values) != 1:
                    raise ValueError("argument line is ambiguous")
                parsed.append(values[0])
        except ValueError as failure:
            raise RuntimeError("The launchd update service arguments are ambiguous.") from failure
        if not parsed or parsed != command:
            raise RuntimeError("The launchd update service command does not match its accepted operation.")
        return True
    sid = descriptor["user"]
    script = ("$ErrorActionPreference = 'Stop'\n$all = @(Get-ScheduledTask -TaskPath '\\' -ErrorAction Stop | Where-Object { $_.TaskName -eq "
              + _ps_quote(name) + " }); if ($all.Count -eq 0) { Write-Output 'ASTRID_NOT_FOUND'; exit 0 }; "
              + "if ($all.Count -ne 1) { throw 'Ambiguous task identity' }; $t=$all[0]; "
              + "$actions = @($t.Actions | ForEach-Object { [ordered]@{ Execute=$_.Execute; Arguments=$_.Arguments; WorkingDirectory=$_.WorkingDirectory } }); "
              + "$o = [ordered]@{ TaskPath=$t.TaskPath; TaskName=$t.TaskName; UserId=$t.Principal.UserId; "
              + "LogonType=[string]$t.Principal.LogonType; RunLevel=[string]$t.Principal.RunLevel; "
              + "TriggerCount=@($t.Triggers).Count; Actions=$actions }; $o | ConvertTo-Json -Compress -Depth 5\n")
    result = _run(_encoded_ps(script))
    if result.returncode:
        raise _failure("inspect", result)
    output = result.stdout.strip()
    if output == "ASTRID_NOT_FOUND":
        return False
    try:
        observed = __import__("json").loads(output)
    except (TypeError, ValueError) as failure:
        raise RuntimeError("The Scheduled Task update action is ambiguous.") from failure
    worker_script = _windows_worker_script(command, Path(descriptor["log_path"]),
                                           dict(descriptor.get("environment") or {}))
    encoded = base64.b64encode(worker_script.encode("utf-16le")).decode("ascii")
    args = subprocess.list2cmdline(["-NoProfile", "-NonInteractive", "-EncodedCommand", encoded])
    actions = observed.get("Actions") if isinstance(observed, dict) else None
    if isinstance(actions, dict):
        actions = [actions]
    if (not isinstance(observed, dict) or observed.get("TaskPath") != "\\"
            or observed.get("TaskName") != name or observed.get("UserId") != sid
            or observed.get("LogonType") != "Interactive" or observed.get("RunLevel") != "Limited"
            or observed.get("TriggerCount") != 0 or not isinstance(actions, list) or len(actions) != 1
            or actions[0].get("Execute", "").casefold() != "powershell.exe"
            or actions[0].get("Arguments") != args
            or actions[0].get("WorkingDirectory") not in {None, ""}):
        raise RuntimeError("The Scheduled Task action does not match its accepted operation.")
    return True


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
