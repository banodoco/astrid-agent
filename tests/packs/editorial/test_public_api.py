from astrid.packs.editorial import api


def test_cut_timeline_api_exposes_supported_builders_and_helpers() -> None:
    names = {
        "build_multitrack_timeline",
        "build_metadata_from_arrangement",
        "arrangement_edl_rows",
        "write_edl",
        "LEGACY_DEFAULT_CLIP_SEC",
        "BRANDED_EFFECT_IDS",
        "TEXT_STYLE_FONT",
        "theme_default_clip_sec",
        "clip_bounds_for_duration",
        "text_style_preset_to_attrs",
        "drop_brand_animation_overrides",
        "merged_theme_for_output",
        "joined_segment_text",
        "pool_entry_caption_kind",
        "tool_fingerprints",
        "ref_path_for_metadata",
        "register_cut_outputs",
    }

    assert names <= set(api.cut_timeline.__all__)
    assert callable(api.notes_overlap_ratio)
    assert callable(api.plan_next_action)
