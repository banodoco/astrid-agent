# Standing attached-meter references

The saved references describe the original attached stand-in workload, not the
D1 speed target or GPU inference. Their acquisition used five uninstrumented
samples and two separate counter samples per caller. Every sample follows a
successful warm-up in its own host session and verifies the delivered PNG and
two distinct succeeded task records.

## LS-2.2 gate

[Owner decision 6987905451](https://github.com/banodoco/astrid-dev/issues/796#issuecomment-6987905451)
sets the current counter policy: duration-following totals are observations,
with no metric limit. The existing ambient classification identifies them;
scoped and deterministic counters retain their caps. SDK/host per-poll rules,
per-request outer-commit caps, exact subprocess counts, dependency identities,
complete evidence and strict historical comparisons retain their checks.
No new invariant is introduced. Original failed runs retain their outcomes.

The duration-following route totals include `POST /v1/executors`: the host
renews registration every 30 seconds, including during active work. Both its
HTTP count and scope count are observations. Its per-request commit caps and
complete request-scope evidence remain gates. Run 44368327839 retained the
original four base-only registration-count breaches before this classification
correction; each recorded request still had exactly one outer commit.

`counter-calibration.json` retains the controls and observation policy;
`summary.json` and the Actions step summary record the held-out and candidate
observations, including samples, extrema and base controls, without a limit.
Where census attribution produces a remainder, both raw samples and the
individually attributed census sleeps remain visible. The unrelated residual
at `subprocess:Popen._wait` keeps its existing cap.

The standing workflow measures the exact comparison base, an independent repeat
of that base, and the exact candidate on one runner with one Python/dependency
environment. Each of those cells has five uninstrumented samples and five
separate counter samples per caller. The base
has only a byte-checked copy of the current test harness; its product, packs,
vendored client and source authority remain the base commit. Import locations,
product and harness identities, dependency freeze and raw results are retained.
The Windows comparison therefore does not use an unrelated historical runner
as its only timing control.

For the `p55/ls-2-2` packet only, the comparison base remains
`19103ea6acbc030b3379e840ccc03491e47f1a0e`, under the Lead's
[original-tuple ruling](https://github.com/banodoco/astrid-dev/issues/796#issuecomment-6967421223).
The log names both that comparison base and the actual PR target base. The
target has since re-pinned VibeComfy; this proof cannot qualify that new tuple.
LS-2.1 owns its measurement after re-pins. Other branches select their exact PR
base. The dependency identity guard remains enforced in every comparison.

The packet branch integrates accepted plan base `91cb4cb4` (including `d0217776`, `24c68a27` and `6451821e`)
before final proof. Under the original-tuple and merge rulings, the workflow
checks the exact clean merge head and ordered parents, reproduces its native
merge, preserves the accepted Runtime-pin reader, and verifies every shared
measurement-harness blob. It then selects the clean original-tuple parent, whose changes
from `b18080bc` are confined to the named observation, policy, qualification and
focused proof files. That
parent retains the original product, dependency pins, references and test
guards. `plan55-qualification.json` records both commits and trees, all harness
hashes and the two plan-added LS-5 harness files outside this measurement.
Follow-ups retain the preceding published controller as the first parent;
the current predecessor is `c6ebb697`, which retains `d0834eb6`, `22b5d21e`, `02add382`, `d7f53b0d` and `d84e7276`. Each
qualification source extends its predecessor's original-tuple source. The
checker follows that published chain back to the accepted plan merge and
verifies both identities and the ordinary two-parent merge;
the published first-parent history is preserved without rebasing.
Under [Lead 6983043853](https://github.com/banodoco/astrid-dev/issues/796#issuecomment-6983043853),
the SDK fix first merged into `02add382`, then the new accepted
plan merges into the result. The checker reproduces both ordinary merges and
requires the current plan as an ancestor; the original-tuple source still
extends the preceding original source. The newly accepted LS-8 product bytes
stay in the integrated head and are not substituted into the original proof.
The final evidence-field correction extends the same qualification source and
merges into `22b5d21e`. Source selection follows both recorded plan bases to
retain the original tuple across that already verified plan advance.
Under [Lead 6985581776](https://github.com/banodoco/astrid-dev/issues/796#issuecomment-6985581776),
the next continuation preserves published `d0834eb6`, then merges accepted
`91cb4cb4`. The recorded plan predecessors are `91cb4cb4` to `d0217776` to
`24c68a27`; source selection traverses each boundary with the same ordered
parent and source-ancestry checks. The accepted LS-6 product and test bytes
stay in the integrated head, and the original product, pins, meter policies,
limits, references and prior assertions stay unchanged in qualification.
The continuation source archive retains the integrated PR head. This source
handoff does not qualify the newer accepted product/dependency tuple; LS-2.1
owns that measurement. Any unexpected source or merge difference fails before
the handoff. The paired runner retains all its source/dependency guards; its
authorized policy-reporting change is named in the qualification path list.

### Practical threat model and policy source

[Owner decision 6979024059](https://github.com/banodoco/astrid-dev/issues/796#issuecomment-6979024059)
sets this gate's scope. It catches realistic weakening intended to make a run
green, including accidental selection loss, changed caps or references, and a
candidate grading itself with changed gate code. It fails closed on incomplete
ordinary results. Reviews have at most two rounds; round two checks only the
findings from round one.

The separate **Meter policy from accepted base** job checks out the exact PR
base and runs that base's `tests/local_speed_policy.py` directly, before any
product or test installation. The proposed commit is fetched only as Git data.
The checker refuses changes, additions or deletions in the meter harness,
oracles, integration helpers, saved references, development constraints,
workflow and pytest configuration, including nested config and conftest files.
This covers marker/skip/xfail changes, `-k`/`-m`, workflow `PYTEST_ADDOPTS`,
self-approved ceilings and replacement gate code. Ordinary product changes
continue to use the accepted gate bytes. A legitimate gate/configuration change
is a visible failure requiring the Lead's review; it cannot approve itself.

This introducing PR's policy job is expected to fail once because accepted
base `91cb4cb4` has no policy checker, under Standing ruling 5. Its measurement
jobs are independent and keep normal failing exits. After integration, the
Lead's required-check/path-protection setup must retain this job and protect
its workflow entrypoint; this packet does not change repository settings.

The existing result checks require both callers once at every sample index,
complete task/output/observer evidence and the full phase test count with no
failures, errors or skips. Missing, empty or truncated logs/XML and ordinary
skip/xfail/deselection results fail. Original byte-identity, dependency, timing,
reference and counter checks remain in force. The Windows source-graph fixture
writes explicit UTF-8/LF bytes, so platform text translation cannot change the
Git blobs that its unchanged byte guards inspect.

Deliberate standard-library shadowing, test-runner-internal patches, packaging
or build hooks, and crafted malformed result files are stated limits for path
protection and review. No defences for those attacks are added. In particular,
the ordinary JUnit count reader is not an authenticity check for forged XML;
the independent raw-observation checks still require complete real reports.

While the saved references are still the original v2 records, the runner also
retains a separate historical measurement:
the **same candidate product**, in a separate checked worktree, using the
historical harness copied from the immutable PR base. That control retains the
original accelerated poll settings and observer, so it can be compared with
the historical numbers. It still uses the current 15% timing policy and exact
counter caps. Its separate raw measurements and harness hashes are retained.
The historical control retains five timing/two counter samples per caller.
Its strict comparison and any rejection stay in the report independently of
the calibrated gate. All cells retain the original per-test and per-phase
deadlines. This packet does not approve, emit or replace a saved reference.

The current comparison is **15% relative** for both median and slowest overhead,
with **no absolute floor**. Counter caps are upper bounds. A missing previously
observed counter fails, and incomplete scoped coverage cannot become zero.
The historical JSON files below retain their original values and acquisition
policy; those policy fields do not select the current comparison tolerance.
Different polling settings and observer costs are never combined into a single
distribution. The historical control keeps all old caps. The active same-runner
comparison follows the Release 14 Lead's
[base-vs-base calibration decision](https://github.com/banodoco/astrid-dev/issues/796#issuecomment-6965824641):

- Five exact-base controls set the policy. Five fresh base samples validate it;
  five candidate samples are then compared with those same limits. Neither the
  holdout nor candidate samples can set or adjust a limit.
- `counter-calibration.json` retains every control value, minimum, maximum,
  spread, margin and group. Duration totals carry `observation_only` and no
  `limit`; scoped and deterministic counters retain their original limits.
- The earlier ambient range and host-window arithmetic remain descriptive
  calibration metadata. They do not turn a duration total into a gate.
- Ambient groups cover whole-workload/parent wait volume, observed polling
  wait producers, Runtime poll/reaper invocation volume, aggregate associated
  commits and native operations. The named lease-pump and parent pipe-readiness
  counts can also vary. A new parent sleep producer has no automatic allowance.
- Separate process, request and polling-cycle guards protect per-job work:
  compiles, emissions, hashes, starts and writes remain independently checked;
  each Runtime request has its own commit/write maxima; each observed SDK
  status or host process polling cycle has its own wait maximum.
- Wait-producer totals must reconcile with process waits. Request scope totals
  must reconcile with observed routes. Scoped commits must reconcile with the
  SQL-trace and process totals. Carry-in requests remain explicit and are not
  counted as new requests. Lost partial observations fail those checks.

The **base-control host polling window cadence model** follows the Lead's
[cadence ruling](https://github.com/banodoco/astrid-dev/issues/796#issuecomment-6968415737).
It applies only to the parent producer
`astrid.core.execution.generic_host:GenericPackHost._run_command_definition`.
Actual `Popen.poll` calls retain their child PID, invocation, start and result;
actual `time.sleep` calls retain the requested period. For each of the five
base controls, complete windows reconcile every producer sleep with the raw
wait totals. The controls must show one sleep per nonterminal poll and one
common positive requested period. The recorded historical count estimate is
the floor of the largest control's total polling-window duration divided by
that period. The old range, window-budget and propagated-increment metadata
remain diagnostic observations. Under decision 6987905451 the host and
containing duration-total metric entries have no limit; the independent
per-poll, worker and per-request checks remain enforced. The policy records
all five window durations, poll and sleep counts and the common period.

Every holdout and candidate still requires exactly one sleep per nonterminal
poll, a per-poll maximum of one, and a requested period at least as long as the
base period. A longer candidate window cannot change the base period or the
per-poll cap. Missing poll results, unknown sleep periods,
ambiguous child identity, clipped windows and unaccounted producer sleeps are
incomplete evidence. An observed elimination of the loop remains an improvement.
Window duration includes guard work and scheduling, so it is not a pure sleep
duration or an uninstrumented latency measurement. Original failed runs remain
failed under their recorded policy; the new model requires a new complete pair.

The **process census invocation rule** follows
[Lead decision 6970432614](https://github.com/banodoco/astrid-dev/issues/796#issuecomment-6970432614).
It applies only to `astrid.core.execution.process_group:_process_snapshot`.
The observer follows the actual `subprocess.run` and `Popen._try_wait` code,
including aliases captured before observation, and joins each call to its
spawned child, actual wait results and requested `Popen._wait` sleeps. Arguments
and environment values are not serialized; recognition of the exact census
command is a boolean.

Five base controls must establish four subprocess invocations per sample,
at most one nonterminal wait per invocation, one sleep for each such wait and
one observed positive period. Each held-out base and candidate retains exactly
four invocations, at most one nonterminal wait per invocation and a period that
is never shortened. A child already exited may need no wait. Missing results,
unknown periods, clipped intervals, unrecognized commands and missing or
ambiguous child identities are rejected. The census sleep total follows from
those individually checked invocations and has no separate allowance.

When every control child has already exited, the observed sleep total is zero
and the requested period remains unknown (`null`). Another fully observed
zero-wait sample passes the same invariants. If a later sample requests a wait,
comparison fails as incomplete period evidence; an unknown base period never
becomes zero or authorizes a shorter wait.

Raw totals remain in the observations and calibration `raw_samples`. Only the
individually guarded census sleeps are removed from the global sleep total,
the parent sleep total and that parent's `subprocess:Popen._wait` site before
their remainders are compared with their policies. Each subtraction is recorded
as `census_sleep_samples`; non-census waits cannot use it. The unrelated
subprocess-site remainder keeps its existing cap. The containing duration
totals are observations under 6987905451. The host per-poll, timing and reference
guards remain unchanged. Earlier failed runs keep their original outcome.

The **resident SDK polling rule** follows
[Lead decision 6981129059](https://github.com/banodoco/astrid-dev/issues/796#issuecomment-6981129059).
It applies only to `resident_sdk` and
`waitsite.parent_runtime_host_sdk.sleep_attempt|astrid.sdk.invocation:_wait_for_kernel_task_loop`.
That total follows task duration: an identical-product held-out base recorded
12 sleeps against the old cap of 11. The total, base samples and range remain
observations; this named metric has no `limit`. Its former range/margin metadata
remains descriptive and does not authorize work.

The observer records each SDK invocation's task identity, boundaries, actual
task-status calls and returns, poll state, and sleep requests and completions.
Each nonterminal poll must contain exactly one sleep; the terminal task read
exits without a sleep. Every requested period must be at least the positive
period established by the base controls. Unknown periods, missing results,
unaccounted sleeps, ambiguous invocation or poll identities, and clipped
invocations fail closed. Aggregate maxima alone cannot prove these invariants.
Older artifacts lacking these records keep their original outcomes and cannot
be reclassified as complete under this rule. If the controls observe no sleep,
the period remains unknown and cannot authorize a later wait.

The original 6981129059 change was confined to this SDK total. The owner then
approved 6987905451 for the other duration totals, including the corresponding
CLI and containing totals. The SDK's independent per-poll maximum, host and
census invariants, timing policy, immutable references and approved dependency
identities remain. SDK sleeps are never subtracted from containing totals.
The six named assertion migrations are kept together in one commit under
6987905451 and the delegated narrow clarification
[6990340205](https://github.com/banodoco/astrid-dev/issues/796#issuecomment-6990340205):
`test_containing_counters_keep_their_existing_caps`,
`test_larger_sdk_total_is_an_observation_and_does_not_set_a_limit` (CLI part only),
`test_five_base_controls_name_each_spread_and_accept_independent_base_variation`,
`test_control_sample_shortfall_and_candidate_derived_limits_are_refused`,
`test_base_window_budget_accepts_independent_base_repeat_without_changing_other_limits`,
and `test_larger_range_bound_is_retained_and_candidate_windows_never_set_it`.
The two forged-policy probes add a forbidden limit field to an observation and
retain their original `ValueError` rejection assertions. All per-poll,
requested-period, completeness and base-only-policy guards remain. No additional
defense for a documented crafted bypass or extra review round is introduced.

The held-out base comparison must pass as well as the candidate comparison.
A failed comparison is retained; it cannot expand its own margin or become a
replacement reference. The historical comparison remains a separate reported
result even when the current calibrated comparison passes.

The attached cell leaves host, SDK and machine polling at product defaults.
It counts `Event.wait`, direct `Condition.wait`, `asyncio.sleep` and selector
waits as well as `time.sleep`; an Event's internal Condition is not counted a
second time. Windows' default asyncio IOCP waits are observed through the exact
`GetQueuedCompletionStatus` C-call identity, including direct drain/close calls;
the event-loop policy is unchanged. Waits already pending when the interval starts are disclosed
separately. Runtime claims, task reads and other requests are counted by method
and normalized path, without query values or per-job identifiers.

Caller, fake-server, Runtime and child-observer intervals use the same
high-resolution `perf_counter` clock. On Windows Python 3.11 this avoids coarse
`monotonic_ns` ticks collapsing a warmup or in-flight-wait boundary. Epoch
timestamps for native trace matching remain separate. The historical control
retains its original clock along with the rest of its immutable harness.
Windows epoch boundaries and write spans use `GetSystemTimePreciseAsFileTime`,
avoiding Python 3.11's coarse UTC clock; the clock identity is retained in raw
reports. Exact native interval containment has no added post-reply tolerance.
The original DELETE/FULL calibration ends before the separate WAL calibration
begins, so the new WAL work cannot satisfy the original native-count check.

Zero worker starts is valid when a worker admitted before the interval supplies
a complete snapshot for that interval. A whole-lifetime/exit-only report is
insufficient. `test_local_speed_resident_observer.py` exercises this protocol
with a real process, two jobs and verified output bytes; it is meter calibration,
not a claim that LS-8's product implementation has landed. The attached product
proof continues to require two succeeded Runtime records and verified delivery.

Forced-write evidence reports effective SQLite settings before and after the
call, successful outer commits separately from attempted COMMIT statements,
all connection commit calls and no-op calls outside a transaction,
application sync origins and Runtime routes, and platform write-through calls.
The autonomous lifetime reaper is identified by its exact server/code owner;
HTTP-triggered reaping and heartbeat/progress commits remain separate contexts.
Native writes carry both an operation category and a request/background
context, including WAL or checkpoint work outside an observed commit call.
An absent request route is not evidence of background work. A Runtime commit
or native write without a recognized context cannot qualify the gate.
Agent host/SDK evidence retains its producer identity when it uses Runtime's
shared filesystem helper; the helper alone does not make it a server write.
Linux native tracing retains descriptor paths and matches actual syncs to
observed application/commit spans. WAL-file syncs during a commit, WAL syncs
outside a commit and database/checkpoint syncs remain distinct measured facts;
a WAL sync is not automatically called a commit. Unmatched native writes fail
attribution. Windows native mapping remains explicitly unqualified under
LS-13; Python calls are never represented as a native count.

`tests.local_speed_paired_run` reads the old reference from the **base Git
commit**, not the candidate checkout. It does not write
`eligible-reference.json` and always reports `reference_eligible: false`.
The separate `approve_reference` API and standalone `--reference-output` path
still require the candidate to pass the prior reference first; a proposed
replacement cannot grade itself. Where an initial historical control is used
for that API, it must be this exact candidate in the same runner pair, with the
same dependency tuple, OS and workload. It cannot substitute an old product or
bypass an established default-cell reference. Reacquisition after a new product
dependency tuple remains separate work; this packet makes no cross-tuple claim.

The workflow covers the harness dependency paths and `pyproject.toml`.
`dev-constraints.txt` pins direct development requirements for this lane;
product dependency source locks are unchanged. The retained environment freeze
also records transitive versions; this constraints file is not claimed to be
a fully resolved lock for every package.

Focused proof lives in `test_local_speed_gate.py`, `test_local_speed_waits.py`,
`test_local_speed_write_attribution.py`, `test_local_speed_ambient_calibration.py`,
`test_local_speed_poll_windows.py`, `test_local_speed_host_cadence.py`
and the resident-process probe, alongside
all original observer/collector tests. `tests.local_speed_gate_probes` retains
old-accept/new-reject results against Agent `19103ea6` in each hosted artifact,
separately from the active calibration mechanism probes. The latter accept
independent base variation and reject a real extra per-job commit or sleep even
inside the aggregate margin. Direct SQL COMMIT, no-op commit, partial coverage
loss and actual retained-worker observations have separate focused probes.
The full paired run remains the product acceptance evidence; neither unit
tests nor an instrumented duration qualify D1 or real GPU performance.

## Original acquisition policy (historical)

The original v2 acquisition checked median and slowest overhead. Overhead is
caller wall time minus the stand-in execution span; queue time is retained.
The policy was fixed before acquiring the references: median at most
`1.5 * reference + 3 seconds`, slowest at most `2 * reference + 5 seconds`.
Deterministic counter maxima may not increase. Background sleep attempts,
Runtime COMMIT statements and observed native flushes have a separate
`50% + 10` allowance. Native coverage must match the reference; an unknown
count cannot replace an observed count.

The v2 comparison requires the same OS, workload, dependency commits, policy,
both callers, native coverage, and at least the recorded number of samples.
A failed reference is never automatically replaced with the current result.
Python force requests, attempted Runtime COMMIT statements and completed
native OS flushes remain separate quantities. Windows native flush attribution
is unqualified and belongs to LS-13; its absence is never treated as zero.
`--native all` retains the strict Windows trace path.

## Windows acquisition

- Run: [44111104582](https://github.com/banodoco/astrid-agent/actions/runs/44111104582)
- Job: [134136613016](https://github.com/banodoco/astrid-agent/actions/runs/44111104582/job/134136613016)
- Branch head: `26c664d435b04ac0f44443cbc3fc6686ca2f4df0`
- Actual tested merge: `3663fd5c667549ae764144866e1354bce46d5c04`
- Both have tree `ead505c1ab097eec7ff9d383becad60fbd868eee`.
- Artifact: `14347147925`; downloaded and verified ZIP SHA-256
  `f9f85595c7e2e6a1d3622771c580a334a2f40bc0b0346e5f7c5da89f0fd9831b`.
- Python 3.11.9, Windows 10 build 26100; 10 latency and 4 counter cases passed,
  with no failures, errors or skips.

The committed Windows reference is derived from the complete JSON summary
printed in that job log and exactly reproduced from the downloaded artifact's
raw latency and counter records with the v2 helper. Its original v1 baseline
is preserved in the artifact. Conversion to v2 adds explicit unobserved native
coverage; measured values, source commits and tolerances are preserved.
The ZIP digest matches GitHub's digest and the extracted evidence files match
their retained SHA256SUMS manifest.

| Caller | Samples | Median overhead, s | Slowest overhead, s |
| --- | --- | --- | --- |
| Resident SDK | 5 | 99.969 | 102.312 |
| Fresh CLI | 5 | 48.125 | 56.000 |

The workflow requires the recorded reference on each OS and fails if its file
is missing. Product changes trigger the meter, retaining the historical
comparison and the calibrated current-default comparison separately. The acquisition-only product freeze is
removed; exact dependency source-byte and identity checks remain.

## Initial Linux acquisition failure retained

[Job 134136612257](https://github.com/banodoco/astrid-agent/actions/runs/44111104582/job/134136612257)
passed all 10 latency cases, but both instrumented SDK cases exceeded the
existing 600-second pytest limit. Both CLI counter cases passed. The outer
capture completed with exit 1, not timeout 124. Artifact `14346966205` was
downloaded and its ZIP SHA-256 verified:
`9b463b09b51b336b692c4ebe7df5c881c43284a7a0f281c649fbce360c13aa00`.

The traceback reaches recursive SDK conversion under the Python observer.
The same observer completes on Windows without Linux syscall tracing.
Linux now requests strace's `--seccomp-bpf` optimization for the unchanged
process/fsync/fdatasync selection. This avoids ptrace stops for unrelated
syscalls; the native calibration, original reader checks, test time limits,
sample counts and assertions remain. See the
[upstream strace documentation](https://github.com/strace/strace/blob/master/doc/strace.1.in).

## Linux acquisition after the tracing correction

- Run: [44127948473](https://github.com/banodoco/astrid-agent/actions/runs/44127948473)
- Job: [134196776058](https://github.com/banodoco/astrid-agent/actions/runs/44127948473/job/134196776058)
- Branch head: `46024d467df98b81621012afdb7e8fcd36ef9f2d`
- Actual tested merge: `6bbc74dbe43e5244e3973e0ea303bd92e53211ac`
- Both have tree `5b0c1234452a2efa883572af85b5c73b105a4d9e`.
- Artifact: `14353432178`; downloaded and verified ZIP SHA-256
  `444dedb9e04bcd2bc64a230eedc7eca984e14037099fd34f1cf664ab20752ece`.
- Python 3.11.18; 10 latency and four counter cases passed, with no failures,
  errors or skips. All extracted manifest entries verified.

The v2 helper reproduces `linux.json` exactly from the artifact's raw records.
The two SDK counter cases finished in 90.679 and 84.044 seconds; both CLI cases
passed in 63.985 and 63.616 seconds. The complete counter capture took 307.813
seconds and returned exit 0 with the original limits. Calibration still sees
nine native flushes. The two measured samples per caller each see 161 completed
flushes for SDK and 160 for CLI, with zero observed failed flushes or coverage
gaps. These are separate from the 150 observed Python force requests per call.

| Caller | Samples | Median overhead, s | Slowest overhead, s |
| --- | --- | --- | --- |
| Resident SDK | 5 | 22.438 | 22.617 |
| Fresh CLI | 5 | 16.920 | 16.991 |

This run acquired the Linux reference; a subsequent run must pass its actual
comparison. The Windows job in the same run did compare its previously recorded
reference: [134196776550](https://github.com/banodoco/astrid-agent/actions/runs/44127948473/job/134196776550)
passed all 10 latency and four counter cases with `baseline_status: PASS` and
no regressions. Its SDK median/slowest overhead was 59.641 / 64.828 seconds;
CLI was 31.609 / 33.000 seconds. The original Windows reference is retained,
not reset to these faster samples.
Artifact `14354311252` and its extracted manifest entries were verified; the
ZIP SHA-256 is
`6abab98ff7e7cc8d79da015b4086cc6be527824fbdaf2ad5173ff4f82a5b2aa3`.

The unchanged latency measurement runs without observers. Differences between
acquisition runs are not attributed to the Linux tracing correction. The
correction is supported by the instrumented SDK cases completing within the
same limit while native visibility and all counter assertions still pass.

Local assembled-candidate checks passed all 110 observer, collector and policy
cases (two existing integration cases deselected in the default lane). Workflow
YAML, its embedded Python, capture syntax and reference-policy validation pass.
Local native calibration is NO-RESULT because this container denies
`PTRACE_TRACEME` with `Operation not permitted`; no privileges were changed.
Both hosted calibrations and the full repeated meter passed in run 44127948473.
At that acquisition, the workflow required a reference comparison on both OSes.
Current LS-2.2 submission additionally requires the calibrated base holdout and
candidate comparisons on both systems, retained in the packet evidence. The
historical acquisition and later rejected/canceled runs remain distinct results.
