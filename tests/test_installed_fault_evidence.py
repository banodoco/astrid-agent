"""Synthetic receipts exercise the single installed-fault verdict path."""
import copy
import hashlib
import json

import pytest

from scripts import check_journey_evidence as gate


def receipts(root):
    candidate = {k: c * 40 for k, c in zip(("app", "agent", "runtime", "pinokio"), "abcd")}
    candidate["manifest_sha256"] = "e" * 64
    manifest = {k: {"sha": candidate[k]} for k in ("agent", "runtime")}
    contracts = (*gate.DIRECT_LINUX_FAULT_SCENARIOS, *gate.DIRECT_MACOS_FAULT_SCENARIOS,
                 *gate.DIRECT_WINDOWS_FAULT_SCENARIOS, *gate.APP_UPDATE_GUARD_MATRIX)
    rows = {}
    for contract in contracts:
        name = contract["scenario_id"]
        directory = root / name
        directory.mkdir(parents=True, exist_ok=True)
        os_name, image = gate.RUNNER_PROVENANCE[contract["runner"]]
        row = {"receipt_kind": "installed-lifecycle-scenario", "scenario_id": name,
               "status": "pass", "source_override_test_mode": False, "execution_scope": "hosted-installed",
               "evidence_class": contract.get("evidence_class", "installed_native"),
               "candidate_tuple": {k: candidate[k] for k in ("app", "agent", "runtime", "pinokio")},
               "manifest_sha256": candidate["manifest_sha256"],
               "installed_tuple": {k: candidate[k] for k in ("app", "agent", "runtime", "pinokio")},
               "sources": {"app": {"sha": candidate["app"]}, **copy.deepcopy(manifest)},
               "runner_image": contract["runner"], "shell": contract["shell"],
               "runner_provenance": {"RUNNER_OS": os_name, "ImageOS": image, "ImageVersion": "synthetic"},
               "os_build": os_name + "-synthetic", "final_cleanup": "pass"}
        row["producer_run"] = {"producer": "github-actions:banodoco/astrid-agent", "provider": "github-actions", "repository": "banodoco/astrid-agent",
                               "workflow_sha": "f" * 40, "run_id": "123", "run_attempt": "1",
                               "job_id": str(len(rows) + 1)}
        def observation(label, **fields):
            data = ("synthetic " + label).encode()
            (directory / (label + ".txt")).write_bytes(data)
            return {"command": ["synthetic", label], "command_exit": 0, "inner_exit": 0,
                    "evidence": [{"path": label + ".txt", "sha256": hashlib.sha256(data).hexdigest()}], **fields}
        if "phases" in contract:
            row["installed_app_identity"] = {"app_sha": candidate["app"], "installation_id": "installation"}
            row["phases"] = [observation(p["phase_id"], phase_id=p["phase_id"], save_state=p["save_state"],
                                          status="pass", update_post_count=p["expected_update_post_count"],
                                          installation_id="installation") for p in contract["phases"]]
        else:
            row.update(mode="direct", predicates={k: True for k in contract["required_evidence"]},
                       service_manager={"name": contract.get("manager", "systemd"), "native": True,
                                        "healthy": True, "foreground_fallback": False, "one_runtime": True},
                       cleanup={"keep_data": True, "owned_processes_stopped": True})
            sentinel = {"pid": 42, "birth": "synthetic", "alive": True}
            steps = {"baseline": observation("baseline", installation_id="installation", direct_release0_fixture=True,
                       native_service_healthy=True, populated_data=True, authentic_predecessor=True,
                       predecessor_sha256="1" * 64, candidate_sha256="2" * 64, data_sha256="3" * 64, sentinel=sentinel)}
            steps["baseline"]["selected_sha256"] = "1" * 64
            fixture_manifest = observation("fixture_manifest")["evidence"][0]
            fixture_provenance = observation("fixture_provenance")["evidence"][0]
            steps["baseline"]["evidence"].extend([fixture_manifest, fixture_provenance])
            row["release0_fixture"] = {"authenticated": True, "manifest_sha256": fixture_manifest["sha256"],
                                       "provenance_sha256": fixture_provenance["sha256"]}
            if "interrupt" in contract["steps"]:
                steps["interrupt"] = observation("interrupt", installation_id="installation", candidate_sha256="2" * 64,
                    inner_exit=1, operation_id="op", fault_observed=True, journal_step="switched")
                steps["recover"] = observation("recover", installation_id="installation", operation_id="op", journal_step="complete")
                selected = "2" * 64
            else:
                steps["fail_candidate"] = observation("fail_candidate", installation_id="installation", candidate_sha256="2" * 64,
                    inner_exit=1, operation_id="op", terminal_operation_id="op", fault_observed=True,
                    journal_step="rolled_back", candidate_started=True)
                selected = "1" * 64
            steps["verify"] = observation("verify", installation_id="installation", native_service_healthy=True,
                                           data_sha256="3" * 64, sentinel=sentinel, selected_sha256=selected)
            if "fail_candidate" in steps:
                selected_tuple = {**row["candidate_tuple"], "agent": "1" * 40, "runtime": "2" * 40}
                row["attempted_tuple"] = copy.deepcopy(row["candidate_tuple"])
                row["selected_tuple"] = selected_tuple
                row["installed_tuple"] = copy.deepcopy(selected_tuple)
                steps["baseline"]["selected_tuple"] = copy.deepcopy(selected_tuple)
                steps["verify"]["selected_tuple"] = copy.deepcopy(selected_tuple)
                steps["fail_candidate"]["attempted_tuple"] = copy.deepcopy(row["candidate_tuple"])
            row["steps"] = steps
        rows[name] = row
    return candidate, manifest, rows


