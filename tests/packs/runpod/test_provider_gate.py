from __future__ import annotations

import threading
from datetime import datetime, timezone
from email.utils import format_datetime

import pytest

from astrid.packs.runpod.provider import ProviderGate, ProviderRefused, provider_failure


class FakeClock:
    def __init__(self, now: float = 1_700_000_000.0):
        self._now = now
        self._lock = threading.Lock()
        self._local = threading.local()

    def __call__(self) -> float:
        with self._lock:
            self._local.last_read = self._now
            return self._now

    def advance(self, seconds: float) -> None:
        with self._lock:
            self._now += seconds

    def sleep(self, seconds: float) -> None:
        assert seconds >= 0
        # The gate also uses 50 ms sleeps to retry the file lock. Those are
        # scheduling yields, not elapsed fake provider time.
        if seconds == 0.05:
            return
        with self._lock:
            start = getattr(self._local, "last_read", self._now)
            self._now = max(self._now, start + seconds)


@pytest.fixture
def deterministic_backoff(monkeypatch):
    monkeypatch.setattr("random.uniform", lambda *_args: 0.0)


@pytest.mark.parametrize(
    ("retry_after", "expected"),
    [("12", 12.0), (format_datetime(datetime.fromtimestamp(1_700_000_025, timezone.utc), usegmt=True), 25.0)],
)
def test_retry_after_seconds_and_http_date_are_shared_as_absolute_cooldown(
    tmp_path, deterministic_backoff, retry_after, expected
):
    clock = FakeClock()
    gate = ProviderGate(tmp_path / "gate.json", clock=clock, sleep=clock.sleep)

    gate.after_error(retry_after)

    assert gate.retry_delay() == expected


def test_two_gate_instances_share_cooldown_and_admission_state(tmp_path, deterministic_backoff):
    clock = FakeClock()
    path = tmp_path / "gate.json"
    first = ProviderGate(path, clock=clock, sleep=clock.sleep)
    second = ProviderGate(path, clock=clock, sleep=clock.sleep)

    first.after_response(429, "10")
    with pytest.raises(ProviderRefused) as refused:
        second.before_call(clock() + 4)
    assert refused.value.retry_after == 10

    clock.advance(10)
    second.before_call(clock() + 1)
    assert first.retry_delay() == 5.0


def test_retry_after_longer_than_budget_is_bounded_to_sixty_second_sleep(
    tmp_path, deterministic_backoff
):
    clock = FakeClock()
    slept: list[float] = []

    def sleep(seconds):
        slept.append(seconds)
        clock.advance(seconds)

    gate = ProviderGate(tmp_path / "gate.json", clock=clock, sleep=sleep)
    gate.after_error("120")
    assert gate.retry_delay() == 60

    gate.before_call(clock() + 180)

    assert slept == [60, 60]
    assert clock() == 1_700_000_120


@pytest.mark.parametrize(
    ("message", "expected"),
    [
        ("RunPod pod listing failed: HTTP 429: limited", 429),
        ("RunPod pod termination failed: HTTP 503: unavailable", 503),
        ("provider gave an unclassified error", None),
    ],
)
def test_lifecycle_status_classifier_uses_only_known_operation_prefixes(message, expected):
    status, retry_after = provider_failure(RuntimeError(message))
    assert status == expected
    assert retry_after is None


def test_concurrent_429_callers_keep_gaps_and_respect_one_deadline_without_real_waits(
    tmp_path, deterministic_backoff
):
    clock = FakeClock()
    path = tmp_path / "gate.json"
    deadline = clock() + 40
    start = threading.Barrier(4)
    first_round = threading.Barrier(3)
    lock = threading.Lock()
    calls: list[tuple[int, float]] = []
    errors: list[BaseException] = []

    class Response:
        status_code = 429
        headers = {"Retry-After": "0.2"}

    def always_rate_limited(caller_id):
        with lock:
            calls.append((caller_id, clock()))
            is_first_call = sum(1 for item_id, _ in calls if item_id == caller_id) == 1
        if is_first_call and caller_id < 3:
            first_round.wait(timeout=1)
        return Response()

    def caller(caller_id):
        gate = ProviderGate(path, clock=clock, sleep=clock.sleep, minimum_interval=0)
        start.wait()
        try:
            gate.call(always_rate_limited, caller_id, deadline=deadline)
        except BaseException as exc:  # noqa: BLE001 - assert every worker failure in the main thread.
            with lock:
                errors.append(exc)

    workers = [threading.Thread(target=caller, args=(caller_id,)) for caller_id in range(3)]
    for worker in workers:
        worker.start()
    start.wait()
    for worker in workers:
        worker.join(timeout=2)

    assert all(not worker.is_alive() for worker in workers)
    assert errors == []
    assert sorted(item_id for item_id, _ in calls) == [0, 1, 2]
    first_burst_end = max(at for _, at in calls)

    # A follow-up process shares the aggregate Retry-After/backoff state from
    # the concurrent 429s and cannot issue its request until the cooldown.
    follow_up = ProviderGate(path, clock=clock, sleep=clock.sleep, minimum_interval=0)
    follow_up.call(always_rate_limited, 3, deadline=deadline)

    assert calls[-1][0] == 3
    assert calls[-1][1] - first_burst_end >= 20.0
    assert all(at <= deadline for _, at in calls)
    with pytest.raises(ProviderRefused):
        follow_up.before_call(deadline)
    assert len(calls) == 4
    assert clock() <= deadline


def test_transport_errors_grow_with_jitter_and_success_resets_exponent(tmp_path, monkeypatch):
    clock = FakeClock()
    monkeypatch.setattr("random.uniform", lambda *_: 0.25)
    gate = ProviderGate(tmp_path / "gate.json", clock=clock, sleep=clock.sleep)

    def unavailable():
        raise OSError("connection failed")

    for expected in (5.25, 10.25, 20.25, 40.25, 60.25, 60.25):
        with pytest.raises(OSError):
            gate.call(unavailable)
        assert gate.retry_delay() == min(60, expected)
        clock.advance(expected)
    gate.call(lambda: None)
    gate.after_error()
    assert gate.retry_delay() == 5.25


def test_rotated_credentials_share_stable_account_cooldown(tmp_path, monkeypatch):
    from astrid.packs.runpod.provider import account_gate

    monkeypatch.setenv("ASTRID_RUNTIME_DATA_ROOT", str(tmp_path))
    first = account_gate("first-key", account_ref="account-one")
    first.after_response(429, "120")
    rotated = account_gate("rotated-key", account_ref="account-one")
    assert first.path == rotated.path
    assert account_gate("rotated-key").path == first.path
    with pytest.raises(ProviderRefused) as refused:
        rotated.before_call(rotated.clock() + 30)
    assert refused.value.retry_after >= 119
    assert account_gate("other-key", account_ref="account-two").path != first.path


def test_binding_preserves_cooldown_observed_before_account_identity(tmp_path, monkeypatch):
    from astrid.packs.runpod.provider import account_gate

    monkeypatch.setenv("ASTRID_RUNTIME_DATA_ROOT", str(tmp_path))
    unbound = account_gate("key")
    unbound.after_response(429, "120")
    bound = account_gate("key", account_ref="account-one")
    assert bound.path != unbound.path
    with pytest.raises(ProviderRefused) as refused:
        bound.before_call(bound.clock() + 30)
    assert refused.value.retry_after >= 119
