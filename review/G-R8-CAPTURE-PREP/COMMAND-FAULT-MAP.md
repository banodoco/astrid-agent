# R8 G0 command and fault map — 053 / 2026-11-02

**Preparation, not capture. All P1–P12 are UNCAPTURED.** This is an operation-level map, not an executable runbook: no source supplies a complete compatible R8 per-row executable invocation, approved per-scenario fixture, and fault hook. A planned test name is not a collected test or a command. Each fault below is a future test procedure and expected observation, not something injected or observed here. The exact control/API and its ordering must be supplied and reviewed before execution; do not substitute a guessed command or fake a real boundary. CPU-prep means documentation/fixture and launch review only. Actual capture uses the separately gated compatible installed boundary; where CPU-capable capture is eventually proved, it still needs its own authorization. A later R8 session is separately gated and this packet authorizes no run.

Target Agent `1bc0f7d43bf03e3c8442676e47cecff308053acc`, lock-only alignment to Runtime `aa1c91d9675296a2411e583bbd0330ddcc718044`, VibeComfy `ac0ee1b80991f2414dae27d264af4b5e2890a7a6`; record resulting aligned Agent commit/tree, both before/after lock hashes and installed provenance. The 04:51Z dispatch is the candidate authority. H launch-record and lock-alignment identify aligned Agent c5327cc208d21fd0f865fd28d16701bae9c1a9bc, tree 852c01bc81a68cc91e19355a080050a73eef528c; H later records installer exit 0, import identity and root-help smoke; its child driver failed before admission/claim because `Backend.verify()` was missing `realm_id`. Neither the smoke nor the failure is an H outcome or qualification.

## Reading the map

Sources: **T** `/home/user/astrid/proof-codex-gen/sources/M2-06-H3-TEST-PLAN.md`, §§Row map and P1–P12 → executable tests (draft planned names); **G** `/home/user/astrid/repo/docs/merge-plan/handoff/M-GPU/PACKS-HANDOFF/G0.md`, §§Frozen required fixture/profile/manifest identities, Exact command/node plan, Capture packet; **C** `/home/user/astrid/repo/docs/merge-plan/handoff/M-GPU/PACKS-HANDOFF/G0-COVERAGE.md`, the corresponding P row; **D** `/home/user/astrid/repo/docs/merge-plan/handoff/M-GPU/PACKS-HANDOFF/G0-COMPARATOR.md`, opening capture schema and required surfaces; **F** `/home/user/astrid/orch/tasks/G0-FIXTURES/REPORT.md`, corresponding P paragraph; **R** `/home/user/astrid/orch/tasks/G0-RUN/REPORT.md`, corresponding P paragraph. The handoff copies and census are also in `reference/`. G and C refer to H81→A537 historical source seams, not the R8 installed tree. R8 must be checked at its actual aligned installed tuple.

Expected files below are proposed collection destinations supplementing the 051 destinations in `capture-manifest.json`, unless an emitted name is explicitly identified. They do not exist merely because they are listed. Each case additionally needs exact argv/environment and exit/count, installed tuple and locks, fixture and tool byte hashes/sizes, source→case mapping, raw evidence inventory and observed absence where relevant; see manifest `evidence_record_per_case` and D. Scenario-specific capture IDs must be unique. Normalized aliases must be reviewed and bijective; do not drop relationships, roles or hashes.

**Shared fixture authority (053):** The 04:51Z directive approves exact fixture identities in G0-FIXTURES REPORT and 051 manifest, including historical `public_workspace.py` SHA256 `ce8a88a9e40965710bca31c9107bc3e61e0246b00137d63873e266bff4482d51` (reported missing). The separate NEW-IDENTITY workspace adapter is `f93d3129afd114a05c1542f468ed22db2f8ed6872b7deda9d703fe4a5aa1237c`; do not substitute one for the other. The report also identifies test-only T9 template `d29938b28ea11ac99b9f89b3787511d813484de70056f2776f3a47d0bffa464b`, historical source profile `d8c745aaec921c3e279abae4fbf9091bce2830fccaf2d8655364f08cbf6fb838` (paths need regeneration), and stopped-session synthetic CPU readiness `2c50fedc3c1af033d27712a0605ea5b100137980e81a195feec10d9f21cb63ee` (not reusable or GPU-qualified). Approval of identities does not supply bytes, prove compatibility or turn the template into readiness. No per-scenario baseline fixture is identified for P1–P12 in those authorities: each remains **A-only (no baseline)**; shared support identities are not scenario baselines. Other scenario inputs, run IDs, hooks and tool identities still need exact binding. The case-v2 digest-only inputs belong to its older shakedown tuple; the 053 directive does not add them to this fixture authority.

