from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

import pytest

from astrid.core.contracts import (
    ManagedGenerationResult,
    ManagedGenerationResultError,
    validate_managed_generation_result,
)


def _result_payload(staging: Path, *, content: bytes = b"video bytes") -> dict[str, Any]:
    output = staging / "outputs" / "video.mp4"
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_bytes(content)
    return {
        "schema_version": 1,
        "kind": "managed-generation-result.v1",
        "inputs": {"prompt": "a test video"},
        "outputs": [
            {
                "producer_output_id": "video-main-0",
                "output_port": "generated_video",
                "ordinal": 0,
                "path": "outputs/video.mp4",
                "media_type": "video/mp4",
                "bytes": len(content),
                "sha256": hashlib.sha256(content).hexdigest(),
            }
        ],
        "created": "2026-09-21T12:00:00Z",
        "warnings": [],
        "task_id": "task-123",
        "attempt_id": "attempt-456",
        "producer_run_id": "producer-run-789",
        "outcomes": {
            "execution": {"status": "succeeded"},
            "retrieval": {"status": "succeeded"},
            "verification": {"status": "succeeded"},
            "publication": {"status": "not_started"},
        },
        "evidence": {
            "producer": {"engine": {"receipt_id": "receipt-1"}},
            "transport": {"location": {"transfer_id": "transfer-1"}},
        },
    }


def test_valid_video_result_has_deterministic_json_and_round_trips(tmp_path: Path) -> None:
    payload = _result_payload(tmp_path)
    result = validate_managed_generation_result(payload, staging_root=tmp_path)

    assert result.to_json() == result.to_json()
    assert json.loads(result.to_json()) == result.to_dict()
    round_trip = ManagedGenerationResult.from_json(
        result.to_json(), staging_root=tmp_path, expected_task_id="task-123", expected_attempt_id="attempt-456"
    )
    assert round_trip.to_dict() == result.to_dict()
    assert round_trip.outputs[0].path == "outputs/video.mp4"


@pytest.mark.parametrize("schema_version", [True, 1.0])
def test_schema_version_must_be_an_integer(tmp_path: Path, schema_version: Any) -> None:
    payload = _result_payload(tmp_path)
    payload["schema_version"] = schema_version

    with pytest.raises(ManagedGenerationResultError, match="schema_version"):
        validate_managed_generation_result(payload, staging_root=tmp_path)


def test_output_identity_ordinal_port_and_evidence_are_preserved(tmp_path: Path) -> None:
    payload = _result_payload(tmp_path)
    result = validate_managed_generation_result(payload, staging_root=tmp_path)

    output = result.to_dict()["outputs"][0]
    assert output == {
        "producer_output_id": "video-main-0",
        "output_port": "generated_video",
        "ordinal": 0,
        "path": "outputs/video.mp4",
        "media_type": "video/mp4",
        "bytes": 11,
        "sha256": hashlib.sha256(b"video bytes").hexdigest(),
    }
    assert result.to_dict()["evidence"] == payload["evidence"]


@pytest.mark.parametrize(
    ("mutation", "message"),
    [
        (lambda p: p["outputs"][0].pop("producer_output_id"), "producer_output_id"),
        (lambda p: p["outputs"][0].update({"output_id": "other"}), "unknown fields"),
        (lambda p: p["outputs"].append(dict(p["outputs"][0])), "duplicate producer output identity"),
        (lambda p: p["outputs"][0].update({"path": "../video.mp4"}), "contained relative path"),
        (lambda p: p["outputs"][0].update({"media_type": "video"}), "lowercase MIME"),
        (lambda p: p["outputs"][0].update({"bytes": 99}), "declares bytes"),
        (lambda p: p["outputs"][0].update({"sha256": "0" * 64}), "hashes to"),
    ],
)
def test_invalid_output_identity_location_metadata_and_hash_are_rejected(
    tmp_path: Path, mutation, message: str
) -> None:
    payload = _result_payload(tmp_path)
    mutation(payload)

    with pytest.raises(ManagedGenerationResultError, match=message):
        validate_managed_generation_result(payload, staging_root=tmp_path)


def test_duplicate_ordinals_and_missing_port_are_rejected(tmp_path: Path) -> None:
    payload = _result_payload(tmp_path)
    second = dict(payload["outputs"][0])
    second.update({"producer_output_id": "video-main-1"})
    payload["outputs"].append(second)
    with pytest.raises(ManagedGenerationResultError, match="duplicate output ordinal"):
        validate_managed_generation_result(payload, staging_root=tmp_path)

    payload = _result_payload(tmp_path)
    payload["outputs"][0].pop("output_port")
    with pytest.raises(ManagedGenerationResultError, match="output_port"):
        validate_managed_generation_result(payload, staging_root=tmp_path)


@pytest.mark.parametrize(
    "phase_mutation",
    [
        lambda p: p["outcomes"]["execution"].update({"status": "pending"}),
        lambda p: p["outcomes"]["retrieval"].update({"status": "pending"}),
        lambda p: p["outcomes"]["retrieval"].update({"status": "failed"}),
        lambda p: (
            p["outcomes"]["verification"].update({"status": "failed"}),
            p["outcomes"]["publication"].update({"status": "succeeded"}),
        ),
        lambda p: p["outcomes"]["execution"].update({"status": "failed"}),
    ],
)
def test_invalid_phase_outcome_combinations_are_rejected(tmp_path: Path, phase_mutation) -> None:
    payload = _result_payload(tmp_path)
    phase_mutation(payload)

    with pytest.raises(ManagedGenerationResultError):
        validate_managed_generation_result(payload, staging_root=tmp_path)


def test_stale_task_or_attempt_correlation_is_rejected(tmp_path: Path) -> None:
    payload = _result_payload(tmp_path)
    with pytest.raises(ManagedGenerationResultError, match="expected task_id"):
        validate_managed_generation_result(
            payload, staging_root=tmp_path, expected_task_id="different-task"
        )
    with pytest.raises(ManagedGenerationResultError, match="expected attempt_id"):
        validate_managed_generation_result(
            payload, staging_root=tmp_path, expected_attempt_id="different-attempt"
        )


def test_failed_execution_can_be_represented_without_outputs(tmp_path: Path) -> None:
    payload = _result_payload(tmp_path)
    payload["outputs"] = []
    payload["outcomes"] = {
        "execution": {"status": "failed", "code": "producer_failed"},
        "retrieval": {"status": "not_started"},
        "verification": {"status": "not_started"},
        "publication": {"status": "not_started"},
    }
    result = validate_managed_generation_result(payload, staging_root=tmp_path)
    assert result.outputs == ()
