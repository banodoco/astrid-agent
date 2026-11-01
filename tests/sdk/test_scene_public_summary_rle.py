"""Public Scene-answer RLE summaries retain detailed answer rows."""

from copy import deepcopy

from astrid.core.timeline.onscreen import Span
from astrid.sdk import timeline_filmstrip
from astrid.sdk.timeline_checkout import Checkout
from tests.sdk.test_coverage_empty_cut import _empty_cut_bundle
from tests.sdk.test_scene_caller_preservation import _scene_context, _scene_result


def _public_spans(segments):
    """Build current public13-shaped answers, then use the active Scene carrier."""
    duration = max(end for _, end, *_ in segments)
    scene_clips = {}
    answers = []
    for start, end, coverage, content, finding in segments:
        for frame in range(start, end):
            clip_id = f"compiled-{frame}"
            scene_clips[clip_id] = {"address": f"c{frame:03d}.field"}
            answers.append(
                {
                    "frame": frame,
                    "coverage": coverage,
                    "content": content,
                    "layers": [] if coverage == "GAP" else [{"clipId": clip_id}],
                    "findings": [f"{finding}-{frame}"],
                }
            )
    context = deepcopy(_scene_context())
    snapshot = context["capture_snapshot"]
    snapshot["duration_frames"] = duration
    snapshot["metadata"]["scene_clips"] = scene_clips
    result, client = _scene_result(
        context,
        answers=answers,
        scene_clips=scene_clips,
    )
    return timeline_filmstrip._scene_only_spans(
        result,
        context,
        duration_frames=duration,
        client=client,
    )


def _summary_rows(monkeypatch, spans):
    checkout = Checkout(_empty_cut_bundle())
    seen = []

    def capture(rows):
        seen.append(rows)
        return []

    def capture_empty(rows):
        seen.append(rows)
        return ([], [])

    monkeypatch.setattr(checkout, "empty_cuts", capture_empty)
    monkeypatch.setattr(checkout, "picture_gaps", capture)
    monkeypatch.setattr(checkout, "dips", capture)
    checkout._scene_problems(spans)
    assert seen
    return seen[0]


def test_public_scene_summary_preserves_exact_coverage_boundaries_and_detail(monkeypatch):
    spans = _public_spans(
        [
            (0, 30, "COVERED", "CONTENT", "content"),
            (30, 45, "GAP", None, "gap"),
            (45, 90, "COVERED", "EMPTY", "empty"),
        ]
    )

    assert len(spans) == 90
    assert [(row.start_frame, row.end_frame, row.cls, row.content) for row in spans] == [
        (
            frame,
            frame + 1,
            "COVERED" if frame < 30 or frame >= 45 else "GAP",
            "CONTENT" if frame < 30 else "EMPTY" if frame >= 45 else None,
        )
        for frame in range(90)
    ]
    assert spans[0].clips == ("c000.field",) and spans[0].findings == ("content-0",)
    assert spans[30].clips == () and spans[30].findings == ("gap-30",)
    assert spans[45].clips == ("c045.field",) and spans[45].findings == ("empty-45",)
    assert [
        (row.start_frame, row.end_frame, row.cls, row.content)
        for row in _summary_rows(monkeypatch, spans)
    ] == [
        (0, 30, "COVERED", "CONTENT"),
        (30, 45, "GAP", None),
        (45, 90, "COVERED", "EMPTY"),
    ]


def test_public_one_sided_fade_summary_is_dip_then_covered_at_half_open_boundary(monkeypatch):
    spans = _public_spans(
        [
            (0, 15, "DIP", None, "fade"),
            (15, 60, "COVERED", "CONTENT", "covered"),
        ]
    )

    assert spans[14].end_frame == 15 and spans[15].start_frame == 15
    assert spans[14].findings == ("fade-14",) and spans[15].findings == ("covered-15",)
    assert [
        (row.start_frame, row.end_frame, row.cls, row.content)
        for row in _summary_rows(monkeypatch, spans)
    ] == [
        (0, 15, "DIP", None),
        (15, 60, "COVERED", "CONTENT"),
    ]


def test_public_300_clip_answers_merge_to_one_class_content_summary(monkeypatch):
    spans = _public_spans([(0, 300, "COVERED", "CONTENT", "clip")])

    assert len(spans) == 300
    assert [
        (row.start_frame, row.end_frame, row.cls, row.content)
        for row in _summary_rows(monkeypatch, spans)
    ] == [(0, 300, "COVERED", "CONTENT")]


def test_summary_does_not_bridge_sparse_public_span_rows_or_gap_and_empty():
    rows = [
        Span(0, 30, "COVERED", "CONTENT", ("content",)),
        Span(30, 45, "GAP", None, ()),
        Span(46, 90, "COVERED", "EMPTY", ("empty",)),
        Span(90, 91, "COVERED", "EMPTY", ("empty-2",)),
    ]

    summary = Checkout._scene_warning_spans(rows)

    assert [(row.start_frame, row.end_frame, row.cls, row.content) for row in summary] == [
        (0, 30, "COVERED", "CONTENT"),
        (30, 45, "GAP", None),
        (46, 91, "COVERED", "EMPTY"),
    ]
    assert rows[2].end_frame == 90
