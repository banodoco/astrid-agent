"""G3 slices 1-2: phase timing on every machine job, and one local failure receipt per failed job.

The pod and provider are the fakes of ``test_machine.py``; the real machine, lease owner,
``run_on_machine`` and host job function run. No log or file ever comes back from the pod.
"""
from __future__ import annotations

import json
from dataclasses import replace
from types import SimpleNamespace

import pytest

from astrid.core._shared import run_logs
from astrid.core.execution import phase_timing
from astrid.core.execution.machine_run import run_on_machine
from astrid.core.execution.phase_timing import PhaseTiming
from astrid.packs.runpod import failure_receipt
from astrid.packs.runpod import machine as rm
from tests.packs.runpod.test_generation_preparation import retained as retained  # noqa: F401  (fixture)
from tests.packs.runpod.test_machine import _claim_machine as _claim_machine, machine_case as machine_case  # noqa: F401
from tests.packs.runpod.test_remote_placement import sources as sources  # noqa: F401  (fixture)
from tests.packs.runpod.test_target_selection import case as case  # noqa: F401  (fixture)
from tests.packs.runpod.test_warm_session import (  # noqa: F401  (fixtures)
    lease_selection_case as lease_selection_case, small_lease as small_lease,
)

FRAME = "astrid-generation-failure: "


@pytest.fixture
def receipts(tmp_path):
    return tmp_path / "failures"


def _run(c, receipts, *, job=None, timing=None, claim_first=True):
    """Run one job the way the host does: timing in the job, the recorder as on_failure."""
    job = job or c.job()
    timing = timing or PhaseTiming()
    job = replace(job, timing=timing)
    # A generation job only uses a machine the owner has already claimed.
    if claim_first and not c.owner.path.exists():
        c.host.start_fresh_claim()
        c.host.claim(replace(c.grant, invocation_id="manual-claim", preparation_seconds=0,
                             execution_seconds=0), requested_idle_seconds=c.grant.idle_seconds)
    machine = {}
    failed = failure_receipt.recorder(job, lambda: machine or None, timing, root=receipts)
    try:
        run_on_machine(c.machine, job, destination=job.scratch / "outputs", cancelled=lambda: False,
                       sleep=lambda s: None, on_claim=lambda lease: machine.update(c.machine.describe(lease)),
                       on_failure=failed)
    except BaseException as exc:  # noqa: BLE001
        return exc, timing, machine
    finally:
        phase_timing.attach(machine or None, timing)  # as the host does, success or failure
    return None, timing, machine


def _only(receipts):
    path = failure_receipt.find_receipt(None, root=receipts)
    assert path is not None, "no receipt was written"
    assert len(list(receipts.glob("*/failure-receipt.json"))) == 1
    return json.loads(path.read_text(encoding="utf-8"))


def test_success_carries_the_timing_block_and_writes_no_receipt(machine_case, receipts):
    c = machine_case
    error, timing, machine = _run(c, receipts)
    assert error is None and not receipts.exists()
    block = machine["timing"]
    assert block["schema"] == "astrid.generation.phase-timing.v1"
    stamps = block["stamps"]
    assert stamps.get("capacity_wait_started_at") is None
    assert (stamps["claimed_at"] <= stamps["preparation_ended_at"]
            <= stamps["job_started_at"] <= stamps["job_ended_at"] <= stamps["collection_ended_at"]
            <= stamps["released_at"])
    assert stamps["collection_started_at"] >= stamps["job_ended_at"] and stamps["release_started_at"] <= stamps["released_at"]
    phases = block["phases"]
    assert phases["capacity_wait"] is None
    assert all(phases[name]["seconds"] is not None for name in
               ("preparation", "execution", "collection", "release", "paid_clock"))
    assert phases["idle"] is None
    assert block["pod_phases"] and all(isinstance(row["at"], float) for row in block["pod_phases"])
    json.dumps(block)


