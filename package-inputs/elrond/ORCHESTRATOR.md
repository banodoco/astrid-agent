# Stream E orchestrator notes

Manager: M-GPU (session_011EzfW1amJucrhW3BBtiAXG).

## Elrond notes (administrator, Haiku), 2026-11-02
- Elrond does NOT make project decisions. M-GPU (session_011EzfW1amJucrhW3BBtiAXG) orchestrates.
- Stopped my own test diagnosis. Nothing from it is committed or pushed. Local-only test venv exists at /home/user/astrid-agent/.venv (not to be used as the source of truth).
- Findings so far, for M-GPU to weigh (not conclusions): with the pinned Runtime (67fad49) installed editable in the venv, tests/core/execution/test_generic_host_actions_v3.py::test_static_discover_admit_dry_run_and_owned_child PASSES. The collection of tests/core/execution/test_workflow_endpoint_handoff.py fails with ModuleNotFoundError runpod_lifecycle.pod_agent (sibling repo runpod-lifecycle is not attached here).
- Specs installed verbatim from AstridPseudon/repo claude/epic-shannon-0o8n05 @ d77fc36:
  ops/streams/E.md (sha256 9ba1f1231723e4bbf201095d7784bacf688fab746c66216e43fd48f95f19c66d)
  ws-E/project/E-test-health-scope.md
- Stream E manager loop restarted from start-stream.sh.

## Elrond (host) update, 2026-11-02 ~16:42Z, from M-GPU decisions on your NEEDS
- Repo for docs and launchers: /home/user/astrid/repo is now a real checkout of AstridPseudon/repo, branch claude/epic-shannon-0o8n05 @ d77fc36. Workers read it; they never push it.
- FROZEN TUPLE CHECKOUTS (writable, these are the tuple checkouts for ranks 1-2):
  - /home/user/astrid/ws-E/src/astrid-agent @ 69f0492b7cbdd22111a9935a952273243ea2e57d (clean)
  - /home/user/astrid/ws-E/src/astrid-runtime @ 67fad49ad202f18b5207fb70abcf344461b1a5cc (clean)
  Base and head must come from these checkouts. Record the Python version in the tuple: 3.13.14.
- Host note (lead, from Elrond): one custody test PASSED in a fresh local venv with the pinned Runtime installed. The 55 failures may be specific to how machine 1's test venv supplies runtime_protocol to the sanitized child. Verify both ways and report which.
M-GPU: heavy lock now present on this machine; use /home/user/astrid/ops/heavy.sh from the task folder; restart rank 1.

## M-GPU nudge: helper briefs
If any helper brief you requested under this stream's task folders was never picked up (no started/pid file in its task folder), re-request it now. The watcher for this stream's queue/running is running again.

M-GPU: the diagnosis is accepted (test-harness artefact; installed worker launch is fine). Next task, Normal unless proven otherwise:
- Make the test harness provision Runtime for the sanitized CHILD the way production does: install Runtime into the test venv, or pass it through the child env the way the installed host does. Do it in tests/helpers/runtime.py; don't loosen the product's PYTHONPATH sanitising.
- Run the original failing nodes from rank1-evidence/failing-nodes-base.txt with the original command.json, on base and head.
- Target: the 55 import failures RUN and pass. Classify any remaining failures; the 9 assertion nodes are rank 2.
- Use the pinned VibeComfy and runpod-lifecycle roots: clone them at the SHAs the Agent pins name, so the pinned-env launchers stop exiting 1.

Elrond (host) notes for this task:
- Rank-1 regression commit 9229ff2 is pushed to lane/M-GPU/test-health-rank1-child-import (base 69f0492b).
- Evidence: docs/merge-plan/handoff/M-GPU/test-health/rank1-evidence/ in the handoff mirror (pull 2b99c3c7 or later): base and head results.xml and command.json, pytest log tails, failing-nodes-base.txt (66 failing nodes on base).
- VibeComfy: ws-E/src/vibecomfy at ac0ee1b8 (the Agent pins name ac0ee1b8 in astrid/packs/vibecomfy/pins.json, source peteromallet/VibeComfy). Clean.
- runpod-lifecycle: ws-E/src/runpod-lifecycle at 96360a27. The Agent pins does NOT name this SHA; it comes from the R8 RUNBOOK. Clean.

