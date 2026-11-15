"""Warm results leave reset to a bounded owner, behind the journal gate."""
import threading
from concurrent.futures import ThreadPoolExecutor

import pytest

from astrid.core.execution.machine_run import run_on_machine
from tests.packs.runpod import test_machine
from tests.packs.runpod.test_generation_preparation import retained as retained
from tests.packs.runpod.test_job_on_claimed_machine import claim, job
from tests.packs.runpod.test_remote_placement import sources as sources
from tests.packs.runpod.test_target_selection import case as case
from tests.packs.runpod.test_warm_session import lease_selection_case as lease_selection_case
from tests.packs.runpod.test_warm_session import small_lease as small_lease

machine_case = test_machine.machine_case

def run(c, request):
    return run_on_machine(c.machine, request, destination=request.scratch / "outputs",
                          cancelled=lambda: False, sleep=lambda _: None)


def hold_reset(c, monkeypatch):
    entered, complete = threading.Event(), threading.Event()
    resets, polls = [], []
    start, poll = c.pod.job_start, c.pod.job_poll

    def job_start(command, *, job_id):
        if "pod_reset reset --run-dir" in command:
            record = c.read()["lease"]
            assert record["job"] == {"id": job_id, "state": "starting"}
            assert record["reset"]["state"] == "pending"
            assert record["reset"]["job_id"] == job_id
            resets.append(job_id)
        return start(command, job_id=job_id)

    def job_poll(job_id):
        if job_id in resets:
            polls.append(threading.get_ident())
            entered.set()
            if not complete.is_set():
                return {"id": job_id, "state": "running"}
        return poll(job_id)

    monkeypatch.setenv("ASTRID_MACHINE_POLL_SECONDS", "0.01")
    monkeypatch.setattr(c.pod, "job_start", job_start)
    monkeypatch.setattr(c.pod, "job_poll", job_poll)
    return entered, complete, resets, polls


@pytest.mark.parametrize("acknowledged", [True, False])
def test_result_budget_has_one_reset_start_and_zero_reset_polls_only_with_ack(
        machine_case, monkeypatch, acknowledged):
    c = machine_case
    claim(c)
    entered, complete, resets, polls = hold_reset(c, monkeypatch)
    if not acknowledged:
        monkeypatch.setattr("astrid.packs.runpod.pod_watchdog.write_deadline", lambda *args: False)
    caller, release_ops = [], []
    releasing = threading.Event()
    release = c.machine.release

    def release_counted(*args, **kwargs):
        releasing.set()
        try:
            return release(*args, **kwargs)
        finally:
            releasing.clear()

    monkeypatch.setattr(c.machine, "release", release_counted)
    for name in ("job_start", "job_poll", "job_cancel", "exec", "put", "get"):
        original = getattr(c.pod, name)

        def counted(*args, _name=name, _original=original, **kwargs):
            if releasing.is_set() and threading.get_ident() == caller[0]:
                release_ops.append(_name)
            return _original(*args, **kwargs)

        monkeypatch.setattr(c.pod, name, counted)

    def invoke():
        caller.append(threading.get_ident())
        return run(c, job(c))

    with ThreadPoolExecutor() as pool:
        result = pool.submit(invoke)
        try:
            assert entered.wait(60)
            if not acknowledged:
                # Host-only custody must spend at least one reset poll before
                # delivery; no reset wait may be skipped without a pod ACK.
                with pytest.raises(TimeoutError):
                    result.result(timeout=0.1)
                assert polls and set(polls) == {caller[0]}
                complete.set()
            assert set(result.result(timeout=1)["files"]) == {"artifacts/cpu.png", "manifest.json"}
            assert len(resets) == 1
            # The idle watchdog deadline is published before delivery. Reset
            # polls, stdout reads and cleanup stay on the bounded owner.
            if acknowledged:
                assert release_ops == ["job_start", "exec"]
                assert caller[0] not in polls
            else:
                assert release_ops.count("job_start") == 1
                assert release_ops.count("job_poll") == len(polls)
                assert release_ops.count("exec") == 1  # reset stdout/cleanup; publication is stubbed
            pending = c.read()["lease"]
            assert pending["state"] == ("active" if acknowledged else "idle")
            assert pending["reset"]["state"] == ("pending" if acknowledged else "ready")
            assert pending["reset"]["job_id"] == resets[0]
            idle_expiry = pending["idle_expires_at"]
            status = test_machine.rm.lease_row(c.read())
            assert status["reset_state"] == pending["reset"]["state"]
            assert status["state"] == pending["state"]
            assert status["release_at"] == status["idle_expires_at"] == idle_expiry
            c.clock.advance(5)
        finally:
            complete.set()
    c.host.wait_for_reset()
    assert c.read()["lease"]["reset"]["state"] == "ready"
    assert c.read()["lease"]["state"] == "idle"
    assert c.read()["lease"]["idle_expires_at"] == idle_expiry
    assert test_machine.rm.lease_row(c.read())["reset_state"] == "ready"


