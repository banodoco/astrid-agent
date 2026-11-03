# AG2-C07-SHARD-FIX-R1 — repair stale custody test fixtures

goal

Fix the test-only stale adapter invocations exposed by exact-published-tuple hosted Actions run 41532549033. Keep the product API and fail-closed binding requirement unchanged.

exact inputs

- Repository: `banodoco/astrid-agent`.
- Product Agent base: published `ab43bad818ef65ca1e929b973a2f61cd9fc4bf61`.
- Exact hosted harness candidate/run: `lane/M-AG2/AG2-C07-INTEGRATOR-R1` @ `47d14ec47937ba20d93526b9fca50de69368f791`; Actions run `41532549033`, shard 1 job `125576175457`.
- Diagnosis: `queue/done/AG2-C07-SHARD-FAILURE-DIAG/REPORT.md`; raw failure in `ORCHESTRATOR.md` at 2026-11-02T22:19:49Z and 22:19:53Z.
- Relevant module: `tests/core/execution/test_generic_host_actions_v3.py`. `_ManagedVibeSessionAdapter.from_host_session` requires keyword-only `binding`; production callers supply it. Three stale test invocations omit it: shared `_sdk_cpu_handoff_fixture` and two direct factory tests.
- Do not edit or weaken the product adapter/API. The other shard failures are outside this task's fixture fix.

write scope and forbidden files

- Edit only `tests/core/execution/test_generic_host_actions_v3.py` in astrid-agent, plus task-local receipts/reports.
- No production modules, CI workflows, Runtime/App/Pinokio, bridge or protected paths, published refs, runtime lock, or other managers' files.

branch and base

- Branch: `lane/M-AG2/AG2-C07-SHARD-FIX-R1`.
- Base: published Agent `ab43bad818ef65ca1e929b973a2f61cd9fc4bf61`; preserve ancestry; no rebase/squash/force-push.

commands to run

Use `/home/user/merge/kit/codex/env-safe.sh` for shell/Git commands and `/home/user/merge/kit/codex/task-clone.sh astrid-agent <task-folder>/agent ab43bad818ef65ca1e929b973a2f61cd9fc4bf61`. Keep scratch in this task folder; use host shared package caches.

1. Construct `SessionBinding` before the adapter factory call in `_sdk_cpu_handoff_fixture`, pass the same binding to the adapter and manager setup, and supply valid bindings to the two direct factory tests.
2. Run the entire named module: `python -m pytest -q -o addopts= tests/core/execution/test_generic_host_actions_v3.py`. Record inner exit and exact count; do not use `--maxfail` or `-x`.
3. Run `git diff --check`, audit the changed path, push WIP before the focused test and push the final lane normally.

Done when

The three stale fixture invocations pass valid bindings without relaxing product behavior; all tests in `tests/core/execution/test_generic_host_actions_v3.py` pass; changed-path audit contains only the named test module; the pushed lane receipt records repo/head/base/parents, exact commands and inner exits, coverage inventory, diff and hash, and limitation that other shard failures remain separately classified.

deliverables

- Pushed WIP and `lane/M-AG2/AG2-C07-SHARD-FIX-R1` refs.
- Canonical `RECEIPT.json` and `REPORT.md` with exact focused proof and hashes.
- Handoff to M-AG for integration only after focused acceptance; no claim that the active C-07 hosted shards are thereby green.
