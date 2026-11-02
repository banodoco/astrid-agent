from __future__ import annotations

import json
import socket
from typing import Any

import pytest

from astrid.core.execution.generic_host import _send_activation_frame
from astrid.core.execution.session_channel import (
    SESSION_CHANNEL_VERSION,
    SessionChannelBroker,
    SessionChannelClosed,
)

RUN_ID = "run-m2-11a-1"
SENTINEL = "TOKEN-SENTINEL-7f3a91c2-do-not-leak"


class FakeSession:
    def __init__(self, *, submit_error: Exception | None = None) -> None:
        self.submit_calls: list[tuple[dict[str, Any], float]] = []
        self.cancel_calls: list[str] = []
        self.progress_calls = 0
        self.submit_error = submit_error

    def submit(self, inputs: dict[str, Any], timeout_seconds: float) -> dict[str, Any]:
        self.submit_calls.append((inputs, timeout_seconds))
        if self.submit_error is not None:
            raise self.submit_error
        return {"result": {"success": True, "errors": []}, "native_error": None}

    def progress(self) -> dict[str, Any]:
        self.progress_calls += 1
        return {"step": self.progress_calls, "percent": 42}

    def cancel(self, reason: str) -> dict[str, Any]:
        self.cancel_calls.append(reason)
        return {"accepted": True}

    def validate_settings(self, inputs: Any) -> None:
        if inputs.get("bad") is True:
            raise ValueError("bad settings")


class Harness:
    """Worker side of a socketpair plus a broker on the parent side."""

    def __init__(self, session: FakeSession | None = None) -> None:
        self.parent, self.child = socket.socketpair()
        self.session = session or FakeSession()
        self.broker = SessionChannelBroker(
            RUN_ID, self.session, self.parent, admission_token=SENTINEL
        )
        self.raw: list[bytes] = []

    def send(self, frame: Any) -> None:
        if isinstance(frame, dict):
            _send_activation_frame(self.child, frame)
        else:
            self.child.sendall(frame)

    def recv_line(self) -> bytes:
        buf = bytearray()
        while b"\n" not in buf:
            chunk = self.child.recv(4096)
            if not chunk:
                break
            buf.extend(chunk)
        line = bytes(buf)
        self.raw.append(line)
        return line

    def exchange(self, frame: Any) -> dict[str, Any]:
        self.send(frame)
        response = self.broker.serve_one()
        line = self.recv_line()
        assert line, "broker sent no frame"
        decoded = json.loads(line.decode("utf-8"))
        assert decoded == response
        return decoded

    def child_sees_eof(self) -> bool:
        self.child.settimeout(1.0)
        try:
            return self.child.recv(1) == b""
        except (ConnectionResetError, BrokenPipeError):
            return True
        finally:
            self.child.settimeout(None)

    def all_bytes(self) -> bytes:
        return b"".join(self.raw)

    def close(self) -> None:
        self.broker.close()
        self.child.close()


@pytest.fixture
def h():
    harness = Harness()
    yield harness
    harness.close()


def _req(seq: int, op: str, args: dict[str, Any] | None = None, run_id: str = RUN_ID, version: str = SESSION_CHANNEL_VERSION) -> dict[str, Any]:
    return {"version": version, "run_id": run_id, "seq": seq, "op": op, "args": args or {}}


SUBMIT_ARGS = {"inputs": {"prompt": "a fox"}, "timeout_seconds": 30}


def test_submit_success_forwards_args_and_returns_session_result(h):
    resp = h.exchange(_req(1, "submit", SUBMIT_ARGS))
    assert resp["ok"] is True
    assert resp["version"] == SESSION_CHANNEL_VERSION
    assert resp["run_id"] == RUN_ID
    assert resp["seq"] == 1
    assert resp["result"] == {"result": {"success": True, "errors": []}, "native_error": None}
    assert h.session.submit_calls == [({"prompt": "a fox"}, 30.0)]


