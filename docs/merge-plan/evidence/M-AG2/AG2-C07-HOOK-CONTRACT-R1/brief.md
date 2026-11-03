# AG2-C07-HOOK-CONTRACT-R1

## goal
Reconcile the 14 failing assertions exposed by the published C-07 integration shard with the current `ComfyHostHooks` contract. Preserve the fail-closed release behavior and prove the tests exercise it. This is a test-only scoped follow-up to AG2-C07-SHARD-FIX-R1.

## exact inputs
- Repository: `banodoco/astrid-agent`
- Base/head: `lane/M-AG2/AG2-C07-SHARD-FIX-R1` @ `80fb5c1eaa20ce5d3d9117d880d2074068c8e1fd`
- Published tuple Agent: `ab43bad818ef65ca1e929b973a2f61cd9fc4bf61`; Runtime: `2776ba973b6c8f3555b5629b0840d9691a98d3c2`; App: `79f7e5397c044c4c97dc7e97b20ad96f787e5247`
- Failure report: `queue/done/AG2-C07-SHARD-FIX-R1/REPORT.md`
- Raw full-module log: `queue/done/AG2-C07-SHARD-FIX-R1/pytest-editable.log`
- Failed node IDs: `queue/done/AG2-C07-SHARD-FIX-R1/FAILED-NODE-IDS.txt`
- Diagnostic: `queue/done/AG2-C07-SHARD-FAILURE-DIAG/REPORT.md`

## write scope and forbidden files
Only `tests/core/execution/test_generic_host_actions_v3.py` in astrid-agent. Do not change product code, the frozen bridge contract, runtime.lock pins, integration assembly, workflows, other managers' files, or proof assertions outside the named stale hook expectations.

## branch and base
Create `lane/M-AG2/AG2-C07-HOOK-CONTRACT-R1` from exact base `80fb5c1eaa20ce5d3d9117d880d2074068c8e1fd`.

## commands to run
- Clone: `/home/user/merge/kit/codex/task-clone.sh astrid-agent <task-folder>/agent 80fb5c1eaa20ce5d3d9117d880d2074068c8e1fd`
- Inspect `ComfyHostHooks` implementation and the 14 failing assertions before editing.
- Run focused module proof with the task-local editable Runtime/VibeComfy environment: `python -m pytest -q -o addopts= tests/core/execution/test_generic_host_actions_v3.py`.
- Record exact inputs, command, inner exit, all node outcomes, changed paths, hashes, and limitations in a canonical receipt and REPORT.md. Push the lane head before reporting.

## Done when
The full named test module is green; its affected handoff cases assert the current typed hook result and fail-closed release/stop behavior; no product behavior or protected proof has been weakened; and the branch, receipt, report, and scope audit are complete.

## deliverables
Pushed lane branch, `REPORT.md`, canonical `RECEIPT.json`, focused pytest log, patch/scope audit, and exact head SHA. No HANDOFF unless the exact-head proof is green.