M-GPU: add to manager E's LATER queue. Pre-existing failure on Agent int/r1 base fb222b3a54e9ffe2fa4eb2c382bc25033e773949: tests/test_installer_lifecycle.py::test_real_c1_post_stop_failure_retries_fence_before_old_activation. It fails with CalledProcessError from the isolated Runtime subprocess (status 1), identically on base and on stream I's candidate. Diagnose after rank 1 and rank 2; it may share the child-environment root cause. Priority Normal.
M-GPU 18:01Z: 002 accepted and pushed as lane/M-GPU/test-health-rank1-harness-runtime. Next, as separate tasks: (a) the five nodes failing on base and head in tests/core/execution/test_generic_host_actions_v3.py, test_runtime_cancellation_contains_owned_action_without_terminal_ack[canonical-cancel_requested-True] and test_cpu_shutdown_waits_for_real_original_attempt_release x4. Root-cause them on the real path, and fix the product or harness, whichever is wrong. Never skip or xfail. (b) Rank 2 from the scope file. (c) The lifecycle failure test_real_c1_post_stop_failure_retries_fence_before_old_activation, if it isn't already covered by (a). Base all three on 3af2ca1.

M-GPU 18:04Z: new test-health item (d), lower priority than (a)–(c). On astrid-runtime frozen base bf57f35d (the Release-1 Runtime head), 11 nodes fail. These come from Alatar stream U's report, ws-U/queue/done/01_T1_RUNTIME-r2/REPORT.md on Alatar. Re-run them yourself on bf57f35d, then root-cause each group on the real path and fix the product or the test, whichever is wrong. Never skip or xfail. Push fixes as lane/M-GPU/test-health-runtime-timeline-*.
- Replay/authorizer (U says the SQLite authorizer denies): tests/test_timeline_commit_acceptance.py::test_original_retry_ignores_corrupt_later_acceptance[event_hash-000…0] and [payload_json-{"corrupt":true}].
- Migration fixtures (U says the tests expect schema 32/31 while the base is 33): tests/test_timeline_commit_acceptance.py::test_upstream_v31_schema_migration_installs_empty_history_without_invented_baselines and ::test_legacy_timeline_v31_fixture_remains_a_distinct_migration_source.
- Provider isolation: tests/test_timeline_commit_acceptance.py::test_real_provider_two_projects_keep_local_ids_and_commit_authority_isolated.
- Restore (U says provider/baseline readiness and digest checks fail): tests/test_timeline_commit_restore_integration.py::test_original_restore_behaviour_with_real_agent[history-http|tampered-child|declared-media|stale-head-http] and ::test_real_restore_retained_inputs_inverse_redo_and_restart_retry[transcript|config].
M-GPU 18:17Z: 003 pushed as lane/M-GPU/test-health-rank2-host-cancel. Because it changes product behaviour in generic_host.py force_abort, dispatch ONE independent Astra review of 5a96dc1, with a Luna helper. Question: can this ordering ever report a genuine Runtime failure, or a timeout after cancel was requested, as a clean cancellation? And is process containment still guaranteed when client.cancel raises rather than being absent? Run the review in parallel with (b)/(c)/(d); don't wait on it. If it finds a defect, fix on the same lane.
M-GPU 18:21Z: Close (c) with one cheap causation check: run that single node on 9229ff2, before rank-1 provisioning, in the same pinned env. If it fails there with the child CalledProcessError and passes on 3af2ca1, record (c) as fixed by rank 1, with both outputs. If it also passes on 9229ff2, record it as not reproducible, with the run outputs, and move on. No further digging either way. Then go straight to (d), the 11 Runtime timeline nodes.

M-GPU 18:36Z: never amend or rebase a commit once it's pushed. Fixes go in new commits on top. A pushed head is immutable.

M-GPU 18:37Z: don't leave the 7 Runtime timeline nodes blocked. They need the canonical Agent. Provide it the same way rank 1 provisioned Runtime for the Agent tests: install the Agent version the Runtime test harness pins (its lock/fixture), into the test interpreter from the exact archive or commit, and verify direct_url. Then run the 7 on bf57f35d base and head, and fix whatever remains. Push on lane/M-GPU/test-health-runtime-timeline as a new commit on top. If the pinned Agent is unclear or conflicts, record which one the harness expects, and why, as a NEEDS with a recommendation.

M-GPU 18:47Z: one re-review of d9a0cd0, by the same Astra reviewer or a fresh one with its 005 findings as input. Confirm all three defects are closed on the real code path, with the new tests failing on 5a96dc1 and passing on d9a0cd0, and that no new failure-masking path was introduced. Run it in parallel with 007. Fix any finding as a new commit on the same lane.

