"""Fake-clock retained custody, with Runtime release dispatch injected."""
import asyncio
import copy
import json
import socket
from datetime import datetime, timezone
from types import SimpleNamespace

import pytest

from astrid.core.execution.warm_hold import WarmHolds
from astrid.packs.runpod import worker_owner, worker_preparation
from astrid.packs.runpod.worker_owner import RunPodWarmSessionOwner, RunPodWorkerOwnerError, runpod_idle_seconds
from tests.packs.runpod.test_worker_release import release_case as release_case


@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    monkeypatch.delenv("RUNPOD_API_KEY", raising=False)
    monkeypatch.setattr(socket.socket, "connect", lambda *args: pytest.fail("network forbidden"))


class Clock:
    now = 1800000000.0

    def __init__(self):
        self.timers = []

    def __call__(self):
        return self.now

    def timer(self, seconds, callback):
        timer = SimpleNamespace(due=self.now + seconds, callback=callback, cancelled=False)
        timer.cancel = lambda: setattr(timer, "cancelled", True)
        self.timers.append(timer)
        return timer

    def advance(self, seconds):
        self.now += seconds
        for timer in list(self.timers):
            if not timer.cancelled and timer.due <= self.now:
                timer.cancel()
                timer.callback()


def utc(seconds):
    return datetime.fromtimestamp(seconds, timezone.utc).isoformat()


@pytest.fixture
def warm_case(release_case, monkeypatch):
    case = release_case
    clock = Clock()
    record = copy.deepcopy(case.record)
    record.update(request_name="astrid-product-operation-1", name="astrid-product-operation-1",
                  target_identity_verified=True, reconciliation_required=False, volume_mount_path="/workspace")
    record["paid_limits"] = {
        "invocation_id": "run-consent-1", "hourly_usd": 1, "max_usd": 1,
        "authorized_at": utc(clock()), "deadline": utc(clock() + 1000),
        "expires_at": utc(clock() + 1000), "idle_seconds": 600,
    }
    worker_preparation.write_target_record(case.path, record)
    case.pods[0]["name"] = record["name"]
    requests = []
    preparer = case.fresh()

    def dispatch(request):
        requests.append(dict(request))
        # Represents F07's Runtime release task executing existing pack release.
        assert json.loads(case.path.read_text())["state"] == "cleanup_pending"
        return worker_owner._run_sync(preparer.release(case.reference, runtime_owner=case.runtime, resume=True))

    def fresh(**kwargs):
        return RunPodWarmSessionOwner(preparer, release_task=kwargs.pop("release_task", dispatch),
                                     clock=clock, timer=clock.timer, environ={}, **kwargs)

    return SimpleNamespace(case=case, clock=clock, requests=requests, fresh=fresh, preparer=preparer,
                           record=record, read=lambda: json.loads(case.path.read_text()))


@pytest.mark.parametrize("requested,env,service,expected", [
    (None, {}, {}, 600), (0, {"ASTRID_WARM_IDLE_SECONDS": "5"}, {}, 0),
    (7, {"ASTRID_WARM_IDLE_SECONDS": "5"}, {"ASTRID_WARM_IDLE_SECONDS": "3"}, 7),
    (None, {"ASTRID_WARM_IDLE_SECONDS": "5"}, {"ASTRID_WARM_IDLE_SECONDS": "3"}, 5),
    (None, {}, {"ASTRID_WARM_IDLE_SECONDS": "3"}, 3),
    (None, {"ASTRID_WARM_IDLE_SECONDS": "nan"}, {"ASTRID_WARM_IDLE_SECONDS": "3"}, 3),
    (None, {"ASTRID_WARM_IDLE_SECONDS": "-1"}, {}, 600),
])
def test_idle_precedence_and_zero(requested, env, service, expected):
    assert runpod_idle_seconds(requested=requested, environ=env, service_environ=service) == expected


@pytest.mark.parametrize("value", [-1, float("nan"), float("inf")])
def test_bad_explicit_idle_is_refused(value):
    with pytest.raises(RunPodWorkerOwnerError):
        runpod_idle_seconds(requested=value)


def test_active_hold_cannot_suppress_paid_deadline_and_exact_release(warm_case):
    c = warm_case
    owner = c.fresh()
    owner.begin(c.case.reference)
    assert not c.requests
    c.clock.advance(1000)
    assert len(c.requests) == 1
    assert c.requests[0]["task_id"] == c.case.reference.task_id
    assert c.requests[0]["idempotency_key"] == "runpod-release:operation-1"
    assert c.read()["state"] == "cleanup_complete"
    assert c.read()["cleanup"]["absence"]["exact_get_status"] == 404
    assert c.case.calls == ["begin", "finish", "retire", "stop", "remove_credential", "delete"]


def fake_retirement(c, monkeypatch):
    async def retire(reference, *, runtime_owner, qualification):
        assert reference is c.case.reference and qualification == c.case.qualification
        c.case.calls.append("task-retired")
        return {"state": "drained"}
    monkeypatch.setattr(c.preparer, "retire_task", retire)


def retire(owner, c):
    return asyncio.run(owner.retire(c.case.reference, runtime_owner=c.case.runtime, qualification=c.case.qualification))


