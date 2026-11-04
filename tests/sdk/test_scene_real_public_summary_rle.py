"""Real public13 Scene answers through the existing summary carrier."""

from copy import deepcopy

from astrid.core.rendering.scene_answers import host_frames_at
from astrid.sdk import timeline_filmstrip
from astrid.sdk.timeline_checkout import Checkout
from tests.packs.rendering.conftest import host_project, native_host  # noqa: F401,F811
from tests.packs.rendering.test_scene_host import BASIS, props
from tests.sdk.test_coverage_empty_cut import _empty_cut_bundle
from tests.sdk.test_scene_caller_preservation import _scene_context, _scene_result


def _real_spans(answers):
    context = deepcopy(_scene_context())
    context["capture_snapshot"]["duration_frames"] = len(answers)
    scene_clips = {
        layer["clipId"]: {"address": layer["clipId"]}
        for answer in answers
        for layer in answer["layers"]
    }
    context["capture_snapshot"]["metadata"]["scene_clips"] = scene_clips
    result, client = _scene_result(context, answers=answers, scene_clips=scene_clips)
    spans = timeline_filmstrip._scene_only_spans(
        result, context, duration_frames=len(answers), client=client
    )
    return spans


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


def test_real_public_scene_answers_reach_rle_summary(native_host, monkeypatch):  # noqa: F811
    monkeypatch.delenv("PYTHONNOUSERSITE", raising=False)
    source = props(
        [
            {"id": "content", "track": "plate", "clipType": "custom-alias", "at": 0, "hold": 1},
            {
                "id": "empty",
                "track": "plate",
                "clipType": "am-snap-plate",
                "at": 1.5,
                "hold": 1.5,
                "params": {"fill": "#000000"},
            },
        ]
    )
    answers = host_frames_at(source, BASIS, range(90))
    spans = _real_spans(answers)
    assert [(row.start_frame, row.end_frame, row.cls, row.content, row.clips) for row in spans] == [
        (0, 30, "COVERED", "CONTENT", ("content",)),
        (30, 45, "GAP", None, ()),
        (45, 90, "COVERED", "EMPTY", ("empty",)),
    ]
    assert [
        (row.start_frame, row.end_frame, row.cls, row.content)
        for row in _summary_rows(monkeypatch, spans)
    ] == [
        (0, 30, "COVERED", "CONTENT"),
        (30, 45, "GAP", None),
        (45, 90, "COVERED", "EMPTY"),
    ]

    faded = props(
        [
            {
                "id": "faded",
                "track": "plate",
                "clipType": "custom-alias",
                "at": 0,
                "hold": 2,
                "effects": {"fade_in": 0.5},
            }
        ]
    )
    fade_answers = host_frames_at(faded, BASIS, range(60))
    fade_spans = _real_spans(fade_answers)
    assert [
        (row.start_frame, row.end_frame, row.cls, row.content, row.clips) for row in fade_spans
    ] == [
        (0, 15, "DIP", None, ("faded",)),
        (15, 60, "COVERED", "CONTENT", ("faded",)),
    ]
    assert [
        (row.start_frame, row.end_frame, row.cls, row.content)
        for row in _summary_rows(monkeypatch, fade_spans)
    ] == [
        (0, 15, "DIP", None),
        (15, 60, "COVERED", "CONTENT"),
    ]

    generated = props(
        [
            {
                "id": "generated-300",
                "track": "plate",
                "clipType": "custom-alias",
                "at": 0,
                "hold": 10,
            }
        ]
    )
    generated_answers = host_frames_at(generated, BASIS, range(300))
    generated_spans = _real_spans(generated_answers)
    assert len(generated_answers) == 300
    assert [
        (row.start_frame, row.end_frame, row.cls, row.content, row.clips) for row in generated_spans
    ] == [
        (0, 300, "COVERED", "CONTENT", ("generated-300",)),
    ]
    assert [
        (row.start_frame, row.end_frame, row.cls, row.content)
        for row in _summary_rows(monkeypatch, generated_spans)
    ] == [
        (0, 300, "COVERED", "CONTENT"),
    ]

    adjacent = props(
        [
            {
                "id": f"generated-{frame:03d}",
                "track": "plate",
                "clipType": "custom-alias",
                "at": frame / BASIS["fps"],
                "hold": 1 / BASIS["fps"],
            }
            for frame in range(300)
        ]
    )
    adjacent_answers = host_frames_at(adjacent, BASIS, range(300))
    adjacent_spans = _real_spans(adjacent_answers)
    assert len(adjacent_answers) == len(adjacent_spans) == 300
    assert [(row.start_frame, row.end_frame, row.cls, row.content) for row in adjacent_spans] == [
        (frame, frame + 1, "COVERED", "CONTENT") for frame in range(300)
    ]
    assert adjacent_spans[0].clips == ("generated-000",)
    assert adjacent_spans[-1].clips == ("generated-299",)
    assert [
        (row.start_frame, row.end_frame, row.cls, row.content)
        for row in _summary_rows(monkeypatch, adjacent_spans)
    ] == [
        (0, 300, "COVERED", "CONTENT"),
    ]
