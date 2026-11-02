"""Focused R-36 checks for the supplied R-25 snapshot producer seam.

The positive fixture models an M-CI-authenticated index.  It is intentionally
synthetic and is not a hosted receipt or a platform-qualification claim.
"""
from __future__ import annotations

import copy
import hashlib
import json
from pathlib import Path

import pytest

from scripts import journey_predicates as predicates
from scripts import pinokio_snapshot_collector as collector
from scripts import r25_snapshot_verifier as verifier


CANDIDATE = {
    "app": "79f7e5397c044c4c97dc7e97b20ad96f787e5247",
    "agent": "ab43bad818ef65ca1e929b973a2f61cd9fc4bf61",
    "runtime": "2776ba973b6c8f3555b5629b0840d9691a98d3c2",
    "pinokio": "3a6a98ae251ca126505245286e2d3e760f4348e9",
    "manifest_sha256": "b2584f0bb5a949890364004572a0182f2b568adcdba061e73b739e1d3d2d623f",
    "artifacts": {
        "public/install": "100bab1deb15d620abd910c8fb0c9a6d77f4945428a3229dac6b506705e3fd0d",
        "public/install.sh": "591c4c51b148fccef2a7d1c45782d9ff949d5fd03d46cd82b5591e3a23d1b285",
        "public/install.ps1": "bba3f2edc41c047cb51475ceeac4489a04610af73a3af44bdc05287964d28258",
    },
    "published_at": "2026-11-02T17:55:27Z",
}
SLOT = "pinokio-backend-macos"


def canonical(value: object) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode("ascii")


def record(path: str, value: dict) -> dict:
    raw = canonical(value) + b"\n"
    return {"path": path, "sha256": hashlib.sha256(raw).hexdigest(), "json": value}


def platform_snapshot(_targets: object) -> dict:
    value = {
        "capture_finished_monotonic_ns": "200",
        "capture_started_monotonic_ns": "100",
        "captured_at_utc": "2026-11-02T18:42:21Z",
        "collector_pid": 912,
        "launchd": [{
            "error": "not-found", "raw_print": None, "returncode": 113,
            "state": "absent", "target": "gui/501/haus.astrid.runtime",
        }],
        "limits": {
            "command_timeout_seconds": 30, "max_processes": 4096,
            "max_ps_bytes": 1048576, "max_service_bytes": 262144,
            "max_service_targets": 64,
        },
        "processes": [{
            "birth_text": "Mon Nov  2 18:42:20 2026",
            "collection_state": "present",
            "command_line": "/Applications/Pinokio.app/Contents/MacOS/Pinokio --serve",
            "executable_path": "/Applications/Pinokio.app/Contents/MacOS/Pinokio",
            "parent_pid": 1,
            "pid": 42,
        }],
        "schema": "astrid.r25.snapshot.macos.v1",
        "source_profile": "astrid.r25.macos-15.bash.ps-libproc-launchctl.v1",
    }
    value["structural_sha256"] = hashlib.sha256(canonical(value)).hexdigest()
    return value


def bound_snapshot_index(tmp_path: Path) -> dict:
    tmp_path.mkdir(parents=True, exist_ok=True)
    action_payloads = {action: {"action": action, "kind": "synthetic-action-record"} for action in verifier.REQUIRED_ACTIONS}
    request = {
        "schema": verifier.REQUEST_SCHEMA,
        "slot_id": SLOT,
        "tuple": copy.deepcopy(CANDIDATE),
        "origin": {
            "repository_id": 101,
            "repository_full_name": "banodoco/astrid-pinokio",
            "workflow_id": 202,
            "workflow_ref": "banodoco/astrid-pinokio/.github/workflows/r25-native-collectors.yml@refs/heads/lane/M-PIN/R-25-SNAPSHOT-COLLECTORS-R1",
            "workflow_sha": verifier.R25_PINOKIO_REF,
            "run_id": 303,
            "run_attempt": 1,
            "head_sha": verifier.R25_PINOKIO_REF,
            "producer_job_id": 404,
            "producer_job_name": SLOT,
            "artifact_id": None,
            "artifact_name": f"r25-snapshots-{SLOT}-303-1",
        },
        "collector": {
            "agent_ref": verifier.R25_AGENT_REF,
            "pinokio_ref": verifier.R25_PINOKIO_REF,
            "sources": copy.deepcopy(verifier.R25_SOURCES),
        },
        "actions": [{
            "action": action,
            "action_id": f"m46-{action}-303-1",
            "action_sha256": hashlib.sha256(canonical(action_payloads[action]) + b"\n").hexdigest(),
        } for action in verifier.REQUIRED_ACTIONS],
        "targets": {"os": "macos", "launchd_targets": ["gui/501/haus.astrid.runtime"]},
    }
    session = collector.SnapshotSession(request, tmp_path / "r25", capture=platform_snapshot)
    for action in verifier.REQUIRED_ACTIONS:
        with session.action(action, poll_interval_seconds=0.01, max_poll_samples=2):
            pass
    session.finalize()

    files = []
    for path in sorted(session.output.rglob("*.json")):
        relative = path.relative_to(session.output).as_posix()
        files.append({"path": relative, "sha256": hashlib.sha256(path.read_bytes()).hexdigest(), "json": json.loads(path.read_text())})
    action_evidence = []
    for action, binding in zip(verifier.REQUIRED_ACTIONS, request["actions"], strict=True):
        action_path = f"actions/{action}.json"
        filesystem_path = f"filesystem/{action}.json"
        action_value = action_payloads[action]
        filesystem_value = {"action": action, "kind": "synthetic-filesystem-record"}
        files.extend([record(action_path, action_value), record(filesystem_path, filesystem_value)])
        action_evidence.append({
            "action": action,
            "action_id": binding["action_id"],
            "action_sha256": binding["action_sha256"],
            "action_path": action_path,
            "filesystem_path": filesystem_path,
            "filesystem_sha256": hashlib.sha256(canonical(filesystem_value) + b"\n").hexdigest(),
        })
    receipt = next(row["json"] for row in files if row["path"] == verifier.RECEIPT_PATH)
    request_file = next(row for row in files if row["path"] == verifier.REQUEST_PATH)
    receipt_file = next(row for row in files if row["path"] == verifier.RECEIPT_PATH)
    context_origin = copy.deepcopy(request["origin"])
    context_origin["artifact_id"] = 505
    return {
        "slot_id": SLOT,
        "candidate": copy.deepcopy(CANDIDATE),
        "authenticated_context": {
            "schema": verifier.CONTEXT_SCHEMA,
            "slot_id": SLOT,
            "candidate": copy.deepcopy(CANDIDATE),
            "origin": context_origin,
            "artifact": {
                "id": 505, "name": context_origin["artifact_name"],
                "archive_sha256": "a" * 64, "archive_size_bytes": 4096,
            },
            "collector": copy.deepcopy(request["collector"]),
            "actions": copy.deepcopy(request["actions"]),
            "finalization": {
                "path": verifier.RECEIPT_PATH,
                "sha256": receipt_file["sha256"],
                "request_path": verifier.REQUEST_PATH,
                "request_sha256": request_file["sha256"],
                "actions": [row["action_id"] for row in request["actions"]],
                "files": [verifier.REQUEST_PATH, *[row["path"] for row in receipt["files"]]],
                "complete": True,
            },
            "action_evidence": action_evidence,
        },
        "files": files,
    }