def test_progress_running_returns_state_and_snapshot(h):
    resp = h.exchange(_req(1, "progress"))
    assert resp["ok"] is True
    assert resp["result"] == {"state": "running", "snapshot": {"step": 1, "percent": 42}}
    # Channel stays open for further requests.
    resp2 = h.exchange(_req(2, "progress"))
    assert resp2["result"]["snapshot"]["step"] == 2
    assert not h.broker.closed


def test_cancel_first_calls_session_once_and_second_is_idempotent(h):
    first = h.exchange(_req(1, "cancel", {"reason": "user"}))
    assert first["ok"] is True
    assert first["result"] == {"state": "cancel_requested"}
    second = h.exchange(_req(2, "cancel", {"reason": "user again"}))
    assert second["ok"] is True
    assert second["result"] == {"state": "already_cancelled"}
    assert h.session.cancel_calls == ["user"]
    assert not h.broker.closed


def test_unknown_op_is_not_allowed_and_closes(h):
    resp = h.exchange(_req(1, "shell", {"cmd": "id"}))
    assert resp["ok"] is False
    assert resp["error"]["code"] == "not_allowed"
    assert h.broker.closed
    assert h.child_sees_eof()


def test_bad_run_id_is_protocol_and_closes(h):
    resp = h.exchange(_req(1, "progress", run_id="other-run"))
    assert resp["ok"] is False
    assert resp["error"]["code"] == "protocol"
    assert resp["run_id"] == RUN_ID
    assert h.broker.closed
    assert h.child_sees_eof()


def test_bad_version_is_protocol_and_closes(h):
    resp = h.exchange(_req(1, "progress", version="astrid.session-channel/v0"))
    assert resp["error"]["code"] == "protocol"
    assert h.broker.closed


def test_first_seq_must_be_one(h):
    resp = h.exchange(_req(2, "progress"))
    assert resp["error"]["code"] == "protocol"
    assert h.broker.closed


def test_non_increasing_seq_is_protocol_and_closes(h):
    assert h.exchange(_req(1, "progress"))["ok"] is True
    resp = h.exchange(_req(1, "progress"))
    assert resp["ok"] is False
    assert resp["error"]["code"] == "protocol"
    assert h.broker.closed
    assert h.child_sees_eof()


def test_bool_seq_is_protocol(h):
    resp = h.exchange(_req(True, "progress"))
    assert resp["error"]["code"] == "protocol"
    assert h.broker.closed


def test_second_submit_is_submit_already_used_and_closes(h):
    # Decision: fail closed. Any error frame closes the channel, per the frozen
    # spec ("on any refusal ... the channel closes").
    assert h.exchange(_req(1, "submit", SUBMIT_ARGS))["ok"] is True
    resp = h.exchange(_req(2, "submit", SUBMIT_ARGS))
    assert resp["ok"] is False
    assert resp["error"]["code"] == "submit_already_used"
    assert len(h.session.submit_calls) == 1
    assert h.broker.closed


def test_invalid_settings_returns_error_without_calling_submit(h):
    args = {"inputs": {"bad": True}, "timeout_seconds": 30}
    resp = h.exchange(_req(1, "submit", args))
    assert resp["ok"] is False
    assert resp["error"]["code"] == "invalid_settings"
    assert h.session.submit_calls == []
    assert h.broker.closed


def test_non_object_settings_is_invalid_settings(h):
    resp = h.exchange(_req(1, "submit", {"inputs": ["x"], "timeout_seconds": 30}))
    assert resp["error"]["code"] == "invalid_settings"
    assert h.session.submit_calls == []


@pytest.mark.parametrize("timeout", [0, -1, True, "30"])
def test_bad_timeout_is_protocol_and_submit_not_called(h, timeout):
    resp = h.exchange(_req(1, "submit", {"inputs": {}, "timeout_seconds": timeout}))
    assert resp["error"]["code"] == "protocol"
    assert h.session.submit_calls == []