def write(root, rows):
    for name, row in rows.items():
        (root / name / "journey-result.json").write_text(json.dumps(row))


def trusted_index(root, candidate, rows):
    return {"candidate_tuple": {k: candidate[k] for k in ("app", "agent", "runtime", "pinokio")},
            "manifest_sha256": candidate["manifest_sha256"],
            "scenarios": {name: {"producer_run": copy.deepcopy(row["producer_run"]),
                                 "receipt_sha256": gate.digest(root / name / "journey-result.json")}
                          for name, row in rows.items()}}


def evaluate(candidate, manifest, root, rows):
    return gate.evaluate_installed_faults(candidate, manifest, root, trusted_index(root, candidate, rows))


@pytest.mark.parametrize("mutation", ["missing_index", "missing_row", "wrong_candidate", "wrong_manifest",
                                      "missing_identity", "forged_identity", "stale_run", "stale_attempt",
                                      "wrong_job", "wrong_workflow", "forged_receipt", "bad_digest"])
def test_producer_run_binding_rejects_unauthenticated_or_stale_evidence(tmp_path, mutation):
    candidate, manifest, rows = receipts(tmp_path)
    write(tmp_path, rows)
    index = trusted_index(tmp_path, candidate, rows)
    name = gate.DIRECT_LINUX_FAULT_SCENARIOS[0]["scenario_id"]
    if mutation == "missing_index": index = None
    elif mutation == "missing_row": del index["scenarios"][name]
    elif mutation == "wrong_candidate": index["candidate_tuple"]["agent"] = "f" * 40
    elif mutation == "wrong_manifest": index["manifest_sha256"] = "f" * 64
    elif mutation == "bad_digest": index["scenarios"][name]["receipt_sha256"] = "0" * 64
    else:
        if mutation == "missing_identity": del rows[name]["producer_run"]
        elif mutation == "forged_identity": rows[name]["producer_run"]["repository"] = "attacker/repo"
        elif mutation == "stale_run": rows[name]["producer_run"]["run_id"] = "122"
        elif mutation == "stale_attempt": rows[name]["producer_run"]["run_attempt"] = "2"
        elif mutation == "wrong_job": rows[name]["producer_run"]["job_id"] = "987"
        elif mutation == "wrong_workflow": rows[name]["producer_run"]["workflow_sha"] = "1" * 40
        elif mutation == "forged_receipt": rows[name]["untrusted_extra"] = "forged"
        write(tmp_path, rows)
        # Isolate identity mismatches from the separate receipt-byte check.
        if mutation != "forged_receipt":
            index["scenarios"][name]["receipt_sha256"] = gate.digest(tmp_path / name / "journey-result.json")
    errors = gate.evaluate_installed_faults(candidate, manifest, tmp_path, index)
    expected_error = ("trusted producer/run index" if mutation in
                      ("missing_index", "missing_row", "wrong_candidate", "wrong_manifest")
                      else "authenticated producer/run identity or receipt binding")
    assert any(expected_error in error for error in errors), (mutation, errors)


