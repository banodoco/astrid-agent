"""Focused inventory and installed-fault adapter contract controls."""

import hashlib
import json

import pytest

from scripts import journey_matrix as matrix
from scripts.failure_mode_executor import execute_installed_fault


BASELINE_JOBS = (
    ("direct-macos", "direct", "macos-14", "sh", "macos", "release0-to-release1"),
    ("direct-linux", "direct", "ubuntu-24.04", "sh", "linux", "release0-to-release1"),
    ("direct-windows", "direct", "windows-2022", "powershell.exe", "windows", "release0-to-release1"),
    ("pinokio-backend-macos", "pinokio-backend", "macos-14", "sh", "macos", None),
    ("pinokio-backend-linux", "pinokio-backend", "ubuntu-24.04", "sh", "linux", None),
    ("pinokio-backend-windows", "pinokio-backend", "windows-2022", "powershell.exe", "windows", None),
    ("pinokio-fully-local-macos", "pinokio-fully-local", "macos-14", "sh", "macos", None),
    ("pinokio-fully-local-linux", "pinokio-fully-local", "ubuntu-24.04", "sh", "linux", None),
    ("pinokio-fully-local-windows", "pinokio-fully-local", "windows-2022", "powershell.exe", "windows", None),
)


def test_inventory_preserves_existing_rows_and_uniquely_names_linux_faults():
    keys = ("job_id", "mode", "runner", "shell", "os", "leg")
    expected = tuple({key: value for key, value in zip(keys, row) if value is not None}
                     for row in BASELINE_JOBS)
    baseline_ids = {row["job_id"] for row in expected}
    assert tuple(row for row in matrix.RELEASE1_MATRIX if row["job_id"] in baseline_ids) == expected
    linux_fault_ids = {"direct-linux-interrupted-update", "direct-linux-rollback"}
    rows = matrix.DIRECT_LINUX_FAULT_SCENARIOS
    assert tuple(row for row in matrix.RELEASE1_MATRIX if row["job_id"] in linux_fault_ids) == rows
    assert [row["scenario_id"] for row in rows] == ["direct-linux-interrupted-update", "direct-linux-rollback"]
    ids = [row["job_id"] for row in matrix.RELEASE1_MATRIX] + list(matrix.M4_5_SCENARIO_IDS)
    assert len(ids) == len(set(ids))
    for row in rows:
        assert (row["mode"], row["os"], row["runner"], row["shell"]) == ("direct", "linux", "ubuntu-24.04", "sh")
        assert {"direct-installed-release0-fixture-and-native-service", "populated-semantic-data-and-live-sentinel-before", "unchanged-semantic-data-and-same-live-sentinel-after", "per-step-command-and-inner-exits-and-sha256-evidence-binding", "candidate-tuple-and-runner-provenance"} <= set(row["required_evidence"])
    assert [row["leg"] for row in rows] == ["interrupted-update", "rollback"]
    assert len(matrix.M4_5_SCENARIO_IDS) == 12
    assert all(row["scenario"] == "release1-to-release2" for row in matrix.M4_5_MATRIX)
    assert rows[0]["steps"] == ("baseline", "interrupt", "recover", "verify")
    assert "durable-interrupted-operation-and-same-operation-terminal-journal" in rows[0]["required_evidence"]
    assert rows[1]["steps"] == ("baseline", "fail_candidate", "verify")
    assert "authentic-selected-release0-predecessor-and-restored-healthy-native-service" in rows[1]["required_evidence"]
    assert "observed-failed-candidate-operation-and-terminal-rollback-journal" in rows[1]["required_evidence"]


