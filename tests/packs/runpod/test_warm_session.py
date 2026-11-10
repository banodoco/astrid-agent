"""Fake-clock retained custody, with Runtime release dispatch injected."""
import asyncio
import copy
import json
import socket
from dataclasses import replace
from datetime import datetime, timezone
from types import SimpleNamespace

import pytest

from astrid.core.execution.warm_hold import WarmHolds
from astrid.packs.runpod import target_selection, worker_owner, worker_preparation
from astrid.packs.runpod.worker_owner import RunPodLeaseOwner, RunPodWarmSessionOwner, RunPodWorkerOwnerError, runpod_idle_seconds
from astrid.packs.runpod.worker_preparation import AcquisitionUnresolvedError
from tests.packs.runpod.test_target_selection import case as lease_selection_case
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
        while True:
            due = [timer for timer in self.timers if not timer.cancelled and timer.due <= self.now]
            if not due:
                break
            for timer in due:
                if not timer.cancelled:
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
        "expires_at": utc(clock() + 1000), "idle_seconds": 1800,
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
    (None, {}, {}, 1800), (0, {"ASTRID_WARM_IDLE_SECONDS": "5"}, {}, 0),
    (7, {"ASTRID_WARM_IDLE_SECONDS": "5"}, {"ASTRID_WARM_IDLE_SECONDS": "3"}, 7),
    (None, {"ASTRID_WARM_IDLE_SECONDS": "5"}, {"ASTRID_WARM_IDLE_SECONDS": "3"}, 5),
    (None, {}, {"ASTRID_WARM_IDLE_SECONDS": "3"}, 3),
    (None, {"ASTRID_WARM_IDLE_SECONDS": "nan"}, {"ASTRID_WARM_IDLE_SECONDS": "3"}, 3),
    (None, {"ASTRID_WARM_IDLE_SECONDS": "-1"}, {}, 1800),
])
def test_idle_precedence_and_zero(requested, env, service, expected):
    assert runpod_idle_seconds(requested=requested, environ=env, service_environ=service) == expected


@pytest.mark.parametrize("value", [-1, float("nan"), float("inf"), True])
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


def test_overdue_recovery_fences_synchronously_before_return(warm_case):
    c = warm_case
    c.fresh().begin(c.case.reference)
    c.clock.now += 1001
    c.fresh().recover(c.case.reference)
    assert c.read()["state"] == "cleanup_complete"
    assert len(c.requests) == 1


def test_recovery_respects_shorter_paid_bound_and_invalid_limits(warm_case):
    c = warm_case
    c.fresh().begin(c.case.reference)
    record = c.read()
    record["paid_limits"]["deadline"] = utc(c.clock() + 5)
    worker_preparation.write_target_record(c.case.path, record)
    c.fresh().recover(c.case.reference)
    c.clock.advance(5)
    assert c.read()["state"] == "cleanup_complete"


def test_invalid_paid_limits_on_restart_request_exact_cleanup(warm_case):
    c = warm_case
    c.fresh().begin(c.case.reference)
    record = c.read()
    record["paid_limits"]["max_usd"] = "unknown"
    worker_preparation.write_target_record(c.case.path, record)
    c.fresh().recover(c.case.reference)
    assert c.read()["state"] == "cleanup_complete" and len(c.requests) == 1


def test_expiry_retries_after_task_retirement_lock_contention(warm_case):
    c = warm_case
    owner = c.fresh()
    owner.begin(c.case.reference)
    with worker_preparation.target_owner(c.case.path):
        c.clock.advance(1000)
        assert not c.requests
    c.clock.advance(1)
    assert len(c.requests) == 1 and c.read()["state"] == "cleanup_complete"


def test_deadline_fences_but_cannot_invent_runtime_drain_for_active_task(warm_case):
    c = warm_case
    c.case.runtime.result = {"state": "pending", "activation_id": c.case.qualification["activation_id"]}
    owner = c.fresh()
    owner.begin(c.case.reference)
    c.clock.advance(1000)
    assert owner.last_error_type == "RunPodWorkerOwnerError"
    assert c.read()["state"] in {"release_pending", "cleanup_pending"}
    assert "delete" not in c.case.calls and "stop" not in c.case.calls
    with pytest.raises(RunPodWorkerOwnerError, match="cleanup prevents"):
        c.fresh().begin(c.case.reference)


def test_expired_idle_cannot_be_renewed_by_new_consent_after_restart(warm_case, monkeypatch):
    c = warm_case
    fake_retirement(c, monkeypatch)
    owner = c.fresh(requested_idle_seconds=10)
    owner.begin(c.case.reference)
    retire(owner, c)
    c.clock.now += 11
    record = c.read()
    record["paid_limits"]["invocation_id"] = "another-consent"
    worker_preparation.write_target_record(c.case.path, record)
    with pytest.raises(RunPodWorkerOwnerError, match="expired"):
        c.fresh().begin(c.case.reference)
    c.fresh().recover(c.case.reference)
    assert len(c.requests) == 1