@pytest.mark.parametrize("mutation", ["missing_attempt", "wrong_attempt", "missing_selected", "candidate_selected",
                                      "installed", "baseline", "verify", "fault", "malformed"])
def test_rollback_attempted_and_selected_tuples(tmp_path, mutation):
    candidate, manifest, rows = receipts(tmp_path)
    name = gate.DIRECT_LINUX_FAULT_SCENARIOS[1]["scenario_id"]
    row = rows[name]
    if mutation == "missing_attempt": del row["attempted_tuple"]
    elif mutation == "wrong_attempt": row["attempted_tuple"]["agent"] = "9" * 40
    elif mutation == "missing_selected": del row["selected_tuple"]
    elif mutation == "candidate_selected": row["selected_tuple"] = copy.deepcopy(row["candidate_tuple"])
    elif mutation == "installed": row["installed_tuple"] = copy.deepcopy(row["candidate_tuple"])
    elif mutation == "baseline": row["steps"]["baseline"]["selected_tuple"]["agent"] = "9" * 40
    elif mutation == "verify": row["steps"]["verify"]["selected_tuple"]["runtime"] = "9" * 40
    elif mutation == "fault": row["steps"]["fail_candidate"]["attempted_tuple"]["agent"] = "9" * 40
    elif mutation == "malformed": row["selected_tuple"]["agent"] = "bad"
    write(tmp_path, rows)
    assert any("attempted candidate and selected predecessor" in e for e in evaluate(candidate, manifest, tmp_path, rows))


def test_interrupted_update_rolled_back_has_separate_tuples(tmp_path):
    candidate, manifest, rows = receipts(tmp_path)
    row = rows[gate.DIRECT_LINUX_FAULT_SCENARIOS[0]["scenario_id"]]
    predecessor = {**row["candidate_tuple"], "agent": "1" * 40, "runtime": "2" * 40}
    row.update(attempted_tuple=copy.deepcopy(row["candidate_tuple"]), selected_tuple=predecessor,
               installed_tuple=copy.deepcopy(predecessor))
    row["steps"]["baseline"]["selected_tuple"] = copy.deepcopy(predecessor)
    row["steps"]["interrupt"]["attempted_tuple"] = copy.deepcopy(row["candidate_tuple"])
    row["steps"]["recover"]["journal_step"] = "rolled_back"
    row["steps"]["verify"].update(selected_tuple=copy.deepcopy(predecessor), selected_sha256="1" * 64)
    write(tmp_path, rows)
    assert evaluate(candidate, manifest, tmp_path, rows) == []
    del row["selected_tuple"]
    write(tmp_path, rows)
    assert evaluate(candidate, manifest, tmp_path, rows)


