from __future__ import annotations

import json
import sys
import threading
import time
from pathlib import Path

import pytest

from astrid.core.execution import generic_host
from astrid.core.execution.generic_host import GenericPackHost, HostCancelled
from tests.test_generic_host import _write_manifest

_PIPE_CAPACITY = 4096


class _BoundedPipe:
    """A Windows-sized pipe that blocks writers when its 4 KiB buffer fills."""

    def __init__(self) -> None:
        self.capacity = _PIPE_CAPACITY
        self._buffer = bytearray()
        self._closed = False
        self._condition = threading.Condition()

    def write_all(self, value: bytes) -> None:
        offset = 0
        while offset < len(value):
            with self._condition:
                while len(self._buffer) == self.capacity and not self._closed:
                    self._condition.wait()
                if self._closed:
                    return
                count = min(self.capacity - len(self._buffer), len(value) - offset)
                self._buffer.extend(value[offset:offset + count])
                offset += count
                self._condition.notify_all()

    def read1(self, size: int) -> bytes:
        with self._condition:
            while not self._buffer and not self._closed:
                self._condition.wait()
            if not self._buffer:
                return b""
            count = min(size, len(self._buffer))
            value = bytes(self._buffer[:count])
            del self._buffer[:count]
            self._condition.notify_all()
            return value

    def read(self, size: int) -> bytes:
        return self.read1(size)

    def close_writer(self) -> None:
        with self._condition:
            self._closed = True
            self._condition.notify_all()

    def close(self) -> None:
        self.close_writer()


class _BoundedProcess:
    def __init__(self, payload: bytes) -> None:
        self.stdout = _BoundedPipe()
        self.stderr = _BoundedPipe()
        self.returncode = None
        self.pid = 424242
        self._done = threading.Event()
        self._done_lock = threading.Lock()
        self._writers = [
            threading.Thread(target=self._write, args=(self.stdout, payload), daemon=True),
            threading.Thread(target=self._write, args=(self.stderr, payload), daemon=True),
        ]
        self._remaining_writers = len(self._writers)
        for writer in self._writers:
            writer.start()

    def _write(self, pipe: _BoundedPipe, payload: bytes) -> None:
        try:
            pipe.write_all(payload)
        finally:
            pipe.close_writer()
            with self._done_lock:
                self._remaining_writers -= 1
                if self._remaining_writers == 0:
                    self.returncode = 0
                    self._done.set()

    def poll(self):
        return self.returncode

    def terminate(self):
        if self.returncode is None:
            self.returncode = -9
        self.stdout.close_writer()
        self.stderr.close_writer()


def test_reader_drains_exact_windows_pipe_capacity_backpressure(tmp_path: Path) -> None:
    process = _BoundedProcess(b"x" * (8 * _PIPE_CAPACITY))
    capture = generic_host._TaskOutputCapture(tmp_path, {}, GenericPackHost._scrub_secret_text)
    capture.start(process)
    deadline = time.monotonic() + 2
    while process.poll() is None and time.monotonic() < deadline:
        time.sleep(0.005)
    capture.finish()
    stdout, stderr = capture.tails()
    assert process.poll() == 0
    assert len(stdout.encode()) == len(stderr.encode()) == 8 * _PIPE_CAPACITY
    metadata = json.loads((tmp_path / "task-output-capture.json").read_text())
    assert metadata["streams"]["stdout"]["byte_count"] == 8 * _PIPE_CAPACITY