def test_real_task_retirement_resumes_credential_cleanup_then_idles(warm_case, monkeypatch):
    c = warm_case
    preparer = c.preparer
    # Use the production P1 lock for task retirement, as well as for release.
    monkeypatch.setattr(preparer, "_claim_owner",
                        worker_owner.RunPodRemoteWorkerPreparer._claim_owner.__get__(preparer))

    def finish(task_id, actual):
        assert task_id == c.case.reference.task_id and actual == c.case.qualification
        assert c.read()["managed_worker"]["task_retirement"]["drain_started"] is True
        c.case.runtime.finished = True
        c.case.calls.append("task-finish")
        return {"state": "drained", "activation_id": actual["activation_id"]}

    async def stop(process):
        assert process == c.case.process
        assert c.read()["managed_worker"]["task_retirement"]["runtime_drain"]["state"] == "drained"
        c.case.owner.stopped = True
        c.case.calls.append("task-stop")
        return {"stopped": True, "pid": process.pid, "birth_id": process.birth_id, "incarnation": process.incarnation}

    remove = c.case.owner.remove_credential
    attempts = []

    async def lost_remove(process, identity):
        attempts.append(identity)
        if len(attempts) == 1:
            raise TimeoutError("credential cleanup reply lost")
        return await remove(process, identity)

    monkeypatch.setattr(c.case.runtime, "finish_remote_drain", finish)
    monkeypatch.setattr(c.case.owner, "stop", stop)
    monkeypatch.setattr(c.case.owner, "remove_credential", lost_remove)
    owner = c.fresh(requested_idle_seconds=10)
    owner.begin(c.case.reference)
    with pytest.raises(TimeoutError):
        retire(owner, c)
    assert c.read()["warm_session"]["state"] == "active"
    retire(owner, c)
    assert c.read()["managed_worker"]["state"] == "stopped"
    assert c.read()["warm_session"]["state"] == "idle"
    c.clock.advance(10)
    assert c.read()["state"] == "cleanup_complete"
    assert c.case.calls == ["begin", "task-finish", "task-stop", "remove_credential", "retire", "delete"]


@pytest.mark.parametrize("state,offset", [("idle", 1000), ("releasing", 1000), ("active", -1)])
def test_park_and_restore_cannot_bypass_retained_expiry(warm_case, monkeypatch, state, offset):
    c = warm_case
    owner = c.fresh()
    owner.begin(c.case.reference)
    monkeypatch.setattr(worker_owner, "time", SimpleNamespace(time=c.clock))
    record = c.read()
    record["warm_session"].update(state=state, paid_expires_at=c.clock() + offset)
    with pytest.raises(RunPodWorkerOwnerError, match="active unexpired exact-task"):
        c.preparer._prepare_locked(SimpleNamespace(reference=c.case.reference), record, -1)
    with pytest.raises(RunPodWorkerOwnerError, match="active unexpired exact-task"):
        c.preparer._restore_locked(c.case.reference, record)


def test_next_run_can_request_zero_idle_on_the_retained_owner(warm_case, monkeypatch):
    c = warm_case
    fake_retirement(c, monkeypatch)
    owner = c.fresh(requested_idle_seconds=10)
    owner.begin(c.case.reference)
    retire(owner, c)
    record = c.read()
    record["paid_limits"]["invocation_id"] = "run-consent-2"
    worker_preparation.write_target_record(c.case.path, record)
    owner.begin(c.case.reference, requested_idle_seconds=0)
    retire(owner, c)
    assert c.read()["warm_session"]["idle_seconds"] == 0
    c.clock.advance(0)
    assert len(c.requests) == 1 and c.read()["state"] == "cleanup_complete"


# Small lease journey: real selection, journal, lock, WarmHolds and exact-release
# owner; fake only provider observations and the host's invocation-drain seam.
@pytest.fixture
def small_lease(lease_selection_case, monkeypatch):
    from runpod_lifecycle import api
    c = lease_selection_case
    clock = Clock()
    clock.now = c.now.timestamp()
    c.owner.compatibility = target_selection.RunPodLeaseCompatibility(
        "sha256:" + "a" * 64, "sha256:" + "b" * 64,
        "1a275bb339ae897e52a5be5e1da99e846e402037", "b2123edfbe7b950848b5c98d575fd9554302fc73",
    ).as_mapping()
    c.drains, c.deletes, c.owners = [], [], []
    c.drain_error = c.delete_error = None
    c.absent_get = True
    c.read = lambda: json.loads(c.owner.path.read_text())
    c.grant = replace(c.grant, idle_seconds=1800)
    c.owner.clock = lambda: datetime.fromtimestamp(clock(), timezone.utc)

    def delete(pod_id, key):
        assert c.read()["release_quiescence"]["state"] == "drained"
        c.deletes.append(pod_id)
        c.pods[:] = [pod for pod in c.pods if pod["id"] != pod_id]
        if c.delete_error:
            raise c.delete_error

    def exact(key, pod_id):
        pod = next((pod for pod in c.pods if pod["id"] == pod_id), None)
        if pod is None:
            return (404, None) if c.absent_get else (200, {"id": pod_id})
        return 200, {**pod, "volumeMountPath": c.mounts}

    async def drain(record):
        c.drains.append(record["lease"]["invocation_id"])
        assert record["state"] == "release_pending" and record["lease"]["state"] == "releasing"
        if c.drain_error:
            raise c.drain_error
        return {"schema": "astrid.runpod.lease-drain.v1", "state": "drained", "admission_closed": True,
                **{key: record[key] for key in ("operation_id", "pod_id", "provider_account_ref")},
                **{key: record["lease"][key] for key in ("owner_epoch", "generation", "invocation_id")}}

    monkeypatch.setattr(api, "terminate_pod", delete)
    monkeypatch.setattr(api, "reconcile_pod_cleanup", lambda *args, **kwargs: None)
    monkeypatch.setattr(target_selection, "read_exact_pod", exact)
    monkeypatch.setattr(worker_preparation, "read_exact_pod", exact)

    def fresh():
        owner = RunPodLeaseOwner(c.owner, holds=WarmHolds(clock=clock, timer=clock.timer),
                                quiesce=drain, clock=clock, timer=clock.timer, environ={})
        c.owners.append(owner)
        return owner

    c.fresh, c.clock = fresh, clock
    yield c
    for owner in c.owners:
        owner.close()


