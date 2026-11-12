"""A failed pod job's own logs come back, unredacted, before the reset."""

import json
import shutil
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from astrid.packs.runpod import failure_receipt, machine


ERROR = "Traceback (most recent call last):\nRuntimeError: CUDA out of memory. Tried to allocate 2.00 GiB"


def _pod(tmp_path, stderr_text=ERROR, stderr_bytes=None):
    job = tmp_path / "pod/jobs/j1"
    run = tmp_path / "pod/runs/r1"
    engine = tmp_path / "pod/data/out/sessions/s1"
    for d in (job, run, engine):
        d.mkdir(parents=True)
    (job / "command.sh").write_text("#!/bin/sh\n")
    (job / "stderr").write_text(stderr_text if stderr_bytes is None else "x" * stderr_bytes)
    (job / "stdout").write_text("progress line\n")
    (run / "result.json").write_text(json.dumps({"ok": False}))
    (engine / "comfy.log").write_text("!!! Exception during processing !!! node AILab_Qwen3TTSCustomVoice\n")
    return job, run, tmp_path / "pod/data", tmp_path / "pod"


def _run_collector(job, run, data_root, pod_root):
    command = machine.failure_logs_command(str(run), str(data_root), str(pod_root), python=sys.executable)
    done = subprocess.run(["bash", "-c", command, str(job / "command.sh")], capture_output=True, text=True,
                          timeout=60)
    assert done.returncode == 0
    return done.stdout


def test_pod_collector_keeps_full_unredacted_logs_and_reports_a_manifest(tmp_path):
    job, run, data_root, pod_root = _pod(tmp_path)
    stdout = _run_collector(job, run, data_root, pod_root)
    folder = run / machine.FAILURE_DIR
    assert (folder / "stderr").read_text() == ERROR
    assert "AILab_Qwen3TTSCustomVoice" in (folder / "engine-1.log").read_text()
    assert json.loads((folder / "result.json").read_text()) == {"ok": False}
    assert any(line.startswith(machine.FAILURE_MANIFEST_MARK) for line in stdout.splitlines())


def test_pod_collector_caps_large_logs_and_marks_them(tmp_path):
    job, run, data_root, pod_root = _pod(tmp_path, stderr_bytes=machine.FAILURE_LOG_CAP_BYTES + 4096)
    _run_collector(job, run, data_root, pod_root)
    folder = run / machine.FAILURE_DIR
    assert (folder / "stderr").stat().st_size == machine.FAILURE_LOG_CAP_BYTES
    assert "stderr" in (folder / "truncated").read_text().split()


class _Ops:
    """Pod transport over the local filesystem: get() copies a pod path to a local path."""

    def __init__(self, tamper=None):
        self.tamper = tamper

    def get(self, remote, local):
        shutil.copyfile(remote, local)
        if self.tamper and Path(remote).name == self.tamper:
            Path(local).write_text("tampered")


def _fetch(tmp_path, ops):
    job, run, data_root, pod_root = _pod(tmp_path)
    stdout = _run_collector(job, run, data_root, pod_root)
    scratch = tmp_path / "scratch"
    scratch.mkdir()
    owner = object.__new__(machine.RunPodMachine)
    fake_run = SimpleNamespace(remote_root=str(run), scratch=scratch)
    return owner._fetch_failure_logs(ops, fake_run, stdout, tmp_path / "local")


def test_controller_fetches_the_failure_folder_digest_verified(tmp_path):
    logs = _fetch(tmp_path, _Ops())
    by_name = {row["name"]: row for row in logs["files"]}
    assert Path(by_name["stderr"]["path"]).read_text() == ERROR
    assert by_name["stderr"]["truncated"] is False


def test_a_tampered_log_is_reported_unavailable_and_never_masks_the_job_error(tmp_path):
    assert _fetch(tmp_path, _Ops(tamper="stderr")) == {"unavailable": "DigestMismatch"}


def test_no_failure_manifest_is_reported_not_reported():
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
