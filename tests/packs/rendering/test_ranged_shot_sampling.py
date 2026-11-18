"""Sampling must choose frames that exist in a ranged render."""

from astrid.packs.rendering.actions.timeline_visualize.filmstrip_cards import plan_filmstrip
from astrid.packs.rendering.actions.timeline_visualize.filmstrip_options import filmstrip_options


def _ranged_snapshot(*, clips, occurrences):
    return {
        "project_slug": "demo",
        "timeline_id": "main",
        "timeline_name": "Film",
        "render_run_id": "ranged-run",
        "video_digest": "sha256:abc",
        "fps_rational": [24, 1],
        # RenderClock uses absolute timeline frame bounds for ranged media.
        "duration_frames": 240,
        "metadata": {
            "timeline_range": {
                "start_frame": 144,
                "end_frame": 240,
                "fps_rational": [24, 1],
            },
        },
        "tracks": [{"id": "plate", "kind": "visual"}],
        "clips": clips,
        "occurrences": occurrences,
        "scripts": [],
    }


def _one_shot_snapshot():
    return _ranged_snapshot(
        clips=[{
            "id": "plate-a",
            "track": "plate",
            "kind": "visual",
            "at": 0,
            "duration": 10,
            "occurrence_id": "occ-a",
            "shot_id": "shot-a",
        }],
        occurrences=[{
            "occurrence_id": "occ-a",
            "shot_id": "shot-a",
            "shot_name": "A",
            "start": 0,
            "end": 10,
            "start_frame": 0,
            "end_frame": 240,
        }],
    )


def test_shot_sampling_inside_one_shot_uses_a_rendered_frame():
    cards = plan_filmstrip(_one_shot_snapshot(), {"sample": "shots"})["cards"]

    assert len(cards) == 1
    assert 144 <= cards[0]["frame"] < 240
    assert "shot_midpoint" in cards[0]["sample_reasons"]
    assert cards[0]["clips"][0]["occurrence_id"] == "occ-a"


def test_default_contact_sampling_inside_one_cut_uses_a_rendered_frame():
    cards = plan_filmstrip(
        _one_shot_snapshot(), filmstrip_options({"view": "contact"})
    )["cards"]

    assert len(cards) == 1
    assert 144 <= cards[0]["frame"] < 240
    assert cards[0]["sample_reasons"] == ["cut_tile"]
    assert cards[0]["clips"][0]["occurrence_id"] == "occ-a"


def test_sampling_keeps_the_first_overlapping_shot_in_a_multi_shot_range():
    snapshot = _ranged_snapshot(
        clips=[
            {
                "id": "plate-a",
                "track": "plate",
                "kind": "visual",
                "at": 0,
                "duration": 8,
                "occurrence_id": "occ-a",
                "shot_id": "shot-a",
            },
            {
                "id": "plate-b",
                "track": "plate",
                "kind": "visual",
                "at": 8,
                "duration": 2,
                "occurrence_id": "occ-b",
                "shot_id": "shot-b",
            },
        ],
        occurrences=[
            {
                "occurrence_id": "occ-a",
                "shot_id": "shot-a",
                "shot_name": "A",
                "start": 0,
                "end": 8,
                "start_frame": 0,
                "end_frame": 192,
            },
            {
                "occurrence_id": "occ-b",
                "shot_id": "shot-b",
                "shot_name": "B",
                "start": 8,
                "end": 10,
                "start_frame": 192,
                "end_frame": 240,
            },
        ],
    )

    cards = plan_filmstrip(snapshot, filmstrip_options({"view": "contact"}))["cards"]

    assert [card["clips"][0]["occurrence_id"] for card in cards] == ["occ-a", "occ-b"]
    assert all(144 <= card["frame"] < 240 for card in cards)
