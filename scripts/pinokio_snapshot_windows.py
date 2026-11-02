"""Bounded, fail-closed Windows snapshots for Pinokio action boundaries.

The collector owns the PowerShell program and executable selection.  Callers
may select only the exact scheduled-task paths and service names to observe;
they cannot inject a command or a parser.  The resulting object is suitable
for canonical JSON and carries its own structural digest, but remains a
bounded snapshot rather than a gap-free event record.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import subprocess
import time
from datetime import datetime, timezone
from typing import Callable, Final, Mapping, Sequence


SCHEMA: Final = "astrid.r25.snapshot.windows.v1"
SOURCE_PROFILE: Final = "windows-cim-task-service-snapshot-v1"
POWERSHELL: Final = r"C:\Windows\System32\WindowsPowerShell\v1.0\powershell.exe"
RUNNER: Final = "windows-2022"
SHELL: Final = "powershell.exe"

DEFAULT_MAX_PROCESSES: Final = 4096
DEFAULT_MAX_TARGETS: Final = 64
DEFAULT_MAX_TASK_ACTIONS: Final = 32
DEFAULT_MAX_OUTPUT_BYTES: Final = 8 * 1024 * 1024
DEFAULT_TIMEOUT_SECONDS: Final = 30
_MAX_PID: Final = 0xFFFFFFFF
_MAX_TEXT: Final = 32768
_TASK_PATH = re.compile(r"^\\(?:[^\\\x00-\x1f]+\\)*[^\\\x00-\x1f]+$")
_SERVICE_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9 _.$-]{0,255}$")


class WindowsSnapshotError(RuntimeError):
    """The platform observation was unavailable, malformed, or ambiguous."""


# Input arrives on stdin, not through a caller-controlled command string.
# Per-target failures remain observations; failure to perform the encompassing
# process/CIM command makes PowerShell exit nonzero and the collector refuses.
_POWERSHELL_PROGRAM: Final = r"""
$ErrorActionPreference = 'Stop'
[Console]::OutputEncoding = [Text.UTF8Encoding]::new($false)
$request = [Console]::In.ReadToEnd() | ConvertFrom-Json -ErrorAction Stop

function Error-Fact($record) {
    return [ordered]@{
        type = [string]$record.Exception.GetType().FullName
        hresult = [int]$record.Exception.HResult
        fully_qualified_error_id = [string]$record.FullyQualifiedErrorId
        category = [string]$record.CategoryInfo.Category
    }
}

$processes = @(Get-CimInstance -ClassName Win32_Process -ErrorAction Stop)
if ($processes.Count -gt [int]$request.max_processes) {
    throw "Win32_Process capacity exceeded"
}
$processRows = @($processes | ForEach-Object {
    $created = $null
    if ($null -ne $_.CreationDate) {
        $created = ([DateTime]$_.CreationDate).ToUniversalTime().ToString('o')
    }
    [ordered]@{
        pid = [int64]$_.ProcessId
        parent_pid = [int64]$_.ParentProcessId
        command_line = if ($null -eq $_.CommandLine) { $null } else { [string]$_.CommandLine }
        creation_time_utc = $created
        executable_path = if ($null -eq $_.ExecutablePath) { $null } else { [string]$_.ExecutablePath }
    }
})

