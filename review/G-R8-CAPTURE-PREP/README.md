# R8 G0 capture preparation — 052 revision, 2026-11-02

This packet is CPU preparation only. **No P1–P12 family has been captured, no R8 tuple qualified, and no complete H3 baseline exists here.** `COMMAND-FAULT-MAP.md` provides the reviewed P1–P12 planned-test, fixture, operation, fault and missing-fact map. `capture-manifest.json` is a list of required operations, dependencies, intended raw artifact names, checks and unresolved prerequisites. A name is an operator collection destination, not a promise about a product-emitted filename. Capture cannot be honestly launched end-to-end until the manifest's fixture, operation and baseline blockers have been resolved by their owners.

The target is Agent `35bae39a586711547722e2724f0a1d52895a2783` with the authorized lock-only alignment to Runtime `aa1c91d9675296a2411e583bbd0330ddcc718044`, and VibeComfy `ac0ee1b80991f2414dae27d264af4b5e2890a7a6`. Record the resulting Agent commit/tree and before/after lock digests, installed source/interpreter identities and launch provenance; the base Agent hash alone is insufficient to identify the aligned installation.

P must attach this entire directory. From its parent run:

```sh
bash ./payload/run-cpu.sh
```

When the directory has another name, run `bash ./run-cpu.sh` inside it. The stdlib-only check generates disposable synthetic captures under the current directory and removes them. It emits 13/13 PASS when the comparator returns the expected PASS/FAIL/BLOCKED for each negative and positive check; synthetic tuple and captured labels are fabricated and cannot become R8 evidence. No install or network is used.

Before a later session: obtain the frozen installed-canary success and real H3 admission/claim gates; the R8 window is separately gated. Honor its no-retry, disk/stop and custody rules. Resolve all relevant fixture bytes, exact invocation and expected per-scenario operation mapping and the compatible baseline before consuming GPU time. Do not replace an unresolved input with a synthetic stand-in or infer a case from a related test. Use `reference/M2-06-H3-TEST-PLAN.md`, `reference/G0.md`, `reference/G0-COVERAGE.md` and its static censuses as coverage obligations; they describe H81→A537, not R8 observations. Follow `SCHEMAS.md` for capture index, reviewed aliases, allowances, and comparator invocation. A full inventory, raw evidence and independent completeness/provenance review are needed in addition to equality.

The older G0-RUN and G0-FIXTURES reports used Agent `81a1a0f91f14f0e61c5119b3a9a021b37e7d2c9d`, Runtime `f64f2229f64acd73dd97e3b5f82ad10e092fccfe`, VibeComfy `ac0ee1b80991f2414dae27d264af4b5e2890a7a6`. Their CPU captures were partial; synthetic worker data and installed-engine refusals did not establish real H3 publication or a baseline. Their source profile had removed paths, and their fixture/readiness identities were not approved. Those artifacts are references, not R8 evidence.
