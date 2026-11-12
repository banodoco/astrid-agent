"""Warm sequence regressions for execution, admission, and install checkpoints."""
from __future__ import annotations

import json
import time

import pytest
from runpod_lifecycle.https import HttpsPodTransport as _CheckpointTransport

from astrid.core.execution.machine_run import run_on_machine
from astrid.packs.runpod import machine as rm
from astrid.packs.runpod.prepared_task import PreparedWorkerError
from tests.packs.runpod.test_failure_receipt import _only, _run
from tests.packs.runpod.test_generation_preparation import (
    retained as retained,  # noqa: F401 (fixture)
)
from tests.packs.runpod.test_machine import machine_case as machine_case  # noqa: F401 (fixture)
from tests.packs.runpod.test_remote_placement import sources as sources  # noqa: F401 (fixture)
from tests.packs.runpod.test_target_selection import case as case  # noqa: F401 (fixture)
from tests.packs.runpod.test_warm_session import (  # noqa: F401 (fixtures)
    lease_selection_case as lease_selection_case,
)
from tests.packs.runpod.test_warm_session import (
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


def test_checkpoint_unreaped_observer_releases_failure_then_timer_owner_recovers(
    machine_case, monkeypatch,
):
    from astrid.packs.runpod import remote_placement
    from astrid.packs.runpod.worker_preparation import AcquisitionUnresolvedError

    c = machine_case
    lease = c.machine.claim(c.job())
    run = c.machine.start(lease, c.job())
    c.machine._checkpoint(lease, {"id": run.job_id, "state": "exited"})

    class Child:
        alive = True
        closed = False
        close_count = 0

        def _check_open(self):
            assert not self.closed, "observer handle accessed after close"

        def is_alive(self):
            self._check_open()
            return self.alive

        def join(self, timeout=0):
            self._check_open()

        def close(self):
            self._check_open()
            self.close_count += 1
            self.closed = True

    child = Child()
    registrations = []
    fence = c.host.fence_unreaped_observer

    def count_registration(*args, **kwargs):
        registrations.append(args)
        return fence(*args, **kwargs)

    monkeypatch.setattr(c.host, "fence_unreaped_observer", count_registration)

    def unreaped(self, *_args, **_kwargs):
        self.unreaped_child = child
        raise remote_placement.BoundedObserverUnreaped("observer could not be reaped")

    monkeypatch.setattr(remote_placement._HttpsOps, "_bounded_call", unreaped)
    monkeypatch.setattr(c.placement, "_canonical_installed",
                        lambda state, **kwargs: state.claim.ops.job_poll(run.job_id))
    with pytest.raises(AcquisitionUnresolvedError) as checkpoint_error:
        c.machine._checkpoint_installed(run, {})
    with pytest.raises(AcquisitionUnresolvedError):
        c.machine.release(lease, False, failure=checkpoint_error.value)
    pending = c.read()
    assert len(registrations) == 1
    assert pending["state"] == "cleanup_pending" and pending["lease"]["state"] == "releasing"
    assert c.deletes == [] and child.alive and not child.closed

    # A provider delete failure leaves cleanup pending for the real fake timer's
    # owner callback to retry. The child is now gone, so the retry may close it.
    child.alive = False
    from runpod_lifecycle import api
    original_delete = api.terminate_pod
    delete_attempts = []

    def fail_first_delete(pod_id, key):
        delete_attempts.append(pod_id)
        if len(delete_attempts) == 1:
            raise RuntimeError("temporary provider failure")
        return original_delete(pod_id, key)

    monkeypatch.setattr(api, "terminate_pod", fail_first_delete)
    c.clock.advance(c.host.deadline_timer.due - c.clock())
    assert delete_attempts == ["pod-1"]
    assert c.read()["state"] == "cleanup_pending"
    assert c.deletes == []
    c.clock.advance(c.host.deadline_timer.due - c.clock())
    assert delete_attempts == ["pod-1", "pod-1"]
    assert c.deletes == ["pod-1"]
    assert c.read()["state"] == "cleanup_complete"
    assert child.closed and child.close_count == 1


def test_earlier_observer_deadline_reason_is_retained_in_receipt(machine_case, monkeypatch):
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
    from threading import Event, Thread

    from astrid.packs.runpod.failure_receipt import build_receipt, render
    from astrid.packs.runpod.remote_placement import PlacementReasonError, _HttpsOps

    c = machine_case
    job = c.job()
    lease = c.machine.claim(job)
    run = c.machine.start(lease, job)
    c.machine._checkpoint(lease, {"id": run.job_id, "state": "exited"})
    entered, unblock = Event(), Event()

    class Stalled(BaseHTTPRequestHandler):
        def do_POST(self):
            self.rfile.read(int(self.headers.get("Content-Length", "0")))
            entered.set()
            unblock.wait(5)
            self.close_connection = True

        def log_message(self, *_args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Stalled)
    Thread(target=server.serve_forever, daemon=True).start()
    monkeypatch.setattr(_HttpsOps, "_bounded_call", c.real_bounded_call)
    deadline = time.monotonic() + 3
    original_ops = _HttpsOps("pod1", "x" * 32, deadline=deadline,
                             base_url=f"http://127.0.0.1:{server.server_port}")
    lease.claim.ops = original_ops
    try:
        with pytest.raises(PlacementReasonError) as caught:
            c.machine._checkpoint_installed(run, {})
        assert entered.is_set(), "the request must stall before its deadline"
        assert time.monotonic() < deadline, "request cleanup must finish inside the reserved allowance"
        assert lease.claim.ops is original_ops
        error = caught.value
        assert error.reason.code == "deadline_exceeded"
        assert error.install_unverified == "status_deadline_exhausted"
        assert "status_deadline_exhausted" in str(error)
        receipt = build_receipt({"failure": error, "stage": "fetch", "lease": lease, "run": run},
                                job=job, machine=c.machine.describe(lease), timing=None)
        assert receipt is not None
        assert receipt["cause"]["install_unverified"] == "status_deadline_exhausted"
        assert receipt["cause"]["reason"]["code"] == "deadline_exceeded"
        assert "status_deadline_exhausted" in receipt["message"]
        headline = render(receipt).splitlines()[1]
        assert headline == receipt["message"] and "status_deadline_exhausted" in headline
    finally:
        unblock.set()
        server.shutdown()
        server.server_close()


def test_owner_reaper_retries_after_one_handle_fails_during_liveness_check(machine_case):
    c = machine_case
    lease = c.machine.claim(c.job())

    class Child:
        def __init__(self, *, alive=False, fail_liveness=False):
            self.alive = alive
            self.fail_liveness = fail_liveness
            self.closed = False
            self.close_count = 0
            self.liveness_calls = 0

        def _check_open(self):
            assert not self.closed, "owner reaper accessed a closed observer handle"

        def is_alive(self):
            self._check_open()
            self.liveness_calls += 1
            if self.fail_liveness:
                self.fail_liveness = False
                raise ValueError("temporary process handle failure")
            return self.alive

        def join(self, timeout=0):
            self._check_open()

        def close(self):
            self._check_open()
            self.close_count += 1
            self.closed = True

    first = Child()
    second = Child(fail_liveness=True)
    c.host.fence_unreaped_observer(lease.lease, lease.claim.pod_id, first)
    c.host.fence_unreaped_observer(lease.lease, lease.claim.pod_id, second)
    scheduled_before = len(c.clock.timers)
    c.clock.advance(c.host.deadline_timer.due - c.clock())
    assert first.closed and first.close_count == 1 and first.liveness_calls == 1
    assert not second.closed and second.liveness_calls == 1
    assert c.read()["state"] == "cleanup_pending" and c.deletes == []
    assert len(c.clock.timers) > scheduled_before, "failed liveness check must schedule another recovery"

    c.clock.advance(c.host.deadline_timer.due - c.clock())
    assert first.closed and first.liveness_calls == 1
    assert second.closed and second.close_count == 1 and second.liveness_calls == 2
    assert c.read()["state"] == "cleanup_complete" and c.deletes == ["pod-1"]


def test_success_checkpoint_verification_does_not_hold_owner_lock_during_stalled_http(
    machine_case, monkeypatch,
):
    import time as _time
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
    from threading import Event, Thread

    from astrid.packs.runpod.remote_placement import _HttpsOps

    c = machine_case
    job = c.job()
    lease = c.machine.claim(job)
    run = c.machine.start(lease, job)
    c.machine._checkpoint(lease, {"id": run.job_id, "state": "exited"})
    with c.host.lock:
        record = c.host._read()
        shortened = {**record["lease"], "paid_expires_at": c.clock() + 10}
        c.host._arm(c.host._save(record, shortened))
    successful_checkpoints = []
    original_checkpoint_save = c.machine._save_installation

    def observe_checkpoint_save(*args, **kwargs):
        result = original_checkpoint_save(*args, **kwargs)
        successful_checkpoints.append(result)
        return result

    monkeypatch.setattr(c.machine, "_save_installation", observe_checkpoint_save)
    entered, unblock = Event(), Event()
    requests = []

    class Stalled(BaseHTTPRequestHandler):
        def do_POST(self):
            length = int(self.headers.get("Content-Length", "0"))
            request_body = self.rfile.read(length)
            requests.append(self.path)
            self.server.request_body = request_body
            entered.set()
            unblock.wait(10)
            body = b'{"output": ""}'
            self.send_response(200)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            try:
                self.wfile.write(body)
            except (BrokenPipeError, ConnectionResetError):
                pass

        def log_message(self, *_args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Stalled)
    Thread(target=server.serve_forever, daemon=True).start()
    monkeypatch.setattr(_HttpsOps, "_bounded_call", c.real_bounded_call)
    original_bounded = _HttpsOps.bounded
    observed_abort_events = []

    def bounded(ops, deadline, *, abort_event=None):
        assert deadline - _time.monotonic() <= 10
        assert abort_event is not None
        observed_abort_events.append(abort_event)
        return original_bounded(ops, deadline, abort_event=abort_event)

    monkeypatch.setattr(_HttpsOps, "bounded", bounded)
    lease.claim.ops = _HttpsOps("pod1", "x" * 32,
                                base_url=f"http://127.0.0.1:{server.server_port}")
    # The fixture replaces the parent transport with its in-memory pod. Keep
    # this one observer on localhost even on the unbounded baseline path.
    monkeypatch.setattr(lease.claim.ops, "_transport", lambda: _CheckpointTransport(
        "pod1", "x" * 32, base_url=f"http://127.0.0.1:{server.server_port}"))
    errors = []
    checkpoint = Thread(target=lambda: _capture_error(
        errors, lambda: c.machine._checkpoint_installed(run, {})), daemon=True)
    expiry = Thread(target=c.clock.advance, args=(10,), daemon=True)
    try:
        checkpoint.start()
        assert entered.wait(5), errors
        expiry.start()
        expiry.join(3)
        assert not expiry.is_alive(), "paid expiry must complete while verification is stalled"
        assert c.read()["state"] == "cleanup_complete"
        assert c.deletes == ["pod-1"]
        assert observed_abort_events and observed_abort_events[0].is_set()
    finally:
        unblock.set()
        checkpoint.join(7)
        if expiry.ident is not None:
            expiry.join(7)
        server.shutdown()
        server.server_close()
    assert not checkpoint.is_alive() and not expiry.is_alive()
    assert requests and requests[0] == "/jobs"
    assert b"engines status" in server.request_body
    assert not successful_checkpoints
    assert errors and isinstance(errors[0], rm.RemotePlacementError)


def _capture_error(errors, call):
    try:
        call()
    except BaseException as exc:  # noqa: BLE001
        errors.append(exc)


@pytest.mark.parametrize("remaining", [2, 300])
def test_success_checkpoint_uses_bounded_paid_verification_window(machine_case, monkeypatch, remaining):
    import time as _time

    c = machine_case
    job = c.job()
    lease = c.machine.claim(job)
    run = c.machine.start(lease, job)
    c.machine._checkpoint(lease, {"id": run.job_id, "state": "exited"})
    with c.host.lock:
        record = c.host._read()
        shortened = {**record["lease"], "paid_expires_at": c.clock() + remaining}
        c.host._arm(c.host._save(record, shortened))
    observed = []

    def observe_window(state, *, verification_seconds=None):
        deadline = state.claim.ops.deadline
        abort_event = state.claim.ops.abort_event
        observed.append((deadline, abort_event, verification_seconds))
        assert deadline is not None
        assert 0 < deadline - _time.monotonic() <= min(120, remaining)
        assert abort_event is not None
        state.install_unverified = "status_no_receipt"
        return False

    monkeypatch.setattr(c.placement, "_canonical_installed", observe_window)
    with pytest.raises(rm.MachineError) as caught:
        c.machine._checkpoint_installed(run, {})
    assert observed and observed[0][1] is not None
    assert 0 < observed[0][2] <= min(120, remaining)
    assert caught.value.install_unverified == "status_no_receipt"


@pytest.mark.parametrize(("mode", "reason", "error_type"), [
    ("verification_false", "status_no_receipt", rm.MachineError),
    ("receipt_changed", "installation_receipt_changed", None),
    ("attestation_missing", "checkpoint_attestation_missing", None),
    ("attestation_mismatch", "checkpoint_attestation_mismatch", None),
    ("installation", "checkpoint_installation_changed", rm.CustodyChanged),
    ("job", "checkpoint_job_changed", rm.CustodyChanged),
    ("epoch", "checkpoint_epoch_changed", None),
    ("invocation", "checkpoint_invocation_changed", None),
    ("paid_expiry", "checkpoint_paid_expired", None),
])
def test_checkpoint_reason_survives_failure_receipt_and_render(
    machine_case, tmp_path, monkeypatch, mode, reason, error_type,
):
    from astrid.packs.runpod import failure_receipt
    from astrid.packs.runpod.remote_placement import InstallationReceiptChanged
    from astrid.packs.runpod.worker_owner import RunPodWorkerOwnerError
    expected_type = error_type or (InstallationReceiptChanged if mode == "receipt_changed" else
                    rm.InstallationUnverified if mode.startswith("attestation") else RunPodWorkerOwnerError)
    c = machine_case
    job = c.job()
    lease = c.machine.claim(job)
    run = c.machine.start(lease, job)
    c.machine._checkpoint(lease, {"id": run.job_id, "state": "exited"})
    canonical = c.placement._canonical_installed
    checkpoint_files = {}

    def fail_verification(state, **kwargs):
        if mode == "verification_false":
            state.install_unverified = reason
            return False
        if mode == "receipt_changed":
            original_exec = c.pod.exec
            def changed_receipt(command, timeout=600):
                if "base64" in command and "os.open" in command:
                    path = command.rsplit(" ", 1)[-1].strip("'")
                    payload = json.loads(c.pod.files[path])
                    payload["receipt_id"] = "changed-receipt-id"
                    c.pod.files[path] = json.dumps(payload).encode()
                return original_exec(command, timeout)
            state.claim.ops.exec = changed_receipt
            return canonical(state, **kwargs)
        if mode == "attestation_missing":
            state.installed = {**state.installed, "receipt_sha256": "sha256:" + "f" * 64}
            return True
        if mode == "attestation_mismatch":
            installed = state.installed
            state.installed = {**installed, "receipt_sha256": "sha256:" + "f" * 64}
            manifest = {
                "managed_generation_result": {
                    "task_id": run.task_id, "attempt_id": run.attempt_id,
                    "evidence": {"producer": {"attempt": {"record": {
                        "managed_engine_preparation": {
                            "schema": "astrid.managed-engine-preparation.v1",
                            "task_id": run.task_id, "attempt_id": run.attempt_id,
                            "installation": {
                                "root": installed["root"],
                                "recipe_sha256": installed["compatibility"]["recipe_digest"].removeprefix("sha256:"),
                                "receipt_sha256": "e" * 64,
                            },
                        },
                    }}}},
                }
            }
            manifest_path = tmp_path / "manifest.json"
            manifest_path.write_text(json.dumps(manifest))
            checkpoint_files["manifest.json"] = str(manifest_path)
            return True
        if mode == "installation":
            record = c.read()
            preparation = dict(record["lease"]["preparation"])
            preparation["installed"] = {**preparation["installed"], "receipt_sha256": "sha256:" + "0" * 64}
            c.host._save(record, {**record["lease"], "preparation": preparation})
            return canonical(state, **kwargs)
        result = canonical(state, **kwargs)
        record = c.read()
        if mode == "job":
            c.host._save(record, {**record["lease"], "job": {"id": "f" * 32, "state": "exited"}})
        elif mode == "epoch":
            c.host.epoch = "foreign-epoch"
        elif mode == "invocation":
            c.host._save(record, {**record["lease"], "invocation_id": "foreign-invocation"})
        elif mode == "paid_expiry":
            c.clock.now = record["lease"]["paid_expires_at"]
        return result

    monkeypatch.setattr(c.placement, "_canonical_installed", fail_verification)
    with pytest.raises(expected_type) as caught:
        c.machine._checkpoint_installed(run, checkpoint_files)
    error = caught.value
    assert type(error) is expected_type
    assert reason in str(error)
    assert "\n" not in str(error)
    assert getattr(error, "install_unverified", None) == reason
    receipt = failure_receipt.build_receipt(
        {"failure": error, "stage": "fetch", "lease": lease, "run": run},
        job=job, machine=c.machine.describe(lease), timing=None,
    )
    assert receipt is not None
    assert receipt["cause"].get("install_unverified") == reason
    assert reason in receipt["message"]
    assert "\n" not in receipt["message"]
    assert any(word in receipt["message"].lower() for word in ("run", "retry", "inspect"))
    assert receipt["message"] in failure_receipt.render(receipt).splitlines()
