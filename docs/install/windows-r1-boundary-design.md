# Windows R0-to-R1 boundary: F-767ae0 implementation plan

Status: **blocked on implementation; Windows remains refused**. This is a
design and acceptance contract, not platform qualification. No hosted run or
LAND until M-R1FIX supplies the independent Astra data check for the eventual
implementation. M-QUAL owns the Windows row, including its installer changes.

Inspected Agent base: `7a731bb77cccc4b5365171a30f92069ff4213265`.
Runtime reference inspected separately: local `origin/int/r1` at
`9fc53b771134940aaac901e4cfc5d03c1db1a20a`; this is not a claim about the
Runtime wheel selected by a hosted run.

## Why a registration-only patch is unsafe

The constructor guard is only the first boundary. Allowing `win32` reaches:

| Component | Missing Windows behavior |
| --- | --- |
| `boundary_registration.stage_runtime_launcher` | Explicit Windows no-op; only shell launchers are staged. |
| `BoundaryRegistration` | Paths, definitions, prepare, activation and recovery select systemd or launchd. No Windows manager branch exists. |
| `r0_stop` | R0 manager validation, R1 absence and R0 recovery refuse Windows. Existing task-name disable/start calls are not ownership evidence. |
| `BoundaryHandoff._records` and staged Python runner | Every non-Linux service label becomes the macOS label. |
| Registration publication | Raw `os.fchmod`; interrupted two-link publication uses `os.geteuid` and POSIX modes. Windows ACL/ownership and durability need explicit treatment. |
| `r0_stop.owner_lock` | Raw `O_NOFOLLOW` and `O_NONBLOCK`; cannot simply erase flags on Windows and preserve reparse-point protection. |
| `adoption_receipt.observe` | Raw `os.listdir(fd)`, `dir_fd`, `O_DIRECTORY` and `O_NOFOLLOW`; independent source observation is not Windows portable. |
| Runtime `workspace_copy` | Raw POSIX descriptors, permissions and directory operations; `_publish` explicitly supports only Linux `renameat2` or macOS `renameatx_np`. Windows exclusive destination publication is absent. |

The last two are mandatory data-preservation checks, not incidental platform
syntax. Runtime's existing `runtime_protocol.dirfd` abstraction is a candidate
for reuse, but importing that facade alone does not implement the offline-copy
publication contract or prove identical inventory semantics. A task-only patch
could stop R0 before encountering these failures and then require unimplemented
Windows recovery. This is not safely small enough to enable as a local branch
of the existing constructor.

## Smallest complete design

Keep the existing serialized handoff and external journals. Add one Windows
Scheduled Task adapter; do not reuse legacy `Service.register`, whose task name
is R0 and whose `-Force` registration can replace an existing task. Keep R0's
local `task.xml`, task registration content and source root unchanged, apart
from the authorized service enabled/running state transition.

Use exactly `\Astrid Runtime` for R0 and `\Astrid Runtime R1` for R1. Keep the
R1 definition at `<program>/task-r1.xml`, outside both data roots. Introduce one
platform-to-label/path mapping used by registration, selection, launcher guards
and recovery. A missing task, an inaccessible scheduler and a task with changed
identity must be three different outcomes.

The adapter's interface should be `observe(identity)`,
`create_disabled(identity, definition)`, `disable_stop(identity)`,
`enable_start(identity)` and `remove_disabled(identity)`. Each mutation must
revalidate identity in the same manager invocation, then verify its result.
Unknown or ambiguous state retains the journal and refuses further mutation.

For R0, bind the exact root task name, current user's SID, interactive-token
principal and least-privilege run level; require one Exec action with the exact
command, arguments and working directory from the frozen R0 definition. Bind
that action to the selected R0 Version/program/data roots, using exact known
R0 command serialization, not a command prefix or executable under a directory.
Compare triggers/settings and reject extra actions or principals. Preserve the
authentic R0 repetition/time trigger: it is a reason to disable before stop,
not permission to rewrite its definition. Journal the normalized identity and
the local definition digest before disabling R0.

For R1, additionally bind a random publication identity in the task definition
and journal. Query and compare the manager's exported definition; local XML or
a matching name alone is insufficient. Normalize only documented scheduler
serialization differences and the enabled bit that this transaction changes.
Do not accept arbitrary unknown fields. Creation must be exclusive: no `-Force`
and no update-existing registration flag. A competing same-name task must cause
refusal. Missing local R1 bytes with an existing manager task requires manual
resolution unless the full journal-bound manager identity is independently
provable.

**Replacement race boundary:** Task Scheduler does not offer a Python-level
compare-and-swap through these existing cmdlets. Merely querying then calling
`Stop-ScheduledTask -TaskName ...` is insufficient. Prototype mutation through
the verified task object in one invocation, including fresh definition checks,
and test replacement at each boundary. The independent reviewer must determine
whether the native API binds the mutation to that registration. If it still
resolves by name, either establish an explicit external reconfiguration
exclusion or continue refusing; do not claim race immunity from postchecks.

