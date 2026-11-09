"""Closed END-STATE inventory and fail-closed visibility checks."""

import hashlib
import json
import subprocess
from collections import Counter

from scripts import check_journey_evidence as gate
from scripts.journey_matrix import (END_STATE_INVENTORY, M4_5_SCENARIO_IDS, M4_6_SLOT_IDS, RELEASE1_1_DEFERRED_PINOKIO_MATRIX,
                                    RELEASE1_MATRIX)


HOSTED = {"route": "hosted-button", "request": {"method": "POST", "path": "/api/runtime/v1/update",
          "origin": "https://astrid.haus", "content_length": 0, "authorization": False, "cookie": False,
          "status": 202, "operation_id": "a" * 32},
          "served": {"/install-manifest.json": 1, "/install": 1, "/install.ps1": 0},
          "final": {"state": "terminal", "outcome": "updated", "target_release": 2}, "intercept_reverted": True}
# The fresh Linux row's update is the hosted App build's own Update now, clicked in a browser.
BUTTON = {**HOSTED, "driver": "app-button",
          "request": {**HOSTED["request"], "headers": ["accept", "content-length", "host", "origin", "user-agent"]},
          "page": {"entry": "/tools/video-editor?settings=updates", "offered": True, "clicked": True,
                   "progress_shown": True, "build_sha256": "b" * 64, "served_manifest": 1}}


def candidate():
    return {key: letter * 40 for key, letter in zip(("app", "agent", "runtime", "pinokio"), "abcd", strict=True)}


