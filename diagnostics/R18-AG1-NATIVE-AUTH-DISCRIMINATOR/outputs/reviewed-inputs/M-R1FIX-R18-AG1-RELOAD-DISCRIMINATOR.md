# R18 AG1 reload discriminator — changed native input ready

The cause of Actions 41596849813 is **not established**. Its idle fixture returned the new Description and Environment before the second explicit `systemctl --user daemon-reload`. An unload and on-demand reload of the idle unit is supported by systemd source and fits the observation; an unrecorded manager-wide reload or other behavior remains possible. The original record has neither unit-lifetime nor reload-event timing to choose among them. The changed diagnostic is pushed at `lane/M-R1FIX/R18-AG1-RELOAD-DISCRIMINATOR@487477b4b4e137d421f6479a9f9692594a653ed3`, based on `149b1f751a430811c0cc8a44c311c5f5718bbddb`. It changes installer diagnostic tests only.

## Original observation and exact sequence

Run 41596849813 attempt 1 was bound by `requested-inputs.json` and `tuple.txt` to Agent `149b1f751a430811c0cc8a44c311c5f5718bbddb`, Runtime `2776ba973b6c8f3555b5629b0840d9691a98d3c2`, App `79f7e5397c044c4c97dc7e97b20ad96f787e5247`, and Pinokio `3a6a98ae251ca126505245286e2d3e760f4348e9`; workflow carrier `venue/M-R1FIX-R18-SYSTEMD-NATIVE-PROOF-5@1552ceceb31762e7d1b7bb7bc2e1cccf151bd5e5`. Driver and suite inner exits were 1; UID/eUID 1002; systemd package `255.4-1ubuntu8.18`. `authority=false`.

| Recorded selected property | Before disk change | Disk changed, before recorded reload | After recorded reload |
|---|---|---|---|
| Description | `Identity discriminator before` | `Identity discriminator after` | `Identity discriminator after` |
| Environment | `ASTRID_IDENTITY_PROBE=before` | `ASTRID_IDENTITY_PROBE=after` | `ASTRID_IDENTITY_PROBE=after` |
| User | empty | empty | empty |
| Group | empty | empty | empty |
| WorkingDirectory | `!/var/tmp/systemd-identity-41596849813-1-sdJi3u/home` | same | same |

The source writes the base unit, explicitly executes `systemctl --user daemon-reload`, opens one direct private socket with native peer sd-bus, takes peer/proc samples, then calls `LoadUnit` and reads its returned object path. Each of three snapshots performs `GetAll` for Unit and Service and typed `Get` for Description, Environment, User, Group and WorkingDirectory. Between the first and second snapshot it creates the drop-in directory and writes `override.conf`; there is no explicit `systemctl` or mutating sd-bus method in that source interval. It then runs the second explicit `daemon-reload`, takes the third snapshot, samples peer/proc again, deletes the fixture, and runs a third cleanup `daemon-reload`. All recorded native calls and three systemctl calls returned nonnegative/exit 0. This is source order and a call list, not a timed event log; it cannot exclude another process or implicit manager activity. The exact call records and artifact names/hashes are retained in the original `result.json`.

The published `result.json` SHA-256 is `db4257ee9fbc5efa34a5ccc3f95950fb7bc129b030e2af35ac5b846e7762081a`; `requested-inputs.json` is `6bc28af0c83c5b3a4a87d34d381a90307a4e7429de58082fa04fd6844e7732ea` and tuple is `a7ef0503d16c01699a398e1a187244b07e9c4ba31b3a19cbc06744d1432f80d2`. The full locally computed manifest of the published files is `original-published-sha256sums.txt`; it is not the run's original signed manifest. The six decoded GetAll files and original `SHA256SUMS` are **not present in the published copy**. Their lengths and hashes are in `result.json`, but their raw contents cannot be independently inspected or checked against those hashes here. The original shell expands `"$run"/export-*.txt` as the runner after making the `mktemp -d` directory owned by the disposable user; the inaccessible mode-700 directory plausibly prevents that expansion. This is a source-based explanation for missing exports, not proof of the collection failure. The new wrapper enumerates with `sudo find` and retains the copied exports and checksum manifest.

