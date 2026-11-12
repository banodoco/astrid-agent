"""Paid acquisition stays bounded through provider checks and embedded preparation."""

import asyncio
import socket
import threading
from dataclasses import replace

import pytest

from astrid.packs.runpod import target_selection
from tests.packs.runpod.test_generation_preparation import retained as retained
from tests.packs.runpod.test_remote_placement import sources as sources
from tests.packs.runpod.test_warm_session import (
    lease_selection_case as lease_selection_case,
    small_lease as small_lease,
)


@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    monkeypatch.delenv("RUNPOD_API_KEY", raising=False)
    monkeypatch.setattr(socket.socket, "connect", lambda *args: pytest.fail("network forbidden"))


@pytest.mark.parametrize("reuse", [False, True])
def test_blocked_rate_lookup_releases_at_paid_expiry(small_lease, monkeypatch, reuse):
    from runpod_lifecycle import api

    c = small_lease
    owner = c.fresh()
    owner.start_fresh_claim()
    grant = replace(c.grant, preparation_seconds=0, execution_seconds=0, idle_seconds=30,
                    max_usd=3, offer_digest=None, deadline=None,
                    policy_digest=target_selection.policy_consent_digest(c.prefs, 1), max_hourly_usd=1)
    if reuse:
        owner.claim(grant, requested_idle_seconds=30)
        grant = owner.job_limits("embedded-job", {"idle_seconds": 0})
    entered, unblock, released = threading.Event(), threading.Event(), threading.Event()
    errors, release_times = [], []
    delete = api.terminate_pod

    def terminate(*args, **kwargs):
        release_times.append(c.clock())
        result = delete(*args, **kwargs)
        released.set()
        return result

    def rate(_pod_id):
        expiry = target_selection._utc(c.read()["paid_limits"]["expires_at"]).timestamp()
        c.clock.now = expiry - 1
        entered.set()
        assert unblock.wait(5), "rate lookup was not unblocked by the test"
        return 1.0

    def acquire():
        try:
            if reuse:
                owner.acquire(grant, requested_idle_seconds=0)
            else:
                owner.claim(grant, requested_idle_seconds=10)
        except BaseException as exc:
            errors.append(exc)

    monkeypatch.setattr(api, "terminate_pod", terminate)
    c.owner.observed_hourly_usd = rate
    thread = threading.Thread(target=acquire, daemon=True)
    thread.start()
    try:
        assert entered.wait(5), "rate verification was not reached"
        expiry = target_selection._utc(c.read()["paid_limits"]["expires_at"]).timestamp()
        # The provider remains blocked while the durable clock reaches expiry.
        c.clock.now = expiry
        assert released.wait(2), "paid release did not start while rate lookup was blocked"
        assert release_times == [expiry]
    finally:
        unblock.set()
        thread.join(5)
    assert not thread.is_alive()
    assert len(errors) == 1 and errors[0].runpod_gate == "lifetime"
    assert ("astrid machines claim" if c.read()["state"] == "cleanup_complete"
            else "astrid machines release") in str(errors[0])
    assert c.creates == 1 and c.deletes == ["pod-1"]
    assert c.read()["state"] == "cleanup_complete"
    assert c.read()["cleanup"]["absence"]["exact_get_status"] == 404


