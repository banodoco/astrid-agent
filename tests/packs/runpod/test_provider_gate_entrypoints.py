"""Product gate coverage with no provider transport."""

import asyncio
import threading
from types import SimpleNamespace

import pytest

from astrid.packs.runpod import provider, worker_preparation as preparation
from tests.packs.runpod.test_target_selection import case as case  # noqa: F401


@pytest.mark.parametrize("entry", ["confirm", "count", "catalog"])
def test_graphql_entrypoints_observe_and_set_shared_cooldown(tmp_path, monkeypatch, entry, real_acquisition_confirmation):
    from runpod_lifecycle import graphql
    from runpod.api import graphql as sdk_graphql

    now = [1000.]
    monkeypatch.setattr("random.uniform", lambda *_: 0.)
    gate = provider.ProviderGate(tmp_path / "gate.json", clock=lambda: now[0], minimum_interval=0)
    monkeypatch.setattr(provider, "account_gate", lambda _: gate)
    calls = []

    class Limited(Exception):
        status_code = 429
        retry_after = "120"

    def limited(*args, **kwargs):
        calls.append(entry)
        raise Limited()

    monkeypatch.setattr(graphql, "get_pod", limited)
    monkeypatch.setattr(sdk_graphql, "run_graphql_query", limited)
    contract = preparation.RunPodTargetContract("pod", "gpu", "image", None, None, 0)

    def invoke():
        if entry == "confirm":
            return preparation.confirm_acquired_machine("fake", contract, timeout_seconds=0)
        if entry == "count":
            return preparation.read_acquired_gpu_count("fake", "pod")
        return preparation._gpu_type_from_graphql("gpu", "fake")

    gate.after_response(429, "120")
    with provider.provider_budget(1):
        provider._provider_deadline.set(now[0] + 1)
        with pytest.raises(Exception):
            invoke()
    assert calls == []
    now[0] += 120
    with provider.provider_budget(1):
        provider._provider_deadline.set(now[0] + 1)
        with pytest.raises(Exception):
            invoke()
    assert calls == [entry]
    fresh = provider.ProviderGate(gate.path, clock=lambda: now[0])
    with pytest.raises(provider.ProviderRefused):
        fresh.before_call(now[0] + 1)


def test_launch_observes_cooldown_without_unjournaled_retry(case, tmp_path, monkeypatch):
    from astrid.packs.runpod import target_selection

    gate = provider.ProviderGate(tmp_path / "gate.json")
    gate.after_response(429, "120")
    monkeypatch.setattr(provider, "account_gate", lambda _: gate)

    async def already_observed_volume(*_):
        return {"id": case.prefs.volume_id, "name": case.prefs.volume_name,
                "size": case.prefs.volume_size_gb, "dataCenterId": None}

    monkeypatch.setattr(target_selection, "resolve_existing_volume", already_observed_volume)
    with provider.provider_budget(0.1):
        with pytest.raises(preparation.AcquisitionUnresolvedError):
            asyncio.run(case.owner.select(case.grant))
    assert case.creates == 0
    if case.owner.path.exists():
        assert case.owner._read()["create_attempts"] <= 1


def test_cancellation_during_admission_never_starts_operation(tmp_path, monkeypatch):
    gate = provider.ProviderGate(tmp_path / "gate.json", minimum_interval=0)
    gate.after_response(429, "1")
    monkeypatch.setattr(provider, "account_gate", lambda _: gate)
    waiting, resume, completed = threading.Event(), threading.Event(), threading.Event()
    calls = []
    after_cooldown = gate.clock() + 10

    def sleep(_):
        waiting.set()
        assert resume.wait(1)
        gate.clock = lambda: after_cooldown

    gate.sleep = sleep

    def operation():
        calls.append("provider-io")

    original = gate.call

    def tracked(*args, **kwargs):
        try:
            return original(*args, **kwargs)
        finally:
            completed.set()

    monkeypatch.setattr(gate, "call", tracked)

    async def run():
        task = asyncio.create_task(provider.async_provider_call("fake", operation))
        assert await asyncio.to_thread(waiting.wait, 1)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        resume.set()
        assert await asyncio.to_thread(completed.wait, 1)

    try:
        asyncio.run(run())
        assert calls == []
    finally:
        resume.set()


