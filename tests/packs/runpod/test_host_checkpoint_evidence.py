"""Offline custody and diagnostics across a failed job and the next job."""
from __future__ import annotations

import hashlib
import json
import shlex
import time
from dataclasses import replace
from pathlib import Path

import pytest

from astrid.core.generation import entries
from astrid.packs.runpod import failure_receipt, machine as rm
from astrid.packs.runpod import remote_placement as rp
from tests.packs.runpod.test_failure_receipt import _only, _run
from tests.packs.runpod.test_generation_preparation import retained as retained  # noqa: F401
from tests.packs.runpod.test_machine import _claim_machine, machine_case as machine_case  # noqa: F401
from tests.packs.runpod.test_remote_placement import sources as sources  # noqa: F401
from tests.packs.runpod.test_target_selection import case as case  # noqa: F401
from tests.packs.runpod.test_warm_session import (  # noqa: F401
    lease_selection_case as lease_selection_case,
    small_lease as small_lease,
)


def _digest(data):
    return hashlib.sha256(data).hexdigest()


@pytest.fixture
def evidence_case(machine_case, monkeypatch):
    c = machine_case
    c.context.deadline = time.monotonic() + 600
    original_entries = entries.user_entries
    template = next(row for row in original_entries() if row["id"] == "cpu-image")
    monkeypatch.setattr(entries, "user_entries", lambda: [*original_entries(),
                        {**template, "id": "cpu-music", "kind": "audio", "modes": ["music"]},
                        {**template, "id": "cpu-edit", "modes": ["edit"]}])
    c.pod.listed.extend(["cpu-music", "cpu-edit"])
    original_start = c.pod.job_start
    c.log_reads = []
    c.generated = []
    c.generation_jobs = []

    def start(command, **kwargs):
        result = original_start(command, **kwargs)
        if " generate " not in command:
            return result
        line = next(row for row in command.splitlines() if "--placement local" in row)
        argv = shlex.split(line.split(" > ")[0])
        root = argv[argv.index("--job") + 1].rsplit("/", 1)[0]
        c.generated.append(argv[2])
        c.generation_jobs.append(result["id"])
        logs = {"engine.log": b"engine observation\n", "install.log": b"install observation\n",
                "job.stdout": b"job output\n", "job.stderr": b"job diagnostic\n"}
        rows = [{"name": name, "role": role, "file": name, "bytes": len(logs[name]),
                 "missing": False, "truncated": False}
                for name, role in (("engine.log", "engine"), ("install.log", "install"),
                                   ("job.stdout", "stdout"), ("job.stderr", "stderr"))]
        logs["index.json"] = json.dumps({"schema": "run-logs.collected.v1", "logs": rows}).encode()
        manifest = json.dumps({"files": {name: {"sha256": _digest(data), "size": len(data)}
                                         for name, data in logs.items()}}).encode()
        c.pod.files.update({f"{root}/out/logs/{name}": data for name, data in logs.items()})
        c.pod.files[f"{root}/out/logs/{rp.MANIFEST_NAME}"] = manifest
        stdout = result["dir"] + "/stdout"
        c.pod.files[stdout] += f"{rm.LOGS_MANIFEST_MARK}{_digest(manifest)}\n".encode()
        return result

    original_get = c.pod.get

    def get(remote, local):
        if "/out/logs/" in remote:
            assert not c.host.lock._is_owned(), "log transfer held owner.lock"
            c.log_reads.append((remote, c.transport_deadlines[-1], time.monotonic(),
                                c.read()["lease"]["paid_expires_at"] - c.clock()))
        return original_get(remote, local)

    original_exec = c.pod.exec

    def execute(command, timeout=600):
        if command.startswith("grep -a ") and command.endswith("/stdout | tail -n 1"):
            path = shlex.split(command)[3]
            lines = c.pod.files.get(path, b"").decode().splitlines()
            return 0, "\n".join(line for line in lines if line.startswith(rm.LOGS_MANIFEST_MARK)), ""
        return original_exec(command, timeout)

    monkeypatch.setattr(c.pod, "job_start", start)
    monkeypatch.setattr(c.pod, "get", get)
    monkeypatch.setattr(c.pod, "exec", execute)
    original_logs = c.machine._fetch_failure_logs

    def collect_logs(ops, run, stdout, destination):
        if run.lease.failure_exit_code is None:
            assert ops.abort_event is not None, "host log collection must be cancellable"
            assert not c.host.lock._is_owned()
        return original_logs(ops, run, stdout, destination)

    monkeypatch.setattr(c.machine, "_fetch_failure_logs", collect_logs)
    _claim_machine(c)
    return c


