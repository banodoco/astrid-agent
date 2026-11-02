from __future__ import annotations

import socket
import threading
import time
from typing import Any, Callable

import pytest

from astrid.core.execution import generic_host
from astrid.core.execution.session_client import (
    SESSION_CHANNEL_VERSION,
    SessionChannelError,
    SessionClient,
)

RUN_ID = "run-l2-1"
CLOSE = object()  # handler return: close the host end without answering
SILENT = object()  # handler return: read the request, never answer


class FakeHost(threading.Thread):
    """Speaks the frame protocol on the host end of a socketpair."""

    def __init__(self, sock: socket.socket, handler: Callable[[dict[str, Any]], Any]) -> None:
        super().__init__(daemon=True)
        self.sock = sock
        self.handler = handler
        self.frames: list[dict[str, Any]] = []
        self._release = threading.Event()

    def run(self) -> None:
        while True:
            try:
                request = generic_host._read_activation_frame(self.sock)
            except Exception:
                return
            self.frames.append(request)
            answer = self.handler(request)
            if answer is CLOSE:
                self.sock.close()
                return
            if answer is SILENT:
                self._release.wait(5)
                return
            generic_host._send_activation_frame(self.sock, answer)

    def stop(self) -> None:
        self._release.set()
        try:
            self.sock.close()
        except OSError:
            pass
        self.join(5)


def _ok(request: dict[str, Any], result: dict[str, Any] | None = None, **overrides: Any) -> dict[str, Any]:
    response = {
        "version": SESSION_CHANNEL_VERSION,
        "run_id": request["run_id"],
        "seq": request["seq"],
        "ok": True,
        "result": result if result is not None else {"state": "running", "snapshot": {}},
    }
    response.update(overrides)
    return response


def _err(request: dict[str, Any], code: str, message: str = "refused") -> dict[str, Any]:
    return {
        "version": SESSION_CHANNEL_VERSION,
        "run_id": request["run_id"],
        "seq": request["seq"],
        "ok": False,
        "error": {"code": code, "message": message},
    }


class Channel:
    def __init__(self, handler: Callable[[dict[str, Any]], Any], timeout: float = 2.0) -> None:
        host_sock, child_sock = socket.socketpair()
        fd = child_sock.detach()  # SessionClient owns the fd now
        self.host = FakeHost(host_sock, handler)
        self.host.start()
        self.client = SessionClient(fd, RUN_ID, timeout)

    def close(self) -> None:
        self.client.close()
        self.host.stop()


@pytest.fixture
def channel_factory():
    channels: list[Channel] = []

    def make(handler: Callable[[dict[str, Any]], Any], timeout: float = 2.0) -> Channel:
        channel = Channel(handler, timeout)
        channels.append(channel)
        return channel

    yield make
    for channel in channels:
        channel.close()


def _echo(request: dict[str, Any]) -> dict[str, Any]:
    if request["op"] == "submit":
        return _ok(request, {"evidence": {"status": "done"}})
    if request["op"] == "progress":
        return _ok(request, {"state": "running", "snapshot": {"step": 1}})
    return _ok(request, {"state": "cancel_requested"})


def test_submit_round_trips_with_request_shape(channel_factory):
    ch = channel_factory(_echo)
    settings = {"prompt": "a", "frames": 4}
    result = ch.client.submit(settings, 5)
    assert result == {"evidence": {"status": "done"}}
    (frame,) = ch.host.frames
    assert frame == {
        "version": SESSION_CHANNEL_VERSION,
        "run_id": RUN_ID,
        "seq": 1,
        "op": "submit",
        "args": {"settings": settings, "timeout_seconds": 5.0},
    }


def test_progress_round_trips_with_empty_args(channel_factory):
    ch = channel_factory(_echo)
    assert ch.client.progress() == {"state": "running", "snapshot": {"step": 1}}
    assert ch.host.frames[0]["op"] == "progress"
    assert ch.host.frames[0]["args"] == {}


def test_cancel_round_trips_with_reason(channel_factory):
    ch = channel_factory(_echo)
    assert ch.client.cancel("operator stop") == {"state": "cancel_requested"}
    frame = ch.host.frames[0]
    assert frame["op"] == "cancel"
    assert frame["args"] == {"reason": "operator stop"}


def test_seq_increments_across_calls_and_ops(channel_factory):
    ch = channel_factory(_echo)
    ch.client.submit({}, 5)
    ch.client.progress()
    ch.client.cancel("x")
    ch.client.progress()
    assert [f["seq"] for f in ch.host.frames] == [1, 2, 3, 4]
    assert [f["op"] for f in ch.host.frames] == ["submit", "progress", "cancel", "progress"]


