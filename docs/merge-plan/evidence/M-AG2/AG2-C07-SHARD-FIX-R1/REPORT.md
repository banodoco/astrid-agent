# AG2-C07-SHARD-FIX-R1 — incomplete: focused acceptance failed

The two refs were pushed at `80fb5c1eaa20ce5d3d9117d880d2074068c8e1fd`, but this lane is **not accepted for integration**. The required full-module run returned **14 failed, 254 passed** (inner exit 1), so no M-AG integration handoff has been made and no active C-07 hosted shard is claimed green.

The test-only patch constructs one binding from the known-good profile before the shared factory call, passes that same binding to the factory and manager, and supplies bindings to both direct factory tests. It uses the verified source-content digest, residency identity, and engine birth evidence required by the published hook, and removes obsolete factory arguments. No production code was changed. The two direct factory groups passed all **7 cases**. The shared fixture passes factory verification and exposes further incompatibilities.

The full corrected-environment run was `python -m pytest -q -o addopts= tests/core/execution/test_generic_host_actions_v3.py` from `agent/` with task-local Python 3.13.18, pytest 9.0.2, Runtime `2776ba973b6c8f3555b5629b0840d9691a98d3c2`, and VibeComfy `e2905d068d5851b9c32856d32b8251852a01d6dd`. The precise exports, earlier attempts, and inner exits are in the receipt. No `--maxfail` or `-x` was used. Complete output: [pytest-editable.log](pytest-editable.log), SHA-256 `aab16fa2acd57e06f457b157823ba87b6c323bcff9db1e3a4f568a07c69abb4d`.

Remaining failures on the published base are visible after the missing-binding repair:

- The host-level SDK handoff fixture fails with `Comfy owner binding requires complete Runtime and engine birth evidence`.
- Eight binding-drift cases expect legacy backend fields or behavior. The factory now returns `ComfyHostHooks`, rather than the old `CheckoutServerAdapter` object.
- Five other handoff cases expect a successful attached release or control side effects; the published hook explicitly returns unverified/fail-closed cancellation and release. For example, the late-finally case receives `SessionCapacityError: managed session release was not verified`, and the model-restart cases observe no control calls.

These failures require owner-hook/test-contract reconciliation beyond the three stale invocations. Altering product behavior to satisfy the old success-path assertions would violate this task's fail-closed constraint. Other hosted shard failures remain separately classified.

The first full attempt on the updated commit reported 16 failed, 252 passed with non-editable dependency installs. The two additional import-isolation failures disappeared after switching Runtime and VibeComfy to the editable setup used by CI; all 12 cases in that import-isolation group then passed. The initial system-Python attempt could not start because pytest was absent. All attempts are recorded in the receipt.

Base `ab43bad818ef65ca1e929b973a2f61cd9fc4bf61` is an ancestor of head `80fb5c1eaa20ce5d3d9117d880d2074068c8e1fd` through `169d4dbc6d044ab37f2a4231c14c1d61d594c352`. Both `wip/M-AG2/AG2-C07-SHARD-FIX-R1` and `lane/M-AG2/AG2-C07-SHARD-FIX-R1` point to head. `git diff --check ab43bad818ef65ca1e929b973a2f61cd9fc4bf61 HEAD` exited 0; the changed-path audit lists only `tests/core/execution/test_generic_host_actions_v3.py`. The working tree is clean. [patch.diff](patch.diff) SHA-256 `17d47821dd18b0bead94a778e5f666e4b7bb8f6986f653f3a931bfa63ea4e1a5`; changed file SHA-256 `9cc297596373f74a0dda6492ea758c5842df207bfe1b297ca63f71cdc7512289`.

[RECEIPT.json](RECEIPT.json) SHA-256 `99ee3410af6e1dffa6f6a0779c8b2eae3f902385618e73bd0d33de010970ce85` contains the command and coverage inventory and all recorded output hashes.
