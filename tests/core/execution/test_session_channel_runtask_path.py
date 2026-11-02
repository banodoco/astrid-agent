"""M2-11a: per-run session channel, exercised through the real GenericPackHost.run_task path.

The python action runs in the real generic host worker (real spawn, real SessionClient,
real SessionChannelBroker, real _HostSessionChannel serving thread). The only observation
points are pass-through wrappers that record what run_task hands to invoke_capability and
to the process launch; nothing is replaced with a fake.
"""
from __future__ import annotations

import json
import socket
import time
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest
import yaml

from astrid.core.execution import generic_host as gh
from astrid.core.execution import session_channel as session_channel_module
from astrid.core.execution.generic_host import GenericPackHost
from tests.core.execution.session_test_profiles import register_test_profile
from tests.core.execution.test_generic_host_actions_v3 import pack, task_for

_REQUEST_NAME = ".astrid-capability-request.json"
_TASK_ID = "python-task"


@pytest.fixture(autouse=True)
def isolated(monkeypatch, tmp_path):
    monkeypatch.setenv("ASTRID_SOURCE_STATE", str(tmp_path / "absent-state.json"))
    monkeypatch.delenv("ASTRID_PACKS_PATH", raising=False)


@pytest.fixture
def observed(monkeypatch):
    """Pass-through recorders on the real invoke_capability, launch, and broker."""
    rec = SimpleNamespace(invoke=[], spawns=[], brokers=[], frames=[], fail_invoke=None)
    real_invoke = GenericPackHost.invoke_capability
    real_spawn = gh.spawn_task_scope
    RealBroker = session_channel_module.SessionChannelBroker

    def invoke_spy(self, **kwargs):
        channel = kwargs.get("run_channel")
        # Capture the fd number while the child socket is still open.
        rec.invoke.append({**kwargs, "run_channel_fd": None if channel is None else channel.fileno()})
        if rec.fail_invoke is not None:
            raise rec.fail_invoke
        return real_invoke(self, **kwargs)

    def spawn_spy(argv, **kwargs):
        request = next(str(arg) for arg in argv if str(arg).endswith(_REQUEST_NAME))
        # The request file is written before launch and unlinked only after the child exits.
        with open(request, encoding="utf-8") as handle:
            payload = json.loads(handle.read())
        rec.spawns.append({"pass_fds": tuple(kwargs.get("pass_fds", ())), "payload": payload})
        return real_spawn(argv, **kwargs)

    class RecordingBroker(RealBroker):
        def __init__(self, *args, **kwargs):
            super().__init__(*args, **kwargs)
            rec.brokers.append(self)

        def serve_one(self):
            frame = super().serve_one()
            rec.frames.append(frame)
            return frame

    monkeypatch.setattr(GenericPackHost, "invoke_capability", invoke_spy)
    monkeypatch.setattr(gh, "spawn_task_scope", spawn_spy)
    monkeypatch.setattr(session_channel_module, "SessionChannelBroker", RecordingBroker)
    return rec


def _host(tmp_path, *, code, session=None):
    """Real pack with one python action; optionally declare `session` on it."""
    root, _, runtime, _, _ = pack(tmp_path, code=code)
    if session is not None:
        manifest = yaml.safe_load((root / "pack.yaml").read_text(encoding="utf-8"))
        manifest["actions"]["calculate"]["session"] = session
        (root / "pack.yaml").write_text(yaml.safe_dump(manifest), encoding="utf-8")
    host = GenericPackHost(pack_roots=[root], client=runtime)
    host.discover()
    host.register()
    # Test-only instance registration so run_task's profile lookup passes; the
    # invoke edge is spied in these tests and must never be reached here.
    manifest = yaml.safe_load((root / "pack.yaml").read_text(encoding="utf-8"))
    register_test_profile(host, manifest["id"], lambda *a, **k: {"result": {"success": True}})
    return host, runtime


DECLARED_FD_CODE = (
    "def run(value='default', session=None):\n"
    "    return {'fd': session._sock.fileno(), 'value': value}\n"
)

PROGRESS_CODE = (
    "def run(value='default', session=None):\n"
    "    return {'fd': session._sock.fileno(), 'progress': session.progress()}\n"
)

UNDECLARED_CODE = "def run(value='default'):\n    return {'value': value}\n"


def _assert_closed(child: socket.socket) -> None:
    with pytest.raises(OSError):
        child.send(b"x")


