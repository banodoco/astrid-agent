"""The evidence gate knows the release11-then-update leg and rejects it unless all four assertions hold.

The leg's own receipt kind is accepted by the Release-1 aggregate for its two rows. A missing assertion
(installed release 12, updated run receipt, exactly one live Runtime, seeded data preserved) is rejected,
and the five-row Release-1 contract stays exactly as strict as before.
"""
import importlib.util
import json
from pathlib import Path

import pytest

ROOT = Path(__file__).parents[1]
spec = importlib.util.spec_from_file_location("journey_evidence", ROOT / "scripts/check_journey_evidence.py")
gate = importlib.util.module_from_spec(spec)
spec.loader.exec_module(gate)

# The five-row fixture builder lives beside the gate tests; load it by path, not by package import.
rows_spec = importlib.util.spec_from_file_location("journey_evidence_rows", Path(__file__).parent / "test_journey_evidence.py")
rows = importlib.util.module_from_spec(rows_spec)
rows_spec.loader.exec_module(rows)

DATA = "/run/data"
PRINCIPAL_ROW = "direct-linux-r11-update"


def release11_contract(job_id=PRINCIPAL_ROW):
    return next(row for row in gate.RELEASE11_UPDATE_MATRIX if row["job_id"] == job_id)


def release11_receipt(candidate, job_id=PRINCIPAL_ROW):
    contract = release11_contract(job_id)
    runner_os, image_os = gate.RUNNER_PROVENANCE[contract["runner"]]
    bootstrap = {path: "1" * 64 for path in ("public/install", "public/install.sh", "public/install.ps1")}
    return {
        "schema_version": 1, "receipt_kind": gate.release1_receipt_kind(contract), "job_id": job_id,
        "mode": "direct", "scenario": "release11-then-update", "status": "pass",
        "runner_image": contract["runner"], "shell": contract["shell"], "source_override_test_mode": False,
        "final_cleanup": "pass", "os_build": runner_os + "-test",
        "runner_provenance": {"RUNNER_OS": runner_os, "ImageOS": image_os, "ImageVersion": "test"},
        "candidate_checkouts": {name: {"sha": sha} for name, sha in candidate.items()},
        "run_roots": {"program": "/run/program", "data": DATA},
        "sources": {name: {"sha": candidate[name]} for name in ("app", "agent", "runtime")},
        "installed_tuple": {name: candidate[name] for name in ("app", "agent", "runtime")},
        "import_origins": {"runtime": "/owned/runtime"},
        "service_manager_prerequisite": {"required": True, "outcome": "pass", "version": "1",
                                         "foreground_fallback": False},
        "service_manager": {"manager": "native", "foreground_fallback": False},
        "release11_source": {"app_commit": gate.RELEASE11_APP_COMMIT, "manifest_sha256": "e" * 64,
                             "bootstrap_sha256": bootstrap, "identity": gate.RELEASE11_IDENTITY},
        "release_transition": {"from": 11, "to": 12, "source": "candidate-install-update"},
        "health_transition": {"candidate_live_release": 12},
        "update": {"command": "install --update", "exit_code": 0,
                   "receipt": {"kind": "updated", "exit_code": 0, "start": 11, "still": 12, "target": 12}},
        "live_runtimes": [{"data_root": DATA, "pid": 1, "runtime_instance_id": "new"}],
        "semantic_data": {"identical": True, "before_sha256": "b" * 64, "after_sha256": "b" * 64},
        "checks": {name: "pass" for name in gate.RELEASE11_CHECKS},
    }


def write_receipt(root, row):
    path = root / row["job_id"] / "journey-result.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(row), encoding="utf-8")


def aggregate(tmp_path, *receipts):
    """The five-row Release-1 receipt set, plus any release11 receipts, through the aggregate."""
    candidate = rows.release1_receipts(tmp_path)
    for row in receipts:
        write_receipt(tmp_path, row)
    return gate.evaluate_release1_matrix(candidate, tmp_path), candidate


def test_release11_rows_are_accepted_by_the_release1_aggregate_when_all_four_assertions_hold(tmp_path):
    candidate = rows.release1_receipts(tmp_path)
    for contract in gate.RELEASE11_UPDATE_MATRIX:
        write_receipt(tmp_path, release11_receipt(candidate, contract["job_id"]))
    assert gate.evaluate_release1_matrix(candidate, tmp_path) == []


def test_release11_rows_are_required_by_the_nine_row_aggregate(tmp_path):
    candidate = rows.release1_receipts(tmp_path)
    assert gate.evaluate_release1_matrix(candidate, tmp_path) == []
    (tmp_path / "direct-linux-r11-update" / "journey-result.json").unlink()
    errors = gate.evaluate_release1_matrix(candidate, tmp_path)
    assert errors == ["direct-linux-r11-update: missing receipt"]


def test_release1_aggregate_expects_the_nine_rows_including_windows_and_both_fresh_rows():
    assert set(gate.RELEASE1_JOB_IDS) == {
        "direct-linux", "direct-macos", "direct-windows", "direct-linux-fresh", "direct-macos-fresh",
        "direct-linux-r9-install", "direct-macos-r9-install",
        "direct-linux-r11-update", "direct-macos-r11-update",
        "direct-linux-autostart-off", "direct-macos-autostart-off"}
    assert {row["job_id"] for row in gate.RELEASE11_UPDATE_MATRIX} <= set(gate.RELEASE1_JOB_IDS)