def test_remote_steps_carry_their_own_time(machine_case, receipts):
    c = machine_case
    c.host.start_fresh_claim()
    c.host.claim(replace(c.grant, invocation_id="manual-claim", preparation_seconds=0,
                         execution_seconds=0), requested_idle_seconds=c.grant.idle_seconds)
    lease = c.machine.claim(c.job())
    try:
        assert lease.state.steps and all(isinstance(step["at"], float) for step in lease.state.steps)
    finally:
        c.machine.release(lease, False)


def test_execution_failure_names_execution_and_carries_timing(machine_case, receipts):
    c = machine_case
    c.pod.fail_jobs = {" generate cpu-image "}
    error, timing, machine = _run(c, receipts)
    assert isinstance(error, rm.MachineError)
    receipt = _only(receipts)
    assert receipt["schema"] == "astrid.generation.failure-receipt.v1" and receipt["outcome"] == "failed"
    assert receipt["stage"] == "execution" and receipt["phase"] == "fetch"
    assert receipt["cause"]["exit_code"] == 1 and receipt["cause"]["exception"] == "MachineError"
    assert receipt["identity"]["task_id"] == "task" and receipt["identity"]["machine_id"] == "pod-1"
    assert receipt["identity"]["run_id"] and receipt["identity"]["remote_job_id"]
    assert receipt["machine"]["after"] == "kept" and receipt["machine"]["release_at"] is not None
    assert receipt["collection"] == {"state": "unavailable", "reason": "not_reported",
                                     "bytes": 0, "cap_bytes": run_logs.CAP_BYTES}
    assert receipt["files"] == []
    assert receipt["timing"]["stamps"]["job_ended_at"] and receipt["timing"]["phases"]["execution"]["seconds"] is not None
    assert machine["timing"] == receipt["timing"], "the attempt's machine record carries the same block"
    assert "boom" not in json.dumps(receipt), "pod stderr must never reach the receipt"


def test_collected_issue_reaches_receipt_and_failure_line(machine_case, receipts, tmp_path, monkeypatch, capsys):
    from astrid.core.generation import cli

    c = machine_case
    c.pod.fail_jobs = {" generate cpu-image "}
    log = tmp_path / "generation.stdout.json"
    reason = "Cannot find the requested asset: private/path"
    log.write_text(json.dumps({"issues": [{"message": reason}]}))
    monkeypatch.setattr(c.machine, "_fetch_failure_logs", lambda *args: {"files": [
        {"name": "generation.stdout", "path": str(log), "bytes": log.stat().st_size}]})
    error, _, _ = _run(c, receipts)
    assert isinstance(error, rm.MachineError)
    receipt = _only(receipts)
    assert receipt["cause"]["issue_message"] == reason
    identity = receipt["identity"]
    monkeypatch.setattr(failure_receipt, "failures_root", lambda: receipts)
    # The pack's lease listing carries the receipt's reason and log folder; the CLI only prints them.
    monkeypatch.setattr(rm, "journal_leases", lambda: rm.with_failure_details([{
        "id": identity["machine_id"], "lease": identity["lease"], "state": "idle", "live": True,
        "release_at": 1800000600.0, "cost_usd": 0.25,
        "last_job": {"generation": identity["generation"], "outcome": "failed", "failure": {}},
    }]))
    cli._kept_failure(set())
    output = capsys.readouterr().err
    assert f"Reason: {reason}" in output
    assert f"logs: {receipts / 'task' / 'logs'}" in output


def test_pod_validation_exit_names_workflow_validation(machine_case, receipts):
    c = machine_case
    c.pod.listed = []  # the job's entry check exits 3
    error, _timing, _machine = _run(c, receipts)
    assert error is not None
    receipt = _only(receipts)
    assert receipt["stage"] == "workflow_validation" and receipt["stage_basis"] == "pod_exit"
    assert receipt["cause"]["exit_code"] == 3