In upstream systemd v255, [`unit_may_gc`](https://github.com/systemd/systemd/blob/v255/src/core/unit.c#L433) permits collection of an eligible inactive unit, whereas a non-inactive unit is retained; [`find_unit`](https://github.com/systemd/systemd/blob/v255/src/core/dbus.c#L275) resolves a unit object path through `manager_load_unit_from_dbus_path`. The original probe never starts or explicitly references the unit, so a path string and connection alone do not prove the same loaded unit persisted between snapshots. The upstream tag is not a byte-for-byte verification of Ubuntu's patched package. Neither this source mechanism nor the values prove that GC occurred in 41596849813.

## New test and how to interpret it

The diagnostic starts a separate disposable `RemainAfterExit=yes` oneshot control and retains the original-style idle fixture. On the same direct private peer it installs a manager-signal match and subscribes before setup, records timestamped actions and native calls, and captures `Reloading`, `UnitNew`, and `UnitRemoved` with receive times and decoded values. It samples `NReloads` where exposed and manager monotonic generator/unit-load timestamps before and after an explicit no-reload interval, then applies drop-ins and reads both units, explicitly reloads, and samples again. The controlled reload must deliver `Reloading(true)` then `Reloading(false)`; any signal inside the interval, missing/undecodable positive control, or inactive control makes the capture inconclusive and returns inner exit 1. A property absent from the manager is stored as an error/unknown, not a zero counter. Signal timestamps are receipt times, not emission times; compare sequence and manager samples accordingly. Venue setup and cleanup are separately traced and the wrapper saves exports, result, inner exits, cleanup and SHA256SUMS.

A no-reload interval with idle `UnitRemoved`/`UnitNew`, a changed idle value, stable active values until explicit reload and the positive control would support the GC/on-demand mechanism for **the new run**, not prove the historical run. A reload signal in the interval would identify contamination of the new experiment but not its requester. Stable timestamps alone cannot exclude an unobserved reload, and absent unit signals are not proof of no collection. Other combinations or failed prerequisites remain unresolved and must be reported as such. This is not a product/contract change or an authentic lifecycle test.

## Focused local checks and limits

- Native monitor plumbing against a real local session `dbus-daemon` passed: match, ordered Reloading true/false and typed UnitRemoved callback, inner exit 0. This was bus-client mode against a test daemon, not the systemd private manager connection.
- Monitor guards passed four negative cases: interval reload, missing positive-control signal, decode error and inactive control; inner exit 0. Root preflight deliberately refused before native effects, inner exit 1. Local PID 1 is `codex`; `systemctl --user show` returned 1 (`No medium found`), so no local non-root manager observation is claimed.
- Existing fail-closed facade test `PYTHONPATH="$TMPDIR/runtime" /home/user/merge/ws/M-R1FIX/queue/done/INST-SCHEMA-PIN-FIX/venv-run/bin/python -m pytest -q -o addopts= tests/test_installer_lifecycle_transfer_adapter.py`: **64 passed**, inner exit 0. Final-head shell syntax, Python compilation and diff check exited 0. No full suite or blind retry was run.

The initial manager identity bracket still had inaccessible `/proc` executable and namespace fields. Selected values establish no authenticated identity, complete effective definition, issuer authority, restart/namespace stability, independent verification or continuous custody. Refusal remains; hosted execution is pending and must not be treated as passed.

## Exact single Actions request

The exact input file is `dispatch-inputs.json`, SHA-256 `400cc6757ffbbf5976b42aa3d182f6772a2ebd5bfd599e5d2ca8f8c020d7eb8e`. For the relay, its published input name is `@inputs/M-R1FIX-reload-discriminator-487477b4.json`:

```text
ACTIONS: astrid-agent merge-suite.yml venue/M-R1FIX-R18-SYSTEMD-NATIVE-PROOF-5 @inputs/M-R1FIX-reload-discriminator-487477b4.json
```

Command: `bash agent/tests/installer_manager_reload_venue.sh`; artifact glob `suite-artifacts/**`; slot `6`; exact tuple as above with Agent `487477b4b4e137d421f6479a9f9692594a653ed3`. Preserve the requested/effective tuple, workflow/carrier SHA, all inner exits, `r18-manager-reload/result.json`, `venue-actions.log`, each `export-*.txt`, `SHA256SUMS`, cleanup, and input hashes. The request is present in the M-R1FIX relay status header; relay acceptance, run ID and hosted result are not yet confirmed. See `canonical-receipt.json` for machine-readable operations, hashes, and status.