@pytest.mark.parametrize("acknowledged", [True, False])
def test_interrupt_after_result_keeps_published_idle_deadline_and_armed_timer(
        machine_case, monkeypatch, acknowledged):
    c = machine_case
    claim(c)
    interrupted = threading.Event()
    order = []
    if not acknowledged:
        monkeypatch.setattr("astrid.packs.runpod.pod_watchdog.write_deadline", lambda *args: False)
    finish, sync, arm = c.machine._finish, c.host._sync_deadline, c.host._arm

    def settled(invocation):
        finish(invocation)
        order.append("settled")

    def published(record):
        record = sync(record)
        if (record["lease"].get("reset") or {}).get("state") != "pending":
            return record
        assert record["lease"]["reset"]["state"] == "pending"
        assert record["self_stop"]["acknowledged"] is acknowledged
        order.append("published")
        return record

    def armed(record):
        arm(record)
        if (record["lease"].get("reset") or {}).get("state") == "pending":
            order.append("armed")

    complete_reset = c.machine._complete_reset

    def reset(*args, **kwargs):
        if acknowledged:
            # Model a controller interrupted after delivery, before completion.
            interrupted.wait(60)
        else:
            complete_reset(*args, **kwargs)
            order.append("reset")

    monkeypatch.setattr(c.machine, "_complete_reset", reset)
    monkeypatch.setattr(c.machine, "_finish", settled)
    monkeypatch.setattr(c.host, "_sync_deadline", published)
    monkeypatch.setattr(c.host, "_arm", armed)
    try:
        assert run(c, job(c))["files"]
        order.append("result")
        record = c.read()
        pending = record["lease"]
        assert order == ["settled", "published", "armed", *([] if acknowledged else ["reset"]), "result"]
        assert pending["state"] == ("active" if acknowledged else "idle")
        assert pending["reset"]["state"] == ("pending" if acknowledged else "ready")
        assert pending["last_job"]["outcome"] == "success"
        assert record["self_stop"]["deadline"] == pending["idle_expires_at"]
        assert record["self_stop"]["acknowledged"] is acknowledged
        hold = c.host.holds._holds[c.host.key]
        assert hold.timer.due == pending["idle_expires_at"]
        assert not hold.timer.cancelled and hold.leases == 0
        assert c.host.deadline_timer.due == pending["paid_expires_at"]
        assert c.active[-1][0] == "done"
    finally:
        interrupted.set()
        if c.host._reset_worker is not None:
            c.host._reset_worker.join(10)
    # Even loss of the owner leaves the pod's shorter deadline published;
    # recovery fences pending cleanup without waiting for another job.
    c.host.close()
    replacement = c.fresh()
    replacement.recover_job()
    if not acknowledged:
        # Completed host-only reset is safe to adopt; no pending work survives.
        assert c.read()["lease"]["reset"]["state"] == "ready"
        assert c.read()["lease"]["state"] == "idle"
        assert c.deletes == []
        replacement.release()
    assert c.read()["state"] == "cleanup_complete"
    assert c.deletes == ["pod-1"]


def test_next_job_waits_for_reset_without_polling_or_starting_it(machine_case, monkeypatch):
    c = machine_case
    claim(c)
    entered, complete, resets, _ = hold_reset(c, monkeypatch)
    waiting, admitted = threading.Event(), threading.Event()
    wait = c.host.wait_for_reset

    def wait_for_reset(**kwargs):
        waiting.set()
        wait(**kwargs)
        admitted.set()

    with ThreadPoolExecutor() as pool:
        first = pool.submit(run, c, job(c))
        try:
            assert entered.wait(60)
            first.result(timeout=1)
            commands = len(c.pod.job_log)
            monkeypatch.setattr(c.host, "wait_for_reset", wait_for_reset)
            second = pool.submit(run, c, job(c, 2))
            assert waiting.wait(60)
            assert not admitted.wait(0.1)
            with pytest.raises(TimeoutError):
                second.result(timeout=0.1)
            assert len(c.pod.job_log) == commands
            assert len(resets) == 1
        finally:
            complete.set()
        # Bound the reset wakeup itself, independently of the CPU-heavy
        # workflow/dispatch verification that follows admission.
        assert admitted.wait(1)
        second.result(timeout=10)  # Bound the remaining job after observing reset admission.
    c.host.wait_for_reset()
    assert len(resets) == 2
    assert c.read()["lease"]["state"] == "idle"


