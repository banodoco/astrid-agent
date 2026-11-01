"""Inventory contracts only; these tests do not execute installed journeys."""

import json

from scripts import check_journey_evidence, journey_matrix


OLD_ROWS = (
    {"job_id": "direct-macos", "mode": "direct", "runner": "macos-14", "shell": "sh", "os": "macos", "leg": "release0-to-release1"},
    {"job_id": "direct-linux", "mode": "direct", "runner": "ubuntu-24.04", "shell": "sh", "os": "linux", "leg": "release0-to-release1"},
    {"job_id": "direct-windows", "mode": "direct", "runner": "windows-2022", "shell": "powershell.exe", "os": "windows", "leg": "release0-to-release1"},
    {"job_id": "pinokio-backend-macos", "mode": "pinokio-backend", "runner": "macos-14", "shell": "sh", "os": "macos"},
    {"job_id": "pinokio-backend-linux", "mode": "pinokio-backend", "runner": "ubuntu-24.04", "shell": "sh", "os": "linux"},
    {"job_id": "pinokio-backend-windows", "mode": "pinokio-backend", "runner": "windows-2022", "shell": "powershell.exe", "os": "windows"},
    {"job_id": "pinokio-fully-local-macos", "mode": "pinokio-fully-local", "runner": "macos-14", "shell": "sh", "os": "macos"},
    {"job_id": "pinokio-fully-local-linux", "mode": "pinokio-fully-local", "runner": "ubuntu-24.04", "shell": "sh", "os": "linux"},
    {"job_id": "pinokio-fully-local-windows", "mode": "pinokio-fully-local", "runner": "windows-2022", "shell": "powershell.exe", "os": "windows"},
)
FAULT_IDS = ("direct-macos-interrupted-update", "direct-macos-rollback")


def test_prior_inventory_and_unique_separate_ids():
    rows = journey_matrix.RELEASE1_MATRIX
    assert tuple(row for row in rows if row["job_id"] not in FAULT_IDS) == OLD_ROWS
    ids = [row["job_id"] for row in rows]
    assert len(ids) == len(set(ids))
    assert ids[1:3] == list(FAULT_IDS)
    assert all(journey_matrix.release1_job(job_id)["scenario_id"] == job_id for job_id in FAULT_IDS)


def test_macos_fault_contracts_require_native_and_bound_observations():
    common = {
        "release0_fixture_and_installed_identity", "populated_data_before_and_after",
        "sentinel_identity_and_survival", "launchd_ownership_health_and_one_runtime",
        "keep_data_and_launchd_process_cleanup", "per_step_command_and_inner_exit",
        "scenario_tuple_runner_and_evidence_file_hashes",
    }
    for job_id in FAULT_IDS:
        row = journey_matrix.release1_job(job_id)
        assert (row["mode"], row["os"], row["runner"], row["shell"], row["manager"]) == (
            "direct", "macos", "macos-14", "sh", "launchd")
        assert row["executor"] == "failure_mode_executor.execute_installed_fault"
        assert common <= set(row["required_evidence"])
    interruption = journey_matrix.release1_job(FAULT_IDS[0])
    assert interruption["leg"] == "interrupted-update"
    assert interruption["steps"] == ("baseline", "interrupt", "recover", "verify")
    assert {"interruption_and_same_operation_terminal_journal", "installed_identity_after_recovery"} <= set(interruption["required_evidence"])
    rollback = journey_matrix.release1_job(FAULT_IDS[1])
    assert rollback["leg"] == "rollback"
    assert rollback["steps"] == ("baseline", "fail_candidate", "verify")
    assert {"authenticated_selected_release0_predecessor_identity", "candidate_failure_and_rollback_transaction_journal", "restored_predecessor_identity"} <= set(rollback["required_evidence"])


def test_checker_rejects_absent_fault_receipts(tmp_path):
    errors = check_journey_evidence.evaluate_release1_matrix({}, tmp_path)
    for job_id in FAULT_IDS:
        assert f"{job_id}: missing receipt" in errors


def test_unintegrated_checker_rejects_fault_leg_even_if_marked_pass(tmp_path):
    for job_id in FAULT_IDS:
        row = journey_matrix.release1_job(job_id)
        destination = tmp_path / job_id
        destination.mkdir()
        (destination / "journey-result.json").write_text(json.dumps({
            "job_id": job_id, "mode": "direct", "scenario": row["leg"], "status": "pass",
            "service_manager_prerequisite": {},
        }))
    errors = check_journey_evidence.evaluate_release1_matrix(
        {name: "a" * 40 for name in ("app", "agent", "runtime", "pinokio")}, tmp_path)
    for job_id in FAULT_IDS:
        assert f"{job_id}: direct receipt did not run the authentic release-0 to release-1 leg" in errors
