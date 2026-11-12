"""Warm sequence regressions for execution, admission, and install checkpoints."""
from __future__ import annotations

import json
import time

import pytest

from astrid.core.execution.machine_run import run_on_machine
from astrid.packs.runpod import machine as rm
from astrid.packs.runpod.prepared_task import PreparedWorkerError
from tests.packs.runpod.test_failure_receipt import _only, _run
from tests.packs.runpod.test_generation_preparation import retained as retained  # noqa: F401 (fixture)
from tests.packs.runpod.test_machine import machine_case as machine_case  # noqa: F401 (fixture)
from tests.packs.runpod.test_remote_placement import sources as sources  # noqa: F401 (fixture)
from tests.packs.runpod.test_target_selection import case as case  # noqa: F401 (fixture)
from tests.packs.runpod.test_warm_session import (  # noqa: F401 (fixtures)
    lease_selection_case as lease_selection_case,
    small_lease as small_lease,
)


@pytest.mark.parametrize("changed", ["job", "epoch", "paid_expiry"])
def test_checkpoint_refuses_custody_changed_during_verification(machine_case, monkeypatch, changed):
    from astrid.packs.runpod.worker_owner import RunPodWorkerOwnerError

    c = machine_case
    job = c.job()
    lease = c.machine.claim(job)
    run = c.machine.start(lease, job)
    c.machine._checkpoint(lease, {"id": run.job_id, "state": "exited"})
    verify = c.placement._canonical_installed

    def changed_while_reading(state, **kwargs):
        result = verify(state, **kwargs)
        if changed == "job":
            record = c.read()
            c.host._save(record, {**record["lease"], "job": {"id": "f" * 32, "state": "exited"}})
        elif changed == "epoch":
            monkeypatch.setattr(c.host, "epoch", "foreign-epoch")
        else:
            monkeypatch.setattr(c.clock, "now", c.read()["lease"]["paid_expires_at"])
        return result

    monkeypatch.setattr(c.placement, "_canonical_installed", changed_while_reading)
    with pytest.raises((rm.CustodyChanged, RunPodWorkerOwnerError)):
        c.machine._checkpoint_installed(run, {})


@pytest.mark.parametrize("near_deadline", [False, True])
def test_warm_failure_admission_success_and_long_success_share_one_machine(
    machine_case, tmp_path, monkeypatch, near_deadline,
):
    c = machine_case
    receipts = tmp_path / "failures"
    # The fake engine context is shared across these four claims. Its wall
    # deadline must cover the sequence; the placement's execution clock below
    # remains independent and is the budget this regression exercises.
    c.context.deadline = time.monotonic() + 600
    ticks = [0.0]
    monkeypatch.setattr(c.placement, "_clock", lambda: ticks[0])

    # The first attempt reaches the pod and exits unsuccessfully. The failure is
    # verified, so the exact machine is retained for another attempt.
    c.pod.fail_jobs = {" generate cpu-image "}
    original_verify = c.placement._canonical_installed

    def unverified_after_failure(state, **kwargs):
        if kwargs.get("verification_seconds") is None:
            return original_verify(state, **kwargs)
        state.install_unverified = "status=failed;error_code=source_changed;failed_step_id=source"
        return False

    monkeypatch.setattr(c.placement, "_canonical_installed", unverified_after_failure)
    error, _, _ = _run(c, receipts / "execution")
    assert isinstance(error, rm.MachineError)
    execution_receipt = _only(receipts / "execution")
    assert execution_receipt["stage"] == "execution"
    assert execution_receipt["machine"]["after"] == "kept"
    assert c.read()["lease"]["preparation"]["state"] == "stale_after_failure"
    assert c.read()["lease"]["state"] == "idle"
    assert c.creates == 1 and c.deletes == []
    monkeypatch.setattr(c.placement, "_canonical_installed", original_verify)

    # Admission is refused before another pod operation. This also leaves the
    # verified idle machine available for the next attempt.
    c.pod.fail_jobs.clear()
    original_validate = c.adapter.validate
    monkeypatch.setattr(c.adapter, "validate", lambda *_a, **_k: (_ for _ in ()).throw(
        PreparedWorkerError("admission refused")))
    admission_error, _, _ = _run(c, receipts / "admission", job=c.job(2))
    assert isinstance(admission_error, PreparedWorkerError)
    admission_receipt = _only(receipts / "admission")
    assert admission_receipt["stage"] == "no_machine"
    assert c.read()["lease"]["state"] == "idle"
    assert c.creates == 1 and c.deletes == []
    monkeypatch.setattr(c.adapter, "validate", original_validate)

    # A regular success reuses that pod and makes it warm again.
    third = c.job(3)
    run_on_machine(c.machine, third, destination=third.scratch / "outputs", cancelled=lambda: False,
                   sleep=lambda _s: None)
    assert c.read()["lease"]["state"] == "idle"
    assert c.read()["lease"]["preparation"]["state"] == "prepared"
    assert c.read()["lease"]["preparation"]["installed"]
    assert c.creates == 1 and c.deletes == []

    # On the fourth job, optionally consume nearly all execution time after the
    # generation process exits. Canonical install verification must use the
    # remaining paid lease budget, not the nearly exhausted execution budget.
    original_start = c.pod.job_start

    def start(command, **kwargs):
        result = original_start(command, **kwargs)
        if near_deadline and " generate cpu-image " in command:
            ticks[0] += 59.5
        return result

    monkeypatch.setattr(c.pod, "job_start", start)
    original_exec = c.pod.exec

    def status_with_short_execution_budget(command, timeout=600):
        if (near_deadline and "engines status" in command and "--deadline " in command
                and float(command.rsplit("--deadline ", 1)[1].split()[0]) < 2):
            return 0, json.dumps({"status": "failed", "error_code": "deadline_exceeded",
                                  "failed_step_id": "health"}), ""
        return original_exec(command, timeout)

    monkeypatch.setattr(c.pod, "exec", status_with_short_execution_budget)
    fourth = c.job(4)
    fetched = run_on_machine(c.machine, fourth, destination=fourth.scratch / "outputs", cancelled=lambda: False,
                             sleep=lambda _s: None)
    assert "artifacts/cpu.png" in fetched["files"]
    assert c.read()["lease"]["state"] == "idle"
    assert c.creates == 1 and c.deletes == []


def test_changed_canonical_installation_has_fixed_reason_in_receipt(machine_case, tmp_path, monkeypatch):
    c = machine_case
    receipts = tmp_path / "failures"
    generated = False
    original_start = c.pod.job_start

    def start(command, **kwargs):
        nonlocal generated
        result = original_start(command, **kwargs)
        if " generate cpu-image " in command:
            generated = True
            next(iter(c.context.sources.values()))["dirty"] = " M changed.py"
        return result

    monkeypatch.setattr(c.pod, "job_start", start)
    error, _, _ = _run(c, receipts)
    assert isinstance(error, rm.MachineError)
    receipt = _only(receipts)
    assert generated
    reason = error.install_unverified
    assert reason and reason in str(error)
    assert receipt["cause"]["install_unverified"] == reason
    from astrid.packs.runpod.failure_receipt import render
    assert reason in render(receipt)
