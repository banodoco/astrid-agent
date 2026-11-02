# M2-06 H3 parity: test plan and task briefs (P1–P12)

Status: DRAFT for M-GPU review. Planning only. No code, no GPU, no provider, no pod.
Scope: rows M2-06a1, M2-06a2, M2-06a3, M2-06b1, M2-06b2, M2-06c and their gates. Row source: `docs/merge-plan/handoff/M-GPU/PACKS-TASKS.md` (M2 section) on `claude/epic-shannon-0o8n05`. Parity source: `H3-SIMPLIFY-ASTRA.md` and `PACKS-HANDOFF/briefs/M2.md` (H3 parity P1–P12).

## Gate before any dispatch
- G0 completeness review: BLOCKED. Partial CPU goldens are on machine 1 (`/home/user/astrid/orch/tasks/G0-FIXTURES`); the remainder is captured in the R8 GPU session. Nothing below dispatches until M-GPU records G0 accepted.
- K A5 + A6 accepted proof packet; K B1 + B2 accepted.
- M1-03 + M1-15 + M1-04 loading hand-off (host-loading packet).
- Each row dispatches the moment its own deps are satisfied, not in batches.

## Row map
| Row | Tier | Parity rows | Deps | Gate (PACKS-TASKS) |
|---|---|---|---|---|
| M2-06a1 | X | P3 | G0 completeness, K A5+A6, M1-03/15/04 | pre-acquisition + frozen-source checks match G0 |
| M2-06a2 | S | P4, P5 | M2-06a1 | media goldens equal |
| M2-06a3 | X | P6, P7 | M2-06a2 | receipt/manifest/delivery goldens equal; allowed-diff list |
| M2-06b1 | S | P8 | M2-06a2 | cancel tests |
| M2-06b2 | S | P9, P11 | M2-06a3 | no-replay + error tests |
| M2-06c | N | P1, P2 | M2-06a1, M2-13 | every G0 option works or is refused with guidance; previews acquire nothing |
| M2-15 | S | P10, P9 | G0, K A5+B1+B2+A6, custody packet, M2-06a1/a2/a3/b1/b2 | readiness/lease/cleanup on real host |
| M2-07 | X | P12 (and P1 removal) | M2-06a*/b*, M2-06c, M2-15, K A6 census | zero "h3" in core/sdk; shared-machinery tests green |

## P1–P12 → executable tests (planned names; Luna writes them)
Each test uses the real host/SDK/Runtime/action/compose/verify/settlement construction. Only network and GPU may be faked (R4). No fake `invoke_result` or Runtime for boundary proof.