def reasons(value: dict) -> list[str]:
    ok, result = predicates.check_pinokio_snapshot(value)
    assert ok is False
    assert result
    return result


def test_r25_valid_independently_bound_synthetic_fixture_is_accepted(tmp_path: Path) -> None:
    evidence = bound_snapshot_index(tmp_path)
    ok, result = predicates.check_pinokio_snapshot(evidence)
    assert ok is True
    assert result == []


def test_r25_missing_and_skipped_receipts_fail_closed(tmp_path: Path) -> None:
    missing = bound_snapshot_index(tmp_path / "missing")
    missing["files"] = [row for row in missing["files"] if row["path"] != verifier.RECEIPT_PATH]
    assert any("missing" in item for item in reasons(missing))

    skipped = bound_snapshot_index(tmp_path / "skipped")
    next(row for row in skipped["files"] if row["path"] == verifier.RECEIPT_PATH)["json"]["status"] = "skipped"
    assert any("completed R-25 receipt" in item for item in reasons(skipped))


def test_r25_forged_and_duplicate_records_fail_closed(tmp_path: Path) -> None:
    forged = bound_snapshot_index(tmp_path / "forged")
    observation = next(row for row in forged["files"] if row["path"].startswith("observations/"))
    observation["json"]["observation"]["processes"][0]["command_line"] = "forged"
    assert any("SHA-256" in item or "structural" in item for item in reasons(forged))

    duplicate = bound_snapshot_index(tmp_path / "duplicate")
    duplicate["files"].append(copy.deepcopy(duplicate["files"][0]))
    assert any("duplicate evidence path" in item for item in reasons(duplicate))


def test_r25_wrong_tuple_fails_closed(tmp_path: Path) -> None:
    evidence = bound_snapshot_index(tmp_path)
    evidence["candidate"]["agent"] = "f" * 40
    assert any("selected tuple" in item or "tuple" in item for item in reasons(evidence))


@pytest.mark.parametrize(
    ("name", "mutate", "needle"),
    [
        ("run", lambda value: value["authenticated_context"]["origin"].__setitem__("run_id", 999), "origin.run_id"),
        ("attempt", lambda value: value["authenticated_context"]["origin"].__setitem__("run_attempt", 2), "origin.run_attempt"),
        ("artifact", lambda value: value["authenticated_context"]["artifact"].__setitem__("id", 999), "artifact"),
        ("finalization", lambda value: value["authenticated_context"]["finalization"].__setitem__("complete", False), "finalization"),
    ],
)
def test_r25_run_attempt_artifact_and_finalization_bindings_fail_closed(tmp_path: Path, name: str, mutate, needle: str) -> None:
    evidence = bound_snapshot_index(tmp_path / name)
    mutate(evidence)
    assert any(needle in item for item in reasons(evidence))


def test_r25_binding_does_not_claim_full_m4_6_acceptance(tmp_path: Path) -> None:
    evidence = bound_snapshot_index(tmp_path)
    binding_ok, _ = predicates.check_pinokio_snapshot(evidence)
    m4_6_ok, m4_6_reasons = predicates.check_m4_6(evidence)
    assert binding_ok is True
    assert m4_6_ok is False
    assert any("independent section 5" in item for item in m4_6_reasons)
