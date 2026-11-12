from __future__ import annotations

from pathlib import Path

import pytest

from astrid.packs.runpod.remote_placement import (
    Claim,
    Limits,
    RemotePlacement,
    RemotePlacementError,
    RemoteRun,
)


class FakeOps:
    def __init__(self, stderr: bytes = b"", *, fail_get: bool = False, exit_code: int = 2):
        self.files = {"/jobs/one/stdout": b"job output", "/jobs/one/stderr": stderr}
        self.fail_get = fail_get
        self.exit_code = exit_code
        self.get_calls: list[str] = []

    def job_start(self, command, **kwargs):
        return {"id": "a" * 32, "dir": "/jobs/one"}

    def job_poll(self, job_id):
        return {"id": job_id, "state": "exited", "exit_code": self.exit_code}

    def exec(self, command, timeout=600):
        if "tail -c" in command:
            return 0, self.files["/jobs/one/stderr"][-4000:].decode(), ""
        return 0, self.files["/jobs/one/stdout"].decode(), ""

    def get(self, remote, local):
        self.get_calls.append(remote)
        if self.fail_get:
            raise OSError("download failed")
        Path(local).write_bytes(self.files[remote])


class FakeProvider:
    def __init__(self, ops):
        self.ops = ops

    def claim(self, spec):
        raise AssertionError("not used")


def _run(tmp_path: Path, ops: FakeOps):
    runner = RemotePlacement(FakeProvider(ops), limits=Limits(poll_seconds=0))
    state = RemoteRun("run", "/remote", tmp_path)
    state.claim = Claim("pod", ops, {})
    return runner, state


def test_failed_job_keeps_full_stderr_and_names_it(tmp_path):
    stderr = b"x" * 20_000
    runner, state = _run(tmp_path, FakeOps(stderr))

    with pytest.raises(RemotePlacementError) as raised:
        runner._run_job(state, "run", "engine install")

    expected = tmp_path / "logs" / "engine-install.stderr"
    assert expected.read_bytes() == stderr
    assert str(raised.value).endswith(f"(full logs: {expected})")
    assert state.steps[-1]["step"] == "logs"
    assert state.steps[-1]["status"] == "kept"


def test_download_failure_preserves_original_error(tmp_path):
    runner, state = _run(tmp_path, FakeOps(b"original tail", fail_get=True))

    with pytest.raises(RemotePlacementError) as raised:
        runner._run_job(state, "run", "engine install")

    assert str(raised.value) == f"engine install job {'a' * 32} exited 2: original tail"
    assert state.steps[-1]["step"] == "logs"
    assert state.steps[-1]["status"] == "unavailable"
    assert state.steps[-1]["error"] == "OSError"


def test_success_does_not_download_logs(tmp_path):
    ops = FakeOps(b"", exit_code=0)
    runner, state = _run(tmp_path, ops)

    assert runner._run_job(state, "run", "engine install") == "job output"
    assert ops.get_calls == []