def test_release11_receipt_kind_is_the_leg_own_kind():
    contract = release11_contract()
    assert gate.release1_receipt_kind(contract) == "release11-then-update"
    assert gate.RELEASE1_RECEIPT_KINDS["release11-then-update"] == "release11-then-update"
    assert "release11-then-update" in gate.RELEASE1_RECEIPT_KINDS.values()


@pytest.mark.parametrize(("breaker", "expected"), [
    # 1. installed release 12: the transition to 12 and the live identity must both say so.
    (lambda row: row.update(health_transition={"candidate_live_release": 11}),
     "installed release is not 12"),
    (lambda row: row.update(release_transition={"from": 11, "to": 11,
                                                "source": "candidate-install-update"}),
     "installed release is not 12"),
    # 2. the candidate's run receipt says updated, exit 0, still and target 12.
    (lambda row: row["update"]["receipt"].update(kind="not-updated"),
     "candidate install --update receipt does not say updated to release 12"),
    (lambda row: row["update"]["receipt"].update(still=11),
     "candidate install --update receipt does not say updated to release 12"),
    # 3. exactly one Runtime is live afterwards.
    (lambda row: row.update(live_runtimes=[]),
     "exactly one live Runtime is not recorded after the update"),
    (lambda row: row.update(live_runtimes=[{"data_root": DATA}, {"data_root": DATA + "-r1"}]),
     "exactly one live Runtime is not recorded after the update"),
    # 4. the seeded user data survives the update.
    (lambda row: row["semantic_data"].update(after_sha256="c" * 64),
     "seeded user data preservation is absent or unequal"),
    (lambda row: row.pop("semantic_data"),
     "seeded user data preservation is absent or unequal"),
])
def test_release11_row_is_rejected_when_one_of_the_four_assertions_is_missing(tmp_path, breaker, expected):
    candidate = rows.release1_receipts(tmp_path)
    row = release11_receipt(candidate)
    breaker(row)
    write_receipt(tmp_path, row)
    errors = gate.evaluate_release1_matrix(candidate, tmp_path)
    assert any(error.startswith(f"{PRINCIPAL_ROW}: ") and expected in error for error in errors), errors
    assert all(error.startswith(f"{PRINCIPAL_ROW}: ") for error in errors), errors


@pytest.mark.parametrize("check", ["release11_install", "run_receipt", "release", "one_runtime",
                                   "semantic_data", "task_settled"])
def test_release11_row_is_rejected_when_a_named_check_did_not_pass(tmp_path, check):
    candidate = rows.release1_receipts(tmp_path)
    row = release11_receipt(candidate)
    row["checks"][check] = "not-run"
    write_receipt(tmp_path, row)
    errors = gate.evaluate_release1_matrix(candidate, tmp_path)
    assert f"{PRINCIPAL_ROW}: {check} assertion did not pass" in errors


def test_release11_row_must_name_the_pinned_release11_source(tmp_path):
    candidate = rows.release1_receipts(tmp_path)
    row = release11_receipt(candidate)
    row["release11_source"]["app_commit"] = "e" * 40
    write_receipt(tmp_path, row)
    errors = gate.evaluate_release1_matrix(candidate, tmp_path)
    assert f"{PRINCIPAL_ROW}: pinned release-11 source identity is absent or wrong" in errors


def test_a_release11_leg_cannot_be_submitted_as_a_five_row_receipt_or_the_reverse(tmp_path):
    candidate = rows.release1_receipts(tmp_path)
    for contract in gate.RELEASE11_UPDATE_MATRIX:
        write_receipt(tmp_path, release11_receipt(candidate, contract["job_id"]))
    assert gate.evaluate_release1_matrix(candidate, tmp_path) == []
    # A release1 kind on the release11 row is not that leg's receipt.
    lifecycle = release11_receipt(candidate)
    lifecycle["receipt_kind"] = "release1-installed-lifecycle"
    write_receipt(tmp_path, lifecycle)
    assert f"{PRINCIPAL_ROW}: not a Release-1 lifecycle receipt" in gate.evaluate_release1_matrix(candidate, tmp_path)
    # The r0 handoff kind on the release11 row is not that leg's receipt either.
    write_receipt(tmp_path, dict(lifecycle, receipt_kind="r0-handoff-then-update"))
    assert f"{PRINCIPAL_ROW}: not a Release-1 lifecycle receipt" in gate.evaluate_release1_matrix(candidate, tmp_path)
    # The release11 kind on a five-row receipt is rejected as that row's kind.
    original = json.loads((tmp_path / "direct-linux" / "journey-result.json").read_text())
    write_receipt(tmp_path, dict(original, job_id="direct-linux", receipt_kind="release11-then-update"))
    assert "direct-linux: not a Release-1 lifecycle receipt" in gate.evaluate_release1_matrix(candidate, tmp_path)


def test_a_release11_row_with_the_wrong_scenario_is_rejected(tmp_path):
    candidate = rows.release1_receipts(tmp_path)
    row = release11_receipt(candidate)
    row["scenario"] = "fresh-install"
    write_receipt(tmp_path, row)
    errors = gate.evaluate_release1_matrix(candidate, tmp_path)
    assert f"{PRINCIPAL_ROW}: scenario does not match the required matrix leg" in errors