def test_idle_expiry_uses_warmholds_and_preserves_volume(warm_case, monkeypatch):
    c = warm_case
    fake_retirement(c, monkeypatch)
    owner = c.fresh(requested_idle_seconds=10)
    owner.begin(c.case.reference)
    assert retire(owner, c) == {"state": "drained"}
    assert owner.holds.status()[0]["leases"] == 0
    c.clock.advance(9)
    assert not c.requests
    c.clock.advance(1)
    assert len(c.requests) == 1 and c.requests[0]["reason"] == "idle"
    assert c.read()["cleanup"]["backup_volume_preserved"] is True


def test_zero_idle_and_duplicate_retirement_never_extend(warm_case, monkeypatch):
    c = warm_case
    fake_retirement(c, monkeypatch)
    owner = c.fresh(requested_idle_seconds=0)
    owner.begin(c.case.reference)
    retire(owner, c)
    first = c.read()["warm_session"]["idle_expires_at"]
    retire(owner, c)
    assert c.read()["warm_session"]["idle_expires_at"] == first
    c.clock.advance(0)
    assert len(c.requests) == 1


def test_reuse_cancels_stale_idle_only_after_fresh_consent(warm_case, monkeypatch):
    c = warm_case
    fake_retirement(c, monkeypatch)
    owner = c.fresh(requested_idle_seconds=10)
    owner.begin(c.case.reference)
    retire(owner, c)
    stale = next(t.callback for t in c.clock.timers if not t.cancelled and t.due == c.clock() + 10)
    with pytest.raises(RunPodWorkerOwnerError, match="fresh per-run"):
        owner.begin(c.case.reference)
    assert c.read()["warm_session"]["idle_expires_at"] == c.clock() + 10
    record = c.read()
    record["paid_limits"]["invocation_id"] = "run-consent-2"
    worker_preparation.write_target_record(c.case.path, record)
    owner.begin(c.case.reference)
    c.clock.advance(10)
    stale()
    assert not c.requests
    retire(owner, c)
    c.clock.advance(10)
    assert len(c.requests) == 1


@pytest.mark.parametrize("phase", ["active", "idle"])
def test_restart_preserves_saved_expiry_without_renewal(warm_case, monkeypatch, phase):
    c = warm_case
    fake_retirement(c, monkeypatch)
    owner = c.fresh(requested_idle_seconds=10)
    owner.begin(c.case.reference)
    if phase == "idle":
        retire(owner, c)
    before = c.read()["warm_session"]
    c.clock.now += 5
    restored = c.fresh(requested_idle_seconds=600)
    restored.recover(c.case.reference)
    after = c.read()["warm_session"]
    assert after["paid_expires_at"] == before["paid_expires_at"]
    assert after.get("idle_expires_at") == before.get("idle_expires_at")
    c.clock.advance(5 if phase == "idle" else 995)
    assert len(c.requests) == 1
    assert c.read()["state"] == "cleanup_complete"


def test_cap_is_wall_time_from_authorization_including_preparation(warm_case):
    c = warm_case
    record = c.read()
    record["paid_limits"]["max_usd"] = 10 / 3600
    worker_preparation.write_target_record(c.case.path, record)
    c.clock.now += 7  # preparation already used seven of ten seconds
    owner = c.fresh()
    owner.begin(c.case.reference)
    c.clock.advance(3)
    assert len(c.requests) == 1


def test_disclosed_idle_cannot_be_exceeded(warm_case):
    c = warm_case
    record = c.read()
    record["paid_limits"]["idle_seconds"] = 0
    worker_preparation.write_target_record(c.case.path, record)
    with pytest.raises(RunPodWorkerOwnerError, match="disclosed idle"):
        c.fresh().begin(c.case.reference)
    c.fresh(requested_idle_seconds=0).begin(c.case.reference)


def test_release_outage_fences_renewal_and_recovers_same_task(warm_case):
    c = warm_case
    def outage(request):
        raise TimeoutError("secret-transport-detail")
    owner = c.fresh(release_task=outage)
    owner.begin(c.case.reference)
    c.clock.advance(1000)
    assert owner.last_error_type == "TimeoutError"
    assert c.read()["state"] == "cleanup_pending"
    assert "secret-" not in c.case.path.read_text()
    with pytest.raises(RunPodWorkerOwnerError, match="cleanup prevents"):
        c.fresh().begin(c.case.reference)
    restored = c.fresh()
    restored.recover(c.case.reference)
    assert c.read()["state"] == "cleanup_complete"
    assert len(c.requests) == 1


@pytest.mark.parametrize("field", ["provider_account_ref", "pod_id", "gpu_type", "worker_image",
                                   "network_volume_id", "volume_mount_path", "operation_id"])
def test_foreign_session_identity_cannot_reuse_or_release(warm_case, field):
    c = warm_case
    owner = c.fresh()
    owner.begin(c.case.reference)
    record = c.read()
    record[field] = "another-broker"
    worker_preparation.write_target_record(c.case.path, record)
    with pytest.raises(RunPodWorkerOwnerError):
        owner.recover(c.case.reference)
    c.clock.advance(1000)
    assert not c.requests and "delete" not in c.case.calls


def test_runtime_task_admission_is_not_absence_proof(warm_case):
    c = warm_case
    requests = []
    def admitted(request):
        requests.append(request)
        return {"task_id": "runtime-release-task", "state": "queued"}
    owner = c.fresh(release_task=admitted)
    owner.begin(c.case.reference)
    c.clock.advance(1000)
    assert c.read()["state"] == "cleanup_pending"
    assert "cleanup" not in c.read()
    c.fresh(release_task=admitted).recover(c.case.reference)
    assert requests[0]["idempotency_key"] == requests[1]["idempotency_key"]
