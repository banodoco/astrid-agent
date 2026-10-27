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
output. The model-catalog budget is 30 seconds to cover sequential OMP metadata
and login-provider startup plus optional usage lookup; the old 10-second budget
killed successful discovery on the operator's Windows installation. A regression
test exercises successful discovery beyond the old cutoff, and timeout tests
still verify child-tree cleanup. The earlier live `/connect` 502 remains
unexplained; the isolated installed bundle later initialized successfully.

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

The follow-up installed-origin probe isolated a separate pack-host startup
failure. For selected version `670baed9-11443b0b-0f6d0184`, all five installed
module origins were absolute existing files with no reparse-point ancestors,
but none was beneath the profile's `source_checkout`. The installer had staged
the pinned checkout at `<version>\a` while installing the candidate noneditable
into `<version>\py\Lib\site-packages`; the host's installed-origin check
correctly rejected that mismatch. The selected `python.exe`, a second probe
with Windows system paths restored, and the scheduled-task `pythonw.exe` probe
all returned valid origin JSON, so this was not explained by interpreter
startup or the task's minimal environment. The probe approximated the caller's
environment and cannot establish the exact Action 5 process environment.

Candidate staging now verifies the selected venv by comparing its reported
`sys.prefix` with the venv containing the chosen interpreter, then checks that
all five module origins are inside that venv's `site-packages`. The candidate
profile, host service declaration, and installed source digest bind to that
product root. `/a` remains an explicit provenance/resource root for Remotion,
the timeline schema, and the capability matrix; the matrix override must be an
absolute regular file without symlinked path components under that root. This
fix applies when a candidate is staged by install/update. It does not rewrite
the already-installed version or prove that the live Windows host has adopted
the candidate; confirm the selected version and pack-host readiness after the
normal update path. ComfyUI on port 7860 is external and remains untouched.

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

For that comparison, run the profile-only subprocess diagnostic:

```powershell
powershell.exe -NoProfile -ExecutionPolicy Bypass -File `
  .\scripts\diagnostics\windows_acp_launch_probe.ps1
```

It reconstructs the configured version and service environment, then compares
four bounded profile lookups: console versus no-console launches, each with
stdin ignored or piped to EOF. It reports exit/error codes and file existence,
without printing the environment or raw child output. It creates no ACP session,
queries no models, and changes no installation settings. This reconstruction
cannot read the already-running process's environment; differences between it
and the live bridge remain possible.

The operator's follow-up profile probe succeeded in all four combinations in
198–218 ms with Bun 1.3.14. This rules out a plain no-console/stdin difference
under the reconstructed configuration; it does not test retained Job membership,
an open supervised stdin pipe, or the running bundle's full launch code.

To test those conditions, use the installed bundle in a temporary bridge:

```powershell
powershell.exe -NoProfile -ExecutionPolicy Bypass -File `
  .\scripts\diagnostics\windows_acp_isolated_bridge.ps1
```

This imports the installed `service.mjs` from its original directory, preserving
relative resources. It holds the temporary service's stdin open and gives it
its own retained Windows Job and temporary registration. It checks model
discovery and ACP initialize, records bounded subprocess diagnostics, deletes
any temporary connection, then stops the Job. It sends no prompt and does not
replace or restart the live bridge. The reported `job_drained` value states
whether cleanup was positively observed.

The operator's `isolated-bridge-4` result (`message (11).txt`, 27 October)
returned health 200, models 500, and connect 200 with successful initialize
and connection deletion. The model-catalog child was killed with SIGKILL at
10,032 ms without stderr, matching the bridge's 10-second discovery deadline.
The profile lookup completed successfully in 215 ms; Job drain was confirmed.
The asynchronous subprocess instrumentation's `timed_out: false` only means
it did not observe a native spawn timeout error; it cannot detect the bridge's
own timer. The later ACP child's SIGTERM is consistent with connection cleanup,
not evidence that initialize failed. The earlier live bridge's 502 remains
unexplained: this successful isolated run does not prove the live bridge is fixed.

