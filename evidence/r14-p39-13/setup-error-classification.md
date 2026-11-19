# Setup-error classification — Lanes 44454822934

Read-only classification under Lead 7002163080. All 248 setup-error IDs are included exactly once; none executes its test body. No failure was fixed or hidden.

| Cluster | Cause | Shard1 | Shard2 | Shard3 | Shard4 | Owner |
| --- | --- | ---: | ---: | ---: | ---: | --- |
| S01 | Provider-managed edge replacement | 0 | 78 | 26 | 0 | unowned |
| S02 | Missing authentic G5 archive at session scratch path | 0 | 44 | 0 | 0 | unowned |
| S03 | Legacy word anchor absent | 0 | 21 | 14 | 0 | unowned |
| S04 | CaptureRuntime sees an empty occurrence list | 0 | 0 | 16 | 0 | unowned |
| S05 | Projection fixture sees empty shot items | 0 | 0 | 0 | 9 | unowned |
| S06 | Sealed Agent commit source hash differs | 0 | 7 | 0 | 0 | unowned |
| S07 | Historical text binding declaration/source refused | 0 | 5 | 0 | 0 | unowned |
| S08 | Native-sheet fixture sees empty clips | 0 | 4 | 0 | 0 | unowned |
| S09 | Missing authentic G5 archive at workstation path | 0 | 0 | 0 | 4 | unowned |
| S10 | Opaque-item patch previous value mismatch | 0 | 4 | 0 | 0 | unowned |
| S11 | Migrated realm not provider/baseline ready | 0 | 3 | 0 | 0 | RT1 |
| S12 | Fixed Runtime SHA witness differs | 0 | 3 | 0 | 0 | unowned |
| S13 | Shot item correspondence invalid | 0 | 2 | 0 | 0 | unowned |
| S14 | Historical short-lease Runtime commit unavailable | 0 | 0 | 2 | 0 | Plan55 LS-7.2 (via Plan39 AE2) |
| S15 | Provider dependency union omits captured read | 0 | 0 | 0 | 1 | unowned |
| S16 | Opaque transcript seed changes closure/provenance | 0 | 0 | 1 | 0 | unowned |
| S17 | Remotion host/worker validation returns no launch | 0 | 0 | 1 | 0 | unowned |
| S18 | Raw patch changes managed closure membership | 0 | 1 | 0 | 0 | unowned |
| S19 | Missing configured fixture | 0 | 0 | 1 | 0 | unowned |
| S20 | Missing pod_watchdog_transport fixture | 0 | 0 | 1 | 0 | unowned |

Counts: **0 / 172 / 62 / 14 = 248**. Explicit/shared ownership: RT1 3; Plan55 LS-7.2 via AE2 2. **Unowned: 243 IDs in18 clusters**, for Lead routing. AT9 and SF6d are not catch-all labels for unrelated setup failures.

Ownership is checked against Plan39 tasks at3a2b40ae53fe07972706ea3b7180d8cd57d3b522 and the sealed G2 cause inventory. Related G2 RT6.50/.55 signatures do not establish assignment of these newly visible Agent consumers.

## S01 — Provider-managed edge replacement

**Representative native ID** (shard2):

```text
tests/editing_conformance/test_commit_runtime_media_manifest_inverse.py::test_real_media_manifest_inverse_retry_restart_and_history[add]
```

**First causal / observable frame:** astrid/sdk/timeline/_doc_patch.py:372 — validate_submitted_change raises CommandError when whole-document replacement changes managed_edges.

**Owner:** unowned. Shared fixture calls _create_fixture at test_commit_runtime_occurrence_media.py:93; no exact G2/current Plan39 row assigns these integration IDs.

## S02 — Missing authentic G5 archive at session scratch path

**Representative native ID** (shard2):

```text
tests/editing_conformance/test_authenticated_legacy_restore.py::test_authenticated_source_baseline_restores_exact_legacy_bytes[inverse]
```

**First causal / observable frame:** tests/editing_conformance/test_current_pair_reader.py:68 — archive.read_bytes(); pathlib.py:1044 raises FileNotFoundError.

**Owner:** unowned. 44 consumers share reader(). AE2 provisions a historical Git commit, not this authenticated tarball; no archive bytes or hash assertion changed.

## S03 — Legacy word anchor absent

**Representative native ID** (shard2):

```text
tests/editing_conformance/test_commit_runtime_m1.py::test_cli_python_outbox_restart_and_guarded_inverse[disjoint-rebase-undo]
```

**First causal / observable frame:** astrid/sdk/timeline/native_word_anchor.py:172 — raises TimingCompileError("legacy word anchor is missing"); later service.py:3274 only republishes the failure.

**Owner:** unowned. Related G2 mechanism RT6.50 (Runtime consumers, R15 RT6 tail). The current Agent setup consumers have no explicit owner assignment; not silently assigned by matching message.

## S04 — CaptureRuntime sees an empty occurrence list

