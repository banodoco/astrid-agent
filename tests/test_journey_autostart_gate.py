"""The fresh-install-autostart-off rows: the eleven-row aggregate requires them, and the gate checks the
autostart assertions by name, including the App capture at deviceScaleFactor 2.
"""
import importlib.util
import json
from pathlib import Path

import pytest

ROOT = Path(__file__).parents[1]
spec = importlib.util.spec_from_file_location("journey_evidence", ROOT / "scripts/check_journey_evidence.py")
gate = importlib.util.module_from_spec(spec)
spec.loader.exec_module(gate)

rows_spec = importlib.util.spec_from_file_location("journey_evidence_rows", Path(__file__).parent / "test_journey_evidence.py")
rows = importlib.util.module_from_spec(rows_spec)
rows_spec.loader.exec_module(rows)

AUTOSTART_ROWS = ("direct-linux-autostart-off", "direct-macos-autostart-off")
ELEVEN = (
    "direct-linux-fresh", "direct-macos-fresh", "direct-windows",
    "direct-linux", "direct-macos",
    "direct-linux-r9-install", "direct-macos-r9-install",
    "direct-linux-r11-update", "direct-macos-r11-update",
    *AUTOSTART_ROWS,
)


def mutate(tmp_path, job_id, change):
    path = tmp_path / job_id / "journey-result.json"
    row = json.loads(path.read_text(encoding="utf-8"))
    change(row)
    path.write_text(json.dumps(row), encoding="utf-8")


def test_the_release1_matrix_is_the_eleven_direct_rows_with_the_two_autostart_rows():
    assert {row["job_id"] for row in gate.RELEASE1_MATRIX} == set(ELEVEN)
    assert len(gate.RELEASE1_MATRIX) == 11
    autostart = {row["job_id"]: row for row in gate.RELEASE1_MATRIX if row["job_id"] in AUTOSTART_ROWS}
    assert autostart["direct-linux-autostart-off"]["runner"] == "ubuntu-24.04"
    assert autostart["direct-macos-autostart-off"]["runner"] == "macos-15"
    assert all(row["leg"] == "fresh-install-autostart-off" for row in autostart.values())


def test_a_complete_eleven_row_run_passes_including_the_release11_and_autostart_rows(tmp_path):
    candidate = rows.release1_receipts(tmp_path)
    assert gate.evaluate_release1_matrix(candidate, tmp_path) == []


@pytest.mark.parametrize("job_id", AUTOSTART_ROWS)
def test_the_aggregate_rejects_a_run_without_either_autostart_row(tmp_path, job_id):
    candidate = rows.release1_receipts(tmp_path)
    (tmp_path / job_id / "journey-result.json").unlink()
    assert gate.evaluate_release1_matrix(candidate, tmp_path) == [f"{job_id}: missing receipt"]


BREAKERS = [
    (lambda row: row["selection"].update(choices_autostart=True),
     "selection record does not have choices.autostart false"),
    (lambda row: row["update_status"].update(reason="none"),
     "GET /update/status does not report reason autostart-off"),
    (lambda row: row["post_update"].update(unchanged=False),
     "POST update was not refused with autostart-off, or it changed the selection, the data or the live Runtime"),
    (lambda row: row["post_update"].update(reason="busy"),
     "POST update was not refused with autostart-off, or it changed the selection, the data or the live Runtime"),
    (lambda row: row["capture"].update(device_scale_factor=1),
     "autostart card capture is absent or not at deviceScaleFactor 2"),
    (lambda row: row["checks"].update(selection_autostart_false="not-run"),
     "selection_autostart_false assertion did not pass"),
    (lambda row: row["checks"].update(update_status_autostart_off="not-run"),
     "update_status_autostart_off assertion did not pass"),
    (lambda row: row["checks"].update(post_update_refused_unchanged="not-run"),
     "post_update_refused_unchanged assertion did not pass"),
    (lambda row: row["checks"].update(autostart_card_capture="not-run"),
     "autostart_card_capture assertion did not pass"),
]


@pytest.mark.parametrize(("change", "expected"), BREAKERS)
@pytest.mark.parametrize("job_id", AUTOSTART_ROWS)
def test_an_autostart_row_is_rejected_when_one_named_assertion_is_missing(tmp_path, job_id, change, expected):
    candidate = rows.release1_receipts(tmp_path)
    mutate(tmp_path, job_id, change)
    errors = gate.evaluate_release1_matrix(candidate, tmp_path)
    assert any(error.startswith(f"{job_id}: ") and expected in error for error in errors), errors
    assert all(error.startswith(f"{job_id}: ") for error in errors), errors


def test_an_autostart_row_cannot_carry_another_legs_receipt_kind(tmp_path):
    candidate = rows.release1_receipts(tmp_path)
    mutate(tmp_path, "direct-linux-autostart-off", lambda row: row.update(receipt_kind="release11-then-update"))
    assert "direct-linux-autostart-off: not a Release-1 lifecycle receipt" in gate.evaluate_release1_matrix(
        candidate, tmp_path)


def test_the_autostart_kind_is_the_leg_own_kind():
    contract = next(row for row in gate.RELEASE1_MATRIX if row["job_id"] == "direct-macos-autostart-off")
    assert gate.release1_receipt_kind(contract) == "fresh-install-autostart-off"
    assert gate.AUTOSTART_CHECKS == (
        "install_exit", "selection_autostart_false", "update_status_autostart_off",
        "post_update_refused_unchanged", "autostart_card_capture", "service", "health", "unrelated_sentinel")


@pytest.mark.parametrize("job_id", ["direct-macos-autostart-off"])
def test_a_macos_capture_may_be_not_gated_when_its_assertions_hold(tmp_path, job_id):
    candidate = rows.release1_receipts(tmp_path)

    def not_gated(row):
        row["checks"]["autostart_card_capture"] = "not-gated"
        row["capture"] = {"status": "not-gated", "reason": "macOS capture not verified in CI", "error": "intercept"}

    mutate(tmp_path, job_id, not_gated)
    assert gate.evaluate_release1_matrix(candidate, tmp_path) == []


def test_a_linux_capture_may_not_be_not_gated(tmp_path):
    candidate = rows.release1_receipts(tmp_path)

    def not_gated(row):
        row["checks"]["autostart_card_capture"] = "not-gated"
        row["capture"] = {"status": "not-gated", "reason": "macOS capture not verified in CI", "error": "intercept"}

    mutate(tmp_path, "direct-linux-autostart-off", not_gated)
    errors = gate.evaluate_release1_matrix(candidate, tmp_path)
    assert "direct-linux-autostart-off: autostart_card_capture assertion did not pass" in errors


def test_a_macos_capture_that_did_not_run_is_still_rejected(tmp_path):
    candidate = rows.release1_receipts(tmp_path)
    mutate(tmp_path, "direct-macos-autostart-off", lambda row: row["checks"].update(autostart_card_capture="not-run"))
    errors = gate.evaluate_release1_matrix(candidate, tmp_path)
    assert "direct-macos-autostart-off: autostart_card_capture assertion did not pass" in errors
