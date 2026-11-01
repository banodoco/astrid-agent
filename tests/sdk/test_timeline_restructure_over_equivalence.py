"""Whole-film acceptance for crossers kept intact on named over rows."""
from __future__ import annotations

import pytest

from astrid.sdk.timeline_restructure import assert_equivalent, describe, split
from tests.sdk.test_timeline_restructure import flat_film


@pytest.mark.parametrize(
    ("clip_id", "name", "effect", "source_fields"),
    [
        (
            "entrancefade-cross",
            "Entrance fade cross",
            ("entrance", {"type": "fade", "duration": 0.5}),
            {"hold": 1.0, "from": 0.0, "to": 1.0},
        ),
        (
            "exitfade-cross",
            "Exit fade cross",
            ("exit", {"type": "fade", "duration": 0.5}),
            {"hold": 1.0, "from": 0.0, "to": 1.0},
        ),
        (
            "fractional-source-cross",
            "Fractional source cross",
            None,
            {"from": 0.0, "to": 1.0, "speed": 1.5},
        ),
    ],
)
def test_uncuttable_crossers_preserve_the_whole_film_on_named_over_rows(
    clip_id, name, effect, source_fields
):
    flat = flat_film()
    clips = flat["shots"]["film"]["internal_timeline"]["clips"]
    clips[:] = [clip for clip in clips if clip["id"] != "cross"]

    source = {
        "id": clip_id,
        "name": name,
        "clipType": "media",
        "track": "sprite",
        "asset": "R",
        "at": 3.5,
        **source_fields,
    }
    if effect:
        source[effect[0]] = effect[1]
    clips.append(source)

    stored = split(flat)
    fallback = next(clip for clip in stored["parent"]["clips"] if clip["id"] == clip_id)
    report = "\n".join(describe(flat))

    assert source["track"] == "sprite"
    assert fallback["track"] == "picture-over"
    assert f"{clip_id} ({name}) [media] -> picture-over" in report
    assert not any(
        clip.get("app", {}).get("astrid", {}).get("timeline_restructure_piece", {}).get("id") == clip_id
        for shot in stored["shots"].values()
        for clip in shot["internal_timeline"]["clips"]
    )

    relevant_fields = {
        "id", "name", "clipType", "asset", "at", "hold", "from", "to", "speed",
        "entrance", "exit",
    }
    for field in relevant_fields:
        assert (field in fallback) == (field in source), field
        if field in source:
            assert fallback[field] == source[field], field

    # This compares the renderer's full projection, including layer stacking.
    assert_equivalent(flat, stored)