def test_explicit_release_completes_during_blocked_rate_lookup(small_lease, monkeypatch):
    from runpod_lifecycle import api

    c = small_lease
    owner = c.fresh()
    owner.start_fresh_claim()
    grant = replace(c.grant, preparation_seconds=0, execution_seconds=0, idle_seconds=30)
    entered, unblock, delete_started = threading.Event(), threading.Event(), threading.Event()
    errors = []
    delete = api.terminate_pod

    def terminate(*args, **kwargs):
        delete_started.set()
        return delete(*args, **kwargs)

    def rate(_pod_id):
        entered.set()
        assert unblock.wait(5), "blocked rate lookup was not released by the test"
        return 1.0

    def acquire():
        try:
            owner.claim(grant, requested_idle_seconds=10)
        except BaseException as exc:
            errors.append(exc)

    monkeypatch.setattr(api, "terminate_pod", terminate)
    c.owner.observed_hourly_usd = rate
    acquire_thread = threading.Thread(target=acquire, daemon=True)
    release_thread = None
    acquire_thread.start()
    try:
        assert entered.wait(5), "rate verification was not reached"
        release_thread = threading.Thread(target=lambda: owner.release(reason="explicit"), daemon=True)
        release_thread.start()
        assert delete_started.wait(2), "explicit release did not reach provider cleanup while lookup was blocked"
        acquire_thread.join(2)
        release_thread.join(2)
        assert not acquire_thread.is_alive(), "acquisition waited for the blocked provider lookup"
        assert not release_thread.is_alive(), "release waited for the blocked provider lookup"
    finally:
        unblock.set()
        acquire_thread.join(5)
        if release_thread is not None:
            release_thread.join(5)
    assert not acquire_thread.is_alive() and not release_thread.is_alive()
    assert len(errors) == 1 and isinstance(errors[0], target_selection.CapacityWaitCancelled)
    assert c.creates == 1 and c.deletes == ["pod-1"]
    assert c.read()["state"] == "cleanup_complete"


def test_expiry_cleanup_failure_and_retry_finish_while_rate_lookup_is_blocked(small_lease, monkeypatch):
    from runpod_lifecycle import api

    c = small_lease
    owner = c.fresh()
    owner.start_fresh_claim()
    grant = replace(c.grant, preparation_seconds=0, execution_seconds=0, idle_seconds=30)
    entered, unblock, delete_started = threading.Event(), threading.Event(), threading.Event()
    errors, retry_errors = [], []
    delete = api.terminate_pod

    def terminate(*args, **kwargs):
        delete_started.set()
        return delete(*args, **kwargs)

    def rate(_pod_id):
        expiry = target_selection._utc(c.read()["paid_limits"]["expires_at"]).timestamp()
        c.clock.now = expiry - 1
        entered.set()
        assert unblock.wait(5), "blocked rate lookup was not released by the test"
        return 1.0

    def acquire():
        try:
            owner.claim(grant, requested_idle_seconds=10)
        except BaseException as exc:
            errors.append(exc)

    monkeypatch.setattr(api, "terminate_pod", terminate)
    c.owner.observed_hourly_usd = rate
    c.delete_error = TimeoutError("delete response lost")
    acquire_thread = threading.Thread(target=acquire, daemon=True)
    acquire_thread.start()
    retry_thread = None
    try:
        assert entered.wait(5), "rate verification was not reached"
        expiry = target_selection._utc(c.read()["paid_limits"]["expires_at"]).timestamp()
        c.clock.now = expiry
        assert delete_started.wait(2), "expiry did not start exact cleanup while lookup was blocked"
        acquire_thread.join(2)
        assert not acquire_thread.is_alive(), "acquisition waited for the blocked provider lookup after cleanup failed"
        assert len(errors) == 1 and getattr(errors[0], "runpod_gate", None) == "lifetime"
        assert c.read()["state"] == "cleanup_pending"
        assert "astrid machines release" in str(errors[0])
        assert c.read()["cleanup_failure_type"] == "TimeoutError"
        assert owner.deadline_timer is not None

        due = owner.deadline_timer.due
        c.delete_error = None

        def retry():
            try:
                c.clock.advance(due - c.clock())
            except BaseException as exc:
                retry_errors.append(exc)

        retry_thread = threading.Thread(target=retry, daemon=True)
        retry_thread.start()
        retry_thread.join(2)
        assert not retry_thread.is_alive(), "timer retry waited for the blocked provider lookup"
        assert not retry_errors
        assert c.read()["state"] == "cleanup_complete"
    finally:
        unblock.set()
        acquire_thread.join(5)
        if retry_thread is not None:
            retry_thread.join(5)
    assert not acquire_thread.is_alive()
    assert retry_thread is None or not retry_thread.is_alive()
    assert c.creates == 1 and c.deletes == ["pod-1"]
    assert c.read()["state"] == "cleanup_complete"


