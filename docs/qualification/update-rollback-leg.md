# R15 update/rollback journey leg: design note (draft, Qualifier lane)

Branch `release/15/q-update-rollback`; base `release/15/main` 514499de. Not a PR; no CI dispatched.

## Shape
* New `workflow_dispatch` input `update_rollback_only` (boolean) on `.github/workflows/journey.yml`, plus the
  existing `candidate` input (App/Agent/Runtime/Pinokio SHAs + manifest_sha256 of the Release 15 candidate).
  When true: `config` skips the Release-1 matrix and old-launcher checks, the `journey` job is skipped, the new
  `update_rollback` job runs on ubuntu-24.04, and `gate` accepts that shape. Every existing leg/test is unchanged.
* New driver `scripts/journey_update_rollback.py` (+ verbatim `scripts/data_fingerprint.py`, stdlib only). It reuses
  `ci_journey` helpers (run scope, bootstrap, installation, wait_ready, healthy, installed_cli) and the existing
  systemd-user isolation; it adds `scripts/ci_linux_user_manager.sh`, a compact copy of the journey job's manager setup.
* Receipts (JSON, journey-result.json style) under `<run>/evidence/update-rollback/`: `step1-r14-install.json`,
  `step2-update-r15.json`, `step3-rollback.json`, `step4-resave-reupdate.json`, `journey-result.json`.
  `journey_update_rollback.py check DIR` re-reads them and exits nonzero on anything missing (also run as a job step).

## Forcing the candidate to fail its health step (no product change)
The failed-candidate mechanism in `failure_mode_executor.py` is an in-process injection into
`Installer._run_update_transaction`; it cannot be driven through the real bootstrap. The hosted leg instead runs the
real `public/install --update` with `ASTRID_RUNTIME_DOOR_PORT=0` for that one invocation. The staged candidate then
records door port 0, starts (the real Release 15 Runtime, serving on an ephemeral door port) and
`Backend.verify` can never see `door_endpoint == http://127.0.0.1:0`, so after its 90 s deadline the installer's own
`rollback_transaction` runs and reactivates Release 14 using Release 14's recorded port. While the candidate is live
(inside that 90 s window) the driver saves the Release 15 edit through the candidate's own CLI/SDK, reads its
content digest, and observes the three history guard triggers. Order therefore is: R14 install+edit (step 1);
real update toward R15 that is forced to fail after the R15 save (steps 2+3: the same transaction); then a clean
`--update` to R15 after an R14 save (3e). Step 2 is also proven by that final clean update.

## Evidence per verifier item
(a) installed `manifest.json` agent/runtime/vibecomfy, `version.json` identity, version dir, health body: before vs after.
(b) Release 14 health + Runtime release 14 after rollback. (c) triggers `timeline_history_no_{update,delete,replace}`
on `timeline_commit_history` in `<realm>/realm.sqlite3`: present while R15 is live, absent after rollback (stopped).
(d) Runtime `inspect` (`head_content_digest`, parent clips) R15 vs R14; fields R14 cannot represent are listed.
(e) one R14 save, then the real `--update` to the candidate succeeds and health is release >= 15.

## Rollback hook status
`remove-history-guards` (astrid-agent #609) is on `release/15/plan-89` (e3da121f) but NOT on `release/15/main`
514499de. The leg detects it in the candidate Agent source (`remove_history_guards` in `astrid/installer/core.py`)
and records `hook_present`. If absent, step 3(c) is the named marker `SKIP_AS_FAIL:rollback-hook-absent`, the
verdict is fail, and the checker exits nonzero. The Qualifier must run it with a candidate whose Agent has the hook.

## Unverified locally (a hosted run is the first proof)
door-port-0 behaviour and the 90 s window; SDK/CLI/inspect shapes on R14; systemd user manager; sqlite read while live.