**Representative native ID** (shard3):

```text
tests/packs/rendering/test_live_source_label_native.py::test_runtime_capture_ref_reaches_existing_host_source[False]
```

**First causal / observable frame:** tests/sdk/test_capture_source_runtime.py:41 — occurrence = self.occurrences[0] raises IndexError.

**Owner:** unowned. 16 rendering/capture consumers assume a seeded occurrence after reading the Runtime parent; different from AT9 imported-source checks and SF6d missing verified-host helper.

## S05 — Projection fixture sees empty shot items

**Representative native ID** (shard4):

```text
tests/sdk/test_timeline_capture_projection.py::test_real_saved_projection_config_registry_and_full_pins_golden
```

**First causal / observable frame:** tests/sdk/test_timeline_capture_projection.py:316 — item = source_payload["items"][0] raises IndexError.

**Owner:** unowned. Nine consumers stop while decorating source fixture items; no cause-specific assigned Plan39 row.

## S06 — Sealed Agent commit source hash differs

**Representative native ID** (shard2):

```text
tests/editing_conformance/test_inverse_stage_runtime_regressions.py::test_known_whole_inverse_authenticates_forward_history_and_exact_redo[case-4-two-step-clips-before]
```

**First causal / observable frame:** tests/editing_conformance/test_inverse_stage_runtime_regressions.py:80 — _assert_source_identity compares commit.py hash f9acfbb1... with expected 2b81e46a....

**Owner:** unowned. Seven exact source-byte witnesses; AT9 concerns Runtime checkout/import identity for its named 179 IDs, not these Agent hash literals. No witness was refreshed.

## S07 — Historical text binding declaration/source refused

**Representative native ID** (shard2):

```text
tests/editing_conformance/test_commit_runtime_shared_inverse.py::test_shared_slot_move_inverse_redo_and_rematerialization[authored-first]
```

**First causal / observable frame:** astrid/sdk/timeline/validation.py:568 — _validate_element_reference calls _fail at :31 because historical text binding requires unchanged declaration and source.

**Owner:** unowned. Related G2 Runtime mechanism RT6.55 in the R15 tail; these five Agent setup IDs are newly exposed consumers, not an explicit current ownership assignment.

## S08 — Native-sheet fixture sees empty clips

**Representative native ID** (shard2):

```text
tests/editing_conformance/test_native_sheet_ordinary_runtime.py::test_http_trim_clear_params_original_basis_reload_undo_retry_or_kept_conflict[none-historical]
```

**First causal / observable frame:** tests/editing_conformance/test_native_sheet_ordinary_runtime.py:114 — clips[0] in the opaque-fields fixture check raises IndexError.

**Owner:** unowned. Four historical-shape setup cases; earlier checks and full trace retained. No stale-fixture versus product-acceptance judgment is claimed.

## S09 — Missing authentic G5 archive at workstation path

**Representative native ID** (shard4):

```text
tests/sdk/test_timeline_checkout_history_runtime.py::test_checkout_reload_undo_cli_redo_uses_retained_history
```

**First causal / observable frame:** tests/sdk/test_timeline_checkout_history_runtime.py:82 — film() raises AssertionError after AUTHENTIC_ARCHIVE.is_file() is false.

**Owner:** unowned. Four cases require /home/user/timeline-hub/hub/design/g5-sdk-authentic-capture-20261023-b6069d1d.tar.gz. Same archive basename as cluster2, distinct provisioning path and fixture.

## S10 — Opaque-item patch previous value mismatch

**Representative native ID** (shard2):

```text
tests/editing_conformance/test_commit_runtime_opaque_items.py::test_occurrence_move_preserves_unknown_item_ids_retry_restart_and_inverse[item_id]
```

**First causal / observable frame:** astrid/sdk/timeline/_doc_patch.py:400 — guard raises CommandError("patch previous value does not match").

**Owner:** unowned. Four setup commits fail guarded patch comparison before the intended test body; no matching assigned card.

## S11 — Migrated realm not provider/baseline ready

**Representative native ID** (shard2):

```text
tests/editing_conformance/test_commit_runtime_identity.py::test_occupied_shared_occurrence_destination_refuses_atomically[same-project]
```

**First causal / observable frame:** astrid-runtime/runtime_protocol/v33_lifecycle.py:300 — require_ready raises ConflictError while verification is pending.

**Owner:** RT1. Same explicit v33 readiness fence and shared mechanism named by Plan39 RT1. These three Agent consumers extend its observed surface; this is not a product fix or a waiver of provider_ready().

## S12 — Fixed Runtime SHA witness differs

**Representative native ID** (shard2):

```text
tests/editing_conformance/test_gate1_fresh_word_transcript_frontends.py::test_fresh_word_transcript_frontends_exact_pin_roundtrip[python]
```

**First causal / observable frame:** tests/editing_conformance/test_gate1_fresh_word_transcript_frontends.py:70 — actual Runtime2537fed2... is compared with fixed f103bb46....