def test_input_rejected_diagnostic_keeps_only_allowlisted_facts(machine_case, receipts):
    c = machine_case
    frame = {"code": "input_rejected", "category": "input", "node_id": "1", "node_class": "TextToSpeech",
             "field": "speaker", "constraint": "enum"}
    original = c.pod.exec

    def with_frame(command, timeout=600):
        if command.startswith("tail -c") and command.endswith("/stdout"):
            return 0, FRAME + json.dumps(frame) + "\n", ""
        return original(command, timeout)

    c.pod.fail_jobs = {" generate cpu-image "}
    c.pod.exec = with_frame
    _run(c, receipts)
    receipt = _only(receipts)
    assert receipt["stage"] == "workflow_validation" and receipt["stage_basis"] == "diagnostic"
    assert receipt["cause"]["code"] == "input_rejected"
    assert receipt["cause"]["node_class"] == "TextToSpeech" and receipt["cause"]["constraint"] == "enum"
    assert "field" not in receipt["cause"], "'speaker' is not allowlisted and must stay dropped"
    assert "speaker" not in json.dumps(receipt)


def test_tampered_output_names_output_collection_and_unsafe_release(machine_case, receipts):
    c = machine_case
    c.pod.corrupt_get = "cpu.png"
    error, _timing, _machine = _run(c, receipts)
    assert isinstance(error, rm.DigestMismatch)
    receipt = _only(receipts)
    assert receipt["stage"] == "output_collection" and receipt["stage_basis"] == "controller_step"
    assert receipt["machine"] == {"after": "released", "release_reason": "output_digest_mismatch",
                                  "release_at": receipt["timing"]["stamps"]["released_at"], "cost_usd": None}
    assert c.deletes == ["pod-1"]


def test_pinned_missing_machine_names_no_machine(machine_case, receipts):
    c = machine_case
    job = c.job()
    job = replace(job, policy={**job.policy, "machine": "pod-missing"})
    error, timing, machine = _run(c, receipts, job=job, claim_first=False)
    assert error is not None and not machine
    receipt = _only(receipts)
    assert receipt["stage"] == "no_machine" and receipt["phase"] == "claim"
    assert receipt["collection"]["state"] == "local_only" and receipt["collection"]["reason"] is None
    assert receipt["machine"]["after"] == "none"
    assert receipt["identity"]["machine_id"] is None and receipt["timing"]["phases"]["preparation"] is None
    assert c.creates == 0


def test_no_machine_refusal_names_the_fixed_reason(machine_case, receipts, monkeypatch):
    from astrid.packs.runpod.target_selection import PaidSelectionError

    c = machine_case
    def forbidden(*args, **kwargs):
        pytest.fail("no-machine job observed offer or capacity")
    c.owner.observe_offer = forbidden
    c.owner.observe_offers = forbidden
    error, _timing, _machine = _run(c, receipts, claim_first=False)
    assert isinstance(error, PaidSelectionError)
    assert str(error) == "no machine: run `astrid machines claim`"
    receipt = _only(receipts)
    assert receipt["stage"] == "no_machine"
    assert receipt["machine"]["after"] == "none"
    assert c.creates == c.configs == 0
    assert receipt["timing"]["stamps"].get("capacity_wait_started_at") is None


def test_preparation_install_failure_names_install(machine_case, receipts):
    c = machine_case
    c.pod.fail_jobs = {"engines install"}
    error, _timing, _machine = _run(c, receipts)
    assert error is not None
    receipt = _only(receipts)
    assert receipt["stage"] == "install" and receipt["phase"] == "claim"
    assert receipt["identity"]["machine_id"] == "pod-1"
    assert receipt["machine"]["after"] == "kept"
    assert receipt["timing"]["stamps"]["claimed_at"] and receipt["timing"]["phases"]["preparation"]["end"] is None


def test_preparation_failure_before_any_job_names_boot(machine_case, receipts, monkeypatch):
    c = machine_case
    monkeypatch.setattr(c.placement, "begin_execution",
                        lambda **kw: (_ for _ in ()).throw(RuntimeError("private text")))
    error, _timing, _machine = _run(c, receipts)
    assert isinstance(error, RuntimeError)
    receipt = _only(receipts)
    assert receipt["stage"] in {"boot", "install"} and "private text" not in json.dumps(receipt)


