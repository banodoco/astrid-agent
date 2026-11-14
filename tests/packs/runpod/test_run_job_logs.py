from __future__ import annotations

import json
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


@pytest.mark.parametrize("failed_stream", ["stdout", "stderr"])
def test_partial_collection_keeps_only_collected_files(tmp_path, failed_stream):
    class PartialOps(FakeOps):
        def get(self, remote, local):
            if remote == f"/jobs/one/{failed_stream}":
                raise OSError("one log transfer failed")
            return super().get(remote, local)

    ops = PartialOps(b"original tail")
    runner, state = _run(tmp_path, ops)
    with pytest.raises(RemotePlacementError) as raised:
        runner._run_job(state, "run", "engine install")

    collected_stream = "stderr" if failed_stream == "stdout" else "stdout"
    collected = tmp_path / "logs" / f"engine-install.{collected_stream}"
    assert collected.read_bytes() == ops.files[f"/jobs/one/{collected_stream}"]
    step = state.steps[-1]
    assert step["step"] == "logs" and step["status"] == "kept"
    assert step["files"] == [str(collected)] and "error" not in step
    rows = json.loads((tmp_path / "logs/index.json").read_text())["logs"]
    assert len(rows) == 6
    assert {row["name"] for row in rows if not row["missing"]} == {
        f"engine-install-{collected_stream}",
    }
    assert next(row for row in rows if row["name"] == f"engine-install-{failed_stream}") == {
        "name": f"engine-install-{failed_stream}", "missing": True, "reason": "FileNotFoundError",
    }
    assert str(raised.value).startswith(f"engine install job {'a' * 32} exited 2: original tail")


@pytest.mark.parametrize("prior_log", [False, True])
def test_absent_logs_are_unavailable_without_a_transfer_exception(tmp_path, prior_log):
    from astrid.core._shared import run_logs

    class AbsentOps(FakeOps):
        def job_start(self, command, **kwargs):
            return {"id": "a" * 32}

        def exec(self, command, timeout=600):
            # The job reported no directory, and the declared engine artifacts
            # do not exist. Their redaction commands fail without a transfer.
            return 1, "", ""

    if prior_log:
        previous = tmp_path / "previous.log"
        previous.write_text("a different step's log")
        run_logs.declare(tmp_path / ".job-logs", run_logs.LogDecl(
            name="previous-step", role="stdout", path=str(previous)))
    ops = AbsentOps()
    runner, state = _run(tmp_path, ops)
    with pytest.raises(RemotePlacementError) as raised:
        runner._run_job(state, "run", "engine install")

    assert str(raised.value) == f"engine install job {'a' * 32} exited 2: "
    assert state.steps[-1]["step"] == "logs"
    assert state.steps[-1]["status"] == "unavailable"
    assert state.steps[-1]["error"] == "missing"
    assert ops.get_calls == []
    rows = json.loads((tmp_path / "logs/index.json").read_text())["logs"]
    missing = [row for row in rows if row["missing"]]
    assert missing == [{"name": f"engine.install.{label}", "missing": True,
                        "reason": "FileNotFoundError"} for label in (
                            "engine-log", "supervisor-log", "session-marker", "registry-config")]
    if prior_log:
        assert next(row for row in rows if row["name"] == "previous-step")["missing"] is False


def test_first_transfer_failure_is_preserved_when_later_transfers_also_fail(tmp_path):
    class MixedFailures(FakeOps):
        def get(self, remote, local):
            if remote == "/jobs/one/stdout":
                raise TimeoutError("first transfer failed")
            raise OSError("later transfer failed")

    runner, state = _run(tmp_path, MixedFailures(b"original tail"))
    with pytest.raises(RemotePlacementError) as raised:
        runner._run_job(state, "run", "engine install")

    assert str(raised.value) == f"engine install job {'a' * 32} exited 2: original tail"
    assert state.steps[-1]["status"] == "unavailable"
    assert state.steps[-1]["error"] == "TimeoutError"
    rows = json.loads((tmp_path / "logs/index.json").read_text())["logs"]
    assert len(rows) == 6 and all(row["missing"] for row in rows)


def test_success_does_not_download_logs(tmp_path):
    ops = FakeOps(b"", exit_code=0)
    runner, state = _run(tmp_path, ops)

    assert runner._run_job(state, "run", "engine install") == "job output"
    assert ops.get_calls == []