def test_small_lease_intent_compatibility_lock_idle_and_exact_release(small_lease):
    c = small_lease
    owner = c.fresh()
    def before_create():
        record = c.read()
        assert record["lease_compatibility"] == c.owner.compatibility
        assert record["lease"]["state"] == "preparing"
        assert record["paid_limits"]["invocation_id"] == record["lease"]["invocation_id"]
    c.before_create = before_create
    first = owner.acquire(c.grant)
    with pytest.raises(AcquisitionUnresolvedError):
        c.fresh()  # account lock stays owned between invocations
    owner.settle(first)
    assert c.read()["lease"]["idle_expires_at"] == c.clock() + 1800
    c.clock.advance(1799)
    owner.settle(first)  # duplicate cannot extend idle
    assert not c.deletes
    c.clock.advance(1)
    assert c.deletes == ["pod-1"]
    assert c.read()["cleanup"]["absence"]["exact_get_status"] == 404
    assert c.read()["cleanup"]["backup_volume_preserved"] is True
    assert c.read()["estimated_cost_usd"] == pytest.approx(0.5)
    # A verified terminal receipt permits a fresh identity in the same journal.
    second = owner.acquire(replace(c.grant, invocation_id="next", deadline=utc(c.clock() + 3600)))
    assert second.operation_id != first.operation_id and c.creates == 2


def test_small_lease_reuses_snapshot_and_stale_holder_cannot_expire_next_run(small_lease):
    c = small_lease
    owner = c.fresh()
    first = owner.acquire(c.grant, requested_idle_seconds=10)
    owner.settle(first)
    stale = next(timer.callback for timer in c.clock.timers if timer.due == c.clock() + 10)
    second = owner.acquire(replace(c.grant, invocation_id="next"), requested_idle_seconds=0)
    stale()
    with pytest.raises(RunPodWorkerOwnerError, match="another holder"):
        owner.settle(first)
    assert c.creates == 1 and not c.deletes
    owner.settle(second)
    c.clock.advance(0)
    assert c.deletes == ["pod-1"]


def test_small_lease_paid_bound_fences_active_invocation(small_lease):
    c = small_lease
    owner = c.fresh()
    grant = replace(c.grant, max_usd=5 / 3600, preparation_seconds=1,
                    execution_seconds=1, idle_seconds=0)
    owner.acquire(grant, requested_idle_seconds=0)
    c.drain_error = RuntimeError("active Runtime invocation cannot drain")
    c.clock.advance(5)
    assert c.read()["state"] == "release_pending" and not c.deletes
    with pytest.raises(RuntimeError):
        owner.acquire(replace(grant, invocation_id="blocked"), requested_idle_seconds=0)
    assert c.creates == 1
    c.drain_error = None
    c.clock.advance(1)
    assert c.deletes == ["pod-1"]


def test_small_lease_restart_terminates_even_unexpired_custody(small_lease):
    c = small_lease
    old = c.fresh()
    old.acquire(c.grant)
    old.close()  # owner death differs from a short-lived client's exit
    restored = c.fresh()
    restored.recover()
    assert c.deletes == ["pod-1"] and c.read()["state"] == "cleanup_complete"


def keep_grant(c, **changes):
    values = {"invocation_id": "keep-1", "deadline": utc(c.clock() + 7200), "max_usd": 2,
              "preparation_seconds": 0, "execution_seconds": 0, "idle_seconds": 60, **changes}
    return replace(c.grant, **values)


