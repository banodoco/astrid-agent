from __future__ import annotations

import copy
import json
import subprocess
from pathlib import Path

import pytest

from scripts import journey_failure_modes as failure
from scripts import failure_mode_executor as executor_module
from scripts.failure_mode_executor import (
    ENTITY_PATHS,
    HistoricalSourcePrerequisiteError,
    execute,
    execution_observation_errors,
    main,
    process_observation,
    semantic_inventory,
)
from tests.failure_mode_fixtures import PROVISIONAL_CANDIDATE

FIXTURE = Path(__file__).parent / "fixtures" / "release0"


def test_recovery_requires_the_same_operation_in_a_terminal_journal() -> None:
    pending = {"operation_id": "original", "step": "switched"}
    for step in ("complete", "rolled_back"):
        assert executor_module._verify_recovery_journal(
            pending, {"operation_id": "original", "step": step}
        ) == "original"
    for terminal in ({"operation_id": "other", "step": "complete"},
                     {"operation_id": "original", "step": "switched"},
                     {"step": "complete"}):
        with pytest.raises(ValueError, match="interrupted operation"):
            executor_module._verify_recovery_journal(pending, terminal)


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
    historical_provenance = receipt["release0_source_provenance"]
    assert historical_provenance["agent"]["ref"] == failure.RELEASE0_REFS["agent"]
    assert historical_provenance["agent"]["object_type"] == "commit"
    assert historical_provenance["runtime"]["ref"] == failure.RELEASE0_REFS["runtime"]
    assert historical_provenance["runtime"]["object_type"] == "commit"
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


def test_executor_fails_closed_without_exact_release0_runtime_repo(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("ASTRID_RELEASE0_AGENT_REPO", raising=False)
    monkeypatch.delenv("ASTRID_RELEASE0_RUNTIME_REPO", raising=False)
    with pytest.raises(
        HistoricalSourcePrerequisiteError,
        match="Historical release-0 prerequisite missing",
    ) as caught:
        execute(
            "direct-linux",
            PROVISIONAL_CANDIDATE,
            FIXTURE,
            tmp_path / "run",
            runtime_repo=tmp_path / "not-a-runtime-repo",
        )
    assert caught.value.prerequisite["component"] == "Runtime"
    assert caught.value.prerequisite["searched_repository"] == str(
        (tmp_path / "not-a-runtime-repo").resolve()
    )
    assert not (tmp_path / "run").exists()


def test_default_discovery_reports_the_actual_git_root_and_does_not_mutate(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("ASTRID_RELEASE0_AGENT_REPO", raising=False)
    monkeypatch.delenv("ASTRID_RELEASE0_RUNTIME_REPO", raising=False)
    repository = tmp_path / "default-repository"
    subprocess.run(["git", "init", "--quiet", str(repository)], check=True)
    with pytest.raises(HistoricalSourcePrerequisiteError) as caught:
        execute(
            "direct-linux",
            PROVISIONAL_CANDIDATE,
            FIXTURE,
            tmp_path / "default-run",
            agent_repo=repository,
        )
    assert caught.value.prerequisite["component"] == "Agent"
    assert caught.value.prerequisite["searched_repository"] == str(repository.resolve())
    assert not (tmp_path / "default-run").exists()


def test_agent_repository_override_is_truthful_and_does_not_mutate(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    configured = tmp_path / "configured-agent"
    monkeypatch.setenv("ASTRID_RELEASE0_AGENT_REPO", str(configured))
    with pytest.raises(HistoricalSourcePrerequisiteError) as caught:
        execute(
            "direct-linux",
            PROVISIONAL_CANDIDATE,
            FIXTURE,
            tmp_path / "override-run",
            agent_repo=Path(__file__).parents[1],
        )
    assert caught.value.prerequisite["component"] == "Agent"
    assert caught.value.prerequisite["searched_repository"] == str(configured.resolve())
    assert not (tmp_path / "override-run").exists()


def test_runtime_repository_override_is_truthful_and_does_not_mutate(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    configured = tmp_path / "configured-runtime"
    monkeypatch.setenv("ASTRID_RELEASE0_RUNTIME_REPO", str(configured))
    with pytest.raises(HistoricalSourcePrerequisiteError) as caught:
        execute(
            "direct-linux",
            PROVISIONAL_CANDIDATE,
            FIXTURE,
            tmp_path / "runtime-override-run",
            runtime_repo=Path(__file__).parents[1],
        )
    assert caught.value.prerequisite["component"] == "Runtime"
    assert caught.value.prerequisite["searched_repository"] == str(configured.resolve())
    assert not (tmp_path / "runtime-override-run").exists()


def test_mismatched_historical_object_fails_before_mutation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("ASTRID_RELEASE0_AGENT_REPO", raising=False)
    monkeypatch.delenv("ASTRID_RELEASE0_RUNTIME_REPO", raising=False)
    required = failure.RELEASE0_REFS["runtime"]
    repository = tmp_path / "mismatched-runtime"
    repository.mkdir()

    def fake_git_output(_repository: Path, *args: str) -> str:
        if args == ("rev-parse", "--show-toplevel"):
            return str(repository)
        if args == ("rev-parse", f"{required}^{{commit}}"):
            return "f" * 40
        if args == ("cat-file", "-t", required):
            return "commit"
        raise AssertionError(args)

    monkeypatch.setattr(executor_module, "_git_output", fake_git_output)
    with pytest.raises(HistoricalSourcePrerequisiteError) as caught:
        execute(
            "direct-linux",
            PROVISIONAL_CANDIDATE,
            FIXTURE,
            tmp_path / "mismatched-run",
            runtime_repo=repository,
        )
    assert caught.value.prerequisite["component"] == "Runtime"
    assert not (tmp_path / "mismatched-run").exists()


def test_cli_records_missing_historical_object_prerequisite(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("ASTRID_RELEASE0_AGENT_REPO", raising=False)
    monkeypatch.delenv("ASTRID_RELEASE0_RUNTIME_REPO", raising=False)
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
    assert receipt["historical_release0"]["searched_repository"] == str(
        (tmp_path / "missing-agent").resolve()
    )
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
