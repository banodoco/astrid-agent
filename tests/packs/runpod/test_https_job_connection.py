"""One claim-scoped HTTPS client, with the existing cancellation/deadline fences."""
import base64
import hashlib
import json
import multiprocessing
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from threading import Event, Thread
from types import SimpleNamespace

import httpx
import pytest

from astrid.core.execution.machine_run import MachineRunCancelled
from astrid.packs.runpod.remote_placement import RemotePlacementError, _HttpsConnection, _HttpsOps
from tests.packs.runpod.test_generation_preparation import retained as retained
from tests.packs.runpod.test_job_on_claimed_machine import claim, job, run
from tests.packs.runpod.test_machine import machine_case as machine_case
from tests.packs.runpod.test_remote_placement import sources as sources
from tests.packs.runpod.test_target_selection import case as case
from tests.packs.runpod.test_warm_session import lease_selection_case as lease_selection_case
from tests.packs.runpod.test_warm_session import small_lease as small_lease


def test_warm_job_constructs_one_client_and_closes_it(request, monkeypatch):
    from runpod_lifecycle import https

    c = request.getfixturevalue("machine_case")
    claim(c)
    run(c, job(c))  # Leave a prepared warm machine.
    transport = https.HttpsPodTransport
    clients = []
    client = httpx.Client

    def counted_client(*args, **kwargs):
        result = client(*args, **kwargs)
        clients.append(result)
        return result

    class CountedTransport(transport):
        def __init__(self, *args, **kwargs):
            super().__init__(*args, **kwargs)
            self.http = httpx.Client(transport=httpx.MockTransport(
                lambda request: httpx.Response(200)))

        def disconnect(self):
            self.http.close()

    monkeypatch.setattr(httpx, "Client", counted_client)
    monkeypatch.setattr(https, "HttpsPodTransport", CountedTransport)
    for number in (2, 3):
        clients.clear()
        run(c, job(c, number))
        # W-C: the deferred reset owner holds the job's connection and closes it last.
        c.host.wait_for_reset()
        # One client for claim, shipping, polls, fetch and release; the deferred reset's owner
        # thread opens at most one more, off the caller's path (integration ruling, W-E + W-C).
        assert 1 <= len(clients) <= 2, len(clients)
        assert all(made.is_closed for made in clients), "every client closes even when the pod stays warm"
        lease = c.read()["lease"]
        assert lease["state"] == "idle" and lease["reset"]["state"] == "ready", lease


@pytest.fixture
def agent():
    state = SimpleNamespace(connections=0, requests=[], mode="ok", entered=Event(), unblock=Event(),
                            abort=None, chunks=[], deleted=[], command_id=None)

    class Agent(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def setup(self):
            super().setup()
            state.connections += 1

        def do_POST(self):
            request = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            state.requests.append(request)
            state.entered.set()
            if state.mode == "stall":
                state.unblock.wait(10)
            if state.mode == "uncertain":
                self.send_response(403)
                self.send_header("Content-Length", "0")
                self.end_headers()
                return
            if self.path == "/jobs":
                state.command_id = request["id"]
                self.reply({"id": request["id"]})
                return
            if self.path == "/uploads":
                self.reply({"id": "a" * 32, "offset": 0, "complete": False})
                return
            path = request["path"]
            data = (b"exited" if path.endswith("/status") else b"0" if path.endswith("/exit")
                    else b"123" if path.endswith("/pid") else b"output")
            self.reply({"offset": 0, "total": len(data), "etag": "fixed",
                        "data": base64.b64encode(data).decode(),
                        "sha256": hashlib.sha256(data).hexdigest()})

        def do_PUT(self):
            size = len(self.rfile.read(int(self.headers["Content-Length"])))
            state.chunks.append(size)
            state.abort.set()
            self.reply({"offset": sum(state.chunks), "complete": False})

        def do_GET(self):
            assert self.path.startswith("/jobs/")
            state.abort.set()
            self.reply({"status": "running", "stdout": "", "stderr": "",
                        "stdout_offset": 0, "stderr_offset": 0,
                        "stdout_eof": True, "stderr_eof": True})

        def do_DELETE(self):
            state.deleted.append(self.path)
            self.reply({})

        def reply(self, value):
            body = json.dumps(value).encode()
            try:
                self.send_response(200)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
            except (BrokenPipeError, ConnectionResetError):
                pass

        def log_message(self, *_):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Agent)
    Thread(target=server.serve_forever, daemon=True).start()
    state.ops = _HttpsOps("pod1", "x" * 32, connection=_HttpsConnection(),
                          base_url=f"http://127.0.0.1:{server.server_port}")
    try:
        yield state
    finally:
        state.unblock.set()
        state.ops.close()
        server.shutdown()
        server.server_close()


def test_real_worker_keeps_one_connection_through_bounded_views_and_threads(agent, tmp_path):
    ops = agent.ops
    before = {child.pid for child in multiprocessing.active_children()}
    ops.get("/output", tmp_path / "first")
    child = ops._connection.observer[0]
    observer = ops.bounded(time.monotonic() + 5)
    assert observer.job_poll("a" * 32)["state"] == "exited"
    errors = []

    def fetch():
        try:
            ops.get("/output", tmp_path / "second")
        except (RemotePlacementError, OSError) as error:
            errors.append(error)

    thread = Thread(target=fetch)
    thread.start()
    thread.join(5)
    assert not thread.is_alive() and not errors
    assert (tmp_path / "second").read_bytes() == b"output"
    assert agent.connections == 1
    assert ops._connection.observer[0] is child
    ops.close()
    assert {child.pid for child in multiprocessing.active_children()} == before
    with pytest.raises(RemotePlacementError, match="closed"):
        observer.job_poll("a" * 32)


