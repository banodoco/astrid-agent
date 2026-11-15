"""A completed job keeps its verified outputs when optional log collection fails."""

import hashlib
import json
import shlex
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
        assert fetched["logs_unavailable"] == {"logs/engine.log": reason}
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
