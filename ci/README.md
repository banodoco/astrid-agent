# Plan 39 K2/K3: trusted per-ID failure ceiling

Packet R14-P39-14; session `r14-P39-14-1`. Authority is astrid-dev
`4223b30a2b56c7eb59da3a47b200516372bdca5a`, RS1 `6995664680`, amendment
`6996255390`, and Lead decisions `6998123631` / `6998138942` / `6998651356`.

## Trust and outcome

`Failure ceiling / trusted failure ceiling` exports `ci/` and `scripts/ci/`
from the immutable PR merge base into a temporary directory, then runs that
copy with `python -I`. Candidate JSON is data. Candidate Python modules,
reporters, dependencies and `PYTHONPATH` cannot replace the comparator. A
scheduled run uses GitHub's explicit accepted default-branch SHA. A manual run
must name its destination branch. There is no `HEAD^` or candidate-code fallback.

The introducing PR is expected to fail the trusted job because its base has no
comparator. Standing ruling 5 permits that bootstrap red once, subject to Lead
review. The candidate probe job tests implementation; it never qualifies product
results. The Lead must install the trusted default-branch gate-integrity policy
and required workflow/check configuration through the existing integration
process. This packet does not change repository settings or merge destinations.

The comparator accepts a complete run only when every failing native identity
is an unchanged sealed entry with exactly matching lane, parameter, subtest,
failing stage and diagnostic. The accepted initial seed has two immutable failed
witnesses and an explicit Release 15 owner for each admitted entry. Missing G1
proof, changed causes and unexplained sensitive failures receive no entry.
Runner checkout paths, pytest temporary roots, ANSI formatting and object memory
addresses are the only diagnostic normalization; values and parameters remain.

Actual commands, output and exit codes are retained by `ceiling_capture.py`.
Existing test processes still fail. A successful ceiling verdict is separate
from their red native results. No test assertion, selector, marker, timeout,
retry or existing exit policy was loosened. Required PR shards/native jobs now
run in full; independent test steps are attempted after an earlier red, unless
the job is cancelled. A blocked setup or unfinished suite cannot qualify.

## Complete evidence

`required-checks.json` names every expanded merge/release job, its original
commands, native invocation count, matrix identity and expected dependency
revisions. Each job uploads `failure-ceiling-<job>-<matrix>-<attempt>`. The trusted
collector selects the latest run/attempt at the exact head, downloads all pages,
checks artifact digests, and binds command receipts to run, job, matrix, attempt,
execution tree and dependency tuple. A synthetic PR merge is accepted only when
its Git tree equals the requested head's tree; both original SHAs are retained.
An unaccounted failed step, missing workflow or skipped/cancelled job is not a
covered failure. Nightly requires the current UTC day's completed release jobs.

The pytest observer records collection, every setup/call/teardown, subtest
context and the terminal exit/count. Runtime's existing wrapper additionally
writes this native stream inside its existing sandbox results mount. Node,
Vitest and Playwright observers preserve selected/result identities and terminal
counts; Playwright retains every retry and treats earlier red as red. Bun's
JUnit file, nested source identity and full native terminal diagnostics are
checked together. Existing result/skip guards remain active.

The expected inventory is append-only in a PR. New identities need actual PASS
and must be retained for future lost-ID detection; they cannot add an omission
allowance. Existing identities, source bindings, omission reasons, quarantine
expiry and authority are immutable. The initial skip allowances come only from
matching native outcomes plus explicit existing source/registry bindings. The
37 raw Runtime macOS listener skips without registry binding receive no
allowance. Quarantines keep their existing 2026-12-01 expiry.

## Retirement without a self-referential commit

Remove a passing entry from `failure-ceiling.json` and append its exact identity
to `retired`, with a selector for the job that must prove the pass:

```json
{
  "lane": "the unchanged lane",
  "native_id": "the unchanged native identity",
  "parameter": null,
  "subtest": null,
  "receipt": {
    "source": "qualifying-run",
    "workflow": "CI",
    "job": "the exact expanded job name"
  }
}
```

