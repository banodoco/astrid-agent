"""Closed END-STATE inventory and fail-closed visibility checks."""

import hashlib
import json
import subprocess
from collections import Counter

from scripts import check_journey_evidence as gate
from scripts.journey_matrix import (
    DEFERRED_R1_X_PINOKIO_CONTINUITY_OBLIGATIONS,
    END_STATE_INVENTORY,
    M4_5_SCENARIO_IDS,
    M4_6_SLOT_IDS,
    RELEASE1_MATRIX,
)


def candidate():
    return {key: letter * 40 for key, letter in zip(("app", "agent", "runtime", "pinokio"), "abcd", strict=True)}


def release1_row(contract, pinned):
    runner_os, image_os = gate.RUNNER_PROVENANCE[contract["runner"]]
    row = {
        "receipt_kind": "release1-installed-lifecycle", "job_id": contract["job_id"],
        "mode": contract["mode"], "scenario": contract.get("leg", "fresh-install"),
        "status": "pass", "runner_image": contract["runner"], "shell": contract["shell"],
        "source_override_test_mode": False, "final_cleanup": "pass", "os_build": runner_os + "-test",
        "runner_provenance": {"RUNNER_OS": runner_os, "ImageOS": image_os, "ImageVersion": "test"},
        "candidate_checkouts": {name: {"sha": sha} for name, sha in pinned.items()},
        "sources": {name: {"sha": pinned[name]} for name in ("app", "agent", "runtime")},
        "installed_tuple": {name: pinned[name] for name in ("app", "agent", "runtime")},
        "import_origins": {"runtime": "/owned/runtime"},
        "service_manager_prerequisite": {"required": True, "outcome": "pass", "manager": "native",
                                           "version": "1", "foreground_fallback": False},
        "checks": {name: "pass" for name in ("semantic_data", "release", "service", "health",
                                                 "task_settled", "one_runtime", "unrelated_sentinel")},
    }
    if contract["mode"] == "direct":
        row.update(
            service_manager={"manager": "native", "foreground_fallback": False},
            release_transition={"from": 0, "to": 1, "harness_override": "frozen-release0-direct-to-release1"},
            release0_fixture={
                "fixture_sums_sha256": gate.RELEASE0_SUMS_SHA256, "manifest_sha256": "f" * 64,
                "provenance_sha256": "a" * 64, "bootstrap_sha256": gate.RELEASE0_BOOTSTRAP_SHA256,
                "sources": {"agent": "3204bb00135bec8ee2d2d3fb21eed5abfceca649",
                            "runtime": "6d53a19070faf63fc15ae1f81417722edc2c8a5f"},
            },
            semantic_data={"identical": True, "before_sha256": "b" * 64, "after_sha256": "b" * 64},
        )
    else:
        row["service_manager_prerequisite"] = {"required": False, "outcome": "explicit-owner",
                                                 "managed_by": "pinokio", "foreground_fallback": False}
    return row


def write_release1_set(root, pinned):
    paths = {}
    for contract in RELEASE1_MATRIX:
        path = root / contract["job_id"] / "journey-result.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(release1_row(contract, pinned)), encoding="utf-8")
        paths[contract["job_id"]] = path
    return paths


def by_id(statuses):
    return {row["id"]: row for row in statuses}


OPTION_B_DEFERRED_IDS = {
    "interruption-recovery:pinokio-backend-linux",
    "rollback:pinokio-backend-linux",
    "keep_data_uninstall:pinokio-backend-linux",
    "stop_safety:pinokio-backend-linux",
    "interruption-recovery:pinokio-fully-local-linux",
    "rollback:pinokio-fully-local-linux",
    "keep_data_uninstall:pinokio-fully-local-linux",
    "stop_safety:pinokio-fully-local-linux",
    "interruption-recovery:pinokio-backend-macos",
    "rollback:pinokio-backend-macos",
    "keep_data_uninstall:pinokio-backend-macos",
    "stop_safety:pinokio-backend-macos",
    "interruption-recovery:pinokio-fully-local-macos",
    "rollback:pinokio-fully-local-macos",
    "keep_data_uninstall:pinokio-fully-local-macos",
    "stop_safety:pinokio-fully-local-macos",
    "interruption-recovery:pinokio-backend-windows",
    "rollback:pinokio-backend-windows",
    "keep_data_uninstall:pinokio-backend-windows",
    "stop_safety:pinokio-backend-windows",
    "interruption-recovery:pinokio-fully-local-windows",
    "rollback:pinokio-fully-local-windows",
    "keep_data_uninstall:pinokio-fully-local-windows",
    "stop_safety:pinokio-fully-local-windows",
}


def option_b_snapshot_end_state():
    return {
        "evidence_class": "action-boundary-snapshots",
        "action_boundaries": {
            "install": "pass", "start": "pass", "stop": "pass",
            "retry-after-stop": "pass", "update": "pass", "uninstall": "pass",
        },
        "bounded_polling": "pass",
        "independent_initial_census": "pass",
        "independent_final_census": "pass",
        "exactly_one_owner": "pass",
        "uninstall_end_state": "pass",
        "data_invariant": "pass",
        "data_disposition": "kept",
        "scenario_assertions": {
            "interruption-recovery": {
                "durable_journal": "pass", "same_operation_recovery": "pass",
                "terminal_identity": "pass", "data_preserved": "pass",
            },
            "rollback": {
                "controlled_candidate_failure": "pass", "release0_identity": "pass",
                "healthy_restart": "pass", "data_preserved": "pass",
            },
            "keep_data_uninstall": {
                "program_removed": "pass", "service_removed": "pass",
                "data_kept": "pass", "sentinel_preserved": "pass",
            },
            "stop_safety": {
                "refused_stop": "pass", "unknown_stop": "pass",
                "no_update_retry_or_activation": "pass", "data_unchanged": "pass",
                "release0_identity": "pass",
            },
        },
    }


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


