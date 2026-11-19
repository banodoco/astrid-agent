# R14-P39-13: synced source proof for Lead dispatch

This packet implements Plan 39 AT1.9 and AT1.54. The merged source passes the
focused comparison below. The four hosted integration shards and their
reconciliation remain pending Lead dispatch; this is not a green-CI or full
integration qualification claim.

## Exact source and merge

The tested merge is `c10c0923ec655b38260076b82f1aafa02f2ed4dd`, tree
`1b14ebd6da681af4dbdf418914e84e8434977e6c`. Its ordered parents are:

1. Saved packet work `12cf7d8705189f162ca4a1b32fd33600deabce87`.
2. Updated plan-39 `32bd22ee69fc12a46c82de4942b1ed26aeae4dcc`.

The merge was conflict-free and preserves both histories. No rebase occurred.
The source delta from updated plan-39 remains exactly the wrapper repair,
four added collection probes, and the separately authorized AT1.54 test
correction. The later evidence commit changes only this report, its JSON and
its raw archive. The final frozen branch SHA is published in PR #428 and the
mailbox so that the proof does not contain a circular self-reference.

Both sides use updated plan-39's exact Runtime pin
`2537fed24385ec1c24f48a812e09af4bb63fb794`. No pin was changed by this packet.

## Paired focused results

Each final invocation selects the same eight files through the real
`python -P -m ci.run_pytest` wrapper. The wrapper starts isolated Python for
both collection and execution. Results are outside each checkout.

| Scope | Selected / primary reported IDs | Outcomes | Raw exit |
| --- | ---: | --- | ---: |
| Synced base `32bd22ee` | 293 / 293 | 273 passed, AT1.54 failed, 19 declared Windows skips | 1 |
| Merged candidate `c10c0923` | 297 / 297 | 278 passed, same 19 declared Windows skips | 0 |
| Original hosted gate at `eb6e9ddf` | 162 / 162 | 158 passed, four new root probes failed | 1 |
| Those exact 162 hosted-gate IDs in the merged local proof | 162 / 162 | 162 passed | 0 for the containing run |

There are no missing base IDs. The only outcome change among the 293 shared
IDs is `test_installed_probe_refuses_a_nonisolated_interpreter`: failed to
passed. The four additions are the parameter groups `[1]` through `[4]` of
`test_external_evidence_preserves_repository_root_and_config_in_both_passes`.
They all passed. Their original hosted nested children all returned raw 4.
The unchanged Windows skip IDs are explicitly retained; a reported skip or
setup error is not evidence that its test body ran.

Both candidate logs retain the repository root and `pyproject.toml`.
Each added probe exercises a real four-way split with eight intended IDs,
two selected IDs per group, an external evidence directory and three nested
dependency test trees that must not be collected. The independent reference
and execution manifests match, and the checker validates the selected IDs
against actual reports.

The AT1.54 refusal occurs in the same 297-ID wrapper invocation. Its retained
output is:

```text
schema refusal probe: parent isolated=1; child exit=1
probe child isolated=0
RuntimeError: schema validation requires an isolated installed interpreter
```

The existing real product probe runs in a fresh `python -E -s -c` child.
The test asserts that child's actual isolation flag, raw exit 1 and intended
refusal. The gate parent remains isolated.

## Gate controls and unsuccessful attempts

The full hosted gate selection and skip guard cover selector rewriting,
missing/dropped IDs, empty/truncated results, alternate configuration,
undeclared skips, xfails that disable execution, replaced gate tools and
candidate-controlled ceiling or scanner changes. Every selected control
passes on the final merged-source invocation. The production collection
checker, result checker, isolated entrypoint, protected-path policy, lane
registry, pytest configuration and Lanes workflow are byte-identical to
updated plan-39. AST comparison preserves every other schema-test node
except the added `subprocess` import, and every existing evidence-test node.

Two earlier 297-ID attempts remain in the archive with raw exit 1:

| Attempt | Outcomes | Diagnosed environment input |
| --- | --- | --- |
| Initial merged source | 250 passed, 13 failed, 19 skipped, 15 setup errors | Private environment lacked pinned Mypy and installed Agent metadata |
| After Mypy and Agent installation | 263 passed, 19 skipped, 15 setup errors | Isolated trusted scanner subprocesses could not import the uninstalled Runtime's `runtime_protocol` |

Installed `mypy==1.17.0`, editable Agent metadata, and the exact synced Runtime
and Python client in the private environment. This supplies the installed
packages that hosted CI also provides. The isolated subprocesses continue to
ignore `PYTHONPATH`; no import guard, assertion, selector or product source was
changed between attempts. The final paired proof uses this fully provisioned
focused environment. Versions, installation commands, logs and native failure
records are retained. Local Python is 3.12.14; pytest is 9.0.2 and Ruff is 0.12.0.

## Trusted integration reference and remaining work

The oracle is G2 Lanes run
[44308562424](https://github.com/banodoco/astrid-agent/actions/runs/44308562424),
Agent `2266c24a8d898ce122da2c9e616a870b3410fe06`. Its original reference
manifests select 516 IDs per shard, with disjoint union **2,064**. The canonical
union SHA256 is
`eb3b5a73c87c19f15fc51bd3a9c761156514843fffc1928194cf3c7c1389d224`
(sorted native IDs, each followed by LF, encoded as UTF-8).

The archive's `reference-census/expected-inventory.json` retains each shard's
complete IDs, hashes and original run/job/artifact provenance. All 192 G2
source paths remain in the synced base; 190 are byte-identical and two have
upstream changes. Actual additions and any shard movement must be reported
from hosted collection, not guessed from this source-file comparison.

Lead dispatches all four integration shards on the frozen topic. The packet
then compares each current reference with its actual primary reports, and
all four selections/executions with the G2 inventory. Raw failures, setup
errors, missing evidence and exits remain visible. G1 shard 3 is unavailable;
Lead 6997062550/6997194447 explicitly prohibit reconstructing it or treating
that absence as an AT1.9 blocker. The older packet count of 2,063 is not the
oracle. Inherited structure issue astrid-dev#964 remains outside this packet.

## Original run and advancement custody

Lead [6998123631 item 2](https://github.com/banodoco/astrid-dev/issues/796#issuecomment-6998123631)
superseded the wait for the tests-only checkpoint. The topic advanced from
`eb6e9ddf` to `12cf7d87` using an exact-head, non-force update before this merge.
No manual cancellation, dispatch or rerun was requested. The original run
and intermediate automatic run are nonqualifying; any workflow-concurrency
cancellation is recorded in the final mailbox receipt.

Original CI [44436312449](https://github.com/banodoco/astrid-agent/actions/runs/44436312449)
retains the gate evidence above. Its terminal unit job 135265962059 has 5,345
selected/reported IDs, no identity loss, raw exit 1: 5,243 passed, 45 failed,
54 setup errors, three skipped and 153 passing subtests. All 99 failing/error
IDs and artifact hashes are retained. These old unit failures are not a
current-candidate comparison or a claim that setup-error bodies executed.
The original full unit archive is hosted artifact 14493818427, SHA256
`1f765c7b918317f86c7eda3732bc8ab88ce2c24a570b4c214ce0ebebb74985bb`;
its full ZIP and logs were also preserved locally.

## Evidence files

- [synced-proof.json](synced-proof.json): exact native outcomes, comparisons,
  commands, source identities, preservation checks and all raw member hashes.
- [synced-raw.tar.gz](synced-raw.tar.gz): 87 members, 641,696 bytes, SHA256
  `9a5fc9f644d31d6955e1162a3d89a62ece31acaf7e034223dac4f4486f537f23`.
  Includes both unsuccessful candidate attempts, final paired runs, baseline
  preparation, original hosted gate evidence, G2 IDs and the receipt builder.
- [local-proof.md](local-proof.md): the unchanged earlier pre-sync snapshot.

Routes used: Unknown; the runtime provides no authenticated model-usage
trace. The packet root performed engineering and judgment. A same-model
helper collected existing evidence; no independent review was commissioned.