The operator subsequently relayed successful OMP-stage results: startup 0.2 s,
plain model listing 5.2 s (8 models), model listing with the Astrid extension
5.5 s (96 providers), and authentication-provider discovery 5.9 s. The production
path does not repeat the plain-list baseline: it lists models with the extension,
then requests login providers over RPC, then optionally fetches usage with its
own three-second timeout. Even the two required operations total roughly 11.4 s,
exceeding the bridge's old 10-second budget. No JSON is printed until they finish.
This explains why the service's model discovery is killed despite successful
individual operations. Missing OMP in a separate interactive shell environment
does not explain the service timeout. A later run of the fully installed model
catalog command completed in 17,006 ms, exited 0, and emitted valid JSON with
96 providers and 8 models. This confirms the increased 30-second discovery
budget covers the installed catalog path; check the live picker separately
through the bridge endpoint before treating the user's app state as verified.

On commit `080382cca96e4936196ad51d14d553ae3f30f32d`, the [Windows chat job]
passed its native child-launch, slow-model-over-HTTP, and PowerShell diagnostic
checks. The [native host custody job] passed, and the [Scheduled Task host
journey] passed under both `powershell.exe` and `pwsh.exe`. These are CI results
for the checked-out commit and do not by themselves establish the state of the
operator's existing installation.

[Windows chat job]: https://github.com/banodoco/astrid-agent/actions/runs/40543065613
[native host custody job]: https://github.com/banodoco/astrid-agent/actions/runs/40543065666
[Scheduled Task host journey]: https://github.com/banodoco/astrid-agent/actions/runs/40543065600

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

## Recover a retained candidate when the previous release cannot start

A failed rollback now records `recovery_required` with separate data-restoration,
data-validation and service-health status. Ordinary installs and updates stop at
that state. They do not repeatedly restore the backup and restart the broken
previous release. A healthy rollback continues to work on every supported OS.
The reboot-assisted legacy-host transition below currently targets Windows x64;
`--recover status` also explains interrupted transactions on other platforms.

Run these commands with the **current recovery-capable bootstrap**, after its
Agent pin has been released. The installed command from an older release cannot
understand the new option. Downloading the bootstrap also avoids running the
broken selected release's host:

```powershell
Invoke-WebRequest -UseBasicParsing https://astrid.haus/install.ps1 -OutFile .\astrid-install.ps1
& .\astrid-install.ps1 --recover status
& .\astrid-install.ps1 --recover prepare
# Save other work and use Windows Restart. Do not run the ordinary installer.
& .\astrid-install.ps1 --recover resume
```

For a custom program directory, pass the same absolute `--root` to all three
commands. The bootstrap forwards these arguments unchanged to its own newly
installed `astrid install` package. No local manifest is required: recovery uses
and validates the retained candidate's `manifest.json`, version record, source
profile, installed product digest and Runtime code. It does not stage arbitrary
code or launch the previous release. This flow requires an interrupted update
that retained both a candidate and a backup, and a live workspace whose current
schema is admitted identically by both releases. A missing/incomplete candidate,
unknown schema or changed realm requires diagnosis rather than a guessed repair.

`prepare` validates actual Task Scheduler action and user ownership, fences new
task admission, stops only the selected Astrid Runtime, and disables its task.
It saves a private checkpoint under `<program>\recovery\<operation-id>` that binds
the transaction, source artifacts, live logical SQLite contents, support metadata,
retained backup, task selection, exact host-marker bytes and native boot evidence.
It does not restore the backup. SQLite WAL checkpoints may change physical pages;
logical database contents must remain unchanged. Keep the checkpoint and backup.
If preparation was interrupted before it committed, rerun `--recover prepare` in
the same boot; `--recover status` reports the durable `preparing` phase.

`resume` requires a different qualified native Windows boot identity and unchanged
checkpoint-bound state. Under the lifecycle locks, it quarantines only the exact
preboot host-marker pair. A new/changed registration, foreign ownership, same-boot
retry, unavailable boot proof, modified checkpoint or changed workspace is refused.
PID absence never substitutes for Job-drain proof, and recovery does not fabricate
cleanup receipts. Normal same-boot host retirement continues to require custody.
The boot-source ABI and the full Restart journey require native Windows release
qualification; platform-neutral tests alone do not establish that boundary.

Forward recovery runs the candidate's real Runtime `upgrade` to reconcile source
profiles and catalog/support metadata even when no schema DDL is needed. It stops
any temporary Runtime, selects/registers/starts the candidate, and completes only
after Runtime/browser health and pack-host readiness pass. It retains the live
workspace, backup and quarantine. New backups keep process markers as separate
evidence; restoring an older backup does not resurrect its host registration or
overwrite the live cleanup proof.

