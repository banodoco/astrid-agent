"""Host-side contract for the M2-11a per-run session channel.

The first group drives ``GenericPackHost.invoke_capability`` directly with
``spawn_task_scope`` stubbed, so no worker starts.  These tests are expected to
be red until the run_task hunk lands the ``run_channel`` wiring.

The last group exercises a helper that mirrors the intended run_task shape
(socketpair, broker, end_run and close in a finally) against the real
``SessionChannelBroker``.  Those tests do NOT prove run_task; they only pin the
broker lifecycle that run_task is expected to use.
"""

from __future__ import annotations

import io
import json
import os
import socket
from pathlib import Path
from typing import Any, Callable

import pytest

from astrid.core.execution import generic_host
from astrid.core.execution.generic_host import GenericPackHost
from astrid.core.execution.managed_tool_session import AdmissionToken
from astrid.core.execution.session_channel import SessionChannelBroker

RUN_ID = "run-m2-11a-host-wiring"
SENTINEL = "TOKEN-SENTINEL-7f3a"
CHILD_ENV = {"PATH": os.environ.get("PATH", "/usr/bin:/bin")}
REQUEST_NAME = ".astrid-capability-request.json"
RESULT_NAME = ".astrid-capability-result.json"


class _StubProcess:
    """Stands in for the Popen-like object returned by spawn_task_scope."""

    pid = 4_242_424  # never signalled: _terminate_process_group is stubbed too

    def __init__(self) -> None:
        self.returncode = 0
        self.stdout = io.BytesIO(b"")
        self.stderr = io.BytesIO(b"")

    def poll(self) -> int:
        return self.returncode

    def wait(self, timeout: float | None = None) -> int:
        return self.returncode


class _BridgeStub:
    """Minimal stand-in for a child bridge that contributes one inherited fd."""

    def __init__(self, fd: int) -> None:
        self._fd = fd
        self.launched_calls = 0

    def inherited_fd(self) -> int:
        return self._fd

    def launched(self) -> None:
        self.launched_calls += 1


def _invoke(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, name: str, *,
            run_channel: socket.socket | None = None,
            child_bridge: Any | None = None) -> dict[str, Any]:
    """Run invoke_capability once with a stubbed launcher and record what it sent."""
    captured: dict[str, Any] = {"terminated": []}

    def fake_spawn(argv, **kwargs):
        attempt_dir = Path(kwargs["cwd"])
        # invoke_capability deletes the request file in its finally block, so
        # the launcher stub is the only place it can be read.
        request_bytes = (attempt_dir / REQUEST_NAME).read_bytes()
        captured.update(
            argv=list(argv),
            env=dict(kwargs["env"]),
            pass_fds=tuple(kwargs.get("pass_fds", ())),
            payload_bytes=request_bytes,
            payload=json.loads(request_bytes.decode("utf-8")),
        )
        (attempt_dir / RESULT_NAME).write_text(
            json.dumps({"ok": True, "outputs": [], "payload": {}}), encoding="utf-8"
        )
        return _StubProcess()

    monkeypatch.setattr(generic_host, "spawn_task_scope", fake_spawn)
    monkeypatch.setattr(
        generic_host, "_terminate_process_group",
        lambda process, **_kwargs: captured["terminated"].append(process),
    )
    host = GenericPackHost(pack_roots=[tmp_path])
    attempt = tmp_path / name
    result = host.invoke_capability(
        capability_kind="executor",
        capability_id="test.session_channel_wiring",
        request={"note": "wiring"},
        attempt=attempt,
        child_env=CHILD_ENV,
        run_channel=run_channel,
        child_bridge=child_bridge,
    )
    captured["result"] = result
    assert captured.get("payload_bytes"), "launcher stub was not reached"
    assert len(captured["terminated"]) == 1, "stubbed process cleanup was not reached"
    return captured


def _leak_hits(blobs: dict[str, bytes], needles: list[bytes]) -> list[str]:
    return [f"{label} contains {needle[:24]!r}"
            for label, blob in blobs.items() for needle in needles if needle in blob]


@pytest.fixture
def channel_pair():
    parent, child = socket.socketpair()
    try:
        yield parent, child
    finally:
        for sock in (parent, child):
            try:
                sock.close()
            except OSError:
                pass


# -- 1. payload contract -----------------------------------------------------

def test_payload_gains_only_run_channel_fd_int_equal_to_child_fileno(tmp_path, monkeypatch, channel_pair):
    _parent, child = channel_pair
    baseline = _invoke(tmp_path, monkeypatch, "attempt-baseline")
    wired = _invoke(tmp_path, monkeypatch, "attempt-wired", run_channel=child)

    # The only new payload key is the fd number; everything else is unchanged.
    assert "run_channel_fd" in wired["payload"], "run_channel hunk missing: payload has no run_channel_fd"
    added = set(wired["payload"]) - set(baseline["payload"])
    removed = set(baseline["payload"]) - set(wired["payload"])
    assert added == {"run_channel_fd"}, f"unexpected new payload keys: {sorted(added)}"
    assert removed == set(), f"payload keys dropped: {sorted(removed)}"
    value = wired["payload"]["run_channel_fd"]
    assert type(value) is int and value == child.fileno()


def test_payload_has_no_token_admission_broker_or_handle_key_added(tmp_path, monkeypatch, channel_pair):
    _parent, child = channel_pair
    baseline = _invoke(tmp_path, monkeypatch, "attempt-baseline")
    wired = _invoke(tmp_path, monkeypatch, "attempt-wired", run_channel=child)

    assert "run_channel_fd" in wired["payload"], "run_channel hunk missing: payload has no run_channel_fd"
    # "admission" is a pre-existing key (value None when no admission is given),
    # so only keys that this change adds are judged here.
    new_keys = set(wired["payload"]) - set(baseline["payload"])
    forbidden = ("token", "admission", "broker", "handle")
    offending = sorted(k for k in new_keys for word in forbidden if word in k.lower())
    assert offending == [], f"forbidden payload keys added: {offending}"