class Adapter:
    def __init__(self, root, mutate=None):
        self.root, self.mutate, self.calls = root, mutate, []

    def run_step(self, step, previous):
        self.calls.append(step)
        sentinel = {"pid": 12, "birth": "birth-12", "alive": True}
        row = {"command": ["test-adapter", step], "command_exit": 0,
               "inner_exit": 9 if step in {"interrupt", "fail_candidate"} else 0}
        row["installation_id"] = "installed-root-1"
        row.update({
            "baseline": {"direct_release0_fixture": True, "native_service_healthy": True,
                         "populated_data": True, "predecessor_sha256": "a" * 64,
                         "candidate_sha256": "b" * 64, "data_sha256": "c" * 64, "sentinel": sentinel},
            "interrupt": {"operation_id": "op-1", "candidate_sha256": "b" * 64, "fault_observed": True, "journal_step": "switched"},
            "recover": {"operation_id": "op-1", "journal_step": "complete"},
            "fail_candidate": {"operation_id": "op-1", "terminal_operation_id": "op-1",
                               "candidate_sha256": "b" * 64, "candidate_started": True, "fault_observed": True, "journal_step": "rolled_back"},
            "verify": {"native_service_healthy": True, "data_sha256": "c" * 64, "sentinel": sentinel,
                       "selected_sha256": ("a" if "fail_candidate" in previous else "b") * 64},
        }[step])
        path = self.root / (step + ".json")
        path.write_text(json.dumps(row), encoding="utf-8")
        row["evidence"] = [{"path": path.name, "sha256": hashlib.sha256(path.read_bytes()).hexdigest()}]
        if self.mutate:
            self.mutate(step, row, self.root)
        return row


@pytest.mark.parametrize("scenario,steps", [("interrupted-update", ["baseline", "interrupt", "recover", "verify"]), ("rollback", ["baseline", "fail_candidate", "verify"])])
def test_helper_calls_each_step_and_returns_observations(tmp_path, scenario, steps):
    adapter = Adapter(tmp_path)
    result = execute_installed_fault(scenario, adapter, tmp_path)
    assert adapter.calls == steps
    assert list(result["steps"]) == steps
    assert "status" not in result


@pytest.mark.parametrize("step,field,value", [
    ("baseline", "direct_release0_fixture", None), ("baseline", "populated_data", False),
    ("baseline", "native_service_healthy", None), ("interrupt", "journal_step", "staged"),
    ("interrupt", "inner_exit", 0), ("interrupt", "installation_id", "other"),
    ("interrupt", "candidate_sha256", "d" * 64), ("recover", "operation_id", "other"),
    ("recover", "journal_step", "switched"), ("verify", "data_sha256", "d" * 64),
    ("verify", "sentinel", {"pid": 12, "birth": "reused", "alive": True}),
    ("verify", "native_service_healthy", False), ("verify", "selected_sha256", "a" * 64),
])
def test_interruption_rejects_unobserved_or_inconsistent_state(tmp_path, step, field, value):
    def mutate(current, row, root):
        if current == step:
            row[field] = value
    adapter = Adapter(tmp_path, mutate)
    with pytest.raises(ValueError):
        execute_installed_fault("interrupted-update", adapter, tmp_path)
    if step == "baseline":
        assert adapter.calls == ["baseline"]


@pytest.mark.parametrize("field,value", [("terminal_operation_id", "other"), ("journal_step", "complete"), ("fault_observed", False), ("candidate_started", False)])
def test_rollback_rejects_wrong_operation_or_unobserved_fault(tmp_path, field, value):
    def mutate(step, row, root):
        if step == "fail_candidate":
            row[field] = value
    with pytest.raises(ValueError):
        execute_installed_fault("rollback", Adapter(tmp_path, mutate), tmp_path)


def test_rollback_rejects_wrong_selected_predecessor(tmp_path):
    def mutate(step, row, root):
        if step == "verify":
            row["selected_sha256"] = "b" * 64
    with pytest.raises(ValueError, match="selected installed program"):
        execute_installed_fault("rollback", Adapter(tmp_path, mutate), tmp_path)


@pytest.mark.parametrize("tamper", ["missing", "wrong-digest", "escape", "changed-later"])
def test_evidence_is_required_confined_and_stable(tmp_path, tamper):
    def mutate(step, row, root):
        if step == "baseline" and tamper == "missing":
            row["evidence"] = []
        if step == "baseline" and tamper == "wrong-digest":
            row["evidence"][0]["sha256"] = "d" * 64
        if step == "baseline" and tamper == "escape":
            row["evidence"][0]["path"] = "../outside"
        if step == "verify" and tamper == "changed-later":
            (root / "baseline.json").write_text("changed", encoding="utf-8")
    with pytest.raises(ValueError):
        execute_installed_fault("rollback", Adapter(tmp_path, mutate), tmp_path)
