# UPD-R0-STOPPED-JOB — stopped R0 launchd recovery

## Disposition

`AWAITING_EVIDENCE` — a scoped candidate is pushed, but this is not a
HANDOFF, protected approval, or native/hosted proof.

`banodoco/astrid-agent`:

`lane/M-R1FIX/UPD-R0-STOPPED-JOB @ efcb3297ab16a3ec47b30b02154089b372293544`

The candidate is based exactly on provisional input
`f4f4fc99b42bcf4421f2aa27ad07ddad85fc18f9` and preserves both Runtime lock
copies at `255ecda21ba904520eb867936ef173cc5f7d3ae4`.

## Result

For interrupted recovery of a loaded, not-running R0 LaunchAgent, the
installer now permits a repeat bootout only when the retained R0 plist/action
definition still matches and the immediately preceding Runtime census exposes
no owner publication. It then follows the existing rollback restoration path,
which bootstraps the retained plist and leaves the job loaded/not-running.

Any live owner still needs the retained exact PID/birth/executable binding. A
changed definition or a live non-R0 owner refuses before bootout/C1. Existing
replacement and post-bootout reappearance refusals remain covered.

Changed paths are limited to:

- `astrid/installer/backend.py`
- `tests/test_installer_launchd_stop_handoff.py`
- `tests/test_installer_launchd_stop_refusals.py`

## Focused evidence

- Named ruling cases plus retained-original and replacement/reappearance
  controls: **8 passed** (inner exit 0).
- Affected launchd handoff/refusal fixture files: **62 passed** (inner exit 0).
- `py_compile`, diff, scope, ancestry, Runtime-pin preservation, clean-tree,
  and remote-exact-head checks: all inner exit 0.

The first focused development run found only a test-harness `FileNotFoundError`
while removing a publication that was intentionally absent; the fixture now
uses `missing_ok=True`. That attempt is recorded but not counted as proof.

## Carry-forward and limits

The predecessor retained-identity work and review rejection are carried from
`queue/pending/UPD-R0-RECOVERY-RETAINED/{REPORT.md,canonical-receipt.json}`.
The required read-only sub-worker completed with exit 0 at
`sub/1/REPORT.md`; it confirmed that the 20:37:10Z ruling supersedes only the
stopped/no-PID ambiguity, while live-owner and reappearance fences remain.

No full suite, protected exact-head review, proof-only App manifest,
hosted journey, or native macOS execution was run or claimed. The requested
`DECISIONS.md` was not present at the literal manager-workspace path; the
matching 19:22 and 20:37 rulings were read from `ORCHESTRATOR.md`.

The complete commands, inner exits, hashes, tuple, parent graph, scope/head
checks, sub-worker result, and limitations are in
`canonical-receipt.json`.
