"""Read-only first-exit evidence; synthetic values never become output text."""
import ast
import json
import os
from pathlib import Path
import subprocess

import pytest

from scripts import ci_windows_journey as w


def write(path, value):
    path.write_text(json.dumps(value), encoding="utf-8")


def records(root):
    state = {"pid": 123, "process_birth_id": "birth-secret", "support_root": str(root),
             "runtime_service_launch_id": "a" * 32, "runtime_instance_id": "instance-secret"}
    item = {"pid": 123, "process_birth_id": "birth-secret", "runtime_service_launch_id": "a" * 32,
            "job_drained": True}
    record = {"version": 1, "service_name": "pack-host", "support_root": str(root), "receipts": [item]}
    return state, record


def test_log_fixed_signatures_phases_and_no_secret_output(tmp_path):
    path = tmp_path / "service-pack-host.log"
    data = b"\n".join(w._EXIT_SIGNATURES.values()) + b'\n' + json.dumps({
        "diagnostic": "astrid.host_startup_phase", "phase": "registration_begin", "secret": "private-token"}).encode()
    data += b'\n' + json.dumps({"status": "failed", "terminal": True, "error": {
        "code": "private-code", "request_id": "private-id", "message": "private-token"}}).encode()
    path.write_bytes(data)
    result = w._exit_log_classification(path)
    assert set(result["categories"]) == set(w._EXIT_SIGNATURES) | {"registration_failure_record"}
    assert result["phases"] == ["registration_begin"]
    assert "private" not in json.dumps(result)
    assert result["absence_is_inconclusive"]


def test_log_missing_other_and_tail_bound(tmp_path):
    path = tmp_path / "service-pack-host.log"
    assert w._exit_log_classification(path)["classification"] == "unavailable"
    path.write_bytes(w._EXIT_SIGNATURES["no_retained_job_custody"] + b'X' * (w.PACK_HOST_DIAGNOSTIC_LOG_TAIL_BYTES + 1))
    result = w._exit_log_classification(path)
    assert result["classification"] == "other" and result["truncated"] is True
    assert result["categories"] == [] and result["absence_is_inconclusive"]


def test_projection_missing_matching_mismatch_invalid_and_bounds(tmp_path):
    reg, receipt = tmp_path / "generic-host.json", tmp_path / "pack-host-job-cleanup.json"
    state, record = records(tmp_path)
    assert w._exit_registration_receipt_projection(tmp_path)["receipt"]["status"] == "missing"
    write(reg, state)
    write(receipt, record)
    before = (reg.read_bytes(), receipt.read_bytes())
    result = w._exit_registration_receipt_projection(tmp_path)
    assert result["registration"]["schema"] == "valid_for_match"
    assert result["receipt"]["drained_match"] == "match_unverified_permissions_and_race"
    assert result["receipt"]["owner_only"] == result["registration"]["process_status"] == "unknown"
    assert (reg.read_bytes(), receipt.read_bytes()) == before
    assert "birth-secret" not in json.dumps(result) and str(tmp_path) not in json.dumps(result)
    record["receipts"][0]["runtime_service_launch_id"] = "b" * 32
    write(receipt, record)
    assert w._exit_registration_receipt_projection(tmp_path)["receipt"]["drained_match"] == "no_match_lexical_only"
    record["receipts"][0]["job_drained"] = "true"
    write(receipt, record)
    invalid = w._exit_registration_receipt_projection(tmp_path)["receipt"]
    assert invalid["schema"] == "invalid_or_unsupported_entry" and invalid["drained_match"] == "unknown"
    record["version"] = True
    write(receipt, record)
    assert w._exit_registration_receipt_projection(tmp_path)["receipt"]["schema"] == "invalid"
    receipt.write_text('{"version":1,"version":1}')
    assert w._exit_registration_receipt_projection(tmp_path)["receipt"]["status"] == "malformed"
    receipt.write_bytes(b'x' * (w.PACK_HOST_DIAGNOSTIC_MARKER_BYTES + 1))
    assert w._exit_registration_receipt_projection(tmp_path)["receipt"]["status"] == "oversized"
    write(receipt, records(tmp_path)[1])
    state["runtime_service_launch_id"] = "bad"
    write(reg, state)
    assert w._exit_registration_receipt_projection(tmp_path)["receipt"]["drained_match"] == "unknown"