Durable recovery stages can be retried with `--recover resume`. An interruption
inside Runtime's own upgrade may leave an incomplete `upgrade-journal.json`.
Recovery preserves that journal and stops with an explicit reconciliation error;
it never deletes it or invents a completion record. The incident candidate Runtime
`6d53a190` lacks `upgrade --reconcile`, so that case needs a compatible Runtime
with a supported reconciliation API. A complete Runtime upgrade interrupted before
the installer's next phase can be stopped and safely replayed. This limitation is
separate from the retained legacy-host marker repair.

### Prerelease operator acceptance handoff

This is a release qualification procedure, not a published repair artifact.
Use a disposable Windows x64 machine that can perform a real Restart, first on
Windows 10 and then on Windows 11. Keep the retained backup and a copy of the
broken-old/legacy-marker installation. Run this journey against the live clone;
copying a backup over the realm during recovery would invalidate the test.

The release owner must supply and record hashes for these exact inputs before
the Windows agent executes the procedure:

| Input | Prerelease qualification selection |
| --- | --- |
| Bootstrap `public/install.ps1` and base manifest/tool pins | App `d174f667aa04a7c43e6b0f767abb168b6022a6ab` |
| Bootstrap Agent package | Agent `cf4db6a1956451fe5233326f17445fdf08ca7151` (or a later explicitly reviewed descendant with the same recovery changes) |
| Bootstrap Runtime and Python client packages | Runtime `9fed6cfc02a1a130011f97af701f5edf9eb374b0` |
| Retained candidate | The candidate already named by the installation's interrupted transaction, including incident `3204bb00-6d53a190-bffecb79` |

Prepare a local manifest from the App base manifest, changing only `agent.sha`
and `runtime.sha` to those bootstrap package commits. Keep all tool pins and
asset hashes. Save it beside the checked, pinned bootstrap as
`recovery-acceptance-manifest.json`. The bootstrap's existing `--manifest` option
installs those packages into its separate temporary environment; it does not
replace the retained candidate selected for forward recovery. Do not substitute
the selected old environment's Python or the withdrawn helper ZIP. Confirm the
Agent native recovery CI at the exact tested ref before starting this journey.

From that supplied directory, the commands are:

```powershell
& .\install.ps1 --manifest .\recovery-acceptance-manifest.json --recover status
& .\install.ps1 --manifest .\recovery-acceptance-manifest.json --recover prepare
# Negative case: same-boot resume must refuse without quarantining markers.
& .\install.ps1 --manifest .\recovery-acceptance-manifest.json --recover resume
# Save other work, use Windows Restart, sign in, and return to this directory.
& .\install.ps1 --manifest .\recovery-acceptance-manifest.json --recover resume
& .\install.ps1 --manifest .\recovery-acceptance-manifest.json --recover status
```

Append the same absolute `--root` on every line for a custom installation.
Before Restart, retain the nonzero same-boot refusal and `prepared` status,
checkpoint path/hash, operation/realm/candidate identities, disabled/nonrunning
Astrid task, and unchanged live logical data/backup digests. An unavailable,
denied, mismatched or unsupported native boot source must also refuse with an
explanatory status error. It must not stop a different owner, remove markers,
write drain receipts, or advise bypassing the check.

After the real Restart, require `step: complete`, recovery `phase: complete` and
`service_healthy: true`; the selected candidate and realm must match the original
transaction. Retain the old marker quarantine and unchanged backup. Check the
actual enabled Task Scheduler action uses the candidate, the task is running,
and authenticated Runtime status, discovery and browser health report the same
Runtime instance. Check new service-log output for competing startup attempts;
Windows venv launcher/child PIDs alone do not establish duplicate Runtime owners.
Require pack-host readiness from this candidate after the quarantine.

Finally, use Astrid's installed host to dispatch one existing, small creative
workflow to the user's existing ComfyUI on port **7860**, and retain the durable
Runtime task ID, completed settlement and produced artifact. A health response or
chat response alone does not satisfy this gate. Preserve the existing ComfyUI
checkout, configuration, models and artifacts; do not provision another ComfyUI
or download replacement models for this check. Windows Restart ends existing
processes, so reopen that same ComfyUI installation if needed before dispatch.
Record source/package/schema hashes and Windows build with the evidence. Native
CI without this actual Restart and installed creative-task settlement leaves
this operator acceptance gate open.