def test_option_b_defers_exactly_the_pinokio_continuity_claims_not_their_r1_end_states():
    deferred = {row["id"]: row for row in DEFERRED_R1_X_PINOKIO_CONTINUITY_OBLIGATIONS}
    assert set(deferred) == OPTION_B_DEFERRED_IDS
    assert len(deferred) == 24
    assert all(
        row["release_scope"] == "deferred-R1.x (Hannah decision, 2026-11-02 option B)"
        and row["decision_pointer"] == "/home/user/repo/docs/merge-plan/DECISIONS.md:316"
        and row["r1_gate"] == "excluded-deferred-R1.x"
        and row["claim"] == "gap-free Pinokio custody and removal-authorization continuity"
        for row in deferred.values()
    )
    assert {row["id"] for row in deferred.values()
            if row["removal_authorization_relation"] == "direct-removal"} == {
        item for item in OPTION_B_DEFERRED_IDS if item.startswith("keep_data_uninstall:")
    }
    assert all(
        row["removal_authorization_relation"] == "slot-coupled-removal"
        for row in deferred.values() if not row["id"].startswith("keep_data_uninstall:")
    )

    r1_rows = {row["id"]: row for row in END_STATE_INVENTORY}
    assert all(row.get("r1_gate", "required") == "required" for row in END_STATE_INVENTORY)
    assert all(
        r1_rows[obligation_id]["evidence_kind"] == "m4-6-snapshot-end-state"
        for obligation_id in OPTION_B_DEFERRED_IDS
    )
    assert all(
        r1_rows[obligation_id]["source"] == "check_journey_evidence.py:_pinokio_snapshot_end_state_errors"
        for obligation_id in OPTION_B_DEFERRED_IDS
    )
    assert all(
        r1_rows[obligation_id]["assertion_id"] == deferred[obligation_id]["r1_snapshot_assertion_id"]
        for obligation_id in OPTION_B_DEFERRED_IDS
    )
    statuses = gate.deferred_pinokio_continuity_statuses()
    assert {row["id"] for row in statuses} == OPTION_B_DEFERRED_IDS
    assert {row["status"] for row in statuses} == {"deferred-R1.x"}
    assert {row["r1_gate"] for row in statuses} == {"excluded-deferred-R1.x"}


def test_obligation_statuses_are_complete_and_missing_routes_are_not_run(tmp_path):
    statuses, errors = gate.evaluate_end_state_obligations(candidate(), tmp_path)
    assert not errors
    assert len(statuses) == len(END_STATE_INVENTORY) == 64
    assert {row["status"] for row in statuses} == {"not-run"}
    rows = by_id(statuses)
    assert "no supported authenticated evidence route" in rows["fresh-install:direct-linux"]["reasons"]
    assert "hosted M4-6 route is unimplemented" in rows["rollback:direct-linux"]["reasons"][0]
    assert rows["release2-button-direct-linux"]["status"] == "not-run"
    assert rows["linux-unsaved-draft-browser"]["status"] == "not-run"
    report = gate.format_end_state_obligations(statuses)
    assert report.count("\n- [") == 64
    assert "[passed]" not in report


def test_pinokio_snapshot_end_state_fails_closed_when_required_evidence_is_missing(tmp_path):
    pinned = candidate()
    write_release1_set(tmp_path, pinned)

    rows = by_id(gate.evaluate_end_state_obligations(pinned, tmp_path)[0])
    assert all(rows[obligation_id]["status"] == "failed" for obligation_id in OPTION_B_DEFERRED_IDS)
    assert all(
        "Pinokio action-boundary snapshot/end-state evidence is absent" in rows[obligation_id]["reasons"]
        for obligation_id in OPTION_B_DEFERRED_IDS
    )
    # The unrelated direct M4-6 check has not been excluded or promoted.
    assert rows["rollback:direct-linux"]["status"] == "not-run"
    assert rows["rollback:direct-linux"]["reasons"] == (
        "hosted M4-6 route is unimplemented; no receipt",
    )


def test_pinokio_snapshot_end_state_rejects_a_custody_continuity_claim(tmp_path):
    pinned = candidate()
    paths = write_release1_set(tmp_path, pinned)
    receipt = json.loads(paths["pinokio-backend-linux"].read_text(encoding="utf-8"))
    receipt["pinokio_snapshot_end_state"] = option_b_snapshot_end_state()
    receipt["pinokio_snapshot_end_state"]["custody_continuity"] = "claimed"
    paths["pinokio-backend-linux"].write_text(json.dumps(receipt), encoding="utf-8")

    row = by_id(gate.evaluate_end_state_obligations(pinned, tmp_path)[0])["rollback:pinokio-backend-linux"]
    assert row["status"] == "failed"
    assert "Pinokio snapshot evidence must not assert custody_continuity" in row["reasons"]


def test_obligation_statuses_attribute_pass_failed_skipped_duplicate_and_wrong_tuple(tmp_path):
    pinned = candidate()
    paths = write_release1_set(tmp_path, pinned)
    rows = by_id(gate.evaluate_end_state_obligations(pinned, tmp_path)[0])
    assert rows["populated-r0-update:direct-linux"]["status"] == "passed"
    assert rows["fresh-install:pinokio-backend-linux"]["status"] == "passed"
    assert rows["fresh-install:direct-linux"]["status"] == "not-run"

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
    assert "## Deferred R1.x continuity obligations" in output
    assert output.count("\n- [not-run] ") == 64