def test_small_lease_keep_extends_paid_limit_accrues_cost_and_rearms(small_lease):
    c = small_lease
    owner = c.fresh()
    invocation = owner.acquire(c.grant, requested_idle_seconds=10)
    owner.settle(invocation)
    previous_expiry = c.read()["lease"]["paid_expires_at"]
    old_timers = list(c.clock.timers)
    c.clock.advance(5)
    kept = owner.keep("pod-1", keep_grant(c))
    assert kept["lease"]["paid_expires_at"] == c.clock() + 7200 > previous_expiry
    assert kept["lease"]["idle_expires_at"] == c.clock() + 60
    assert kept["lease"]["estimated_cost_usd"] == pytest.approx(5 / 3600)
    assert kept["lease"]["accounted_at"] == c.clock()
    assert kept["lease"]["invocation_id"] == invocation.invocation_id
    assert kept["paid_limits"]["invocation_id"] == "keep-1"
    assert all(timer.cancelled for timer in old_timers)
    assert any(not timer.cancelled and timer.due == c.clock() + 7200 for timer in c.clock.timers)
    c.clock.advance(5)  # The cancelled idle timer must not release the extended lease.
    assert not c.deletes and c.creates == 1


@pytest.mark.parametrize("state", ["active", "idle"])
def test_small_lease_keep_until_release_disables_idle_but_releases_at_cap(small_lease, state):
    c = small_lease
    owner = c.fresh()
    invocation = owner.acquire(c.grant, requested_idle_seconds=2)
    if state == "idle":
        owner.settle(invocation)
    grant = keep_grant(c, max_usd=20 / 3600, deadline=utc(c.clock() + 100), idle_seconds=20)
    owner.keep("pod-1", grant, until_release=True)
    if state == "active":
        owner.settle(invocation)
    assert c.read()["lease"]["idle_expires_at"] == c.read()["lease"]["paid_expires_at"] == c.clock() + 20
    c.clock.advance(2)
    assert not c.deletes
    c.clock.advance(18)
    assert c.deletes == ["pod-1"] and c.read()["state"] == "cleanup_complete"


def test_small_lease_keep_until_release_survives_a_reused_job(small_lease):
    c = small_lease
    owner = c.fresh()
    first = owner.acquire(c.grant, requested_idle_seconds=2)
    owner.settle(first)
    owner.keep("pod-1", keep_grant(c, max_usd=20 / 3600,
                                 deadline=utc(c.clock() + 100), idle_seconds=20), True)
    second = owner.acquire(replace(c.grant, invocation_id="reused", preparation_seconds=1,
                                    execution_seconds=1, idle_seconds=2), requested_idle_seconds=2)
    owner.settle(second)
    assert c.read()["lease"]["idle_expires_at"] == c.clock() + 20
    c.clock.advance(2)
    assert not c.deletes
    c.clock.advance(18)
    assert c.deletes == ["pod-1"]


@pytest.mark.parametrize("reason", ["no-live-owner", "foreign-id", "foreign-journal-pod", "stale-epoch",
                                    "release-inflight", "replayed-consent", "not-claimed", "expired"])
def test_small_lease_keep_refusal_leaves_custody_and_timers_unchanged(small_lease, reason):
    c = small_lease
    owner = c.fresh()
    owner.acquire(c.grant)
    machine = "pod-1"
    grant = keep_grant(c)
    record = c.read()
    if reason == "no-live-owner":
        owner.started = False
    elif reason == "foreign-id":
        machine = "pod-foreign"
    elif reason == "foreign-journal-pod":
        record["pod_id"] = machine = "pod-foreign"
    elif reason == "stale-epoch":
        record["lease"]["owner_epoch"] = "another-owner"
    elif reason == "release-inflight":
        owner._release_inflight = True
    elif reason == "replayed-consent":
        grant = replace(grant, invocation_id=c.grant.invocation_id)
    elif reason == "not-claimed":
        record["state"] = "cleanup_pending"
    elif reason == "expired":
        c.clock.now = record["lease"]["paid_expires_at"]
    worker_preparation.write_target_record(c.owner.path, record)
    before = c.owner.path.read_bytes()
    timers = [(timer, timer.cancelled) for timer in c.clock.timers]
    try:
        with pytest.raises((RunPodWorkerOwnerError, AcquisitionUnresolvedError, target_selection.PaidSelectionError)):
            owner.keep(machine, grant)
        assert c.owner.path.read_bytes() == before
        assert len(c.clock.timers) == len(timers)
        assert all(timer.cancelled == cancelled for timer, cancelled in timers)
        assert not c.deletes and c.creates == 1
    finally:
        owner._release_inflight = False


def test_small_lease_keep_rejects_older_consent_after_two_keeps(small_lease):
    c = small_lease
    owner = c.fresh()
    owner.acquire(c.grant)
    first = keep_grant(c)
    owner.keep("pod-1", first)
    owner.keep("pod-1", replace(first, invocation_id="keep-2"))
    before = c.owner.path.read_bytes()
    with pytest.raises(target_selection.PaidSelectionError, match="already used"):
        owner.keep("pod-1", first)
    assert c.owner.path.read_bytes() == before


@pytest.mark.parametrize("change", [{"consent": ()}, {"offer_digest": "changed"},
                                   {"max_usd": 0.001}, {"deadline": "2026-11-01T00:00:01Z"}])
