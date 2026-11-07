"""Real anonymous pipes exercise the same pump on Linux and Windows."""
from __future__ import annotations

import io
import queue
import subprocess
import sys
import threading
import time
from types import SimpleNamespace

import pytest

from astrid.core.execution.pipe_io import PipeReader, PipeWriter


@pytest.fixture
def children():
    children = []

    def launch(code):
        process = subprocess.Popen([sys.executable, "-u", "-c", code], stdin=subprocess.PIPE,
                                   stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        workers = []
        children.append((process, workers))

        def own(worker):
            workers.append(worker)
            return worker

        return process, own

    yield launch
    for process, workers in children:
        if process.poll() is None:
            process.kill()
        process.wait(timeout=10)
        try:
            for worker in workers:
                assert worker._thread.daemon
                worker.close(timeout=5)
                assert not worker._thread.is_alive()
        finally:
            for stream in (process.stdin, process.stdout, process.stderr):
                stream.close()


def test_silent_child_read_deadline_does_not_wait_for_eof(children):
    process, own = children("import sys; sys.stdin.buffer.read()")
    reader = own(PipeReader(process.stdout))
    deadline = time.monotonic() + .1
    with pytest.raises(TimeoutError):
        reader.read(deadline=deadline)
    assert time.monotonic() >= deadline
    assert process.poll() is None


def test_cancellation_while_waiting_for_the_rest_of_a_partial_read(children):
    process, own = children("import os, sys; os.write(1, b'partial'); sys.stdin.buffer.read()")
    reader = own(PipeReader(process.stdout))
    assert reader.read(deadline=time.monotonic() + 10) == b"partial"
    cancelled = threading.Event()
    timer = threading.Timer(.1, cancelled.set)
    timer.start()

    def guard():
        if cancelled.is_set():
            raise RuntimeError("invocation cancelled")

    try:
        with pytest.raises(RuntimeError, match="invocation cancelled"):
            reader.read(deadline=time.monotonic() + 10, guard=guard)
        assert process.poll() is None
    finally:
        timer.cancel()
        timer.join()


def test_original_exit_does_not_discard_buffered_output_and_eof_is_sticky(children):
    process, own = children("import os; os.write(1, b'x' * 131072 + b'tail')")
    reader = own(PipeReader(process.stdout))
    assert process.wait(timeout=10) == 0
    output = bytearray()
    deadline = time.monotonic() + 10
    while chunk := reader.read(777, deadline=deadline):
        output.extend(chunk)
    assert output == b"x" * 131072 + b"tail"
    assert reader.read(deadline=deadline) == b""


def test_full_stdout_and_stderr_are_drained_without_deadlock(children):
    process, own = children("""import os, threading
def write(fd, byte):
    for _ in range(64):
        os.write(fd, byte * 65536)
t = threading.Thread(target=write, args=(2, b'e'))
t.start()
write(1, b'o')
t.join()
""")
    events = queue.Queue(maxsize=2)
    for name in ("stdout", "stderr"):
        own(PipeReader(getattr(process, name), chunk_size=8192, events=events, name=name))
    output = {"stdout": bytearray(), "stderr": bytearray()}
    open_pipes = set(output)
    deadline = time.monotonic() + 30
    while open_pipes:
        name, chunk = PipeReader.receive(events, deadline=deadline)
        assert len(chunk) <= 8192 and events.qsize() <= 2
        if chunk:
            output[name].extend(chunk)
        else:
            open_pipes.remove(name)
    assert process.wait(timeout=10) == 0
    assert output == {"stdout": b"o" * (64 * 65536), "stderr": b"e" * (64 * 65536)}


@pytest.mark.parametrize("cancel", [False, True])
def test_blocked_writer_keeps_deadline_and_cancellation_responsive(children, cancel):
    process, own = children("import time; time.sleep(60)")
    writer = own(PipeWriter(process.stdin))
    cancelled = threading.Event()
    timer = threading.Timer(.1, cancelled.set)

    def guard():
        if cancel and cancelled.is_set():
            raise RuntimeError("write cancelled")

    if cancel:
        timer.start()
    try:
        with pytest.raises(RuntimeError if cancel else TimeoutError, match="cancelled" if cancel else "timed out"):
            writer.write(b"x" * (4 * 1024 * 1024), deadline=time.monotonic() + (10 if cancel else .1), guard=guard)
        assert process.poll() is None
    finally:
        if cancel:
            timer.cancel()
            timer.join()


def test_writer_handles_large_request_while_both_response_pipes_are_full(children):
    size = 2 * 1024 * 1024
    process, own = children(f"""import os, sys
os.write(1, b'o' * {size})
os.write(2, b'e' * {size})
value = sys.stdin.buffer.read({size})
assert value == b'i' * {size}
""")
    output, diagnostics = bytearray(), bytearray()
    stdout = own(PipeReader(process.stdout, sink=output.extend))
    stderr = own(PipeReader(process.stderr, sink=diagnostics.extend))
    writer = own(PipeWriter(process.stdin))
    writer.write(b"i" * size, deadline=time.monotonic() + 30)
    assert process.wait(timeout=10) == 0
    # Closing sinks drains bytes still buffered after the original wait.
    stdout.close()
    stderr.close()
    assert len(output) == size and len(diagnostics) == size


def test_reader_error_is_preserved_after_queued_bytes():
    failure = OSError("original read failure")

    class BrokenStream(io.BytesIO):
        def read(self, count):
            if self.tell() == 0:
                return super().read(count)
            raise failure

    reader = PipeReader(BrokenStream(b"partial"))
    try:
        assert reader.read(deadline=time.monotonic() + 10) == b"partial"
        for _ in range(2):
            with pytest.raises(OSError) as caught:
                reader.read(deadline=time.monotonic() + 10)
            assert caught.value is failure
    finally:
        reader.close()


def test_writer_preserves_broken_pipe_error_after_child_exit(children):
    process, own = children("pass")
    writer = own(PipeWriter(process.stdin))
    assert process.wait(timeout=10) == 0
    with pytest.raises(BrokenPipeError):
        writer.write(b"command\n", deadline=time.monotonic() + 10)


def test_completed_write_does_not_recheck_child_liveness(children, monkeypatch):
    process, own = children("import sys; sys.stdin.buffer.readline(); print('terminal', flush=True)")
    writer = own(PipeWriter(process.stdin))
    reader = own(PipeReader(process.stdout))
    put = writer._jobs.put

    def submit(job, **kwargs):
        put(job, **kwargs)
        assert job[2].wait(10)
        assert process.wait(timeout=10) == 0

    def guard():
        if process.poll() is not None:
            raise RuntimeError("child exited before command")

    monkeypatch.setattr(writer._jobs, "put", submit)
    writer.write(b"command\n", deadline=time.monotonic() + 20, guard=guard)
    output = bytearray()
    deadline = time.monotonic() + 10
    while chunk := reader.read(deadline=deadline):
        output.extend(chunk)
    assert output == b"terminal\n"


def test_all_workers_are_joined_when_one_close_fails():
    from astrid.core.execution.pipe_io import close_pipe_workers

    closed = []
    failure = RuntimeError("reader remains live")

    class Worker:
        def __init__(self, name):
            self.name = name

        def close(self, timeout):
            closed.append((self.name, timeout))
            if self.name == "first":
                raise failure

    with pytest.raises(RuntimeError) as caught:
        close_pipe_workers(Worker("first"), Worker("second"), timeout=.1)
    assert caught.value is failure
    assert closed == [("first", .1), ("second", .1)]


def test_close_reports_a_blocked_read_with_a_bounded_join():
    reading, finished = threading.Event(), threading.Event()

    class Stream:
        def read(self, count):
            reading.set()
            finished.wait()
            return b""

    reader = PipeReader(Stream())
    try:
        assert reading.wait(10)
        with pytest.raises(RuntimeError, match="pipe reader remains live after close"):
            reader.close(timeout=.01)
        assert reader._thread.daemon
    finally:
        finished.set()
        reader.close(timeout=10)


def test_close_unblocks_a_reader_waiting_on_a_full_queue(children):
    process, own = children("import os; os.write(1, b'x' * (4 * 1024 * 1024))")
    events = queue.Queue(maxsize=1)
    reader = own(PipeReader(process.stdout, chunk_size=1, events=events))
    # One returned event proves the reader started; it will refill the queue.
    PipeReader.receive(events, deadline=time.monotonic() + 10)
    process.kill()
    process.wait(timeout=10)
    reader.close()
    assert not reader._thread.is_alive()


def test_wan_partial_frames_survive_observation_timeout_and_coalesced_frames(children):
    from astrid.core.execution.generic_host import _ManagedWanChildAdapter

    process, own = children("""import os, sys
os.write(1, b'{"birth":"birth",')
sys.stdin.buffer.readline()
os.write(1, b'"kind":"ready"}\\n{"birth":"birth","kind":"result"}\\n')
""")
    adapter = _ManagedWanChildAdapter(spec={}, binding=SimpleNamespace(process_birth_id="birth"),
                                     readiness_timeout=10, release_timeout=10,
                                     track=lambda p: None, untrack=lambda p: None)
    adapter.process = process
    adapter._pipe_reader = own(PipeReader(process.stdout))
    adapter._pipe_writer = own(PipeWriter(process.stdin))
    with pytest.raises(TimeoutError, match="Wan child observation timed out"):
        adapter._read(time.monotonic() + .2)
    adapter._write({"continue": True}, time.monotonic() + 10)
    assert adapter._read(time.monotonic() + 10) == {"birth": "birth", "kind": "ready"}
    assert adapter._read(time.monotonic() + 10) == {"birth": "birth", "kind": "result"}


def test_wan_fence_interrupts_a_silent_read(children):
    from astrid.core.execution.generic_host import HostError, _ManagedWanChildAdapter

    process, own = children("import sys; sys.stdin.buffer.read()")
    adapter = _ManagedWanChildAdapter(spec={}, binding=SimpleNamespace(process_birth_id="birth"),
                                     readiness_timeout=10, release_timeout=10,
                                     track=lambda p: None, untrack=lambda p: None)
    adapter.process = process
    adapter._pipe_reader = own(PipeReader(process.stdout))
    timer = threading.Timer(.1, lambda: adapter.fence(reason="test"))
    timer.start()
    try:
        with pytest.raises(HostError, match="cancelled or fenced"):
            adapter._read(time.monotonic() + 10)
    finally:
        timer.cancel()
        timer.join()


def test_remotion_parser_reads_fragmented_json_until_eof(children):
    from astrid.core.execution.remotion_host_session import _PipeReader, _read

    value = {"ok": True, "output": "é" * 8192}
    process, own = children("import json, os; os.write(1, json.dumps({'ok': True, 'output': chr(233) * 8192}).encode() + b'\\n')")
    reader = own(PipeReader(process.stdout, chunk_size=7))
    assert _read(_PipeReader(reader, timeout=10)) == value
    with pytest.raises(EOFError, match="renderer lease channel closed"):
        _read(_PipeReader(reader, timeout=10))


def test_remotion_exchange_shares_write_and_response_deadline(children, monkeypatch):
    from astrid.core.execution import remotion_host_session as host

    process, own = children("import sys; sys.stdin.buffer.readline(); sys.stdin.buffer.read()")
    manager = host.HostRemotionSession(track=lambda p: None, untrack=lambda p: None)
    manager.process = process
    manager._stdout_reader = own(PipeReader(process.stdout))
    manager._stdin_writer = own(PipeWriter(process.stdin))
    monkeypatch.setattr(host, "observe_tree", lambda p: {})
    monkeypatch.setattr(manager, "_guard", lambda: None)
    with pytest.raises(TimeoutError, match="original Remotion worker response timed out"):
        manager._exchange({"type": "render"}, timeout=.1)


def test_remotion_guard_interrupts_a_response_read(children, monkeypatch):
    from astrid.core.execution import remotion_host_session as host

    process, own = children("import sys; sys.stdin.buffer.readline(); sys.stdin.buffer.read()")
    manager = host.HostRemotionSession(track=lambda p: None, untrack=lambda p: None)
    manager.process = process
    manager._stdout_reader = own(PipeReader(process.stdout))
    manager._stdin_writer = own(PipeWriter(process.stdin))
    monkeypatch.setattr(host, "observe_tree", lambda p: {})

    def guard():
        if cancelled.is_set():
            raise RuntimeError("renderer cancelled")

    cancelled = threading.Event()
    monkeypatch.setattr(manager, "_guard", guard)
    timer = threading.Timer(.1, cancelled.set)
    timer.start()
    try:
        with pytest.raises(RuntimeError, match="renderer cancelled"):
            manager._exchange({"type": "render"}, timeout=10)
    finally:
        timer.cancel()
        timer.join()


def test_remotion_census_failure_retains_custody_but_joins_pipe_workers(children, monkeypatch):
    from astrid.core.execution import remotion_host_session as host

    process, own = children("pass")
    manager = host.HostRemotionSession(track=lambda p: None, untrack=lambda p: None)
    manager.process = process
    reader = manager._stdout_reader = own(PipeReader(process.stdout))
    writer = manager._stdin_writer = own(PipeWriter(process.stdin))
    assert process.wait(timeout=10) == 0
    failure = RuntimeError("native census unavailable")

    def verify(process):
        raise failure

    monkeypatch.setattr(host, "verify_tree_absent", verify)
    with pytest.raises(RuntimeError) as caught:
        manager._close_original(already_exited=True)
    assert caught.value is failure and manager.uncertain is failure
    assert manager.process is process
    assert not reader._thread.is_alive() and not writer._thread.is_alive()


def test_wan_census_failure_retains_custody_but_joins_pipe_workers(children, monkeypatch):
    from astrid.core.execution import generic_host as host

    process, own = children("pass")
    adapter = host._ManagedWanChildAdapter(spec={}, binding=SimpleNamespace(process_birth_id="birth"),
                                          readiness_timeout=10, release_timeout=10,
                                          track=lambda p: None, untrack=lambda p: None)
    adapter.process = process
    reader = adapter._pipe_reader = own(PipeReader(process.stdout))
    writer = adapter._pipe_writer = own(PipeWriter(process.stdin))
    assert process.wait(timeout=10) == 0
    monkeypatch.setattr(host, "_release_owned_group", lambda process: None)
    monkeypatch.setattr(host, "_process_snapshot", lambda: {})
    with pytest.raises(host.HostError, match="release census unavailable"):
        adapter.release(reason="test")
    assert adapter.process is process
    assert not reader._thread.is_alive() and not writer._thread.is_alive()


def test_retired_remotion_child_never_reuses_failed_pipe_workers(children, monkeypatch):
    from astrid.core.execution import remotion_host_session as host

    process, own = children("pass")
    manager = host.HostRemotionSession(track=lambda p: None, untrack=lambda p: None)
    manager.process = process
    manager.uncertain = RuntimeError("old child uncertain")
    writer = manager._stdin_writer = own(PipeWriter(process.stdin))
    assert process.wait(timeout=10) == 0
    monkeypatch.setattr(host, "terminate_tree", lambda *a, **kw: None)
    monkeypatch.setattr(host, "verify_tree_absent", lambda process: None)
    monkeypatch.setattr(host, "observe_tree", lambda process: {})
    close = writer.close

    def failed_close(timeout):
        close(timeout=timeout)
        raise RuntimeError("old pipe close uncertain")

    with monkeypatch.context() as patch:
        patch.setattr(writer, "close", failed_close)
        manager._retire_uncertain()
    assert manager.process is None and manager.uncertain is None
    process, own = children("import sys; sys.stdin.buffer.readline(); print('{\"ok\":true}', flush=True)")
    manager.process = process
    manager._stdout_reader = own(PipeReader(process.stdout))
    try:
        assert manager._exchange({"type": "render"}, timeout=10) == {"ok": True}
    finally:
        own(manager._stdin_writer)
