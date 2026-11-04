#!/usr/bin/env python3
"""Controlled subprocess boundary used only by the Agent fixture tests."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import shutil


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def read_json(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--manifest", type=Path, required=True)
    parser.add_argument("--root", type=Path, required=True)
    parser.add_argument("--data-root", type=Path, required=True)
    parser.add_argument("--update-job", type=Path, required=True)
    args = parser.parse_args()
    env = os.environ
    required = [
        "ASTRID_BRIDGE_BOUNDARY",
        "ASTRID_BRIDGE_EVIDENCE_MODE",
        "ASTRID_BRIDGE_FIXTURE_FORMAT",
        "ASTRID_BRIDGE_NODE_VERSION",
        "ASTRID_BRIDGE_RELEASE",
        "ASTRID_BRIDGE_TOKEN",
        "ASTRID_BRIDGE_SCENARIO_FILE",
        "ASTRID_BRIDGE_MANIFEST_SHA256",
        "ASTRID_BRIDGE_JOB_SHA256",
        "ASTRID_BRIDGE_RELEASE0_SPEC",
        "ASTRID_BRIDGE_RECEIPT",
    ]
    if any(name not in env for name in required):
        return 2
    scenario_path = Path(env["ASTRID_BRIDGE_SCENARIO_FILE"])
    scenario = read_json(scenario_path)
    manifest = read_json(args.manifest)
    args.root.mkdir(parents=True, exist_ok=True)
    args.data_root.mkdir(parents=True, exist_ok=True)
    job_bytes = args.update_job.read_bytes()
    (args.data_root / "install.json").write_bytes(job_bytes)
    operation_id = scenario["operation_id"]
    journal_fixture = scenario.get("journal_fixture")
    journal_path = args.data_root / "update-transaction.json"
    if journal_fixture:
        source = (scenario_path.parent.parent / journal_fixture).resolve()
        if source.is_file():
            shutil.copyfile(source, journal_path)
    lock_path = args.data_root / "update.lock.json"
    lock_path.write_text(
        json.dumps({"format": "astrid-update-lock/v1", "operation_id": operation_id, "held": True}, sort_keys=True),
        encoding="utf-8",
    )
    expected = scenario["expected"]
    job = None
    try:
        job = json.loads(job_bytes)
    except json.JSONDecodeError:
        pass
    result = job.get("result") if isinstance(job, dict) else None
    if not isinstance(result, dict):
        result = {"operation_id": operation_id, "state": "malformed", "message": "fixture record malformed"}
    post_lost = expected.get("post_observation") == "lost-ack"
    post_response = None if post_lost else {
        "accepted": expected.get("post_state") == "accepted",
        "state": expected.get("post_state"),
        "operation_id": operation_id,
        "message": "fixture response",
    }
    release0 = None
    if expected.get("release0_skip"):
        adoption_path = Path(env["ASTRID_BRIDGE_RELEASE0_SPEC"])
        adoption = read_json(adoption_path)
        consumed = {}
        retained_root = args.data_root / "adopted-release0"
        for relative in adoption["required_files"]:
            source = (adoption_path.parent / relative).resolve()
            relative_key = source.relative_to(adoption_path.parent.parent)
            destination = retained_root / relative_key
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(source, destination)
            consumed[relative] = sha256(source)
        release0 = {
            "provenance_path": str(adoption_path),
            "source_kind": adoption["source_kind"],
            "consumed": consumed,
            "release2_skip": {
                "release1_skipped": True,
                "target_release": 2,
                "retained_service_files": [
                    "release0/services/launchd.plist",
                    "release0/services/systemd.service",
                    "release0/services/task.xml",
                    "release0/services/service-env.json",
                ],
            },
        }
    receipt = {
        "format": "astrid-bridge-worker/v1",
        "scenario": scenario["scenario"],
        "operation_id": operation_id,
        "argv": [part for part in os.sys.argv[1:]],
        "paths": {
            "manifest": str(args.manifest),
            "root": str(args.root),
            "data_root": str(args.data_root),
            "update_job": str(args.update_job),
        },
        "environment": {name: env[name] for name in required},
        "evidence": {
            "mode": "observed",
            "boundary": "controlled-subprocess",
            "synthetic": False,
            "source_only": False,
            "worker_pid": os.getpid(),
            "token": env["ASTRID_BRIDGE_TOKEN"],
        },
        "fixture_provenance": {
            "scenario_sha256": sha256(scenario_path),
            "manifest_sha256": sha256(args.manifest),
            "job_sha256": sha256(args.update_job),
            "release0_spec_sha256": sha256(Path(env["ASTRID_BRIDGE_RELEASE0_SPEC"])),
        },
        "http": [
            {
                "method": "POST",
                "path": "/v1/update",
                "body_bytes": 0,
                "request": {"operation_id": operation_id, "envelope": "astrid-update-request/v1"},
                "status": expected.get("post_status"),
                "observation": "lost-ack" if post_lost else "response",
                "response": post_response,
            },
            {
                "method": "GET",
                "path": "/v1/update/status",
                "status": 200,
                "response": {
                    "operation_id": operation_id,
                    "state": expected.get("status_state"),
                    "job_state": expected.get("job_state"),
                    "message": "fixture status observation",
                },
            },
        ],
        "records": {
            "operation_id": operation_id,
            "job_path": str(args.data_root / "install.json"),
            "journal_path": str(journal_path),
            "terminal_result": result,
        },
        "stop": {
            "request": {"operation_id": operation_id, "controller": "fixture-controller"},
            "ack": {"operation_id": operation_id, "status": expected.get("stop_ack")},
            "lock": {
                "path": str(lock_path),
                "present": lock_path.is_file(),
                "held": True,
            },
            "proceeded": expected.get("stop_proceeded"),
        },
    }
    if release0 is not None:
        receipt["release0"] = release0
    Path(env["ASTRID_BRIDGE_RECEIPT"]).write_text(
        json.dumps(receipt, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
