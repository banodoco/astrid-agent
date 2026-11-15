"""A completed job keeps its verified outputs when optional log collection fails."""

import hashlib
import json
import shlex
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

from astrid.core.execution.machine_run import run_on_machine
from astrid.packs.runpod import machine as rm
from tests.packs.runpod.test_generation_preparation import retained as retained  # noqa: F401
from tests.packs.runpod.test_machine import _claim_machine  # noqa: F401
from tests.packs.runpod.test_machine import machine_case as machine_case
from tests.packs.runpod.test_remote_placement import sources as sources  # noqa: F401
from tests.packs.runpod.test_target_selection import case as case  # noqa: F401
from tests.packs.runpod.test_warm_session import (  # noqa: F401
    lease_selection_case as lease_selection_case,
)
from tests.packs.runpod.test_warm_session import (
    small_lease as small_lease,
)


@pytest.fixture
def completed_case(machine_case, monkeypatch):
    c = machine_case
    original_start, original_get = c.pod.job_start, c.pod.get
    c.output_reads = []
    c.log_fault = None
    c.output_fault = None
    c.manifest_logs = None

    def start(command, **kwargs):
        result = original_start(command, **kwargs)
        if " generate " in command:
            line = next(row for row in command.splitlines() if "--placement local" in row)
            argv = shlex.split(line.split(" > ")[0])
            root = argv[argv.index("--job") + 1].rsplit("/", 1)[0]
            manifest_path = f"{root}/out/{rm.MANIFEST_NAME}"
            manifest = json.loads(c.pod.files[manifest_path])
            for name in ("engine.log", "stdout.log"):
                data = b"verified log\n"
                c.pod.files[f"{root}/out/logs/{name}"] = data
                manifest["files"][f"logs/{name}"] = {
                    "sha256": hashlib.sha256(data).hexdigest(), "size": len(data)}
            # A size-only mismatch must fail even when the bytes have the expected digest.
            if c.log_fault == "size":
                manifest["files"]["logs/engine.log"]["size"] += 1
            if c.output_fault == "size":
                manifest["files"]["artifacts/cpu.png"]["size"] += 1
            if c.manifest_logs is not None:
                manifest["files"] = {key: value for key, value in manifest["files"].items()
                                     if not key.startswith("logs/")}
                manifest["files"].update(c.manifest_logs)
            data = json.dumps(manifest).encode()
            c.pod.files[manifest_path] = data
            c.pod.files[result["dir"] + "/stdout"] = (
                f'{rm.RESULT_MARK}{{"status": "succeeded"}}\n'
                f"{rm.MANIFEST_MARK}{hashlib.sha256(data).hexdigest()}\n").encode()
        return result

    def get(remote, local):
        if "/out/" in remote:
            relative = remote.split("/out/", 1)[1]
            c.output_reads.append(relative)
            fault = (c.log_fault if relative == "logs/engine.log" else
                     c.output_fault if relative == "artifacts/cpu.png" else None)
            if fault == "timeout":
                Path(local).write_bytes(b"unverified partial transfer")
                raise TimeoutError("simulated transfer timeout")
            if fault == "digest":
                data = c.pod.files[remote]
                Path(local).write_bytes(b"x" * len(data))
                return
        return original_get(remote, local)

    monkeypatch.setattr(c.pod, "job_start", start)
    monkeypatch.setattr(c.pod, "get", get)
    _claim_machine(c)
    return c


@pytest.mark.parametrize("fault", ["timeout", "digest", "size", None])
def test_completed_job_log_fetch_budget(completed_case, fault):
    c = completed_case
    c.log_fault = fault
    job = c.job()
    destination = job.scratch / "outputs"
    fetched = run_on_machine(c.machine, job, destination=destination,
                             cancelled=lambda: False, sleep=lambda _: None)
    assert fetched["returncode"] == 0 and fetched["result"]["status"] == "succeeded"
    assert (destination / "artifacts/cpu.png").read_bytes() == b"png-bytes"
    assert "manifest.json" in fetched["files"]
    # Budget: one authenticated manifest, two outputs, two log attempts; no retry/failure fallback.
    assert c.output_reads == [rm.MANIFEST_NAME, "artifacts/cpu.png", "manifest.json",
                              "logs/engine.log", "logs/stdout.log"]
    assert (destination / "logs/stdout.log").read_bytes() == b"verified log\n"
    if fault:
        reason = "TimeoutError" if fault == "timeout" else "DigestMismatch"
        assert fetched["logs_unavailable"] == {"engine.log": reason}
        assert "logs/engine.log" not in fetched["files"]
        assert not (destination / "logs/engine.log").exists()
    else:
        assert not fetched.get("logs_unavailable")
        assert Path(fetched["files"]["logs/engine.log"]).read_bytes() == b"verified log\n"
    assert c.read()["lease"]["state"] == "idle"
    c.host.release()


