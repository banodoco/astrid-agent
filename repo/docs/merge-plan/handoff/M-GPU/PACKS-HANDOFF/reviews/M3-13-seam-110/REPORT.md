# 110 — M3-13 selected-source boundary and production composition

**NOT READY.** The repair candidate and regressions are committed and its patch/bundle verify, but mandatory qualification is incomplete. At **2026-11-02 13:24:37 UTC**, available space fell to **2,873,292 KiB**, below the task's 3 GB guard. Qualification stopped immediately; the queued corrected composition check was cancelled before preflight. Later disk recovery was observed during retention, but no qualification resumed. The corrected Runtime/catalog/CLI test, exact-base overlay run, exact-final-head run, M1 seam review and subsequent Terra review remain outstanding.

## Exact tuple and artifacts

- Base / head parent: `d1b5da756599f7f373d2eb2c1e5bb0892e417453`.
- Base tree: `b0a031c3d62429bfcbd7835fd514ea583e345b99`.
- Final head: `4cf47a6a440580119ab3eee6ab828b149604e918`.
- Final tree: `1ac1e0bead92a1975649100affc7fe88d3c47578`.
- Branch: `lane/M-GPU/packs-M3-13-r6`.
- Subject: `Verify selected managed inputs and share production dependency admission [skip ci]`.
- [M3-13-r6.patch](M3-13-r6.patch): SHA-256 `c0fcf3df1622a0435a41d516778ac7c723a821f7d1409546302da364497ee4af`.
- [incremental.bundle](incremental.bundle): SHA-256 `049e35e8a9f7affd5cc81ba10e2027785a9b66145c0ce8e847c59b333c2124a5`.

Reconstructed the exact task80 base in a task-local shared clone from `sources/packs-lanes`, adding the shallow source's object-store alternate explicitly, then verifying/fetching bundles 46 → 53 → 61 → 64 → 72 → 80. Task80 bundle hash matched `f3b07a77cdba5eb00821d71dd9c6c6573f7342c8101ca66b748a1d86a00cc8fd`. No source mirror or W/src was edited. Final bundle verification identifies exactly the base prerequisite and final branch/head. The exact binary/full-index diff is byte-identical to the patch; `git diff --check`, isolated-index `git apply --cached --check`, apply and `git write-tree` passed, producing the final tree. See [artifact-verification.log](logs/artifact-verification.log), [materialization.log](logs/materialization.log) and [final-source-state.log](logs/final-source-state.log).

## Repair and scope

The authorized `source_setup.py` additive `selected_paths` seam leaves default whole-checkout verification unchanged. Opt-in callers verify only explicit files against blob identities in the pinned Git tree, checking the manifest first, regular-file mode, revision and manifest SHA-256. Checkout-wide status and `_tree_digest` are avoided because both can acquire unselected working-tree bytes. Unnamed roots receive no file verification; requested roots that cease to be active fail closed. The stored whole-tree digest remains provisioning provenance, not a relabelled selected-runtime digest.

Discovery observes state first, then verifies managed manifest/descriptors and canonical selected files at loader acquisition boundaries. Warm cache payload reuse verifies those selected managed files again. External managed/extra/env candidates now share the loader's descriptor/dependency closure, dependency-first acquisition and reclosure after optional I/O failure. Source v3 refusal and external v2 policy remain. Managed revision-address folders retain their canonical/provisioned identity policy; shared cycle checking and dependency presence close their graph without fabricating a canonical path. Canonical external discovery projects the same admitted inventory. Existing selection/repair/symlink fingerprint machinery is reused; no cache or schema was added.

Changed product paths are only `astrid/core/pack/{source_setup,discovery,loader}.py`. Existing single-scan/v3 test doubles accept the additive keyword; their assertions and tests were retained. The new `tests/packs/test_selected_source_composition.py` has nine cases: real managed cold/warm custody and default refusal; extra/env/managed dependency failure and repair, cached and cold; optional external I/O failure; and a joined loader/discovery → rendering CLI → GenericPackHost → real pinned Runtime capability catalog → task-family CLI/SDK admission. The joined test asserts withheld capability/admission before dependency repair and exact registered digest/admission after repair. Managed sentinel packs export no host actions; the external action pack contains only declared files. No action executes. No internal owner is replaced; the controlled I/O failure is injected at the filesystem edge.