def test_submit_host_exception_is_host_error_and_leaks_no_token(h):
    h.session.submit_error = RuntimeError(f"engine exploded near {SENTINEL}")
    resp = h.exchange(_req(1, "submit", SUBMIT_ARGS))
    assert resp["ok"] is False
    assert resp["error"]["code"] == "host_error"
    assert h.broker.closed
    assert SENTINEL.encode() not in h.all_bytes()


def test_progress_after_end_returns_terminal_then_closes(h):
    assert h.exchange(_req(1, "progress"))["result"]["state"] == "running"
    h.broker.end_run()
    resp = h.exchange(_req(2, "progress"))
    assert resp["ok"] is True
    assert resp["result"]["state"] == "terminal"
    assert resp["result"]["snapshot"] == {"step": 2, "percent": 42}
    assert h.broker.closed
    assert h.child_sees_eof()


def test_submit_after_end_is_refused_and_closes(h):
    h.broker.end_run()
    resp = h.exchange(_req(1, "submit", SUBMIT_ARGS))
    assert resp["error"]["code"] == "run_ended"
    assert h.session.submit_calls == []
    assert h.broker.closed


def test_cancel_after_end_is_terminal_without_session_call(h):
    h.broker.end_run()
    resp = h.exchange(_req(1, "cancel", {"reason": "late"}))
    assert resp["ok"] is True
    assert resp["result"] == {"state": "terminal"}
    assert h.session.cancel_calls == []
    assert h.broker.closed


def test_late_request_after_close_raises_closed(h):
    # A request already in flight when the channel closes is never read.
    h.send(_req(1, "progress"))
    h.broker.close()
    with pytest.raises(SessionChannelClosed):
        h.broker.serve_one()


def test_close_is_idempotent(h):
    h.broker.close()
    h.broker.close()
    assert h.broker.closed


def test_end_run_then_close_is_safe(h):
    h.broker.end_run()
    h.broker.close()
    h.broker.close()
    with pytest.raises(SessionChannelClosed):
        h.broker.serve_one()


def test_malformed_frame_is_protocol_and_closes(h):
    h.send(b"this is not json\n")
    resp = h.broker.serve_one()
    assert resp["ok"] is False
    assert resp["error"]["code"] == "protocol"
    assert json.loads(h.recv_line().decode("utf-8")) == resp
    assert h.broker.closed
    assert h.child_sees_eof()


def test_extra_field_is_protocol_and_closes(h):
    frame = _req(1, "progress")
    frame["extra"] = 1
    resp = h.exchange(frame)
    assert resp["error"]["code"] == "protocol"
    assert h.broker.closed


def test_progress_with_args_is_protocol(h):
    resp = h.exchange(_req(1, "progress", {"unexpected": True}))
    assert resp["error"]["code"] == "protocol"
    assert h.broker.closed


def test_cancel_missing_reason_is_protocol(h):
    resp = h.exchange(_req(1, "cancel", {}))
    assert resp["error"]["code"] == "protocol"
    assert h.session.cancel_calls == []


def test_peer_gone_without_frame_closes_quietly(h):
    h.child.close()
    resp = h.broker.serve_one()
    assert resp["error"]["code"] == "protocol"
    assert h.broker.closed


def test_token_never_appears_in_any_frame_or_repr(h):
    assert h.exchange(_req(1, "progress"))["ok"] is True
    assert h.exchange(_req(2, "submit", SUBMIT_ARGS))["ok"] is True
    assert h.exchange(_req(3, "cancel", {"reason": "done"}))["ok"] is True
    assert SENTINEL not in repr(h.broker)
    assert SENTINEL.encode() not in h.all_bytes()
    # The admission token is not used by the session either.
    assert SENTINEL not in json.dumps(h.session.submit_calls)


def test_token_never_appears_in_refusals(h):
    h.send(_req(1, "shell"))
    h.broker.serve_one()
    h.recv_line()
    assert SENTINEL.encode() not in h.all_bytes()


def test_broker_rejects_empty_run_id():
    parent, child = socket.socketpair()
    try:
        with pytest.raises(ValueError):
            SessionChannelBroker("", FakeSession(), parent)
    finally:
        parent.close()
        child.close()
