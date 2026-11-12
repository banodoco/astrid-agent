"""W39 review regressions, with fake provider I/O only."""

import asyncio
from types import SimpleNamespace

import pytest

from astrid.packs.runpod import provider, target_selection as selection
from astrid.packs.runpod import worker_preparation as preparation
from tests.packs.runpod.test_target_selection import case as case  # noqa: F401


def test_admission_refusal_restores_intent_then_reacquires(case, tmp_path, monkeypatch):
    import time

    now = [time.time()]
    gate = provider.ProviderGate(tmp_path / "gate.json", clock=lambda: now[0], minimum_interval=0)
    monkeypatch.setattr(provider, "account_gate", lambda _: gate)
    volume = {"id": case.prefs.volume_id, "name": case.prefs.volume_name,
              "size": case.prefs.volume_size_gb, "dataCenterId": None}

    async def discover(*_):
        gate.after_response(429, "120")
        return volume

    monkeypatch.setattr(selection, "resolve_existing_volume", discover)
    with provider.provider_budget(1):
        provider._provider_deadline.set(now[0] + 1)
        with pytest.raises(preparation.AcquisitionUnresolvedError):
            asyncio.run(case.owner.select(case.grant))
    assert case.creates == 0
    refused = case.owner._read()
    assert refused["state"] == "allocation_ready"
    assert refused["create_attempts"] == 0
    assert refused["safe_to_retry"] is True
    assert refused["reconciliation_required"] is False

    now[0] += 121

    async def available(*_):
        return volume

    monkeypatch.setattr(selection, "resolve_existing_volume", available)
    with provider.provider_budget(10):
        provider._provider_deadline.set(now[0] + 10)
        acquired = asyncio.run(case.owner.select(case.grant))
    assert acquired["pod_id"] == "pod-1"
    assert acquired["operation_id"] == refused["operation_id"]
    assert acquired["create_attempts"] == case.creates == 1



def test_selection_binding_cancels_while_alias_lock_is_held(case, tmp_path, monkeypatch):
    import threading
    from runtime_protocol import platform

    monkeypatch.setenv("ASTRID_RUNTIME_DATA_ROOT", str(tmp_path / "gates"))
    gate = provider.account_gate("fake-only", account_ref="account-a")
    alias_lock = gate.alias_path.with_suffix(".json.lock")
    held, cancelled, released = threading.Event(), threading.Event(), threading.Event()
    released_before_cancellation = []
    lookups = []

    def hold_lock():
        with alias_lock.open("r+") as stream:
            platform.lock_file(stream.fileno(), blocking=True)
            held.set()
            released_before_cancellation.append(not cancelled.wait(0.5))
            platform.unlock_file(stream.fileno())
        released.set()

    async def volume(*_):
        lookups.append("volume")
        raise RuntimeError("unexpected provider access after lock wait")

    monkeypatch.setattr(selection, "resolve_existing_volume", volume)
    thread = threading.Thread(target=hold_lock)
    thread.start()
    assert held.wait(1)

    async def run():
        task = asyncio.create_task(case.owner.select(case.grant))
        asyncio.get_running_loop().call_later(0.01, task.cancel)
        try:
            with pytest.raises(asyncio.CancelledError):
                await task
        finally:
            cancelled.set()

    try:
        asyncio.run(run())
    finally:
        cancelled.set()
        thread.join(1)
    assert released.is_set()
    assert released_before_cancellation == [False]
    assert lookups == []
    assert case.creates == 0


@pytest.mark.parametrize("capacity_seconds,max_usd", [(600., 1.), (None, 0.02)])
def test_policy_readiness_can_finish_after_thirty_seconds(case, tmp_path, monkeypatch, capacity_seconds, max_usd):
    import time
    from dataclasses import replace
    from datetime import timedelta
    import runpod_lifecycle
    from runpod_lifecycle import graphql, pod as pod_module

    wall = time.time()
    elapsed = [0.]
    stamp = case.now
    case.owner.clock = lambda: stamp + timedelta(seconds=elapsed[0])
    case.owner.monotonic = lambda: elapsed[0]
    monkeypatch.setattr(time, "time", lambda: wall + elapsed[0])
    monkeypatch.setattr(pod_module, "time", SimpleNamespace(monotonic=lambda: elapsed[0]))
    gate = provider.ProviderGate(tmp_path / "gate.json", clock=lambda: wall + elapsed[0], minimum_interval=0)
    monkeypatch.setattr(provider, "account_gate", lambda _: gate)
    original_launch = runpod_lifecycle.launch_when_available
    polls = []

    async def launch(config, *, name, **kwargs):
        allocated = await original_launch(config, name=name, **kwargs)
        config.transport, config.https_token = "https", "fake-token"
        return pod_module.Pod(allocated.id, name, config)

    def status(*_):
        polls.append(elapsed[0])
        return {"id": "pod-1", "desiredStatus": "RUNNING" if elapsed[0] >= 40 else "STARTING"}

    sleep = asyncio.sleep

    async def advance(seconds):
        elapsed[0] += seconds
        await sleep(0)

    monkeypatch.setattr(runpod_lifecycle, "launch_when_available", launch)
    monkeypatch.setattr(graphql, "get_pod", status)
    monkeypatch.setattr(asyncio, "sleep", advance)
    monkeypatch.setattr(pod_module.Pod, "https_transport", lambda _: SimpleNamespace(
        wait_ready=lambda *_: None, disconnect=lambda: None))
    consent = replace(case.grant, offer_digest=None, deadline=None, max_usd=max_usd,
                      preparation_seconds=1, execution_seconds=1, idle_seconds=1,
                      policy_digest=selection.policy_consent_digest(case.prefs, 1.),
                      max_hourly_usd=1., capacity_timeout_seconds=capacity_seconds)
    acquired = asyncio.run(case.owner.select(consent))
    assert acquired["state"] == "claimed"
    assert acquired["pod_id"] == "pod-1"
    assert case.creates == 1
    assert polls[-1] == elapsed[0] == 40
    assert provider.provider_remaining_budget() == 30.  # Individual-call default stays bounded.
