# LS-15 evidence checkpoint

This is evidence for R14-P55-09 only. The packet targets `p55/ls-15` into
`release/14/plan-55`; the Lead owns review, integration and the task board.
`checkpoint.json` identifies the sources, outcomes, raw archive and its hash.
`observations.zip` retains the executed failures as well as passes, complete
pytest summaries, XML, counter captures and selected Runtime records. Its
`manifest.json` gives each retained member's size and SHA-256.

## Integrated sources

The requested native merge is `c3f25f227ae1579eb8c0a9457b1fcfec1a2e8f19`, with
parents packet `3977eef3` and accepted Plan 55
`91cb4cb499306b2f09686de0fb67b803bcb89077`. There was no rebase. The accepted LS-6
files and dependency pins remain identical to that target. VibeComfy is
`d8e61a507f7eb0c133db9d39a41bc429cb12f54b`; Runtime is
`f3dc970af71e7d413871b4a8e45e4ba0fbfabf5e`.

## Passing local product evidence

Exact product `4efff70f9fbb3fb26079613a2b1dda41050c6d84`, tree
`4589ac7cf45b4f6c79b236556c6c10a3a4106bfd`, has these focused results:

| Probe | Result |
| --- | --- |
| Second same-key host job and public SDK/real Runtime records | 2 pass; pytest exit 0; 69.02 s |
| Effective route, overlapping contexts and retained readiness | 17 pass; exit 0; 11.86 s |
| Actual overlapping Vibe jobs, cancel-one, failed-job replacement and worker crash | 4 pass; exit 0; 62.54 s |
| Identical second-job counter probe on exact `91cb4cb4` | Expected zero-work assertion fails; exit 1; 44.76 s |

The second job has zero instrumented process starts, emissions, compiles,
schema fetches, pack scans and audits in both parent and resident child. The
baseline parent has five starts, one emission, five compiles, 63 schema calls
and seven catalogue scans. Baseline disposable children are not instrumented;
an empty child list is not a zero-work claim.

Warm work remains: the host copies 68 selected pack files / 499,540 bytes before
resident lookup. This local capture took approximately 34 ms under
instrumentation. Parent source validation occurred 36 times; the child had
three source validations and five prepared authorization calls. These are
observations, not latency qualification or a claim of constant-time validation.

The separate public SDK probe dispatches two real Runtime tasks through the
owned host. Each has its own task/attempt, output prefix and stored result;
both succeed without replay and retain one worker/websocket. Every fetched
output is independently SHA-256 checked and both PNGs are decoded for validity.
The stored image digests are `f0d3f642c1606da47a655d2927f4ac29a7e07a43daf44b3aef9e1dcc31120be1`
and `47a391a288dfd7b7b9aeb585b305707587ea817e3c09911f7964b54395f3665e`.
The counter and public SDK probes are separate; this is CPU protocol evidence.

## Hosted failures retained for repair

[Run 44361088825](https://github.com/banodoco/astrid-agent/actions/runs/44361088825)
tested `705900b5cc152b3aee805851a8133016863c7b88` against exact `91cb4cb4` with
equal dependency pins. The canonical sorter is the unchanged
`astrid-dev@02d368f0:env/baseline-diff.py`. It reads the final `-rA` summary
sections; captured application log lines are not pytest test IDs.

| Platform | Unchanged test IDs per side | Regression | Preexisting | Missing |
| --- | ---: | ---: | ---: | ---: |
| Linux | 625 | 4 | 12 | 0 |
| Windows | 575 | 1 | 146 | 0 |
| macOS | No result: cancelled before any step | — | — | — |

Linux's four new failures are the unchanged managed-preparation variants
`repairs-before-worker`, `trust-skips-absent-auxiliary`, `device-explicit` and
`device-default`: their native supervisor-socket denial assertion failed. The
new resident command seam omitted the effective network policy when building
the command environment. The repair restores the same explicit policy
projection; no Plan38 validator or assertion changes.

The native warm probe reached retained-generation verification but exceeded
its unchanged 120-second timeout. Calling the host cancellation/evidence
callback for every file and read block repeatedly walked evidence under the
profiler. The repair retains every content read and before/after file identity
check, with host checks at entry, completion and bounded 50 ms intervals during
traversal. New interrupted-read cases preserve cancellation/deadline refusal.
The repair working-tree hashes and its 17 passing readiness cases are in the
checkpoint. These repairs need new hosted proof.

Windows' unchanged-test delta is the personal relocation fixture's second use
of `Path.home()/Astrid/packs/personal`. The first baseline case creates it; the
head then fails `mkdir`. The tests remain unchanged. Subsequent comparison
commands have separate Windows profiles and include three isolated executions
of that exact ID per source.

The Windows packet's cold job stopped after entering the pinned producer's
advisory `git rev-parse HEAD`. It has no valid warm result. Child stack capture
is added without increasing the test timeout. A new unit fixture also supplied
Windows paths to the Linux managed-preparation wire protocol; its wire facts
now use POSIX locations, with their manifest digest recomputed. All previous
assertions remain. Native installations use the separate real Linux proof.

## Limits

Local native-supervisor setup has no passing result. A direct local AF_UNIX
socket-construction probe returned `PermissionError` before bind; hosted native
proof is required. No GPU, installed-user latency or LS-14 result is claimed.
The standing meter used VibeComfy `dba38f8`, which has no `vibecomfy.prepared`,
and failed before producing speed observations. Dependency guards stay intact.
Existing failures remain executed. Plan38 ownership, astrid-dev #896 and
LS-15.1 remain outside this packet. This checkpoint is not completion or a
review verdict.