**Installed-r10c partial P1 observation:** H ran `/home/user/astrid/proof-codex/work/49/install/program/versions/c5327cc2-aa1c91d9-831068e1/py/bin/python -I -B -m astrid --help`, exit 0, with combined stdout/stderr in `logs/installed-cli-smoke.log`; `post-verification-results.json` records argv/exit and `run-installed-cli-smoke.sh.txt` records the inherited-and-scrubbed environment. Exact cwd was inherited and not recorded. Root help lists the H3 route, but did not execute H3 help or SDK cases. The H install may be cleaned up. This partial smoke is not a P1 capture; the complete row command remains null.

**Source-derived partial P1 help proposal:** H aligned Agent `c5327cc208d21fd0f865fd28d16701bae9c1a9bc` `pyproject.toml` maps the `astrid` console script to `astrid.console:main`, which dispatches `h3` to `astrid/packs/h3_av/cli.py`. Static source supports `<installed-astrid> h3 --help` and `<installed-astrid> h3 {plan,run,compose,edit} --help` as separate argv proposals. The placeholder is not a known executable, cwd or environment and none has been run; these are not complete P1 capture commands. Store stdout, stderr and exit separately, with an explicit clean HOME/environment and source/install identities; argparse help is expected to exit 0, subject to installed observation. The old `python -m astrid` is not proof of console dispatch in r10c.

**Case-v2 boundary:** P77 retains the seven-file inventory. Its `run.sh` uses individual `--input-video`, `--mask`, `--request` and `--asset-map` flags, while `run-public.sh` uses derived `--inputs-json`; its README calls the live entry and flags unverified. Its synthetic shim and older tuple cannot decide the current route. Neither script is an installed-r10c command here; do not select one by inference.

**Historical argv, not runnable R8 commands:** `reference/historical-case-commands.json` preserves the exact G0-RUN root/plan/run/compose/edit help, generate/reference plan and three refusal argv and exit codes; its old venv was removed. Its plan uses `A quiet cafe.`, duration `5`, `look.png`, and task-local asset map; it does not establish a per-scenario fixture binding or installed R8 interface. F §Commands identifies `fixtures/run-h3.sh`, `run-recovery.sh`, and `run-vibe-validate.sh` with historical environment and test nodes; those scripts' referenced clones/venv were removed. G §Exact command/node plan contains an exact future **partial H81** recovery argv and explicitly says it is blocked and manually settles synthetic worker bytes. No historical command proves complete R8 composition. The actual per-row R8 executable argv, cwd, installed interpreter/import origins and exact injection hook remain missing in every row. Do not build argv from the planned test names.

## P1 — public CLI and SDK

- **053 disposition:** A-only (no baseline); command `null`, confidence `unresolved`. Primary missing fact: Installed r10c console executable path and observed H3 help/SDK signature for the P1 surface matrix. See manifest for exact support fixture identities and section below for other input/control gaps.