def _assert_logs(receipt):
    assert receipt["collection"]["state"] == "collected"
    assert {row["role"] for row in receipt["logs"]} == {"engine", "install", "stdout", "stderr"}
    for row in receipt["logs"]:
        assert Path(row["path"]).is_file() and not row["missing"]
        assert Path(row["path"]).read_bytes()


def _assert_bounded_reads(c):
    assert c.log_reads
    for _remote, deadline, observed, paid_remaining in c.log_reads:
        assert deadline is not None, "log transfer lacked a paid deadline"
        assert deadline - observed <= min(120, paid_remaining)


@pytest.mark.parametrize("edit_status", ["installed", "unavailable", "not_installed", "out_of_time"])
def test_music_integrity_then_edit_completion(evidence_case, tmp_path, monkeypatch, edit_status):
    c = evidence_case
    c.pod.fail_jobs = {" generate cpu-music "}
    original_exec = c.pod.exec
    music_status_failed = False
    reuse_snapshots = []
    original_prepare = c.placement.prepare_claimed

    def prepare(*args, **kwargs):
        reuse_snapshots.append(kwargs.get("reuse_snapshot"))
        return original_prepare(*args, **kwargs)

    monkeypatch.setattr(c.placement, "prepare_claimed", prepare)

    def integrity_frame(command, timeout=600):
        nonlocal music_status_failed
        code, stdout, stderr = original_exec(command, timeout)
        if command.startswith("tail -c") and command.endswith("/stdout"):
            stdout += 'astrid-generation-failure:{"code":"integrity_failed","category":"integrity"}\n'
        if "engines status" in command and c.generated == ["cpu-music"] and not music_status_failed:
            music_status_failed = True
            return 0, '{"status":"failed","error_code":"source_changed","failed_step_id":"source"}', ""
        if "engines status" in command and c.generated == ["cpu-music", "cpu-edit"]:
            if edit_status == "unavailable":
                return 7, "private provider body", "private credential"
            if edit_status == "not_installed":
                return 0, json.dumps({"status": "failed", "error_code": "source_changed",
                                      "failed_step_id": "source", "body": "private provider body"}), ""
        return code, stdout, stderr

    monkeypatch.setattr(c.pod, "exec", integrity_frame)
    canonical = c.placement._canonical_installed
    verifications = []

    def verify(state, **kwargs):
        verifications.append((list(c.generated), kwargs))
        if edit_status == "out_of_time" and c.generated == ["cpu-music", "cpu-edit"]:
            return canonical(state, verification_seconds=0)
        return canonical(state, **kwargs)

    monkeypatch.setattr(c.placement, "_canonical_installed", verify)
    music = replace(c.job(), entry="cpu-music", mode="music")
    error, _, _ = _run(c, tmp_path / "music", job=music)
    assert isinstance(error, rm.MachineError)
    first = _only(tmp_path / "music")
    assert first["cause"]["code"] == "integrity_failed"
    assert first["machine"]["after"] == "kept" and c.deletes == []
    assert c.read()["lease"]["preparation"]["state"] == "stale_after_failure"
    assert c.read()["lease"]["last_job"]["failure"]["stage"] == first["stage"]
    c.pod.fail_jobs.clear()
    c.log_reads.clear()
    edit = replace(c.job(2), entry="cpu-edit", mode="edit")
    error, _, _ = _run(c, tmp_path / "edit", job=edit)
    assert c.generated == ["cpu-music", "cpu-edit"] and c.creates == 1
    assert reuse_snapshots == [None, None], "edit must re-prepare the stale installation"
    assert any(jobs == c.generated for jobs, _ in verifications), "edit never re-verified the install"
    if edit_status == "installed":
        assert error is None
        assert (edit.scratch / "outputs/artifacts/cpu.png").read_bytes() == b"png-bytes"
        assert c.read()["lease"]["last_job"]["outcome"] == "success"
        assert c.read()["lease"]["preparation"]["state"] == "prepared"
        assert c.deletes == []
    else:
        assert error is not None
        receipt = _only(tmp_path / "edit")
        assert receipt["verification"]["outcome"] == edit_status
        expected = {"unavailable": "status_exit", "not_installed": "status_not_installed",
                    "out_of_time": "status_deadline_exhausted"}[edit_status]
        assert receipt["verification"]["reason"]["code"] == expected
        assert receipt["cause"]["reason"]["code"] == expected
        assert any(word in receipt["message"] for word in ("retry", "inspect"))
        _assert_logs(receipt)
        _assert_bounded_reads(c)
        assert "private" not in json.dumps(receipt)
        assert c.read()["lease"]["last_job"]["failure"]["stage"] == receipt["stage"]
        assert rm.lease_row(c.read())["last_job"]["failure"]["stage"] == receipt["stage"]


