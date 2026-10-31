# Regression evidence contract

The coordinator must call `.github/workflows/regression-green.yml` in an `if: always()` job with `needs` on **every** coverage job and C-07, passing `needs.<C-07 job>.result` as `freshness_result`. The caller must make its evidence available as the current run artifact `regression-evidence` even when lane tests fail. A failed or skipped C-07 job, absent artifact, inventory, tuple, ledger, or evidence is red. `workflow_dispatch` is deliberately red: it has no C-07 job. The caller must protect cancellation of integration runs.

The tuple is the P-04 JSON object; its optional digest is SHA-256 of UTF-8 JSON with sorted keys, compact separators and no ASCII escaping, excluding `digest`. The tuple is supplied as `regression-evidence/tuple.json`; the workflow checks its Agent SHA against the coordinator checkout. Inventory and ledger are reviewed files checked out from the tuple's Agent commit, not artifact contents. The inventory may optionally bind to a tuple digest; an unbound inventory is reusable across tuples. No empty or placeholder inventory is accepted. Inventory shape:

```json
{"lanes":[{"repo":"agent","lane":"unit-shard-1","platform":"linux","tests":[{"node_id":"tests/example.py::test_one","protected":false}]}]}
```

It must enumerate every required lane and every expected test, with protected journeys marked true. Tests absent from this reviewed inventory, missing from JUnit, or skipped are red. The inventory must include all install/update/rollback/data-preservation/bridge and feature journeys as protected. A test identity change therefore requires reviewed inventory and ledger changes; a `renamed_from` ledger row requires `admitted_by`. All rows, including unused rows, must validate against the P-04 schema. `expires` is inclusive, in UTC.

`regression-evidence/lanes/<lane-id>/manifest.json` contains `tuple_digest`, `repo_shas` (the complete repo-to-SHA map from the tuple), `repo`, `lane`, `platform` and `junit_sha256`; `<lane-id>` is the SHA-256 canonical JSON digest of `[repo,lane,platform]`. The adjacent `junit.xml` must contain at least one testcase. Each testcase has a `properties/property` named `node_id` containing the full inventory ID. A failure/error also has `outcome` (`failed`, `error`, or `timeout`), `phase`, and `fingerprint` properties, and `semantics_hash` when needed by its row. Producers must calculate the P-04 normalized fingerprint and test/fixture semantics hash consistently with admitted rows. Collection errors, missing identity properties, ambiguous or duplicate testcases are red. JUnit counters alone never stand in for testcases.

`regression-evidence/freshness.json` is C-07's receipt: `{"check":"C-07","result":"pass","tuple_digest":"<digest>","repo_shas":{...}}`. The C-07 job must itself perform all freshness, pin and installed checks and emit this receipt only on success. The caller must pass its actual job result; a receipt alone is not sufficient. Evidence must be produced from asserted effective checkouts on this run, not copied from a different run.

C-05 must connect the caller and implement the evidence producers. C-07 must implement the checks and receipt. Until then the workflow is intentionally unable to turn green.
