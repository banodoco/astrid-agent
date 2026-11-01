"""Synthetic acceptance tests for the single installed-fault evidence gate.

These fixtures describe evidence envelopes only. They do not execute an
installed journey and must never be treated as execution receipts.
"""
import copy
import hashlib
import json
from pathlib import Path

import pytest

from scripts import check_journey_evidence as gate
from scripts import journey_matrix


SCENARIOS = (
    journey_matrix.DIRECT_LINUX_FAULT_SCENARIOS
    + journey_matrix.DIRECT_MACOS_FAULT_SCENARIOS
    + journey_matrix.DIRECT_WINDOWS_FAULT_SCENARIOS
)
GUARD_SCENARIOS = journey_matrix.APP_UPDATE_GUARD_MATRIX
SCENARIO_IDS = tuple(row["scenario_id"] for row in SCENARIOS + GUARD_SCENARIOS)
TUPLE = {key: f"{letter}123456789012345678901234567890123456789"
         for key, letter in zip(("app", "agent", "runtime", "pinokio"), "abcd")}
MANIFEST_SHA256 = "f" * 64


def _manifest():
    return {"agent": {"sha": TUPLE["agent"]},
            "runtime": {"sha": TUPLE["runtime"]}}


def _write_receipt(root, row, name=None):
    path = root / (name or row["scenario_id"]) / "journey-result.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(row, sort_keys=True), encoding="utf-8")
    return path


def _runner(contract):
    if contract["runner"] == "ubuntu-24.04":
        return "Linux", "ubuntu24", "Linux-6.8.0"
    if contract["runner"] == "macos-14":
        return "macOS", "macos14", "macOS-14.7-arm64"
    return "Windows", "win22", "Windows-Server-2022"


def _positive_fault_receipt(contract, root):
    runner_os, image_os, os_build = _runner(contract)
    scenario_name = contract.get("scenario", contract.get("leg"))
    step_evidence = {}
    operation_id = f"operation-{contract['scenario_id']}"
    baseline_data = "1" * 64
    candidate_program = "2" * 64
    predecessor_program = "3" * 64
    sentinel = {"pid": 1234, "birth": "synthetic-birth", "alive": True}
    for index, step in enumerate(contract["steps"]):
        payload = f"synthetic:{contract['scenario_id']}:{step}".encode()
        evidence_root = root / contract["scenario_id"]
        evidence_root.mkdir(parents=True, exist_ok=True)
        sidecar = evidence_root / f"{step}.json"
        sidecar.write_bytes(payload)
        row = {"command": ["synthetic", step], "command_exit": 0,
               "inner_exit": 1 if step in {"interrupt", "fail_candidate"} else 0,
               "evidence": [{"path": sidecar.name,
                             "sha256": hashlib.sha256(payload).hexdigest()}],
               "installation_id": "synthetic-install",
               "candidate_sha256": candidate_program}
        if step == "baseline":
            row.update(direct_release0_fixture=True, native_service_healthy=True,
                       populated_data=True, predecessor_sha256=predecessor_program,
                       data_sha256=baseline_data, sentinel=sentinel,
                       authentic_predecessor=True,
                       selected_sha256=predecessor_program)
            fixture_hashes = {}
            row["evidence"].clear()
            for label in ("fixture-manifest", "fixture-provenance"):
                fixture_payload = f"synthetic:{contract['scenario_id']}:{label}".encode()
                fixture_file = evidence_root / f"{label}.json"
                fixture_file.write_bytes(fixture_payload)
                fixture_digest = hashlib.sha256(fixture_payload).hexdigest()
                row["evidence"].append({"path": fixture_file.name,
                                         "sha256": fixture_digest})
                fixture_hashes[label] = fixture_digest
        elif step in {"interrupt", "fail_candidate"}:
            row.update(operation_id=operation_id, fault_observed=True)
            if step == "interrupt":
                row["journal_step"] = "switched"
            else:
                row.update(journal_step="rolled_back", candidate_started=True,
                           terminal_operation_id=operation_id)
        elif step == "recover":
            row.update(operation_id=operation_id, journal_step="complete")
        else:
            row.update(native_service_healthy=True, data_sha256=baseline_data,
                       sentinel=sentinel,
                       selected_sha256=(predecessor_program if scenario_name == "rollback"
                                        else candidate_program))
        step_evidence[step] = row
    return {
        "receipt_kind": "installed-lifecycle-scenario",
        "execution_scope": "hosted-installed",
        "evidence_class": "installed_native",
        "scenario_id": contract["scenario_id"],
        "status": "pass",
        "source_override_test_mode": False,
        "runner_image": contract["runner"],
        "shell": contract["shell"],
        "runner_provenance": {"RUNNER_OS": runner_os, "ImageOS": image_os,
                              "ImageVersion": "synthetic-20261101.1"},
        "os_build": os_build,
        "candidate_tuple": copy.deepcopy(TUPLE),
        "installed_tuple": copy.deepcopy(TUPLE),
        "manifest_sha256": MANIFEST_SHA256,
        "sources": {"app": {"sha": TUPLE["app"]}, "agent": _manifest()["agent"],
                    "runtime": _manifest()["runtime"]},
        "mode": contract["mode"],
        "final_cleanup": "pass",
        "predicates": {name: True for name in contract["required_evidence"]},
        "service_manager": {"name": contract.get("manager", "systemd"),
                            "native": True, "healthy": True,
                            "foreground_fallback": False, "one_runtime": True},
        "cleanup": {"keep_data": True, "owned_processes_stopped": True},
        "steps": step_evidence,
        "release0_fixture": {"authenticated": True,
                              "manifest_sha256": fixture_hashes["fixture-manifest"],
                              "provenance_sha256": fixture_hashes["fixture-provenance"]},
    }


