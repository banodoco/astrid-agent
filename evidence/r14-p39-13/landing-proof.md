# R14-P39-13 — terminal shard reconciliation and landing proof

Plan39 **AT1.9 / AT1.54**, Agent PR[#428](https://github.com/banodoco/astrid-agent/pull/428), `p39/at1.9` → `release/14/plan-39`.

The terminal hosted run establishes that the wrapper collects and reports the intended integration IDs in all four shards while retaining every real failure. The new plan head is merged without rebasing, and the wrapper/refusal proof has been repeated with the newly landed capture shell and native reporter. **This is submitted for Lead review; it is not a green-CI, sealed-ceiling, new-head hosted, or Done claim.** Lead owns any further hosted dispatch.

## Source and authority

- Tested merge: **`1a234537e768fe1641f308433da5bcd05b59f875`**.
- Tested tree: **`202445e1615e0acd7cff1acf5ca6559484e57623`**.
- Ordered parents: frozen hosted topic `086845def6ec28216384163d835f47d8cebbae81`, then destination `68e7aa8593d13eaeb6a55da1d97bed8b494df15b`.
- The source merge is conflict-free and retains both histories. No rebase occurred.
- The final publication adds only four evidence files to this tested merge. The exact final landing SHA is in the mailbox and PR, avoiding a circular self-reference here.

[Lead7002163080](https://github.com/banodoco/astrid-dev/issues/796#issuecomment-7002163080) requires terminal reconciliation, exhaustive setup-error classification without fixes, then the merge and frozen landing head. The complete classification was submitted before the source merge in[7002668254](https://github.com/banodoco/astrid-dev/issues/796#issuecomment-7002668254). The paired local result and two inherited workflow-test failures were reported in[7002784820](https://github.com/banodoco/astrid-dev/issues/796#issuecomment-7002784820).

The only source/test differences from destination68e7 are the existing packet changes in `scripts/ci/run_pytest.py`, `tests/core/rendering/test_local_schema.py`, and `tests/scripts/test_ci_test_evidence.py`. These three files are byte-identical to the hosted086 head. Incoming K2/K3 workflow, reporter, collector, checker, ceiling and inventory bytes remain exactly as accepted on68e7. There is no pin change, setup-error fix, Plan38/55 test change, destination merge, board edit or second packet.

## Terminal hosted proof

Candidate [Lanes44454822934](https://github.com/banodoco/astrid-agent/actions/runs/44454822934), attempt1, on086845de, completed **failure** at06:41:06UTC on19Nov2026. Its comparison base is [Lanes44439705490](https://github.com/banodoco/astrid-agent/actions/runs/44439705490) on32bd22ee. The newer68e7 head is not substituted into this historical pair.

| Shard | G2 selected | Candidate selected / primary reported / JUnit | Completed calls | Pass | Call fail | Setup error | Setup skip | Raw pytest exit |
| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
|1|516|517 / 517 / 517|517|480|37|0|0|1|
|2|516|517 / 517 / 517|345|268|77|172|0|1|
|3|516|517 / 517 / 517|454|371|83|62|1|1|
|4|516|516 / 516 / 516|502|401|101|14|0|1|
|Total|2064|2067 / 2067 / 2067|1818|1520|298|248|1|all1|

All four reference/execution manifest pairs agree. Each selected set equals its primary reported set, and each JUnit identity multiset agrees with the native reports. Both passes retain `/home/runner/work/astrid-agent/astrid-agent`, `pyproject.toml`, and testpaths `tests, tools/astrid-rename/tests`. There are no subtest reports.

The base had **75 dependency collection errors per shard**, zero primary/call reports, absent execution manifests and raw test exit4; its separate XML-summary steps exited1. The candidate has **zero dependency collection errors in every shard**. Its248 setup errors occur after collection; they do not execute their test bodies. All four candidate JSONL finish events retain raw exit1, and all four XML-summary steps have successful API conclusions. The reference logs and successful summary steps do not print numeric success exits, so none is synthesized.

The single setup skip is the existing declared Linux exclusion:

```text
tests/integrations/test_generic_host_activation_acceptance.py::test_local_host_control_is_separate_and_survives_multiple_bounded_requests
[declared:darwin_native] needs native macOS APIs
```

### G2 and base identity reconciliation

G2 run[44308562424](https://github.com/banodoco/astrid-agent/actions/runs/44308562424) supplies four disjoint sets of516, union2064. The complete candidate sets equal the current comparison base **per shard**, with no changes relative to that base. Against G2, every original ID remains; three upstream source-copy IDs were added and all pass:

```text
tests/editing_conformance/test_commit_runtime_source_copy.py::test_a_duplicate_of_a_valid_source_passes_the_full_validator_with_the_copy_flag_off
tests/editing_conformance/test_commit_runtime_source_copy.py::test_duplicate_copies_a_head_that_already_fails_newer_rules[False]
tests/editing_conformance/test_commit_runtime_source_copy.py::test_duplicate_copies_a_head_that_already_fails_newer_rules[True]
```

The two G2 movements are confirmed in the candidate:

| Movement | Native ID |
| --- | --- |
|2→1|`tests/core/generation/test_generate_target_journey.py::test_f02_sdk_bound_target_failure_is_read_only[repo]`|
|2→3|`tests/editing_conformance/test_native_word_anchor.py::test_equal_text_restore_recovers_the_selected_publications_binding`|

The no-duration equal split changes516/516/516/516 to517/517/517/516. No duplicate shard memberships or missing G2 IDs exist. Canonical candidate/base union SHA256, sorted UTF-8 IDs with LF and final newline:

```text
1c0102f572497adaaf76a8375212200b6ac0a30b85682b7d9aed9edebffe3e13
```

### Setup-error classification

[setup-error-classification.md](setup-error-classification.md) gives **every cluster's per-shard counts, full representative native ID, first causal or observable frame, ownership and uncertainty**. Its full membership and exact representative traces are in the raw archive and JSON proof.

The partition contains20 clusters and248 distinct IDs, counts **0 / 172 / 62 / 14**. Three IDs match RT1's shared v33 readiness fence; two missing-df3d4dd cases are explicitly routed by Plan39 AE2 to Plan55 LS-7.2. **243 IDs in18 clusters are unowned**, for Lead routing. The104 managed-edge replacements split78/26 across shards2/3. The44 session-scratch archive errors are distinct from four missing-workstation-archive errors. Similar G2 Runtime RT6.50/.55 signatures are noted as related mechanisms, not silently assigned ownership of newly visible Agent consumers. AT9 and SF6d do not name these unrelated setup mechanisms.

No cluster was fixed, deselected, skipped, deleted, re-expected or added to a ceiling. A diagnostic that exposes only a fixture/HTTP frame is labeled accordingly; missing child traceback lines are not invented.

### Observed historical outcome changes

Only complete recovered G1 logs for shards1/2/4 are used. **G1 shard3 is unavailable and is not reconstructed.** Among1548 matching current IDs,1533 outcomes are unchanged and15 differ: one PASS→FAIL, two FAIL→PASS, three ERROR→FAIL and nine ERROR→PASS. Another519 current IDs have no available G1 outcome:516 from missing G1 shard3 and the three additions.

The one PASS→FAIL is:

```text
tests/core/execution/test_generic_host_child_bridge_d18.py::test_host_bridge_outer_send_diagnostic_preserves_revoke_close[False]
```

The fixture observes FD2 flags2049 instead of1 at`test_generic_host_child_bridge_d18.py:4891`, leaving O_NONBLOCK set. The test and SDK diagnostic writer files are byte-identical G1→086. Concurrent flag restoration is consistent with the observation; no introducing-change or deterministic-reproduction claim is made.

The three ERROR→FAIL cases now reach`tests/helpers/bootmanifest2_r12.py:52` but their subprocesses return1 with`unshare: unshare failed: Operation not permitted`:

```text
tests/test_bootmanifest2_admission.py::test_busy_rejected_attachment_preserves_r12_resident_reader
tests/test_bootmanifest2_rollback.py::test_real_rollback_activates_r12_own_composition[True]
tests/test_bootmanifest2_rollback.py::test_real_rollback_activates_r12_own_composition[False]
```

All15 exact changed IDs and all546 current failed/error native IDs are retained. The collection-aborted32bd base cannot establish prior passing product outcomes. G1's source/dependency tuple also differs; historical outcome changes are not attributed to this packet.

### Hosted artifacts and custody limits

| Shard | Job | Artifact | Bytes | SHA256 |
| --- | --- | --- | ---: | --- |
|1|135327185345|[14500195070](https://github.com/banodoco/astrid-agent/actions/runs/44454822934/artifacts/14500195070)|3057136|`fbee19351c6478fbb45d36bbcea94a848b9a8281df78346e66644d7a5cf02f0d`|
|2|135327185267|[14499772722](https://github.com/banodoco/astrid-agent/actions/runs/44454822934/artifacts/14499772722)|4396545|`4c1eb43946e3bc7ff2a5f7b6d2052416571ac25c4ae9336ccdab36eeb924e54c`|
|3|135327185419|[14504969230](https://github.com/banodoco/astrid-agent/actions/runs/44454822934/artifacts/14504969230)|4126548|`235a638c2d7d4954dfe080ab5f631492dd90d0f74b102e54d77adbe607828042`|
|4|135327185484|[14503716542](https://github.com/banodoco/astrid-agent/actions/runs/44454822934/artifacts/14503716542)|4049116|`b3e7a5b1e43f1ca1fc23493ecc8e87d4ec9cf9f5e0909504b6579f696c7dad94`|

Every archive matches its API size and supplied hash. Shards1/2/4 full job logs independently confirm Agent086 and the same base dependencies: Runtime2537fed24385ec1c24f48a812e09af4bb63fb794, VibeComfy c27fe76b79d917fd9c08246660bc77211be64146, Pinokio d15c58b5cd1f7e24c1e699340b33961027f7c573, lifecycle7f0b025e9391baac9f5c2fb7393617ea6fef0704 and ComfyUI6bfaacc67c2103481e5f0c84d75257cd0581d86a.

**Shard3's full Actions setup/job log is unavailable** after connector Transport-closed responses and a public403. Its original complete child reference/execution logs, manifests, events and JUnit are intact. Run/artifact metadata pins its provenance to086, but independently observed dependency checkout SHAs and its outer-shell numeric exit line remain unavailable. The child JSONL explicitly records exit1. No expected dependency value is presented as an observation.

## Local proof after merging68e7

Both sides run the same eight-file focused selection through the accepted`ceiling_capture.py` Bash shell and the actual`python -P -m ci.run_pytest -p ceiling_pytest` entrypoint. This exercises the new reporter alongside the packet's collection guard. Local Python3.12.14, pytest9.0.2, pinned Mypy1.17.0 and Ruff0.12.0 use the already provisioned private environment. Both sides use exact Runtime2537fed2 and its client.

| Source | Selected / reported | Pass | Fail | Declared Windows skips | Raw outer / execution exits |
| --- | ---: | ---: | ---: | ---: | --- |
|Destination68e7|293 / 293|271|3|19|1 / 1|
|Merge1a234537|297 / 297|276|2|19|1 / 1|

There are no missing shared IDs and no new failing IDs. AT1.54's refusal test is the **only shared outcome change**, FAIL→PASS. The four added external-evidence root/config shard probes all pass. The same19 Windows skip IDs remain explicit. The full control selection still exercises selectors, missing/short evidence, alternate config, skip/xfail attempts and candidate-controlled gate changes.

Both reference and execution passes keep their checkout root and`pyproject.toml`. The added ceiling reporter's native IDs, phases and outcomes match the existing collection_guard reports exactly. Its collect-only finish records0 and its execution finish1; the capture shell retains1. No reporter suppresses the inherited failures. The separately run new comparator/hosted-collector suite has **30 passing tests, raw exit0**.

The same merged wrapper run records the required real refusal:

```text
schema refusal probe: parent isolated=1; child exit=1
probe child isolated=0
RuntimeError: schema validation requires an isolated installed interpreter
```

### Two inherited failures kept visible

These existing tests fail identically on destination68e7 and the merge:

1. `tests/test_release_branch_workflows.py::test_pull_requests_into_release_mains_run_only_the_cheap_ci_subset` — line25 raises`KeyError: 'if'` because the accepted K2/K3 workflow no longer excludes the heavy PR jobs as the old test expects.
2. `tests/test_release_branch_workflows.py::test_chat_pull_request_runs_only_the_ubuntu_job` — line119 expects only`release/*/main`, while the accepted workflow also includes`release/*/plan-*`.

Their full diagnostic bodies match across the pair. The workflow/test bytes are identical to destination68e7. Neither assertion or workflow was modified. They remain for Lead disposition; this report does not claim all focused tests or fast checks are green.

The local capture journal detects its Agent checkout. Runtime's exact SHA is separately recorded in invocation metadata; the local directory is named`runtime-sync`, outside the capture shell's hosted checkout aliases. Thus local proof is not presented as a complete hosted dependency-tuple receipt.

## Evidence and remaining work

- [landing-proof.json](landing-proof.json): exact source/parents, selected IDs and outcomes, per-cluster membership and ownership, comparisons, capture records, commands, limits and every raw-member hash.
- [setup-error-classification.md](setup-error-classification.md): all20 clusters with counts, complete representative IDs and causal/observable frames.
- [landing-raw.tar.gz](landing-raw.tar.gz): **3251653 bytes,69 members**, SHA256`3b600dc432ddb366113880f8e1d21c302e525c235b2e095a6f76b44e3de8de49`. Every member was re-read and verified against its internal manifest.

The archive retains complete hosted child logs, reference logs, exact selected inventories, all native report/start/finish event lines, every failure diagnostic, the full setup classification, paired local logs/manifests/JUnit/native reporters and capture journals, exact authority snapshots and builders. The hosted native-report files are explicitly named`native-reports-without-collection-array.jsonl`; their original full-repository collection arrays remain in the original hashed GitHub artifacts. They are not mislabeled as complete original JSONL files.

Before this advancement, all four existing topic workflows were terminal: qualifying Lanes44454822934 failure, current CI44452062891 failure, intermediate44450803440 cancelled and original tests-only44436312449 cancelled. Original terminal gate/unit evidence remains preserved. No manual cancellation was invoked; cancellation initiators are not exposed. The cancelled checkpoint runs remain unqualified.

The final evidence commit changes no product/test/config/dependency input relative to tested1a234537. Lead decides any further hosted dispatch and dispositions for the two inherited workflow tests and unowned setup clusters. Comparator activation/required-check issue#984 and inherited structure issue#964 are outside this packet. No new-head hosted, sealed-ceiling, native Windows/macOS or destination-integration claim is made.

Routes used: Unknown; authenticated model-route provenance is unavailable. Root performed engineering and judgment; a helper recovered raw evidence without commissioned review.
