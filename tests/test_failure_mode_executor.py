from __future__ import annotations

from pathlib import Path

import pytest

from scripts import journey_failure_modes as failure
from scripts.failure_mode_executor import (
    ENTITY_PATHS,
    execute,
    main,
    process_observation,
    semantic_inventory,
)
from tests.failure_mode_fixtures import PROVISIONAL_CANDIDATE

FIXTURE = Path(__file__).parent / "fixtures" / "release0"


def test_executor_runs_real_disposable_boundaries_and_validates_receipt(tmp_path: Path) -> None:
    work_root = tmp_path / "run"
    observation = execute("direct-linux", PROVISIONAL_CANDIDATE, FIXTURE, work_root)

    receipt = failure.create_receipt("direct-linux", PROVISIONAL_CANDIDATE, observation)

    assert failure.validate_receipt(receipt, PROVISIONAL_CANDIDATE, "direct-linux") == []
    assert receipt["status"] == "pass"
    assert receipt["execution_scope"] == "local-disposable; not hosted OS evidence"
    assert receipt["scenarios"]["interruption_recovery"]["terminal_identity"]["running"] is True
    assert receipt["scenarios"]["failed_candidate_rollback"]["candidate_started"] is False
    assert receipt["scenarios"]["keep_data_uninstall"]["data_root_exists"] is True
    assert set(receipt["scenarios"]["stop_safety"]["cases"]) == {"refused", "unknown"}
    assert all((work_root / item["path"]).is_file() for item in receipt["evidence_files"])


def test_executor_rejects_work_root_outside_system_temp(tmp_path: Path) -> None:
    outside = Path("/var") / "astrid-failure-mode-test"
    with pytest.raises(ValueError, match="temporary directory"):
        execute("direct-linux", PROVISIONAL_CANDIDATE, FIXTURE, outside)


def test_cli_writes_receipt_and_run_owned_evidence(tmp_path: Path) -> None:
    candidate_path = tmp_path / "candidate.json"
    failure.write_json(candidate_path, PROVISIONAL_CANDIDATE)
    receipt_path = tmp_path / "results" / "direct-linux" / failure.RECEIPT_NAME

    result = main([
        "--slot-id", "direct-linux", "--candidate", str(candidate_path),
        "--fixture", str(FIXTURE), "--work-root", str(tmp_path / "work"),
        "--output", str(receipt_path),
    ])

    assert result == 0
    receipt = failure.read_json(receipt_path)
    assert receipt["status"] == "pass"
    from scripts.check_failure_receipts import verify_evidence_files

    assert verify_evidence_files(receipt, receipt_path) == []


def test_identity_probe_fails_closed_for_missing_process() -> None:
    with pytest.raises(ValueError, match="process PID"):
        process_observation(0)


def test_semantic_inventory_requires_all_entities(tmp_path: Path) -> None:
    data = tmp_path / "data"
    for relative in ENTITY_PATHS.values():
        path = data / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"entity")
    snapshot = semantic_inventory(data)
    assert snapshot["identical"] is True
    (data / ENTITY_PATHS["cas"]).unlink()
    with pytest.raises(ValueError, match="semantic entity"):
        semantic_inventory(data)


def test_receipt_cannot_admit_synthetic_identity_or_changed_data(tmp_path: Path) -> None:
    work_root = tmp_path / "run"
    observation = execute("direct-linux", PROVISIONAL_CANDIDATE, FIXTURE, work_root)
    observation["scenarios"]["failed_candidate_rollback"]["executable_identity"]["after"]["running"] = False
    observation["scenarios"]["keep_data_uninstall"]["semantic_data"]["identical"] = False
    receipt = failure.create_receipt("direct-linux", PROVISIONAL_CANDIDATE, observation)

    errors = failure.validate_receipt(receipt, PROVISIONAL_CANDIDATE, "direct-linux")

    assert any("executable was not observed running" in error for error in errors)
    assert any("semantic data is not identical" in error for error in errors)
