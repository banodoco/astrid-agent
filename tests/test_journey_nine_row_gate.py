"""The Release-1 aggregate is the nine-row direct run, and every row's leg is validated by its own assertions.

Nine rows: three fresh-install rows (linux and macOS fresh, plus Windows), two r0-handoff-then-update rows,
two release9-then-install rows and two release11-then-update rows. The aggregate rejects a run missing any
one of them. The release9-then-install rows are strict: their own receipt fields must hold.
"""
import importlib.util
import json
import shutil
from pathlib import Path

import pytest

ROOT = Path(__file__).parents[1]
spec = importlib.util.spec_from_file_location("journey_evidence", ROOT / "scripts/check_journey_evidence.py")
gate = importlib.util.module_from_spec(spec)
spec.loader.exec_module(gate)

# The five-row fixture builder, loaded by path (it writes every row of gate.RELEASE1_MATRIX).
rows_spec = importlib.util.spec_from_file_location("journey_evidence_rows", Path(__file__).parent / "test_journey_evidence.py")
rows = importlib.util.module_from_spec(rows_spec)
rows_spec.loader.exec_module(rows)

NINE = {
    # job_id: (leg, os, runner)
    "direct-linux-fresh": ("fresh-install", "linux", "ubuntu-24.04"),
    "direct-macos-fresh": ("fresh-install", "macos", "macos-15"),
    "direct-windows": ("fresh-install", "windows", "windows-2022"),
    "direct-linux": ("r0-handoff-then-update", "linux", "ubuntu-24.04"),
    "direct-macos": ("r0-handoff-then-update", "macos", "macos-15"),
    "direct-linux-r9-install": ("release9-then-install", "linux", "ubuntu-24.04"),
    "direct-macos-r9-install": ("release9-then-install", "macos", "macos-15"),
    "direct-linux-r11-update": ("release11-then-update", "linux", "ubuntu-24.04"),
    "direct-macos-r11-update": ("release11-then-update", "macos", "macos-15"),
    "direct-linux-autostart-off": ("fresh-install-autostart-off", "linux", "ubuntu-24.04"),
    "direct-macos-autostart-off": ("fresh-install-autostart-off", "macos", "macos-15"),
}


def mutate(tmp_path, job_id, change):
    path = tmp_path / job_id / "journey-result.json"
    row = json.loads(path.read_text(encoding="utf-8"))
    change(row)
    path.write_text(json.dumps(row), encoding="utf-8")


def test_the_release1_matrix_is_exactly_the_nine_direct_rows():
    matrix = {row["job_id"]: (row["leg"], row["os"], row["runner"]) for row in gate.RELEASE1_MATRIX}
    assert matrix == NINE
    assert len(gate.RELEASE1_MATRIX) == 11
    assert all(row["mode"] == "direct" for row in gate.RELEASE1_MATRIX)
    assert set(gate.RELEASE1_JOB_IDS) == set(NINE)


def test_a_complete_nine_row_run_passes_the_aggregate(tmp_path):
    candidate = rows.release1_receipts(tmp_path)
    assert gate.evaluate_release1_matrix(candidate, tmp_path) == []


@pytest.mark.parametrize("job_id", sorted(NINE))
def test_the_aggregate_rejects_a_run_missing_any_one_of_the_nine(tmp_path, job_id):
    candidate = rows.release1_receipts(tmp_path)
    (tmp_path / job_id / "journey-result.json").unlink()
    assert gate.evaluate_release1_matrix(candidate, tmp_path) == [f"{job_id}: missing receipt"]


def test_release9_rows_carry_the_release9_kind_and_their_own_leg():
    for job_id in ("direct-linux-r9-install", "direct-macos-r9-install"):
        contract = next(row for row in gate.RELEASE1_MATRIX if row["job_id"] == job_id)
        assert contract["leg"] == "release9-then-install"
        assert gate.release1_receipt_kind(contract) == "release1-installed-lifecycle"


R9_BREAKERS = [
    (lambda row: row["release9_source"].update(app_commit="e" * 40),
     "pinned release-9 source identity is absent or wrong"),
    (lambda row: row["release_transition"].update({"from": 10}),
     "release 9 to candidate transition is absent or wrong"),
    (lambda row: row["health_transition"].update(candidate_runtime_instance_id="old"),
     "the candidate Runtime instance did not replace the release-9 one"),
    (lambda row: row["checks"].update(release="not-run"), "release assertion did not pass"),
    (lambda row: row["checks"].update(task_settled="not-run"), "task_settled assertion did not pass"),
    (lambda row: row["semantic_data"].update(after_sha256="c" * 64),
     "semantic data preservation is absent or unequal"),
]


