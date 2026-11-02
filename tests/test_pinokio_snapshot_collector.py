"""Focused checks for exact-boundary Pinokio snapshot sessions."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

from scripts import pinokio_snapshot_collector as collector


def _canonical(value: object) -> bytes:
    return json.dumps(
        value, sort_keys=True, separators=(",", ":"), ensure_ascii=True, allow_nan=False
    ).encode("ascii")


def _request(*, artifact_id: int | None = 42) -> dict:
    return {
        "schema": collector.REQUEST_SCHEMA,
        "slot_id": "pinokio-backend-macos",
        "tuple": {
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
        },
        "origin": {
            "repository_id": 101,
            "repository_full_name": "banodoco/astrid-pinokio",
            "workflow_id": 202,
            "workflow_ref": "banodoco/astrid-pinokio/.github/workflows/r25.yml@refs/heads/lane",
            "workflow_sha": "1" * 40,
            "run_id": 303,
            "run_attempt": 1,
            "head_sha": "2" * 40,
            "producer_job_id": 404,
            "producer_job_name": "pinokio-backend-macos",
            "artifact_id": artifact_id,
            "artifact_name": "r25-snapshots-pinokio-backend-macos-303-1",
        },
        "collector": {
            "agent_ref": "3" * 40,
            "pinokio_ref": "4" * 40,
            "sources": {"scripts/pinokio_snapshot_collector.py": "5" * 64},
        },
        "actions": [
            {
                "action": action,
                "action_id": f"m46-{action}-303-1",
                "action_sha256": f"{index + 6:x}" * 64,
            }
            for index, action in enumerate(collector.REQUIRED_ACTIONS)
        ],
        "targets": {"os": "macos", "launchd_targets": ["gui/501/haus.astrid.runtime"]},
    }


def _capture(_targets: object) -> dict:
    value = {
        "schema": "fixture-platform-snapshot.v1",
        "profile": "fixture",
        "processes": [],
        "services": [],
        "observed_at_utc": "2026-11-02T18:14:13Z",
        "monotonic_ns": 1,
    }
    value["structural_sha256"] = hashlib.sha256(_canonical(value)).hexdigest()
    return value


def test_exact_six_actions_write_bound_hashed_receipts(tmp_path: Path) -> None:
    session = collector.SnapshotSession(_request(), tmp_path / "receipts", capture=_capture)

    for action in collector.REQUIRED_ACTIONS:
        with session.action(action, poll_interval_seconds=0.01, max_poll_samples=2):
            pass
    receipt = session.finalize()

    assert [row["action"] for row in receipt["actions"]] == list(collector.REQUIRED_ACTIONS)
    assert receipt["status"] == "collector-complete"
    assert receipt["qualification"] == "requires-r36-verifier"
    assert receipt["m4_6_acceptance"] is False
    assert len(receipt["files"]) == 18
    for row in receipt["files"]:
        raw = (session.output / row["path"]).read_bytes()
        value = json.loads(raw)
        assert hashlib.sha256(raw).hexdigest() == row["sha256"]
        assert value["binding_sha256"] == session.binding_digest
        assert value["origin"] == _request()["origin"]
        assert value["tuple"] == _request()["tuple"]
        assert value["action_binding"] == _request()["actions"][value["action_index"]]


def test_missing_duplicate_and_out_of_order_actions_fail_closed(tmp_path: Path) -> None:
    session = collector.SnapshotSession(_request(), tmp_path / "missing", capture=_capture)
    with pytest.raises(collector.SnapshotCollectorError, match="action order"):
        with session.action("ready", poll_interval_seconds=0.01):
            pass
    with session.action("start", poll_interval_seconds=0.01):
        pass
    with pytest.raises(collector.SnapshotCollectorError, match="action order"):
        with session.action("start", poll_interval_seconds=0.01):
            pass
    with pytest.raises(collector.SnapshotCollectorError, match="missing, duplicate"):
        session.finalize()


def test_poll_or_action_failure_prevents_finalization_but_retains_boundaries(
    tmp_path: Path,
) -> None:
    session = collector.SnapshotSession(_request(), tmp_path / "failed", capture=_capture)
    with pytest.raises(ValueError, match="controlled"):
        with session.action("start", poll_interval_seconds=0.01):
            raise ValueError("controlled action failure")
    assert session._actions[0]["status"] == "refused"
    assert {row["phase"] for row in session._files} == {"before", "during", "boundary"}
    with pytest.raises(collector.SnapshotCollectorError, match="missing, duplicate"):
        session.finalize()


def test_request_rejects_wrong_tuple_slot_artifact_and_duplicate_json(tmp_path: Path) -> None:
    request = _request()
    request["tuple"]["agent"] = "short"
    with pytest.raises(collector.SnapshotCollectorError, match="tuple.agent"):
        collector.validate_request(request)

    request = _request()
    request["targets"]["os"] = "windows"
    with pytest.raises(collector.SnapshotCollectorError, match="slot OS"):
        collector.validate_request(request)

    request = _request()
    request["origin"]["artifact_name"] = "unbound-artifact"
    with pytest.raises(collector.SnapshotCollectorError, match="selected slot"):
        collector.validate_request(request)

    request = _request()
    request["actions"][1]["action_id"] = request["actions"][0]["action_id"]
    with pytest.raises(collector.SnapshotCollectorError, match="action IDs"):
        collector.validate_request(request)

    path = (tmp_path / "request.json").resolve()
    path.write_text('{"schema":"one","schema":"two"}', encoding="utf-8")
    with pytest.raises(collector.SnapshotCollectorError, match="duplicate JSON key"):
        collector.load_request(path)


def test_external_artifact_id_join_is_explicit_and_output_is_single_use(tmp_path: Path) -> None:
    output = tmp_path / "receipts"
    session = collector.SnapshotSession(_request(artifact_id=None), output, capture=_capture)
    for action in collector.REQUIRED_ACTIONS:
        with session.action(action, poll_interval_seconds=0.01):
            pass
    receipt = session.finalize()
    assert receipt["artifact_binding"] == {
        "artifact_id": None,
        "artifact_name": "r25-snapshots-pinokio-backend-macos-303-1",
        "artifact_id_authority": "r36-external-origin-join-required",
    }
    with pytest.raises(collector.SnapshotCollectorError, match="absent"):
        collector.SnapshotSession(_request(), output, capture=_capture)


def test_platform_observation_digest_is_recomputed(tmp_path: Path) -> None:
    def forged(_targets: object) -> dict:
        value = _capture({})
        value["structural_sha256"] = "0" * 64
        return value

    session = collector.SnapshotSession(_request(), tmp_path / "forged", capture=forged)
    with pytest.raises(collector.SnapshotCollectorError, match="structural digest"):
        with session.action("start", poll_interval_seconds=0.01):
            pass
