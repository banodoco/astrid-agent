# UPD-R0-STOPPED-JOB-R1 — explicit stopped-job order proof

## Disposition

`AWAITING_EVIDENCE` — the scoped replacement candidate is pushed for the
required protected re-review. This is not a HANDOFF, protected approval, or
native/hosted proof.

`banodoco/astrid-agent`:

`lane/M-R1FIX/UPD-R0-STOPPED-JOB-R1 @ bf87469258c19fbc25167b6011f1fa53defbad90`

The branch is based exactly on the rejected review candidate
`efcb3297ab16a3ec47b30b02154089b372293544`, whose provisional parent is
`f4f4fc99b42bcf4421f2aa27ad07ddad85fc18f9`. Both Runtime lock copies still
point to `255ecda21ba904520eb867936ef173cc5f7d3ae4`.

## Review-finding resolution

Diagnosis: product ordering was already correct. In `Backend.stop()`, the
matching stopped R0 path makes the repeated admitted `launchctl bootout`
before Runtime `down()` is invoked; the selected Darwin `service.stop()` is
inside the manager callback supplied to that later C1 handoff. No product code
changed.

The existing full fresh-recovery stopped/no-owner test now records the real
Runtime `down()` entry and return plus the selected manager callback. It
strictly requires this chain:

```
launchctl-bootout
  < fresh-c1-down-enter
  < fresh-c1-manager-stop
  < fresh-c1-down-return
```

It also requires exactly one manager stop. This proves bootout before both C1
entry and the no-owner manager-stop callback, while retaining the existing
restoration assertions. The R1 diff is test-only:

- `tests/test_installer_launchd_stop_handoff.py`

## Focused evidence

- Explicit stopped-order regression: **1 passed** (inner exit 0).
- Stopped recovery admission plus changed-definition, live-non-R0,
  retained-live-identity replacement, and post-bootout-reappearance controls:
  **6 passed** (inner exit 0).
- `py_compile`, diff check, ancestry, Runtime-lock preservation, scope,
  clean-tree, and remote exact-head checks: all inner exit 0.

The first run in a newly created task-local venv failed before the assertion
because its fixture child interpreter had not yet been given the local source
and retained Runtime test paths; `Backend.run` deliberately strips
`PYTHONPATH`. That failed setup run is not proof. After registering those paths
through the task-local venv, the named test and six-test focused selection
passed.

The required read-only sub-worker finished with exit 0 at `sub/1/REPORT.md`.
It independently confirmed both the source ordering and the strict event
assertion. It made no product or test-source edits.

## Limits

No full suite, protected exact-head re-review, proof-only App manifest,
hosted journey, or native macOS launchctl proof was run or claimed. The
literal M-AG review-report path was unavailable locally; the M-AG status file
provided the stated missing-ordering finding. Full commands, exits, hashes,
tuple, scope, and non-proof setup attempts are in `canonical-receipt.json`.