def test_release_failure_after_a_good_job_names_release(machine_case, receipts, monkeypatch):
    c = machine_case

    def broken(self, *args, **kwargs):
        raise rm.MachineError("settlement text")

    monkeypatch.setattr(rm.RunPodMachine if hasattr(rm, "RunPodMachine") else type(c.machine), "_release_impl", broken)
    error, _timing, _machine = _run(c, receipts)
    assert isinstance(error, rm.MachineError)
    receipt = _only(receipts)
    assert receipt["stage"] == "release" and receipt["stage_basis"] == "controller_step"
    assert "settlement text" not in json.dumps(receipt)


def test_cancelled_runs_write_no_receipt(machine_case, receipts):
    from astrid.core.execution.machine_run import MachineRunCancelled

    c = machine_case
    _claim_machine(c)
    job = replace(c.job(), timing=PhaseTiming())
    failed = failure_receipt.recorder(job, lambda: None, job.timing, root=receipts)
    claimed = []
    with pytest.raises(MachineRunCancelled):
        run_on_machine(c.machine, job, destination=job.scratch / "outputs", cancelled=lambda: bool(claimed),
                       sleep=lambda s: None, on_claim=claimed.append, on_failure=failed)
    assert not receipts.exists()


def test_a_receipt_write_problem_never_masks_the_job_error(machine_case, tmp_path, capsys):
    c = machine_case
    blocked = tmp_path / "blocked"
    blocked.write_text("a file where the folder should be")
    c.pod.listed = []
    error, _timing, _machine = _run(c, blocked)
    assert isinstance(error, rm.MachineError) and "remote generation failed" in str(error)
    assert "failure receipt not written" in capsys.readouterr().err


def test_only_the_newest_twenty_receipts_are_kept(tmp_path):
    import os
    for number in range(23):
        body = {"identity": {"task_id": f"t{number}"}}
        path = failure_receipt.write_receipt(body, root=tmp_path)
        os.utime(path, (1000 + number, 1000 + number))
        os.utime(path.parent, (1000 + number, 1000 + number))
    names = sorted(p.parent.name for p in tmp_path.glob("*/failure-receipt.json"))
    assert len(names) == 20 and "t22" in names and "t0" not in names


@pytest.mark.parametrize(("kwargs", "stage", "basis"), [
    (dict(phase="claim"), "no_machine", "controller_step"),
    (dict(phase="claim", claim_context={"acquired": True}), "boot", "controller_step"),
    (dict(phase="claim", claim_context={"acquired": True, "preparation_job": True}), "install", "controller_step"),
    (dict(phase="fetch", digest_mismatch=True), "output_collection", "controller_step"),
    (dict(phase="fetch"), "output_collection", "controller_step"),
    (dict(phase="fetch", exit_code=75), "boot", "pod_exit"),
    (dict(phase="fetch", exit_code=3, category="input"), "workflow_validation", "pod_exit"),
    (dict(phase="fetch", exit_code=1, category="model"), "model_download", "diagnostic"),
    (dict(phase="fetch", exit_code=1, category="health"), "install", "diagnostic"),
    (dict(phase="fetch", exit_code=1, category="execution", progress_phase="align"), "model_download", "progress"),
    (dict(phase="fetch", exit_code=1, category="execution", progress_phase="start_engine"), "install", "progress"),
    (dict(phase="fetch", exit_code=1, category="execution", progress_phase="run"), "execution", "controller_step"),
    (dict(phase="poll", exit_code=None), "execution", "controller_step"),
    (dict(phase="release"), "release", "controller_step"),
])
def test_stage_decision_rules(kwargs, stage, basis):
    assert failure_receipt.decide_stage(**kwargs) == (stage, basis)
    assert stage in failure_receipt.STAGES