def test_readiness_polls_enter_the_gate_individually(tmp_path, monkeypatch):
    from runpod_lifecycle import api, graphql

    gate = provider.ProviderGate(tmp_path / "gate.json", minimum_interval=0)
    monkeypatch.setattr(provider, "account_gate", lambda _: gate)
    admissions = []
    original = gate.before_call

    def admit(deadline):
        admissions.append(deadline)
        return original(deadline)

    monkeypatch.setattr(gate, "before_call", admit)
    monkeypatch.setattr(graphql, "get_pod", lambda *_: {"desiredStatus": "RUNNING"})
    monkeypatch.setattr(api, "_normalize_pod_status", lambda _, record: {"desired_status": record["desiredStatus"]})

    class Pod:
        id = "pod"

        async def status(self):
            return {"desired_status": "RUNNING"}

        async def wait_ready(self, *, timeout):
            assert timeout == 1
            assert await self.status() == {"desired_status": "RUNNING"}
            return await self.status()

    result = asyncio.run(preparation._wait_provider_ready(SimpleNamespace(api_key="fake"), Pod(), 1))
    assert result == {"desired_status": "RUNNING"}
    assert len(admissions) == 2


def test_readiness_429_preserves_shared_cooldown_without_fallbacks(tmp_path, monkeypatch):
    from runpod_lifecycle import graphql

    gate = provider.ProviderGate(tmp_path / "gate.json", minimum_interval=0)
    monkeypatch.setattr(provider, "account_gate", lambda _: gate)
    calls = []

    class Limited(Exception):
        status_code = 429
        retry_after = "120"

    def limited(*_):
        calls.append("graphql")
        raise Limited()

    monkeypatch.setattr(graphql, "get_pod", limited)

    class Pod:
        id = "pod"

        async def status(self):
            pytest.fail("ungated status fallbacks must not run")

        async def wait_ready(self, *, timeout):
            return await self.status()

    with pytest.raises(Limited):
        asyncio.run(preparation._wait_provider_ready(SimpleNamespace(api_key="fake"), Pod(), 1))
    assert calls == ["graphql"]
    with pytest.raises(provider.ProviderRefused):
        provider.ProviderGate(gate.path).before_call(gate.clock() + 1)


def test_cleanup_replaces_acquisition_budget_and_preserves_its_own_deadline():
    with provider.provider_budget(0):
        expired = provider._provider_deadline.get()
        with provider.provider_budget(1, cleanup=True):
            cleanup = provider._provider_deadline.get()
            assert cleanup > expired
            with provider.provider_budget(30, cleanup=True):
                assert provider._provider_deadline.get() == cleanup


def test_cleanup_retry_scheduling_does_not_wait_on_pacing_lock(tmp_path, monkeypatch):
    import time

    from runtime_protocol import platform
    from astrid.packs.runpod import worker_owner

    monkeypatch.setenv("ASTRID_RUNTIME_DATA_ROOT", str(tmp_path))
    gate = provider.account_gate("fake", account_ref="account")
    alias_lock = next(gate.path.parent.glob("*.account.json.lock"))
    owner = object.__new__(worker_owner.RunPodLeaseOwner)
    owner.selection = SimpleNamespace(config_factory=lambda _: SimpleNamespace(api_key="fake"))
    record = {"provider_account_ref": "account", "gpu_type": "gpu", "paid_limits": {"hourly_usd": 1}}
    now = [1000.]
    monkeypatch.setattr(time, "time", lambda: now[0])
    monkeypatch.setattr(time, "sleep", lambda seconds: now.__setitem__(0, now[0] + seconds))
    with alias_lock.open("r+") as locked:
        platform.lock_file(locked.fileno(), blocking=False)
        assert owner._provider_retry_delay(record) == 1.
        assert now[0] == 1000.  # No fresh 30-second alias-lock budget.