- **Contract/tests:** flags and SDK entry retained; retired placement controls refuse with migration guidance. Planned `test_cli_surface.py::{test_h3_plan_run_compose_edit_flags,test_removed_placement_controls_reject_with_guidance}` and `test_sdk_entry.py::{test_generate_video_entry_h3_transform,test_invoke_result_shape}`. T P1, C P1 CLI and SDK rows.
- **Fixture:** approved request/spec and asset-map bytes, option-value matrix, selector/SDK input and exact installed parser/SDK identity missing. Historical help needs no request fixture; its refusal inputs do not establish a per-scenario R8 baseline.
- **Operation/phase:** CPU-prep inventories the census, every parser/help and SDK signature; later execute real `h3 plan/run/compose/edit`, `generate.video(entry=h3,mode=transform)` and `invoke_result` cases on a compatible boundary. No supported exact R8 argv: need installed CLI entrypoint/interpreter, valid case arguments and fixture paths, SDK call construction and option matrix. R capture-index `cli-help-*` is only historical help.
- **Raw:** `P1/<case-id>/argv.json`, `stdout.bin`, `stderr.bin`, `exit.json`, `sdk-result.json`, plus option/signature snapshot and refusal envelope (operator names); R index `cases/cli/{root,plan,run,compose,edit}.txt` are historical actual paths.
- **Fault:** pass one retired placement control to the relevant public parser; expect explicit rejection and approved migration guidance, with stderr/envelope and exit recorded. Exact retired flag/value, route, current message and injection argv need owner-confirmed installed parser/census. This tests one case; enumerate the remainder separately.
- **Order/state:** map options before P11; T M2-06c depends on M2-06a1 and M2-13. UNCAPTURED. Sources T P1, C P1, R P1, D schema.

## P2 — previews without acquisition

- **053 disposition:** A-only (no baseline); command `null`, confidence `unresolved`. Primary missing fact: Installed r10c preview entry/call with a bound scenario request and no-acquisition observation hook. See manifest for exact support fixture identities and section below for other input/control gaps.

- **Contract/tests:** rich plan and static dry-run are versioned and acquire nothing; planned `test_plan_previews.py::{test_rich_plan_fields,test_dry_run_acquires_nothing,test_static_vs_rich_versioned}`. T P2, C P2.
- **Fixture:** R's historical plan request is the prompt/duration/reference and image digest in shared notes; the exact R8 approved request, image/asset-map bytes and mode matrix are missing.
- **Operation/phase:** CPU-prep review request branches and no-acquisition witnesses; later execute rich plan and SDK dry-run, compare outputs and observe task/lease/provider submission boundaries. Exact R8 argv and SDK dry-run request/call, instrumentation and fixture locations missing. Historical exact argv in `reference/historical-case-commands.json` `plan-generate-reference` only; R index actual `cases/plan/stdout.json`.
- **Raw:** `P2/<case-id>/request.json`, `plan.json`, `dry-run.json`, `acquisition-audit.json`, `absence.json` (collection names), including version, diagnostics and observed zero acquisitions.
- **Fault:** arrange for the test-owned lease and submission call counters to fail the case if invoked during dry-run; run dry-run and retain zero-call audit and preview. Required missing fact: the compatible installed boundary's actual observation hooks and request. No fake Runtime or proof by missing log.
- **Order/state:** P1 inputs and P3 fixture review precede capture; T M2-06c depends on M2-06a1/M2-13. UNCAPTURED. Sources T P2, G Capture packet item 3, C P2, R P2.

## P3 — frozen inputs and pre-acquisition refusal

- **053 disposition:** A-only (no baseline); command `null`, confidence `unresolved`. Primary missing fact: Installed real-boundary preflight invocation and test-owned bundle tamper control for a named case. See manifest for exact support fixture identities and section below for other input/control gaps.

- **Contract/tests:** frozen request/assets, hashes/sizes, confined extraction; errors before lease; planned `test_input_freeze.py::{test_member_hash_size_and_confined_extraction,test_source_av_errors_before_gpu,test_mask_refusal_before_gpu,test_pre_acquisition_checks_before_lease}`. T P3, C P3.
- **Fixture:** approved R8 request, source AV/mask/model and bundle/member identities and bytes absent; H81 workspace/profile/T9/manifest gaps in shared notes. F request/bundle and refusal are partial and not admitted H3 custody.
- **Operation/phase:** CPU-prep specify digest/member/confined-path and pre-acquisition audit; later freeze and invoke the real task/action/host path with valid and invalid variants. Need exact R8 entrypoint, supported bundle builder and injection/control API, approved fixture identities.
- **Raw:** `P3/<case-id>/request.json`, `input-manifest.json`, `bundle-members.json`, `preflight.json`, `acquisition-audit.json`, `absence.json`; raw bundle and member bytes/digests as evidence. Names are collection destinations.
- **Fault:** modify a frozen bundle member after the manifest hash is calculated, retaining original manifest and altered member; submit through real preflight. Expect hash/size refusal before lease and explicit zero-acquisition witness. Missing: actual extraction/preflight entry, approved member identity and test-owned tamper hook; refusal code is not specified.
- **Order/state:** before P4/5, P6/7 and lifecycle; T M2-06a1 requires G0 completeness, K A5+A6 and M1-03/15/04. UNCAPTURED. Sources T P3, G Capture packet items 3–4, C P3, F P3.

