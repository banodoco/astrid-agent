# Standing attached-meter references

These references describe the current attached stand-in workload, not the D1
speed target or GPU inference. Each caller has five uninstrumented samples
and two separate instrumented counter samples. Every sample follows a
successful warm-up in its own host session and verifies the delivered PNG and
two distinct succeeded task records.

## LS-2.2 gate

The standing workflow measures the exact PR base and exact candidate, in that
order, on one runner with one Python/dependency environment. Each side has five
uninstrumented samples and two separate counter samples per caller. The base
has only a byte-checked copy of the current test harness; its product, packs,
vendored client and source authority remain the base commit. Import locations,
product and harness identities, dependency freeze and raw results are retained.
The Windows comparison therefore does not use an unrelated historical runner
as its only timing control.

The current comparison is **15% relative** for both median and slowest overhead,
with **no absolute floor**. All counter caps remain upper bounds, including
waits, commits and forced writes. A missing previously observed counter fails.
The historical JSON files below retain their original values and acquisition
policy; those policy fields do not select the current comparison tolerance.
Newly observed counters are compared with the paired base under the same
observer, while the existing counters also keep the historical caps.

The attached cell leaves host, SDK and machine polling at product defaults.
It counts `Event.wait`, direct `Condition.wait`, `asyncio.sleep` and selector
waits as well as `time.sleep`; an Event's internal Condition is not counted a
second time. Waits already pending when the interval starts are disclosed
separately. Runtime claims, task reads and other requests are counted by method
and normalized path, without query values or per-job identifiers.

Zero worker starts is valid when a worker admitted before the interval supplies
a complete snapshot for that interval. A whole-lifetime/exit-only report is
insufficient. `test_local_speed_resident_observer.py` exercises this protocol
with a real process, two jobs and verified output bytes; it is meter calibration,
not a claim that LS-8's product implementation has landed. The attached product
proof continues to require two succeeded Runtime records and verified delivery.

Forced-write evidence reports effective SQLite settings before and after the
call, successful outer commits separately from attempted COMMIT statements,
application sync origins and Runtime routes, and platform write-through calls.
Linux native tracing retains descriptor paths and matches actual syncs to
observed application/commit spans. WAL-file syncs during a commit, WAL syncs
outside a commit and database/checkpoint syncs remain distinct measured facts;
a WAL sync is not automatically called a commit. Unmatched native writes fail
attribution. Windows native mapping remains explicitly unqualified under
LS-13; Python calls are never represented as a native count.

`tests.local_speed_paired_run` reads the old reference from the **base Git
commit**, not the candidate checkout. A proposed replacement cannot grade
itself. It writes `eligible-reference.json` only after the candidate passes
both the old-reference and paired-reference comparisons. This is an evidence
artifact, not an automatic repository reference update. The standalone meter's
`--reference-output` likewise requires and passes the prior reference first.

The workflow covers the harness dependency paths and `pyproject.toml`.
`dev-constraints.txt` pins direct development requirements for this lane;
product dependency source locks are unchanged. The retained environment freeze
also records transitive versions; this constraints file is not claimed to be
a fully resolved lock for every package.

Focused proof lives in `test_local_speed_gate.py`, `test_local_speed_waits.py`,
`test_local_speed_write_attribution.py` and the resident-process probe, alongside
all original observer/collector tests. `tests.local_speed_gate_probes` retains
old-accept/new-reject results against Agent `19103ea6` in each hosted artifact.
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
is missing. Product changes trigger the meter so future optimizations are
compared against these references. The acquisition-only product freeze is
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
The workflow now requires a reference comparison on both OSes. Submission
requires a later CI run passing both comparisons, retained in the packet evidence.
