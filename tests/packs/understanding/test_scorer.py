from astrid.packs.understanding.actions.visual_understand.run import OrderedImageEvidence
from astrid.packs.understanding.actions.visual_understand.scorer import (
    AnswerSpec,
    aggregate_sessions,
    process_evidence_for_gate,
    score_answers,
)


def _evidence(answers, *, returned_model="vision-v1", image_hashes=("image-a",)):
    return OrderedImageEvidence(
        prompt="Answer the questions.",
        prompt_sha256="prompt-hash",
        image_paths=("image.png",),
        image_hashes=image_hashes,
        model="vision-v1",
        settings={},
        response_id="response-1",
        returned_model=returned_model,
        usage=None,
        answers=answers,
        cost_ceiling=1,
    )


def test_score_answers_handles_exact_values_and_tolerance():
    evidence = _evidence(
        {
            "answers": [
                {"question_id": "frame", "frames": 12},
                {"question_id": "time", "time_seconds": 1.04},
                {"question_id": "label", "answer": "blue"},
            ]
        }
    )
    specs = [
        AnswerSpec("frame", "frames", None, 12),
        AnswerSpec("time", "seconds", 0.05, 1.0),
        AnswerSpec("label", "exact", None, "blue"),
    ]

    accuracy, results = score_answers(evidence, specs)

    assert accuracy == 1.0
    assert [result.detail for result in results] == ["exact-match"] * 3


def test_score_answers_fails_closed_on_invalid_envelope():
    evidence = _evidence({"answers": "not-a-list"})
    specs = [AnswerSpec("q1", "choice", None, "yes")]

    accuracy, results = score_answers(evidence, specs)

    assert accuracy == 0.0
    assert [(result.correct, result.detail) for result in results] == [(False, "schema-failure")]


def test_gate_reports_provenance_divergence_and_aggregates_distinct_sessions():
    evidence = _evidence({"answers": [{"answer": "yes"}]}, returned_model="other-v1")
    spec = [AnswerSpec("prompt-1", "exact", None, "yes")]

    result = process_evidence_for_gate(
        evidence,
        spec,
        declared_image_hashes=("expected-image",),
    )

    assert result["accuracy"] == 1.0
    assert result["valid_for_gate"] is False
    assert len(result["divergences"]) == 2
    assert aggregate_sessions(
        [("session-1", 1.0, []), ("session-2", 1.0, []), ("session-3", 0.95, [])]
    )["pass"] is True