@pytest.mark.parametrize("fault", ["timeout", "digest", "size"])
def test_completed_job_output_failure_still_fails(completed_case, fault):
    c = completed_case
    c.output_fault = fault
    job = c.job()
    error = TimeoutError if fault == "timeout" else rm.DigestMismatch
    with pytest.raises(error):
        run_on_machine(c.machine, job, destination=job.scratch / "outputs",
                       cancelled=lambda: False, sleep=lambda _: None)
    assert "logs/engine.log" not in c.output_reads
    c.host.release()


def test_host_records_successful_job_logs_unavailable(tmp_path, monkeypatch):
    from astrid.core.execution import generic_host, machine_run

    unavailable = {"logs/engine.log": "TimeoutError"}
    machine = SimpleNamespace()
    monkeypatch.setattr(machine_run, "resolve_machine", lambda _: SimpleNamespace(for_host=lambda *a, **k: machine))
    monkeypatch.setattr(machine_run, "run_on_machine", lambda *a, **k: {
        "files": {"artifacts/cpu.png": "verified.png"}, "returncode": 0,
        "logs_unavailable": unavailable})
    context = SimpleNamespace(get=lambda **k: None, begin=lambda *a: None, done=lambda *a: None)
    terminal = {}
    result = generic_host.GenericPackHost._run_machine_job_in_scope(
        object(), SimpleNamespace(id="vibecomfy.run"), {}, {"kind": "runpod"}, context, {},
        root=tmp_path, output_root=tmp_path / "outputs", cancelled=lambda: False,
        cancel_signal=None, task_id="task", attempt_id="attempt", fence=1, terminal=terminal)
    assert result.returncode == 0
    assert terminal["machine"]["logs_unavailable"] == unavailable


@pytest.mark.parametrize("cancel", [False, True])
def test_stalled_success_log_budget_aborts_transfer_and_releases(completed_case, monkeypatch, cancel):
    import multiprocessing
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
    from threading import Event, Thread

    from astrid.core.execution.machine_run import MachineRunCancelled
    from astrid.packs.runpod.remote_placement import _HttpsOps

    c = completed_case
    entered, stop, cancelled = Event(), Event(), Event()
    attempts, deadlines = [], []
    budget = 2.0
    monkeypatch.setattr(rm, "SUCCESS_LOG_SECONDS", budget, raising=False)

    class Stalled(BaseHTTPRequestHandler):
        def do_POST(self):
            entered.set()
            if cancel:
                cancelled.set()
            # This fake server never supplies the requested body until teardown.
            self.send_response(200)
            self.send_header("Content-Length", "1000")
            self.end_headers()
            stop.wait(10)

        def log_message(self, *_):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Stalled)
    Thread(target=server.serve_forever, daemon=True).start()
    first_log = []
    original_call = _HttpsOps._call_direct

    def call(ops, name, *args, **kwargs):
        if name == "get" and "/out/logs/" in args[0]:
            first_log.append(time.monotonic())
            attempts.append(args[0].rsplit("/", 1)[1])
            deadlines.append(ops.deadline)
            # The in-memory fixture's pod-1 is not a valid lifecycle proxy ID.
            ops.pod_id, ops.token = "pod1", "x" * 32
            ops.base_url = f"http://127.0.0.1:{server.server_port}"
            return c.real_bounded_call(ops, name, *args, **kwargs)
        return original_call(ops, name, *args, **kwargs)

    monkeypatch.setattr(_HttpsOps, "_bounded_call", call)
    job = c.job()
    destination = job.scratch / "outputs"
    children = {child.pid for child in multiprocessing.active_children()}
    try:
        if cancel:
            with pytest.raises(MachineRunCancelled):
                run_on_machine(c.machine, job, destination=destination,
                               cancelled=cancelled.is_set, sleep=lambda _: None)
        else:
            fetched = run_on_machine(c.machine, job, destination=destination,
                                     cancelled=cancelled.is_set, sleep=lambda _: None)
            assert fetched["returncode"] == 0
            assert fetched["logs_unavailable"] == {"engine.log": "deadline", "stdout.log": "deadline"}
            assert c.read()["lease"]["state"] == "idle"
            assert c.pod.resets[-1]["ok"]
        assert entered.is_set() and attempts == ["engine.log"]
        assert deadlines[0] is not None
        assert time.monotonic() - first_log[0] < budget + 1.5
        assert {child.pid for child in multiprocessing.active_children()} == children
        assert (destination / "artifacts/cpu.png").read_bytes() == b"png-bytes"
        assert not list(destination.glob("logs/*"))
        assert not list(job.scratch.rglob("*.https-part"))
        assert not list(job.scratch.rglob("engine.log"))
        assert [kind for kind, _ in c.active] == ["begin", "done"]
    finally:
        stop.set()
        server.shutdown()
        server.server_close()
        c.host.release()