@pytest.mark.parametrize("phase", ["status", "health", "inventory", "machine", "reuse_inventory"])
@pytest.mark.parametrize("trigger", ["cancel", "cancel_retry", "expiry_retry"])
@pytest.mark.parametrize("late_failure", [False, True])
def test_nested_observation_cannot_delay_cleanup_or_change_next_claim(
    small_lease, monkeypatch, phase, trigger, late_failure,
):
    import runpod_lifecycle
    from runpod_lifecycle import api
    from runpod_lifecycle.https import HttpsPodTransport
    from runpod_lifecycle.pod import Pod

    from astrid.packs.runpod import worker_preparation

    c = small_lease
    retry = trigger.endswith("retry")
    owner = c.fresh()
    owner.start_fresh_claim()
    grant = replace(c.grant, preparation_seconds=0, execution_seconds=0, idle_seconds=30)
    reuse = phase == "reuse_inventory"
    if reuse:
        owner.claim(grant, requested_idle_seconds=30)
        grant = owner.job_limits("embedded-job", {"idle_seconds": 0})
    entered, unblock, cancel = threading.Event(), threading.Event(), threading.Event()
    errors, observers = [], []

    def block(result):
        if not entered.is_set():
            observers.append(threading.current_thread())
            entered.set()
            assert unblock.wait(10), "observation was not released by the test"
            if late_failure:
                raise TimeoutError("late observation failed")
        return result

    if phase in {"status", "health"}:
        launch = runpod_lifecycle.launch_when_available

        async def real_readiness(config, **kwargs):
            pod = await launch(config, **kwargs)
            config.transport = "https"
            return Pod(pod.id, pod.name, config)

        def status(*_args):
            result = {"desired_status": "RUNNING", "ports": []}
            return block(result) if phase == "status" else result

        def health(*_args):
            return block(None) if phase == "health" else None

        monkeypatch.setattr(runpod_lifecycle, "launch_when_available", real_readiness)
        monkeypatch.setattr(api, "get_pod_status", status)
        monkeypatch.setattr(Pod, "https_transport", lambda _self: object.__new__(HttpsPodTransport))
        monkeypatch.setattr(HttpsPodTransport, "wait_ready", health)
        monkeypatch.setattr(HttpsPodTransport, "disconnect", lambda _self: None)
    elif phase in {"inventory", "reuse_inventory"}:
        observe = worker_preparation.observe_target

        def inventory(*args, **kwargs):
            return block(observe(*args, **kwargs))

        monkeypatch.setattr(worker_preparation, "observe_target", inventory)
    else:
        confirm = worker_preparation.confirm_acquired_machine

        def machine(*args, **kwargs):
            return block(confirm(*args, **kwargs))

        monkeypatch.setattr(worker_preparation, "confirm_acquired_machine", machine)

    def acquire():
        try:
            if reuse:
                owner.acquire(grant, requested_idle_seconds=0, cancelled=cancel.is_set)
            else:
                owner.claim(grant, requested_idle_seconds=10, cancelled=cancel.is_set)
        except BaseException as exc:
            errors.append(exc)

    thread = threading.Thread(target=acquire, daemon=True)
    thread.start()
    try:
        assert entered.wait(5), "nested provider observation was not reached"
        if retry:
            c.delete_error = TimeoutError("delete response lost")
        if trigger == "expiry_retry":
            c.clock.now = target_selection._utc(c.read()["paid_limits"]["expires_at"]).timestamp()
        else:
            # No release caller: cancellation itself must start exact cleanup.
            cancel.set()
        thread.join(2)
        assert not thread.is_alive(), "acquisition waited for an abandoned nested observation"
        assert len(errors) == 1
        if trigger == "expiry_retry":
            assert getattr(errors[0], "runpod_gate", None) == "lifetime"
        else:
            assert isinstance(errors[0], target_selection.CapacityWaitCancelled)
        if retry:
            assert c.read()["state"] == "cleanup_pending"
            assert c.read()["cleanup_failure_type"] == "TimeoutError"
            assert owner.deadline_timer is not None
            c.delete_error = None
            c.clock.advance(owner.deadline_timer.due - c.clock())
        assert c.read()["state"] == "cleanup_complete"
        assert c.creates == 1 and c.deletes == ["pod-1"]
        assert observers[0].is_alive(), "the test must keep the observation blocked through cleanup"

        # Give custody to a fresh operation before the abandoned read returns.
        next_grant = replace(c.grant, invocation_id="next-claim", preparation_seconds=0,
                             execution_seconds=0, idle_seconds=30)
        claimed = owner.claim(next_grant, requested_idle_seconds=10)
        assert claimed["pod_id"] == "pod-2" and c.creates == 2
        snapshot = c.owner.path.read_bytes()
        unblock.set()
        observers[0].join(2)
        assert not observers[0].is_alive()
        assert c.owner.path.read_bytes() == snapshot
        assert c.creates == 2 and c.deletes == ["pod-1"]
    finally:
        unblock.set()
        thread.join(5)
        for observer in observers:
            observer.join(5)


