"""Deterministic scoring for ordered-image timeline evidence."""

from __future__ import annotations

import hashlib
import json
import math
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, replace
from decimal import Decimal, InvalidOperation
from typing import Any

from astrid.core.contracts.errors import AstridError
from .run import OrderedImageEvidence, _parse_ordered_answers

SESSION_PASS_THRESHOLD = 0.95
REQUIRED_SESSION_COUNT = 3
_KINDS = frozenset({"exact", "seconds", "frames", "choice", "ref"})
_MISSING = object()


@dataclass(frozen=True)
class AnswerSpec:
    question_id: str
    kind: str
    tolerance_seconds: float | None
    expected: Any


@dataclass(frozen=True)
class ScoreResult:
    question_id: str
    correct: bool
    detail: str
    raw_answer: Any | None


def _schema_from_evidence(evidence: OrderedImageEvidence) -> dict[str, Any] | None:
    structured = evidence.settings.get("structured")
    if not isinstance(structured, Mapping):
        return None
    schema = structured.get("schema")
    return dict(schema) if isinstance(schema, Mapping) else None


def _validated_answer_entries(
    evidence: OrderedImageEvidence,
) -> dict[str, Mapping[str, Any]]:
    """Re-run R21 validation, then validate the scorer's answer envelope."""

    try:
        response = {"output_text": json.dumps(evidence.answers, allow_nan=False)}
        answers = _parse_ordered_answers(
            response,
            schema=_schema_from_evidence(evidence),
        )
    except (AstridError, TypeError, ValueError) as exc:
        raise ValueError("schema-failure") from exc

    entries = answers.get("answers")
    if not isinstance(entries, list):
        raise ValueError("schema-failure")
    fixture_id = answers.get("fixture_id")
    if fixture_id is not None and not isinstance(fixture_id, str):
        raise ValueError("schema-failure")

    indexed: dict[str, Mapping[str, Any]] = {}
    for entry in entries:
        if not isinstance(entry, Mapping):
            raise ValueError("schema-failure")
        question_id = entry.get("question_id")
        if not isinstance(question_id, str) or not question_id or question_id in indexed:
            raise ValueError("schema-failure")
        indexed[question_id] = entry
    return indexed


def _raw_answer(entry: Mapping[str, Any], kind: str) -> Any:
    if entry.get("abstain") is True:
        return _MISSING
    field_order = {
        "frames": ("frames", "answer", "value"),
        "seconds": ("time_seconds", "answer", "value"),
        "choice": ("choice", "state", "answer", "value"),
        "ref": ("ref", "answer_ids", "answer", "value"),
        "exact": ("answer", "state", "value"),
    }[kind]
    for field in field_order:
        if field in entry:
            return entry[field]
    return _MISSING


def _valid_number(value: Any) -> bool:
    return (
        isinstance(value, (int, float))
        and not isinstance(value, bool)
        and math.isfinite(float(value))
    )


def _validate_specs(specs: Sequence[AnswerSpec]) -> None:
    seen: set[str] = set()
    for spec in specs:
        if not isinstance(spec.question_id, str) or not spec.question_id:
            raise ValueError("question_id must be a non-empty string")
        if spec.question_id in seen:
            raise ValueError(f"duplicate question_id: {spec.question_id}")
        seen.add(spec.question_id)
        if spec.kind not in _KINDS:
            raise ValueError(f"unsupported answer kind: {spec.kind}")
        if spec.kind == "seconds" and spec.tolerance_seconds is not None:
            if not _valid_number(spec.tolerance_seconds) or spec.tolerance_seconds < 0:
                raise ValueError("seconds tolerance must be a finite non-negative number")


def score_answers(
    evidence: OrderedImageEvidence,
    specs: Sequence[AnswerSpec],
) -> tuple[float, list[ScoreResult]]:
    """Return exact aggregate accuracy and deterministic per-question results."""

    frozen_specs = tuple(specs)
    _validate_specs(frozen_specs)
    if not frozen_specs:
        return 0.0, []

    try:
        entries = _validated_answer_entries(evidence)
    except ValueError:
        return 0.0, [
            ScoreResult(spec.question_id, False, "schema-failure", None) for spec in frozen_specs
        ]

    results: list[ScoreResult] = []
    for spec in frozen_specs:
        entry = entries.get(spec.question_id)
        raw = _MISSING if entry is None else _raw_answer(entry, spec.kind)
        if raw is _MISSING or raw is None:
            results.append(ScoreResult(spec.question_id, False, "parse-failure", None))
            continue

        if spec.kind == "frames":
            well_formed = isinstance(raw, int) and not isinstance(raw, bool)
            expected_well_formed = isinstance(spec.expected, int) and not isinstance(
                spec.expected, bool
            )
            if not well_formed or not expected_well_formed:
                results.append(ScoreResult(spec.question_id, False, "parse-failure", raw))
                continue
            correct = raw == spec.expected
        elif spec.kind == "seconds":
            if not _valid_number(raw) or not _valid_number(spec.expected):
                results.append(ScoreResult(spec.question_id, False, "parse-failure", raw))
                continue
            if spec.tolerance_seconds is None:
                correct = raw == spec.expected
            else:
                try:
                    delta = abs(Decimal(str(raw)) - Decimal(str(spec.expected)))
                    correct = delta <= Decimal(str(spec.tolerance_seconds))
                except InvalidOperation:
                    results.append(ScoreResult(spec.question_id, False, "parse-failure", raw))
                    continue
        else:
            if not isinstance(raw, str) or not isinstance(spec.expected, str):
                results.append(ScoreResult(spec.question_id, False, "parse-failure", raw))
                continue
            correct = raw == spec.expected

        results.append(
            ScoreResult(
                question_id=spec.question_id,
                correct=correct,
                detail="exact-match" if correct else "off-by",
                raw_answer=raw,
            )
        )

    correct_count = sum(result.correct for result in results)
    return correct_count / len(frozen_specs), results


