"""M2-11a host identity enforcement over the real session-channel broker.

Policy: the HOST enforces identity.  Nothing identity-like crosses to the
worker, and the worker's run id is a label, not authority.  These tests drive
``SessionChannelBroker`` over a real ``socket.socketpair()`` and read responses
with the production frame helpers.

Case (c) (host adapter refuses mismatched settings and submits host-compiled
settings) is NOT covered here: ``_HostManagedSessionAdapter`` and
``_HostSessionChannel`` do not exist at eb21dfd91, so there is nothing to drive.
"""

from __future__ import annotations

import json
import socket
from typing import Any

import pytest

from astrid.core.execution.generic_host import _read_activation_frame, _send_activation_frame
from astrid.core.execution.session_channel import (
    SESSION_CHANNEL_VERSION,
    SessionChannelBroker,
)

RUN_ID = "run-m2-11a-identity"
FORGED_RUN_ID = "run-m2-11a-forged"
SENTINEL = "TOKEN-SENTINEL-identity-41c0-do-not-leak"
SUBMIT_SETTINGS = {"prompt": "a fox", "frames": 1}


class RecordingSession:
    """Host-side session double that records every host operation."""

    def __init__(self) -> None:
        self.validate_calls: list[dict[str, Any]] = []
        self.submit_calls: list[tuple[dict[str, Any], float]] = []
        self.progress_calls = 0
        self.cancel_calls: list[str] = []

    def validate_settings(self, settings: Any) -> None:
        self.validate_calls.append(dict(settings))

    def submit(self, settings: dict[str, Any], timeout_seconds: float) -> dict[str, Any]:
        self.submit_calls.append((dict(settings), timeout_seconds))
        return {"result": {"success": True, "errors": []}}

    def progress(self) -> dict[str, Any]:
        self.progress_calls += 1
        return {"step": self.progress_calls}

    def cancel(self, reason: str) -> dict[str, Any]:
        self.cancel_calls.append(reason)
        return {"accepted": True}

    def host_calls(self) -> int:
        return (len(self.submit_calls) + self.progress_calls + len(self.cancel_calls))


class Channel:
    """Worker end of a socketpair; the broker serves the parent end."""

    def __init__(self) -> None:
        self.parent, self.child = socket.socketpair()
        self.child.settimeout(5.0)
        self.session = RecordingSession()
        self.broker = SessionChannelBroker(
            RUN_ID, self.session, self.parent, admission_token=SENTINEL
        )
        self.frames: list[bytes] = []

    def exchange(self, request: dict[str, Any]) -> dict[str, Any]:
        _send_activation_frame(self.child, request)
        response = self.broker.serve_one()
        decoded = _read_activation_frame(self.child)
        assert decoded == response
        self.frames.append(json.dumps(decoded, sort_keys=True).encode("utf-8"))
        return decoded

    def child_sees_eof(self) -> bool:
        try:
            return self.child.recv(1) == b""
        except (ConnectionResetError, BrokenPipeError):
            return True

    def close(self) -> None:
        self.broker.close()
        self.child.close()


@pytest.fixture
def channel():
    ch = Channel()
    yield ch
    ch.close()


def _request(seq: int, op: str, args: dict[str, Any] | None = None, *,
             run_id: str = RUN_ID) -> dict[str, Any]:
    return {"version": SESSION_CHANNEL_VERSION, "run_id": run_id, "seq": seq,
            "op": op, "args": args if args is not None else {}}


def _submit_args(**extra: Any) -> dict[str, Any]:
    args: dict[str, Any] = {"settings": dict(SUBMIT_SETTINGS), "timeout_seconds": 30}
    args.update(extra)
    return args


# -- (a) worker-supplied identity keys are refused ----------------------------

@pytest.mark.parametrize("key,value", [
    ("identity", "worker-chosen-identity"),
    ("token_id", "tok-forged-000"),
])
def test_identity_key_inside_submit_args_is_refused_and_channel_closes(channel, key, value):
    args = _submit_args(**{key: value})
    resp = channel.exchange(_request(1, "submit", args))

    assert resp["ok"] is False
    assert resp["error"]["code"] == "protocol"
    assert channel.session.validate_calls == []
    assert channel.session.submit_calls == []
    assert channel.broker.closed
    assert channel.child_sees_eof()


@pytest.mark.parametrize("key,value", [
    ("identity", "worker-chosen-identity"),
    ("token_id", "tok-forged-000"),
])
def test_identity_key_at_envelope_level_is_refused_and_channel_closes(channel, key, value):
    frame = _request(1, "submit", _submit_args())
    frame[key] = value
    resp = channel.exchange(frame)

    assert resp["ok"] is False
    assert resp["error"]["code"] == "protocol"
    assert channel.session.validate_calls == []
    assert channel.session.submit_calls == []
    assert channel.broker.closed
    assert channel.child_sees_eof()


def test_identity_key_in_submit_does_not_reach_host_even_with_correct_settings(channel):
    # The worker's settings are fine; only the extra identity key is wrong.
    resp = channel.exchange(_request(1, "submit", _submit_args(identity="someone-else")))
    assert resp["error"]["code"] == "protocol"
    assert channel.session.submit_calls == []


# -- (b) forged run id is refused and nothing is served -----------------------

@pytest.mark.parametrize("op,args", [
    ("submit", _submit_args()),
    ("progress", {}),
    ("cancel", {"reason": "stop"}),
])
def test_forged_run_id_is_protocol_serves_nothing_and_closes(channel, op, args):
    resp = channel.exchange(_request(1, op, args, run_id=FORGED_RUN_ID))

    assert resp["ok"] is False
    assert resp["error"]["code"] == "protocol"
    assert "result" not in resp
    assert channel.session.validate_calls == []
    assert channel.session.submit_calls == []
    assert channel.session.progress_calls == 0
    assert channel.session.cancel_calls == []
    assert channel.broker.closed
    assert channel.child_sees_eof()


def test_forged_run_id_after_valid_request_closes_and_serves_nothing_more(channel):
    ok = channel.exchange(_request(1, "progress"))
    assert ok["ok"] is True
    assert channel.session.progress_calls == 1

    resp = channel.exchange(_request(2, "submit", _submit_args(), run_id=FORGED_RUN_ID))
    assert resp["error"]["code"] == "protocol"
    assert channel.session.submit_calls == []
    assert channel.broker.closed
    assert channel.child_sees_eof()


# -- token sentinel never crosses the wire ------------------------------------

def test_token_sentinel_never_appears_in_any_frame(channel):
    channel.exchange(_request(1, "progress"))
    channel.exchange(_request(2, "submit", _submit_args(identity=SENTINEL)))
    assert SENTINEL.encode("utf-8") not in b"".join(channel.frames)