@pytest.mark.parametrize("publication", ["unknown", "no", "error"])
def test_unavailable_watchdog_reset_failure_releases_before_delivery(machine_case, monkeypatch, publication):
    c = machine_case
    claim(c)
    execute = c.pod.exec
    release = c.host.release
    order = []

    def exec_unavailable(command, *args):
        if ("__pod_watchdog__" in command
                and (c.read()["lease"].get("reset") or {}).get("state") == "pending"):
            order.append("publication")
            if publication == "error":
                raise OSError("unavailable watchdog")
            return 1, "pod_watchdog:termination_" + ("unknown" if publication == "unknown" else "denied"), ""
        return execute(command, *args)

    def released(**kwargs):
        result = release(**kwargs)
        assert c.deletes == ["pod-1"]
        order.append("released")
        return result

    c.pod.fail_jobs = {"--run-dir"}
    monkeypatch.setattr(c.pod, "exec", exec_unavailable)
    monkeypatch.setattr(c.host, "release", released)
    if publication == "error":
        from astrid.packs.runpod.pod_watchdog import PodWatchdogError

        # Publishing failure refuses settlement; exact release precedes the error.
        with pytest.raises(PodWatchdogError):
            run(c, job(c))
    else:
        assert run(c, job(c))["files"]
    order.append("result")
    assert order.index("released") < order.index("result")
    assert c.read()["state"] == "cleanup_complete"
    if publication != "error":
        assert c.read()["lease"]["reset"]["state"] == "failed"
        assert test_machine.rm.lease_row(c.read())["reset_state"] == "failed"
        assert c.read()["lease"]["ended"] == "reset_failed"
    assert c.host._reset_worker is None


def test_unacknowledged_failed_reset_and_release_never_delivers_result(machine_case, monkeypatch):
    from runpod_lifecycle import api

    c = machine_case
    claim(c)
    terminate = api.terminate_pod

    def terminate_or_fail(pod_id, key):
        # The shared fake removes the pod before raising. A rejected delete
        # must leave it alive, so a second release cannot mistake it for gone.
        if c.delete_error:
            raise c.delete_error
        return terminate(pod_id, key)

    monkeypatch.setattr(api, "terminate_pod", terminate_or_fail)
    monkeypatch.setattr("astrid.packs.runpod.pod_watchdog.write_deadline", lambda *args: False)
    c.pod.fail_jobs = {"--run-dir"}
    c.delete_error = RuntimeError("fake release failure")
    try:
        with pytest.raises(RuntimeError, match="fake release failure"):
            run(c, job(c))
        record = c.read()
        assert record["state"] in {"cleanup_pending", "release_pending"}
        assert record["lease"]["state"] == "releasing"
        assert record["lease"]["reset"]["state"] == "failed"
        assert not c.host.deadline_timer.cancelled
        assert c.host._reset_worker is None
    finally:
        c.delete_error = None
        c.host.release(reason="reset_failed")
    assert c.read()["state"] == "cleanup_complete"


@pytest.mark.parametrize("failure", ["exit", "timeout"])
def test_deferred_reset_failure_keeps_exact_release_state(machine_case, monkeypatch, failure):
    from astrid.packs.runpod import machine

    c = machine_case
    claim(c)
    errors = []
    wait = c.machine._wait_reset
    release = c.host.release

    def observe_wait(*args, **kwargs):
        try:
            return wait(*args, **kwargs)
        except machine.MachineError as error:
            errors.append(str(error))
            raise

    def release_after_done(**kwargs):
        assert c.active[-1][0] == "done"
        return release(**kwargs)

    monkeypatch.setattr(c.machine, "_wait_reset", observe_wait)
    monkeypatch.setattr(c.host, "release", release_after_done)
    if failure == "exit":
        c.pod.fail_jobs = {"--run-dir"}
    else:
        monkeypatch.setattr(machine, "RESET_SECONDS", 0.05)
        start, poll = c.pod.job_start, c.pod.job_poll
        resets = []

        def job_start(command, *, job_id):
            if "pod_reset reset --run-dir" in command:
                resets.append(job_id)
            return start(command, job_id=job_id)

        monkeypatch.setattr(c.pod, "job_start", job_start)
        monkeypatch.setattr(c.pod, "job_poll", lambda job_id: (
            {"id": job_id, "state": "running"} if job_id in resets else poll(job_id)))
    result = run(c, job(c))
    assert result["files"]
    c.host._reset_worker.join(10)
    assert not c.host._reset_worker.is_alive()
    record = c.read()
    assert record["state"] == "cleanup_complete"
    assert record["lease"]["ended"] == "reset_failed"
    assert record["lease"]["reset"]["state"] == "failed"
    assert test_machine.rm.lease_row(record)["reset_state"] == "failed"
    assert record["lease"]["job"]["state"] == ("exited" if failure == "exit" else "cancelled")
    assert c.deletes == ["pod-1"] and c.creates == 1
    assert errors == (["pod reset failed (exit 1): unknown"] if failure == "exit" else
                      [f"reset did not finish in {machine.RESET_SECONDS:.0f}s"])
    if failure == "timeout":
        assert record["lease"]["reset"]["job_id"] in c.pod.cancelled


