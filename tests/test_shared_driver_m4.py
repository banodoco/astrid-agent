"""Regressions for the explicitly enabled, local-only M4 shared routes."""

import copy
import json
from pathlib import Path

import pytest

from scripts import ci_journey, check_journey_evidence, journey_matrix
from scripts import failure_mode_executor, journey_failure_modes
from tests.failure_mode_fixtures import PROVISIONAL_CANDIDATE


def _local_observation(slot_id):
    contract = failure_mode_executor.failure.SLOT_CONTRACTS[slot_id]
    def identity():
        return {
            "loaded_sources": {
                "agent_module": "/tmp/agent.py", "agent_module_sha256": "a" * 64,
                "runtime_module": "/tmp/runtime.py", "runtime_module_sha256": "b" * 64,
                "runtime_discovery": {
                    "pid": 123, "process_birth_id": "birth", "runtime_instance_id": "instance",
                },
            },
        }

    cleanup = {"owned_processes_after": 0, "process_absent": True}
    pair = {"before": identity(), "after": identity()}
    cases = {}
    for outcome in failure_mode_executor.failure.STOP_OUTCOMES:
        cases[outcome] = {
            "executable_identity": copy.deepcopy(pair),
            "cleanup": copy.deepcopy(cleanup),
            "mutation_inventory": {"unchanged": True, "before": "same", "after": "same"},
        }
    return {
        "slot_id": slot_id,
        "runner_image": contract["runner"],
        "shell": contract["shell"],
        "execution_scope": "local-disposable",
        "runner_provenance": {"ImageVersion": "local-disposable"},
        "candidate_checkouts": {
            name: {"sha": PROVISIONAL_CANDIDATE[name]}
            for name in ("app", "agent", "runtime", "pinokio")
        },
        "manifest_sha256": PROVISIONAL_CANDIDATE["manifest_sha256"],
        "evidence_files": [],
        "scenarios": {
            "interruption_recovery": {"terminal_identity": identity(), "cleanup": copy.deepcopy(cleanup)},
            "failed_candidate_rollback": {
                "executable_identity": copy.deepcopy(pair),
                "candidate_process_boundary": {
                    "started": True, "exit_code": 73, "process_identity_sha256": "c" * 64,
                    "loaded_sources": {"agent_module_sha256": "d" * 64},
                },
                "cleanup": copy.deepcopy(cleanup),
            },
            "keep_data_uninstall": {"executable_identity_before": identity(), "cleanup": copy.deepcopy(cleanup)},
            "stop_safety": {"cases": cases},
        },
    }


def _write_local_matrix(root, mutate=None):
    for contract in journey_matrix.M4_6_MATRIX:
        slot = contract["job_id"]
        observation = _local_observation(slot)
        row = {
            "schema_version": 1,
            "receipt_kind": "m4-6-local-disposable-execution",
            "status": "exercised",
            "slot_id": slot,
            "execution_scope": "local-disposable",
            "source_override_test_mode": False,
            "candidate": copy.deepcopy(PROVISIONAL_CANDIDATE),
            "evidence_files": [],
            "observation": observation,
        }
        if mutate:
            mutate(slot, row)
        path = root / slot / "m4-6-local-result.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(row) + "\n", encoding="utf-8")


def test_m4_6_local_matrix_requires_all_nine_valid_local_outputs(tmp_path):
    _write_local_matrix(tmp_path)
    assert check_journey_evidence.evaluate_m4_6_local_matrix(
        tmp_path, PROVISIONAL_CANDIDATE
    ) == []


@pytest.mark.parametrize("mutation,expected", [
    ("incomplete", "missing M4-6 local execution"),
    ("synthetic", "executor.rollback"),
    ("wrong_tuple", "wrong candidate tuple"),
    ("cleanup", "owned-process cleanup is missing"),
    ("hosted", "wrong tuple or scope"),
])
def test_m4_6_local_aggregate_rejects_unadmitted_outputs(tmp_path, mutation, expected):
    def alter(slot, row):
        if slot != journey_matrix.M4_6_SLOT_IDS[0]:
            return
        if mutation == "incomplete":
            return
        elif mutation == "synthetic":
            row["observation"]["scenarios"]["failed_candidate_rollback"][
                "candidate_process_boundary"
            ]["exit_code"] = 0
        elif mutation == "wrong_tuple":
            row["observation"]["candidate_checkouts"]["agent"]["sha"] = "f" * 40
        elif mutation == "cleanup":
            row["observation"]["scenarios"]["stop_safety"]["cases"]["unknown"][
                "cleanup"
            ]["process_absent"] = False
        elif mutation == "hosted":
            row["execution_scope"] = "hosted"
            row["observation"]["execution_scope"] = "hosted"

    _write_local_matrix(tmp_path, alter)
    if mutation == "incomplete":
        (tmp_path / journey_matrix.M4_6_SLOT_IDS[0] / "m4-6-local-result.json").unlink()
    errors = check_journey_evidence.evaluate_m4_6_local_matrix(
        tmp_path, PROVISIONAL_CANDIDATE
    )
    assert errors
    assert any(expected in error for error in errors)


