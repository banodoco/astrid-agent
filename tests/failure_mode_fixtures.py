from __future__ import annotations

import copy

from scripts import journey_failure_modes as failure

PROVISIONAL_CANDIDATE = {
    "app": "33f1de3f33dfb1e9799ca745c5c3b8437173e06e",
    "agent": "3cc84be5d8c565f1a4a500dcf713478498e34885",
    "runtime": "a3e3f2ed95d6363e7f5b463b521da94221cf6f00",
    "pinokio": "c5b42701641889ecb916c60cde09a717f83d62ff",
    "manifest_sha256": "9" * 64,
}


def semantic(seed: str = "a") -> dict:
    return {
        "before_sha256": seed * 64,
        "after_sha256": seed * 64,
        "identical": True,
        "entities": {
            name: {"before_sha256": char * 64, "after_sha256": char * 64}
            for name, char in zip(failure.DATA_ENTITIES, "bcde", strict=True)
        },
    }


def release0_identity(process: str = "4") -> dict:
    return {
        "selected_release": "release0",
        "executable_path": "/owned/program/versions/release0/py/bin/astrid",
        "executable_sha256": "1" * 64,
        "version_manifest_sha256": "2" * 64,
        "process_identity_sha256": process * 64,
        "source_refs": dict(failure.RELEASE0_REFS),
        "running": True,
        "health": "pass",
    }


def release0_pair() -> dict:
    before = release0_identity("3")
    after = release0_identity("4")
    return {"before": before, "after": after, "match": True}


def candidate_identity(candidate: dict) -> dict:
    return {
        "selected_release": "candidate",
        "executable_path": "/owned/program/versions/candidate/py/bin/astrid",
        "executable_sha256": "5" * 64,
        "version_manifest_sha256": candidate["manifest_sha256"],
        "process_identity_sha256": "6" * 64,
        "source_refs": {"agent": candidate["agent"], "runtime": candidate["runtime"]},
        "running": True,
        "health": "pass",
    }


def valid_observation(slot_id: str, candidate: dict | None = None) -> dict:
    candidate = copy.deepcopy(candidate or PROVISIONAL_CANDIDATE)
    contract = failure.SLOT_CONTRACTS[slot_id]
    os_name, runner_os, image_os = failure.RUNNER_PROVENANCE[contract["runner"]]
    evidence = [
        {
            "path": f"evidence/{scenario}.json",
            "sha256": str(index) * 64,
            "kind": "machine-observation",
            "scenario": scenario,
        }
        for index, scenario in enumerate(failure.SCENARIOS, start=1)
    ]
    stop_cases = {
        outcome: {
            "outcome": outcome,
            "reason": f"controlled {outcome} stop result",
            "update_admitted": False,
            "candidate_downloaded_or_written": False,
            "selection_changed": False,
            "journal_changed_to_update": False,
            "candidate_activated": False,
            "data_changed": False,
            "automatic_retry": False,
            "executable_identity": release0_pair(),
            "semantic_data": semantic(),
        }
        for outcome in failure.STOP_OUTCOMES
    }
    return {
        "slot_id": slot_id,
        "job_id": slot_id,
        "mode": contract["mode"],
        "os": os_name,
        "runner_image": contract["runner"],
        "runner_provenance": {
            "RUNNER_OS": runner_os,
            "ImageOS": image_os,
            "ImageVersion": "20261030.1",
        },
        "os_build": f"{runner_os}-fixture-build",
        "shell": contract["shell"],
        "source_override_test_mode": False,
        "candidate_checkouts": {
            name: {"sha": candidate[name]} for name in ("app", "agent", "runtime", "pinokio")
        },
        "manifest_sha256": candidate["manifest_sha256"],
        "release0_fixture": {
            "fixture_sums_sha256": failure.RELEASE0_SUMS_SHA256,
            "manifest_sha256": "7" * 64,
            "provenance_sha256": "8" * 64,
            "bootstrap_sha256": dict(failure.RELEASE0_BOOTSTRAP_SHA256),
            "sources": dict(failure.RELEASE0_REFS),
            "launcher_refs": dict(failure.RELEASE0_LAUNCHERS),
        },
        "evidence_files": evidence,
        "scenarios": {
            "interruption_recovery": {
                "status": "pass",
                "operation_id": "operation-123",
                "phase": "switched",
                "injected": True,
                "first_request": {"interrupted": True, "journal_state": "switched"},
                "recovery_request": {"explicit": True, "operation_id": "operation-123"},
                "terminal_state": "complete",
                "duplicate_operation": False,
                "recovery_verified": True,
                "terminal_identity": candidate_identity(candidate),
                "semantic_data": semantic(),
            },
            "failed_candidate_rollback": {
                "status": "pass",
                "failure_injected": True,
                "failure_phase": "candidate-readiness",
                "candidate_started": False,
                "candidate_activated": False,
                "rollback_selected": True,
                "journal_terminal_state": "rolled_back",
                "real_release0_started": True,
                "real_release0_identity_match": True,
                "data_intact": True,
                "executable_identity": release0_pair(),
                "semantic_data": semantic(),
            },
            "keep_data_uninstall": {
                "status": "pass",
                "requested": True,
                "keep_data": True,
                "exit_code": 0,
                "program_root_removed": True,
                "selection_removed": True,
                "native_service_removed": True,
                "data_root_exists": True,
                "workspace_and_cas_present": True,
                "runtime_processes_after": 0,
                "unrelated_sentinel_survived": True,
                "semantic_data": semantic(),
            },
            "stop_safety": {"status": "pass", "cases": stop_cases},
        },
    }
