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


def test_result_budget_has_one_reset_start_and_zero_reset_polls_on_caller(machine_case, monkeypatch):
    c = machine_case
    claim(c)
    entered, complete, resets, polls = hold_reset(c, monkeypatch)
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
            assert set(result.result(timeout=1)["files"]) == {"artifacts/cpu.png", "manifest.json"}
            assert len(resets) == 1
            assert release_ops == ["job_start"]
            assert caller[0] not in polls
            pending = c.read()["lease"]
            assert pending["state"] == "active"
            assert pending["reset"]["state"] == "pending"
            assert pending["reset"]["job_id"] == resets[0]
        finally:
            complete.set()
    c.host.wait_for_reset()
    assert c.read()["lease"]["reset"]["state"] == "ready"
    assert c.read()["lease"]["state"] == "idle"


def test_next_job_waits_for_reset_without_polling_or_starting_it(machine_case, monkeypatch):
    c = machine_case
    claim(c)
    entered, complete, resets, _ = hold_reset(c, monkeypatch)
    with ThreadPoolExecutor() as pool:
        first = pool.submit(run, c, job(c))
        try:
            assert entered.wait(60)
            first.result(timeout=1)
            commands = len(c.pod.job_log)
            second = pool.submit(run, c, job(c, 2))
            with pytest.raises(TimeoutError):
                second.result(timeout=0.1)
            assert len(c.pod.job_log) == commands
            assert len(resets) == 1
        finally:
            complete.set()
        second.result(timeout=10)
    c.host.wait_for_reset()
    assert len(resets) == 2
    assert c.read()["lease"]["state"] == "idle"


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


def test_old_reset_completion_cannot_signal_a_later_reset(monkeypatch):
    from types import SimpleNamespace

    from astrid.packs.runpod import machine

    old, later = threading.Event(), threading.Event()
    owner = SimpleNamespace(
        _reset_done=old, lock=threading.RLock(), clock=lambda: 0,
        preparation_claim=lambda lease: {"lease": {"reset": {"state": "pending"}}},
        _save=lambda *args: None,
    )
    # A subsequent release can replace the completion event as the old owner
    # leaves settlement; its final notification belongs only to its own reset.
    owner.settle = lambda *args, **kwargs: setattr(owner, "_reset_done", later)
    adapter = machine.RunPodMachine(SimpleNamespace(lease_owner=owner))
    lease = SimpleNamespace(lease=None, claim=SimpleNamespace(ops=SimpleNamespace()),
                            node_ids=frozenset(), node_names=frozenset())
    monkeypatch.setattr(adapter, "_wait_reset", lambda *args, **kwargs: None)
    monkeypatch.setattr(adapter, "_finish", lambda *args: None)
    adapter._complete_reset(lease, {"limit": 600}, 600)
    assert old.is_set()
    assert not later.is_set()
