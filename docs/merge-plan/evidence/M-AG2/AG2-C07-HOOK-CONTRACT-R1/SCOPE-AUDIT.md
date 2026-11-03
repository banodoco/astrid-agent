# Scope audit — AG2-C07-HOOK-CONTRACT-R1

Base `80fb5c1eaa20ce5d3d9117d880d2074068c8e1fd`; committed and pushed head `63016d096441aa33d6fc1905ecaf969b7a5f9931`. `git diff --check 80fb5c1eaa20ce5d3d9117d880d2074068c8e1fd HEAD` exited 0, `git status --porcelain=v1` was empty, and `git diff --name-status 80fb5c1eaa20ce5d3d9117d880d2074068c8e1fd HEAD` reports exactly:

```
M	tests/core/execution/test_generic_host_actions_v3.py
```

The patch is [patch.diff](patch.diff), SHA-256 `08c634e33a7dd415744187878c782b7bcf53965e8e1f61eee64b79e53230d798`; the changed test file SHA-256 is `0ad8db3527f1d874e15ee30129b8b014b8984355b83741bbee805c901ef32075`. No product code, bridge contract, pins, assembly, workflows, or other test files changed. The host end-to-end success and finally assertions remain present; its fixture now supplies `launch_generation` and `runtime_epoch`. The drift matrix now targets actual `SessionBinding` fields and hook observation, replacing legacy backend-internal field expectations; model/template/invocation are not fields of that binding. The concurrency case blocks the real hook release and asserts its typed refusal, serialized admission rejection, fenced custody and shutdown uncertainty. Unmodified control-fault cases remain limited because this hook refuses release before HTTP controls, so their configured transport faults are not necessarily reached.
