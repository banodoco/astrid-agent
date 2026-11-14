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
            asyncio.run(case.owner._select(case.grant))
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
        acquired = asyncio.run(case.owner._select(case.grant))
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
        task = asyncio.create_task(case.owner._select(case.grant))
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
    acquired = asyncio.run(case.owner._select(consent))
    assert acquired["state"] == "claimed"
    assert acquired["pod_id"] == "pod-1"
    assert case.creates == 1
    assert polls[-1] == elapsed[0] == 40
    assert provider.provider_remaining_budget() == 30.  # Individual-call default stays bounded.


def _fresh_acquisition(tmp_path):
    attempt = {"schema_version": "astrid.runpod.allocation-attempt.v1",
               "operation_id": "review-operation", "request_name": "review-pod",
               "state": "allocation_ready", "create_attempts": 0}
    path = tmp_path / "attempt.json"
    preparation.write_target_record(path, attempt)
    config = SimpleNamespace(api_key="fake", gpu_type_candidates=("gpu",),
                             ram_tiers_enabled=False, storage_volumes=(), storage_name=None,
                             network_volume_id=None, worker_image="fake-image")
    kwargs = dict(handle_path=path, attempt=attempt, volume={"size": 0}, pod_id=None,
                  max_wait_seconds=600, capacity_window_seconds=60,
                  retry_interval_seconds=15, ready_timeout_seconds=1)
    return config, path, kwargs


def test_cancellation_during_lifecycle_capacity_sleep_never_posts_again(tmp_path, monkeypatch):
    import json
    import threading
    import runpod_lifecycle
    from runpod_lifecycle import lifecycle
    from runpod_lifecycle.errors import ProviderCapacityUnavailable

    config, path, kwargs = _fresh_acquisition(tmp_path)
    gate = provider.ProviderGate(tmp_path / "gate.json", minimum_interval=0)
    monkeypatch.setattr(provider, "account_gate", lambda _: gate)
    sleeping, resume, finished = threading.Event(), threading.Event(), threading.Event()
    posts = []

    async def launch(*_, **__):
        posts.append(json.loads(path.read_text())["create_attempts"])
        if len(posts) == 1:
            raise lifecycle._CapacityLaunchFailure("no capacity") from ProviderCapacityUnavailable(503, "no capacity")
        return SimpleNamespace(id="late-pod", name="review-pod")

    async def pause(_):
        sleeping.set()
        await asyncio.to_thread(resume.wait, 1)

    original = lifecycle.launch_when_available

    async def observed(*args, **kwargs):
        try:
            return await original(*args, **kwargs)
        finally:
            finished.set()

    monkeypatch.setattr(lifecycle, "launch", launch)
    monkeypatch.setattr(runpod_lifecycle, "launch_when_available", observed)
    monkeypatch.setattr(asyncio, "sleep", pause)

    async def run():
        task = asyncio.create_task(preparation.acquire_target(config, **kwargs))
        assert await asyncio.to_thread(sleeping.wait, 1)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        resume.set()
        assert await asyncio.to_thread(finished.wait, 1)

    try:
        asyncio.run(run())
        assert posts == [1]
        state = json.loads(path.read_text())
        assert state["state"] == "capacity_retry_ready"
        assert state["safe_to_retry"] is True
        assert state["create_attempts"] == 1
    finally:
        resume.set()


