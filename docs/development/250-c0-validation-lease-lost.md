# Task 250: C0 validation lease loss — evidence boundary

## Disposition

Report-only at Agent `39541d934c989c23f20fa7e05bd3b7cf5e648382`.
The sanitized evidence does not identify the initiating lease-loss mechanism or
prove a child leak. No production code, tests, assertions, lease timing, deadline,
progress-renewal rule, terminal publication, ownership or fencing was changed.
The requested failing-before/passing-after causal regression is not established.

## Sanitized evidence

`W/ORCHESTRATOR.md`, 2026-10-30 18:43:10 UTC: operation
`4948859a0147d6c3b42fd81a8a58a7ad` passed warm storage and Runtime readiness,
then failed during `vibecomfy.validate` with `task_failed / lease_lost`.
Clean stop warned that the generic host had active child work. The operation is
terminal, the pod is retained, charged jobs are 21, and no GPU generation/pass
is claimed.

The 18:44:22 UTC note says the surviving top-level result has no diagnostic
fields. It explicitly does not establish
whether the child diagnostics were generated or deleted. No typed lease reason
has been supplied. The subsequent 18:48:55 UTC design note invites evaluation
of conditional owner-liveness renewal independent of stall detection, deadlines
and revocation. It adds no observed lease category or child-cleanup disposition,
so it does not establish a causal reason to change renewal for C0. Only sanitized
ORCHESTRATOR text and local source/tests were read; no pod, provider, credentials, private inputs/media or output artifacts
were accessed.

## Local causal map (line numbers at the base)

1. H3 already invokes canonical `vibecomfy.validate` via `_invoke_stage`
   (`astrid/packs/h3_av/actions/transform/run.py:1478`). `_invoke` waits through
   `client.invoke_result` (`:616`) and formats a failed result into a new error
   (`:629`). There is no justification for adding a second action lifecycle.
2. The generic host launches the command with `ASTRID_PROGRESS_PATH`
   (`astrid/core/execution/generic_host.py:8483`). Validation redirects stdout
   and stderr while validating, verifying model-root files and compiling
   (`astrid/packs/vibecomfy/actions/validate/run.py:232`). It writes no semantic
   progress checkpoint in this body. A long silent interval is possible; its
   occurrence, duration and relationship to this lease loss are unproven.
3. `renew_progress` credits only changed host stage or changed semantic progress
   (`generic_host.py:9569`); allowed keys are phase/percent/current/total
   (`:9556`). It refuses renewal after the immutable deadline or a terminal
   decision. `pump_lease` separately detects task-read exceptions (`:9634`),
   expired/invalid lease metadata (`:9642`), state/attempt/fence changes
   (`:9658`) and heartbeat exceptions (`:9675`). `cancelled()` can also map a
   task-read exception to an unclassified lease loss (`:10075`). The two
   production symptoms do not select one of these branches.
4. `force_abort` sets the abort event and publishes fenced failure/terminal
   evidence (`generic_host.py:9595`). It intentionally does so before cleanup
   finishes; terminal task status is not a child-cleanup acknowledgement.
   The typed record is passed to Runtime failure (`:9546`) and attempt
   `result.json` (`:9391`). SDK failed-task responses carry the settled result
   under `error.details.result` (`astrid/sdk/invocation.py:3331`). H3's error
   formatting does not preserve that as a separate typed diagnostic field.
   Ephemeral attempts may then be deleted (`generic_host.py:11060`).
5. Cancellation reaches the command cleanup finalizer (`generic_host.py:8706`).
   `cleanup_once` delegates to `_terminate_process_group` (`:8585`), whose
   contract requires direct-child reaping and complete owned-scope settlement
   (`:1598`). A task-scope child delegates to `task_scope.stop` (`:1609`),
   while other owned trees use `terminate_tree` plus `verify_tree_absent`
   (`:1614`). The command untracks after reported teardown (`:8712`).
   Host shutdown fences and waits for active attempts/processes (`:6090`),
   retaining uncertainty when that cannot be proved (`:6107`).
6. The exact active-child warning is emitted by
   `astrid/sdk/upgrade.py:155`. `_host_idle` normally checks the in-flight task
   count in the busy record (`astrid/sdk/host_bootstrap.py:1032`); only its
   fallback inspects descendants. The count is decremented after the attempt
   finalizer (`generic_host.py:9743`). An early stop can therefore observe a
   legitimately busy cleanup. The warning alone proves neither an orphan nor
   successful cleanup nor an unreaped direct child.