## P4 — raw engine AV roles

- **053 disposition:** A-only (no baseline); command `null`, confidence `unresolved`. Primary missing fact: Installed real-boundary output-validation invocation and engine response control for a named role case. See manifest for exact support fixture identities and section below for other input/control gaps.

- **Contract/tests:** exactly one muxed AV or video+audio pair, generated_audio binding; audio-only and ambiguous roles refuse. Planned `test_engine_output.py::{test_exactly_one_av_or_pair,test_generated_audio_binding,test_audio_only_rejected_with_diagnostic,test_no_arbitrary_output_choice}`. T P4, C P4.
- **Fixture:** approved deterministic muxed AV and paired raw video/audio bytes, hashes, output role mappings, model/graph and tool identities missing; F has no admitted engine output.
- **Operation/phase:** CPU-prep document required raw outputs; separately gated capture executes real task/host/SDK/custody with only engine/network fake where authorized, retaining upload/poll/retrieval/provenance. Need installed exact executable/call, fake-engine interface, output fixture bytes and role identifiers.
- **Raw:** `P4/<case-id>/engine-request.json`, `engine-response.json`, `raw-muxed.*` or `raw-video.*` plus `raw-audio.*`, `roles.json`, `retrieval.json`, `provenance.json`, and refusal/absence evidence. Names selected for collection, extension determined from actual bytes.
- **Fault:** make the test engine return audio only, with no video or muxed AV; run output validation and expect diagnostic refusal with no arbitrary selection or final publication. Missing actual fake-engine response schema, fixture and injection hook; do not assert a specific diagnostic code.
- **Order/state:** P3 frozen inputs and P10 readiness/admission gate precede P4; T M2-06a2 follows a1. UNCAPTURED. Sources T P4, G Capture packet item 4, C P4, F P4.

## P5 — composition and verified media

- **053 disposition:** A-only (no baseline); command `null`, confidence `unresolved`. Primary missing fact: Installed compose/verify invocation and spool tamper control for a named media case. See manifest for exact support fixture identities and section below for other input/control gaps.

- **Contract/tests:** compose trim/splice/continuation/replacement/mux and verify real bytes, masks, protected audio ≥0.985; fail closed on probe/tamper; planned `test_compose_semantics.py` and `test_verify.py::{test_probe_missing_fails_closed,test_tamper_fails_closed,test_audio_similarity_threshold,test_final_rehash_before_return}`. T P5, C P5.
- **Fixture:** approved source, raw outputs, masks/moving masks, offsets and ffmpeg/probe identity/digests missing. F recovery media is synthetic worker settlement, not real H3 final media.
- **Operation/phase:** CPU-prep review coverage and decoded witness schema; gated capture performs real compose/verify with bytes from P4 and captures before/after publication observations. Need exact compatible compose/verify invocation, input mapping and tamper hook.
- **Raw:** `P5/<case-id>/source.*`, `raw.*`, `final.*`, `composition.json`, `probe.json`, `witnesses.json`, `publication-audit.json` and raw mask/coverage evidence; exact media filenames chosen at collection.
- **Fault:** alter final spool bytes after their initial digest and before final verification/return; expect rehash/tamper failure and explicit no-final-publication witness. Missing precise hook, spool ownership and verification invocation; never mutate shared source bytes.
- **Order/state:** after P3/P4; T M2-06a2 follows a1. UNCAPTURED. Sources T P5, G Capture packet item 5, C P5, F P5.

## P6 — managed delivery and atomic export

- **053 disposition:** A-only (no baseline); command `null`, confidence `unresolved`. Primary missing fact: Installed managed export invocation and second-file staging failure control. See manifest for exact support fixture identities and section below for other input/control gaps.

