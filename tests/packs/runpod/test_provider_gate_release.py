"""Provider pacing preserves exact cleanup and paid expiry boundaries."""

import threading
from types import SimpleNamespace

import pytest

from astrid.packs.runpod.provider import ProviderGate, ProviderRefused


def test_release_inventory_429_keeps_observation_unknown_and_cools_account(tmp_path, monkeypatch):
    import runpod_lifecycle.api as api

    from astrid.packs.runpod import provider, worker_preparation

    now = 1000.0
    gate = ProviderGate(tmp_path / "gate.json", clock=lambda: now, sleep=lambda _: pytest.fail("budget cannot wait"))
    monkeypatch.setattr(provider, "account_gate", lambda _: gate)
    monkeypatch.setattr("random.uniform", lambda *_: 0.25)
    attempts = []

    def limited(_):
        attempts.append(now)
        raise RuntimeError("RunPod pod listing failed: HTTP 429: limited")

    monkeypatch.setattr(api, "list_pods", limited)

    async def run():
        with pytest.raises(worker_preparation.TargetObservationError) as failed:
            await worker_preparation._release_inventory(SimpleNamespace(api_key="account"), SimpleNamespace())
        assert failed.value.gate == "inventory"
        assert failed.value.retryable
        assert isinstance(failed.value.__cause__, RuntimeError)

    import asyncio

    asyncio.run(run())
    with pytest.raises(ProviderRefused):
        gate.before_call(now + 1)
    assert attempts == [now]
    assert gate.retry_delay() == 5.25


def test_paid_deadline_enters_release_during_cooldown_and_arms_shared_retry(tmp_path, monkeypatch):
    from astrid.packs.runpod import worker_owner

    gate = ProviderGate(tmp_path / "gate.json", clock=lambda: 1000.)
    monkeypatch.setattr("random.uniform", lambda *_: 0.25)
    gate.after_response(429, "45")
    record = {"operation_id": "exact", "state": "claimed", "provider_account_ref": "account",
              "gpu_type": "gpu", "paid_limits": {"hourly_usd": 1},
              "lease": {"owner_epoch": "owner", "generation": 1, "state": "active",
                        "paid_expires_at": 999, "idle_expires_at": 1100}}
    owner = object.__new__(worker_owner.RunPodLeaseOwner)
    owner.lock = threading.RLock()
    owner.selection = SimpleNamespace(_read=lambda: record, config_factory=lambda _: SimpleNamespace(api_key="account"))
    owner._read = lambda: record
    owner.clock = lambda: 1000.
    owner.epoch = "owner"
    owner.closed = False
    owner.deadline_timer = None
    owner.expiry_retry_timer = None
    timers = []
    owner.timer = lambda delay, callback: timers.append((delay, callback))
    started = []

    def release(**kwargs):
        started.append(kwargs)
        raise ProviderRefused(45.25)

    owner.release = release
    monkeypatch.setattr(worker_owner, "account_gate", lambda _, **kwargs: gate)
    owner._expire("paid_deadline", 1, "exact")
    assert started == [{"reason": "paid_deadline", "_expected_operation": "exact"}]
    assert len(timers) == 1
    assert timers[0][0] == 45.25


def test_lifecycle_budget_does_not_wait_for_executor_shutdown(tmp_path, monkeypatch):
    import asyncio

    from astrid.packs.runpod import provider

    started = threading.Event()
    finish = threading.Event()
    completed = threading.Event()
    gate = ProviderGate(tmp_path / "gate.json", minimum_interval=0)
    monkeypatch.setattr(provider, "account_gate", lambda _: gate)
    monkeypatch.setattr("random.uniform", lambda *_: 0.25)

    def lifecycle():
        started.set()
        try:
            assert finish.wait(2), "test must release its lifecycle operation"
        finally:
            completed.set()

    async def budget_expires(future, *, timeout):
        assert timeout <= 30
        assert started.wait(1)
        future.cancel()
        raise TimeoutError

    monkeypatch.setattr(asyncio, "wait_for", budget_expires)

    async def run():
        with provider.provider_budget(30):
            with pytest.raises(TimeoutError):
                await provider.async_provider_call("account", lifecycle)

    try:
        asyncio.run(run())
        assert not completed.is_set()
        assert gate.retry_delay() > 5
    finally:
        finish.set()
        assert completed.wait(1)


def test_retry_timer_survives_unreadable_journal():
    from astrid.packs.runpod.worker_owner import RunPodLeaseOwner

    def unreadable():
        raise OSError("journal temporarily unavailable")

    owner = object.__new__(RunPodLeaseOwner)
    owner.selection = SimpleNamespace(_read=unreadable)
    assert owner._provider_retry_delay() == 1.0


def test_bounded_exact_read_feeds_gate_only_once(tmp_path, monkeypatch):
    import asyncio

    from astrid.packs.runpod import provider

    gate = ProviderGate(tmp_path / "gate.json", clock=lambda: 1000., minimum_interval=0)
    monkeypatch.setattr(provider, "account_gate", lambda _: gate)
    monkeypatch.setattr("random.uniform", lambda *_: 0.25)

    def transport():
        raise OSError("connection failed")

    def exact_read():
        return provider.provider_call("account", transport)

    async def run():
        with pytest.raises(OSError):
            await provider.async_provider_call("account", exact_read, already_gated=True)

    asyncio.run(run())
    assert gate.retry_delay() == 5.25


def test_liveness_binds_rotated_key_before_provider_observation(monkeypatch):
    from astrid.packs.runpod import worker_owner

    record = {"operation_id": "exact", "state": "claimed", "pod_id": "pod",
              "provider_account_ref": "account-one", "gpu_type": "gpu",
              "paid_limits": {"hourly_usd": 1}, "lease": {"state": "active"}}
    owner = object.__new__(worker_owner.RunPodLeaseOwner)
    owner.lock = threading.RLock()
    owner.closed = False
    owner._release_inflight = False
    owner._read = lambda: record
    owner.selection = SimpleNamespace(config_factory=lambda _: SimpleNamespace(api_key="rotated-key"))
    bound = []
    monkeypatch.setattr(worker_owner, "account_gate", lambda key, **kwargs: bound.append((key, kwargs)))

    def observe(key, pod):
        assert bound == [("rotated-key", {"account_ref": "account-one"})]
        assert (key, pod) == ("rotated-key", "pod")
        return "live"

    monkeypatch.setattr(worker_owner, "provider_pod_state", observe)
    assert owner.observe_liveness() == "live"
