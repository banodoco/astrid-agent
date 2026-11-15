from __future__ import annotations

import subprocess
import sys
import time
from pathlib import Path

import pytest

from astrid.core.execution.guards import (
    ExecutionDeadlineError,
    ExecutionGuardPolicy,
    EvidenceCapError,
    WarmReuseExpectationError,
)
import astrid.core.execution.generic_host as generic_host
from astrid.core.execution.generic_host import GenericPackHost


def test_generated_evidence_cap_is_enforced(tmp_path) -> None:
    (tmp_path / "evidence.bin").write_bytes(b"12345")
    policy = ExecutionGuardPolicy(evidence_cap_bytes=4)
    with pytest.raises(EvidenceCapError, match="exceeds cap") as failure:
        policy.assert_evidence_cap(tmp_path)
    assert failure.value.diagnostic == {
        "category": "attempt_cap_exceeded",
        "attempt_observed_bytes": 5,
        "cap_bytes": 4,
        "observed_bytes": 5,
        "generated_file_count": 1,
        "immutable_file_count": 0,
        "immutable_input_bytes": 0,
        "immutable_input_manifest_digest": (
            "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855"
        ),
        "vanished_file_count": 0,
        "path_classes": {"evidence.bin": {"files": 1, "bytes": 5}},
        "largest_paths": [
            {"path": "evidence.bin", "bytes": 5, "classification": "generated"}
        ],
        "largest_paths_truncated": False,
    }

    policy = ExecutionGuardPolicy(evidence_cap_bytes=5)
    assert policy.assert_evidence_cap(tmp_path)["observed_bytes"] == 5


