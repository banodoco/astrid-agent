"""Real lease-owner recovery stays paced when provider calls keep returning 429."""

import pytest

from astrid.packs.runpod import worker_owner
from astrid.packs.runpod.provider import ProviderGate
from astrid.packs.runpod.worker_preparation import AcquisitionUnresolvedError
from tests.packs.runpod.test_warm_session import (
    lease_selection_case as lease_selection_case,
    small_lease as small_lease,
)


class Throttled(Exception):
    status_code = 429

    def __init__(self, retry_after=None):
        super().__init__("provider throttled the request")
        self.retry_after = retry_after


def _gate(clock, path):
    return ProviderGate(
        path,
        clock=clock,
        # Avoid firing owner timers from inside the gate's short lock yields.
        sleep=lambda seconds: setattr(clock, "now", clock.now + seconds),
    )


def test_inflight_create_recovery_keeps_exact_custody_and_backs_off_on_429(
    small_lease, monkeypatch, tmp_path
):
    from runpod_lifecycle import api
    from astrid.packs.runpod import provider

    c = small_lease
    owner = c.fresh()
    gate = _gate(c.clock, tmp_path / "acquisition-gate.json")
    monkeypatch.setattr(worker_owner, "account_gate", lambda *_args, **_kwargs: gate)
    monkeypatch.setattr(provider, "account_gate", lambda *_args, **_kwargs: gate)
    monkeypatch.setattr("random.uniform", lambda *_args: 0.25)

    # The fake lifecycle records the created pod, then loses the reply. The
    # journal must retain the one in-flight create for exact recovery.
    c.create_error = TimeoutError("create reply lost")
    with pytest.raises(AcquisitionUnresolvedError):
        owner.acquire(c.grant)
    record = c.read()
    assert record["state"] == "allocation_pending"
    assert record["create_attempts"] == 1
    assert record.get("pod_id") is None
    assert c.creates == 1

    requests = []

    def list_pods(_key):
        requests.append(c.clock())
        if len(requests) <= 3:
            raise Throttled("7" if len(requests) == 1 else None)
        return list(c.pods)

    monkeypatch.setattr(api, "list_pods", list_pods)
    retry_delays = []

    # Use the owner's real timer callbacks and real release/reconciliation
    # path. Each failed exact inventory call arms the next recovery timer.
    timer = owner.deadline_timer
    for _ in range(3):
        assert timer is not None
        c.clock.now = max(c.clock(), timer.due)
        timer.cancel()
        timer.callback()
        timer = owner.deadline_timer
        if timer is not None:
            retry_delays.append(timer.due - c.clock())
            assert timer.due - c.clock() >= 1.0

    assert len(requests) == 3
    gaps = [later - earlier for earlier, later in zip(requests, requests[1:])]
    assert all(delay >= 1.0 for delay in gaps)
    assert gaps[1] > gaps[0]
    assert len(retry_delays) == 3
    assert retry_delays[0] == 7.25
    assert retry_delays[1] > retry_delays[0]
    assert retry_delays[2] > retry_delays[1]
    record = c.read()
    assert record["state"] == "allocation_unknown"
    assert record["create_attempts"] == 1
    assert record.get("pod_id") is None
    assert c.creates == 1


def test_release_inventory_429_arms_owner_retry_from_real_release(small_lease, monkeypatch, tmp_path):
    from runpod_lifecycle import api
    from astrid.packs.runpod import provider

    c = small_lease
    owner = c.fresh()
    invocation = owner.acquire(c.grant)
    gate = _gate(c.clock, tmp_path / "release-gate.json")
    monkeypatch.setattr(worker_owner, "account_gate", lambda *_args, **_kwargs: gate)
    monkeypatch.setattr(provider, "account_gate", lambda *_args, **_kwargs: gate)
    monkeypatch.setattr("random.uniform", lambda *_args: 0.25)
    assert gate.retry_delay() == 5.0
    assert owner._provider_retry_delay(c.read()) == 1.0

    requests = []
    def reconcile_cleanup(_pod_id, _key, **_kwargs):
        requests.append(c.clock())
        raise Throttled("7" if len(requests) == 1 else None)

    monkeypatch.setattr(api, "reconcile_pod_cleanup", reconcile_cleanup)
    with pytest.raises(Throttled):
        owner.release(_expected_operation=invocation.operation_id)
    assert owner.deadline_timer is not None
    assert owner.deadline_timer.due - c.clock() == 7.25
    delays = []
    timer = owner.deadline_timer
    for _ in range(2):
        assert timer is not None
        c.clock.now = max(c.clock(), timer.due)
        timer.cancel()
        timer.callback()
        timer = owner.deadline_timer
        assert timer is not None
        delays.append(timer.due - c.clock())
    assert len(requests) == 3
    gaps = [later - earlier for earlier, later in zip(requests, requests[1:])]
    assert all(delay >= 1.0 for delay in gaps)
    assert gaps[1] > gaps[0]
    assert delays == [10.25, 20.25]
    assert c.read()["state"] == "cleanup_pending"
    assert c.read()["pod_id"] == "pod-1"


def test_provider_gate_retry_delay_default_remains_five_seconds(tmp_path):
    from tests.packs.runpod.test_warm_session import Clock

    clock = Clock()
    gate = _gate(clock, tmp_path / "default-gate.json")

    assert gate.retry_delay() == 5.0
    assert gate.retry_delay(minimum_seconds=1.0) == 1.0
