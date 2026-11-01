from copy import deepcopy
from types import SimpleNamespace

import pytest

from astrid.sdk.timeline._common import TimelineEditError
from astrid.sdk.timeline.edits import _EditMixin


def checkout(rows, *, parent_clips=None):
    return SimpleNamespace(
        bundle={
            "placements": deepcopy(rows),
            "parent": {"clips": deepcopy(parent_clips or [])},
        },
        _mcache=None,
        clips=lambda: (),
    )


def row(occurrence, shot, start, duration=1000, **fields):
    return {
        "occurrence_id": occurrence,
        "shot_id": shot,
        "placement": {"start_ms": start, "track": "V1"},
        "duration_ms": duration,
        **fields,
    }


def test_arrange_uses_exact_occurrences_and_preserves_shared_shot_aliases():
    original = [
        row("occ-a", "shared-shot", 0, source_offset=0),
        row("occ-b", "shared-shot", 1000, source_offset=500),
        row("occ-spare", "spare-shot", 10000, marker="keep"),
    ]
    tl = checkout(original)

    _EditMixin.arrange(tl, ["occ-b", "occ-a"], at=2.25, gap=0.125)

    placements = tl.bundle["placements"]
    assert [r["occurrence_id"] for r in placements] == ["occ-b", "occ-a", "occ-spare"]
    assert [r["shot_id"] for r in placements] == ["shared-shot", "shared-shot", "spare-shot"]
    assert [r["placement"]["start_ms"] for r in placements] == [2250, 3375, 10000]
    assert [r["duration_ms"] for r in placements] == [1000, 1000, 1000]
    assert [r["source_offset"] for r in placements[:2]] == [500, 0]
    assert placements[2] == original[2]


def test_arrange_refuses_unknown_duration_without_mutation():
    original = [row("occ-a", "shot-a", 0, duration=None), row("occ-b", "shot-b", 1000)]
    tl = checkout(original)

    with pytest.raises(TimelineEditError, match="unknown duration"):
        _EditMixin.arrange(tl, ["occ-a"], at=3)

    assert tl.bundle["placements"] == original


def test_arrange_keeps_fractional_duration_and_exact_adjacency():
    tl = checkout([row("occ-a", "shot-a", 0, duration=1000.5), row("occ-b", "shot-b", 1000)])

    _EditMixin.arrange(tl, ["occ-a", "occ-b"], at=0, gap=0)

    assert [r["placement"]["start_ms"] for r in tl.bundle["placements"]] == [0, 1000.5]
    assert [r["duration_ms"] for r in tl.bundle["placements"]] == [1000.5, 1000]


def test_arrange_refuses_overlap_with_unselected_occurrence_without_mutation():
    original = [row("occ-a", "shot-a", 0), row("occ-b", "shot-b", 1000)]
    tl = checkout(original)

    with pytest.raises(TimelineEditError, match="unselected occurrence 'occ-b'"):
        _EditMixin.arrange(tl, ["occ-a"], at=1.5)

    assert tl.bundle["placements"] == original


def test_arrange_allows_touching_same_lane_boundary():
    tl = checkout([row("occ-a", "shot-a", 0), row("occ-b", "shot-b", 1000)])

    _EditMixin.arrange(tl, ["occ-a"], at=0)

    assert tl.bundle["placements"] == [row("occ-a", "shot-a", 0), row("occ-b", "shot-b", 1000)]


def test_arrange_allows_cross_lane_overlap_and_keeps_other_row_unchanged():
    original = [row("occ-a", "shot-a", 0), row("occ-b", "shot-b", 1000)]
    original[1]["placement"]["track"] = "V2"
    tl = checkout(original)

    _EditMixin.arrange(tl, ["occ-a"], at=1.5)

    assert tl.bundle["placements"][0]["placement"]["start_ms"] == 1500
    assert tl.bundle["placements"][1] == original[1]


def test_arrange_refuses_overlap_with_parent_clip_without_mutation():
    original = [row("occ-a", "shot-a", 0)]
    tl = checkout(original, parent_clips=[
        {"id": "parent-title", "track": "V1", "at": 2, "hold": 1},
    ])

    with pytest.raises(TimelineEditError, match="parent clip 'parent-title'"):
        _EditMixin.arrange(tl, ["occ-a"], at=2.25)

    assert tl.bundle["placements"] == original


def test_arrange_refuses_parent_clip_with_unknown_duration():
    original = [row("occ-a", "shot-a", 0)]
    tl = checkout(original, parent_clips=[{"id": "unknown", "track": "V1", "at": 20}])

    with pytest.raises(TimelineEditError, match="parent clip 'unknown'.*unknown timing"):
        _EditMixin.arrange(tl, ["occ-a"], at=1)

    assert tl.bundle["placements"] == original


@pytest.mark.parametrize(
    "ids,kwargs,reason",
    [
        (["occ-a", "occ-a"], {}, "unique"),
        (["missing"], {}, "unknown occurrence"),
        (["occ-a"], {"at": -1}, "nonnegative"),
        (["occ-a"], {"gap": float("nan")}, "finite"),
        (["occ-a"], {"at": 1e308}, "finite millisecond timing"),
    ],
)
def test_arrange_validates_ids_and_timing_before_mutation(ids, kwargs, reason):
    original = [row("occ-a", "shot-a", 0)]
    tl = checkout(original)

    with pytest.raises(TimelineEditError, match=reason):
        _EditMixin.arrange(tl, ids, **kwargs)

    assert tl.bundle["placements"] == original
