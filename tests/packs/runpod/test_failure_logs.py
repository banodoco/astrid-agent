"""Declared logs from a failed pod job come back before the reset."""

import json
import shutil
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

from astrid.core._shared import run_logs
from astrid.packs.runpod import failure_receipt, machine


ERROR = "Traceback (most recent call last):\nRuntimeError: CUDA out of memory. Tried to allocate 2.00 GiB"


def _pod(tmp_path, stderr_text=ERROR, stderr_bytes=None):
    job = tmp_path / "pod/jobs/j1"
    run = tmp_path / "pod/runs/r1"
    out = run / "out"
    engine = tmp_path / "engine.log"
    job.mkdir(parents=True)
    out.mkdir(parents=True)
    (job / "command.sh").write_text("#!/bin/sh\n")
    (job / "stderr").write_text(stderr_text if stderr_bytes is None else "x" * stderr_bytes)
    (job / "stdout").write_text("progress line\n")
    (run / "result.json").write_text(json.dumps({"ok": False}))
    engine.write_text("!!! Exception during processing !!! node AILab_Qwen3TTSCustomVoice\n")
    run_logs.declare(out, run_logs.LogDecl("engine", "engine", str(engine)))
    return job, run


def _finish(job, run):
    command = machine.finish_command(str(run), python=sys.executable)
    done = subprocess.run(["bash", "-c", command, str(job / "command.sh")], capture_output=True,
                          text=True, timeout=60)
    assert done.returncode == 0
    return done.stdout


def test_finish_collects_job_stdout_stderr_and_declared_engine_log(tmp_path):
    job, run = _pod(tmp_path)
    stdout = _finish(job, run)
    index = json.loads((run / "out/logs/index.json").read_text())
    by_name = {row["name"]: row for row in index["logs"]}
    assert Path(run / "out/logs" / by_name["job.stdout"]["file"]).read_text() == "progress line\n"
    assert Path(run / "out/logs" / by_name["job.stderr"]["file"]).read_text() == ERROR
    assert "AILab_Qwen3TTSCustomVoice" in Path(run / "out/logs" / by_name["engine"]["file"]).read_text()
    assert any(line.startswith("LOGS_MANIFEST_SHA256=") for line in stdout.splitlines())


def test_finish_caps_large_logs_and_writes_truncation_marker(tmp_path, monkeypatch):
    # Exercise the embedded, package-independent collector at a small cap.
    original_read_text = Path.read_text

    def smaller_embedded_cap(path, *args, **kwargs):
        text = original_read_text(path, *args, **kwargs)
        if path == Path(run_logs.__file__):
            text = text.replace("CAP_BYTES = 256 * 1024 * 1024", "CAP_BYTES = 1024")
        return text

    monkeypatch.setattr(Path, "read_text", smaller_embedded_cap)
    job, run = _pod(tmp_path, stderr_bytes=4096)
    _finish(job, run)
    index = json.loads((run / "out/logs/index.json").read_text())
    row = next(row for row in index["logs"] if row["name"] == "job.stderr")
    content = (run / "out/logs" / row["file"]).read_bytes()
    assert row["truncated"] is True
    assert len(content) <= 1024 and b"[run-logs gap]" in content


class _Ops:
    """Pod transport over the local filesystem: get() copies a pod path to a local path."""

    def __init__(self, tamper=None):
        self.tamper = tamper

    def get(self, remote, local):
        shutil.copyfile(remote, local)
        if self.tamper and Path(remote).name == self.tamper:
            Path(local).write_text("tampered")


def _fetch(tmp_path, ops):
    job, run = _pod(tmp_path)
    stdout = _finish(job, run)
    scratch = tmp_path / "scratch"
    scratch.mkdir()
    owner = object.__new__(machine.RunPodMachine)
    fake_run = SimpleNamespace(remote_root=str(run), scratch=scratch)
    return owner._fetch_failure_logs(ops, fake_run, stdout, tmp_path / "local")


def test_controller_fetches_declared_logs_digest_verified(tmp_path):
    logs = _fetch(tmp_path, _Ops())
    by_name = {row["name"]: row for row in logs["files"]}
    assert Path(by_name["job.stderr"]["path"]).read_text() == ERROR
    assert by_name["job.stderr"]["truncated"] is False


def test_digest_mismatch_is_unavailable_and_never_masks_the_job_error(tmp_path):
    assert _fetch(tmp_path, _Ops(tamper="job.stderr.log")) == {"unavailable": "DigestMismatch"}


def test_nothing_reported_is_unavailable():
    owner = object.__new__(machine.RunPodMachine)
    assert owner._fetch_failure_logs(None, SimpleNamespace(remote_root="/r", scratch=Path("/tmp")),
                                    "no marker here", Path("/tmp")) == {"unavailable": "not_reported"}


def test_receipt_keeps_its_own_copy_of_the_logs(tmp_path):
    source = tmp_path / "stderr"
    source.write_text(ERROR)
    receipt = {"identity": {"task_id": "task-1"}, "logs": [{"name": "stderr", "path": str(source),
                                                           "bytes": len(ERROR), "truncated": False}]}
    path = failure_receipt.write_receipt(receipt, root=tmp_path / "failures")
    stored = json.loads(path.read_text())
    copy = Path(stored["logs"][0]["path"])
    assert copy.parent == path.parent / "logs" and copy.read_text() == ERROR
    source.unlink()
    assert copy.read_text() == ERROR