# -- 2. pass_fds ---------------------------------------------------------------

def test_pass_fds_is_exactly_the_child_end_fd(tmp_path, monkeypatch, channel_pair):
    _parent, child = channel_pair
    # Pre-existing contributors to inherited_fds on this path: none. The source
    # channel needs _runtime_external_sources (unset here), the remotion lease
    # needs rendering.timeline_visualize, and child_bridge is None.
    wired = _invoke(tmp_path, monkeypatch, "attempt-wired", run_channel=child)
    assert wired["pass_fds"] == (child.fileno(),)


def test_pass_fds_with_child_bridge_adds_bridge_fd_alongside_channel(tmp_path, monkeypatch, channel_pair):
    _parent, child = channel_pair
    bridge_parent, bridge_child = socket.socketpair()
    try:
        bridge = _BridgeStub(bridge_child.fileno())
        wired = _invoke(tmp_path, monkeypatch, "attempt-wired",
                        run_channel=child, child_bridge=bridge)
        # The bridge fd was already passed before this change; the channel fd is added on top.
        assert sorted(wired["pass_fds"]) == sorted((child.fileno(), bridge_child.fileno()))
        assert len(wired["pass_fds"]) == 2
        assert bridge.launched_calls == 1
        assert "--child-bridge" in wired["argv"]
    finally:
        bridge_parent.close()
        bridge_child.close()


# -- 3. token and repr never leave the host ---------------------------------

def test_token_and_admission_repr_absent_from_payload_argv_env(tmp_path, monkeypatch, channel_pair):
    parent, child = channel_pair
    token = AdmissionToken(
        token_id=SENTINEL, session_id="session-m2-11a", capability_id="test.session_channel_wiring",
        invocation_id="inv-1", generation=1, binding_identity=("binding", SENTINEL),
    )
    broker = SessionChannelBroker(RUN_ID, FakeSession(), parent, admission_token=token)
    try:
        wired = _invoke(tmp_path, monkeypatch, "attempt-wired", run_channel=child)
    finally:
        broker.end_run()
        broker.close()

    assert "run_channel_fd" in wired["payload"], "run_channel hunk missing: payload has no run_channel_fd"
    assert SENTINEL in repr(token)  # the needle really is in the repr
    needles = [SENTINEL.encode(), repr(token).encode()]
    blobs = {
        "payload bytes": wired["payload_bytes"],
        "argv": "\0".join(wired["argv"]).encode("utf-8"),
        "env": "\0".join(f"{k}={v}" for k, v in wired["env"].items()).encode("utf-8"),
    }
    assert _leak_hits(blobs, needles) == []


def test_leak_scanner_detects_planted_sentinel():
    """Negative control: the scanner used above does catch a leaked token."""
    needles = [SENTINEL.encode()]
    assert _leak_hits({"argv": f"--x {SENTINEL}".encode()}, needles) != []
    assert _leak_hits({"env": b"A=1"}, needles) == []


# -- 4. broker lifecycle mirror (does NOT prove run_task) ----------------------

class FakeSession:
    """Three host operations behind the broker; records calls, no real work."""

    def __init__(self) -> None:
        self.cancel_calls: list[str] = []

    def submit(self, settings: dict[str, Any], timeout_seconds: float) -> dict[str, Any]:
        return {"result": {"success": True}, "native_error": None}

    def progress(self) -> dict[str, Any]:
        return {"step": 1}

    def cancel(self, reason: str) -> dict[str, Any]:
        self.cancel_calls.append(reason)
        return {"accepted": True}

    def validate_settings(self, settings: Any) -> None:
        return None


class RecordingBroker(SessionChannelBroker):
    """The real broker, with end_run/close calls recorded in order."""

    def __init__(self, *args: Any, events: list[str], **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self.events = events

    def end_run(self) -> None:
        self.events.append("end_run")
        super().end_run()

    def close(self) -> None:
        self.events.append("close")
        super().close()


def _mirror_run_lifecycle(body: Callable[[socket.socket], Any], events: list[str]) -> Any:
    """Shape run_task is expected to use: socketpair, broker, end_run and close in finally.

    This is a test helper. It mirrors the intended lifecycle; it is not run_task.
    """
    parent, child = socket.socketpair()
    broker = RecordingBroker(RUN_ID, FakeSession(), parent,
                             admission_token=AdmissionToken(
                                 token_id=SENTINEL, session_id="s", capability_id="c",
                                 invocation_id="i", generation=1, binding_identity=()),
                             events=events)
    try:
        return body(child)
    finally:
        broker.end_run()
        broker.close()
        child.close()


def test_broker_mirror_closes_on_success():
    events: list[str] = []
    result = _mirror_run_lifecycle(lambda child: "ok", events)
    assert result == "ok"
    assert events == ["end_run", "close"]


def test_broker_mirror_closes_on_exception():
    events: list[str] = []

    def body(child: socket.socket) -> None:
        raise RuntimeError("worker crashed")

    with pytest.raises(RuntimeError, match="worker crashed"):
        _mirror_run_lifecycle(body, events)
    assert events == ["end_run", "close"]


def test_broker_mirror_closes_on_timeout():
    events: list[str] = []

    def body(child: socket.socket) -> None:
        # Real timeout: nobody writes to the worker end, so recv must time out.
        child.settimeout(0.05)
        child.recv(1)

    with pytest.raises(TimeoutError):
        _mirror_run_lifecycle(body, events)
    assert events == ["end_run", "close"]
