# R1 registration at the frozen R0 boundary

Piece B provides **disabled registration and recovery**, including the real
staged executable and recovery through `astrid install`. It does not complete
an R0-to-R1 upgrade. The normal entry point continues to stop, restore R0 and
refuse until Piece C supplies copy-only adoption and the activation commit.
Windows registration refuses: neither its R0 task definition nor the literal
extensionless launcher/adoption path has been qualified.

## Interface for Piece C

Under the existing per-user installer lock, use:

1. `Backend.stage(...)` to materialize the candidate. Direct POSIX R1 candidates
   now include `versions/<id>/bin/runtime` and its self-contained Python runner.
   The runner uses the candidate interpreter, version environment, source
   manifest, service settings, door port and adopted data root. Staging does not
   change `current`, the service selection or R0's service entry.
2. Piece A's `stop_r0_and_wait(service, realm, data_root=source)` to retain
   durable stop intent outside both program/data roots, stop the validated R0
   OS entry, and wait for the source owner lock. Never call legacy stop,
   discovery matching, backup, migration or restore on the R0 root.
3. **C must perform actual adoption and validate its successful command result,
   durable receipt and independent before/after source observations.** A
   directory, a published receipt after interruption, or B's journal is not
   adoption/launch authority. C must refuse uncertain adoption and retain both
   roots. B does not implement or guess receipt semantics.
4. `Backend.prepare_r1_registration(candidate, source, realm_id, source_realm)`
   returns the durable journal path. It requires matching A stop intent,
   disabled/stopped R0, a matching selected source realm, and free existing
   source and adopted realm locks. The destination is exactly `<source>-r1`.
   Existing R1 definitions/launchers/intents are refused. This call publishes
   `<root>/bin/runtime` and the distinct `astrid-r1.service` or
   `haus.astrid.runtime.r1` plist. Linux reloads its manager and disables/stops
   the owned entry; macOS disables the label **before** publishing its plist
   and does not bootstrap it. No branch enables R1.
5. **C must add the durable selection/pointer/activation commit before it can
   enable R1.** B deliberately has no success/commit API to bypass that work.
   Calling the legacy `Backend.register` while A/B intent exists is refused.

The fixed POSIX launcher resolves `current` every time. Its staged runner
requires matching install and selection `r1_data_root` values, retained
`data_root` provenance with the exact `-r1` sibling relationship, owner and
program-root binding, `service_label`, and a selected R1 `realm_id`/`realm_root`.
It refuses an uncommitted selection, a different data argument, or a version
that is no longer current. These are launch guards, not adoption validation.
C must bind these records durably to the validated adoption transaction before
activation. Changing them by hand is not a supported upgrade path.

## Journal and replay

`astrid-r1-registration.json` lives beside A's
`astrid-r0-upgrade-in-progress.json` and the installer lock, outside both data
roots. It records schema version 1, platform, program/source/destination roots,
realm, candidate, log path, original pointer bytes, a copy of A's exact stop
intent, and phase. Expected service and fixed-launcher bytes are derived from
that bound record. File publication uses complete fsynced temporary files,
exclusive linking and directory fsync; phase replacement is atomic and fsynced.
An unrelated or changed file is never overwritten or removed.

New journals also retain a random `publication_id` before publishing the fixed
launcher or definition. Each exclusive publication, including the initial
journal, reserves an exact sibling alias named from the final filename and
that identity. If interruption leaves two links, recovery removes only that
alias after checking the same device/inode, exactly two links, the current
owner, publication permissions and exact expected bytes. This completes the
publisher's cleanup before the usual private-file check and manager recovery.
Recovery never searches a temporary-name prefix or removes an unknown link.
An interruption after alias removal is replayable as a single-link file.
Older journals with single-link files still recover; an older two-link state
without retained publication identity remains uncertain and is refused.

| Phase | Durable before | Allowed replay |
| --- | --- | --- |
| `registering` | Any R1 service/launcher write or manager mutation | Roll back a missing, partial or complete publication |
| `disabled` | Return from registration | Same rollback; never activate from this record |
| `restoring_r0` | A's restore/removal of its intent | Finish A recovery, or validate its already completed restore |

`Backend.recover_r1_registration()` is called by the public direct-install
boundary **before** A recovery and before legacy preflight/recovery. The CLI
protects journal-bound source/destination roots before opening its lock or log,
including when the current selection changed. Other CLI operations and the
programmatic Installer refuse pending registration rather than using legacy
stop/uninstall paths.

Replay validates exact owned files and the OS entry. Linux reloads the manager,
requires the expected FragmentPath and no drop-ins, then disables/stops R1 and
checks inactive/failed plus disabled state. macOS compares the loaded plist path
and ProgramArguments before disabling/booting out, and verifies absence. Replay
then waits for the retained R1 realm's existing owner.lock without reading its
contents. Only after the lock is free does it remove its own service/launcher
files, confirm the R1 manager entry is absent, and invoke A recovery. A
revalidates R0's unchanged definition and OS binding before enabling it. Both
data roots and all staged versions are retained; there is no data restore,
merge, migration, or deletion.

Manager errors, changed files/actions, unknown manager state, a busy/missing
lock, or changed R0 evidence retain intent and refuse R0 restart. A changed R0
entry does not prevent an owned R1 from being stopped first. A changed pointer
or committed R1 selection also stops R1, then refuses automatic R0 restart:
C's future commit/recovery protocol must handle these states. It must extend
this journal *before* changing those records or enabling R1; it cannot infer
commit from B's `disabled` phase. The ordinary install line never reaches that
unfinished handoff.

Power loss qualification and native service-manager proof remain release
validation work. Unit tests simulate managers, use real POSIX locks and the
captured R0 Linux/macOS service files, and cover CLI replay and interrupted
publication/cleanup. They do not prove adoption or data acceptance.