@pytest.mark.parametrize("interrupt", ["cancel", "timeout"])
def test_started_create_late_result_retains_exact_custody(tmp_path, monkeypatch, interrupt):
    import json
    import threading
    import runpod_lifecycle

    config, path, kwargs = _fresh_acquisition(tmp_path)
    gate = provider.ProviderGate(tmp_path / "gate.json", minimum_interval=0)
    monkeypatch.setattr(provider, "account_gate", lambda _: gate)
    started, resume, returned, recorded = [threading.Event() for _ in range(4)]
    posts = []
    replace_record = preparation.replace_target_record

    def track_record(*args, **kwargs):
        replace_record(*args, **kwargs)
        if args[1].get("pod_id") == "late-pod":
            recorded.set()

    async def launch(*_, **__):
        posts.append(json.loads(path.read_text())["create_attempts"])
        started.set()
        assert await asyncio.to_thread(resume.wait, 1)
        returned.set()
        return SimpleNamespace(id="late-pod", name="review-pod")

    monkeypatch.setattr(runpod_lifecycle, "launch_when_available", launch)
    monkeypatch.setattr(preparation, "replace_target_record", track_record)
    if interrupt == "timeout":
        monkeypatch.setattr(provider, "provider_remaining_budget", lambda: 0.05)

    async def run():
        task = asyncio.create_task(preparation.acquire_target(config, **kwargs))
        assert await asyncio.to_thread(started.wait, 1)
        if interrupt == "cancel":
            task.cancel()
        with pytest.raises(asyncio.CancelledError if interrupt == "cancel" else TimeoutError):
            await task
        unresolved = json.loads(path.read_text())
        assert unresolved["reconciliation_required"] is True
        assert unresolved["safe_to_retry"] is False
        resume.set()
        assert await asyncio.to_thread(returned.wait, 1)
        assert await asyncio.to_thread(recorded.wait, 1), "late successful POST lost its exact pod ID"

    try:
        asyncio.run(run())
        state = json.loads(path.read_text())
        assert posts == [1]
        assert state["pod_id"] == "late-pod"
        assert state["operation_id"] == "review-operation"
        assert state["state"] == "allocated_unverified"
        assert state["reconciliation_required"] is True
        assert state["safe_to_retry"] is False
    finally:
        resume.set()


@pytest.mark.parametrize("route", ["attach", "reconcile"])
def test_attach_and_reconcile_preserve_initial_429_without_fallback(tmp_path, monkeypatch, route):
    from runpod_lifecycle import api, graphql

    config, path, kwargs = _fresh_acquisition(tmp_path)
    gate = provider.ProviderGate(tmp_path / "gate.json", minimum_interval=0)
    monkeypatch.setattr(provider, "account_gate", lambda _: gate)
    calls = []

    class Limited(Exception):
        status_code = 429
        retry_after = "120"

    def limited(*_):
        calls.append("graphql-429")
        raise Limited()

    def fallback(*_, **__):
        calls.append("rest-success")
        return SimpleNamespace(status_code=200, json=lambda: {"id": "pod", "desiredStatus": "RUNNING"})

    observed = preparation.RunPodTargetObservation(
        "pod", "review-pod", "RUNNING", "RUNNING", "gpu", "fake-image", None, None, 0, None, "now")

    async def observe(*_, **__):
        return observed

    async def ready(*_):
        return {"desired_status": "RUNNING"}

    monkeypatch.setattr(api, "_get_runpod", lambda: SimpleNamespace())
    monkeypatch.setattr(graphql, "get_pod", limited)
    monkeypatch.setattr(api.httpx, "get", fallback)
    monkeypatch.setattr(preparation, "observe_exact_target", observe)
    monkeypatch.setattr(preparation, "observe_and_confirm_acquired_target", observe)
    monkeypatch.setattr(preparation, "_wait_provider_ready", ready)
    attempt = {**kwargs["attempt"], "state": "attachment_pending" if route == "attach" else "allocation_unknown",
               "create_attempts": 0 if route == "attach" else 1,
               "reconciliation_required": True, "safe_to_retry": False}
    if route == "reconcile":
        attempt["pod_id"] = "pod"
    preparation.write_target_record(path, attempt)
    if route == "attach":
        operation = preparation.acquire_target(config, **{**kwargs, "attempt": attempt, "pod_id": "pod"})
    else:
        operation = preparation.reconcile_target(
            config, handle_path=path, attempt=attempt, volume={"size": 0}, ready_timeout_seconds=1)
    with pytest.raises(Limited):
        asyncio.run(operation)
    assert calls == ["graphql-429"]
    with pytest.raises(provider.ProviderRefused):
        provider.ProviderGate(gate.path).before_call(gate.clock() + 1)
    unresolved = __import__("json").loads(path.read_text())
    assert unresolved["reconciliation_required"] is True
    assert unresolved.get("pod_id", unresolved.get("requested_pod_id")) == "pod"