def test_small_lease_keep_validates_paid_consent_before_mutating(small_lease, change):
    c = small_lease
    owner = c.fresh()
    owner.acquire(c.grant)
    before = c.owner.path.read_bytes()
    with pytest.raises(target_selection.PaidSelectionError):
        owner.keep("pod-1", keep_grant(c, **change))
    assert c.owner.path.read_bytes() == before and not c.deletes
    assert c.creates == 1


def test_small_lease_paid_clock_and_cost_include_delayed_post(small_lease):
    c = small_lease
    owner = c.fresh()
    initial = c.clock()
    c.before_create = lambda: setattr(c.clock, "now", c.clock() + 30)
    lease = owner.acquire(c.grant)
    assert lease.claim["lease"]["started_at"] == initial
    assert lease.claim["lease"]["paid_expires_at"] == initial + 3600
    assert lease.claim["lease"]["estimated_cost_usd"] == pytest.approx(30 / 3600)
    assert lease.claim["lease"]["accounted_at"] == initial + 30


def test_deadline_passing_during_post_keeps_paid_clock_and_releases_exact_pod(small_lease):
    c = small_lease
    owner = c.fresh()
    initial = c.clock()
    grant = replace(c.grant, preparation_seconds=0, execution_seconds=0, idle_seconds=1,
                    deadline=utc(initial + 10))
    c.before_create = lambda: setattr(c.clock, "now", initial + 11)
    with pytest.raises(target_selection.PaidSelectionError):
        owner.acquire(grant, requested_idle_seconds=0)
    assert c.read()["paid_limits"]["authorized_at"] == utc(initial)
    assert target_selection._utc(c.read()["paid_limits"]["expires_at"]).timestamp() == initial + 10
    c.clock.advance(1)
    assert c.deletes == ["pod-1"] and c.creates == 1 and c.read()["state"] == "cleanup_complete"


def test_cancellation_during_post_reconciles_and_releases_without_second_create(small_lease, monkeypatch):
    import asyncio
    import runpod_lifecycle
    c = small_lease
    owner = c.fresh()
    cancelled = [False]

    async def launch(config, *, name, **kwargs):
        c.creates += 1
        c.pods.append({"id": "pod-1", "name": name, "desiredStatus": "RUNNING", "actualStatus": "RUNNING",
                       "machineType": c.offer.gpu_type, "imageName": c.prefs.image,
                       "networkVolumeId": c.prefs.volume_id})
        cancelled[0] = True
        await asyncio.sleep(100)

    monkeypatch.setattr(runpod_lifecycle, "launch_when_available", launch)
    with pytest.raises(target_selection.CapacityWaitCancelled):
        owner.acquire(c.grant, cancelled=lambda: cancelled[0])
    assert c.read()["state"] == "allocation_unknown"
    c.clock.advance(1)
    assert c.creates == 1 and c.deletes == ["pod-1"] and c.read()["state"] == "cleanup_complete"


def test_concurrent_release_cancels_a_capacity_wait_while_owner_lock_is_held(small_lease):
    import asyncio
    import threading
    from tests.packs.runpod.test_target_selection import waiting_grant

    c = small_lease
    owner = c.fresh()
    waiting = threading.Event()
    errors = []
    c.owner.observe_offer = lambda: (_ for _ in ()).throw(target_selection.OfferObservationError("stock_unavailable"))

    async def sleep(seconds):
        waiting.set()
        await asyncio.sleep(100)

    c.owner.sleep = sleep

    def acquire():
        try:
            owner.acquire(waiting_grant(c))
        except BaseException as exc:
            errors.append(exc)

    thread = threading.Thread(target=acquire)
    thread.start()
    assert waiting.wait(3)
    assert owner.release() is None
    thread.join(3)
    assert not thread.is_alive() and isinstance(errors[0], target_selection.CapacityWaitCancelled)
    assert c.creates == 0 and not c.owner.path.exists()


def stale_capacity_record(c, owner, *, ambiguous=False):
    owner.acquire(c.grant)
    record = c.read()
    record.pop("pod_id")
    record.pop("name", None)
    record.pop("provider_observation", None)
    record["state"] = "allocation_unknown" if ambiguous else "capacity_retry_ready"
    record["safe_to_retry"] = not ambiguous
    record["capacity_rejection"] = None if ambiguous else "ProviderCapacityUnavailable"
    record["lease"]["state"] = "preparing"
    worker_preparation.write_target_record(c.owner.path, record)
    owner.close()
    return record


def test_restart_discards_only_confirmed_zero_pod_typed_capacity_intent(small_lease, capsys):
    c = small_lease
    stale_capacity_record(c, c.fresh())
    c.pods.clear()
    restarted = c.fresh()
    restarted.recover()
    assert not c.owner.path.exists() and restarted.started
    assert "recovered_stale_capacity_intent" in capsys.readouterr().err
    restarted.acquire(replace(c.grant, invocation_id="after-restart"))
    assert c.creates == 2


