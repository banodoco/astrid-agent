"""Contact and motion requests must honor explicit ranged-render addresses."""

import pytest

from astrid.packs.rendering.actions.timeline_visualize.filmstrip_cards import plan_filmstrip
from astrid.packs.rendering.actions.timeline_visualize.filmstrip_options import filmstrip_options
from astrid.sdk import invocation
from astrid.sdk.exceptions import CapabilityValidationError


def _snapshot():
    return {
        "project_slug": "demo",
        "timeline_id": "main",
        "timeline_name": "Film",
        "render_run_id": "ranged-run",
        "video_digest": "sha256:abc",
        "fps_rational": [30, 1],
        "duration_frames": 420,
        "metadata": {
            "timeline_range": {
                "start_frame": 180,
                "end_frame": 420,
                "fps_rational": [30, 1],
            },
        },
        "tracks": [{"id": "plate", "kind": "visual"}],
        "clips": [{
            "id": "plate-a",
            "track": "plate",
            "kind": "visual",
            "at": 0,
            "duration": 14,
            "occurrence_id": "occ-a",
            "shot_id": "shot-a",
        }],
        "occurrences": [{
            "occurrence_id": "occ-a",
            "shot_id": "shot-a",
            "shot_name": "A",
            "start": 0,
            "end": 14,
            "start_frame": 0,
            "end_frame": 420,
        }],
        "scripts": [],
    }


@pytest.mark.parametrize(
    "values",
    [
        {"view": "contact", "at": 3.0},
        {"view": "motion", "preset": "motion", "at": 5.8},
        {"view": "motion", "preset": "motion", "range": "5..7"},
    ],
    ids=["contact-before-render", "motion-at-before-render", "motion-range-crosses-start"],
)
def test_sdk_preflight_rejects_out_of_render_addresses_for_all_views(monkeypatch, values):
    monkeypatch.setattr(
        "astrid.sdk.timeline_filmstrip.prepare_filmstrip",
        lambda *_, **__: {"mode": "filmstrip", "filmstrip_snapshot": _snapshot()},
    )

    with pytest.raises(CapabilityValidationError, match="render that range, or ask for a time inside it"):
        invocation._validate_timeline_visualize_inputs(
            {"render_run": "run-1", **values}, project="demo"
        )


@pytest.mark.parametrize(
    "values",
    [
        {"view": "contact", "at": 3.0},
        {"view": "motion", "preset": "motion", "at": 5.8},
        {"view": "motion", "preset": "motion", "range": [5.0, 7.0]},
    ],
    ids=["contact-before-render", "motion-at-before-render", "motion-range-crosses-start"],
)
def test_planner_rejects_out_of_render_addresses_for_all_views(values):
    options = filmstrip_options(values)

    with pytest.raises(ValueError, match="render that range, or ask for a time inside it"):
        plan_filmstrip(_snapshot(), options)


@pytest.mark.parametrize(
    "values",
    [
        {"view": "contact", "at": 6.0},
        {"view": "motion", "preset": "motion", "at": 6.0},
    ],
    ids=["contact-at-render-start", "motion-at-render-start"],
)
def test_valid_address_at_render_start_remains_available_after_context_clipping(values):
    index = plan_filmstrip(_snapshot(), filmstrip_options(values))

    assert index["cards"]
    assert all(180 <= card["frame"] < 420 for card in index["cards"])


@pytest.mark.parametrize(
    "values",
    [
        {"view": "contact", "at": 6.0},
        {"view": "motion", "preset": "motion", "at": 6.0},
    ],
    ids=["contact-at-render-start", "motion-at-render-start"],
)
def test_sdk_preflight_keeps_valid_address_at_render_start(monkeypatch, values):
    monkeypatch.setattr(
        "astrid.sdk.timeline_filmstrip.prepare_filmstrip",
        lambda *_, **__: {"mode": "filmstrip", "filmstrip_snapshot": _snapshot()},
    )

    context = invocation._validate_timeline_visualize_inputs(
        {"render_run": "run-1", **values}, project="demo"
    )

    assert context["mode"] == "filmstrip"