$scheduler = New-Object -ComObject 'Schedule.Service'
$scheduler.Connect()
$taskRows = @($request.scheduled_task_paths | ForEach-Object {
    $target = [string]$_
    try {
        $task = $scheduler.GetTask($target)
        $actionRows = @($task.Definition.Actions | ForEach-Object {
            [ordered]@{
                type = [int]$_.Type
                execute = if ($null -eq $_.Path) { $null } else { [string]$_.Path }
                arguments = if ($null -eq $_.Arguments) { $null } else { [string]$_.Arguments }
                working_directory = if ($null -eq $_.WorkingDirectory) { $null } else { [string]$_.WorkingDirectory }
            }
        })
        if ($actionRows.Count -gt [int]$request.max_task_actions) {
            throw "scheduled-task action capacity exceeded"
        }
        $states = @('unknown', 'disabled', 'queued', 'ready', 'running')
        $stateNumber = [int]$task.State
        if ($stateNumber -lt 0 -or $stateNumber -ge $states.Count) {
            throw "scheduled-task state is outside the reviewed enum"
        }
        [ordered]@{
            target = $target
            status = 'present'
            state = $states[$stateNumber]
            enabled = [bool]$task.Enabled
            actions = $actionRows
        }
    } catch [System.IO.FileNotFoundException] {
        [ordered]@{ target = $target; status = 'absent' }
    } catch [System.Runtime.InteropServices.COMException] {
        if ($_.Exception.HResult -eq -2147024894) {
            [ordered]@{ target = $target; status = 'absent' }
        } else {
            [ordered]@{ target = $target; status = 'collection-error'; error = Error-Fact $_ }
        }
    } catch {
        [ordered]@{ target = $target; status = 'collection-error'; error = Error-Fact $_ }
    }
})

$serviceRows = @($request.service_names | ForEach-Object {
    $target = [string]$_
    try {
        $escaped = $target.Replace("'", "''")
        $services = @(Get-CimInstance -ClassName Win32_Service -Filter "Name = '$escaped'" -ErrorAction Stop)
        if ($services.Count -eq 0) {
            [ordered]@{ target = $target; status = 'absent' }
        } elseif ($services.Count -ne 1) {
            throw "service query returned an ambiguous result"
        } else {
            $service = $services[0]
            [ordered]@{
                target = $target
                status = 'present'
                state = [string]$service.State
                start_type = [string]$service.StartMode
                process_id = [int64]$service.ProcessId
            }
        }
    } catch {
        [ordered]@{ target = $target; status = 'collection-error'; error = Error-Fact $_ }
    }
})