@pytest.mark.parametrize("failure", ["cancel", "deadline", "uncertain"])
def test_shared_client_failure_is_bounded_and_next_operation_reconnects(agent, tmp_path, failure):
    ops = agent.ops
    ops.get("/output", tmp_path / "first")  # The failure uses an already warm connection.
    agent.entered.clear()
    agent.mode = "uncertain" if failure == "uncertain" else "stall"
    abort = Event()
    timer = None
    if failure == "cancel":
        view = ops.cancellable(abort.is_set)
        # Wait for the reused connection to enter the stalled request before cancelling.
        def cancel():
            if agent.entered.wait(3):
                abort.set()
        timer = Thread(target=cancel)
        timer.start()
        expected = MachineRunCancelled
    else:
        view = ops.bounded(time.monotonic() + (1 if failure == "deadline" else 5))
        expected = RemotePlacementError
    started = time.monotonic()
    try:
        with pytest.raises(expected) as caught:
            view.job_poll("a" * 32)
        assert agent.entered.is_set() and agent.connections == 1
        assert time.monotonic() - started < (1.3 if failure == "deadline" else 2)
        if failure == "deadline":
            assert caught.value.reason.code == "deadline_exceeded"
        assert ops._connection.observer is None
    finally:
        agent.unblock.set()
        if timer is not None:
            timer.join(3)
    agent.mode = "ok"
    ops.get("/output", tmp_path / "after")
    assert agent.connections == 2
    assert (tmp_path / "after").read_bytes() == b"output"


def test_shared_direct_client_checks_every_upload_chunk_and_removes_hooks(tmp_path, monkeypatch):
    from runpod_lifecycle.https import CHUNK, HttpsPodTransport

    abort = Event()
    chunks = []

    def upload(request):
        if request.method == "POST":
            return httpx.Response(200, json={"id": "a" * 32, "offset": 0, "complete": False})
        chunks.append(len(request.content))
        abort.set()
        return httpx.Response(200, json={"offset": CHUNK, "complete": False})

    transport = HttpsPodTransport("pod1", "x" * 32, base_url="http://localhost")
    transport.http.close()
    transport.http = httpx.Client(base_url="http://localhost", transport=httpx.MockTransport(upload))
    ops = _HttpsOps("pod1", "x" * 32, connection=_HttpsConnection())
    view = ops.cancellable(abort.is_set)
    monkeypatch.setattr(view, "_transport", lambda: transport)
    local = tmp_path / "input"
    local.write_bytes(b"x" * (3 * CHUNK))
    with pytest.raises(MachineRunCancelled):
        view._call_direct("put", local, "/input")
    assert chunks == [CHUNK]
    assert transport.http.event_hooks["request"] == []
    assert transport.http.is_closed and ops._connection.transport is None


def test_waiting_for_shared_client_still_obeys_deadline(agent, tmp_path):
    ops = agent.ops
    ops.get("/output", tmp_path / "first")
    agent.mode = "stall"
    agent.entered.clear()
    abort = Event()
    errors = []

    def fetch():
        try:
            ops.cancellable(abort.is_set).get("/output", tmp_path / "blocked")
        except MachineRunCancelled as error:
            errors.append(error)

    thread = Thread(target=fetch)
    thread.start()
    try:
        assert agent.entered.wait(3)
        started = time.monotonic()
        with pytest.raises(RemotePlacementError) as caught:
            ops.bounded(started + 0.2).job_poll("a" * 32)
        assert caught.value.reason.code == "deadline_exceeded"
        assert time.monotonic() - started < 0.4
    finally:
        abort.set()
        thread.join(3)
        agent.unblock.set()
    assert not thread.is_alive() and len(errors) == 1


@pytest.mark.parametrize("operation", ["put", "exec"])
def test_real_worker_checks_host_cancellation_at_every_request_and_stops_commands(agent, tmp_path, operation):
    from runpod_lifecycle.https import CHUNK

    ops = agent.ops
    ops.get("/output", tmp_path / "first")
    agent.abort = Event()
    local = tmp_path / "input"
    local.write_bytes(b"x" * (3 * CHUNK))
    started = time.monotonic()
    with pytest.raises(MachineRunCancelled):
        view = ops.cancellable(agent.abort.is_set)
        if operation == "put":
            view.put(local, "/input")
        else:
            view.exec("command-with-a-supervised-agent-job", timeout=5)
    assert time.monotonic() - started < 1.5
    assert agent.connections == 1
    if operation == "put":
        assert agent.chunks == [CHUNK]
    else:
        assert agent.deleted == ["/jobs/" + agent.command_id]


def test_reused_transport_gets_each_views_exact_deadline_and_scoped_hook(monkeypatch):
    from runpod_lifecycle.https import HttpsPodTransport

    transport = HttpsPodTransport("pod1", "x" * 32, base_url="http://localhost")
    seen = []
    transport.execute_command = lambda *args: seen.append(
        (transport.deadline, len(transport.http.event_hooks["request"]))) or (0, "ok", "")
    ops = _HttpsOps("pod1", "x" * 32, connection=_HttpsConnection())
    monkeypatch.setattr(_HttpsOps, "_transport", lambda self: transport)
    monkeypatch.setattr(_HttpsOps, "_bounded_call", _HttpsOps._call_direct)
    try:
        ops.cancellable(lambda: False).exec("first")
        assert transport.http.event_hooks["request"] == []
        deadline = time.monotonic() + 10
        ops.bounded(deadline).exec("bounded")
        ops.exec("last")
        assert seen == [(None, 1), (deadline, 0), (None, 0)]
        assert not transport.http.is_closed
    finally:
        ops.close()
    assert transport.http.is_closed