- **Contract/tests:** final media/receipt/unique manifest, generation/variant/association role/port/MIME/hash/size and atomic `--out`; planned `test_delivery.py::{test_manifest_role_port_mime_hash_size,test_out_all_or_nothing,test_no_intermediate_publication}`. T P6, C P6.
- **Fixture:** approved verified multi-file output with final receipt, role and export destination identities missing; one historical recovery raw readback is partial only.
- **Operation/phase:** CPU-prep document rows and export audit; gated capture executes managed delivery and `--out` on real final verified products. Need installed delivery/export entrypoint, second-file test-owned failure hook and approved media/receipt fixtures.
- **Raw:** `P6/<case-id>/manifest.json`, `delivery-rows.json`, `export-before.json`, `export-after.json`, `media.*`, `final-receipt.json`, `absence.json`; emitted `final-receipt.json` is named in T and G, other paths are collection choices.
- **Fault:** inject failure on export of the second file after the first is staged, then inspect destination and transaction state; expect no partial final export, with before/after evidence and failure envelope. Missing actual export staging hook and exact multi-file fixture/argv.
- **Order/state:** P5 verification before publication/export; T M2-06a3 follows a2. UNCAPTURED. Sources T P6, G Capture packet item 6, C P6, F P6.

## P7 — receipt and status readers

- **053 disposition:** A-only (no baseline); command `null`, confidence `unresolved`. Primary missing fact: Installed receipt reader invocation and supported-version catalogue for a named version-refusal case. See manifest for exact support fixture identities and section below for other input/control gaps.

- **Contract/tests:** versioned final receipt, migrated readers and truthful witnesses, editorial approval a caller assertion; planned `test_receipt.py::{test_versioned_final_receipt_fields,test_status_readers_migrated,test_editorial_approved_is_assertion_only,test_no_fabricated_child_or_cas_witness}`. T P7, C P7.
- **Fixture:** approved final verified action result and versioned receipt/status fixtures with request/graph/model/assets/task/attempt/endpoint/prompt IDs missing; F recovery receipt is historical and partial.
- **Operation/phase:** CPU-prep review receipt fields and exact leaf allowances; gated capture invokes status/readback after real delivery. Missing exact R8 status-reader invocation, fixture and allowed-diff decisions.
- **Raw:** `P7/<case-id>/final-receipt.json`, `status.json`, `readback.json`, `alias-map.json`, `allowed-diff-v1.json` with raw IDs retained; only `final-receipt.json` and allowed-diff schema have source-defined names.
- **Fault:** supply a receipt version unsupported by the reader in a test-owned copy; approved acceptance is an explicit unsupported-version refusal without fabricated child/CAS facts. Missing the supported-version catalogue, applicable reader contract, reader entrypoint and actual error envelope; no precise diagnostic is specified.
- **Order/state:** P5 verified result then P6 delivery/status; T M2-06a3 follows a2. UNCAPTURED. Sources T P7, G Capture packet item 6, C P7, D allowance rules.

## P8 — cancellation and prompt ownership

- **053 disposition:** A-only (no baseline); command `null`, confidence `unresolved`. Primary missing fact: Installed cancellation interface and controllable prompt/ACK race schedule. See manifest for exact support fixture identities and section below for other input/control gaps.

- **Contract/tests:** cancel before/after accepted prompt, queued/running/race, scoped interrupt and honest unconfirmed stop; planned `test_cancel.py::{test_cancel_before_acceptance,test_cancel_queued_prompt_deleted,test_cancel_running_interrupt_scoped,test_cancel_race,test_unconfirmed_stop_reported}`. T P8, C P8.
- **Fixture:** accepted task/attempt/prompt IDs and queued/running engine state in approved R8 fixture absent; historical G0 has no accepted prompt.
- **Operation/phase:** CPU-prep review cancellation state machine; gated capture performs real task cancel against accepted prompt and observes queue delete/scoped interrupt, races and teardown. Missing actual cancel/status API, fake-engine scheduler/ACK controls, injection hook and exact invocation.
- **Raw:** `P8/<case-id>/prompt.json`, `cancel-request.json`, `engine-transcript.json`, `status-timeline.json`, `teardown.json`, `absence.json` as collection paths.
- **Fault:** withhold the engine's stop confirmation after cancellation of a running accepted prompt; expect an unconfirmed/unknown stop, not a claimed cancellation, with scoped interrupt and raw status transcript. Missing stop-ACK suppression hook and exact status vocabulary.
- **Order/state:** after P3/P4 admitted prompt and P10 admission/custody; T M2-06b1 follows a2. UNCAPTURED. Sources T P8, G Capture packet item 7, C P8, F P8.