def test_reader_scrubs_secrets_across_pipe_chunks_and_bounds_long_secrets(tmp_path: Path) -> None:
    crossing = "TOPSECRET"
    long_secret = "L" * 5000
    overlap_left = "AAAShared"
    overlap_right = "SharedBBB"
    prefix = "x" * (_PIPE_CAPACITY - 3)
    payload = (prefix + crossing + "z" * 128 + long_secret + overlap_left + overlap_right + "tail").encode()
    pipe = _BoundedPipe()
    def write_and_close():
        pipe.write_all(payload)
        pipe.close_writer()

    writer = threading.Thread(target=write_and_close, daemon=True)
    writer.start()
    process = type("Process", (), {"stdout": pipe, "stderr": _BoundedPipe()})()
    process.stderr.close_writer()
    capture = generic_host._TaskOutputCapture(
        tmp_path,
        {
            "ASTRID_RUNTIME_CREDENTIAL": crossing,
            "ASTRID_NETWORK_BROKER_TOKEN": long_secret,
            "FIRST_OVERLAPPING_SECRET": overlap_left,
            "SECOND_OVERLAPPING_SECRET": overlap_right,
        },
        GenericPackHost._scrub_secret_text,
    )
    capture.start(process)
    capture.finish()
    writer.join(timeout=1)
    stdout, _ = capture.tails()
    assert "TOPSECRET" not in stdout
    assert long_secret not in stdout
    assert overlap_left not in stdout
    assert overlap_right not in stdout
    assert stdout.endswith("tail")
    assert stdout.count("<redacted>") == 4
    assert len((tmp_path / "task-stdout-tail.log").read_bytes()) <= 64 * 1024


def test_repeated_overlapping_short_secrets_do_not_grow_pending_buffer(tmp_path: Path) -> None:
    payload = b"a" * (128 * 1024)
    process = _BoundedProcess(payload)
    capture = generic_host._TaskOutputCapture(
        tmp_path, {"SHORT_TOKEN": "aa"}, GenericPackHost._scrub_secret_text,
    )
    capture.start(process)
    deadline = time.monotonic() + 2
    while process.poll() is None and time.monotonic() < deadline:
        time.sleep(0.005)
    capture.finish()
    assert process.poll() == 0
    assert capture._pending["stdout"] == ""
    stdout, stderr = capture.tails()
    assert "aa" not in stdout and "aa" not in stderr
    assert len((tmp_path / "task-stdout-tail.log").read_bytes()) <= 64 * 1024


@pytest.mark.parametrize("worker_path", [False, True], ids=["direct-command", "generic-worker"])
def test_command_paths_drain_pipe_output_and_persist_scrubbed_bounded_tails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, worker_path: bool
) -> None:
    _write_manifest(tmp_path / "echo")
    manifest_path = tmp_path / "echo" / "executor.yaml"
    manifest = json.loads(manifest_path.read_text())
    manifest["command"]["argv"][2] = "pass"
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    host = GenericPackHost(pack_roots=[tmp_path])
    host.discover()
    original_environment = host._child_environment

    def child_environment(*args, **kwargs):
        env, secrets = original_environment(*args, **kwargs)
        env["ASTRID_RUNTIME_CREDENTIAL"] = "credential-value-COORD5"
        env["ASTRID_NETWORK_BROKER_TOKEN"] = "broker-token-COORD5"
        secrets["ASTRID_RUNTIME_CREDENTIAL"] = env["ASTRID_RUNTIME_CREDENTIAL"]
        return env, secrets

    monkeypatch.setattr(host, "_child_environment", child_environment)
    attempt = tmp_path / "attempt"
    output_root = attempt / "outputs"
    output_root.mkdir(parents=True)
    started = time.monotonic()
    process = _BoundedProcess(
        b"O" * 524288 + b"credential-value-COORD5stdout-tail" +
        b"broker-token-COORD5stderr-tail"
    )

    def launch(argv, **kwargs):
        if worker_path:
            request_data = json.loads(Path(argv[-1]).read_text())
            Path(request_data["result_path"]).write_text(json.dumps({"ok": True, "returncode": 0}))
        else:
            (output_root / "answer.txt").write_text("ok")
        return process

    monkeypatch.setattr(generic_host, "spawn_task_scope", launch)
    monkeypatch.setattr(generic_host, "_terminate_process_group", lambda child: child.terminate())

    def cancelled() -> bool:
        return time.monotonic() - started > 5

    try:
        if worker_path:
            definition, admission = host.admit("executor", "test.echo")
            result = host.invoke_capability(
                capability_kind="executor",
                capability_id="test.echo",
                request={
                    "inputs": {}, "out": str(output_root), "project": "demo",
                    "project_was_auto_resolved": True, "python_exec": sys.executable,
                    "invocation": "runtime",
                },
                attempt=attempt,
                definition=definition,
                admission=admission,
                child_env={
                    "ASTRID_RUNTIME_CREDENTIAL": "credential-value-COORD5",
                    "ASTRID_NETWORK_BROKER_TOKEN": "broker-token-COORD5",
                },
                cancelled=cancelled,
            )
            assert result.ok
        else:
            result = host._run_command_definition(
                host.capabilities["test.echo"], {}, output_root, attempt, cancelled=cancelled
            )
            assert result.returncode == 0
    finally:
        process.terminate()
        for writer in process._writers:
            writer.join(timeout=1)
        assert all(not writer.is_alive() for writer in process._writers)

    for name, marker in (("stdout", "stdout-tail"), ("stderr", "stderr-tail")):
        tail_path = attempt / f"task-{name}-tail.log"
        tail = tail_path.read_bytes()
        assert len(tail) <= 64 * 1024
        text = tail.decode("utf-8")
        assert marker in text
        assert "credential-value-COORD5" not in text
        assert "broker-token-COORD5" not in text
    metadata = json.loads((attempt / "task-output-capture.json").read_text())
    assert all(metadata["streams"][name]["truncated"] for name in ("stdout", "stderr"))