def test_synthetic_complete_and_inventory_fail_closed(tmp_path):
    candidate, manifest, rows = receipts(tmp_path)
    write(tmp_path, rows)
    assert evaluate(candidate, manifest, tmp_path, rows) == []
    index = trusted_index(tmp_path, candidate, rows)
    first = next(iter(rows))
    (tmp_path / first / "journey-result.json").unlink()
    assert any("missing receipt" in e for e in gate.evaluate_installed_faults(candidate, manifest, tmp_path, index))
    write(tmp_path, rows)
    extra = tmp_path / "extra"
    extra.mkdir()
    (extra / "journey-result.json").write_text(json.dumps(rows[first]))
    assert any("duplicate" in e for e in evaluate(candidate, manifest, tmp_path, rows))
    (extra / "journey-result.json").write_text(json.dumps({"scenario_id": "unknown"}))
    assert any("unknown" in e for e in evaluate(candidate, manifest, tmp_path, rows))


@pytest.mark.parametrize("mutation", ["skipped", "failed", "candidate", "manifest", "runner", "provenance", "installed", "sources", "cleanup", "manager", "predicate", "baseline", "predecessor", "fixture", "operation", "data", "sentinel", "selected", "command", "exit", "inner", "hash", "missing_file", "extra_step", "override"])
def test_synthetic_fault_rejections(tmp_path, mutation):
    candidate, manifest, rows = receipts(tmp_path)
    name = gate.DIRECT_LINUX_FAULT_SCENARIOS[0]["scenario_id"]
    row = rows[name]
    step = row["steps"]
    if mutation in ("skipped", "failed"): row["status"] = mutation
    elif mutation == "candidate": row["candidate_tuple"]["agent"] = "f" * 40
    elif mutation == "manifest": row["manifest_sha256"] = "f" * 64
    elif mutation == "runner": row["runner_image"] = "macos-14"
    elif mutation == "provenance": row["runner_provenance"]["ImageVersion"] = ""
    elif mutation == "installed": row["installed_tuple"]["runtime"] = "f" * 40
    elif mutation == "sources": row["sources"]["runtime"]["sha"] = "f" * 40
    elif mutation == "cleanup": del row["cleanup"]["keep_data"]
    elif mutation == "manager": row["service_manager"]["foreground_fallback"] = True
    elif mutation == "predicate": row["predicates"].pop(next(iter(row["predicates"])))
    elif mutation == "baseline": step["baseline"]["populated_data"] = False
    elif mutation == "predecessor": del step["baseline"]["authentic_predecessor"]
    elif mutation == "fixture": row["release0_fixture"]["provenance_sha256"] = "9" * 64
    elif mutation == "operation": step["recover"]["operation_id"] = "other"
    elif mutation == "data": step["verify"]["data_sha256"] = "4" * 64
    elif mutation == "sentinel": step["verify"]["sentinel"]["alive"] = False
    elif mutation == "selected": step["verify"]["selected_sha256"] = "1" * 64
    elif mutation == "command": del step["baseline"]["command"]
    elif mutation == "exit": del step["baseline"]["command_exit"]
    elif mutation == "inner": del step["interrupt"]["inner_exit"]
    elif mutation == "hash": step["verify"]["evidence"][0]["sha256"] = "0" * 64
    elif mutation == "missing_file": (tmp_path / name / "verify.txt").unlink()
    elif mutation == "extra_step": step["extra"] = {}
    elif mutation == "override": row["source_override_test_mode"] = True
    write(tmp_path, rows)
    assert evaluate(candidate, manifest, tmp_path, rows), mutation