def _positive_guard_receipt(contract, root):
    evidence_root = root / contract["scenario_id"]
    evidence_root.mkdir(parents=True, exist_ok=True)
    installation_id = "synthetic-installed-app"
    phases = []
    for phase in contract["phases"]:
        payload = f"synthetic:{contract['scenario_id']}:{phase['phase_id']}".encode()
        sidecar = evidence_root / f"{phase['phase_id']}.json"
        sidecar.write_bytes(payload)
        phases.append({
            "phase_id": phase["phase_id"], "save_state": phase["save_state"],
            "status": "pass", "update_post_count": phase["expected_update_post_count"],
            "installation_id": installation_id,
            "command": ["synthetic", phase["phase_id"]], "command_exit": 0,
            "inner_exit": 0,
            "evidence": [{"path": sidecar.name,
                          "sha256": hashlib.sha256(payload).hexdigest()}],
        })
    return {
        "receipt_kind": "installed-lifecycle-scenario",
        "execution_scope": "hosted-installed",
        "evidence_class": contract["evidence_class"],
        "scenario_id": contract["scenario_id"],
        "status": "pass",
        "source_override_test_mode": False,
        "runner_image": contract["runner"],
        "shell": contract["shell"],
        "runner_provenance": {"RUNNER_OS": contract["runner_os"],
                              "ImageOS": contract["image_os"],
                              "ImageVersion": "synthetic-20261101.1"},
        "os_build": "Linux-6.8.0",
        "candidate_tuple": copy.deepcopy(TUPLE),
        "installed_tuple": copy.deepcopy(TUPLE),
        "manifest_sha256": MANIFEST_SHA256,
        "sources": {"app": {"sha": TUPLE["app"]}, "agent": _manifest()["agent"],
                    "runtime": _manifest()["runtime"]},
        "final_cleanup": "pass",
        "installed_app_identity": {"app_sha": TUPLE["app"],
                                   "installation_id": installation_id},
        "phases": phases,
    }


