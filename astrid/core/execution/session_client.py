"""Worker-side client for the M2-11a session channel (frame schema v1).

The client wraps the CHILD end of the private socketpair the host passes to
the worker. It never holds a token: the host keeps the credential and the
client only sends ``version``/``run_id``/``seq``/``op``/``args`` frames.

Failure codes raised as ``SessionChannelError.code``:

- host error codes (``submit_already_used``, ``invalid_settings``,
  ``not_allowed``, ...) are passed through unchanged;
- ``protocol``: a response whose version, run_id or seq does not match the
  request, a malformed or oversized response, or a request that would exceed
  the frame limit (nothing is sent in that case);
- ``transport``: EOF, socket error or timeout before a response (progress and
  cancel only);
- ``transport_uncertain``: the same failures for ``submit``. The host may have
  started the run, so the caller must never resend a submit;
- ``closed``: the client was closed, or a previous failure already closed it.

Lazy imports keep ``generic_host`` out of module import time.
"""

from __future__ import annotations

import json
import math
import socket
import time
from typing import Any, Mapping

SESSION_CHANNEL_VERSION = "astrid.session-channel/v1"

# Codes after which the host closes the channel (spec: "Refusal and close").
_HOST_CLOSING_CODES = frozenset({"not_allowed", "protocol"})


class SessionChannelError(Exception):
    """A session channel request failed. ``code`` is machine-readable."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


def _frame_api() -> tuple[Any, Any, type]:
    """Return (send, read, HostError) from generic_host, imported lazily."""
    from astrid.core.execution import generic_host

    return (
        generic_host._send_activation_frame,
        generic_host._read_activation_frame,
        generic_host.HostError,
    )


class _DeadlineSocket:
    """Socket view that caps each send/recv by one overall request deadline."""

    def __init__(self, sock: socket.socket, deadline: float) -> None:
        self._sock = sock
        self._deadline = deadline

    def _remaining(self) -> float:
        left = self._deadline - time.monotonic()
        if left <= 0:
            raise socket.timeout("session channel request deadline exceeded")
        return left

    def sendall(self, data: bytes) -> None:
        self._sock.settimeout(self._remaining())
        self._sock.sendall(data)

    def recv(self, bufsize: int) -> bytes:
        self._sock.settimeout(self._remaining())
        return self._sock.recv(bufsize)


def _check_timeout(value: Any, name: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"{name} must be a number of seconds")
    value = float(value)
    if not math.isfinite(value) or value <= 0:
        raise ValueError(f"{name} must be a positive finite number of seconds")
    return value


class SessionClient:
    """Synchronous client for one run's session channel.

    Single-threaded: one request is in flight at a time. The client is not
    reusable after any transport or protocol failure; the socket is closed.
    """

    def __init__(self, fd: int, run_id: str, timeout_seconds: float) -> None:
        if isinstance(fd, bool) or not isinstance(fd, int) or fd < 3:
            raise ValueError("session channel fd is invalid")
        if not isinstance(run_id, str) or not run_id:
            raise ValueError("session channel run_id is invalid")
        self._timeout = _check_timeout(timeout_seconds, "timeout_seconds")
        self._run_id = run_id
        self._seq = 0
        self._closed = False
        # Takes ownership of fd; close() releases it.
        self._sock = socket.socket(fileno=fd)

    # ----------------------------------------------------------------- ops

    def submit(self, settings: Mapping[str, Any], timeout_seconds: float) -> dict[str, Any]:
        """Start the run once. Never retried. Returns the evidence mapping."""
        if not isinstance(settings, Mapping):
            raise ValueError("settings must be a mapping")
        run_budget = _check_timeout(timeout_seconds, "timeout_seconds")
        args = {"settings": dict(settings), "timeout_seconds": run_budget}
        # The host may take the whole run budget before answering; allow the
        # client-side client timeout on top of it.
        return self._request("submit", args, wait=run_budget + self._timeout)

    def progress(self) -> dict[str, Any]:
        return self._request("progress", {}, wait=self._timeout)

    def cancel(self, reason: str) -> dict[str, Any]:
        if not isinstance(reason, str):
            raise ValueError("cancel reason must be a string")
        return self._request("cancel", {"reason": reason}, wait=self._timeout)

    def close(self) -> None:
        """Release the channel. Idempotent."""
        if self._closed:
            return
        self._closed = True
        self._sock.close()

    # ------------------------------------------------------------- internals

    def _request(self, op: str, args: dict[str, Any], *, wait: float) -> dict[str, Any]:
        if self._closed:
            raise SessionChannelError("closed", "session channel is closed")
        send, read, host_error = _frame_api()
        from astrid.core.execution.generic_host import _ACTIVATION_FRAME_LIMIT

        seq = self._seq + 1
        frame = {
            "version": SESSION_CHANNEL_VERSION,
            "run_id": self._run_id,
            "seq": seq,
            "op": op,
            "args": args,
        }
        try:
            encoded = json.dumps(frame, sort_keys=True, separators=(",", ":"), allow_nan=False)
        except (TypeError, ValueError) as exc:
            raise ValueError("session channel arguments are not canonical JSON") from exc
        if len(encoded.encode("utf-8")) + 1 > _ACTIVATION_FRAME_LIMIT:
            # Nothing was sent, so the channel stays usable and seq is not consumed.
            raise SessionChannelError("protocol", "session channel request exceeds frame limit")

        self._seq = seq
        uncertain_code = "transport_uncertain" if op == "submit" else "transport"
        deadline = _DeadlineSocket(self._sock, time.monotonic() + wait)
        try:
            send(deadline, frame)
            response = read(deadline)
        except host_error as exc:
            if str(exc) == "parked activation channel closed before a frame":
                self._fail_closed()
                raise SessionChannelError(uncertain_code, "session channel closed before response") from None
            self._fail_closed()
            raise SessionChannelError("protocol", "session channel response is invalid") from None
        except OSError:
            self._fail_closed()
            raise SessionChannelError(uncertain_code, "session channel transport failed") from None

        if (
            response.get("version") != SESSION_CHANNEL_VERSION
            or response.get("run_id") != self._run_id
            or response.get("seq") != seq
            or type(response.get("seq")) is not int
        ):
            self._fail_closed()
            raise SessionChannelError("protocol", "session channel response does not match request")

        ok = response.get("ok")
        if ok is True:
            result = response.get("result")
            if not isinstance(result, dict):
                self._fail_closed()
                raise SessionChannelError("protocol", "session channel success response has no result")
            return result
        if ok is False:
            error = response.get("error")
            if (
                not isinstance(error, dict)
                or not isinstance(error.get("code"), str)
                or not error["code"]
                or not isinstance(error.get("message"), str)
            ):
                self._fail_closed()
                raise SessionChannelError("protocol", "session channel error response is malformed")
            if error["code"] in _HOST_CLOSING_CODES:
                self._fail_closed()
            raise SessionChannelError(error["code"], error["message"])

        self._fail_closed()
        raise SessionChannelError("protocol", "session channel response has no ok flag")

    def _fail_closed(self) -> None:
        """Close after a transport or protocol failure; the stream is unusable."""
        if not self._closed:
            self._closed = True
            self._sock.close()