def test_declared_session_action_gets_channel_and_inherits_exact_fd(tmp_path, observed):
    host, runtime = _host(tmp_path, code=DECLARED_FD_CODE, session="managed_tool")
    assert host.run_task(task_for(runtime), lease_token="lease")["task"]["status"] == "completed"

    assert len(observed.invoke) == 1
    kwargs = observed.invoke[0]
    child = kwargs["run_channel"]
    assert child is not None
    assert kwargs["run_channel_run_id"] == _TASK_ID

    fd = kwargs["run_channel_fd"]
    assert fd is not None and fd >= 3
    assert len(observed.spawns) == 1
    spawn = observed.spawns[0]
    assert spawn["pass_fds"] == (fd,)
    assert spawn["payload"]["run_channel_fd"] == fd
    assert spawn["payload"]["run_channel_run_id"] == _TASK_ID

    # The real worker process saw the same fd number as an open socket it was handed.
    action_result = runtime.settlements[0][2]["result"]["action_result"]
    assert action_result["fd"] == fd
    assert action_result["value"] == "default"


def test_undeclared_python_action_gets_no_channel(tmp_path, observed):
    host, runtime = _host(tmp_path, code=UNDECLARED_CODE)
    assert host.run_task(task_for(runtime), lease_token="lease")["task"]["status"] == "completed"

    assert len(observed.invoke) == 1
    assert observed.invoke[0]["run_channel"] is None
    assert observed.invoke[0]["run_channel_run_id"] is None
    assert len(observed.spawns) == 1
    assert observed.spawns[0]["pass_fds"] == ()
    assert "run_channel_fd" not in observed.spawns[0]["payload"]
    assert "run_channel_run_id" not in observed.spawns[0]["payload"]
    assert observed.brokers == []


def test_broker_and_child_closed_after_success(tmp_path, observed):
    host, runtime = _host(tmp_path, code=DECLARED_FD_CODE, session="managed_tool")
    assert host.run_task(task_for(runtime), lease_token="lease")["task"]["status"] == "completed"

    assert len(observed.brokers) == 1
    assert observed.brokers[0].closed
    _assert_closed(observed.invoke[0]["run_channel"])


def test_broker_and_child_closed_when_invoke_raises(tmp_path, observed):
    host, runtime = _host(tmp_path, code=DECLARED_FD_CODE, session="managed_tool")
    observed.fail_invoke = RuntimeError("injected invoke_capability failure")
    with pytest.raises(RuntimeError, match="injected invoke_capability failure"):
        host.run_task(task_for(runtime), lease_token="lease")

    assert len(observed.invoke) == 1
    assert len(observed.brokers) == 1
    assert observed.brokers[0].closed
    _assert_closed(observed.invoke[0]["run_channel"])


def test_broker_and_child_closed_on_deadline_timeout(tmp_path, observed):
    host, runtime = _host(
        tmp_path,
        code="import time\ndef run(value='default', session=None):\n    time.sleep(60)\n    return {}\n",
        session="managed_tool",
    )
    task = task_for(runtime)
    deadline = datetime.now(timezone.utc) + timedelta(seconds=3)
    task["task"]["deadline_at"] = deadline.isoformat().replace("+00:00", "Z")
    started = time.monotonic()
    result = host.run_task(task, lease_token="lease")

    assert time.monotonic() - started < 50
    # run_task's deadline branch returns a flat mapping, not a "task" envelope.
    assert result == {"task_id": _TASK_ID, "status": "failed", "deadline_exceeded": True}
    assert len(observed.invoke) == 1
    assert len(observed.brokers) == 1
    assert observed.brokers[0].closed
    _assert_closed(observed.invoke[0]["run_channel"])


def test_channel_served_while_run_alive(tmp_path, observed):
    host, runtime = _host(tmp_path, code=PROGRESS_CODE, session="managed_tool")
    assert host.run_task(task_for(runtime), lease_token="lease")["task"]["status"] == "completed"

    # The worker's progress request was answered before the action returned.
    action_result = runtime.settlements[0][2]["result"]["action_result"]
    assert action_result["progress"] == {"state": "running", "snapshot": {}}

    # The broker sends the response in serve_one; the recorder appends right after.
    deadline = time.monotonic() + 2
    while not observed.frames and time.monotonic() < deadline:
        time.sleep(0.01)
    # A later error frame (worker closed its end) is expected; only the served op matters here.
    served = [frame for frame in observed.frames if frame["ok"]]
    assert len(served) == 1
    frame = served[0]
    assert frame["seq"] == 1
    assert frame["result"] == {"state": "running", "snapshot": {}}
    assert observed.brokers[0].closed