## Independent cleanup counterexample, not a C0 diagnosis

A deterministic, process-free local reproducer demonstrates a separate direct
leader accounting hole in `astrid/core/execution/task_scope.py`:

- `_scope_census` excludes the direct leader (`:218`).
- `_stop` observes the leader in its bounded loop (`:303`) and attempts a final
  direct-child wait (`:317`). A timeout followed by a denied signal is retained
  only as a note (`:319`).
- Its final settlement decision checks descendants/census error (`:328`),
  without requiring the original leader wait to succeed. When descendant
  censuses are empty, the original child can remain active while the returned
  status is `settled`; the notes are discarded by that return (`:331`).

The synthetic child always returns `None` from poll and raises `TimeoutExpired`
from wait. Empty descendant censuses, denied signaling and zero budgets produce
`scope_status=settled`, `child_poll=null`, `wait_calls=1`, `detail=""`.
No real child or OS signal is used. This is a source-supported cleanup defect,
but there is no sanitized evidence of this condition in C0, and it cannot by
itself explain the initiating lease loss. The brief explicitly requires a
report-only patch when that causal evidence is absent, so no speculative source
fix or unrelated regression was added.

## Evidence needed to resume a causal fix

The orchestrator should append sanitized text identifying:

- The actual validation host/child imported module path and SHA-256, rather
  than only its parent's staged source identity.
- `lease_diagnostic.category`, UTC observation time, observed state,
  attempt/fence/expiry, last successful heartbeat time/expiry, and bounded
  exception class/code/request ID where present. Exclude secrets, payloads,
  media paths and workflow values.
- The warning's time relative to terminal publication and finalizer completion;
  busy count, owned scope disposition, and positive direct-child wait/reap
  result (or the exact timeout/denied-signal/survivor diagnostic).

For `lease_expired`, also supply the lease duration and actual semantic-progress
checkpoint times during validation. For a Runtime/transport error, supply its
bounded typed error metadata instead. Do not infer silence, expiry or fence
loss from `lease_lost` alone. An instrumented orchestrator run should preserve
only the bounded diagnostic before ephemeral cleanup or use an already
supported retained-attempt option; it must retain the existing canonical route.

## Local verification

The existing `W/work/33-agent-venv` (Python 3.13.2, pytest 9.0.2,
xdist 3.8.0) and Runtime fixture source `W/work/238/astrid-runtime` at
`df3d4ddac7bf73cd858106cc51a802521a4d3552` were reused, with both Runtime Python
roots explicitly on PYTHONPATH. No dependencies were installed/downloaded.

Both named files were run once with unchanged pytest marker defaults:
`tests/core/execution/test_fail_closed_g28.py` and
`tests/core/execution/test_process_group.py`, `pytest -q -n auto` with four
workers. Result: **113 passed, 1 failed, 4 skipped**, 34.30 s pytest time,
35.183 s measured wall time. The four existing Linux-only platform cases were
skipped by the unmodified lane policy on macOS; five native Darwin cases are
outside the repository's default selection. No test was edited, weakened,
xfail-marked or newly skipped.

The failure is
`tests/core/execution/test_process_group.py::test_instantly_exiting_launches_are_a_known_outcome_not_a_custody_error`.
It is present in both baseline failure sets in
`W/manager/a0/astrid-agent/paired-failure-ids.json` linked by `W/BASELINE.md`.
This run failed at native custody registration with `task_name_for_pid`,
`kern_return=5`; the session process guard also could not execute `ps` due to
sandbox denial. Native cleanup completeness is not claimed. There were no
source changes during the run; it was already a base execution, so no duplicate
base replay or outside-sandbox run was attempted.

The synthetic cleanup counterexample ran once and reproduced the above result.
It is not the required before/after regression and does not establish C0's
cause. The named lease-diagnostic/progress/deadline assertions were left intact.
The documentation-only patch is whitespace-checked and checked/applied against
an isolated index populated from the exact base, without creating another full
checkout. Exact commands, durable logs, allocation and patch digest are in the
external task `REPORT.md`.