This selector is a request for evidence, not a claimed test result. The trusted
comparator requires that exact test to execute and pass in the complete run,
then emits the immutable head/dependency tuple, run ID, job ID, attempt and PASS
in `verdict.json`'s `retirement_receipts`. This avoids asking a commit to contain
its own future Actions identifiers. An exact already-known current-run receipt
is also checked, never trusted just because it appears in candidate JSON. A
retirement of a release-only entry adds that job to the PR's required evidence.
The Lead retains the emitted receipt with the accepted packet/release proof.
A passing parent with a failing subtest cannot retire that subtest or mask it.
Any subsequent failure of a retired identity is rejected, whatever its cause.

## D4 and proof

Each comparator preserves `observed.json`, `verdict.json`, original downloaded
archives/provenance and `nightly-report.md` in its report artifact, and writes a
job summary. The summary names the first new red, missing evidence and impending
quarantine expiry; complete diagnostics stay in JSON. Per Lead decision
6998123631, the Lead posts new red manually to **banodoco/astrid-dev#971**. A
cross-repository secret/poster is outside this packet. A clean night needs no
rolling-issue comment. Artifacts are retained for 90 days.

Run the stdlib-only comparator rejection probes with:

```sh
python -I scripts/ci/test_failure_ceiling.py -v
```

The suite covers complete covered merge/release fixtures; exact actual-retirement
receipts; new red and same-count lost ID; missing job/nightly; stale/cancelled or
partial evidence; new skips/xfails; changed job/inventory/ledger; addition/swap;
retired recurrence; sensitive same-ID different-cause/stage; failing subtests;
native-ID source swaps; dependency mismatch; new-ID retention; release-only
retirement; and candidate-code/PYTHONPATH isolation.

Run actual native fixture execution, with Git and Node available, using a new output directory:

```sh
python -I scripts/ci/prove_failure_ceiling.py --output /tmp/p39-native-proof
```

The retained `native-executed-proof.tar.gz` contains all ten executed native
commands, raw exits, collection/results, and verdicts, including exact passing
fixture-retirement receipts. `verification-summary.json` binds those receipts to
the implementation hashes and records the 27 passing rejection probes. The
separate adapter archive records actual pytest, Node, Bun, Vitest and Playwright
probes. These local fixtures do not establish a complete product run or remove
any product ledger entry.

`ci/evidence/p39-k2-k3/` contains the source-bound seed, nine-ID reconciliation,
33-subtest reconciliation, missing-job facts, omission source facts, source tuple
proof and native probe records. The extraction recipe is not run by CI and
cannot enlarge an accepted ledger. Reproduction inputs are the immutable dev
G1/G2 reports and native artifact IDs/hashes named in the evidence; local paths
in extraction facts identify the original workspace copies, not extra evidence.

## Qualification limits at this checkpoint

This is a draft implementation with the initial seed accepted by Lead in
6998651356; product qualification remains blocked. All nine historical missing
identities are reconciled: eight exact aliases and ID9's two explicitly accepted
coverage replacements, the gate-level structure test and its direct comparison
test. This does not introduce a general alias rule. The historical absent/incomplete jobs
are kept explicitly unknown; a count or a duration does not become a PASS.
Unrecorded lane censuses still need complete current native evidence before the
initial inventory can be accepted. The inventory therefore retains explicit
unresolved reconciliation items that prevent a green qualification.

The accepted conservative initial seed contains **Agent 163, Runtime 0, App 20**
entries. These are eligibility decisions, not total failure counts. Runtime's
remaining same-message candidates touch unresolved custody, persistence,
settlement or refusal behavior; other pairs changed from G1 setup errors to G2
call failures. No R15 family label overrides that difference. The one App tour
case with an undefined persisted status is also excluded. All such actual reds
remain uncovered.

The Agent Windows journey's sibling checkout tuple does not establish its
installed product tuple: the selected App installer defaults to `main` and uses
its own manifest revisions. That job stays NO-RESULT until its owner supplies
installed custody and a reviewed expected tuple. No pin or journey test was
changed here.

Native executable fixtures prove reporter behavior and the comparator protocol.
They are labelled fixtures and do not retire product failures. No complete
covered product run or actual product-ledger retirement is claimed. The initial
App run additionally has ten exact G2 PASS-to-FAIL identities, reported to Lead in
6998501613; none enters the ceiling. Lead owns their bisection and routing under
6998651356. Lead subsequently classified these as stale contract tests and routed
them to FIX-14 (6998771438); they remain uncovered until the fix lands and syncs.
Product qualification must preserve
these distinctions before Lead acceptance.