## Launcher and filesystem work

The fixed Windows launcher must resolve `current`, validate its identifier and
the same committed selection/marker fields as the POSIX runner, then select the
candidate interpreter and staged runner with an explicit argument vector. Use
the selected candidate's environment/profile, isolated Python mode and a log
outside both retained roots. Never run a `.cmd` string containing user paths.

A possible minimal implementation is a fixed PowerShell script invoked by the
absolute system PowerShell path, dispatching the selected candidate's Python
runner. This needs a native proof before adoption: a scheduler stop must release
the child's owner lock, failures must propagate, and spaces, Unicode, quotes,
`&` and `%` in paths must remain literal. If task termination does not contain
the child, use a launcher with a Windows Job Object and kill-on-close custody.
Do not fall back to PID discovery or broad process termination. The exact
launcher choice is a blocking prototype decision, not an approved shortcut.

Port Agent inventory observation and Runtime offline copy together against a
shared documented filesystem contract: pinned directory/file handles, no
reparse traversal, volume/file identity revalidation, existing read-only owner
lock access, and deterministic matching inventory/SQLite digests. Preserve the
independent observer; do not replace it with trust in Runtime's receipt.
Destination publication must atomically fail if `<old>-r1` exists, including a
race creating an empty directory. Copy DB/WAL/SHM and mutable support files;
clone or hard-link only the write-once CAS under its existing rules. No media
copy fallback that silently duplicates the whole store. Refuse unsupported
filesystems/cross-volume layouts before stopping R0 where detectable.

## State transitions and crash recovery

1. Under the installer lock, preflight both manager identities, filesystem
   capability and launcher support. Refuse existing/unknown R1 state. Save R0
   intent, disable verified R0, stop it and prove its owner lock free. On a
   failure, revalidate R0 before re-enabling; preserve all intent on uncertainty.
2. Stage R1; observe source; perform copy-only adoption; compare successful
   command result, durable receipt and independent source observations. An
   interrupted copy is not success. Preserve any published destination.
3. Save registration intent before publishing launcher/XML or creating R1.
   Create R1 **disabled in the initial definition**, with no opportunity to run
   at logon during the create/disable gap. Verify disabled and not running.
4. While holding the existing R0 owner lock, recheck source, publish the R1
   marker/selection/current records, then enable/start only the verified R1 task.
   Verify health against `<old>-r1` and retain all rollback evidence.
5. Recovery validates journals/files and the live R1 task before any mutation;
   disables/stops only that task; proves and holds the R1 owner lock; restores
   selection; unregisters only the verified disabled R1 registration; verifies
   its absence; revalidates/re-enables original R0. Never remove either data
   root. A changed/unknown R1 task or busy R1 lock prevents R0 restart.

Every mutating step needs an idempotent replay cut, including creation before
its success response, activation before journal update, and R1 removal before
R0 re-enable. Retain evidence when a task or file changes. Do not infer task
absence from an exception, or adoption completion from a directory alone.

## Work boundaries and precise next actions

1. **M-QUAL:** implement the Windows manager adapter and native launcher
   prototype; add host-independent parser/state-machine tests plus isolated
   native task tests. Report changes to `r0_stop`, `boundary_registration`,
   `boundary_handoff` and `adoption_receipt` to M-R1FIX before integration.
2. **M-R1FIX / M-RT coordination:** port and qualify Runtime `workspace_copy`
   exclusive publication/inventory/clone behavior and the compatible Agent
   observer. Agree the exact candidate Runtime SHA/wheel; no pin changes in
   this design patch. Existing protocol helpers are reusable only after their
   Windows semantics meet this contract.
3. **M-QUAL:** integrate all pieces, then test every transition and refusal:
   foreign R0/R1; same name in another folder; changed SID/action/arguments/
   working directory/triggers; query failure; replacement before mutation;
   disabled-but-running R1; busy/missing/replaced locks; crash after each write
   and manager call; activation failure; repeated rollback; unhealthy R0;
   destination collision; reparse/hard-link attacks; path quoting. Assert R0
   DB/WAL/SHM/support bytes unchanged and R1 accepted work retained. The new
   refusal suite records today's safety boundary, not these future passes.
4. **M-R1FIX:** obtain the required independent Astra data check on exact
   Agent/Runtime SHAs, including the task replacement and launcher custody
   decisions. No hosted workflow or LAND before that check.
5. Only after the check: isolated native Windows unit job, then
   `row=direct-windows`, inspect registration/recovery and source-preservation
   receipts, and request LAND through the existing manager process. Keep
   F-767ae0 OPEN until the actual implementation and evidence justify closure.
