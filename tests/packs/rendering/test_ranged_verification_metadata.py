"""Ranged media needs an explicit policy in pixel verification and filmstrip metadata."""

from types import SimpleNamespace

import pytest

from astrid.packs.rendering.actions.timeline_visualize import fast_lane, sweep
from astrid.packs.rendering.actions.timeline_visualize.filmstrip_cards import plan_filmstrip


def _ranged_snapshot():
    return {
        "project_slug": "demo",
        "timeline_id": "main",
        "timeline_name": "Film",
        "render_run_id": "ranged-run",
        "video_digest": "sha256:abc",
        "fps_rational": [24, 1],
        "duration_frames": 336,
        "metadata": {
            "timeline_range": {
                "start_frame": 144,
                "end_frame": 336,
                "fps_rational": [24, 1],
            },
        },
        "tracks": [{"id": "plate", "kind": "visual"}],
        "clips": [{"id": "plate", "track": "plate", "kind": "visual", "at": 0, "duration": 14}],
        "scripts": [],
    }


def test_full_explicit_ranged_review_reports_complete_render_coverage():
    index = plan_filmstrip(_ranged_snapshot(), {"range": [6.0, 14.0], "every": 1.0})

    assert index["coverage"]["window_seconds"] == [6.0, 14.0]
    assert index["coverage"]["full_duration"] is True


def test_ranged_overview_counts_decoded_frames_and_excludes_pre_range_boundaries():
    index = plan_filmstrip(_ranged_snapshot(), {})

    assert index["boundary_index"]["rendered_frame_count"] == 192
    assert all(entry["frame"] >= 144 for entry in index["boundary_index"]["entries"])


class _Draft:
    bundle = {"base_parent": {"revision_id": "saved-revision"}}

    @staticmethod
    def document():
        return {"parent": {"config": {"theme_overrides": {"visual": {"canvas": {"width": 1920, "height": 1080, "fps": 24}}}}}}


def _ranged_client():
    snapshot = {
        "metadata": {"timeline_range": {"start_frame": 144, "end_frame": 336, "fps_rational": [24, 1]}},
        "config": {"theme_overrides": {"visual": {"canvas": {"width": 1920, "height": 1080, "fps": 24}}}},
    }
    run = {"run_id": "base-run", "status": "completed", "task_ids": ["base-task"]}
    task = {
        "task_id": "base-task",
        "run_id": "base-run",
        "state": "succeeded",
        "capability_id": "rendering.render",
        "spec": {"spec": {"inputs": {
            "review": False,
            "profile": None,
            "render_window": {"start_frame": 144, "end_frame": 336, "fps_rational": [24, 1]},
            "timeline_snapshot": snapshot,
        }}},
    }
    return SimpleNamespace(
        runs=SimpleNamespace(show=lambda _run_id: SimpleNamespace(ok=True, data=run)),
        tasks=SimpleNamespace(show=lambda _task_id: SimpleNamespace(ok=True, data=task)),
    )


@pytest.mark.parametrize("entrypoint", ["fast_lane", "sweep"])
def test_pixel_verification_refuses_ranged_base_before_rendering_candidate(monkeypatch, tmp_path, entrypoint):
    monkeypatch.setattr(
        "astrid.sdk.timeline_filmstrip.prepare_filmstrip",
        lambda *_, **__: {"render_run_id": "base-run"},
    )
    monkeypatch.setattr(
        "astrid.sdk.authoring_bundle.render_authoring_candidate_preview",
        lambda *_, **__: pytest.fail("ranged BASE must be refused before rendering WORKING"),
    )

    with pytest.raises(fast_lane.FastLaneError, match="ranged"):
        if entrypoint == "fast_lane":
            fast_lane.prepare_pair(_ranged_client(), "demo", "main", _Draft(), media_root=tmp_path)
        else:
            sweep.sweep(_ranged_client(), "demo", "main", draft=_Draft(), root=tmp_path)
