from __future__ import annotations

import copy
import hashlib
import json
from pathlib import Path

import pytest

from scripts import check_failure_receipts as gate
from scripts import journey_failure_modes as failure
from tests.failure_mode_fixtures import PROVISIONAL_CANDIDATE, valid_observation


def write_complete_set(root: Path) -> dict:
    needs = {}
    for slot_id in failure.REQUIRED_SLOT_IDS:
        observation = valid_observation(slot_id)
        for item in observation["evidence_files"]:
            evidence_path = root / slot_id / item["path"]
            evidence_path.parent.mkdir(parents=True, exist_ok=True)
            content = f"{slot_id}:{item['scenario']}\n".encode()
            evidence_path.write_bytes(content)
            item["sha256"] = hashlib.sha256(content).hexdigest()
        receipt = failure.create_receipt(slot_id, PROVISIONAL_CANDIDATE, observation)
        failure.write_json(root / slot_id / failure.RECEIPT_NAME, receipt)
        needs[slot_id] = {"result": "success", "outputs": {"receipt": slot_id}}
    return needs


def test_aggregate_accepts_exact_nine_verified_receipts(tmp_path: Path) -> None:
    needs = write_complete_set(tmp_path)

    assert gate.evaluate(PROVISIONAL_CANDIDATE, tmp_path, needs) == []


@pytest.mark.parametrize("slot_id", failure.REQUIRED_SLOT_IDS)
def test_aggregate_fails_for_each_missing_required_receipt(tmp_path: Path, slot_id: str) -> None:
    needs = write_complete_set(tmp_path)
    (tmp_path / slot_id / failure.RECEIPT_NAME).unlink()

    errors = gate.evaluate(PROVISIONAL_CANDIDATE, tmp_path, needs)

    assert f"{slot_id}: missing receipt" in errors


@pytest.mark.parametrize("slot_id", failure.REQUIRED_SLOT_IDS)
def test_aggregate_fails_for_each_skipped_needs_dependency(tmp_path: Path, slot_id: str) -> None:
    needs = write_complete_set(tmp_path)
    needs[slot_id]["result"] = "skipped"

    errors = gate.evaluate(PROVISIONAL_CANDIDATE, tmp_path, needs)

    assert f"{slot_id}: needs result 'skipped' is not success" in errors


def test_successful_needs_prose_cannot_replace_a_receipt(tmp_path: Path) -> None:
    needs = write_complete_set(tmp_path)
    missing = failure.REQUIRED_SLOT_IDS[0]
    (tmp_path / missing / failure.RECEIPT_NAME).unlink()
    needs[missing]["outputs"] = {"receipt": "Tests passed; see source and logs."}

    errors = gate.evaluate(PROVISIONAL_CANDIDATE, tmp_path, needs)

    assert f"{missing}: missing receipt" in errors


def test_skipped_status_inside_receipt_is_not_a_pass(tmp_path: Path) -> None:
    needs = write_complete_set(tmp_path)
    slot_id = "direct-linux"
    receipt_path = tmp_path / slot_id / failure.RECEIPT_NAME
    receipt = failure.read_json(receipt_path)
    receipt["status"] = "skipped"
    failure.write_json(receipt_path, receipt)

    errors = gate.evaluate(PROVISIONAL_CANDIDATE, tmp_path, needs)

    assert any(f"{slot_id}: status: 'skipped' is not pass" == error for error in errors)


def test_aggregate_rejects_duplicate_malformed_and_unknown_receipts(tmp_path: Path) -> None:
    needs = write_complete_set(tmp_path)
    duplicate = failure.read_json(tmp_path / "direct-macos" / failure.RECEIPT_NAME)
    failure.write_json(tmp_path / "duplicate" / failure.RECEIPT_NAME, duplicate)
    (tmp_path / "malformed" / failure.RECEIPT_NAME).parent.mkdir()
    (tmp_path / "malformed" / failure.RECEIPT_NAME).write_text("[]\n", encoding="utf-8")
    unknown = copy.deepcopy(duplicate)
    unknown["slot_id"] = "unknown-slot"
    failure.write_json(tmp_path / "unknown" / failure.RECEIPT_NAME, unknown)

    errors = gate.evaluate(PROVISIONAL_CANDIDATE, tmp_path, needs)

    assert any("duplicate receipt for direct-macos" in error for error in errors)
    assert any("expected a JSON object" in error for error in errors)
    assert any("unknown or absent failure-mode slot id" in error for error in errors)


def test_aggregate_revalidates_machine_observations(tmp_path: Path) -> None:
    needs = write_complete_set(tmp_path)
    slot_id = "pinokio-backend-windows"
    receipt_path = tmp_path / slot_id / failure.RECEIPT_NAME
    receipt = failure.read_json(receipt_path)
    receipt["evidence_files"][0]["kind"] = "prose"
    failure.write_json(receipt_path, receipt)

    errors = gate.evaluate(PROVISIONAL_CANDIDATE, tmp_path, needs)

    assert any("prose/source evidence is not admissible" in error for error in errors)


def test_aggregate_requires_hashed_machine_evidence_file(tmp_path: Path) -> None:
    needs = write_complete_set(tmp_path)
    slot_id = "direct-macos"
    evidence = tmp_path / slot_id / "evidence" / "interruption_recovery.json"
    evidence.unlink()

    errors = gate.evaluate(PROVISIONAL_CANDIDATE, tmp_path, needs)

    assert any("missing evidence file" in error for error in errors)


def test_cli_reports_failure_for_missing_dependency(tmp_path: Path, capsys) -> None:
    results = tmp_path / "results"
    needs = write_complete_set(results)
    needs.pop("direct-windows")
    candidate_path = tmp_path / "candidate.json"
    candidate_path.write_text(json.dumps(PROVISIONAL_CANDIDATE), encoding="utf-8")
    needs_path = tmp_path / "needs.json"
    needs_path.write_text(json.dumps(needs), encoding="utf-8")

    result = gate.main(
        ["--candidate", str(candidate_path), "--results", str(results), "--needs", str(needs_path)]
    )

    assert result == 1
    assert "direct-windows: missing needs dependency" in capsys.readouterr().out