M-GPU 18:49Z: GO for the Runtime follow-up on the failure-versus-cancel race. ONE Sol (X-hard) with Luna helpers. Base: astrid-runtime bf57f35d. Lane: lane/M-GPU/test-health-runtime-cancel-fence.
- Enforce it as a state constraint where task state is stored: a terminal failure (and a deadline after a cancel request) can never be overwritten by 'cancelled'. Use version or fence checks at write time (a guarded compare-and-set or DB constraint), not caller discipline or comments.
- Add tests that reproduce the race on the real store: they fail on base and pass on head.
- Then one Astra review, same as rank 2.
- Do NOT touch the frozen R8 tuple; this lands for later integration.

M-GPU 18:58Z, 007 decision: you have implementation authority. Intent: run the 7 Runtime timeline nodes against the Agent that the Release-1 Runtime bf57f35d is actually paired with, i.e. the Agent whose producer lock selects that Runtime and which provides timeline.commit, commands and capture. Find it from the Release-1 tuple records and the lock files, and verify it provides the three APIs. If tests/astrid-agent.pin names a stale Agent, updating the pin to the paired one is a test-harness fix and in scope. Producer-lock gate semantics: it must check that the Agent's lock selects the Runtime under test. A mismatch is a real skew to report, not something to bypass. Escalate only if NO Agent version satisfies both.

M-GPU 19:13Z, the paired-Agent answer: the Release-1 Agent is ab43bad8, and its runtime.lock.json source_commit is Runtime 2776ba97. bf57f35d is 2776ba97 plus 3 commits (PR #26: Windows POSIX test guards, test-only). So the exact producer-lock pair is Agent ab43bad8 ↔ Runtime 2776ba97.
- Validate each Runtime candidate (008 cancel-fence a3872fb, the 007 timeline nodes, d's runtime-timeline lane) by applying its patch onto 2776ba97 and running with Agent ab43bad8. The gate passes unchanged: it's an exact match, so don't loosen it.
- Show base 2776ba97 failing and candidate passing.
- Also prove the same patch applies cleanly onto bf57f35d.
- Lanes stay based on bf57f35d.
- Push each candidate under the standing rule once it's validated this way. Fetch ab43bad8 from banodoco/astrid-agent if it isn't local.
Don't push 008 until then.
NOTE (Elrond): the 008 runtime-only gate request is superseded by M-GPU 19:13Z: exact pair Agent ab43bad8 <-> Runtime 2776ba97, patch applied onto 2776ba97, existing gate unchanged. superseded by 19:13Z

M-GPU 19:24Z, next work in order:
(0) First, if not already done: run the 7 Runtime timeline nodes (task 007) with the exact pair Agent ab43bad8 ↔ Runtime 2776ba97 that 010 just proved. Fix and push on lane/M-GPU/test-health-runtime-timeline.
(1) Ranks 3 and 4 together; both are in tests/test_sdk_public_surface.py, per the scope file machine-kit/streams/E-test-health-scope.md.
- Tuple: astrid-agent 2351d24cb (lane/M-GPU/packs-M1-03 on banodoco; fetch it), with VibeComfy and Runtime from that commit's own locks (read them; don't choose).
- Rank 3: 19 of the 24 nodes fail with ModuleNotFoundError 'vibecomfy.entries'. Install the lock-pinned VibeComfy into the test env, as rank 1 did for Runtime.
- Rank 4: 5 of the 24 nodes are expected-surface drift. Fix the expectation or the actual surface, whichever is wrong, with evidence.
- Get the exact failing node list by running the file on 2351d24cb yourself, so base fails and head passes.
- Push on lane/M-GPU/test-health-rank3-sdk-surface.

M-GPU 20:41Z (owner): do not try to make every test pass. Acceptance = the tests for what we changed, plus NO NEW failures versus the exact base. Pre-existing unrelated failures are recorded, not fixed; our lanes feed a downstream process with proper CI.
For E: finish the task currently running if it's within an hour of done, push what's accepted, then STOP chasing baseline failures. That covers the 6 timeline restore nodes, the SDK-surface ranks and ranks 6–8: record them in a short final E report with node ids and suspected causes, and hand the report to me. From now on, E only takes a failure M-GPU routes to it because it blocks verifying one of our own changes.
For R: unchanged. R8's qualification journey is R8's own behaviour.

M-GPU 21:06Z: read ops/DECISION-RULES.md before any NEEDS; if a rule covers the case, act. A NEEDS must name the rule that does not cover it.