def _positive_receipts(root):
    rows = [_positive_fault_receipt(row, root) for row in SCENARIOS]
    rows.extend(_positive_guard_receipt(row, root) for row in GUARD_SCENARIOS)
    for row in rows:
        _write_receipt(root, row)
    return rows


def _evaluate(root):
    # The checker derives all seven required IDs from the sole canonical matrix.
    candidate = {**TUPLE, "manifest_sha256": MANIFEST_SHA256}
    return gate.evaluate_installed_faults(candidate, _manifest(), root)


def test_complete_synthetic_receipts_cover_all_seven_matrix_scenarios(tmp_path):
    rows = _positive_receipts(tmp_path)

    assert len(rows) == 7
    assert {row["scenario_id"] for row in rows} == set(SCENARIO_IDS)
    assert _evaluate(tmp_path) == []


@pytest.mark.parametrize("mutation", ["missing", "duplicate", "unknown", "skipped"])
def test_receipt_set_must_be_complete_unique_known_and_passing(tmp_path, mutation):
    rows = _positive_receipts(tmp_path)
    victim = rows[0]
    path = tmp_path / victim["scenario_id"] / "journey-result.json"

    if mutation == "missing":
        path.unlink()
    elif mutation == "duplicate":
        _write_receipt(tmp_path, copy.deepcopy(victim), "duplicate-copy")
    elif mutation == "unknown":
        extra = copy.deepcopy(victim)
        extra["scenario_id"] = "not-in-canonical-matrix"
        _write_receipt(tmp_path, extra, "unknown-copy")
    else:
        bad = copy.deepcopy(victim)
        bad["status"] = "skipped"
        _write_receipt(tmp_path, bad)

    assert _evaluate(tmp_path), mutation


@pytest.mark.parametrize(
    "field,mutate",
    [
        ("candidate_tuple", lambda row: row["candidate_tuple"].update(app="e" * 40)),
        ("manifest_sha256", lambda row: row.update(manifest_sha256="0" * 64)),
        ("runner", lambda row: row.update(runner_image="ubuntu-22.04")),
        ("installed_tuple", lambda row: row["installed_tuple"].update(runtime="e" * 40)),
    ],
)
def test_tuple_manifest_runner_and_installed_identity_mismatches_fail_closed(
    tmp_path, field, mutate
):
    rows = _positive_receipts(tmp_path)
    fault_row = copy.deepcopy(rows[0])
    mutate(fault_row)
    _write_receipt(tmp_path, fault_row)

    assert _evaluate(tmp_path), field


@pytest.mark.parametrize("field", ["command_exit", "inner_exit", "evidence"])
def test_each_fault_step_requires_exit_and_hash_bound_evidence(tmp_path, field):
    rows = _positive_receipts(tmp_path)
    row = copy.deepcopy(rows[0])
    step = next(iter(row["steps"].values()))
    if field == "evidence":
        step.pop("evidence")
    else:
        step.pop(field)
    _write_receipt(tmp_path, row)

    assert _evaluate(tmp_path), field


@pytest.mark.parametrize(
    "phase_id,wrong_count",
    [("unresolved-save", 1), ("settled-save", 0), ("settled-save", 2)],
)
def test_app_guard_enforces_exact_update_post_count_per_save_state(
    tmp_path, phase_id, wrong_count
):
    _positive_receipts(tmp_path)
    guard = _positive_guard_receipt(GUARD_SCENARIOS[0], tmp_path)
    phase = next(item for item in guard["phases"] if item["phase_id"] == phase_id)
    phase["update_post_count"] = wrong_count
    _write_receipt(tmp_path, guard)

    assert _evaluate(tmp_path), f"{phase_id} count={wrong_count}"


def test_app_guard_requires_per_phase_outbox_observation(tmp_path):
    _positive_receipts(tmp_path)
    guard = _positive_guard_receipt(GUARD_SCENARIOS[0], tmp_path)
    guard["phases"][0].pop("evidence")
    _write_receipt(tmp_path, guard)

    assert _evaluate(tmp_path)