def test_production_command_path_drains_exact_four_kib_pipes(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _write_manifest(tmp_path / "echo")
    host = GenericPackHost(pack_roots=[tmp_path])
    host.discover()
    attempt = tmp_path / "attempt"
    output_root = attempt / "outputs"
    output_root.mkdir(parents=True)
    process = _BoundedProcess(b"x" * (8 * _PIPE_CAPACITY))

    def launch(*args, **kwargs):
        (output_root / "answer.txt").write_text("ok")
        return process

    monkeypatch.setattr(generic_host, "spawn_task_scope", launch)
    monkeypatch.setattr(generic_host, "_terminate_process_group", lambda child: child.terminate())
    started = time.monotonic()
    try:
        result = host._run_command_definition(
            host.capabilities["test.echo"], {}, output_root, attempt,
            cancelled=lambda: time.monotonic() - started > 5,
        )
    finally:
        process.terminate()
        for writer in process._writers:
            writer.join(timeout=1)
        assert all(not writer.is_alive() for writer in process._writers)
    assert result.returncode == 0
    assert len((attempt / "task-stdout-tail.log").read_bytes()) == 8 * _PIPE_CAPACITY


def test_negative_control_without_capture_blocks_on_full_pipe(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _write_manifest(tmp_path / "echo")
    host = GenericPackHost(pack_roots=[tmp_path])
    host.discover()
    attempt = tmp_path / "attempt"
    output_root = attempt / "outputs"
    output_root.mkdir(parents=True)
    process = _BoundedProcess(b"x" * (8 * _PIPE_CAPACITY))
    monkeypatch.setattr(generic_host, "spawn_task_scope", lambda *args, **kwargs: process)
    monkeypatch.setattr(generic_host, "_terminate_process_group", lambda child: child.terminate())
    monkeypatch.setattr(generic_host._TaskOutputCapture, "start", lambda self, child: None)
    deadline = time.monotonic() + 1
    while time.monotonic() < deadline:
        if all(len(pipe._buffer) == _PIPE_CAPACITY for pipe in (process.stdout, process.stderr)):
            break
        time.sleep(0.005)
    assert all(len(pipe._buffer) == _PIPE_CAPACITY for pipe in (process.stdout, process.stderr))
    try:
        with pytest.raises(HostCancelled):
            host._run_command_definition(
                host.capabilities["test.echo"], {}, output_root, attempt,
                cancelled=lambda: process in host._active_processes,
            )
    finally:
        for writer in process._writers:
            writer.join(timeout=1)
    assert all(not writer.is_alive() for writer in process._writers)


def test_cancellation_finishes_output_readers_and_keeps_final_tail(tmp_path: Path) -> None:
    _write_manifest(tmp_path / "echo")
    manifest_path = tmp_path / "echo" / "executor.yaml"
    manifest = json.loads(manifest_path.read_text())
    manifest["command"]["argv"][2] = "pass"
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    host = GenericPackHost(pack_roots=[tmp_path])
    host.discover()
    attempt = tmp_path / "attempt"
    output_root = attempt / "outputs"
    output_root.mkdir(parents=True)
    class WaitingProcess:
        def __init__(self):
            self.stdout = _BoundedPipe()
            self.stderr = _BoundedPipe()
            self.stderr.write_all(b"before-cancel")
            self.returncode = None

        def poll(self):
            return self.returncode

        def terminate(self):
            self.returncode = -9
            self.stdout.close_writer()
            self.stderr.close_writer()

    process = WaitingProcess()
    monkeypatch = pytest.MonkeyPatch()
    monkeypatch.setattr(generic_host, "spawn_task_scope", lambda *args, **kwargs: process)
    monkeypatch.setattr(generic_host, "_terminate_process_group", lambda *_args, **_kwargs: process.terminate())
    started = time.monotonic()
    readers_before = {id(thread) for thread in threading.enumerate() if thread.name == "astrid-pipe-reader"}
    try:
        with pytest.raises(HostCancelled):
            host._run_command_definition(
                host.capabilities["test.echo"], {}, output_root, attempt,
                cancelled=lambda: time.monotonic() - started > 0.6,
            )
        assert "before-cancel" in (attempt / "task-stderr-tail.log").read_text()
        assert not any(
            thread.name == "astrid-pipe-reader" and id(thread) not in readers_before
            for thread in threading.enumerate()
        )
    finally:
        monkeypatch.undo()


@pytest.mark.parametrize("worker_path", [False, True], ids=["direct-command", "generic-worker"])
def test_cleanup_drains_final_bytes_from_pipe_held_open_after_leader_exit(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, worker_path: bool
) -> None:
    _write_manifest(tmp_path / "echo")
    host = GenericPackHost(pack_roots=[tmp_path])
    host.discover()
    attempt = tmp_path / "attempt"
    output_root = attempt / "outputs"
    output_root.mkdir(parents=True)

    class ExitedLeader:
        def __init__(self):
            self.stdout = _BoundedPipe()
            self.stderr = _BoundedPipe()
            self.returncode = 0
            self.pid = 424243

        def poll(self):
            return 0

        def terminate(self):
            self.stderr.write_all(b"final-tail-after-leader-exit")
            self.stdout.close_writer()
            self.stderr.close_writer()

    process = ExitedLeader()

    def launch(argv, **kwargs):
        if worker_path:
            request_data = json.loads(Path(argv[-1]).read_text())
            Path(request_data["result_path"]).write_text(json.dumps({"ok": True, "returncode": 0}))
        else:
            (output_root / "answer.txt").write_text("ok")
        return process

    monkeypatch.setattr(generic_host, "spawn_task_scope", launch)
    monkeypatch.setattr(generic_host, "_terminate_process_group", lambda child: child.terminate())
    if worker_path:
        definition, admission = host.admit("executor", "test.echo")
        result = host.invoke_capability(
            capability_kind="executor", capability_id="test.echo",
            request={
                "inputs": {}, "out": str(output_root), "project": "demo",
                "project_was_auto_resolved": True, "python_exec": sys.executable,
                "invocation": "runtime",
            },
            attempt=attempt, definition=definition, admission=admission,
        )
        assert "final-tail-after-leader-exit" in result.stderr
    else:
        result = host._run_command_definition(
            host.capabilities["test.echo"], {}, output_root, attempt, cancelled=lambda: False,
        )
        assert result.returncode == 0
    assert "final-tail-after-leader-exit" in (attempt / "task-stderr-tail.log").read_text()