@pytest.mark.parametrize(("change", "expected"), R9_BREAKERS)
@pytest.mark.parametrize("job_id", ["direct-linux-r9-install", "direct-macos-r9-install"])
def test_a_release9_row_is_rejected_when_one_of_its_assertions_is_missing(tmp_path, job_id, change, expected):
    candidate = rows.release1_receipts(tmp_path)
    mutate(tmp_path, job_id, change)
    errors = gate.evaluate_release1_matrix(candidate, tmp_path)
    assert any(error.startswith(f"{job_id}: ") and expected in error for error in errors), errors
    assert all(error.startswith(f"{job_id}: ") for error in errors), errors


def test_a_release9_row_cannot_carry_the_r0_handoff_kind_or_the_r0_scenario(tmp_path):
    candidate = rows.release1_receipts(tmp_path)
    mutate(tmp_path, "direct-linux-r9-install", lambda row: row.update(receipt_kind="r0-handoff-then-update"))
    assert "direct-linux-r9-install: not a Release-1 lifecycle receipt" in gate.evaluate_release1_matrix(
        candidate, tmp_path)
    mutate(tmp_path, "direct-linux-r9-install", lambda row: row.update(
        receipt_kind="release1-installed-lifecycle", scenario="r0-handoff-then-update"))
    assert "direct-linux-r9-install: scenario does not match the required matrix leg" in gate.evaluate_release1_matrix(
        candidate, tmp_path)


def test_the_r0_handoff_rows_keep_their_own_strict_checks(tmp_path):
    candidate = rows.release1_receipts(tmp_path)
    mutate(tmp_path, "direct-linux", lambda row: row.pop("handoff"))
    assert any(error.startswith("direct-linux: ") for error in gate.evaluate_release1_matrix(candidate, tmp_path))


AUTOMATIC_FIVE = ("direct-linux", "direct-macos", "direct-windows", "direct-linux-fresh", "direct-macos-fresh")


def test_an_automatic_run_is_the_five_rows_and_a_direct_run_is_the_nine(tmp_path):
    candidate = rows.release1_receipts(tmp_path)
    automatic = tmp_path / "automatic"
    for job_id in AUTOMATIC_FIVE:
        shutil.copytree(tmp_path / job_id, automatic / job_id)
    assert gate.evaluate_release1_matrix(candidate, automatic, scope="five") == []
    # The nine-row scope needs the release9 and release11 rows too.
    errors = gate.evaluate_release1_matrix(candidate, automatic)
    assert sorted(errors) == sorted(f"{job}: missing receipt" for job in NINE if job not in AUTOMATIC_FIVE)
    # The five-row scope rejects the release9 and release11 rows as outside its scope.
    scoped = gate.evaluate_release1_matrix(candidate, tmp_path, scope="five")
    assert len(scoped) == 6 and all("unknown or absent Release-1 job id" in error for error in scoped)


UPGRADE_ROWS = ("direct-linux", "direct-macos", "direct-linux-r9-install", "direct-macos-r9-install",
                "direct-linux-r11-update", "direct-macos-r11-update")


def set_installed(row, installed):
    """Record the installed release the way each upgrade leg does: the transition, and the live identity where present."""
    row["release_transition"]["to"] = installed
    if "candidate_live_release" in (row.get("health_transition") or {}):
        row["health_transition"]["candidate_live_release"] = installed


@pytest.mark.parametrize("job_id", UPGRADE_ROWS)
@pytest.mark.parametrize("installed", [11, 13])
def test_an_upgrade_row_installing_a_release_other_than_the_candidate_is_rejected(tmp_path, job_id, installed):
    candidate = rows.release1_receipts(tmp_path)
    mutate(tmp_path, job_id, lambda row: set_installed(row, installed))
    errors = gate.evaluate_release1_matrix(candidate, tmp_path)
    assert f"{job_id}: installed release {installed} is not the candidate release 12" in errors


@pytest.mark.parametrize("job_id", UPGRADE_ROWS)
def test_an_upgrade_row_installing_the_candidate_release_is_accepted(tmp_path, job_id):
    candidate = rows.release1_receipts(tmp_path)
    mutate(tmp_path, job_id, lambda row: set_installed(row, 12))
    assert gate.evaluate_release1_matrix(candidate, tmp_path) == []


def test_the_candidate_release_is_read_from_the_candidate_manifest(tmp_path):
    candidate = rows.release1_receipts(tmp_path)
    del candidate["release"]
    errors = gate.evaluate_release1_matrix(candidate, tmp_path)
    assert "direct-linux: candidate release is absent from the candidate manifest" in errors
