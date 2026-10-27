# Windows Runtime and ACP host recovery

Use this runbook when the browser Runtime health check succeeds but ACP reports
a failed startup or an attempted Runtime start says it is already running.

## Source changes and verification boundary

The Windows host path uses native process creation identity and a read-only CIM
command-line observation instead of POSIX `ps` enumeration. It checks the
selected interpreter, host module, support directory, and process identity
again after observation. Windows venv launchers can start a separate interpreter
process; registration therefore distinguishes the selected launcher from the
observed process image. See [Python's venv documentation](https://docs.python.org/3.11/library/venv.html)
and [Win32_Process](https://learn.microsoft.com/windows/win32/cimwin32prov/win32-process).

Retiring a tree still requires custody. The companion Runtime change terminates
the retained Windows Job even after its leader exits and observes its active
process count reach zero. It records private cleanup evidence for that service
launch. Astrid can consume matching evidence on a supervised restart, including
when a Windows venv wrapper and the running host have different PIDs. These
Agent and Runtime changes must be delivered together; updating only the Agent
does not add Runtime cleanup evidence.

An old registration with no matching cleanup evidence remains blocked. A failed
new startup does not establish that the older host's descendants were stopped.
Do not delete its registration or infer cleanup from an absent PID. Recovery of
that legacy state needs independently established owner-side cleanup; the new
receipt protocol cannot retroactively prove it.

New Windows service declarations bind chat to the selected environment's
`astrid.exe`, preserving an explicit launcher override. Chat discovery, packaged
profile lookup, and preference lookup use the same Windows command-shim handling
as ACP startup. Failures report bounded process status rather than private child
output. These fixes do not establish the cause of the report's live `.exe`
failure: standalone profile lookup worked, but a later live diagnostic still
returned `/models` 500 and `/connect` 502 after the absolute command change.

Native Windows checks are included in CI. Local macOS checks cannot establish
that the Windows chat panel answers or a task completes on the user's existing
ComfyUI. Run the diagnostic below before changing the installation; after a
candidate update, repeat it, send a short chat message, then submit one small
existing ComfyUI workflow through Astrid and check the Runtime task result.

## Inspect without starting or stopping anything

Astrid's default Windows paths are under `%LOCALAPPDATA%`. If installation used
a custom workspace, read the `--data-root` value from the task action locally;
do not paste full task arguments into a support message.

```powershell
$task = Get-ScheduledTask -TaskName 'Astrid Runtime' -ErrorAction Stop
$task | Select-Object TaskName, State
$task.Settings | Select-Object MultipleInstances, StartWhenAvailable, ExecutionTimeLimit
Get-ScheduledTaskInfo -TaskName 'Astrid Runtime' |
  Select-Object LastRunTime, LastTaskResult, NextRunTime

$dataRoot = Join-Path $env:LOCALAPPDATA 'Astrid Runtime'
$discovery = Get-Content (Join-Path $dataRoot 'runtime\discovery.json') -Raw |
  ConvertFrom-Json
$health = Invoke-RestMethod `
  -Uri "$($discovery.door_endpoint)/api/runtime/v1/health" `
  -Headers @{ Origin = 'https://astrid.haus' }
$health | Select-Object status

Get-CimInstance Win32_Process -Filter "Name='pythonw.exe'" |
  Where-Object { $_.CommandLine -like '*astrid_runtime*' } |
  Select-Object ProcessId, Name
Get-Content (Join-Path $dataRoot 'logs\runtime-service.log') -Tail 60
```

The default healthy response has `status: ok`. The task is configured with
`MultipleInstances=IgnoreNew`, an interactive-token principal for the signed-in
user, repeating one-minute logon and time triggers, and a one-minute
restart-on-failure interval. The task action uses the installed Runtime's
explicit environment (including its versioned `PATH`) and the allowlisted
`service-env.json` settings. It does not inherit ad hoc environment changes
from the PowerShell window. The service log is
`%LOCALAPPDATA%\Astrid Runtime\logs\runtime-service.log` by default.

If health is `ok`, do not start a second Runtime or stop the scheduled task just
because ACP failed. That Runtime is already available; collect the ACP host's
own error and inspect the task state and service log locally. Logs and task
arguments can contain local paths or other operational details, so redact them
before sharing. Never share credentials, tokens, or raw environment dumps.

The 27 October 2026 Windows diagnostic report observed task state `Running`,
last task result `2147946720` (`0x800710E0`), and Runtime health HTTP 200, while
the service log recorded an attempted second `run` failing with “Runtime is
already running in another process”. This establishes that a competing Runtime
start was attempted while the door still answered; it does not identify which
launcher made that attempt or prove the responding owner's identity is stale.
Keep the healthy owner running until its process identity can be reconciled.
The same report separately observed the pack host failing on its unsupported
Windows POSIX census; that is a distinct failure from the Runtime duplicate.

For ACP, the report's standalone Bun profile lookup succeeded twice using the
configured service working directory and PATH. An absolute `astrid.exe` path was
then written into the chat service declaration. The follow-up diagnostic
confirmed this file was saved at 14:47:05 UTC, Runtime started at 14:47:37 UTC,
and the Bun bridge started at 14:47:38 UTC. The selected command was an absolute
executable in the selected installation, yet `/models` returned 500 and
`/connect` returned 502 with `Astrid ACP packaged profile lookup failed.`
The absolute-path change did not resolve the live failure. Runtime remained
healthy; pack-host had retried 73 times. Compare supervised/no-console child
startup with standalone Bun next, capturing bounded process error codes rather
than inferring an authentication problem.

To capture current, bounded evidence without printing the bridge credential or
environment, run the repository diagnostic from PowerShell:

```powershell
powershell.exe -NoProfile -ExecutionPolicy Bypass -File `
  .\scripts\diagnostics\windows_acp_bridge.ps1
```

The script resolves a custom workspace from Astrid's installer selection file;
pass `-DataRoot` only if that metadata is unavailable. It reads the protected
`runtime\chat.json` registration into memory,
checks Runtime health and the bridge's `/health`, `/models`, and `/agents`,
then POSTs `/connect` for `astrid` to perform ACP initialize only. On success it
DELETEs that temporary connection. It sends no prompt, reads no environment
dump, and prints only task/process identity fields, endpoint statuses, model
counts, and sanitized structured errors. Do not attach other files or raw logs
unless separately redacted.

If health is unavailable, use Astrid's repair launcher from a normal
PowerShell session. It verifies the managed Runtime, stops its task through the
installer's owner-aware path when needed, then registers and starts it again:

```powershell
& "$env:LOCALAPPDATA\Astrid\run-astrid.cmd"
```

Do not run `astrid_runtime run`/`up`, invoke `Start-ScheduledTask` while the
Runtime is healthy, or kill every `pythonw.exe` process. The Runtime lock
correctly rejects a second owner, and broad process kills can terminate
unrelated Python work. Astrid's task does not start or manage a user's ComfyUI
server; leave ComfyUI on port 7860 untouched.

## Installer implementation references

- Task XML, user identity, PATH, service settings, stop/start polling:
  `astrid/installer/service.py` (`render`, `command`, `Service.stop`, `Service.start`).
- Healthy repair and restart decision:
  `astrid/installer/core.py` (`Installer.run`). A healthy Runtime with unchanged
  service settings returns without restarting.
- Default Windows service log path:
  `docs/testing/TESTING.md`.