def test_receipt_legacy_pid_birth_and_wrong_root(tmp_path):
    state, record = records(tmp_path)
    del state["runtime_service_launch_id"]
    write(tmp_path / "generic-host.json", state)
    write(tmp_path / "pack-host-job-cleanup.json", record)
    assert w._exit_registration_receipt_projection(tmp_path)["receipt"]["drained_match"].startswith("match_")
    record["receipts"][0]["process_birth_id"] = "different"
    write(tmp_path / "pack-host-job-cleanup.json", record)
    assert w._exit_registration_receipt_projection(tmp_path)["receipt"]["drained_match"] == "no_match_lexical_only"
    state["support_root"] = "untrusted-path"
    write(tmp_path / "generic-host.json", state)
    assert w._exit_registration_receipt_projection(tmp_path)["receipt"]["drained_match"] == "unknown"


def test_first_exit_captured_once_and_only_exact_child_log(tmp_path, monkeypatch):
    root = tmp_path / "data" / "runtime"
    native = tmp_path / "native"
    root.mkdir(parents=True)
    native.mkdir()
    evidence = tmp_path / "evidence"
    monkeypatch.setattr(w, "data_root", lambda: root.parent)
    monkeypatch.setattr(w, "EVIDENCE", evidence)
    monkeypatch.setattr(w, "version", lambda: {})
    monkeypatch.setenv(w.NATIVE_RUNTIME_LOG_DIR_ENV, str(native))
    (native / "runtime.log").write_bytes(w._EXIT_SIGNATURES["no_retained_job_custody"])
    (native / "service-pack-host.log").write_bytes(w._EXIT_SIGNATURES["missing_matching_job_drain_receipt"])
    health = {"status": "ok", "services": [{"name": "pack-host", "state": "restarting", "last_exit": 1}]}
    w.capture_pack_host_readiness(health, False)
    path = evidence / "pack-host-readiness-first-service-exit.json"
    first = path.read_bytes()
    assert json.loads(first)["exit_discriminator"]["child_log"]["categories"] == ["missing_matching_job_drain_receipt"]
    (native / "service-pack-host.log").write_text("changed")
    w.capture_pack_host_readiness(health, False)
    assert path.read_bytes() == first and len(first) <= w.PACK_HOST_DIAGNOSTIC_MANIFEST_BYTES


def test_against_real_frozen_r0_sources(tmp_path):
    agent_repo = os.environ.get("R0_AGENT_REPO")
    runtime_repo = os.environ.get("R0_RUNTIME_REPO")
    if not agent_repo or not runtime_repo:
        pytest.skip("R0 source repositories not supplied")
    def source(repo, revision, path):
        return subprocess.check_output(["git", "-C", repo, "show", f"{revision}:{path}"], text=True)
    guard = source(agent_repo, "3204bb00135bec8ee2d2d3fb21eed5abfceca649", "astrid/sdk/host_bootstrap.py")
    service = source(runtime_repo, "6d53a19070faf63fc15ae1f81417722edc2c8a5f", "runtime_protocol/services.py")
    host = source(agent_repo, "3204bb00135bec8ee2d2d3fb21eed5abfceca649", "astrid/core/execution/generic_host.py")
    guard_literals = {node.value for node in ast.walk(ast.parse(guard))
                      if isinstance(node, ast.Constant) and isinstance(node.value, str)}
    assert all(signature.decode() in guard_literals for signature in w._EXIT_SIGNATURES.values())
    assert '"service_name": service_name' in service and '"job_drained": True' in service
    assert '"host_registration_failed"' in host and '"registration_begin"' in host
    # Execute the actual R0 guard's item matcher, with only its path comparator supplied.
    tree = ast.parse(guard)
    node = next(n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == "_runtime_receipt_matches_state")
    node.returns = None
    for arg in node.args.args + node.args.kwonlyargs:
        arg.annotation = None
    module = ast.fix_missing_locations(ast.Module(body=[node], type_ignores=[]))
    ns = {"re": __import__("re"), "_same_windows_path": lambda a, b: w._same_projection_path(a, b)}
    exec(compile(module, "<real-r0-guard>", "exec"), ns)
    state, record = records(tmp_path)
    item = record["receipts"][0]
    matches = ns["_runtime_receipt_matches_state"]
    assert matches(item, state, str(tmp_path))
    item["runtime_service_launch_id"] = "b" * 32
    assert not matches(item, state, str(tmp_path))
    item["runtime_service_launch_id"] = "a" * 32
    item["process_birth_id"] = None
    assert not matches(item, state, str(tmp_path))