These are implementation/test-design claims, not a passing joined-composition claim. The first composition attempt exposed a fixture entry declared outside `actions/`; only that fixture path was corrected. Its corrected final-head execution did not start.

Read review108 and reports 98/79/63/68/64/61/53/46. [BUGS.md](BUGS.md) binds the R4/R5 roots and all nine adjudicated invariants. No new in-scope R1 concrete-pack import, R2 policy owner or R3 writable-fact owner was introduced. No Runtime/M2/SDK/installer/schema product file changed. The authorized cross-owner seam still needs M1-owner review; the requested Terra review follows this report.

## Qualification accounting

| Revision / selection | Collected | Outcome |
|---|---:|---|
| Exact base + final new-tests-only overlay; seven frozen selectors + new module | 0 | Not run |
| Exact final head `4cf47a6a`; same frozen selection | 0 | Not run |
| Development `173df1e1`; new regression module | 9 | Four completed passes; fifth started then interrupted by the original 290 s cap; exit 124 |
| Development `173df1e1`; joined composition case | 1 | One fixture failure in 21.41 s; exit 1 |
| Final `4cf47a6a`; corrected joined case | 0 | Cancelled while queued; no preflight or tests |

The four development passes were managed cold/warm custody and the three cold extra/env/managed dependency failure/repair parameters. They are not reused as exact-final-head qualification. The interrupted run reported no assertion failure before timeout. The distinct fixture failure signature was `PackValidationError: actions.echo.invocation.path must be beneath actions/`; [development-fixture-fix.patch](logs/development-fixture-fix.patch) records its correction. No baseline/head failure equivalence can be established. No test was deleted, skipped or weakened.

Successful development preflights at `173df1e1` verified Python `/home/user/astrid/proof-codex/work/21/python-env/bin/python` (3.13.17), Runtime `/home/user/astrid/ws-X/sources/runtime-1e6d899` at `1e6d899efa77a2d6ec247c38250f9748330bb587`, VibeComfy `4acc1525236294e3542890a4a3000a399841ba55`, lifecycle root/import and distribution `0.3.1.dev0`, wheel `0.46.3`, build `1.4.0`, pytest `9.0.2`, httpx `0.28.1`, PyYAML `6.0.3`. Separate light Git inspection confirmed lifecycle `96360a27a53c4608f780f7b370f747a2aa3c7ca8`; see `logs/environment-provenance.log`. Runtime was in both PYTHONPATH and ASTRID_RUNTIME_CHECKOUT. Earlier launch corrections/pin refusals, a canary-yield exit 75 and queued cancellations executed no tests. Logs retain those attempts separately.

Every actual preflight, collection and execution ran through `/home/user/astrid/ops/heavy.sh`, with unique task-local TMPDIR and `--basetemp`; none bypassed the shared slot. The initial 290 s exploratory cap was too short for repeated real shipped-tree scans. The retained frozen launcher uses a bounded 900 s cap without reducing selectors/assertions; that frozen execution was never run. Commands, selectors and pins are in [run-validation.sh](logs/run-validation.sh) and [commands.log](logs/commands.log); node manifests and output are `logs/nodes-development-{run,main}.log` and `logs/tests-development-{run,main}.log`.

The final base overlay is **only** the new regression module; it applies cleanly and is byte-identical to the committed head module. See [regressions-overlay.patch](logs/regressions-overlay.patch) and [base-overlay.log](logs/base-overlay.log). No product fix or existing-test edit was overlaid on base. This is static overlay evidence, not collection/execution.

## Retention and limits

The disk-stop evidence is [stop-guard.log](logs/stop-guard.log). Task-created base/source clones, temp/basetemp/cache/scratch and sub-worker directory were removed after live-reference checks; delegated brief/report/exit are retained in logs. Only REPORT, task-local BUGS, logs, final patch and verified incremental bundle remain, alongside pre-existing runner files. Work stopped within the two-hour timebox.

No full suite, installed-artifact/installer, remote acquisition, cross-platform or hostile concurrent-mutation qualification occurred. No GPU/provider/pod, ambient credentials, ~/.codex or push activity occurred. Disposable Runtime credentials were generated only in test-owned state. The one Luna worker completed read-only mapping, wrote `exit=0`, and its REPORT was read; see `logs/luna-{brief,inventory}.md` and `logs/luna-exit`. The coordinator owns implementation, testing and this verdict.

Delegation: used 1 sub-workers
