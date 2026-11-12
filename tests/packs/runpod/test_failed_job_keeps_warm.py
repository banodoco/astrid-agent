"""Failure settlement preserves healthy paid machines and exact custody."""
import pytest

from astrid.packs.runpod import machine as rm
from tests.packs.runpod.test_generation_preparation import retained as retained  # noqa: F401
from tests.packs.runpod.test_machine import _claim_machine as _claim_machine, machine_case as machine_case  # noqa: F401
from tests.packs.runpod.test_remote_placement import sources as sources  # noqa: F401
from tests.packs.runpod.test_target_selection import case as case  # noqa: F401
from tests.packs.runpod.test_warm_session import lease_selection_case as lease_selection_case, small_lease as small_lease  # noqa: F401


def _failed(c):
    _claim_machine(c)
    job = c.job()
    lease = c.machine.claim(job)
    run = c.machine.start(lease, job)
    c.machine._checkpoint(lease, {"id": run.job_id, "state": "exited"})
    return lease


def test_refreshed_receipt_reuses_preparation(machine_case, monkeypatch):
    c = machine_case
    lease = _failed(c)
    updated = {**lease.state.installed, "receipt_sha256": "sha256:" + "f" * 64}
    def refresh(state, **kwargs):
        state.installed = updated
        return True
    monkeypatch.setattr(c.placement, "_canonical_installed", refresh)
    c.machine.release(lease, False, failure=rm.MachineError("job failed"))
    record = c.read()["lease"]
    assert record["state"] == "idle" and record["preparation"]["installed"] == updated
    assert record["idle_expires_at"] == min(c.clock() + record["idle_seconds"], record["paid_expires_at"])
    assert c.pod.resets[-1]["ok"] and c.deletes == []
    seen = []
    original = c.placement.prepare_claimed
    def prepare(*args, **kwargs):
        seen.append(kwargs.get("reuse_snapshot"))
        return original(*args, **kwargs)
    monkeypatch.setattr(c.placement, "prepare_claimed", prepare)
    second = c.machine.claim(c.job(2))
    assert seen == [updated] and c.creates == 1
    c.machine.release(second, False)


def test_unverified_installation_reprepares_same_pod(machine_case, monkeypatch):
    c = machine_case
    lease = _failed(c)
    original_verify = c.placement._canonical_installed
    monkeypatch.setattr(c.placement, "_canonical_installed", lambda *a, **k: False)
    c.machine.release(lease, False, failure=rm.MachineError("doctor failed"))
    record = c.read()["lease"]
    assert record["state"] == "idle" and record["preparation"]["state"] == "stale_after_failure"
    assert c.pod.resets[-1]["ok"] and c.deletes == []
    monkeypatch.setattr(c.placement, "_canonical_installed", original_verify)
    seen = []
    original = c.placement.prepare_claimed
    def prepare(*args, **kwargs):
        seen.append(kwargs.get("reuse_snapshot"))
        return original(*args, **kwargs)
    monkeypatch.setattr(c.placement, "prepare_claimed", prepare)
    second = c.machine.claim(c.job(2))
    assert seen == [None] and c.creates == 1 and c.read()["lease"]["preparation"]["state"] == "prepared"
    c.machine.release(second, False)


def test_unreachable_engine_releases(machine_case, monkeypatch):
    c = machine_case
    lease = _failed(c)
    def unavailable(*args, **kwargs):
        raise TimeoutError("unreachable")
    monkeypatch.setattr(c.placement, "_canonical_installed", unavailable)
    c.machine.release(lease, False, failure=rm.MachineError("failed"))
    assert c.read()["lease"]["ended"] == "engine_unreachable" and c.deletes == ["pod-1"]


@pytest.mark.parametrize("condition,reason", [
    ("receipt_id", "installation_unverified"),
    ("stop", "job_stop_unverified"),
    ("custody", "custody_changed"),
    ("digest", "output_digest_mismatch"),
])
def test_safety_reasons_release(machine_case, monkeypatch, condition, reason):
    c = machine_case
    lease = _failed(c)
    failure = rm.MachineError("failed")
    if condition == "receipt_id":
        def changed(state, **kwargs):
            state.installed = {**state.installed, "receipt_id": "different"}
            return True
        monkeypatch.setattr(c.placement, "_canonical_installed", changed)
    elif condition == "stop":
        lease.unsafe_job_stop = True
    elif condition == "custody":
        failure = rm.CustodyChanged("changed")
    else:
        failure = rm.DigestMismatch("changed")
    c.machine.release(lease, False, failure=failure)
    assert c.read()["lease"]["ended"] == reason and c.deletes == ["pod-1"]


def test_first_preparation_failure_is_retained_and_reprepared(machine_case, monkeypatch):
    import time

    c = machine_case
    _claim_machine(c)
    # This fixture shares one fake local engine deadline across all claims.
    c.context.deadline = time.monotonic() + 300
    original = c.placement._install
    def fail_after_install(state):
        original(state)
        raise rm.MachineError("doctor failed")
    monkeypatch.setattr(c.placement, "_install", fail_after_install)
    with pytest.raises(rm.MachineError):
        c.machine.claim(c.job())
    assert c.read()["lease"]["preparation"]["state"] == "stale_after_failure"
    assert c.read()["lease"]["state"] == "idle" and c.deletes == []
    with pytest.raises(rm.MachineError):
        c.machine.claim(c.job(2))
    assert c.read()["lease"]["preparation"]["state"] == "stale_after_failure"
    assert c.read()["lease"]["state"] == "idle" and c.creates == 1 and c.deletes == []
    monkeypatch.setattr(c.placement, "_install", original)
    lease = c.machine.claim(c.job(3))
    assert c.creates == 1 and c.read()["lease"]["preparation"]["state"] == "prepared"
    c.machine.release(lease, False)