[ordered]@{
    processes = $processRows
    scheduled_tasks = $taskRows
    services = $serviceRows
} | ConvertTo-Json -Depth 8 -Compress
""".strip()


def _canonical(value: object) -> bytes:
    try:
        return json.dumps(
            value,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=True,
            allow_nan=False,
        ).encode("ascii")
    except (TypeError, ValueError, OverflowError) as exc:
        raise WindowsSnapshotError("observation is not canonical-JSON-compatible") from exc


def _unique_targets(values: Sequence[str], *, kind: str, maximum: int) -> list[str]:
    if isinstance(values, (str, bytes)) or not isinstance(values, Sequence):
        raise WindowsSnapshotError(f"{kind} targets must be a sequence")
    if len(values) > maximum:
        raise WindowsSnapshotError(f"{kind} target capacity exceeded")
    result: list[str] = []
    seen: set[str] = set()
    for value in values:
        if not isinstance(value, str):
            raise WindowsSnapshotError(f"{kind} target must be a string")
        valid = _TASK_PATH.fullmatch(value) if kind == "scheduled-task" else _SERVICE_NAME.fullmatch(value)
        if not valid or value != value.strip() or len(value) > 512:
            raise WindowsSnapshotError(f"unsafe {kind} target")
        key = value.casefold()
        if key in seen:
            raise WindowsSnapshotError(f"duplicate {kind} target")
        seen.add(key)
        result.append(value)
    return result


def _strict_object(value: object, keys: set[str], label: str) -> Mapping[str, object]:
    if not isinstance(value, dict) or set(value) != keys:
        raise WindowsSnapshotError(f"{label} has missing or unknown fields")
    return value


def _bounded_text(
    value: object, label: str, *, nullable: bool = False, allow_empty: bool = False
) -> str | None:
    if value is None and nullable:
        return None
    if (
        not isinstance(value, str)
        or (not value and not allow_empty)
        or len(value) > _MAX_TEXT
        or "\x00" in value
    ):
        raise WindowsSnapshotError(f"{label} is malformed")
    return value


def _pid(value: object, label: str) -> int:
    if type(value) is not int or value < 0 or value > _MAX_PID:
        raise WindowsSnapshotError(f"{label} is malformed")
    return value


def _creation_time(value: object) -> str:
    text = _bounded_text(value, "process creation_time_utc")
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError as exc:
        raise WindowsSnapshotError("process creation_time_utc is malformed") from exc
    if parsed.tzinfo is None or parsed.utcoffset() != timezone.utc.utcoffset(parsed):
        raise WindowsSnapshotError("process creation_time_utc must be UTC")
    return text


def _error_fact(value: object, label: str) -> dict[str, object]:
    row = _strict_object(
        value,
        {"type", "hresult", "fully_qualified_error_id", "category"},
        label,
    )
    if type(row["hresult"]) is not int or not -(2**31) <= row["hresult"] < 2**31:
        raise WindowsSnapshotError(f"{label}.hresult is malformed")
    return {
        "type": _bounded_text(row["type"], f"{label}.type"),
        "hresult": row["hresult"],
        "fully_qualified_error_id": _bounded_text(
            row["fully_qualified_error_id"], f"{label}.fully_qualified_error_id"
        ),
        "category": _bounded_text(row["category"], f"{label}.category"),
    }


def _process_rows(value: object, maximum: int) -> list[dict[str, object]]:
    if not isinstance(value, list):
        raise WindowsSnapshotError("process rows must be a list")
    if len(value) > maximum:
        raise WindowsSnapshotError("process capacity exceeded")
    rows: list[dict[str, object]] = []
    seen: set[int] = set()
    for index, value_row in enumerate(value):
        row = _strict_object(
            value_row,
            {"pid", "parent_pid", "command_line", "creation_time_utc", "executable_path"},
            f"processes[{index}]",
        )
        process_id = _pid(row["pid"], f"processes[{index}].pid")
        if process_id in seen:
            raise WindowsSnapshotError("process rows contain a duplicate PID")
        seen.add(process_id)
        rows.append(
            {
                "pid": process_id,
                "parent_pid": _pid(row["parent_pid"], f"processes[{index}].parent_pid"),
                "command_line": _bounded_text(
                    row["command_line"], f"processes[{index}].command_line", nullable=True
                ),
                "creation_time_utc": _creation_time(row["creation_time_utc"]),
                "executable_path": _bounded_text(
                    row["executable_path"], f"processes[{index}].executable_path", nullable=True
                ),
            }
        )
    return sorted(rows, key=lambda item: item["pid"])


def _task_rows(
    value: object, targets: Sequence[str], maximum_actions: int
) -> list[dict[str, object]]:
    if not isinstance(value, list) or len(value) != len(targets):
        raise WindowsSnapshotError("scheduled-task rows do not match requested targets")
    indexed: dict[str, dict[str, object]] = {}
    for index, value_row in enumerate(value):
        if not isinstance(value_row, dict):
            raise WindowsSnapshotError(f"scheduled_tasks[{index}] is malformed")
        target = value_row.get("target")
        if not isinstance(target, str) or target.casefold() in indexed:
            raise WindowsSnapshotError("scheduled-task rows contain a duplicate or malformed target")
        status = value_row.get("status")
        if status == "absent":
            _strict_object(value_row, {"target", "status"}, f"scheduled_tasks[{index}]")
            parsed: dict[str, object] = {"target": target, "status": status}
        elif status == "collection-error":
            row = _strict_object(
                value_row, {"target", "status", "error"}, f"scheduled_tasks[{index}]"
            )
            parsed = {"target": target, "status": status, "error": _error_fact(row["error"], "task error")}
        elif status == "present":
            row = _strict_object(
                value_row,
                {"target", "status", "state", "enabled", "actions"},
                f"scheduled_tasks[{index}]",
            )
            if row["state"] not in {"unknown", "disabled", "queued", "ready", "running"}:
                raise WindowsSnapshotError("scheduled-task state is malformed")
            if type(row["enabled"]) is not bool or not isinstance(row["actions"], list):
                raise WindowsSnapshotError("scheduled-task present row is malformed")
            if len(row["actions"]) > maximum_actions:
                raise WindowsSnapshotError("scheduled-task action capacity exceeded")
            actions = []
            for action_index, value_action in enumerate(row["actions"]):
                action = _strict_object(
                    value_action,
                    {"type", "execute", "arguments", "working_directory"},
                    f"scheduled_tasks[{index}].actions[{action_index}]",
                )
                if type(action["type"]) is not int or not 0 <= action["type"] <= 255:
                    raise WindowsSnapshotError("scheduled-task action type is malformed")
                actions.append(
                    {
                        "type": action["type"],
                        "execute": _bounded_text(
                            action["execute"], "task action execute", nullable=True, allow_empty=True
                        ),
                        "arguments": _bounded_text(
                            action["arguments"], "task action arguments", nullable=True, allow_empty=True
                        ),
                        "working_directory": _bounded_text(
                            action["working_directory"],
                            "task action working_directory",
                            nullable=True,
                            allow_empty=True,
                        ),
                    }
                )
            parsed = {
                "target": target,
                "status": status,
                "state": row["state"],
                "enabled": row["enabled"],
                "actions": actions,
            }
        else:
            raise WindowsSnapshotError("scheduled-task status is malformed")
        indexed[target.casefold()] = parsed
    if set(indexed) != {target.casefold() for target in targets}:
        raise WindowsSnapshotError("scheduled-task rows differ from requested targets")
    return [indexed[target.casefold()] for target in targets]


def _service_rows(value: object, targets: Sequence[str]) -> list[dict[str, object]]:
    if not isinstance(value, list) or len(value) != len(targets):
        raise WindowsSnapshotError("service rows do not match requested targets")
    indexed: dict[str, dict[str, object]] = {}
    for index, value_row in enumerate(value):
        if not isinstance(value_row, dict):
            raise WindowsSnapshotError(f"services[{index}] is malformed")
        target = value_row.get("target")
        if not isinstance(target, str) or target.casefold() in indexed:
            raise WindowsSnapshotError("service rows contain a duplicate or malformed target")
        status = value_row.get("status")
        if status == "absent":
            _strict_object(value_row, {"target", "status"}, f"services[{index}]")
            parsed: dict[str, object] = {"target": target, "status": status}
        elif status == "collection-error":
            row = _strict_object(value_row, {"target", "status", "error"}, f"services[{index}]")
            parsed = {"target": target, "status": status, "error": _error_fact(row["error"], "service error")}
        elif status == "present":
            row = _strict_object(
                value_row,
                {"target", "status", "state", "start_type", "process_id"},
                f"services[{index}]",
            )
            parsed = {
                "target": target,
                "status": status,
                "state": _bounded_text(row["state"], "service state"),
                "start_type": _bounded_text(row["start_type"], "service start_type"),
                "process_id": _pid(row["process_id"], "service process_id"),
            }
        else:
            raise WindowsSnapshotError("service status is malformed")
        indexed[target.casefold()] = parsed
    if set(indexed) != {target.casefold() for target in targets}:
        raise WindowsSnapshotError("service rows differ from requested targets")
    return [indexed[target.casefold()] for target in targets]


def _no_duplicate_json(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise WindowsSnapshotError(f"PowerShell returned duplicate JSON key {key!r}")
        result[key] = value
    return result


def collect_windows_snapshot(
    *,
    scheduled_task_paths: Sequence[str] = (),
    service_names: Sequence[str] = (),
    max_processes: int = DEFAULT_MAX_PROCESSES,
    max_targets: int = DEFAULT_MAX_TARGETS,
    max_task_actions: int = DEFAULT_MAX_TASK_ACTIONS,
    max_output_bytes: int = DEFAULT_MAX_OUTPUT_BYTES,
    timeout_seconds: int = DEFAULT_TIMEOUT_SECONDS,
    run: Callable[..., object] = subprocess.run,
) -> dict[str, object]:
    """Capture one OS-owned observation through the fixed Windows PowerShell."""
    limits = {
        "max_processes": max_processes,
        "max_targets": max_targets,
        "max_task_actions": max_task_actions,
        "max_output_bytes": max_output_bytes,
        "timeout_seconds": timeout_seconds,
    }
    if any(type(value) is not int or value < 1 for value in limits.values()):
        raise WindowsSnapshotError("collector limits must be positive integers")
    tasks = _unique_targets(scheduled_task_paths, kind="scheduled-task", maximum=max_targets)
    services = _unique_targets(service_names, kind="service", maximum=max_targets)
    if len(tasks) + len(services) > max_targets:
        raise WindowsSnapshotError("combined target capacity exceeded")
    request = {
        "scheduled_task_paths": tasks,
        "service_names": services,
        "max_processes": max_processes,
        "max_task_actions": max_task_actions,
    }
    started_utc = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
    started_monotonic = time.monotonic_ns()
    argv = [
        POWERSHELL,
        "-NoLogo",
        "-NoProfile",
        "-NonInteractive",
        "-ExecutionPolicy",
        "Bypass",
        "-Command",
        _POWERSHELL_PROGRAM,
    ]
    try:
        completed = run(
            argv,
            input=_canonical(request).decode("ascii"),
            capture_output=True,
            text=True,
            timeout=timeout_seconds,
            check=False,
            shell=False,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise WindowsSnapshotError("Windows snapshot command is unavailable") from exc
    completed_monotonic = time.monotonic_ns()
    completed_utc = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
    returncode = getattr(completed, "returncode", None)
    stdout = getattr(completed, "stdout", None)
    if type(returncode) is not int or returncode != 0:
        raise WindowsSnapshotError(f"Windows snapshot command failed with exit {returncode!r}")
    if not isinstance(stdout, str):
        raise WindowsSnapshotError("Windows snapshot command returned non-text output")
    raw = stdout.lstrip("\ufeff").strip()
    if len(raw.encode("utf-8")) > max_output_bytes:
        raise WindowsSnapshotError("Windows snapshot output capacity exceeded")
    try:
        payload = json.loads(
            raw,
            object_pairs_hook=_no_duplicate_json,
            parse_constant=lambda value: (_ for _ in ()).throw(
                WindowsSnapshotError(f"PowerShell returned non-JSON number {value}")
            ),
        )
    except (json.JSONDecodeError, UnicodeError) as exc:
        raise WindowsSnapshotError("PowerShell returned malformed JSON") from exc
    root = _strict_object(payload, {"processes", "scheduled_tasks", "services"}, "PowerShell result")
    observation: dict[str, object] = {
        "schema": SCHEMA,
        "source": {
            "profile": SOURCE_PROFILE,
            "platform": "windows",
            "runner": RUNNER,
            "shell": SHELL,
            "executable": POWERSHELL,
            "process_interface": "Get-CimInstance Win32_Process",
            "task_interface": "Task Scheduler COM",
            "service_interface": "Get-CimInstance Win32_Service",
            "continuity": "bounded-snapshot",
        },
        "capture": {
            "started_at_utc": started_utc,
            "completed_at_utc": completed_utc,
            "started_monotonic_ns": started_monotonic,
            "completed_monotonic_ns": completed_monotonic,
            "collector_pid": os.getpid(),
        },
        "limits": limits,
        "targets": {"scheduled_task_paths": tasks, "service_names": services},
        "processes": _process_rows(root["processes"], max_processes),
        "scheduled_tasks": _task_rows(root["scheduled_tasks"], tasks, max_task_actions),
        "services": _service_rows(root["services"], services),
    }
    observation["structural_sha256"] = hashlib.sha256(_canonical(observation)).hexdigest()
    return observation


__all__ = [
    "POWERSHELL",
    "RUNNER",
    "SCHEMA",
    "SHELL",
    "SOURCE_PROFILE",
    "WindowsSnapshotError",
    "collect_windows_snapshot",
]