## P9 — follow, uncertain ACK and recovery

- **053 disposition:** A-only (no baseline); command `null`, confidence `unresolved`. Primary missing fact: Installed recovery interface and controlled response-loss or crash/restart schedule. See manifest for exact support fixture identities and section below for other input/control gaps.

- **Contract/tests:** same-task follow never resubmits; unknown ACK no replay; host crash/new attempt explicit; planned `test_follow_reattach.py::{test_same_task_follow_no_resubmit,test_unknown_ack_no_replay,test_host_crash_new_attempt_explicit}`. T P9, C P9.
- **Fixture:** approved R8 request, admitted task/attempt/engine prompt, submission counter and host recovery state missing. G names exact H81 partial recovery node; F's successful recovery manually settled synthetic worker bytes.
- **Operation/phase:** CPU-prep define durable identity/count assertions; gated capture follow/reattach real task and record attempt/terminal ownership. Exact R8 follow API, crash/restart controls, ACK injection boundary and invocation missing. H81 partial argv is in G §Exact command/node plan and cannot be repurposed as R8 evidence.
- **Raw:** `P9/<case-id>/request.json`, `admission-response.json`, `engine-ack.json`, `task-timeline.json`, `attempts.json`, `submission-count.json`, `recovery-receipt.json` (collection names).
- **Fault:** drop the response after engine submission so acceptance is unknown; follow the same task and observe durable submission count. Expect no second submission, and honest unknown/reattachment state. Missing precise post-submit response-loss hook, counter and accepted API.
- **Order/state:** P3 frozen inputs, P4 admission and P10 custody; T M2-06b2 follows a3; M2-15 has additional K/custody dependencies. UNCAPTURED. Sources T P9, G Exact command/node plan and Capture packet item 7, C P9, F P9.

## P10 — qualification, custody and lease

- **053 disposition:** A-only (no baseline); command `null`, confidence `unresolved`. Primary missing fact: Exact installed invocation for the real H3 admission/claim and subsequent custody/lease case. See manifest for exact support fixture identities and section below for other input/control gaps.

- **Contract/tests:** K ownership, one endpoint binding before first engine call, no Runtime binding write, receipt/journal, consent/budget/deadline/warm/release/reconcile, local zero RunPod reads, old receipt compatibility; planned `test_lease_ownership.py::{test_bind_once_before_first_engine_call,test_no_runtime_binding_write,test_zero_runpod_reads_local,test_old_engine_receipts_valid_after_asset_move}`. T P10, C P10; single 1800-second warm default/precedence, holder-specific expiry and verified snapshot reuse are requirements supplied by the 052 brief, not historical capture.
- **Fixture:** Installed tuple identity and accepted K A5/A6/B1/B2/custody packet, compatible readiness/profile/manifest and holder/snapshot identities are not established by the launch record. Historical and NEW-IDENTITY workspace identities are separately approved as described above; neither proves R8 qualification.
- **Operation/phase:** CPU-prep inspect custody/lease evidence schema. Later verified-ready and real admission/claim gate P4; subsequent separately gated lifecycle captures bind/journal/release/reconcile. Need real installed qualification/claim argv, K accepted implementation/proofs, controlled lease expiry/clock hook and endpoint identity. Historical local operations cannot prove future K contract.
- **Raw:** `P10/<case-id>/tuple.json`, `qualification.json`, `claim.json`, `endpoint-binding.json`, `runpod-read-audit.json`, `lease-timeline.json`, `journal.json`, `custody-terminal.json` as collection paths; retain actual raw receipt and audit.
- **Fault:** expire one holder's lease in a test-owned controlled lifecycle while another holder remains valid; observe journal/reconciliation and verify no release of the other holder. Missing approved clock/lease control API and two-holder fixture. No provider call is authorized here.
- **Order/state:** readiness/admission precedes P4; full lease/cleanup cases may span later phases, avoiding a circular dependency. T M2-15 requires G0, K A5+B1+B2+A6, custody and a1/a2/a3/b1/b2. UNCAPTURED. Sources T P10/Row map, G Capture packet item 8, C P10, F P10; 052 brief structural invariants.

