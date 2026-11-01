"""Pure proposal tests for the SDK scene-warning summary projection."""

from types import SimpleNamespace

from astrid.core.timeline.onscreen import Span
from astrid.sdk.timeline_checkout import Checkout
from tests.sdk.test_coverage_empty_cut import _empty_cut_bundle


def span(start, end, cls, content=None, clips=(), findings=()):
    return SimpleNamespace(
        start_frame=start, end_frame=end, cls=cls, content=content, clips=clips, findings=findings
    )


def test_scene_warning_summary_collapses_adjacent_dip_rows_but_keeps_detail_findings(monkeypatch):
    checkout = Checkout(_empty_cut_bundle())
    spans = [
        span(0, 1, "DIP", "CONTENT", ("fade-a",)),
        span(1, 15, "DIP", "CONTENT", ("fade-b",)),
        span(15, 16, "UNKNOWN", "UNKNOWN", ("unknown-a",), ("first finding",)),
        span(16, 17, "UNKNOWN", "UNKNOWN", ("unknown-b",), ("second finding",)),
    ]
    seen = {}

    def capture(name, result):
        def formatter(rows):
            seen[name] = rows
            return result

        return formatter

    monkeypatch.setattr(checkout, "empty_cuts", capture("empty", ([], [])))
    monkeypatch.setattr(checkout, "picture_gaps", capture("gaps", []))

    problems = checkout._scene_problems(spans)

    for rows in seen.values():
        assert [(row.start_frame, row.end_frame, row.cls, row.content) for row in rows] == [
            (0, 15, "DIP", "CONTENT"),
            (15, 17, "UNKNOWN", "UNKNOWN"),
        ]
    assert problems == [
        "DIP     c01 0.00–0.50 s (0.50 s): a declared fade/dip (not a gap)",
        "scene   UNKNOWN 0.500–0.533 s: first finding",
        "scene   UNKNOWN 0.533–0.567 s: second finding",
    ]
    assert spans[0].end_frame == 1 and spans[1].end_frame == 15
    assert spans[2].clips == ("unknown-a",) and spans[3].clips == ("unknown-b",)


def test_scene_warning_summary_keeps_gap_and_covered_empty_distinct_and_never_bridges_sparse_rows(
    monkeypatch,
):
    checkout = Checkout(_empty_cut_bundle())
    spans = [
        span(0, 1, "GAP", "CONTENT", ("gap-a",)),
        span(1, 2, "COVERED", "EMPTY", ("empty-a",)),
        span(3, 4, "GAP", "CONTENT", ("gap-b",)),
        span(4, 5, "GAP", "CONTENT", ("gap-c",)),
    ]
    seen = []

    def capture_empty(rows):
        seen.append(rows)
        return ([], [])

    def capture_list(rows):
        seen.append(rows)
        return []

    monkeypatch.setattr(checkout, "empty_cuts", capture_empty)
    monkeypatch.setattr(checkout, "picture_gaps", capture_list)
    monkeypatch.setattr(checkout, "dips", capture_list)

    checkout._scene_problems(spans)

    for rows in seen:
        assert [(row.start_frame, row.end_frame, row.cls, row.content) for row in rows] == [
            (0, 1, "GAP", "CONTENT"),
            (1, 2, "COVERED", "EMPTY"),
            (3, 5, "GAP", "CONTENT"),
        ]


def test_scene_warning_summary_replaces_frozen_onscreen_spans_without_mutating_input():
    spans = [
        Span(0, 1, "DIP", "CONTENT", ("fade-a",)),
        Span(1, 15, "DIP", "CONTENT", ("fade-b",)),
    ]

    summary = Checkout._scene_warning_spans(spans)

    assert summary == [Span(0, 15, "DIP", "CONTENT", ("fade-a",))]
    assert spans == [
        Span(0, 1, "DIP", "CONTENT", ("fade-a",)),
        Span(1, 15, "DIP", "CONTENT", ("fade-b",)),
    ]