def test_restart_capacity_intent_with_matching_pod_reconciles_and_releases(small_lease):
    c = small_lease
    stale_capacity_record(c, c.fresh())
    c.fresh().recover()
    assert c.deletes == ["pod-1"] and c.read()["state"] == "cleanup_complete"


def test_restart_never_discards_an_ambiguous_create_intent(small_lease):
    c = small_lease
    stale_capacity_record(c, c.fresh(), ambiguous=True)
    c.pods.clear()
    with pytest.raises(AcquisitionUnresolvedError):
        c.fresh().recover()
    assert c.owner.path.exists() and c.read()["state"] == "allocation_unknown"


def test_small_lease_owner_process_death_releases_lock_and_recovers(small_lease):
    import multiprocessing

    c = small_lease
    context = multiprocessing.get_context("fork")
    parent, child = context.Pipe()

    def hold_custody():
        try:
            c.fresh().acquire(c.grant)
            child.send((c.creates, c.pods))
            child.recv()  # The parent kills the owner without close().
        except BaseException as exc:
            child.send(("error", repr(exc)))

    process = context.Process(target=hold_custody)
    process.start()
    child.close()
    try:
        assert parent.poll(10), "owner did not acquire custody"
        creates, pods = parent.recv()
        assert creates == 1, pods
        c.creates, c.pods[:] = creates, pods
        process.kill()
        process.join(10)
        assert process.exitcode is not None and process.exitcode < 0
    finally:
        if process.is_alive():
            process.kill()
            process.join(10)
        parent.close()
    restored = c.fresh()
    restored.recover()
    assert c.creates == 1 and c.deletes == ["pod-1"]
    assert c.read()["state"] == "cleanup_complete"
    assert c.read()["cleanup"]["absence"]["account_list_absent"] is True


def test_small_lease_lost_create_reconciles_without_replacement(small_lease):
    c = small_lease
    owner = c.fresh()
    c.create_error = TimeoutError("fake lost response")
    with pytest.raises(AcquisitionUnresolvedError):
        owner.acquire(c.grant)
    assert c.read()["state"] == "allocation_pending"
    owner.close()
    c.create_error = None
    restored = c.fresh()
    restored.recover()
    assert c.creates == 1 and c.deletes == ["pod-1"]


def test_small_lease_zero_matches_stays_unresolved(small_lease):
    c = small_lease
    owner = c.fresh()
    c.create_error = TimeoutError("fake lost response")
    with pytest.raises(AcquisitionUnresolvedError):
        owner.acquire(c.grant)
    c.pods.clear()
    with pytest.raises(AcquisitionUnresolvedError):
        owner.recover()
    with pytest.raises(AcquisitionUnresolvedError):
        owner.acquire(replace(c.grant, invocation_id="cannot-replace"))
    assert c.creates == 1 and not c.deletes


def test_small_lease_lost_delete_ack_and_failed_get_remain_pending(small_lease):
    c = small_lease
    owner = c.fresh()
    owner.acquire(c.grant)
    c.delete_error = TimeoutError("delete response lost")
    with pytest.raises(TimeoutError):
        owner.release()
    c.delete_error = None
    c.absent_get = False
    with pytest.raises(worker_preparation.TargetObservationError):
        owner.recover()
    assert c.read()["state"] == "cleanup_pending" and c.deletes == ["pod-1"]
    c.absent_get = True
    owner.recover()
    assert c.read()["state"] == "cleanup_complete" and c.deletes == ["pod-1"]
    assert c.read()["cleanup"]["absence"] == {
        "provider_account_ref": "account-a", "pod_id": "pod-1", "exact_get_status": 404,
        "account_list_succeeded": True, "account_list_absent": True,
    }


@pytest.mark.parametrize("field", ["recipe_digest", "model_lock_digest", "runtime_commit", "vibecomfy_commit"])
def test_small_lease_incompatible_snapshot_refused_without_acquisition(small_lease, field):
    c = small_lease
    owner = c.fresh()
    owner.settle(owner.acquire(c.grant))
    c.owner.compatibility = {**c.owner.compatibility, field: "changed"}
    before = c.owner.path.read_bytes()
    with pytest.raises(AcquisitionUnresolvedError, match="incompatible"):
        owner.acquire(replace(c.grant, invocation_id="different"))
    assert c.creates == 1 and c.owner.path.read_bytes() == before


def test_small_lease_cli_exit_leaves_retained_host_timer_running(small_lease):
    import subprocess
    import sys
    c = small_lease
    host_owner = c.fresh()
    # A real short-lived client asks the retained host to acquire and settle
    # over pipes. The client process exits before the host's expiry fires.
    script = ("import sys; print('acquire', flush=True); "
              "assert sys.stdin.readline().strip() == 'pod-1'; "
              "print('settle', flush=True); assert sys.stdin.readline().strip() == 'idle'")
    with subprocess.Popen([sys.executable, "-c", script], stdin=subprocess.PIPE,
                          stdout=subprocess.PIPE, text=True) as client:
        assert client.stdout.readline().strip() == "acquire"
        invocation = host_owner.acquire(c.grant, requested_idle_seconds=1)
        client.stdin.write(invocation.claim["pod_id"] + "\n")
        client.stdin.flush()
        assert client.stdout.readline().strip() == "settle"
        host_owner.settle(invocation)
        client.stdin.write("idle\n")
        client.stdin.flush()
        assert client.wait(timeout=10) == 0
    c.clock.advance(1)
    assert c.deletes == ["pod-1"] and c.read()["state"] == "cleanup_complete"