def release1_row(contract, pinned):
    runner_os, image_os = gate.RUNNER_PROVENANCE[contract["runner"]]
    row = {
        "receipt_kind": gate.release1_receipt_kind(contract), "job_id": contract["job_id"],
        "mode": contract["mode"], "scenario": contract.get("leg", "fresh-install"),
        "status": "pass", "runner_image": contract["runner"], "shell": contract["shell"],
        "source_override_test_mode": False, "final_cleanup": "pass", "os_build": runner_os + "-test",
        "runner_provenance": {"RUNNER_OS": runner_os, "ImageOS": image_os, "ImageVersion": "test"},
        "candidate_checkouts": {name: {"sha": sha} for name, sha in pinned.items()},
        "run_roots": {"program": "/run/program", "data": "/run/data"},
        "sources": {name: {"sha": pinned[name]} for name in ("app", "agent", "runtime")},
        "installed_tuple": {name: pinned[name] for name in ("app", "agent", "runtime")},
        "import_origins": {"runtime": "/owned/runtime"},
        "service_manager_prerequisite": {"required": True, "outcome": "pass", "manager": "native",
                                           "version": "1", "foreground_fallback": False},
        "checks": {name: "pass" for name in ("semantic_data", "release", "service", "health",
                                                 "task_settled", "unrelated_sentinel",
                                                 "handed_off_start", "update_exit")},
    }
    if contract["mode"] == "direct":
        row.update(
            service_manager={"manager": "native", "foreground_fallback": False},
            release_transition={"from": 9, "to": 10, "start_state": "r0-handoff-complete",
                                "source": "candidate-install-update"},
            release0_fixture={"fixture_sums_sha256": gate.RELEASE0_SUMS_SHA256,
                              "bootstrap_sha256": gate.RELEASE0_BOOTSTRAP_SHA256,
                              "manifest_sha256": "4" * 64, "provenance_sha256": "5" * 64,
                              "sources": {"agent": "3204bb00135bec8ee2d2d3fb21eed5abfceca649",
                                          "runtime": "6d53a19070faf63fc15ae1f81417722edc2c8a5f"}},
            handoff_source={
                "app_commit": gate.RELEASE9_APP_COMMIT, "manifest_sha256": "f" * 64,
                "bootstrap_sha256": {"public/install": "1" * 64, "public/install.sh": "2" * 64,
                                     "public/install.ps1": "3" * 64},
                "identity": gate.RELEASE9_IDENTITY,
            },
            handoff={"phase": "complete", "release": 9, "r0_data_root": "/run/data",
                     "r1_data_root": "/run/data-r1"},
            update={"command": "install --update", "exit_code": 0},
            health_transition={"candidate_live_release": 10},
            semantic_data={"identical": True, "before_sha256": "b" * 64, "after_sha256": "b" * 64},
        )
    else:
        row["service_manager_prerequisite"] = {"required": False, "outcome": "explicit-owner",
                                                 "managed_by": "pinokio", "foreground_fallback": False}
    if contract.get("leg", "fresh-install") == "fresh-install":
        row["checks"]["workspace_content"] = "pass"
    if contract["job_id"] in {"direct-linux-fresh", "direct-macos-fresh"}:
        row["update"] = {
            "status": "pass", "release_from": 1, "release_to": 2,
            "hosted": BUTTON if contract["job_id"] == "direct-linux-fresh" else HOSTED,
            "old_version": "old", "new_version": "new", "manifest_sha256": "d" * 64,
            "service": {"foreground_fallback": False, "service_path": "/run/astrid.service"},
            "service_names": ["astrid.service"],
            "checks": {key: "pass" for key in (
                "new_version", "old_version_retained", "same_service", "no_boundary_handoff",
                "health", "workspace_content", "unrelated_sentinel", "verify_update", "task_settled",
            )},
        }
    if contract["job_id"] == "direct-windows":
        row["update"] = {
            "outcome": "updated", "from_release": 1, "to_release": 2, "hosted": HOSTED,
            "marker": "M-RT UPDATE TEST STAND-IN", "manifest_sha256": "d" * 64,
            "old_version": "old", "current_version": "new", "exit_code": 0, "refusal": None,
            "task_id": "task-1", "task_labels": [{"name": "Astrid Runtime", "path": "\\"}], "service_names": [],
            "checks": {key: "pass" for key in ("running_before", "owned_task", "healthy", "same_labels",
                       "no_r0_handoff", "workspace", "sentinel", "prior_version", "verify_update", "task_settled")},
        }
        row["windows_upgrade_refusal"] = {
            "receipt_kind": "windows-upgrade-refusal", "job_id": "direct-windows", "scenario": "windows-upgrade-refusal", "mode": "direct", "status": "pass", "final_cleanup": "pass", "source_override_test_mode": False,
            "runner_image": row["runner_image"], "runner_provenance": row["runner_provenance"], "os_build": row["os_build"], "shell": row["shell"], "candidate_checkouts": row["candidate_checkouts"],
            "release0_fixture": {"fixture_sums_sha256": gate.RELEASE0_SUMS_SHA256, "manifest_sha256": "f" * 64, "provenance_sha256": "a" * 64, "bootstrap_sha256": gate.RELEASE0_BOOTSTRAP_SHA256, "sources": {"agent": "3204bb00135bec8ee2d2d3fb21eed5abfceca649", "runtime": "6d53a19070faf63fc15ae1f81417722edc2c8a5f"}}, "data_marker": {"sha256": "b" * 64},
            "scheduled_task": {"before_sha256": "c" * 64, "after_sha256": "c" * 64},
            "refusal": {"exit_code": 1, "message": "Upgrading from an earlier Windows version isn't supported yet. Your data was kept; reinstall to get the new version."},
            "checks": {key: "pass" for key in ("refusal", "data_marker", "r0_selected", "scheduled_task")},
        }
    return row


def write_release1_set(root, pinned):
    paths = {}
    for contract in (*RELEASE1_MATRIX, *RELEASE1_1_DEFERRED_PINOKIO_MATRIX):
        path = root / contract["job_id"] / "journey-result.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(release1_row(contract, pinned)), encoding="utf-8")
        paths[contract["job_id"]] = path
    return paths


def by_id(statuses):
    return {row["id"]: row for row in statuses}


