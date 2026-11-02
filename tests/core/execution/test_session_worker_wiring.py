"""M2-11a worker-side session wiring contract.

Pins how the worker (``generic_host_worker.run``) hands the run's session
channel to a python action:

- the worker reads ``payload["run_channel_fd"]`` and builds a ``SessionClient``;
- the client is passed as the ``session=`` keyword ONLY to python actions whose
  function signature declares a parameter named ``session``;
- actions without that parameter are called exactly as before;
- a payload without the fd builds no client (the worker never invents one).

Tests 1 and 5 pin the threading itself and are red until the integrator threads
the client through the worker.  Tests 2 to 4 pin behaviour that already exists
(the broker, the client error mapping, the action call path) and must stay green.
"""

from __future__ import annotations

import json
import re
import socket
import sys
import threading
from pathlib import Path
from typing import Any

import pytest
import yaml

from astrid.core.execution import generic_host_worker
from astrid.core.execution import session_client as session_client_module
from astrid.core.execution.generic_host import GenericPackHost, _json_safe
from astrid.core.execution.session_channel import (
    SessionChannelBroker,
    SessionChannelClosed,
)
from astrid.core.execution.session_client import SessionChannelError, SessionClient
from tests.test_generic_host import FakeRuntime

RUN_ID = "run-m2-11a-luna-b"


@pytest.fixture(autouse=True)
def _internal_invocation(monkeypatch):
    monkeypatch.setenv("ASTRID_INTERNAL_INVOCATION", "1")
    monkeypatch.setenv("ASTRID_SOURCE_STATE", "/nonexistent-astrid-source-state.json")
    monkeypatch.delenv("ASTRID_PACKS_PATH", raising=False)


# --------------------------------------------------------------------------
# Pack fixture: a tiny admitted python action whose function is named ``echo``.
# --------------------------------------------------------------------------

def _admitted_echo_pack(tmp_path: Path, code: str):
    # The worker's pack import finder binds one pack id per process, so each
    # test uses its own id (and therefore its own capability id).
    pack_id = "m211a_" + tmp_path.name.lower().replace("test_", "").replace("-", "_")[:24]
    pack_id = re.sub(r"[^a-z0-9_]", "_", pack_id)
    root = tmp_path / "pack" / pack_id
    source = root / "actions" / "echo.py"
    source.parent.mkdir(parents=True, exist_ok=True)
    source.write_text(code)
    declaration = {
        "description": "Echo action used by the M2-11a worker wiring tests.",
        "invocation": {"kind": "python", "path": "actions/echo.py", "function": "echo"},
        "inputs": [{"name": "message", "type": "string", "required": True}],
        "outputs": {},
        "metadata": {"adapter_family": "cpu", "resource_keys": ["cpu"], "project_scope": "optional"},
    }
    (root / "pack.yaml").write_text(yaml.safe_dump({
        "schema_version": 3, "pack_api": "1.0", "id": pack_id, "name": pack_id,
        "version": "1.0.0", "actions": {"echo": declaration},
    }))
    host = GenericPackHost(pack_roots=[root], client=FakeRuntime())
    host.discover()
    capability_id = f"{pack_id}.echo"
    definition, admission = host.admit(capability_kind="executor", capability_id=capability_id)
    return definition, admission, capability_id


def _echo_source(record: Path, signature: str) -> str:
    """Action source that records what it received, then returns."""
    return (
        "import json\n"
        "from pathlib import Path\n"
        f"RECORD = Path({str(record)!r})\n"
        f"def echo({signature}):\n"
        "    session_type = type(session).__qualname__ if session is not None else None\n"
        "    session_module = type(session).__module__ if session is not None else None\n"
        "    RECORD.write_text(json.dumps({'message': message, 'session_type': session_type,\n"
        "                                  'session_module': session_module}))\n"
        "    return {'echo': message}\n"
    )