**Owner:** unowned. Three pin-equality witnesses, not AT9 root/site-packages mismatches. No repin or witness edit is authorized or performed.

## S13 — Shot item correspondence invalid

**Representative native ID** (shard2):

```text
tests/editing_conformance/test_commit_runtime_opaque_items.py::test_occurrence_move_preserves_unknown_item_ids_retry_restart_and_inverse[id]
```

**First causal / observable frame:** astrid/sdk/timeline/_command_state.py:393 — _item_ids raises CommandError("shot item correspondence changed").

**Owner:** unowned. Two opaque-item setup variants fail the item identity guard; later service.py:3274 is only the public wrapper.

## S14 — Historical short-lease Runtime commit unavailable

**Representative native ID** (shard3):

```text
tests/integrations/test_generic_host_runtime_restart.py::test_real_host_renews_short_lease_during_cpu_execution
```

**First causal / observable frame:** tests/integrations/test_generic_host_runtime_restart.py:30 calls use_runtime(df3d4dd...); tests/helpers/runtime.py:84 emits the missing-commit refusal.

**Owner:** Plan55 LS-7.2 (via Plan39 AE2). Plan39 AE2 explicitly routes these two df3d4dd short-lease cases to LS-7.2. Raw error is message-only; frame is source-located. Fetching that commit previously leaked schema into later tests; no fetch/fixture repair attempted.

## S15 — Provider dependency union omits captured read

**Representative native ID** (shard4):

```text
tests/sdk/test_timeline_selected_schema_runtime.py::test_http_attached_selected_param_retains_original_pin_after_v2_removal_restart
```

**First causal / observable frame:** astrid/sdk/timeline/sync.py:162 — _http_response raises WorkspaceClientError from the Runtime refusal, then :1160 wraps it as CommitRefused.

**Owner:** unowned. First recorded causal response frame. Runtime timeline_commit_auth.py has matching guards at434 and462, but the HTTP trace does not identify which; no exact upstream frame is invented.

## S16 — Opaque transcript seed changes closure/provenance

**Representative native ID** (shard3):

```text
tests/editing_conformance/test_next_command_cohort_runtime.py::test_grouped_inverse_retains_surviving_opaque_transcript_pin[retained2-occurrence]
```

**First causal / observable frame:** tests/editing_conformance/native_word_anchor_fixture.py:332 — _retained_case observes seed child exit1; its stderr names emitter.DocumentPatchError: closure membership or provenance changed.

**Owner:** unowned. First preserved parent frame. Exact matching raise is source-located at astrid/sdk/timeline/emitter.py:267; pytest truncates the middle of child stderr, so that line is not claimed as an observed child stack frame.

## S17 — Remotion host/worker validation returns no launch

**Representative native ID** (shard3):

```text
tests/integrations/test_generic_host_remotion_render.py::test_generic_host_remotion_register_claim_execute_settle_and_cas
```

**First causal / observable frame:** tests/integrations/test_generic_host_remotion_render.py:249 — remotion_launch asserts launch is not None after _prepare_render_validation returns None.

**Owner:** unowned. First observable failed assertion; prerequisites passed. No lower causal trace is emitted, so no renderer-input, custody or AT1.x attribution is inferred.

## S18 — Raw patch changes managed closure membership

**Representative native ID** (shard2):

```text
tests/editing_conformance/test_native_checkout_current_equivalent.py::test_current_checkout_gate3_reload_undo_and_visualize_report_unavailable
```

**First causal / observable frame:** astrid/sdk/timeline/_doc_patch.py:366 — validate_submitted_change raises CommandError("closure membership and edges require semantic commands").

**Owner:** unowned. One current-checkout fixture uses a guarded raw patch before the journey body; separate from whole-replacement edge mismatch at372.

## S19 — Missing configured fixture

**Representative native ID** (shard3):

```text
tests/integrations/test_pinned_element_render_proof.py::test_pinned_element_render_proof_on_standin
```

**First causal / observable frame:** tests/integrations/test_pinned_element_render_proof.py:379 — pytest fixture resolution cannot supply configured.

**Owner:** unowned. One setup lookup failure; not a dependency collection error and not the SF6d verified-host helper.

## S20 — Missing pod_watchdog_transport fixture

**Representative native ID** (shard3):

```text
tests/integrations/test_runpod_host_endpoint_r5.py::test_runtime_claim_binds_one_endpoint_and_delivers
```

**First causal / observable frame:** tests/packs/runpod/test_target_selection.py:20 — case fixture requests absent pod_watchdog_transport, reached through retained→small_lease.

**Owner:** unowned. One setup lookup failure in test_runpod_host_endpoint_r5.py:261. AT1.4 is the lifecycle checkout environment and does not assign this fixture visibility issue.

Full per-cluster membership, exact representative traces and every diagnostic hash are in setup-error-classification.json. Original artifact JSONL/JUnit and logs retain every raw diagnostic.