def test_end_state_inventory_lists_every_required_obligation_once():
    ids = [row["id"] for row in END_STATE_INVENTORY]
    assert len(ids) == len(set(ids)) == 64
    assert all(row["owner"] and row["dependency"] and row["predicate"] and row["source"] for row in END_STATE_INVENTORY)
    assert all(row["route_state"] in {"execution-capable", "unimplemented"} for row in END_STATE_INVENTORY)
    assert {row["id"] for row in END_STATE_INVENTORY if row["phase"] == "r2" and row["id"].startswith("release2-")} == set(M4_5_SCENARIO_IDS)
    for prefix in ("interruption-recovery", "rollback", "keep_data_uninstall", "stop_safety"):
        assert {row["job_id"] for row in END_STATE_INVENTORY if row["id"].startswith(prefix + ":")} == set(M4_6_SLOT_IDS)
    assert Counter(row["phase"] for row in END_STATE_INVENTORY) == {
        "fresh-install": 9, "populated-r0-update": 4, "interruption-recovery": 10,
        "rollback-uninstall-stop": 27, "r2": 13, "linux-app-guard": 1,
    }
    assert {"linux-unsaved-draft-browser", "r0-to-r2-skip", "populated-v31-to-v33-backup-restore",
            "interrupted-migration-restore"} <= set(ids)


def test_obligation_statuses_are_complete_and_missing_routes_are_not_run(tmp_path):
    statuses, errors = gate.evaluate_end_state_obligations(candidate(), tmp_path)
    assert not errors
    assert len(statuses) == len(END_STATE_INVENTORY) == 64
    assert {row["status"] for row in statuses} == {"not-run"}
    rows = by_id(statuses)
    assert "fresh-install:direct-linux" not in rows
    assert "missing Release-1 lifecycle receipt" in rows["fresh-install:direct-linux-fresh"]["reasons"]
    assert "hosted M4-6 route is unimplemented" in rows["rollback:direct-linux"]["reasons"][0]
    assert rows["release2-button-direct-linux"]["status"] == "not-run"
    assert rows["linux-unsaved-draft-browser"]["status"] == "not-run"
    report = gate.format_end_state_obligations(statuses)
    assert report.count("\n- [") == 64
    assert "[passed]" not in report


def test_obligation_statuses_attribute_pass_failed_skipped_duplicate_and_wrong_tuple(tmp_path):
    pinned = candidate()
    paths = write_release1_set(tmp_path, pinned)
    rows = by_id(gate.evaluate_end_state_obligations(pinned, tmp_path)[0])
    assert rows["populated-r0-update:direct-linux"]["status"] == "passed"
    assert rows["fresh-install:pinokio-backend-linux"]["status"] == "passed"
    assert rows["fresh-install:direct-linux-fresh"]["status"] == "passed"
    assert rows["fresh-install:direct-macos-fresh"]["status"] == "passed"

    wrong_tuple = json.loads(paths["direct-linux"].read_text(encoding="utf-8"))
    wrong_tuple["installed_tuple"]["runtime"] = "e" * 40
    paths["direct-linux"].write_text(json.dumps(wrong_tuple), encoding="utf-8")
    rows = by_id(gate.evaluate_end_state_obligations(pinned, tmp_path)[0])
    assert rows["populated-r0-update:direct-linux"]["status"] == "failed"
    assert any("identity differs" in reason for reason in rows["populated-r0-update:direct-linux"]["reasons"])

    skipped = json.loads(paths["pinokio-backend-linux"].read_text(encoding="utf-8"))
    skipped["status"] = "skipped"
    paths["pinokio-backend-linux"].write_text(json.dumps(skipped), encoding="utf-8")
    rows = by_id(gate.evaluate_end_state_obligations(pinned, tmp_path)[0])
    assert rows["fresh-install:pinokio-backend-linux"]["status"] == "failed"

    duplicate = tmp_path / "duplicate" / "journey-result.json"
    duplicate.parent.mkdir()
    duplicate.write_text(paths["pinokio-backend-macos"].read_text(encoding="utf-8"), encoding="utf-8")
    rows = by_id(gate.evaluate_end_state_obligations(pinned, tmp_path)[0])
    assert rows["fresh-install:pinokio-backend-macos"]["status"] == "failed"
    assert rows["fresh-install:pinokio-backend-macos"]["reasons"] == ("duplicate Release-1 lifecycle receipts",)