def _run_worker(tmp_path: Path, definition, admission, capability_id: str, *, message: str, run_channel_fd: int | None):
    """Drive the real worker entrypoint ``generic_host_worker.run`` in-process."""
    result_path = tmp_path / "result.json"
    payload: dict[str, Any] = {
        "remotion_host_fd": None,
        "capability_kind": "executor",
        "capability_id": capability_id,
        "request": _json_safe({
            "inputs": {"message": message},
            "out": str(tmp_path / "attempt" / "outputs"),
            "project": "demo",
            "project_was_auto_resolved": True,
            "python_exec": sys.executable,
            "verbose": True,
            "dry_run": False,
            "invocation": "runtime",
        }),
        "definition": _json_safe(definition),
        "admission": _json_safe(admission),
        "result_path": str(result_path),
    }
    if run_channel_fd is not None:
        payload["run_channel_fd"] = run_channel_fd
    request_path = tmp_path / "request.json"
    request_path.write_text(json.dumps(payload, sort_keys=True), encoding="utf-8")
    code = generic_host_worker.run(request_path)
    return code, json.loads(result_path.read_text(encoding="utf-8"))


# --------------------------------------------------------------------------
# 1. A python action declaring ``session`` receives a SessionClient.
# --------------------------------------------------------------------------

def test_action_declaring_session_receives_session_client(tmp_path):
    record = tmp_path / "record.json"
    definition, admission, capability_id = _admitted_echo_pack(
        tmp_path, _echo_source(record, "message, session=None"),
    )
    parent, child = socket.socketpair()
    fd = child.detach()  # the worker's SessionClient takes ownership of this fd
    try:
        code, result = _run_worker(tmp_path, definition, admission, capability_id, message="hello", run_channel_fd=fd)
    finally:
        parent.close()

    assert code == 0, result
    assert result["ok"] is True
    seen = json.loads(record.read_text(encoding="utf-8"))
    assert seen["message"] == "hello"
    # The worker, not the action, builds the client; it must be the SessionClient class.
    assert seen["session_type"] == "SessionClient", (
        f"action received {seen['session_type']!r} from {seen['session_module']!r}; "
        "expected a SessionClient built by the worker from payload['run_channel_fd']"
    )
    assert seen["session_module"] == session_client_module.__name__


# --------------------------------------------------------------------------
# 2. An action WITHOUT a ``session`` parameter is called exactly as before.
# --------------------------------------------------------------------------

def test_action_without_session_parameter_is_called_unchanged(tmp_path):
    record = tmp_path / "record.json"
    # No ``session`` parameter at all: an injected kwarg would raise TypeError.
    source = (
        "import json\n"
        "from pathlib import Path\n"
        f"RECORD = Path({str(record)!r})\n"
        "def echo(message):\n"
        "    RECORD.write_text(json.dumps({'message': message}))\n"
        "    return {'echo': message}\n"
    )
    definition, admission, capability_id = _admitted_echo_pack(tmp_path, source)
    # Even with a channel fd present, this action must not receive a session kwarg.
    parent, child = socket.socketpair()
    fd = child.detach()
    try:
        code, result = _run_worker(tmp_path, definition, admission, capability_id, message="plain", run_channel_fd=fd)
    finally:
        parent.close()

    assert code == 0, result
    assert result["ok"] is True
    assert result["payload"]["action_result"] == {"echo": "plain"}
    assert json.loads(record.read_text(encoding="utf-8")) == {"message": "plain"}


# --------------------------------------------------------------------------
# 3. The broker refuses a raw ``shell`` op with not_allowed and closes; the
#    client surfaces that as SessionChannelError("not_allowed").
# --------------------------------------------------------------------------