@pytest.mark.parametrize("failure_stage", ["prepare", "engine_start", "execution", "output_collection"])
def test_failure_then_edit_completion(evidence_case, tmp_path, monkeypatch, failure_stage):
    c = evidence_case
    with monkeypatch.context() as patch:
        if failure_stage == "prepare":
            c.pod.fail_jobs = {"engines install"}
        elif failure_stage == "engine_start":
            original_generate = c.pod._generate

            def engine_start(command):
                stdout = original_generate(command)
                c.pod.files[next(path for path in c.pod.files if path.endswith("/progress.json"))] = (
                    b'{"phase":"start_engine"}')
                return stdout

            patch.setattr(c.pod, "_generate", engine_start)
            original_poll = c.pod.job_poll

            def poll(job_id):
                record = original_poll(job_id)
                if job_id in c.generation_jobs:
                    record["exit_code"] = 1
                return record

            patch.setattr(c.pod, "job_poll", poll)
        elif failure_stage == "execution":
            c.pod.fail_jobs = {" generate cpu-image "}
        else:
            original_get = c.pod.get

            def output_unavailable(remote, local):
                if remote.endswith("/artifacts/cpu.png"):
                    raise OSError("output transfer unavailable")
                return original_get(remote, local)

            patch.setattr(c.pod, "get", output_unavailable)
        error, _, _ = _run(c, tmp_path / "first")
    c.pod.fail_jobs.clear()
    assert error is not None
    receipt = _only(tmp_path / "first")
    expected = {"prepare": "install", "engine_start": "install", "execution": "execution",
                "output_collection": "output_collection"}[failure_stage]
    assert receipt["stage"] == expected
    if failure_stage == "engine_start":
        assert receipt["stage_basis"] == "progress"
    assert c.read()["lease"]["last_job"]["failure"]["stage"] == expected
    if failure_stage != "prepare":
        _assert_logs(receipt)
    if failure_stage == "output_collection":
        _assert_bounded_reads(c)
    assert receipt["machine"]["after"] == "kept" and c.deletes == []
    edit = replace(c.job(2), entry="cpu-edit", mode="edit")
    error, _, _ = _run(c, tmp_path / "edit", job=edit)
    assert error is None and not (tmp_path / "edit").exists()
    assert (edit.scratch / "outputs/artifacts/cpu.png").read_bytes() == b"png-bytes"
    assert c.read()["lease"]["last_job"]["outcome"] == "success"
    assert c.read()["lease"]["preparation"]["state"] == "prepared"
    assert c.creates == 1 and c.deletes == []


@pytest.mark.parametrize("refusal", ["missing_markers", "bad_json", "manifest", "checkpoint"])
def test_host_fetch_refusal_keeps_logs(evidence_case, tmp_path, monkeypatch, refusal):
    c = evidence_case
    original = c.pod.exec

    def refuse(command, timeout=600):
        code, stdout, stderr = original(command, timeout)
        if command.startswith("cat ") and command.endswith("/stdout"):
            if refusal == "missing_markers":
                stdout = "\n".join(line for line in stdout.splitlines() if not line.startswith(rp.RESULT_MARK))
            elif refusal == "bad_json":
                stdout = "\n".join(rp.RESULT_MARK + "{" if line.startswith(rp.RESULT_MARK) else line
                                   for line in stdout.splitlines())
            elif refusal == "manifest":
                stdout = "\n".join(rp.MANIFEST_MARK + "0" * 64 if line.startswith(rp.MANIFEST_MARK) else line
                                   for line in stdout.splitlines())
        if refusal == "checkpoint" and "engines status" in command and c.generated:
            return 0, "null", ""
        return code, stdout, stderr

    monkeypatch.setattr(c.pod, "exec", refuse)
    error, _, _ = _run(c, tmp_path / "failures")
    assert error is not None
    receipt = _only(tmp_path / "failures")
    _assert_logs(receipt)
    _assert_bounded_reads(c)
    assert c.read()["lease"]["last_job"]["failure"]["stage"] == receipt["stage"]
    assert rm.lease_row(c.read())["last_job"]["failure"]["stage"] == receipt["stage"]