@pytest.mark.parametrize(("marker", "error_type"), [
    ("credential", "PaidSelectionError"), ("volume", "AcquisitionUnresolvedError"),
    ("machine", "AcquisitionUnresolvedError"),
])
def test_claim_cause_marker_is_projected_with_identity_gate(marker, error_type):
    from astrid.packs.runpod.failure_diagnostics import EXCEPTIONS
    from astrid.packs.runpod.target_selection import PaidSelectionError
    from astrid.packs.runpod.worker_preparation import AcquisitionUnresolvedError, observation_failure_gate

    error = (PaidSelectionError("private") if error_type == "PaidSelectionError"
             else AcquisitionUnresolvedError("private"))
    error.cause = marker
    receipt = failure_receipt.build_receipt({"failure": error, "stage": "claim"},
        job=SimpleNamespace(task_id="t"), machine=None, timing=None)
    assert receipt["cause"]["cause"] == marker and receipt["stage"] == "no_machine"
    assert error.gate == "identity" and observation_failure_gate(error) == "identity"
    assert receipt["cause"]["exception"] == error_type and error_type in EXCEPTIONS
    assert "private" not in json.dumps(receipt)


@pytest.mark.parametrize("name", ["PaidSelectionError", "RunPodSetupRequiredError", "OfferObservationError",
                                  "CapacityExhaustedError", "CapacityWaitTimeout"])
def test_claim_exception_names_are_allowlisted(name):
    from astrid.packs.runpod import target_selection
    from astrid.packs.runpod.worker_preparation import CapacityExhaustedError

    types = {"PaidSelectionError": target_selection.PaidSelectionError,
             "RunPodSetupRequiredError": target_selection.RunPodSetupRequiredError,
             "OfferObservationError": target_selection.OfferObservationError,
             "CapacityExhaustedError": CapacityExhaustedError,
             "CapacityWaitTimeout": target_selection.CapacityWaitTimeout}
    error = types[name]("secret")
    receipt = failure_receipt.build_receipt({"failure": error, "stage": "claim"},
        job=SimpleNamespace(task_id="t"), machine=None, timing=None)
    assert receipt["cause"]["exception"] == name


def test_failure_callback_exception_is_logged_by_class_and_never_its_text(capsys):
    from astrid.core.execution.machine_run import _report_failure

    def broken(_facts):
        raise RuntimeError("private secret https://private.example")

    _report_failure(broken, ValueError("job error"), "claim", None, None)
    stderr = capsys.readouterr().err
    assert stderr == "failure receipt not written (RuntimeError)\n"


def test_host_job_function_puts_timing_on_terminal_machine_and_writes_the_receipt(machine_case, receipts,
                                                                                  monkeypatch):
    """The real host method, with the real machine: success and failure both carry ``terminal['machine']['timing']``."""
    from astrid.core.execution import generic_host, machine_run

    c = machine_case
    _claim_machine(c)
    monkeypatch.setattr(machine_run, "resolve_machine", lambda kind: SimpleNamespace(for_host=lambda *a, **k: c.machine))
    monkeypatch.setattr(failure_receipt, "failures_root", lambda: receipts)
    context = SimpleNamespace(get=lambda **k: c.adapter, begin=lambda *a: None, done=lambda *a: None)

    def host_job(job, terminal):
        return generic_host.GenericPackHost._run_machine_job(
            None, SimpleNamespace(id="vibecomfy.run"), {}, job.policy, context, dict(job.inputs),
            root=job.scratch, output_root=job.scratch / "outputs", cancelled=lambda: False,
            cancel_signal=None, task_id="task", attempt_id=job.attempt_id, fence=1, terminal=terminal)

    good = c.job(1)
    ok = {}
    monkeypatch.setattr(generic_host, "_task_generation_intent",
                        lambda task: {"metadata": {"model": "cpu-image", "mode": "t2i"}})
    monkeypatch.setenv("ASTRID_MACHINE_POLL_SECONDS", "0")
    host_job(good, ok)
    assert ok["machine"]["timing"]["phases"]["execution"]["seconds"] is not None and ok["machine"]["files"]
    assert not receipts.exists()

    c.pod.fail_jobs = {" generate cpu-image "}
    bad = {}
    with pytest.raises(rm.MachineError):
        host_job(c.job(2), bad)
    receipt = _only(receipts)
    assert receipt["identity"]["attempt_id"] == "attempt-2" and receipt["stage"] == "execution"
    assert bad["machine"]["timing"] == receipt["timing"]


