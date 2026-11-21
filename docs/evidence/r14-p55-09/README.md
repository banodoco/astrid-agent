# LS-15 evidence checkpoint

This is evidence for R14-P55-09 only. The packet targets `p55/ls-15` into
`release/14/plan-55`; the Lead owns review, integration and the task board.
`checkpoint.json` identifies the sources, outcomes, raw archive and its hash.
`observations.zip` retains the executed failures as well as passes, complete
pytest summaries, XML, counter captures and selected Runtime records. Its
`manifest.json` gives each retained member's size and SHA-256.

## Replacement recovery checkpoint — 19 November 2026

Session `r14-P55-09-2` continues the same packet and PR #408 under Lead decisions
[6997582358](https://github.com/banodoco/astrid-dev/issues/796#issuecomment-6997582358)
and [6997585669](https://github.com/banodoco/astrid-dev/issues/796#issuecomment-6997585669).
`recovery-checkpoint.json` and `recovery-observations.zip` preserve the local
repair evidence and the previous published head's exact-ID hosted comparison.
This checkpoint precedes the new hosted qualification; it is not a completion
claim. The earlier checkpoints below remain historical evidence.

The fetched full history establishes `50db28a3` as an **ancestor**, not a child,
of the directed published head: `7e50a1a6 → 50db28a3 → afde8661 → c9f7940b`.
No recovered commit was discarded or replayed. Native merge
`f457f0f5092eee13511a81dfb03d852ceed4b3de` has parents `c9f7940b` and directed
Plan 55 `493616c1cfc7a83f18894dc81773571e977f26ef`. The dependency pins are
unchanged. Product repair `d8db907e7ac89637109815cbe3a5b0f84feb351a` follows
that merge; workflow commit `125cdb011293ec3e7d966b8d94699998be915c31` pairs
the proof with `493616c1` and adds the new trust-evidence probe.

HTTPS Git publication lacked a local write credential. The authenticated
GitHub connector published equivalent Git trees with new commit metadata.
Every tree below was compared for exact identity before the single guarded
packet-ref advance; the existing `c9f7940b` history and both merge parents
remain intact. The local source IDs above are retained as evidence identities.

| Change | Local commit | GitHub commit | Identical tree |
| --- | --- | --- | --- |
| Native merge | `f457f0f5` | `86a9653d2e32c5c35365940faedbd9ebcac8ef60` | `8c5a2e8c06774b787798107ca6b8864d71764430` |
| Product repair | `d8db907e` | `027657aae28c8f555efc7474541c4b50053c6b93` | `6b83c195dcbc73e76f99ad718a1be06fe3701a06` |
| Proof workflow | `125cdb01` | `4aab7942415c786467957bd7c44831e8cec7a623` | `8cbe2887f7c09ea843faa85ae392b76c0c5a6090` |

The previous `c9f7940b` run
[44384238876](https://github.com/banodoco/astrid-agent/actions/runs/44384238876)
establishes that three of the four former supervisor-socket failures pass.
The fourth, `trust-skips-absent-auxiliary`, passes the old socket point but
fails later because prepared jobs omit `declared_unverified` model evidence.
Canonical `astrid-dev@4223b30a:env/baseline-diff.py` classifies its unchanged
baseline `91cb4cb4` comparison as follows; these are previous-run observations,
not results against the new destination.

| Previous suite | IDs per side | Regression | Preexisting | Fixed | Missing / no baseline |
| --- | ---: | ---: | ---: | ---: | ---: |
| Linux regression | 582 | 0 | 10 | 0 | 0 / 0 |
| macOS regression | 582 | 0 | 32 | 0 | 0 / 0 |
| Windows regression | 575 | 0 | 146 | 1 | 0 / 0 |
| Linux native and contention | 43 | 1 | 2 | 0 | 0 / 0 |

The repair restores full ordinary approval on the cold prepared admission.
Each job then receives its own copies of that admission's model diagnostics
in its result and native attempt/metadata records. The prepared authorization
remains intact and the warm path performs no new approval. An identical new
trust probe fails on recovered merge `f457f0f5` with an empty model-evidence
list, and passes after the repair. It checks the declared model name and hash,
honest unverified status, distinct per-job inputs/results/prefixes, both native
record copies, valid output paths, one held websocket, and a guard that fails
if the second job repeats approval.

Local Python 3.13.5 proof ran on the working bytes subsequently committed as
`d8db907e`; the archive distinguishes post-run source hashes from the raw
execution-time command receipts. It retains every earlier setup and diagnostic
failure. The combined 36-ID selection produced 34 passes and two HTTPX setup
failures because the inherited local SOCKS proxy lacked optional `socksio`.
After installing `socksio==1.0.0`, only those same two unchanged IDs were rerun;
both passed. This is 36 IDs with passing observations across those two runs,
not a claim that the first invocation was green.

Both UI JSON and closed Python warm jobs have zero instrumented process
starts, emissions, compiles, schema fetches, catalogue scans and audits in
parent and child. Source capture still copies **68 files / 506,281 bytes**
before lookup. UI JSON has 46 parent source validations; closed Python has 42.
Both have five child source validations and five prepared authorizations.
These are measured costs, not latency or constant-time validation claims.
The separate public SDK probe passes with distinct real Runtime records and
independently checked outputs. Concurrent completion, cancel-one, failed-job
replacement, worker-crash custody, route identity and generation checks pass.

Two existing fixture corrections are proposed in
`proposed-fixture-repair.patch` and remain **unapplied** pending Lead's ruling
on [REQUEST 6997732085](https://github.com/banodoco/astrid-dev/issues/796#issuecomment-6997732085),
also recorded as [BLOCKED 6997967389](https://github.com/banodoco/astrid-dev/issues/796#issuecomment-6997967389).
The helper's Windows journal locator hard-codes the Unix runtime directory,
and its requested alignment mode is placed outside the validated generation
intent. The proposal uses the canonical runtime-root function and the existing
validated intent shape, retaining every assertion. The host helper, native
managed proof and unchanged native regression test are byte-identical to
`c9f7940b`. Native local AF_UNIX execution remains unavailable; hosted native
and Windows proof is required. No GPU, installed-user latency, LS-14, LS-15.1
or overall CI qualification is claimed.

## Earlier follow-up qualification checkpoint (retained history)

`followup-checkpoint.json` and `followup-observations.zip` extend the original
checkpoint below. They retain all four completed `a6155415` hosted artifacts'
raw logs, XML, inner exits, command receipts, exact-ID comparison lists and
selected outputs, plus the subsequent local failing and passing observations.
The archive manifest hashes every retained member. The original archive is
unchanged. This checkpoint does not claim completed hosted repair proof.

Local product `afde8661923aeb0a63fc08a4cdf13ed249eae544`, tree
`bde8fc118e007511d40afe720df6bce27ba951d7`, passes 28 route/context/generation
cases and all seven actual host/Runtime cases. Both UI JSON and closed Python
second jobs have zero of all six prohibited work categories in parent and
child. They still perform 46 parent source validations, five child source
validations and five per-job authorizations. Full capture before lookup is
68 files / 504,225 bytes; observed capture time is 32–43 ms. These measurements
are not latency qualification. The separate Runtime probe validates independent
task/attempt records, output digests and valid PNGs. The evidence publication
commit changes documentation and confines the Windows three-per-source
isolation step to one matrix suite; it leaves these product/test sources intact.

Completed [run 44373655368](https://github.com/banodoco/astrid-agent/actions/runs/44373655368)
compared `a6155415` with exact `91cb4cb4`:

| Suite | IDs per side | Regression | Preexisting | Fixed | Missing |
| --- | ---: | ---: | ---: | ---: | ---: |
| Linux resident | 582 | 0 | 10 | 0 | 0 |
| Linux native and contention | 43 | 4 | 2 | 0 | 0 |
| macOS resident | 582 | 0 | 32 | 0 | 0 |
| Windows resident | 575 | 0 | 146 | 1 | 0 |

There are no no-baseline IDs. Windows personal relocation passes all three
isolated executions on each source. Its fixed ID is
`test_inflight_identity_loss_fails_once_without_replay[kill]`; this comparison
does not establish a stable product improvement for that process race.

The earlier native diagnosis was incomplete: `a615`'s explicit policy addition
did **not** repair the four supervisor-socket failures. The LS-15 command branch
also forced passive network-hook startup, unlike the ordinary command path.
`50db28a3` preserves the caller's ordinary startup mode. A new actual denied-TCP
probe fails before this change and passes afterward. All existing native tests
are unchanged; hosted AF_UNIX proof is still required. That repair exposed two
warm Git children while re-resolving approved import locations. `afde8661`
retains the startup-approved roots with the incarnation. The checkout path and
execution profile remain in the session key, the Runtime root is the already
selected host import, and Vibe source/catalogue byte checks remain active.
The failed `50db` counters are preserved alongside the passing `afde` counters.

`7e50a1a6` fixes two other observed causes. Canonical source declarations are
retained within a bounded cache, copied for each job and bound to catalogue
resource bytes, removing a repeated `coverage_manifest` scan. The resident-only
advisory revision lookup reads bounded on-disk Git metadata and starts no child;
unsupported discovery remains unknown. It does not replace source attestation
with an advisory revision. Windows `a615` child stacks establish the stalled
advisory Git `communicate` call, but do not establish every other timeout cause.

The `a615` native probe made two valid records on the same worker and target,
then its nonempty native-command assertion exposed that its fixture had selected
`check` mode. The new fixture explicitly selects `align` for all three jobs and
additionally requires cold `nodes`, `prepare` and `doctor` commands. All 14 prior
assertions remain. The third changed-generation job and the repaired Windows
path need the next hosted run. All five unchanged regression command strings
and their time bounds remain intact; the OS comparison suites now have separate
matrix jobs so they do not share a time budget with packet probes.

## Original checkpoint (retained history)

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
the command environment. The initial repair restored that explicit policy
projection but was insufficient, as the follow-up checkpoint above records.
No Plan38 validator or assertion changed.

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
