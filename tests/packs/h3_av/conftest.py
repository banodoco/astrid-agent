"""Narrow doubles for synthetic opaque source fixtures in structural tests."""

from __future__ import annotations

import pytest


_OPAQUE_SOURCE_STRUCTURAL_TESTS = {
    ("test_continuation_timing.py", "test_compiler_exports_raw_h3_duration_not_requested_suffix_duration"),
    ("test_compile.py", "test_compile_freezes_bundle_and_is_deterministic"),
    ("test_compile.py", "test_compile_binds_arbitrary_prompt_in_the_native_graph"),
    ("test_compile.py", "test_compilation_manifest_round_trips"),
    ("test_managed_asset_staging.py", "test_compiler_archive_stages_to_comfy_input"),
    ("test_h3dur_exact_time.py", "test_h3dur_exact_windows_validate_persist_and_compile"),
    ("test_h3dur_exact_time.py", "test_h3dur_plan_persists_exact_spec_as_json"),
    ("test_compile_generalized.py", "test_audio_regeneration_fails_closed_without_legacy_edit_dispatch"),
    ("test_compile_generalized.py", "test_imported_v1_regional_change_binds_native_red_mask"),
    ("test_compile_generalized.py", "test_native_masked_edit_source_binding_matches_worker_contract"),
    ("test_compile_generalized.py", "test_native_continuation_binds_zero_one_or_two_actual_reference_slots"),
    ("test_compile_generalized.py", "test_unsupported_capabilities_fail_before_graph_submission"),
    ("test_compile_generalized.py", "test_lanpaint_bindings_preserve_prompt_duration_masks_and_audio_intervals"),
    ("test_compile_generalized.py", "test_lanpaint_regional_change_requires_and_binds_real_mask_asset"),
    ("test_planner.py", "test_plan_records_canonical_spec_bindings_schedule_and_preserve_pass"),
    ("test_planner.py", "test_plan_static_image_mask_has_a_preview_and_runtime_binding"),
    ("test_planner.py", "test_box_region_is_previewed_and_bound_by_native_compilation"),
    ("test_planner.py", "test_plan_requested_raw_and_trim_timing"),
    ("test_planner.py", "test_plan_orders_multiple_modifying_passes_and_rejects_unresolved_assets"),
    ("test_planner.py", "test_plan_reports_ignored_v1_constant_overrides_and_keeps_supported_controls"),
    ("test_planner.py", "test_v2_creative_prompt_contains_only_the_author_prompt"),
    ("test_planner.py", "test_v2_ignored_constant_settings_and_disabled_turbo_strength"),
    ("test_planner.py", "test_plan_binds_per_edit_reference_selection"),
    ("test_planner.py", "test_plan_rejects_a_preserve_only_spec"),
    ("test_edit_binding.py", "test_imported_v1_and_v2_use_the_same_native_lowering"),
    ("test_edit_binding.py", "test_static_mask_is_the_canonical_b2_raster"),
    ("test_edit_binding.py", "test_partial_interval_mask_is_black_outside_and_repeatable"),
    ("test_edit_binding.py", "test_per_operation_strength_seed_and_selected_reference_order"),
    ("test_edit_binding.py", "test_unsupported_edits_fail_before_any_legacy_route"),
    ("test_edit_binding.py", "test_per_operation_controls_fail_closed_on_other_native_routes"),
    ("test_runtime_contract.py", "test_interval_lists_cannot_certify_a_copied_non_media_candidate"),
    ("test_h3_av_slice.py", "test_prepare_resolves_and_hashes_declared_assets"),
    ("test_h3_av_slice.py", "test_partial_preservation_requires_complete_permission_evidence"),
    ("test_native_v2_contract.py", "test_native_v2_schema_profile_and_supported_branch_bindings"),
    ("test_character_swap.py", "test_silent_timeline_source_does_not_claim_an_audio_baseline"),
}


@pytest.fixture(autouse=True)
def _double_source_probe_only_for_opaque_structural_fixtures(request, monkeypatch):
    key = (request.path.name, request.node.originalname)
    if key not in _OPAQUE_SOURCE_STRUCTURAL_TESTS:
        return
    from astrid.packs.h3_av.shared import prepare

    monkeypatch.setattr(prepare, "_check_source_av_rate", lambda _asset_id, _path: None)
