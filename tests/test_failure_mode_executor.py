from __future__ import annotations

import copy
import json
from pathlib import Path

import pytest

from scripts import journey_failure_modes as failure
from scripts.failure_mode_executor import (
    ENTITY_PATHS,
    execute,
    execution_observation_errors,
    main,
    process_observation,
    semantic_inventory,
)
from tests.failure_mode_fixtures import PROVISIONAL_CANDIDATE

FIXTURE = Path(__file__).parent / "fixtures" / "release0"


def test_executor_runs_real_boundaries_but_local_receipt_is_not_hosted(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("GITHUB_ACTIONS", raising=False)
    work_root = tmp_path / "run"
    observation = execute("direct-linux", PROVISIONAL_CANDIDATE, FIXTURE, work_root)

    receipt = failure.create_receipt("direct-linux", PROVISIONAL_CANDIDATE, observation)

    errors = failure.validate_receipt(receipt, PROVISIONAL_CANDIDATE, "direct-linux")
    assert receipt["status"] == "fail"
    assert any("hosted runner identity" in error for error in errors)
    assert receipt["execution_scope"] == "local-disposable; not hosted OS evidence"
    terminal = receipt["scenarios"]["interruption_recovery"]["terminal_identity"]
    assert terminal["running"] is True
    assert Path(terminal["executable_path"]).is_file()
    assert not Path(terminal["executable_path"]).is_symlink()
    loaded = terminal["loaded_sources"]
    assert Path(loaded["agent_module"]).is_relative_to(work_root / "sources" / "release0")
    assert Path(loaded["runtime_module"]).is_relative_to(work_root / "sources" / "release0")
    assert loaded["runtime_discovery"]["pid"] > 0
    assert loaded["runtime_discovery"]["active_realm"] == "release0-executable"
    assert receipt["scenarios"]["failed_candidate_rollback"]["candidate_started"] is False
    candidate_boundary = receipt["scenarios"]["failed_candidate_rollback"][
        "candidate_process_boundary"
    ]
    assert candidate_boundary["started"] is True
    assert candidate_boundary["exit_code"] == 73
    assert candidate_boundary["loaded_sources"]["agent_module_sha256"]
    uninstall = receipt["scenarios"]["keep_data_uninstall"]
    assert uninstall["data_root_exists"] is True
    assert uninstall["runtime_processes_after"] == 0
    assert uninstall["native_service_removed"] is False
    assert uninstall["disposable_service_selection_removed"] is True
    assert set(receipt["scenarios"]["stop_safety"]["cases"]) == {"refused", "unknown"}
    assert all((work_root / item["path"]).is_file() for item in receipt["evidence_files"])

    synthetic = copy.deepcopy(observation)
    synthetic["scenarios"]["interruption_recovery"]["terminal_identity"].pop("loaded_sources")
    synthetic["scenarios"]["failed_candidate_rollback"]["candidate_process_boundary"][
        "exit_code"
    ] = 0
    synthetic["scenarios"]["stop_safety"]["cases"]["unknown"]["cleanup"]["process_absent"] = False
    executor_errors = execution_observation_errors(synthetic)
    assert any("loaded-source observation is missing" in error for error in executor_errors)
    assert any("candidate process boundary is missing" in error for error in executor_errors)
    assert any("owned-process cleanup is missing" in error for error in executor_errors)


def test_executor_rejects_work_root_outside_system_temp(tmp_path: Path) -> None:
    outside = Path("/var") / "astrid-failure-mode-test"
    with pytest.raises(ValueError, match="temporary directory"):
        execute("direct-linux", PROVISIONAL_CANDIDATE, FIXTURE, outside)


def test_cli_writes_evidence_but_refuses_local_as_hosted(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("GITHUB_ACTIONS", raising=False)
    candidate_path = tmp_path / "candidate.json"
    failure.write_json(candidate_path, PROVISIONAL_CANDIDATE)
    receipt_path = tmp_path / "results" / "direct-linux" / failure.RECEIPT_NAME

    result = main(
        [
            "--slot-id",
            "direct-linux",
            "--candidate",
            str(candidate_path),
            "--fixture",
            str(FIXTURE),
            "--work-root",
            str(tmp_path / "work"),
            "--output",
            str(receipt_path),
        ]
    )

    assert result == 1
    receipt = failure.read_json(receipt_path)
    assert receipt["status"] == "fail"
    assert receipt["execution_scope"] == "local-disposable; not hosted OS evidence"
    from scripts.check_failure_receipts import verify_evidence_files

    assert verify_evidence_files(receipt, receipt_path) == []


def test_executor_fails_closed_without_exact_release0_runtime_repo(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="Historical release-0 prerequisite missing"):
        execute(
            "direct-linux",
            PROVISIONAL_CANDIDATE,
            FIXTURE,
            tmp_path / "run",
            runtime_repo=tmp_path / "not-a-runtime-repo",
        )
    assert not (tmp_path / "run").exists()


def test_cli_records_missing_historical_object_prerequisite(tmp_path: Path) -> None:
    candidate_path = tmp_path / "candidate.json"
    failure.write_json(candidate_path, PROVISIONAL_CANDIDATE)
    receipt_path = tmp_path / "results" / "direct-linux" / failure.RECEIPT_NAME

    result = main(
        [
            "--slot-id", "direct-linux", "--candidate", str(candidate_path),
            "--fixture", str(FIXTURE), "--work-root", str(tmp_path / "run"),
            "--agent-repo", str(tmp_path / "missing-agent"),
            "--output", str(receipt_path),
        ]
    )

    assert result == 1
    receipt = failure.read_json(receipt_path)
    assert receipt["historical_release0"]["component"] == "Agent"
    assert receipt["historical_release0"]["required_ref"] == failure.RELEASE0_REFS["agent"]
    assert json.loads(
        (receipt_path.parent / "historical-prerequisite.json").read_text(encoding="utf-8")
    )["historical_release0"]["object_type"] == "commit"
    assert not (tmp_path / "run").exists()


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
    observation["scenarios"]["failed_candidate_rollback"]["executable_identity"]["after"][
        "running"
    ] = False
    observation["scenarios"]["keep_data_uninstall"]["semantic_data"]["identical"] = False
    receipt = failure.create_receipt("direct-linux", PROVISIONAL_CANDIDATE, observation)

    errors = failure.validate_receipt(receipt, PROVISIONAL_CANDIDATE, "direct-linux")

    assert any("executable was not observed running" in error for error in errors)
    assert any("semantic data is not identical" in error for error in errors)