@pytest.mark.parametrize("mutation", ["unresolved_post", "settled_post", "bool_count", "missing_phase", "duplicate_phase", "wrong_save", "identity", "missing_exit", "hash", "reused"])
def test_synthetic_guard_rejections(tmp_path, mutation):
    candidate, manifest, rows = receipts(tmp_path)
    row = rows[gate.APP_UPDATE_GUARD_MATRIX[0]["scenario_id"]]
    phases = row["phases"]
    if mutation == "unresolved_post": phases[0]["update_post_count"] = 1
    elif mutation == "settled_post": phases[1]["update_post_count"] = 0
    elif mutation == "bool_count": phases[1]["update_post_count"] = True
    elif mutation == "missing_phase": phases.pop()
    elif mutation == "duplicate_phase": phases[1] = copy.deepcopy(phases[0])
    elif mutation == "wrong_save": phases[0]["save_state"] = "settled"
    elif mutation == "identity": row["installed_app_identity"]["app_sha"] = "f" * 40
    elif mutation == "missing_exit": del phases[0]["inner_exit"]
    elif mutation == "hash": phases[0]["evidence"][0]["sha256"] = "0" * 64
    elif mutation == "reused": phases[1]["evidence"] = copy.deepcopy(phases[0]["evidence"])
    write(tmp_path, rows)
    assert evaluate(candidate, manifest, tmp_path, rows), mutation


@pytest.mark.parametrize("contract", (*gate.DIRECT_LINUX_FAULT_SCENARIOS, *gate.DIRECT_MACOS_FAULT_SCENARIOS,
                                      *gate.DIRECT_WINDOWS_FAULT_SCENARIOS), ids=lambda c: c["scenario_id"])
def test_each_fault_row_rejects_bad_terminal_observation(tmp_path, contract):
    candidate, manifest, rows = receipts(tmp_path)
    row = rows[contract["scenario_id"]]
    if "fail_candidate" in row["steps"]:
        row["steps"]["fail_candidate"]["terminal_operation_id"] = "other"
    else:
        row["steps"]["recover"]["operation_id"] = "other"
    write(tmp_path, rows)
    assert any(contract["scenario_id"] in e for e in evaluate(candidate, manifest, tmp_path, rows))


def test_cli_routes_to_single_checker(tmp_path, monkeypatch, capsys):
    candidate, manifest, rows = receipts(tmp_path)
    write(tmp_path, rows)
    candidate_file = tmp_path / "candidate.json"
    candidate_file.write_text(json.dumps(candidate))
    index_file = tmp_path.parent / (tmp_path.name + "-trusted.json")
    index_file.write_text(json.dumps(trusted_index(tmp_path, candidate, rows)))
    monkeypatch.setattr(gate, "validate_tuple", lambda *args: manifest)
    monkeypatch.setattr(gate.sys, "argv", ["checker", "--installed-faults", "--candidate", str(candidate_file),
                                           "--app-checkout", str(tmp_path), "--results", str(tmp_path), "--trusted-producer-index", str(index_file)])
    assert gate.main() == 0
    assert "PASS" in capsys.readouterr().out
    rows[gate.APP_UPDATE_GUARD_MATRIX[0]["scenario_id"]]["phases"][0]["update_post_count"] = 1
    write(tmp_path, rows)
    index_file.write_text(json.dumps(trusted_index(tmp_path, candidate, rows)))
    assert gate.main() == 1
    assert "FAIL" in capsys.readouterr().out


def test_cli_rejects_missing_or_results_supplied_index(tmp_path, monkeypatch, capsys):
    candidate, manifest, rows = receipts(tmp_path)
    write(tmp_path, rows)
    candidate_file = tmp_path / "candidate.json"
    candidate_file.write_text(json.dumps(candidate))
    index_file = tmp_path / "untrusted-index.json"
    index_file.write_text(json.dumps(trusted_index(tmp_path, candidate, rows)))
    monkeypatch.setattr(gate, "validate_tuple", lambda *args: manifest)
    args = ["checker", "--installed-faults", "--candidate", str(candidate_file),
            "--app-checkout", str(tmp_path), "--results", str(tmp_path)]
    monkeypatch.setattr(gate.sys, "argv", args)
    assert gate.main() == 1
    assert "trusted producer index" in capsys.readouterr().out
    monkeypatch.setattr(gate.sys, "argv", args + ["--trusted-producer-index", str(index_file)])
    assert gate.main() == 1
    assert "independent of downloaded results" in capsys.readouterr().out