| P | Contract | Planned tests (under `tests/packs/h3/`) | Golden / evidence |
|---|---|---|---|
| P1 | CLI/SDK entry and flags kept; removed placement controls reject with migration guidance | `test_cli_surface.py::test_h3_plan_run_compose_edit_flags`, `::test_removed_placement_controls_reject_with_guidance`; `test_sdk_entry.py::test_generate_video_entry_h3_transform`, `::test_invoke_result_shape` | G0 CLI/SDK option snapshot and rejection envelopes |
| P2 | CPU plan/previews kept; dry-run acquires no task/GPU/provider; static vs rich JSON versioned | `test_plan_previews.py::test_rich_plan_fields`, `::test_dry_run_acquires_nothing` (lease and submit must not be called), `::test_static_vs_rich_versioned` | G0 dry-run vs plan JSON |
| P3 | Frozen request/source/assets; exact member/hash/size; confined extraction; source AV/mask/model/branch errors before GPU; pre-acquisition checks before lease | `test_input_freeze.py::test_member_hash_size_and_confined_extraction`, `::test_source_av_errors_before_gpu`, `::test_mask_refusal_before_gpu`, `::test_pre_acquisition_checks_before_lease` | G0 frozen-input manifest; scratch-only intermediates |
| P4 | Exactly one muxed AV or video+audio pair; generated_audio binding; upload/poll/verified retrieval/provenance; audio-only remains a rejection | `test_engine_output.py::test_exactly_one_av_or_pair`, `::test_generated_audio_binding`, `::test_audio_only_rejected_with_diagnostic`, `::test_no_arbitrary_output_choice` | G0 fake-engine raw muxed/paired AV digests |
| P5 | Compose trim/splice/continuation/replacement/mux; verify coverage on actual bytes; protected video, audio ≥0.985, masks; fail closed on probe/tamper; fresh spool/MIME/copy rehash before final return | `test_compose_semantics.py` (raw-tail, continuation, splice, whole-source copy, full replacement, paired mux); `test_verify.py::test_probe_missing_fails_closed`, `::test_tamper_fails_closed`, `::test_audio_similarity_threshold`, `::test_final_rehash_before_return` | G0 deterministic final bytes, stream coverage, protected witnesses |
| P6 | media + final_receipt + unique universal manifest; generation/variant/association role/port/MIME/hash/size; `--out` all-or-nothing; no unmanaged intermediate publication | `test_delivery.py::test_manifest_role_port_mime_hash_size`, `::test_out_all_or_nothing`, `::test_no_intermediate_publication` | G0 delivery rows and `--out` paths; allowed-diff list |
| P7 | Versioned final-receipt.json; migrated status/state readers; request/graph/model/assets/bundle/result/task/attempt/endpoint/prompt/verification facts; truthful removed-witness mapping; editorial_approved is an assertion only | `test_receipt.py::test_versioned_final_receipt_fields`, `::test_status_readers_migrated`, `::test_editorial_approved_is_assertion_only`, `::test_no_fabricated_child_or_cas_witness` | G0 final-receipt.json; allowed-diff list (IDs, timing, cleanup links) |
| P8 | Task cancel reaches the accepted Comfy prompt: before/after acceptance, queued, running, race; queue-delete/scoped interrupt; teardown evidence; honest unconfirmed stop | `test_cancel.py::test_cancel_before_acceptance`, `::test_cancel_queued_prompt_deleted`, `::test_cancel_running_interrupt_scoped`, `::test_cancel_race`, `::test_unconfirmed_stop_reported` | G0 cancel transcript on the CPU fake engine |
| P9 | Same-task follow/reattach never resubmits; unknown ACK does not replay; host-crash and new-attempt behavior explicit and tested | `test_follow_reattach.py::test_same_task_follow_no_resubmit`, `::test_unknown_ack_no_replay`, `::test_host_crash_new_attempt_explicit` | G0 resubmission counter = 0 on follow |
| P10 | K owns qualification/lease/cleanup; LOCAL task with policy in action inputs; endpoint bound once before first engine call; receipt and lease-journal evidence; no Runtime binding write; consent/budget/deadline/warm/release/reconcile; zero local RunPod reads; old engine receipts valid | `test_lease_ownership.py::test_bind_once_before_first_engine_call`, `::test_no_runtime_binding_write`, `::test_zero_runpod_reads_local`, `::test_old_engine_receipts_valid_after_asset_move` | G0 readiness/lease evidence; K A6 receipt-hash compatibility |
| P11 | H3_SOURCE_AV_* and mask/plan field+fix/domain/SDK diagnostics; failed/cancel-requested/cancelled/unknown distinction; redaction; retired child-only codes enumerated | `test_errors.py::test_source_av_codes_kept`, `::test_failed_cancel_requested_cancelled_unknown_distinct`, `::test_lease_evidence_redacted`, `::test_retired_child_codes_enumerated` | G0 error envelope snapshot; retired-code list |
| P12 | Delete only H3's shared-child uses; validate import receipt, training/iteration/stream fan-out, editorial recoverable state and stitch stay and are tested on real paths | `test_shared_children.py::test_validate_import_receipt_still_used`, `::test_fan_out_iteration_training_stream_unchanged`, `::test_editorial_recoverable_state`, `::test_stitch_dependencies` | Caller census before deletion (M2-07) |

## Allowed losses (only these; anything else is a regression)
- Rejected legacy placement controls (with migration guidance).
- Documented loss of independent stage attestation.
- Already-blocked multi-edit loop (public admission rejects >1 modifying native edit before the loop).
- Intermediate child histories.
- Audio-only stays a diagnostic rejection (already unreachable; not a loss).

## Task briefs (one per row; dispatch only at its gate)
- **M2-06a1 (X, Terra)**: implement P3 frozen input snapshot, pre-acquisition checks and scratch-only intermediates. Acceptance: P3 tests green; pre-acquisition output matches G0.
- **M2-06a2 (S, Sol)**: one engine submission (P4) plus compose and verify witnesses (P5); failure leads to no publication. Acceptance: P4/P5 tests green; media goldens equal. Sol reviews failure cases in depth.
- **M2-06a3 (X, Terra)**: final-receipt.json, status readers, delivery rows, atomic `--out` (P6, P7); editorial_approved stays an assertion. Acceptance: delivery goldens equal; allowed-diff list only.
- **M2-06b1 (S, Sol)**: cancel to accepted prompt and honest unconfirmed stop (P8). Acceptance: all five cancel tests green; accepted-prompt evidence persisted for host cleanup.
- **M2-06b2 (S, Sol)**: follow/reattach without resubmission, unknown-ACK no-replay, domain error codes kept, child-only codes retired (P9, P11). Acceptance: no-replay and error tests green.
- **M2-06c (N, Luna)**: CLI/SDK surface (P1) and dry-run/previews (P2). If Luna finds the target unfixed, STOP and hand to Terra.

## Rules
- Focused tests only per implementer; the manager reruns the union once at each inter-manager hand-off SHA. No full suite per agent.
- Never weaken or skip a test. Never add a fake invoke_result, Runtime or child bridge for boundary proof.
- No GPU, provider, pod or RunPod call without separate go. No `~/.codex` or credential reads.
- Each worker: frozen scope, isolated worktree, patch and REPORT with base/tuple/commands/counts/NEW/unproven, incremental verified bundle, commit head `[skip ci]`, scratch cleaned before exit.
