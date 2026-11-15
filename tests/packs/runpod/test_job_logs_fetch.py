"""The controller fetches the declared log folder on a failed RunPod job."""

import hashlib
import json
import shutil
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest

from astrid.packs.runpod import machine


class _Ops:
    def __init__(self, tamper=None):
        self.tamper = tamper

    def exec(self, command, timeout):
        done = subprocess.run(["bash", "-c", command], capture_output=True, text=True, timeout=timeout)
        return done.returncode, done.stdout, done.stderr

    def get(self, remote, local):
        shutil.copyfile(remote, local)
        if self.tamper and Path(remote).name == self.tamper:
            Path(local).write_text("tampered")


def _manifest(folder):
    entries = {}
    for path in sorted(p for p in folder.rglob("*") if p.is_file()):
        relative = path.relative_to(folder).as_posix()
        entries[relative] = {"size": path.stat().st_size,
                             "sha256": hashlib.sha256(path.read_bytes()).hexdigest()}
    content = json.dumps({"files": entries}, sort_keys=True, separators=(",", ":"))
    manifest = folder / machine.MANIFEST_NAME
    manifest.write_text(content)
    return hashlib.sha256(manifest.read_bytes()).hexdigest()


class _State:
    def note(self, *args, **kwargs):
        pass


class _Placement:
    def _check_deadline(self, stage):
        pass


def _fixture(tmp_path, *, mark=True, far=False):
    root = tmp_path / "pod/runs/r1"
    job = tmp_path / "pod/jobs/j1"
    logs = root / "out/logs"
    logs.mkdir(parents=True)
    job.mkdir(parents=True)
    (logs / "index.json").write_text(json.dumps({"schema": "run-logs.collected.v1", "logs": [
        {"name": "engine", "role": "engine", "file": "engine.log", "bytes": 6,
         "sha256": "x", "truncated": False, "missing": False},
        {"name": "engine-error", "role": "engine", "file": "engine-error.json", "bytes": 2,
         "sha256": "y", "truncated": False, "missing": False},
        {"name": "run", "role": "run", "file": "run.txt", "bytes": 3,
         "sha256": "z", "truncated": False, "missing": False},
    ]}))
    (logs / "engine.log").write_bytes(b"engine")
    (logs / "engine-error.json").write_bytes(b"{}")
    (logs / "run.txt").write_bytes(b"run")
    digest = _manifest(logs)
    stdout = "progress\n"
    if mark:
        stdout += f"LOGS_MANIFEST_SHA256={digest}\n"
    if far:
        stdout += "x" * 10000
    (job / "stdout").write_text(stdout)
    scratch = tmp_path / "scratch"
    scratch.mkdir()
    lease = SimpleNamespace(state=_State(), timing=None, node_ids=frozenset(), node_names=frozenset())
    run = SimpleNamespace(lease=lease, job_id="j1", directory=str(job), remote_root=str(root), scratch=scratch,
                          progress=[])
    ops = _Ops()
    adapter = SimpleNamespace(placement=_Placement(), lease_owner=None)
    owner = machine.RunPodMachine(adapter)
    owner._checkpoint = lambda *args: None
    # fetch obtains the job poll and operation transport from the lease claim.
    ops.job_poll = lambda job_id: {"exit_code": 1}
    run.lease.claim = SimpleNamespace(ops=ops)
    return owner, run, ops, tmp_path / "local"


@pytest.mark.parametrize("far", [False, True])
def test_fetches_declared_logs_and_reports_local_paths(tmp_path, far):
    owner, run, ops, destination = _fixture(tmp_path, far=far)
    with pytest.raises(machine.MachineError, match="remote generation failed"):
        owner.fetch(run, destination)
    rows = run.lease.failure_logs["files"]
    assert {row["file"] for row in rows} == {"engine-error.json", "engine.log", "run.txt"}
    for row in rows:
        path = Path(row["path"])
        assert path.read_bytes() == (run.remote_root and Path(run.remote_root) / "out/logs" / row["file"]).read_bytes()


def test_tampered_log_is_unavailable_and_original_error_is_preserved(tmp_path):
    owner, run, ops, destination = _fixture(tmp_path)
    ops.tamper = "engine.log"
    with pytest.raises(machine.MachineError) as raised:
        owner.fetch(run, destination)
    assert str(raised.value) == "remote generation failed (exit 1; category=unknown; code=unknown)"
    assert run.lease.failure_logs == {"unavailable": "DigestMismatch"}


def test_missing_mark_is_not_reported(tmp_path):
    owner, run, _ops, destination = _fixture(tmp_path, mark=False)
    with pytest.raises(machine.MachineError):
        owner.fetch(run, destination)
    assert run.lease.failure_logs == {"unavailable": "not_reported"}
