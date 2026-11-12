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
    assert c.creates == 1 and c.deletes == ["pod-1"]
    assert c.read()["state"] == "cleanup_complete"
    assert c.read()["cleanup"]["absence"]["exact_get_status"] == 404


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