## P11 — error and lifecycle status envelopes

- **053 disposition:** A-only (no baseline); command `null`, confidence `unresolved`. Primary missing fact: Installed error/status entrypoint and exact input for each named refusal case. See manifest for exact support fixture identities and section below for other input/control gaps.

- **Contract/tests:** source AV/mask/plan field/fix/domain diagnostics, failed/cancel-requested/cancelled/unknown distinct, redaction and retired codes enumerated; planned `test_errors.py::{test_source_av_codes_kept,test_failed_cancel_requested_cancelled_unknown_distinct,test_lease_evidence_redacted,test_retired_child_codes_enumerated}`. T P11, C P11.
- **Fixture:** approved invalid request for each error family and status-driving cases, R8 error catalogue and redaction policy absent. R `refusal-run`, `refusal-compose`, `refusal-edit` are historical subset only; exact argv/exit in historical-case-commands.
- **Operation/phase:** CPU-prep map codes/fields; gated capture invokes each installed error/status path and retains envelopes. Need full installed error catalogue, request bytes, status reader and exact command/control for each variant.
- **Raw:** `P11/<case-id>/argv.json`, `request.json`, `stdout.bin`, `stderr.bin`, `error-envelope.json`, `status.json` and no-acquisition evidence where applicable (collection paths); R index has `cases/refusals/{run,compose,edit}.stderr`.
- **Fault:** present a request whose source AV is invalid at preflight, then inspect the public envelope and pre-acquisition audit; expect the applicable `H3_SOURCE_AV_*` diagnostic with correct domain/field/fix, no acquisition. Missing concrete malformed bytes, exact code/field/fix and R8 entrypoint; do not invent them.
- **Order/state:** P1 parser mapping, P3 requests and P8/P9 lifecycle statuses; T M2-06b2 follows a3. UNCAPTURED. Sources T P11, G Capture packet items 2–3 and 7, C P11, R P11.

## P12 — shared machinery controls

- **053 disposition:** A-only (no baseline); command `null`, confidence `unresolved`. Primary missing fact: Installed shared-child/stitch invocation and dependency-failure control for a named case. See manifest for exact support fixture identities and section below for other input/control gaps.

- **Contract/tests:** retain real validate import receipt, training/iteration/stream fan-out, editorial recovery and stitch; planned `test_shared_children.py::{test_validate_import_receipt_still_used,test_fan_out_iteration_training_stream_unchanged,test_editorial_recoverable_state,test_stitch_dependencies}`. T P12, C P12.
- **Fixture:** approved workflow, child/import receipt, fan-out, training/iteration/stream and editorial/stitch inputs and hashes, plus live target identity where needed, are missing. F actual `cases/vibe-validate/validate-consented-attempt/outputs/validation-report.json` is a historical structural-only validation, not a live import-receipt proof.
- **Operation/phase:** CPU-prep review shared-caller census; gated capture exercise actual validate import, fan-out, editorial recovery and stitch paths. Need exact installed operation schemas, runnable commands, workflow/receipt fixtures and child-dependency failure hook; F `run-vibe-validate.sh` is only a historical consent/structural-validation integration.
- **Raw:** `P12/<case-id>/workflow.json`, `validate.json`, `import-receipt.json`, `fanout.json`, `editorial-state.json`, `stitch.json`, `status.json`, dependency/refusal transcript (collection paths).
- **Fault:** fail a required stitch dependency before stitch is scheduled; approved acceptance is that stitch remains blocked or fails truthfully with retained dependency status and no fabricated success. Missing the exact dependency failure hook, scheduler contract and observable status names.
- **Order/state:** P3 shared-input custody and P10 installed target where applicable; T M2-07 requires a*/b*, c, M2-15 and K A6 census. UNCAPTURED. Sources T P12/Row map, G Capture packet item 9, C P12, F P12.
