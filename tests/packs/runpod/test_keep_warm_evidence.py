"""Evidence boundaries for retained stale preparation."""
from threading import RLock
from types import SimpleNamespace

import pytest

from astrid.packs.runpod.worker_owner import RunPodLeaseOwner
from astrid.packs.runpod.worker_preparation import TargetObservationError, validate_lease_preparation


@pytest.mark.parametrize("child", ["preparation", "machine"])
def test_stale_preparation_requires_each_exact_job_stopped(child):
    job = {"id": "a" * 32, "state": "running"}
    preparation = {"schema": "astrid.runpod.lease-preparation.v1", "state": "stale_after_failure",
                   "operation_id": "op", "pod_id": "pod", "compatibility": {},
                   "bootstrap_digest": "a" * 16}
    lease = {"preparation": preparation}
    if child == "preparation":
        preparation["job"] = job
    else:
        lease["job"] = job
    record = {"operation_id": "op", "pod_id": "pod", "lease_compatibility": {}, "lease": lease}
    with pytest.raises(TargetObservationError, match="unresolved job"):
        validate_lease_preparation(record)
    job["state"] = "cancelled"
    validate_lease_preparation(record)


def test_recovery_preserves_release_cause():
    record = {"operation_id": "op", "lease": {"release_reason": "idle"}}
    calls = []
    owner = SimpleNamespace(lock=RLock(), closed=False, _read=lambda: record,
                            _save=lambda old, lease: calls.append(("save", lease)),
                            release=lambda **kw: calls.append(("release", kw)))
    RunPodLeaseOwner._retry_recovery(owner, "op")
    assert calls[0][1]["release_reason"] == "idle"
    assert calls[0][1]["release_mechanism"] == "recovery"
    assert calls[1][1] == {"reason": "idle", "_expected_operation": "op"}

from astrid.packs.runpod import machine as rm  # noqa: E402
from tests.packs.runpod.test_failed_job_keeps_warm import _failed  # noqa: E402
from tests.packs.runpod.test_generation_preparation import retained as retained  # noqa: E402,F401
from tests.packs.runpod.test_machine import _claim_machine, machine_case as machine_case  # noqa: E402,F401
from tests.packs.runpod.test_remote_placement import sources as sources  # noqa: E402,F401
from tests.packs.runpod.test_target_selection import case as case  # noqa: E402,F401
from tests.packs.runpod.test_warm_session import lease_selection_case as lease_selection_case, small_lease as small_lease  # noqa: E402,F401


@pytest.mark.parametrize("paid_expired", [False, True])
def test_verification_deadline_is_distinct_from_paid_expiry(machine_case, monkeypatch, paid_expired):
    c = machine_case
    lease = _failed(c)
    if paid_expired:
        c.clock.now = c.read()["lease"]["paid_expires_at"]
    else:
        monkeypatch.setattr(rm, "FAILURE_VERIFY_SECONDS", 0)
    c.machine.release(lease, False, failure=rm.MachineError("failed"))
    if paid_expired:
        assert c.read()["lease"]["ended"] == "paid_limit_exhausted"
        assert c.deletes == ["pod-1"]
    else:
        assert c.read()["lease"]["preparation"]["state"] == "stale_after_failure"
        assert c.read()["lease"]["state"] == "idle" and not c.deletes
        assert lease.release_reason == "verification_timeout"


def test_changed_source_digest_reprepares_on_same_pod(machine_case, monkeypatch):
    import time
    from astrid.packs.runpod import generation

    c = machine_case
    c.context.deadline = time.monotonic() + 300
    _claim_machine(c)
    first = c.machine.claim(c.job())
    c.host.settle(first.lease)
    old = c.read()["lease"]["preparation"]["bootstrap_digest"]
    new = "0" * 16 if old != "0" * 16 else "1" * 16
    monkeypatch.setattr(type(c.installation), "validate_sources", lambda *args: new)
    monkeypatch.setattr(generation, "source_digest", lambda *args: new)
    seen = []
    original = c.placement.prepare_claimed
    def prepare(*args, **kwargs):
        seen.append(kwargs.get("reuse_snapshot"))
        return original(*args, **kwargs)
    monkeypatch.setattr(c.placement, "prepare_claimed", prepare)
    second = c.machine.claim(c.job(2))
    assert seen == [None] and c.creates == 1 and not c.deletes
    assert c.read()["lease"]["preparation"]["bootstrap_digest"] == new
    c.machine.release(second, False)


def test_kept_preparation_failure_receipt_has_expiry_cost_and_state(machine_case, tmp_path):
    from tests.packs.runpod.test_failure_receipt import _only, _run

    c = machine_case
    c.pod.fail_jobs = {"engines install"}
    root = tmp_path / "failures"
    error, _, _ = _run(c, root)
    assert error is not None
    receipt = _only(root)
    machine = receipt["machine"]
    assert receipt["identity"]["machine_id"] == "pod-1"
    assert machine["after"] == "kept" and machine["release_reason"] == "preparation_failed"
    assert machine["expires_at"] == c.read()["lease"]["idle_expires_at"]
    assert machine["cost_usd"] is not None and machine["preparation_state"] == "stale_after_failure"
    from astrid.packs.runpod.failure_receipt import render
    assert "Kept until:" in render(receipt) and "stale_after_failure" in render(receipt)


def test_upload_failure_before_submission_keeps_pod(machine_case, monkeypatch, tmp_path):
    from tests.packs.runpod.test_failure_receipt import _run

    c = machine_case
    original = c.pod.put
    def put(source, destination):
        if destination.endswith("/job.json"):
            raise OSError("upload failed")
        return original(source, destination)
    monkeypatch.setattr(c.pod, "put", put)
    error, _, _ = _run(c, tmp_path / "failures")
    assert error is not None
    assert c.read()["lease"]["state"] == "idle" and not c.deletes
    assert c.read()["lease"]["preparation"]["state"] == "prepared"
    assert not any(" generate " in command for command in c.pod.job_log)


@pytest.mark.parametrize("reason", ["idle", "paid_deadline"])
def test_stale_pod_expires_on_its_existing_timer(machine_case, monkeypatch, reason):
    c = machine_case
    lease = _failed(c)
    monkeypatch.setattr(c.placement, "_canonical_installed", lambda *args, **kwargs: False)
    c.machine.release(lease, False, failure=rm.MachineError("failed"))
    record = c.read()
    assert record["lease"]["preparation"]["state"] == "stale_after_failure"
    c.clock.now = record["lease"]["idle_expires_at" if reason == "idle" else "paid_expires_at"]
    c.host._expire(reason, record["lease"]["generation"], record["operation_id"])
    assert c.read()["lease"]["ended"] == reason and c.deletes == ["pod-1"]


def test_unverified_preparation_stop_receipt_names_pending_pod(machine_case, tmp_path):
    from tests.packs.runpod.test_failure_receipt import _only, _run

    c = machine_case
    c.lost_start = c.cancel_unknown = True
    root = tmp_path / "failures"
    error, _, _ = _run(c, root)
    assert error is not None
    record = c.read()
    assert record["state"] == "cleanup_pending" and not c.deletes
    assert record["lease"]["preparation"]["state"] == "unresolved"
    receipt = _only(root)
    assert receipt["identity"]["machine_id"] == "pod-1"
    assert receipt["machine"]["after"] == "unknown"
    assert receipt["machine"]["release_reason"] == "preparation_unverified"