def test_duplicate_json_is_rejected_not_promoted_to_a_pass(tmp_path):
    pinned = candidate()
    path = tmp_path / "forged" / "journey-result.json"
    path.parent.mkdir()
    path.write_text('{"receipt_kind":"release1-installed-lifecycle","receipt_kind":"release1-installed-lifecycle"}', encoding="utf-8")
    statuses, errors = gate.evaluate_end_state_obligations(pinned, tmp_path)
    assert any("duplicate JSON field receipt_kind" in error for error in errors)
    assert all(row["status"] != "passed" for row in statuses)


def test_m4_6_malformed_or_local_evidence_is_never_a_pass(tmp_path):
    pinned = candidate()
    pinned["manifest_sha256"] = "f" * 64
    path = tmp_path / "m4-6-local-result.json"
    path.write_text(json.dumps({"slot_id": "direct-linux"}), encoding="utf-8")
    rows = by_id(gate.evaluate_end_state_obligations(pinned, tmp_path)[0])
    for prefix in ("interruption-recovery", "rollback", "keep_data_uninstall", "stop_safety"):
        assert rows[f"{prefix}:direct-linux"]["status"] == "failed"


def test_m4_5_wrong_checker_tuple_is_explicitly_failed(tmp_path):
    pinned = candidate()
    path = tmp_path / "journey-result.json"
    path.write_text(json.dumps({
        "receipt_kind": "release2-installed-lifecycle",
        "scenario_id": "release2-button-direct-linux",
        "candidate": {key: "e" * 40 for key in ("app", "agent", "runtime", "pinokio")},
    }), encoding="utf-8")
    row = by_id(gate.evaluate_end_state_obligations(pinned, tmp_path)[0])["release2-button-direct-linux"]
    assert row["status"] == "failed"
    assert "M4-5 receipt candidate differs from the frozen checker candidate" in row["reasons"]


def test_checker_cli_lists_the_closed_inventory_and_fails_not_run_rows(tmp_path, monkeypatch, capsys):
    app = tmp_path / "app"
    public = app / "public"
    public.mkdir(parents=True)
    for name in ("install", "install.sh", "install.ps1"):
        (public / name).write_text(name + "\n", encoding="utf-8")
    pinned = candidate()
    manifest = {"agent": {"sha": pinned["agent"]}, "runtime": {"sha": pinned["runtime"]}}
    (public / "install-manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
    subprocess.run(["git", "init", "-q", str(app)], check=True)
    subprocess.run(["git", "-C", str(app), "add", "."], check=True)
    subprocess.run(["git", "-C", str(app), "-c", "user.name=test", "-c", "user.email=test@example.invalid",
                    "commit", "-qm", "fixture"], check=True)
    pinned["app"] = subprocess.check_output(["git", "-C", str(app), "rev-parse", "HEAD"], text=True).strip()
    pinned["manifest_sha256"] = hashlib.sha256((public / "install-manifest.json").read_bytes()).hexdigest()
    pinned["artifacts"] = {
        f"public/{name}": hashlib.sha256((public / name).read_bytes()).hexdigest()
        for name in ("install", "install.sh", "install.ps1")
    }
    candidate_path = tmp_path / "candidate.json"
    candidate_path.write_text(json.dumps(pinned), encoding="utf-8")
    monkeypatch.setattr(gate.sys, "argv", [
        "check_journey_evidence.py", "--report-end-state-obligations", "--candidate", str(candidate_path),
        "--app-checkout", str(app), "--results", str(tmp_path / "results"),
    ])
    assert gate.main() == 1
    output = capsys.readouterr().out
    assert "## Journey evidence: FAIL" in output
    assert "## Release-obligation visibility" in output
    assert output.count("\n- [not-run] ") == 64