class _FakeHostSession:
    def __init__(self) -> None:
        self.submit_calls = 0
        self.cancel_calls: list[str] = []

    def submit(self, settings, timeout_seconds):
        self.submit_calls += 1
        return {"result": {"success": True}}

    def progress(self):
        return {"step": 1}

    def cancel(self, reason):
        self.cancel_calls.append(reason)
        return {"accepted": True}

    def validate_settings(self, settings):
        return None


@pytest.fixture
def channel():
    parent, child = socket.socketpair()
    session = _FakeHostSession()
    broker = SessionChannelBroker(RUN_ID, session, parent, admission_token=object())
    client = SessionClient(child.detach(), RUN_ID, 2.0)
    yield broker, client, session
    client.close()
    broker.close()


def _serve_in_thread(broker):
    box: dict[str, Any] = {}

    def target():
        try:
            box["frame"] = broker.serve_one()
        except BaseException as exc:  # surfaced by the test body
            box["error"] = exc

    thread = threading.Thread(target=target, daemon=True)
    thread.start()
    return thread, box


def test_raw_shell_op_is_not_allowed_and_channel_closes(channel):
    broker, client, session = channel
    thread, box = _serve_in_thread(broker)

    # The public client exposes only submit/progress/cancel, so an off-list op
    # can only be produced by a raw request.  The client's mapping is under test.
    with pytest.raises(SessionChannelError) as excinfo:
        client._request("shell", {"cmd": "id"}, wait=2.0)
    thread.join(5)

    assert "error" not in box, box.get("error")
    assert box["frame"]["ok"] is False
    assert box["frame"]["error"]["code"] == "not_allowed"
    assert excinfo.value.code == "not_allowed"
    assert broker.closed
    assert session.submit_calls == 0 and session.cancel_calls == []

    # not_allowed closes the channel on the client too: nothing more is sent.
    with pytest.raises(SessionChannelError) as after:
        client.progress()
    assert after.value.code == "closed"


# --------------------------------------------------------------------------
# 4. A late request after end_run is refused: terminal, then closed.
# --------------------------------------------------------------------------

def test_late_request_after_end_run_is_refused_with_transport_code(channel):
    broker, client, _session = channel
    broker.end_run()
    thread, box = _serve_in_thread(broker)

    final = client.progress()
    thread.join(5)
    assert final["state"] == "terminal"
    assert box["frame"]["ok"] is True
    assert broker.closed

    # The broker has closed; it raises SessionChannelClosed and sends nothing more.
    with pytest.raises(SessionChannelClosed):
        broker.serve_one()

    # The worker's late request fails at the transport layer: the broker closed
    # the socket, so the client reads EOF (or a send/recv error) and reports
    # "transport" for progress. After that failure the client is closed.
    with pytest.raises(SessionChannelError) as late:
        client.progress()
    assert late.value.code == "transport"

    with pytest.raises(SessionChannelError) as reuse:
        client.progress()
    assert reuse.value.code == "closed"


# --------------------------------------------------------------------------
# 5. Without run_channel_fd the worker builds no client and never reads a token.
# --------------------------------------------------------------------------

def test_payload_without_run_channel_fd_builds_no_client(tmp_path, monkeypatch):
    record = tmp_path / "record.json"
    definition, admission, capability_id = _admitted_echo_pack(
        tmp_path, _echo_source(record, "message, session=None"),
    )

    constructed: list[tuple[Any, ...]] = []
    original_init = SessionClient.__init__

    def spy_init(self, *args, **kwargs):
        constructed.append(args)
        original_init(self, *args, **kwargs)

    monkeypatch.setattr(SessionClient, "__init__", spy_init)

    code, result = _run_worker(tmp_path, definition, admission, capability_id, message="no-fd", run_channel_fd=None)

    assert code == 0, result
    assert constructed == [], "worker built a SessionClient without payload['run_channel_fd']"
    seen = json.loads(record.read_text(encoding="utf-8"))
    # Pinned: the action sees its own default (None); the worker passes no client.
    assert seen == {"message": "no-fd", "session_type": None, "session_module": None}