def test_success_logs_share_one_deadline(completed_case, monkeypatch):
    from astrid.packs.runpod.remote_placement import _HttpsOps

    c = completed_case
    monkeypatch.setattr(rm, "SUCCESS_LOG_SECONDS", 0.05, raising=False)
    original_call = _HttpsOps._call_direct
    attempts = []

    def call(ops, name, *args, **kwargs):
        if name == "get" and "/out/logs/" in args[0]:
            attempts.append(args[0].rsplit("/", 1)[1])
            Path(args[1]).write_bytes(b"partial")
            assert ops.deadline is not None
            time.sleep(max(0, ops.deadline - time.monotonic()))
            raise TimeoutError("deadline")
        return original_call(ops, name, *args, **kwargs)

    monkeypatch.setattr(_HttpsOps, "_bounded_call", call)
    job = c.job()
    fetched = run_on_machine(c.machine, job, destination=job.scratch / "outputs",
                             cancelled=lambda: False, sleep=lambda _: None)
    assert attempts == ["engine.log"]
    assert fetched["logs_unavailable"] == {"engine.log": "deadline", "stdout.log": "deadline"}
    assert not list(job.scratch.rglob("engine.log"))
    assert c.read()["lease"]["state"] == "idle"
    c.host.release()


@pytest.mark.parametrize("relative, size, reason", [
    ("logs/engine.log", 256 * 1024 * 1024 + 1, "too_large"),
    ("/home/alice/x/logs/engine.log", 1, "DigestMismatch"),
    ("logs/../engine.log", 1, "DigestMismatch"),
    ("logs/nested/engine.log", 256 * 1024 * 1024 + 1, "too_large"),
])
def test_success_log_manifest_refusals_have_relative_keys(completed_case, relative, size, reason):
    c = completed_case
    c.manifest_logs = {relative: {"sha256": "0" * 64, "size": size}}
    job = c.job()
    fetched = run_on_machine(c.machine, job, destination=job.scratch / "outputs",
                             cancelled=lambda: False, sleep=lambda _: None)
    key = "nested/engine.log" if "nested/" in relative else "engine.log"
    assert fetched["logs_unavailable"] == {key: reason}
    assert relative not in c.output_reads
    assert fetched["returncode"] == 0
    assert c.read()["lease"]["state"] == "idle"
    c.host.release()


def test_success_logs_refuse_symlink_destination(completed_case, tmp_path):
    c = completed_case
    job = c.job()
    destination = job.scratch / "outputs"
    destination.mkdir()
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    (destination / "logs").symlink_to(elsewhere, target_is_directory=True)
    fetched = run_on_machine(c.machine, job, destination=destination,
                             cancelled=lambda: False, sleep=lambda _: None)
    assert fetched["logs_unavailable"] == {
        "engine.log": "unsafe_destination", "stdout.log": "unsafe_destination"}
    assert "logs/engine.log" not in c.output_reads and not list(elsewhere.iterdir())
    assert fetched["returncode"] == 0
    c.host.release()