def test_small_lease_no_volume_uses_same_acquire_and_verified_release(small_lease, monkeypatch):
    import runpod_lifecycle
    from runpod_lifecycle import api
    c = small_lease
    prefs = target_selection.RunPodPreferences.from_mapping({**c.raw, "volume": None})
    assert prefs.as_mapping().get("volume") is None
    c.owner.preferences = prefs
    c.grant = replace(c.grant, offer_digest=c.offer.digest(prefs))
    launch = runpod_lifecycle.launch_when_available

    async def no_volume_launch(*args, **kwargs):
        pod = await launch(*args, **kwargs)
        c.pods[-1]["networkVolumeId"] = None
        return pod

    monkeypatch.setattr(runpod_lifecycle, "launch_when_available", no_volume_launch)
    monkeypatch.setattr(api, "get_network_volumes", lambda *args: pytest.fail("no volume API in ephemeral mode"))
    owner = c.fresh()
    owner.acquire(c.grant)
    result = owner.release()
    assert result["storage_mode"] == "ephemeral"
    assert result["cleanup"]["backup_volume_id"] is None
    assert result["cleanup"]["absence"]["account_list_absent"] is True


def test_small_lease_foreign_drain_cannot_authorize_delete(small_lease):
    c = small_lease
    owner = c.fresh()
    owner.acquire(c.grant)
    real_drain = owner.quiesce

    async def foreign(record):
        return {**await real_drain(record), "invocation_id": "foreign"}

    owner.quiesce = foreign
    with pytest.raises(worker_preparation.TargetObservationError, match="exact host drain"):
        owner.release()
    assert not c.deletes and c.read()["state"] == "release_pending"


def test_small_lease_stale_recovery_callback_cannot_terminate_next_operation(small_lease):
    c = small_lease
    owner = c.fresh()
    owner.acquire(c.grant)
    c.delete_error = TimeoutError("lost delete")
    with pytest.raises(TimeoutError):
        owner.release()
    stale = owner.deadline_timer.callback
    c.delete_error = None
    owner.recover()
    next_lease = owner.acquire(replace(c.grant, invocation_id="next"))
    stale()
    assert c.creates == 2 and c.deletes == ["pod-1"]
    assert c.read()["operation_id"] == next_lease.operation_id
    assert c.read()["lease"]["state"] == "active"


def test_small_lease_idle_never_extends_paid_deadline(small_lease):
    c = small_lease
    owner = c.fresh()
    invocation = owner.acquire(c.grant)
    paid = c.read()["lease"]["paid_expires_at"]
    c.clock.advance(3590)
    owner.settle(invocation)
    assert c.read()["lease"]["idle_expires_at"] == paid
    c.clock.advance(10)
    assert c.deletes == ["pod-1"]


@pytest.mark.parametrize("phase", ["active", "idle"])
def test_small_lease_expiry_retries_transient_custody_read_failure(small_lease, monkeypatch, phase):
    c = small_lease
    owner = c.fresh()
    invocation = owner.acquire(c.grant, requested_idle_seconds=10)
    if phase == "idle":
        owner.settle(invocation)
        remaining = 10
    else:
        remaining = c.read()["lease"]["paid_expires_at"] - c.clock()

    read = c.owner._read
    failed = False

    def transient_read():
        nonlocal failed
        if not failed:
            failed = True
            raise OSError("transient custody read failure")
        return read()

    monkeypatch.setattr(c.owner, "_read", transient_read)
    c.clock.advance(remaining)
    assert failed and not c.deletes
    assert c.read()["state"] == "claimed"
    c.clock.advance(1)
    assert c.deletes == ["pod-1"]
    assert c.read()["state"] == "cleanup_complete"
    assert c.read()["cleanup"]["absence"]["account_list_absent"] is True


def test_small_lease_drain_allows_exact_holder_settlement_without_reopening(small_lease):
    import threading
    c = small_lease
    owner = c.fresh()
    invocation = owner.acquire(c.grant)
    real_drain = owner.quiesce
    progressed, errors = threading.Event(), []

    def finish_holder():
        try:
            owner.settle(invocation)
        except Exception as exc:
            errors.append(exc)
        finally:
            progressed.set()

    async def drain(record):
        thread = threading.Thread(target=finish_holder, daemon=True)
        thread.start()
        assert progressed.wait(5), "the release mutex must not block the holder it is draining"
        thread.join()
        assert not errors
        with pytest.raises(AcquisitionUnresolvedError, match="in progress"):
            owner.acquire(replace(c.grant, invocation_id="racing"))
        assert c.read()["lease"]["state"] == "releasing"
        return await real_drain(record)

    owner.quiesce = drain
    owner.release()
    assert c.deletes == ["pod-1"] and c.read()["state"] == "cleanup_complete"