@pytest.mark.parametrize("release_fails", [False, True])
def test_lifetime_gate_failure_releases_immediately(small_lease, release_fails):
    c = small_lease
    owner = c.fresh()
    owner.start_fresh_claim()
    grant = replace(c.grant, preparation_seconds=0, execution_seconds=0, idle_seconds=30)
    if release_fails:
        c.delete_error = TimeoutError("delete response lost")

    def rate(_pod_id):
        raise target_selection.PaidSelectionError("paid lifetime expired", gate="lifetime")

    c.owner.observed_hourly_usd = rate
    with pytest.raises(target_selection.PaidSelectionError) as failure:
        owner.claim(grant, requested_idle_seconds=10)
    assert failure.value.runpod_gate == "lifetime"
    assert "\n" not in str(failure.value)
    assert ("astrid machines release" if release_fails else "astrid machines claim") in str(failure.value)
    assert c.creates == 1 and c.deletes == ["pod-1"]
    if release_fails:
        assert c.read()["state"] == "cleanup_pending"
        assert owner.deadline_timer is not None
        c.delete_error = None
        c.clock.advance(owner.deadline_timer.due - c.clock())
    assert c.read()["state"] == "cleanup_complete"


class BoundedLock:
    """Turn a cross-thread lock handoff deadlock into a bounded test failure."""

    def __init__(self, lock):
        self.lock = lock

    def __enter__(self):
        assert self.lock.acquire(timeout=2), "thread handoff waited on the calling thread's owner lock"
        return self

    def __exit__(self, *_args):
        self.lock.release()


def test_embedded_retained_generation_completes_and_paid_expiry_releases(retained):
    c = retained
    c.host.lock = BoundedLock(c.host.lock)
    c.host.start_fresh_claim()
    c.host.claim(replace(c.grant, invocation_id="manual-claim", preparation_seconds=0,
                         execution_seconds=0), requested_idle_seconds=c.grant.idle_seconds)
    paid_expiry = c.read()["lease"]["paid_expires_at"]
    consent = c.host.job_limits("embedded-job", {"preparation_seconds": 60, "idle_seconds": 1800})
    request = replace(c.request, consent=consent.consent, max_usd=consent.max_usd,
                      deadline=consent.deadline)

    async def prepare():
        prepared = c.adapter.prepare(request, consent)
        c.host.settle(prepared.lease)
        return prepared

    prepared = asyncio.run(prepare())
    assert prepared.lease.claim["pod_id"] == "pod-1"
    assert c.creates == 1 and not c.deletes
    assert c.read()["lease"]["paid_expires_at"] == paid_expiry
    c.clock.advance(paid_expiry - c.clock())
    assert c.deletes == ["pod-1"] and c.read()["state"] == "cleanup_complete"