def test_m4_6_route_emits_local_exercised_output_without_hosted_validation(tmp_path, monkeypatch):
    import scripts.journey_failure_modes as failure

    slot = journey_matrix.M4_6_SLOT_IDS[0]
    candidate_path = tmp_path / "candidate.json"
    candidate_path.write_text(json.dumps(PROVISIONAL_CANDIDATE), encoding="utf-8")
    output = tmp_path / "result.json"
    observation = _local_observation(slot)
    monkeypatch.setattr(failure_mode_executor, "execute", lambda *args, **kwargs: observation)
    monkeypatch.setattr(failure_mode_executor, "execution_observation_errors", lambda value: [])
    monkeypatch.setattr(failure, "validate_receipt", lambda *args: pytest.fail("hosted validator called"))
    args = ["m4-6", "--slot-id", slot, "--candidate", str(candidate_path),
            "--fixture", str(tmp_path / "fixture"), "--output", str(output),
            "--work-root", str(tmp_path / "work"), "--agent-repo", str(tmp_path / "agent"),
            "--release0-runtime-repo", str(tmp_path / "runtime")]

    assert ci_journey._shared_executor_main(args) == 0
    receipt = json.loads(output.read_text(encoding="utf-8"))
    assert receipt["status"] == "exercised"
    assert receipt["execution_scope"] == "local-disposable"


def test_m4_6_route_fails_on_executor_observation_errors(tmp_path, monkeypatch):
    slot = journey_matrix.M4_6_SLOT_IDS[0]
    candidate_path = tmp_path / "candidate.json"
    candidate_path.write_text(json.dumps(PROVISIONAL_CANDIDATE), encoding="utf-8")
    monkeypatch.setattr(failure_mode_executor, "execute", lambda *args, **kwargs: _local_observation(slot))
    monkeypatch.setattr(failure_mode_executor, "execution_observation_errors", lambda value: ["cleanup failed"])
    args = ["m4-6", "--slot-id", slot, "--candidate", str(candidate_path),
            "--fixture", str(tmp_path / "fixture"), "--output", str(tmp_path / "result.json"),
            "--work-root", str(tmp_path / "work"), "--agent-repo", str(tmp_path / "agent"),
            "--release0-runtime-repo", str(tmp_path / "runtime")]
    assert ci_journey._shared_executor_main(args) == 1
    assert not (tmp_path / "result.json").exists()


def test_m4_6_windows_route_uses_powershell_51_and_argv_safe_quoting(monkeypatch):
    captured = {}

    def fake_run(command, **kwargs):
        captured["command"] = command
        return type("Result", (), {"returncode": 0})()

    monkeypatch.setattr(ci_journey.sys, "platform", "win32")
    monkeypatch.setattr(ci_journey.sys, "executable", r"C:\Program Files\Python\python.exe")
    monkeypatch.setattr(ci_journey.subprocess, "run", fake_run)
    args = ["m4-6", "--slot-id", "direct-windows", "--candidate", r"C:\Input Dir\it's.json",
            "--fixture", r"C:\Fixture Dir\release0", "--output", r"C:\Out Dir\result.json",
            "--work-root", r"C:\Temp Dir\work", "--agent-repo", r"C:\Agent's Repo",
            "--release0-runtime-repo", r"C:\Runtime Repo"]

    assert ci_journey._shared_executor_main(args) == 0
    command = captured["command"]
    assert command[:5] == ["powershell.exe", "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass"]
    script = command[-1]
    assert "C:\\Program Files\\Python\\python.exe" in script
    assert "C:\\Input Dir\\it's.json" not in script
    assert "C:\\Input Dir\\it''s.json" in script
    assert "C:\\Agent''s Repo" in script


def test_m4_4b_local_aggregate_fails_closed_when_missing_or_hosted(tmp_path):
    assert check_journey_evidence.evaluate_m4_4b_local_smoke(tmp_path)
    path = tmp_path / "m4-4b-local-result.json"
    path.write_text(json.dumps({"receipt_kind": "m4-4b-fully-local-editor-journey",
                                "evidence_class": "hosted-linux", "status": "pass"}), encoding="utf-8")
    assert check_journey_evidence.evaluate_m4_4b_local_smoke(tmp_path)


def test_workflow_explicitly_wires_both_local_families_and_hosted_m4_6_gate():
    workflow = (Path(__file__).parents[1] / ".github/workflows/journey.yml").read_text(encoding="utf-8")
    for fragment in ("m4_4b_enabled", "m4_6_enabled", "m4_6_candidate", "m4_6_matrix",
                     "m4_4b_local:", "failure_modes:", "m4-4b-local-m4-4b-fully-local-linux-smoke",
                     "m4-6-local-${{",
                     "--aggregate-m4-4b-local", "--aggregate-m4-6-local",
                     "check_failure_receipts.py", "m4-6-hosted-needs.json"):
        assert fragment in workflow
    assert "powershell.exe" in workflow or "powershell5_command" in workflow
    assert journey_failure_modes.candidate_errors(PROVISIONAL_CANDIDATE) == []