def test_checkpoint_observation_survives_release_reverification(evidence_case, tmp_path, monkeypatch):
    c = evidence_case
    original = c.pod.exec
    status_reads = []

    def first_status_refuses(command, timeout=600):
        if "engines status" in command and c.generated:
            status_reads.append(command)
            if len(status_reads) == 1:
                return 0, '{"status":"failed","error_code":"source_changed","failed_step_id":"source"}', ""
        return original(command, timeout)

    monkeypatch.setattr(c.pod, "exec", first_status_refuses)
    error, _, _ = _run(c, tmp_path / "failures")
    assert isinstance(error, rm.MachineError)
    assert len(status_reads) == 2, "release must independently re-verify"
    receipt = _only(tmp_path / "failures")
    assert receipt["verification"]["outcome"] == "not_installed"
    assert receipt["verification"]["reason"]["code"] == "status_not_installed"
    assert receipt["machine"]["after"] == "kept"
    assert c.read()["lease"]["preparation"]["state"] == "prepared"
    _assert_logs(receipt)
    _assert_bounded_reads(c)


@pytest.mark.parametrize("log_failure", ["transfer", "paid_expiry"])
def test_log_collection_cannot_mask_host_refusal(evidence_case, tmp_path, monkeypatch, log_failure):
    c = evidence_case
    original_exec, original_get = c.pod.exec, c.pod.get
    original_release, before_expiry = c.machine.release, c.clock()

    def release(lease, warm, *, failure=None, stage="execution"):
        # Isolate expiry of diagnostic collection from the separately tested
        # expired-lease settlement contract.
        c.clock.now = before_expiry
        return original_release(lease, warm, failure=failure, stage=stage)

    def missing_markers(command, timeout=600):
        code, stdout, stderr = original_exec(command, timeout)
        if (command.startswith("cat ") and command.endswith("/stdout")
                and c.generation_jobs and c.generation_jobs[-1] in command):
            if log_failure == "paid_expiry":
                c.clock.now = c.read()["lease"]["paid_expires_at"]
            stdout = "\n".join(line for line in stdout.splitlines() if not line.startswith(rp.RESULT_MARK))
        return code, stdout, stderr

    def unavailable(remote, local):
        if "/out/logs/" in remote:
            raise OSError("private transport body")
        return original_get(remote, local)

    monkeypatch.setattr(c.pod, "exec", missing_markers)
    monkeypatch.setattr(c.machine, "release", release)
    if log_failure == "transfer":
        monkeypatch.setattr(c.pod, "get", unavailable)
    error, _, _ = _run(c, tmp_path / "failures")
    assert type(error) is rm.MachineError
    assert str(error) == "remote job did not report a result and manifest digest"
    receipt = _only(tmp_path / "failures")
    assert receipt["verification"]["outcome"] == "not_observed"
    assert receipt["collection"]["state"] == "unavailable"
    assert "private transport body" not in json.dumps(receipt)
    if log_failure == "paid_expiry":
        assert not c.log_reads, "expired paid time must not start a log transfer"


@pytest.mark.parametrize(("exit_code", "diagnostic", "progress_phase", "expected"), [
    (None, None, None, "output_collection"),
    (75, None, None, "boot"),
    (3, None, None, "workflow_validation"),
    (1, None, None, "execution"),
    (1, {"code": "missing_model", "category": "model"}, None, "model_download"),
    (1, None, "start_engine", "install"),
])
def test_fetch_and_output_collection_use_the_same_receipt_stage(exit_code, diagnostic, progress_phase, expected):
    from types import SimpleNamespace

    stages = []
    lease = SimpleNamespace(failure_exit_code=exit_code, failure_diagnostic=diagnostic)
    run = SimpleNamespace(progress=[{"phase": progress_phase}] if progress_phase else [])
    for phase in ("fetch", "output_collection"):
        receipt = failure_receipt.build_receipt({"failure": rm.MachineError("refused"), "stage": phase,
                                                 "lease": lease, "run": run},
                                                job=SimpleNamespace(), machine=None, timing=None)
        stages.append(receipt["stage"])
    assert stages == [expected, expected]