def test_generate_failure_prints_the_newest_or_named_receipt(machine_case, tmp_path, monkeypatch, capsys):
    from astrid.core.generation.cli import main as generate

    monkeypatch.setenv("ASTRID_RUNTIME_DATA_ROOT", str(tmp_path))
    root = tmp_path / "generation" / "failures"
    c = machine_case
    c.pod.fail_jobs = {" generate cpu-image "}
    _run(c, root)
    on_disk = _only(root)
    capsys.readouterr()
    assert generate(["failure", "--json"]) == 0
    assert json.loads(capsys.readouterr().out) == on_disk
    assert generate(["failure", "task"]) == 0
    human = capsys.readouterr().out
    assert human.startswith("Failure: execution (controller_step)\n") and "Task: task  attempt: attempt-1" in human
    assert on_disk["collection"]["state"] == "unavailable" and "Logs: unavailable" in human
    assert generate(["failure", "no-such-task"]) == 1
    assert "no failure receipt for task no-such-task" in capsys.readouterr().err


def test_a_non_runpod_machine_failure_writes_no_runpod_receipt_and_no_timing(tmp_path, monkeypatch):
    from astrid.core.execution import generic_host, machine_run

    seen = {}

    class Other:
        def claim(self, job, cancelled=None):
            seen["timing"] = job.timing
            raise RuntimeError("no capacity on the other provider")

        def release(self, lease, success):  # pragma: no cover - claim fails first
            raise AssertionError("nothing was claimed")

    monkeypatch.setattr(machine_run, "resolve_machine", lambda kind: SimpleNamespace(for_host=lambda *a, **k: Other()))
    monkeypatch.setattr(failure_receipt, "failures_root", lambda: tmp_path / "failures")
    monkeypatch.setattr(generic_host, "_task_generation_intent", lambda task: {"metadata": {"model": "cpu-image"}})
    context = SimpleNamespace(get=lambda **k: None, begin=lambda *a: None, done=lambda *a: None)
    terminal = {}
    with pytest.raises(RuntimeError, match="no capacity"):
        generic_host.GenericPackHost._run_machine_job(
            None, SimpleNamespace(id="vibecomfy.run"), {}, {"kind": "other"}, context, {},
            root=tmp_path, output_root=tmp_path / "out", cancelled=lambda: False, cancel_signal=None,
            task_id="task", attempt_id="a1", fence=1, terminal=terminal)
    assert seen["timing"] is None and terminal == {}
    assert not (tmp_path / "failures").exists()


def test_provider_gate_in_acquired_claim_receipt_is_fixed_and_private():
    from astrid.packs.runpod.worker_preparation import AcquisitionUnresolvedError

    error = AcquisitionUnresolvedError("private provider body", gate="volume")
    error.claim_context = {"acquired": True}
    receipt = failure_receipt.build_receipt({"failure": error, "stage": "claim"},
        job=SimpleNamespace(task_id="t"), machine=None, timing=None)
    assert receipt["stage"] == "boot"
    assert receipt["cause"]["gate"] == "volume"
    assert "private provider body" not in json.dumps(receipt)


def test_failure_command_logs_prints_stderr_first(tmp_path, monkeypatch, capsys):
    from astrid.core.generation import cli

    stderr = tmp_path / "stderr"
    stdout = tmp_path / "stdout"
    stderr.write_text("Traceback ... RuntimeError: CUDA out of memory\n")
    stdout.write_text("job output\n")
    receipt_path = tmp_path / "failure-receipt.json"
    receipt_path.write_text(json.dumps({"logs": [
        {"name": "stdout", "path": str(stdout)},
        {"name": "stderr", "path": str(stderr)},
    ]}))
    monkeypatch.setattr(failure_receipt, "find_receipt", lambda task_id: receipt_path)
    parsed = cli._parser().parse_args(["failure", "--logs"])
    assert cli._failure_command(parsed) == 0
    output = capsys.readouterr().out
    assert "Traceback ... RuntimeError: CUDA out of memory" in output
    assert output.index("Traceback") < output.index("job output")


def test_render_shows_collected_log_path():
    receipt = {"logs": [{"name": "stderr", "path": "/tmp/example/stderr"}]}
    assert "/tmp/example/stderr" in failure_receipt.render(receipt)