@pytest.mark.parametrize("state", ["active", "idle"])
def test_crashed_reset_is_fenced_by_recovery_without_next_job(machine_case, monkeypatch, state):
    c = machine_case
    claim(c)
    # Simulate losing the host after its start checkpoint, before its wait.
    monkeypatch.setattr(c.machine, "_complete_reset", lambda *args: None)
    run(c, job(c))
    c.host._reset_worker.join(5)
    pending = c.read()["lease"]
    assert pending["reset"]["state"] == "pending"
    reset_id = pending["reset"]["job_id"]
    if state == "idle":
        record = c.read()
        c.host._save(record, {**pending, "state": "idle", "idle_expires_at": c.clock() + 60})
    c.host.close()
    replacement = c.fresh()
    replacement.recover_job()
    record = c.read()
    assert record["state"] == "cleanup_complete"
    assert record["lease"]["job"] == {"id": reset_id, "state": "cancelled"}
    assert reset_id in c.pod.cancelled and c.deletes == ["pod-1"]


def test_pending_reset_admission_wait_is_bounded(machine_case, monkeypatch):
    from astrid.packs.runpod.worker_preparation import AcquisitionUnresolvedError

    c = machine_case
    claim(c)
    entered, complete, _, _ = hold_reset(c, monkeypatch)
    with ThreadPoolExecutor() as pool:
        result = pool.submit(run, c, job(c))
        try:
            assert entered.wait(60)
            result.result(timeout=1)
            with c.host.lock:
                record = c.read()
                info = record["lease"]
                info["reset"]["deadline"] = c.clock() - 1
                c.host._save(record, info)
            commands = len(c.pod.job_log)
            with pytest.raises(AcquisitionUnresolvedError, match="bounded wait"):
                c.machine.claim(job(c, 2))
            assert len(c.pod.job_log) == commands
        finally:
            complete.set()
            if c.host._reset_worker is not None:
                c.host._reset_worker.join(10)


def test_pending_reset_does_not_delay_idle_expiry_release(machine_case, monkeypatch):
    c = machine_case
    claim(c)
    entered, complete, resets, _ = hold_reset(c, monkeypatch)
    try:
        assert run(c, job(c))["files"]
        assert entered.wait(10)
        expiry = c.read()["lease"]["idle_expires_at"]
        c.clock.advance(expiry - c.clock())
        record = c.read()
        assert record["state"] == "cleanup_complete"
        assert record["lease"]["ended"] == "idle"
        assert resets[0] in c.pod.cancelled and c.deletes == ["pod-1"]
    finally:
        complete.set()
        c.host._reset_worker.join(10)
        assert not c.host._reset_worker.is_alive()


def test_post_result_release_exception_keeps_durable_fence_and_retry(machine_case, monkeypatch):
    c = machine_case
    claim(c)
    entered, complete, _, _ = hold_reset(c, monkeypatch)
    c.pod.fail_jobs = {"--run-dir"}
    c.delete_error = RuntimeError("fake release failure")
    try:
        assert run(c, job(c))["files"]
        assert entered.wait(10)
    finally:
        complete.set()
        c.host._reset_worker.join(10)
    assert not c.host._reset_worker.is_alive()
    record = c.read()
    assert record["state"] in {"cleanup_pending", "release_pending"}
    assert record["lease"]["state"] == "releasing"
    assert record["lease"]["release_reason"] == "reset_failed"
    assert record["lease"]["reset"]["state"] == "failed"
    assert c.host.last_error_type == "RuntimeError"
    assert not c.host.deadline_timer.cancelled
    with pytest.raises(Exception, match="unresolved preparation|recovery is pending"):
        c.machine.claim(job(c, 2))
    c.delete_error = None
    c.host.release(reason="reset_failed")
    assert c.read()["state"] == "cleanup_complete"