def manual_claim_grant(c, **changes):
    return replace(c.grant, preparation_seconds=0, execution_seconds=0, idle_seconds=600,
                   **changes)


def test_manual_claim_waits_then_settles_idle_and_reuses_without_extending_paid_limit(small_lease):
    c = small_lease
    owner = c.fresh()
    owner.start_fresh_claim()
    observations, waits = [], []

    def observe():
        observations.append(1)
        if len(observations) < 3:
            raise target_selection.OfferObservationError("stock_unavailable")
        return c.offer

    async def sleep(seconds):
        waits.append(seconds)
        c.clock.now += seconds

    c.owner.observe_offer, c.owner.sleep = observe, sleep
    grant = manual_claim_grant(c, offer_digest=None, deadline=None,
        policy_digest=target_selection.policy_consent_digest(c.prefs, 1), max_hourly_usd=1)
    claimed = owner.claim(grant, requested_idle_seconds=600)
    assert len(observations) == 3 and len(waits) == 2 and c.creates == 1
    assert claimed["lease"]["state"] == "idle"
    assert claimed["lease"]["idle_expires_at"] == c.clock() + 600
    expiry, started = claimed["lease"]["paid_expires_at"], claimed["lease"]["started_at"]
    # A normal generation admission needs fresh consent but no market stock.
    c.owner.observe_offer = lambda: pytest.fail("retained reuse observed market stock")
    later = owner.acquire(replace(c.grant, invocation_id="later-job", idle_seconds=60,
                                  deadline=utc(c.clock() + 7200)), requested_idle_seconds=60)
    assert c.creates == 1 and later.claim["pod_id"] == claimed["pod_id"]
    assert later.claim["lease"]["paid_expires_at"] == expiry
    assert later.claim["lease"]["started_at"] == started
    owner.settle(later)
    # The original claim authority stays used even after later consent replaced it.
    before = c.owner.path.read_bytes()
    with pytest.raises(target_selection.PaidSelectionError, match="already used"):
        owner.acquire(grant, requested_idle_seconds=600)
    with pytest.raises(target_selection.PaidSelectionError, match="already used"):
        owner.keep(claimed["pod_id"], replace(grant, policy_digest=None, max_hourly_usd=None,
                   offer_digest=claimed["paid_limits"]["offer_digest"], deadline=utc(c.clock() + 7200)))
    assert c.owner.path.read_bytes() == before and c.creates == 1


def test_manual_claim_initial_idle_expires_without_generation_or_extra_consent(small_lease):
    c = small_lease
    owner = c.fresh()
    owner.start_fresh_claim()
    record = owner.claim(manual_claim_grant(c), requested_idle_seconds=600)
    assert "preparation" not in record["lease"] and "job" not in record["lease"]
    c.clock.advance(600)
    assert c.deletes == ["pod-1"] and c.read()["state"] == "cleanup_complete"


def test_manual_claim_cold_owner_refuses_prior_custody_without_recovery_or_delete(small_lease):
    c = small_lease
    old = c.fresh()
    old.acquire(c.grant)
    old.close()
    restored = c.fresh()
    before = c.owner.path.read_bytes()
    with pytest.raises(RunPodWorkerOwnerError, match="existing custody pod-1.*astrid machines"):
        restored.start_fresh_claim()
    assert c.owner.path.read_bytes() == before and not c.deletes and c.creates == 1


def test_manual_claim_rejects_pending_create_even_without_pod(small_lease):
    c = small_lease
    owner = c.fresh()
    c.owner.path.parent.mkdir(parents=True, exist_ok=True)
    c.owner.path.write_text(json.dumps({"state": "allocation_unknown", "operation_id": "unknown-attempt"}))
    before = c.owner.path.read_bytes()
    with pytest.raises(RunPodWorkerOwnerError, match="unknown-attempt.*astrid machines"):
        owner.start_fresh_claim()
    assert c.owner.path.read_bytes() == before and c.creates == 0 and not c.deletes


def test_manual_claim_cancellation_reaches_capacity_wait(small_lease):
    c = small_lease
    owner = c.fresh()
    owner.start_fresh_claim()
    stopped = []
    c.owner.observe_offer = lambda: (_ for _ in ()).throw(target_selection.OfferObservationError("stock_unavailable"))

    async def stop(seconds):
        stopped.append(True)

    c.owner.sleep = stop
    grant = manual_claim_grant(c, offer_digest=None, deadline=None,
        policy_digest=target_selection.policy_consent_digest(c.prefs, 1), max_hourly_usd=1)
    with pytest.raises(target_selection.CapacityWaitCancelled):
        owner.claim(grant, requested_idle_seconds=600, cancelled=lambda: bool(stopped))
    assert c.creates == 0 and not c.owner.path.exists()