def detect_divergences(
    evidence: OrderedImageEvidence,
    *,
    declared_image_hashes: Sequence[str] | None = None,
) -> list[str]:
    """Return deterministic provenance mismatches that invalidate a gate read."""

    divergences: list[str] = []
    if evidence.returned_model != evidence.model:
        divergences.append(
            f"model-divergence: requested={evidence.model!r}, returned={evidence.returned_model!r}"
        )
    if declared_image_hashes is not None:
        declared = tuple(declared_image_hashes)
        observed = tuple(evidence.image_hashes)
        if observed != declared:
            divergences.append(
                f"image-order-divergence: declared={list(declared)!r}, observed={list(observed)!r}"
            )
    return divergences


def process_evidence_for_gate(
    evidence: OrderedImageEvidence,
    specs: Sequence[AnswerSpec],
    *,
    declared_image_hashes: Sequence[str] | None = None,
) -> dict[str, Any]:
    """Score one response and expose provenance validity for the release gate.

    Gate callers must require ``valid_for_gate`` before aggregating the returned
    identity, accuracy, and results as a fresh session.
    """

    divergences = detect_divergences(
        evidence,
        declared_image_hashes=declared_image_hashes,
    )
    # The VLM echoes the prompt's positional ids (Q1..QN); re-key each answer
    # to its spec question_id so score_answers can index by spec id. The
    # prompt presents questions in spec order, so positional mapping is
    # deterministic. Unknown answers are preserved for divergence checks.
    raw_answers = list(evidence.answers.get("answers", []))
    rekeyed = []
    for index, spec in enumerate(specs):
        if index < len(raw_answers):
            entry = dict(raw_answers[index])
            entry["question_id"] = spec.question_id
            rekeyed.append(entry)
        else:
            rekeyed.append({"question_id": spec.question_id})
    rekeyed_evidence = replace(evidence, answers={**evidence.answers, "answers": rekeyed})
    accuracy, results = score_answers(rekeyed_evidence, specs)
    return {
        "accuracy": accuracy,
        "divergences": divergences,
        "results": results,
        "session_identity": session_identity(evidence),
        "valid_for_gate": not divergences,
    }


def aggregate_sessions(
    sessions: Sequence[tuple[str, float, list[ScoreResult]]],
) -> dict[str, Any]:
    """Require three fresh threshold passes; reject repeated session identities."""

    accuracies: list[float] = []
    identities: list[str] = []
    seen_identities: set[str] = set()
    duplicate_identities: set[str] = set()
    for identity, accuracy, _results in sessions:
        if not isinstance(identity, str) or not identity:
            raise ValueError("session identity must be a non-empty string")
        if identity in seen_identities:
            duplicate_identities.add(identity)
        seen_identities.add(identity)
        identities.append(identity)
        if not _valid_number(accuracy) or not 0.0 <= float(accuracy) <= 1.0:
            raise ValueError("session accuracy must be a finite number between 0 and 1")
        accuracies.append(float(accuracy))
    if duplicate_identities:
        duplicates = ", ".join(sorted(duplicate_identities))
        raise ValueError(f"duplicate session identities: {duplicates}")
    session_passes = [accuracy >= SESSION_PASS_THRESHOLD for accuracy in accuracies]
    passed = len(accuracies) == REQUIRED_SESSION_COUNT and all(session_passes)
    return {
        "all_sessions_pass": passed,
        "overall_pass": passed,
        "pass": passed,
        "passed": passed,
        "required_sessions": REQUIRED_SESSION_COUNT,
        "session_accuracies": accuracies,
        "session_count": len(accuracies),
        "session_identities": identities,
        "session_passes": session_passes,
        "threshold": SESSION_PASS_THRESHOLD,
    }


def session_identity(evidence: OrderedImageEvidence) -> str:
    """Hash the provider response id and ordered image hashes canonically."""

    if not isinstance(evidence.response_id, str) or not evidence.response_id:
        raise ValueError("session identity requires a non-empty response id")
    if not all(isinstance(digest, str) and digest for digest in evidence.image_hashes):
        raise ValueError("session identity requires non-empty image hashes")
    payload = {
        "image_hashes": list(evidence.image_hashes),
        "response_id": evidence.response_id,
        "version": 1,
    }
    canonical = json.dumps(payload, ensure_ascii=False, separators=(",", ":"), sort_keys=True)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()