def test_settlement_exception_before_delivery_remains_a_release_stage_failure(machine_case, monkeypatch):
    c = machine_case
    claim(c)
    failures = []

    def failed_settlement(*args, **kwargs):
        raise RuntimeError("fake settlement failure")

    monkeypatch.setattr(c.host, "settle", failed_settlement)
    request = job(c)
    with pytest.raises(RuntimeError, match="fake settlement failure"):
        run_on_machine(c.machine, request, destination=request.scratch / "outputs",
                       cancelled=lambda: False, sleep=lambda _: None,
                       on_failure=failures.append)
    assert c.read()["state"] == "cleanup_complete"
    assert c.read()["lease"]["ended"] == "settlement_unverified"
    assert c.deletes == ["pod-1"]
    assert len(failures) == 1 and failures[0]["stage"] == "release"
    assert c.host._reset_worker is None


def test_post_result_reset_keeps_the_original_invocation_fence(machine_case, monkeypatch):
    c = machine_case
    claim(c)
    entered, complete, _, _ = hold_reset(c, monkeypatch)
    try:
        assert run(c, job(c))["files"]
        assert entered.wait(10)
        with c.host.lock:
            record = c.read()
            c.host._save(record, {**record["lease"], "invocation_id": "different-invocation"})
    finally:
        complete.set()
        c.host._reset_worker.join(10)
    assert not c.host._reset_worker.is_alive()
    record = c.read()
    assert record["state"] == "cleanup_complete"
    assert record["lease"]["ended"] == "reset_failed"
    assert record["lease"]["reset"]["state"] != "ready"
    assert c.deletes == ["pod-1"]


def test_old_reset_completion_cannot_signal_a_later_reset(monkeypatch):
    from types import SimpleNamespace

    from astrid.packs.runpod import machine

    old, later = threading.Event(), threading.Event()
    owner = SimpleNamespace(
        _reset_done=old, lock=threading.RLock(), clock=lambda: 0,
        preparation_claim=lambda lease: {"lease": {"reset": {"state": "pending", "job_id": "reset"},
                                                   "job": {"id": "reset", "state": "exited"}}},
    )
    # A subsequent release can replace the completion event as the old owner
    # marks ready; its final notification belongs only to its own reset.
    owner._save = lambda *args: setattr(owner, "_reset_done", later)
    adapter = machine.RunPodMachine(SimpleNamespace(lease_owner=owner))
    lease = SimpleNamespace(lease=None, claim=SimpleNamespace(ops=SimpleNamespace()),
                            node_ids=frozenset(), node_names=frozenset())
    monkeypatch.setattr(adapter, "_wait_reset", lambda *args, **kwargs: None)
    monkeypatch.setattr(adapter, "_finish", lambda *args: None)
    adapter._complete_reset(lease, {"id": "reset", "limit": 600}, 600)
    assert old.is_set()
    assert not later.is_set()


def test_zero_idle_reset_budget_finishes_before_immediate_release(machine_case, monkeypatch):
    from dataclasses import replace

    c = machine_case
    claim(c)
    paid = dict(c.read()["paid_limits"])
    request = job(c)
    request = replace(request, policy={**request.policy, "idle_timeout_s": 0})
    reset_ids, poll_threads, armed_jobs = [], [], []
    start, poll, arm = c.pod.job_start, c.pod.job_poll, c.host._arm

    def started(command, **kwargs):
        if "pod_reset reset --run-dir" in command:
            reset_ids.append(kwargs["job_id"])
        return start(command, **kwargs)

    def polled(job_id):
        if job_id in reset_ids:
            poll_threads.append(threading.get_ident())
        return poll(job_id)

    def armed(record):
        info = record["lease"]
        last = info.get("last_job") or {}
        if (info["idle_seconds"] == 0 and last.get("outcome") == "success"
                and last.get("generation") == info["generation"]):
            armed_jobs.append(info["job"]["state"])
        return arm(record)

    monkeypatch.setattr(c.host, "_arm", armed)
    monkeypatch.setattr(c.pod, "job_start", started)
    monkeypatch.setattr(c.pod, "job_poll", polled)
    caller = threading.get_ident()
    assert run(c, request)["files"]
    c.host.wait_for_reset()
    assert len(reset_ids) == 1
    assert armed_jobs == ["exited"]
    assert poll_threads == [caller]
    assert c.host._reset_worker is None
    record = c.read()
    assert record["lease"]["idle_seconds"] == 0
    assert record["lease"]["job"]["state"] == "exited"
    assert record["paid_limits"] == paid
    c.clock.advance(0)
    assert c.read()["state"] == "cleanup_complete"
    assert c.creates == 1 and c.deletes == ["pod-1"]
