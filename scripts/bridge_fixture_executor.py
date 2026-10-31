"""Receipt-first executor for the Agent-only release-1 bridge fixtures.

This module deliberately does not contain a worker implementation.  A caller must
provide a concrete subprocess command.  The worker writes a receipt to the path
supplied by the executor; the validator then checks that the receipt came from that
child, consumed the fixture files, and proves the cross-file relationships.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import secrets
import shutil
import subprocess
import sys
import tempfile
from typing import Any, Sequence


class BridgeFixtureError(RuntimeError):
    """Raised when fixture evidence is absent, ambiguous, or fails closed."""


NEEDS = (
    "NEEDS: product owners must freeze the exact lost-ack HTTP timeout/error "
    "envelope and operation-id transport; this fixture records only the observed "
    "subprocess boundary.",
    "NEEDS: A/B/Q must settle the production stop payload, lock lifetime, and "
    "release-2 adoption result before this implementation evidence can qualify "
    "a hosted run.",
)


def _fail(message: str) -> None:
    raise BridgeFixtureError(message)


def _read_json(path: Path, label: str) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        _fail(f"missing {label}: {path}")
    except (OSError, json.JSONDecodeError) as exc:
        _fail(f"malformed {label} {path}: {exc}")
    if not isinstance(value, dict):
        _fail(f"{label} must be a JSON object: {path}")
    return value


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _resolve_fixture(root: Path, relative: str, label: str, *, required: bool = True) -> Path:
    candidate = (root / relative).resolve()
    if required and not candidate.is_file():
        _fail(f"missing {label} fixture: {candidate}")
    return candidate


def _load_scenario(scenario_path: Path) -> tuple[dict[str, Any], Path, Path, Path, Path]:
    scenario_path = scenario_path.resolve()
    if scenario_path.parent.name != "scenarios" or scenario_path.parent.parent.name != "release1_bridge":
        _fail("scenario must be a file in tests/fixtures/release1_bridge/scenarios")
    scenario = _read_json(scenario_path, "scenario")
    if scenario.get("format") != "astrid-bridge-scenario/v1":
        _fail("unsupported or missing scenario format")
    for key in ("scenario", "operation_id", "manifest_fixture", "job_fixture", "expected"):
        if key not in scenario:
            _fail(f"scenario missing {key}")
    if not isinstance(scenario["expected"], dict):
        _fail("scenario expected section must be an object")
    fixture_root = scenario_path.parent.parent
    manifest = _resolve_fixture(fixture_root, scenario["manifest_fixture"], "manifest")
    job = _resolve_fixture(fixture_root, scenario["job_fixture"], "job")
    adoption = _resolve_fixture(
        fixture_root,
        scenario.get("release0_adoption", "release0-adoption.json"),
        "release-0 adoption",
    )
    journal = _resolve_fixture(
        fixture_root,
        scenario.get("journal_fixture", ""),
        "journal",
        required=False,
    )
    return scenario, manifest, job, journal, adoption


def _validate_worker_command(worker: Sequence[str]) -> list[str]:
    if isinstance(worker, (str, bytes)) or not worker:
        _fail("worker must be a non-empty argv sequence, never a shell string")
    command = [str(part) for part in worker]
    if any(part in {"-c", "-m"} for part in command):
        _fail("synthetic/source-only worker commands (-c/-m) are not accepted")
    if any(Path(part).name == "bridge_fixture_executor.py" for part in command):
        _fail("executor cannot be supplied as its own worker")
    executable = Path(command[0])
    if not executable.is_file() and shutil.which(command[0]) is None:
        _fail(f"supplied worker executable does not exist: {command[0]}")
    return command


def _copy_fixture(source: Path, destination: Path) -> None:
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_bytes(source.read_bytes())


def _expected_environment(
    *,
    token: str,
    scenario_path: Path,
    manifest: Path,
    job: Path,
    adoption: Path,
    receipt: Path,
) -> dict[str, str]:
    return {
        "ASTRID_BRIDGE_BOUNDARY": "controlled-subprocess",
        "ASTRID_BRIDGE_EVIDENCE_MODE": "observed",
        "ASTRID_BRIDGE_FIXTURE_FORMAT": "astrid-bridge-worker/v1",
        "ASTRID_BRIDGE_NODE_VERSION": "20.19.4",
        "ASTRID_BRIDGE_RELEASE": "1",
        "ASTRID_BRIDGE_TOKEN": token,
        "ASTRID_BRIDGE_SCENARIO_FILE": str(scenario_path),
        "ASTRID_BRIDGE_MANIFEST_SHA256": _sha256(manifest),
        "ASTRID_BRIDGE_JOB_SHA256": _sha256(job),
        "ASTRID_BRIDGE_RELEASE0_SPEC": str(adoption),
        "ASTRID_BRIDGE_RECEIPT": str(receipt),
    }


def _validate_provenance(
    receipt: dict[str, Any],
    expected_env: dict[str, str],
    child_pid: int,
    manifest: Path,
    job: Path,
    adoption: Path,
    scenario_path: Path,
) -> None:
    if receipt.get("format") != "astrid-bridge-worker/v1":
        _fail("worker receipt has no supported fixture format")
    evidence = receipt.get("evidence")
    if not isinstance(evidence, dict):
        _fail("worker receipt has no evidence provenance")
    if evidence.get("mode") != "observed" or evidence.get("boundary") != "controlled-subprocess":
        _fail("synthetic or source-only evidence mode/boundary")
    if evidence.get("synthetic") is not False or evidence.get("source_only") is not False:
        _fail("synthetic or source-only evidence tag")
    if evidence.get("worker_pid") != child_pid:
        _fail("receipt worker PID does not match the supplied subprocess")
    if evidence.get("token") != expected_env["ASTRID_BRIDGE_TOKEN"]:
        _fail("receipt provenance token does not match the executor")
    environment = receipt.get("environment")
    if environment != expected_env:
        _fail("worker environment provenance was altered or incomplete")
    provenance = receipt.get("fixture_provenance")
    expected_hashes = {
        "scenario_sha256": _sha256(scenario_path),
        "manifest_sha256": _sha256(manifest),
        "job_sha256": _sha256(job),
        "release0_spec_sha256": _sha256(adoption),
    }
    if provenance != expected_hashes:
        _fail("worker did not prove consumption of the supplied fixture files")


def _validate_paths(
    receipt: dict[str, Any],
    manifest_path: Path,
    root: Path,
    data_root: Path,
    update_job: Path,
) -> None:
    paths = receipt.get("paths")
    expected = {
        "manifest": str(manifest_path),
        "root": str(root),
        "data_root": str(data_root),
        "update_job": str(update_job),
    }
    if paths != expected:
        _fail(f"worker argv/path provenance mismatch: expected {expected!r}, got {paths!r}")
    argv = receipt.get("argv")
    expected_argv = [
        "--manifest",
        str(manifest_path),
        "--root",
        str(root),
        "--data-root",
        str(data_root),
        "--update-job",
        str(update_job),
    ]
    if argv != expected_argv:
        _fail(f"exact worker argv mismatch: expected {expected_argv!r}, got {argv!r}")


def _validate_http(receipt: dict[str, Any], expected: dict[str, Any], operation_id: str) -> None:
    exchanges = receipt.get("http")
    if not isinstance(exchanges, list) or len(exchanges) != 2:
        _fail("ambiguous or incomplete v1 route observations")
    post, status = exchanges
    if post.get("method") != "POST" or post.get("path") != "/v1/update":
        _fail("missing exact POST /v1/update observation")
    if status.get("method") != "GET" or status.get("path") != "/v1/update/status":
        _fail("missing exact GET /v1/update/status observation")
    if post.get("body_bytes") != 0:
        _fail("POST /v1/update body/framing is not the required zero-byte fixture")
    request = post.get("request")
    if not isinstance(request, dict) or request.get("operation_id") != operation_id:
        _fail("update request has no unambiguous operation identity")
    if post.get("status") != expected.get("post_status"):
        _fail("unexpected POST /v1/update status observation")
    if expected.get("post_observation") == "lost-ack":
        if post.get("response") is not None or post.get("observation") != "lost-ack":
            _fail("lost acknowledgement was not represented as an unacknowledged POST")
    else:
        response = post.get("response")
        if not isinstance(response, dict):
            _fail("POST response is missing")
        if set(response) != {"accepted", "state", "operation_id", "message"}:
            _fail("POST response envelope is ambiguous or incomplete")
        if response.get("operation_id") != operation_id:
            _fail("POST response operation identity mismatch")
        if response.get("state") != expected.get("post_state"):
            _fail("POST response state mismatch")
        if response.get("accepted") != (expected.get("post_state") == "accepted"):
            _fail("POST response acceptance mismatch")
    if status.get("status") != 200:
        _fail("status observation is not HTTP 200")
    status_response = status.get("response")
    if not isinstance(status_response, dict):
        _fail("status response is missing")
    if set(status_response) != {"operation_id", "state", "job_state", "message"}:
        _fail("status response envelope is ambiguous or incomplete")
    if status_response.get("operation_id") != operation_id:
        _fail("status response operation identity mismatch")
    if status_response.get("state") != expected.get("status_state"):
        _fail("status response state mismatch")


def _validate_records(
    receipt: dict[str, Any],
    expected: dict[str, Any],
    operation_id: str,
    data_root: Path,
) -> tuple[dict[str, Any], dict[str, Any]]:
    records = receipt.get("records")
    if not isinstance(records, dict):
        _fail("worker supplied no durable record evidence")
    if records.get("operation_id") != operation_id:
        _fail("record operation identity is missing")
    job_path = Path(records.get("job_path", ""))
    journal_path = Path(records.get("journal_path", ""))
    if job_path != data_root / "install.json":
        _fail("job record path is not the expected data-root/install.json")
    if journal_path != data_root / "update-transaction.json":
        _fail("journal path is not the expected data-root/update-transaction.json")
    job = _read_json(job_path, "job record")
    journal = _read_json(journal_path, "journal record")
    if job.get("format") != "astrid-update-job/v1":
        _fail("malformed job record format")
    if journal.get("format") != "astrid-install-update/v1":
        _fail("malformed journal record format")
    transaction_id = job.get("transaction_id")
    if not isinstance(transaction_id, str) or journal.get("transaction_id") != transaction_id:
        _fail("job/journal transaction identity mismatch")
    identities = [
        operation_id,
        job.get("operation_id"),
        journal.get("operation_id"),
        job.get("result", {}).get("operation_id") if isinstance(job.get("result"), dict) else None,
        records.get("terminal_result", {}).get("operation_id")
        if isinstance(records.get("terminal_result"), dict)
        else None,
    ]
    if any(value != operation_id for value in identities):
        _fail("operation identity is not consistent across request, job, journal, and result")
    if job.get("state") != expected.get("job_state"):
        _fail("unexpected durable job state")
    result = job.get("result")
    if not isinstance(result, dict) or records.get("terminal_result") != result:
        _fail("terminal result is not proved by the durable job record")
    if result.get("state") != expected.get("result_state"):
        _fail("terminal result state mismatch")
    if journal.get("step") != expected.get("journal_step"):
        _fail("unexpected journal step")
    return job, journal


def _validate_stop(receipt: dict[str, Any], expected: dict[str, Any], operation_id: str, data_root: Path) -> None:
    stop = receipt.get("stop")
    if not isinstance(stop, dict):
        _fail("stop-handshake evidence is missing")
    request = stop.get("request")
    ack = stop.get("ack")
    lock = stop.get("lock")
    if not isinstance(request, dict) or request.get("operation_id") != operation_id:
        _fail("stop request is not bound to the update operation")
    if not isinstance(ack, dict) or ack.get("operation_id") != operation_id:
        _fail("stop acknowledgement is not bound to the update operation")
    if not isinstance(lock, dict) or lock.get("path") != str(data_root / "update.lock.json"):
        _fail("lifetime lock path is missing or altered")
    lock_path = Path(lock["path"])
    if lock.get("present") is not True or not lock_path.is_file():
        _fail("lifetime lock was deleted or not proved")
    lock_record = _read_json(lock_path, "lifetime lock")
    if lock_record.get("operation_id") != operation_id or lock_record.get("held") != lock.get("held"):
        _fail("lifetime lock ownership/provenance mismatch")
    if ack.get("status") != expected.get("stop_ack"):
        _fail("stop acknowledgement outcome mismatch")
    expected_proceed = ack.get("status") == "stopped" and lock.get("held") is True
    if stop.get("proceeded") != expected_proceed or stop.get("proceeded") != expected.get("stop_proceeded"):
        _fail("stop-handshake bypass or lock-free progression detected")


def _validate_release0(receipt: dict[str, Any], adoption: Path, data_root: Path) -> None:
    release0 = receipt.get("release0")
    if not isinstance(release0, dict):
        _fail("release-0 fixture proof is missing")
    if release0.get("provenance_path") != str(adoption):
        _fail("release-0 provenance path was altered")
    if release0.get("source_kind") != "authentic-release0-fixture":
        _fail("release-0 proof is synthetic or source-only")
    spec = _read_json(adoption, "release-0 adoption spec")
    provenance_path = (adoption.parent / spec["provenance"]).resolve()
    checksums_path = (adoption.parent / spec["checksums"]).resolve()
    provenance = _read_json(provenance_path, "release-0 provenance")
    if not provenance.get("format", "").startswith("astrid-release0-fixture-provenance/"):
        _fail("release-0 provenance is not the authentic fixture format")
    if provenance.get("service_render", {}).get("renderer") != "astrid.installer.service.render":
        _fail("release-0 service renderer provenance is missing")
    consumed = release0.get("consumed")
    if not isinstance(consumed, dict):
        _fail("release-0 consumed file proof is missing")
    checksum_lines = checksums_path.read_text(encoding="utf-8").splitlines()
    expected_hashes: dict[str, str] = {}
    for line in checksum_lines:
        parts = line.split(maxsplit=1)
        if len(parts) == 2:
            expected_hashes[parts[1].lstrip(" *")] = parts[0]
    for relative in spec.get("required_files", []):
        source = (adoption.parent / relative).resolve()
        if not source.is_file():
            _fail(f"missing authentic release-0 input: {source}")
        if consumed.get(relative) != _sha256(source):
            _fail(f"release-0 input was not consumed byte-for-byte: {relative}")
        source_key = str(source.relative_to(adoption.parent.parent))
        if not source_key.startswith("release0/"):
            _fail(f"release-0 input escaped the fixture root: {relative}")
        checksum_key = source_key.removeprefix("release0/")
        if expected_hashes and expected_hashes.get(checksum_key) != consumed[relative]:
            _fail(f"release-0 checksum proof mismatch: {relative}")
        retained = data_root / "adopted-release0" / source_key
        if not retained.is_file() or _sha256(retained) != consumed[relative]:
            _fail(f"release-0 adopted file is missing or altered: {relative}")
    release2 = release0.get("release2_skip")
    if not isinstance(release2, dict) or release2.get("release1_skipped") is not True:
        _fail("release-0-skip compatibility is not proved")
    if release2.get("target_release") != 2 or not release2.get("retained_service_files"):
        _fail("release-2 retention proof is incomplete")


def execute_fixture(scenario_path: Path, worker: Sequence[str], *, timeout: float = 15.0) -> dict[str, Any]:
    """Run one file-backed fixture against a supplied worker boundary."""

    command = _validate_worker_command(worker)
    scenario, manifest, job, journal, adoption = _load_scenario(Path(scenario_path))
    expected = scenario["expected"]
    operation_id = scenario["operation_id"]
    if not isinstance(operation_id, str) or len(operation_id) != 32:
        _fail("scenario operation_id must be a 32-character identity")
    manifest_record = _read_json(manifest, "release-1 worker manifest")
    if manifest_record.get("node", {}).get("version") != "20.19.4":
        _fail("release-1 worker manifest does not prove Node 20.19.4")
    with tempfile.TemporaryDirectory(prefix="astrid bridge fixture ") as temp:
        temp_root = Path(temp)
        root = temp_root / "program root with spaces"
        data_root = temp_root / "data root with spaces"
        manifest_path = root / "release1-manifest.json"
        update_job = data_root / "update job.json"
        receipt_path = temp_root / "worker receipt.json"
        root.mkdir(parents=True)
        data_root.mkdir(parents=True)
        _copy_fixture(manifest, manifest_path)
        _copy_fixture(job, update_job)
        token = secrets.token_hex(16)
        scenario_path = Path(scenario_path).resolve()
        env_provenance = _expected_environment(
            token=token,
            scenario_path=scenario_path,
            manifest=manifest_path,
            job=update_job,
            adoption=adoption,
            receipt=receipt_path,
        )
        child_env = {"PATH": os.environ.get("PATH", "/usr/bin:/bin"), "PYTHONIOENCODING": "utf-8"}
        child_env.update(env_provenance)
        process = subprocess.Popen(
            command
            + [
                "--manifest",
                str(manifest_path),
                "--root",
                str(root),
                "--data-root",
                str(data_root),
                "--update-job",
                str(update_job),
            ],
            cwd=temp_root,
            env=child_env,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        try:
            stdout, stderr = process.communicate(timeout=timeout)
        except subprocess.TimeoutExpired as exc:
            process.kill()
            process.communicate()
            _fail(f"supplied worker timed out: {exc}")
        if process.returncode != 0:
            _fail(f"supplied worker failed with {process.returncode}: {stderr.strip()}")
        if not receipt_path.is_file():
            _fail("supplied worker did not write a receipt")
        receipt = _read_json(receipt_path, "worker receipt")
        _validate_provenance(receipt, env_provenance, process.pid, manifest_path, update_job, adoption, scenario_path)
        _validate_paths(receipt, manifest_path, root, data_root, update_job)
        if receipt.get("scenario") != scenario["scenario"] or receipt.get("operation_id") != operation_id:
            _fail("receipt scenario/operation identity mismatch")
        _validate_http(receipt, expected, operation_id)
        _validate_records(receipt, expected, operation_id, data_root)
        _validate_stop(receipt, expected, operation_id, data_root)
        if expected.get("release0_skip"):
            _validate_release0(receipt, adoption, data_root)
        return {
            "qualification": "implementation-evidence-only",
            "scenario": scenario["scenario"],
            "operation_id": operation_id,
            "receipt": receipt,
            "needs": list(NEEDS),
        }


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--scenario", type=Path, required=True)
    parser.add_argument("--worker", nargs="+", required=True)
    parser.add_argument("--report", type=Path)
    args = parser.parse_args(argv)
    try:
        result = execute_fixture(args.scenario, args.worker)
    except BridgeFixtureError as exc:
        parser.error(str(exc))
    if args.report:
        args.report.write_text(json.dumps(result, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    else:
        print(json.dumps(result, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
