"""Read-only initial R0 task observation; never authority for a mutation.

The selected installation and current token are independent of the task XML.
The returned snapshot is in memory only: durable custody and replacement-safe
manager operations remain prerequisites for the Windows handoff.
"""
from __future__ import annotations

import json
import os
import re
import subprocess
from dataclasses import dataclass
from pathlib import Path

from .core import absolute, current
from .service import LABEL, TASK, _release0_service_command
from .windows_task_identity import TaskIdentity, TaskIdentityError, verify_task_definition

# Root enumeration avoids treating a named lookup's error text as absence.
# Keep query and export in one read-only invocation; no claim is made that the
# resulting object remains the same scheduler registration after it returns.
_QUERY = r"""
$tasks=@(Get-ScheduledTask -TaskPath '\' -ErrorAction Stop | Where-Object { $_.TaskName -eq 'Astrid Runtime' -and $_.TaskPath -eq '\' })
$sid=[Security.Principal.WindowsIdentity]::GetCurrent().User.Value
if ($tasks.Count -gt 1) { throw 'Ambiguous Astrid Runtime tasks' }
if ($tasks.Count -eq 0) {
    @{status='absent'; sid=$sid} | ConvertTo-Json -Compress
} else {
    $task=$tasks[0]
    $xml=[string]($task | Export-ScheduledTask -ErrorAction Stop)
    @{status='present'; sid=$sid; name=[string]$task.TaskName; path=[string]$task.TaskPath; xml=$xml} | ConvertTo-Json -Compress
}
"""


@dataclass(frozen=True)
class R0TaskObservation:
    identity: TaskIdentity
    enabled: bool


def _query():
    # Do not use Backend.run: it writes its captured output to an installer log.
    executable = Path(os.environ.get("SystemRoot", r"C:\Windows")) / "System32/WindowsPowerShell/v1.0/powershell.exe"
    result = subprocess.run(
        [str(executable), "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass",
         "-Command", "$ErrorActionPreference='Stop'; [Console]::OutputEncoding=[Text.UTF8Encoding]::new($false); " + _QUERY],
        check=True, capture_output=True, encoding="utf-8", errors="strict", timeout=30,
    )
    if result.stderr.strip():
        raise ValueError("Unexpected scheduler query diagnostics.")
    return result.stdout


def _object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("Duplicate scheduler response field.")
        result[key] = value
    return result


def observe_selected_r0(service, data):
    """Observe and admit the selected R0 task, or return None for absence.

    The caller must hold the installer's selection lock. This reads the current
    version and installer selection independently of the task and rechecks both
    after the manager call. A returned definition is not persisted, does not
    prove process state, and cannot authorize stop, registration, or recovery.
    """
    if service.system != "win32":
        raise ValueError("R0 Scheduled Task observation requires Windows.")
    root, data = absolute(service.root), absolute(data)
    if root != service.root:
        raise ValueError("The selected installation root is not canonical.")
    selection = service.recorded()
    if (selection.get("owner") != LABEL or selection.get("program_root") != str(root)
            or selection.get("data_root") != str(data)):
        raise ValueError("The installer selection does not bind these R0 roots.")
    version = current(root)
    if (version is None or version.release != 0 or version.managed_by is not None
            or not version.python.is_relative_to(version.root)
            or not version.profile.is_relative_to(version.root)):
        raise ValueError("The current selected version is not a direct R0 installation.")
    selected = version.document()
    output = _query()
    if not isinstance(output, str) or len(output) > 256 * 1024:
        raise ValueError("Malformed scheduler response.")
    try:
        response = json.loads(output, object_pairs_hook=_object)
    except (ValueError, TypeError) as exc:
        raise ValueError("Malformed scheduler response.") from exc
    if not isinstance(response, dict):
        raise ValueError("Malformed scheduler response.")
    if service.recorded() != selection or (again := current(root)) is None or again.document() != selected:
        raise ValueError("The selected installation changed during observation.")
    sid = response.get("sid")
    # Validate SID even on absence: failed or partial identity acquisition is
    # not a successful observation.
    if not isinstance(sid, str) or not re.fullmatch(r"S-1-\d+(?:-\d+)+", sid):
        raise ValueError("The current Windows SID is unavailable.")
    if response.get("status") == "absent" and set(response) == {"status", "sid"}:
        return None
    if (response.get("status") != "present"
            or set(response) != {"status", "sid", "name", "path", "xml"}):
        raise ValueError("Malformed scheduler response.")

    # Only complete, locally reconstructed R0 action bytes are admitted. Both
    # sidecar locations are historical forms; no script from the task is run.
    for env_file in (version.root / "service-env.json", service.env_file):
        argv = _release0_service_command(version, data, windows=True, log=service.log,
                                         env_file=env_file)
        identity = TaskIdentity(TASK, sid, argv[0], subprocess.list2cmdline(argv[1:]),
                                str(version.root), response["xml"])
        for enabled in (True, False):
            try:
                verify_task_definition(response["xml"], task_path=response["path"],
                                       task_name=response["name"], expected=identity, enabled=enabled)
            except TaskIdentityError:
                continue
            return R0TaskObservation(identity, enabled)
    raise TaskIdentityError("The live R0 task does not match the selected installation and current SID.")