def test_generated_evidence_scan_tolerates_a_file_vanishing_during_render(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    vanished = tmp_path / "element-2744.jpeg"
    retained = tmp_path / "element-2745.jpeg"
    vanished.write_bytes(b"gone")
    retained.write_bytes(b"kept")

    original_stat = Path.stat

    def stat_without_vanished(path, *args, **kwargs):
        if path == vanished:
            vanished.unlink(missing_ok=True)
            raise FileNotFoundError(path)
        return original_stat(path, *args, **kwargs)

    monkeypatch.setattr(Path, "stat", stat_without_vanished)
    policy = ExecutionGuardPolicy(evidence_cap_bytes=64)
    assert policy.evidence_bytes(tmp_path) == len(b"kept")


def test_generated_evidence_scan_tolerates_outer_walk_directory_vanishing(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    original_rglob = Path.rglob

    def rglob_with_vanished_directory(path, pattern):
        if path == tmp_path:
            raise FileNotFoundError(path / "render-service")
        return original_rglob(path, pattern)

    monkeypatch.setattr(Path, "rglob", rglob_with_vanished_directory)
    policy = ExecutionGuardPolicy(evidence_cap_bytes=64)
    measurement = policy.evidence_measurement(tmp_path)
    assert measurement["observed_bytes"] == 0
    assert measurement["vanished_file_count"] == 1


def test_generated_evidence_scan_tolerates_a_file_vanishing_during_input_hash(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = tmp_path / "source.bin"
    source.write_bytes(b"input")
    policy = ExecutionGuardPolicy(evidence_cap_bytes=64)
    baseline = policy.immutable_input_baseline(tmp_path)

    original_read_bytes = Path.read_bytes

    def read_without_source(path):
        if path == source:
            source.unlink(missing_ok=True)
            raise FileNotFoundError(path)
        return original_read_bytes(path)

    monkeypatch.setattr(Path, "read_bytes", read_without_source)
    assert policy.evidence_bytes(tmp_path, immutable_inputs=baseline) == 0


def test_generated_evidence_scan_still_fails_closed_on_other_os_errors(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    evidence = tmp_path / "evidence.bin"
    evidence.write_bytes(b"evidence")
    original_stat = Path.stat

    def stat_with_permission_error(path, *args, **kwargs):
        if path == evidence:
            raise PermissionError(path)
        return original_stat(path, *args, **kwargs)

    monkeypatch.setattr(Path, "stat", stat_with_permission_error)
    policy = ExecutionGuardPolicy(evidence_cap_bytes=64)
    with pytest.raises(EvidenceCapError, match="cannot measure generated evidence"):
        policy.evidence_bytes(tmp_path)


def test_generated_evidence_scan_error_has_distinct_diagnostic(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    evidence = tmp_path / "evidence.bin"
    evidence.write_bytes(b"evidence")
    original_stat = Path.stat

    def stat_with_permission_error(path, *args, **kwargs):
        if path == evidence:
            raise PermissionError(path)
        return original_stat(path, *args, **kwargs)

    monkeypatch.setattr(Path, "stat", stat_with_permission_error)
    policy = ExecutionGuardPolicy(evidence_cap_bytes=64)
    with pytest.raises(EvidenceCapError) as failure:
        policy.evidence_measurement(tmp_path)
    assert failure.value.diagnostic == {
        "category": "scan_error",
        "error_type": "PermissionError",
    }


def test_generated_budget_accumulates_and_excludes_immutable_inputs(tmp_path) -> None:
    inputs = tmp_path / "inputs"
    inputs.mkdir()
    (inputs / "source.bin").write_bytes(b"input-bytes")
    output = tmp_path / "generated.log"
    output.write_bytes(b"123")
    policy = ExecutionGuardPolicy(evidence_cap_bytes=5)
    baseline = policy.immutable_input_baseline(inputs)
    first = policy.assert_evidence_cap(tmp_path, immutable_inputs=baseline)
    assert first["observed_bytes"] == 3
    output.write_bytes(b"12345")
    second = policy.assert_evidence_cap(tmp_path, immutable_inputs=baseline)
    assert second["attempt_delta_bytes"] == 2
    assert second["run_observed_bytes"] == 5
    output.write_bytes(b"123456")
    with pytest.raises(EvidenceCapError, match="exceeds cap"):
        policy.assert_evidence_cap(tmp_path, immutable_inputs=baseline)


def test_live_budget_blocks_retained_roots_and_recovers_after_cleanup(tmp_path) -> None:
    import shutil
    policy = ExecutionGuardPolicy(evidence_cap_bytes=5)
    first, second, third = (tmp_path / name for name in ("first", "second", "third"))
    for directory in (first, second, third):
        directory.mkdir()
    (first / "out").write_bytes(b"123")
    (second / "out").write_bytes(b"456")
    (third / "out").write_bytes(b"7")
    policy.assert_evidence_cap(first)
    with pytest.raises(EvidenceCapError, match="on disk") as failure:
        policy.assert_evidence_cap(second)
    assert failure.value.diagnostic["category"] == "live_budget_exceeded"
    assert failure.value.diagnostic["path_classes"] == {"out": {"files": 1, "bytes": 3}}
    with pytest.raises(EvidenceCapError, match="on disk"):
        policy.assert_evidence_cap(third)
    shutil.rmtree(second)
    assert policy.assert_evidence_cap(third)["run_observed_bytes"] == 4
    assert policy.evidence_budget.charged_bytes == 4
    assert policy.evidence_budget.live_attempts == 2


def test_modified_or_managed_input_is_not_exempt(tmp_path) -> None:
    inputs = tmp_path / "inputs"
    managed = tmp_path / "managed-objects"
    inputs.mkdir()
    managed.mkdir()
    input_file = inputs / "source.bin"
    managed_file = managed / "asset.bin"
    input_file.write_bytes(b"input-data")
    managed_file.write_bytes(b"managed-data")
    policy = ExecutionGuardPolicy(evidence_cap_bytes=64)
    baseline = policy.immutable_input_baseline(inputs)
    baseline.update(policy.immutable_input_baseline(managed))
    assert policy.assert_evidence_cap(tmp_path, immutable_inputs=baseline)["observed_bytes"] == 0
    input_file.write_bytes(b"changedata")
    assert policy.assert_evidence_cap(tmp_path, immutable_inputs=baseline)["observed_bytes"] == len(b"changedata")


def test_running_writer_is_detected_while_still_alive(tmp_path) -> None:
    output = tmp_path / "running-output.bin"
    process = subprocess.Popen(
        [
            sys.executable,
            "-c",
            "from pathlib import Path; import sys, time; Path(sys.argv[1]).write_bytes(b'x' * 2048); time.sleep(30)",
            str(output),
        ]
    )
    policy = ExecutionGuardPolicy(evidence_cap_bytes=1024)
    try:
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            if output.exists():
                assert process.poll() is None
                with pytest.raises(EvidenceCapError, match="exceeds cap"):
                    policy.assert_evidence_cap(tmp_path)
                break
            time.sleep(0.01)
        else:
            pytest.fail("writer did not publish evidence before watchdog deadline")
    finally:
        process.terminate()
        process.wait(timeout=5)


def test_deadline_gate_rejects_expired_attempt() -> None:
    policy = ExecutionGuardPolicy(evidence_cap_bytes=1, deadline_seconds=1)
    with pytest.raises(ExecutionDeadlineError, match="deadline"):
        policy.assert_deadline(time.monotonic() - 0.001)


def test_warm_expectation_is_not_derived_from_a_listener_port() -> None:
    cold = ExecutionGuardPolicy(evidence_cap_bytes=1)
    assert cold.warm_expectation() == {
        "warm_reuse_expected": False,
        "listener_port": None,
        "port_independent": True,
    }
    warm = ExecutionGuardPolicy(
        evidence_cap_bytes=1,
        warm_reuse_expected=True,
    )
    assert warm.warm_expectation()["warm_reuse_expected"] is True
    with pytest.raises(WarmReuseExpectationError):
        warm.warm_expectation(listener_port=0)


def test_warm_expectation_requires_a_boolean() -> None:
    with pytest.raises(WarmReuseExpectationError):
        ExecutionGuardPolicy(
            evidence_cap_bytes=1,
            warm_reuse_expected=1,
        )


def test_generic_host_carries_the_guard_policy_without_a_listener_port(tmp_path) -> None:
    policy = ExecutionGuardPolicy(
        evidence_cap_bytes=1,
        warm_reuse_expected=False,
    )
    host = GenericPackHost(pack_roots=[tmp_path], execution_policy=policy)
    try:
        assert host.execution_policy is policy
        assert host.execution_policy.warm_expectation()["listener_port"] is None
    finally:
        host.shutdown()


def test_cleanup_helper_does_not_claim_absence_after_recursive_delete_error(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "attempt"
    root.mkdir()
    monkeypatch.setattr(
        generic_host.shutil,
        "rmtree",
        lambda _path: (_ for _ in ()).throw(FileNotFoundError("descendant vanished")),
    )
    with pytest.raises(generic_host.HostError, match="cleanup was not verified"):
        generic_host._cleanup_ephemeral_attempt(root)


def test_cleanup_helper_fails_closed_on_root_observation_error(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "attempt"
    root.mkdir()
    monkeypatch.setattr(
        generic_host,
        "_strict_root_exists",
        lambda _path: (_ for _ in ()).throw(generic_host.HostError("injected observation failure")),
    )
    with pytest.raises(generic_host.HostError, match="injected observation failure"):
        generic_host._cleanup_ephemeral_attempt(root)


def test_cleanup_failure_is_recorded_and_does_not_block_admissions(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    host = GenericPackHost(pack_roots=[tmp_path])
    root = tmp_path / "attempt"
    root.mkdir()
    monkeypatch.setattr(
        generic_host,
        "_cleanup_ephemeral_attempt",
        lambda _path: (_ for _ in ()).throw(generic_host.HostError("injected cleanup failure")),
    )
    with pytest.raises(generic_host.HostError, match="injected cleanup failure"):
        host._cleanup_ephemeral_attempt_or_latch(root)
    assert host.last_cleanup_receipt == {
        "path": str(root),
        "intended_disposition": "deleted",
        "status": "uncertain",
        "errors": ["injected cleanup failure"],
    }
    assert root.is_dir() and "injected cleanup failure" in host._unresolved_task_scopes[0]
    assert str(root) in host._unresolved_task_scopes[0]
    # Nothing is blocked: the next admission proceeds to its ordinary checks.
    with pytest.raises(generic_host.HostError, match="runtime client is required"):
        host.run_task({}, lease_token="", attempt_id="", fence=1)



def test_host_live_evidence_publication_uses_measured_charge_and_private_atomic_snapshot(tmp_path):
    import json
    import os
    import threading
    from astrid.core.execution.generic_host import GenericPackHost
    from astrid.core.execution.guards import EvidenceBudget, ExecutionGuardPolicy
    from astrid.core.execution.local_engine_idle import LocalEngineIdle
    budget = EvidenceBudget()
    host = GenericPackHost.__new__(GenericPackHost)
    host._local_engine_idle = LocalEngineIdle()
    host.execution_policy = ExecutionGuardPolicy(evidence_budget=budget, evidence_cap_bytes=100)
    host.evidence_status_path = tmp_path / "generic-host.evidence.json"
    host._evidence_observation_lock = threading.RLock()
    host._evidence_observation_binding = {"process_birth_id": "original", "runtime_instance_id": "instance", "source_closure_digest": "closure"}
    host._handoff_accounting_owner, host._claim_slots = host, ()
    from astrid.core.execution.warm_hold import WarmHolds
    host.warm_holds = WarmHolds()
    attempt = tmp_path / "attempt"; attempt.mkdir()
    budget.account(str(attempt), 23, 100)
    host._publish_evidence_status()
    record = json.loads(host.evidence_status_path.read_text())
    assert record["charged_bytes"] == 23 and record["live_attempts"] == 1
    assert record["warm_holds"] == []
    assert record["pid"] == os.getpid() and record["process_birth_id"] == "original"
    assert record["runtime_instance_id"] == "instance" and record["source_closure_digest"] == "closure"
    assert record["observation_kind"] == "measured_live_evidence_budget"
    assert host.evidence_status_path.stat().st_mode & 0o777 == 0o600
    budget.release(str(attempt))
    host._publish_evidence_status()
    record = json.loads(host.evidence_status_path.read_text())
    assert record["charged_bytes"] == 0 and record["live_attempts"] == 0
    assert not list(tmp_path.glob('.*.tmp'))


def test_cleanup_doubt_record_is_written_for_doctor_and_never_gates(tmp_path) -> None:
    import json
    from astrid.core.execution.process_group import read_cleanup_latch
    host = GenericPackHost(pack_roots=[tmp_path])
    host.cleanup_latch_path = tmp_path / "latch.json"
    host._latch_cleanup_uncertainty("Codex credential copy not removed: /x/codex-home/auth.json")
    record = read_cleanup_latch(host.cleanup_latch_path)
    assert record["notes"] == ["Codex credential copy not removed: /x/codex-home/auth.json"]
    from astrid.core.gateway.diagnostics import collect_product_observations
    runtime = tmp_path / "runtime"
    runtime.mkdir()
    host.cleanup_latch_path.rename(runtime / host.cleanup_latch_path.name)
    from astrid.core.execution.process_group import CLEANUP_LATCH_NAME
    (runtime / host.cleanup_latch_path.name).rename(runtime / CLEANUP_LATCH_NAME)
    shown = collect_product_observations(str(tmp_path), {})["cleanupLatch"]
    assert shown["unresolved"] and not shown["blocked"] and shown["record"]["notes"]
    with pytest.raises(generic_host.HostError, match="runtime client is required"):
        host.run_task({}, lease_token="", attempt_id="", fence=1)