def test_ok_false_raises_with_host_code_and_message(channel_factory):
    ch = channel_factory(lambda r: _err(r, "submit_already_used", "submit already used"))
    with pytest.raises(SessionChannelError) as info:
        ch.client.submit({}, 5)
    assert info.value.code == "submit_already_used"
    assert str(info.value) == "submit already used"


def test_ok_false_invalid_settings_code(channel_factory):
    ch = channel_factory(lambda r: _err(r, "invalid_settings"))
    with pytest.raises(SessionChannelError) as info:
        ch.client.submit({"bad": True}, 5)
    assert info.value.code == "invalid_settings"


def test_not_allowed_refusal_closes_client(channel_factory):
    ch = channel_factory(lambda r: _err(r, "not_allowed"))
    with pytest.raises(SessionChannelError) as info:
        ch.client.progress()
    assert info.value.code == "not_allowed"
    with pytest.raises(SessionChannelError) as info:
        ch.client.progress()
    assert info.value.code == "closed"


@pytest.mark.parametrize(
    "overrides",
    [{"seq": 99}, {"run_id": "other-run"}, {"version": "astrid.session-channel/v0"}],
)
def test_mismatched_response_is_protocol_error(channel_factory, overrides):
    ch = channel_factory(lambda r: _ok(r, **overrides))
    with pytest.raises(SessionChannelError) as info:
        ch.client.progress()
    assert info.value.code == "protocol"


def test_bool_seq_in_response_is_protocol_error(channel_factory):
    ch = channel_factory(lambda r: _ok(r, seq=True))
    with pytest.raises(SessionChannelError) as info:
        ch.client.progress()
    assert info.value.code == "protocol"


def test_host_close_before_answer_is_transport(channel_factory):
    ch = channel_factory(lambda r: CLOSE)
    with pytest.raises(SessionChannelError) as info:
        ch.client.progress()
    assert info.value.code == "transport"


def test_host_close_after_submit_is_transport_uncertain(channel_factory):
    ch = channel_factory(lambda r: CLOSE)
    with pytest.raises(SessionChannelError) as info:
        ch.client.submit({}, 5)
    assert info.value.code == "transport_uncertain"
    assert len(ch.host.frames) == 1


def test_submit_timeout_is_uncertain_and_never_resent(channel_factory):
    ch = channel_factory(lambda r: SILENT, timeout=0.2)
    with pytest.raises(SessionChannelError) as info:
        ch.client.submit({"n": 1}, 0.1)
    assert info.value.code == "transport_uncertain"
    time.sleep(0.1)
    assert len(ch.host.frames) == 1

    # The channel is dead after the timeout; a retry must not reach the wire.
    with pytest.raises(SessionChannelError) as info:
        ch.client.submit({"n": 1}, 0.1)
    assert info.value.code == "closed"
    time.sleep(0.1)
    assert len(ch.host.frames) == 1


def test_progress_timeout_is_transport(channel_factory):
    ch = channel_factory(lambda r: SILENT, timeout=0.2)
    with pytest.raises(SessionChannelError) as info:
        ch.client.progress()
    assert info.value.code == "transport"


def test_oversized_request_is_rejected_before_sending(channel_factory):
    ch = channel_factory(_echo)
    with pytest.raises(SessionChannelError) as info:
        ch.client.submit({"blob": "x" * (generic_host._ACTIVATION_FRAME_LIMIT + 1)}, 5)
    assert info.value.code == "protocol"
    assert ch.host.frames == []
    # Nothing was sent, so the channel and its seq are still usable.
    assert ch.client.progress()["state"] == "running"
    assert ch.host.frames[0]["seq"] == 1


def test_oversized_response_is_protocol_error(channel_factory):
    big = {"state": "running", "snapshot": {"blob": "y" * (generic_host._ACTIVATION_FRAME_LIMIT + 1)}}
    ch = channel_factory(lambda r: _ok(r, big))
    with pytest.raises(SessionChannelError) as info:
        ch.client.progress()
    assert info.value.code == "protocol"


def test_close_is_idempotent_and_later_calls_raise_closed(channel_factory):
    ch = channel_factory(_echo)
    ch.client.close()
    ch.client.close()
    with pytest.raises(SessionChannelError) as info:
        ch.client.progress()
    assert info.value.code == "closed"
    with pytest.raises(SessionChannelError) as info:
        ch.client.submit({}, 5)
    assert info.value.code == "closed"
    assert ch.host.frames == []


def test_invalid_constructor_and_timeout_arguments_raise_value_error():
    a, b = socket.socketpair()
    try:
        with pytest.raises(ValueError):
            SessionClient(-1, RUN_ID, 1.0)
        with pytest.raises(ValueError):
            SessionClient(b.fileno(), "", 1.0)
        with pytest.raises(ValueError):
            SessionClient(b.fileno(), RUN_ID, 0)
    finally:
        a.close()
        b.close()