def test_success_logs_refuse_nested_symlink_destination(completed_case, tmp_path, monkeypatch):
    c = completed_case
    data = b"verified log\n"
    c.manifest_logs = {"logs/a/engine.log": {
        "sha256": hashlib.sha256(data).hexdigest(), "size": len(data)}}
    original_get = c.pod.get

    def get(remote, local):
        if remote.endswith("/out/logs/a/engine.log"):
            c.pod.files[remote] = data
        return original_get(remote, local)

    monkeypatch.setattr(c.pod, "get", get)
    job = c.job()
    destination = job.scratch / "outputs"
    (destination / "logs").mkdir(parents=True)
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    (destination / "logs/a").symlink_to(elsewhere, target_is_directory=True)
    fetched = run_on_machine(c.machine, job, destination=destination,
                             cancelled=lambda: False, sleep=lambda _: None)
    assert fetched.get("logs_unavailable") == {"a/engine.log": "unsafe_destination"}
    assert "logs/a/engine.log" not in fetched["files"]
    assert not list(elsewhere.iterdir())
    assert fetched["returncode"] == 0
    c.host.release()


@pytest.mark.parametrize("entry", [
    {}, {"size": -1}, {"size": "12"}, {"size": 1.5}, {"size": True},
], ids=["missing", "negative", "string", "float", "bool"])
def test_success_logs_invalid_size_request_budget(completed_case, entry):
    c = completed_case
    c.manifest_logs = {"logs/engine.log": {"sha256": "0" * 64, **entry}}
    job = c.job()
    fetched = run_on_machine(c.machine, job, destination=job.scratch / "outputs",
                             cancelled=lambda: False, sleep=lambda _: None)
    # Budget: malformed log sizes cause zero log requests; required outputs still arrive.
    assert c.output_reads == [rm.MANIFEST_NAME, "artifacts/cpu.png", "manifest.json"]
    assert fetched["logs_unavailable"] == {"engine.log": "invalid_size"}
    assert fetched["returncode"] == 0
    assert c.read()["lease"]["state"] == "idle"
    c.host.release()


@pytest.mark.parametrize("paid, job, transport, expected", [
    (300, 300, None, 1060),
    (20, 300, None, 1005),
    (300, 22, None, 1007),
    (300, 300, 1018, 1003),
    (10, 300, None, 995),
])
def test_success_log_deadline_reserves_cleanup_time(monkeypatch, paid, job, transport, expected):
    from threading import RLock

    from astrid.packs.runpod.remote_placement import _HttpsOps

    monkeypatch.setattr(rm.time, "monotonic", lambda: 1000)
    original = _HttpsOps("fake-pod", "fake-only", deadline=transport)
    owner = SimpleNamespace(lock=RLock(), clock=lambda: 100,
                            preparation_claim=lambda _: {"lease": {"paid_expires_at": 100 + paid}})
    placement = SimpleNamespace(_clock=lambda: 200, _deadline_at=200 + job)
    machine = rm.RunPodMachine(SimpleNamespace(lease_owner=owner, placement=placement))
    def cancelled():
        return False

    run = SimpleNamespace(lease=SimpleNamespace(claim=SimpleNamespace(ops=original), lease=object()),
                          cancelled=cancelled)
    with machine._success_log_transport(run) as ops:
        assert ops.deadline == expected
        assert ops.cancelled is cancelled
    assert run.lease.claim.ops is original


@pytest.mark.parametrize("job, expected", [(20, 1005), (300, 1060)])
def test_success_log_deadline_without_lease_owner(monkeypatch, job, expected):
    from astrid.packs.runpod.remote_placement import _HttpsOps

    monkeypatch.setattr(rm.time, "monotonic", lambda: 1000)
    original = _HttpsOps("fake-pod", "fake-only")
    placement = SimpleNamespace(_clock=lambda: 200, _deadline_at=200 + job)
    machine = rm.RunPodMachine(SimpleNamespace(placement=placement))
    run = SimpleNamespace(lease=SimpleNamespace(claim=SimpleNamespace(ops=original)), cancelled=None)
    with machine._success_log_transport(run) as ops:
        assert ops.deadline == expected
    assert run.lease.claim.ops is original


def test_nested_log_named_output_failure_still_fails(completed_case, monkeypatch):
    c = completed_case
    c.manifest_logs = {"artifacts/logs/engine.log": {"sha256": "0" * 64, "size": 1}}
    original_get = c.pod.get

    def get(remote, local):
        if remote.endswith("/artifacts/logs/engine.log"):
            raise TimeoutError("required output transfer failed")
        return original_get(remote, local)

    monkeypatch.setattr(c.pod, "get", get)
    job = c.job()
    with pytest.raises(TimeoutError):
        run_on_machine(c.machine, job, destination=job.scratch / "outputs",
                       cancelled=lambda: False, sleep=lambda _: None)
    c.host.release()
