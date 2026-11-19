# R14-P39-13: focused proof before the Lead's plan-39 sync

This is an immutable evidence snapshot for Plan 39 AT1.9 and AT1.54, not
release qualification. The starting Agent is `9a698c39f51089feeb83f51791c7e6b19823d644`;
the candidate is `7206f0f718f0fc81a8ba28476b69ff833ba7cd26` (tree
`752731d0505d3f8162f3b34e08a742fee13a052e`). The exact tree is also recorded
in `local-proof.json`.

## What failed, and what changed

The first checkpoint, `eb6e9ddfdb74ad0cde85fee91a3dec19f5cc1510`, adds four
real-subprocess reproductions. Each creates the repository's `pyproject.toml`,
eight integration IDs, three nested dependency test trees, and an evidence
directory outside the checkout. On the original wrapper all four cases fail:
the reference selects the intended IDs, but execution collects the dependency
trees and returns raw exit 4.

Pytest discovers its configuration before loading the collection plugin's
options. A separate, already-existing `--collection-reference` value looks
like a positional test path at that point. The common root moves above the
repository's configuration. AT1.9 commit
`32e703f0cb6b2e3e2a918891f31d304bb0282101` attaches all internal evidence paths
to their options as `--name=path` in both passes. It changes no declared
selectors, config, lane policy, reference validation or exit handling.

The original AT1.54 test fails at `assert not sys.flags.isolated`, before it
can test the product. Commit `7206f0f718f0fc81a8ba28476b69ff833ba7cd26` is
the separate, packet-authorized test correction. It moves that precondition
and the unchanged real product probe to a fresh `python -E -s -c` child.
The wrapper parent remains isolated. The raw captured output records:

```text
schema refusal probe: parent isolated=1; child exit=1
probe child isolated=0
RuntimeError: schema validation requires an isolated installed interpreter
```

The test asserts the child's isolation flag, raw exit 1, and exact refusal.
Every other AST node in the schema test file, apart from importing
`subprocess`, is identical to the starting source. No other Plan 38/55 test,
product security rule, pin, workflow or configuration changed.

## Focused outcomes

`local-proof.json` records exact test-ID comparisons and every raw file hash.
`local-raw.tar.gz` contains the retained console logs, JUnit, manifests,
JSONL reports and raw exit files, including unsuccessful attempts.

| Selection | Before | Candidate | Identity comparison |
| --- | --- | --- | --- |
| `tests/scripts/test_ci_test_evidence.py` | 33 passed, 4 new-case failures | 37 passed | Same 37 native IDs; only the four new root cases change |
| `tests/core/rendering/test_local_schema.py`, isolated wrapper | 85 passed, 1 failed, 19 declared Windows skips | 86 passed, 19 declared Windows skips | Same 105 IDs; only AT1.54 changes |
| `tests/test_skip_guard.py` and `tests/scripts/test_gate_integrity.py`, isolated wrapper | Existing unchanged witnesses | 64 passed | Includes both initial missing-Ruff cases after provisioning the pinned tool |

The combined first gate run also executes nine alternate/nested configuration
controls: all pass. Its 185 IDs yield 164 passed, 19 declared Windows skips,
and two environment failures because Ruff was absent. The raw exit 1 remains
in the archive. Installing the repository's pinned Ruff 0.12.0 fixes those
two environment failures; no test was changed or deselected. The entire
64-ID skip/integrity selection then passes.

The existing witnesses exercise undeclared skip/skipif/importorskip,
unregistered and non-strict xfail, xfail that disables execution, copied
reasons, empty/deselected runs, lost IDs, plugin selection changes,
`-k`/`-m` and `PYTEST_ADDOPTS` precedence, truncated/missing/empty results,
replaced gate entrypoints and exact-head owner approval. The collection
checker and trusted protected-path policy are unchanged. This evidence does
not claim an arbitrary-code sandbox or implement the separate K2/K3 packet.

## Original hosted references

`reference-identities.json.gz` preserves native-ID projections and original
artifact/log hashes and URLs. The sources are G2 Lanes
[44308562424](https://github.com/banodoco/astrid-agent/actions/runs/44308562424)
and G1 Lanes
[43888589840](https://github.com/banodoco/astrid-agent/actions/runs/43888589840).

All four G2 reference passes have the repository root, `pyproject.toml`,
32,510 collected IDs and 516 selected IDs. Their selections are disjoint,
with a union of **2,064**. Every execution has the wrong `/home/runner/work`
root, 75 dependency collection errors, zero test reports and raw exit 4.

The recovered G1 primary ID sets for shards 1, 2 and 4 equal the corresponding
G2 reference selections exactly: 1,548 IDs, no losses or additions. G1 shard
3's full log is unavailable (`Transport closed` on two connector reads),
and that run has no integration result artifact. The sealed G2 report
records the same gap. The packet's stated **2,063 G1 primary executions**
therefore cannot be fully reconstructed from the recovered evidence. No
missing ID or outcome is inferred from that count.

## Environment, commands and limits

Linux, Python 3.12.14, pytest 9.0.2; Runtime checkout
`6855dfa8df65cf8ae4c002b57134d6505493c17c`. Local dependencies were reused
read-only initially; the final skip/integrity proof uses a private copy with
the pinned Ruff installed. Hosted jobs use their declared Python and complete
dependency checkouts. See `commands.txt` for the exact local entrypoints.

The current focused source bytes are unchanged from their recorded runs;
the full hosted four-shard proof is not claimed here. Final proof must use
the updated plan-39 destination merged into the topic, and must reconcile
all four selected/executed inventories against trusted references while
preserving failures and raw exits. Native Windows behavior is not established
by the unchanged, declared Linux skips.

Routes used: Unknown — the execution runtime does not provide an authenticated
model-usage trace. The packet session performed engineering and judgment;
one same-model helper gathered read-only historical evidence.
