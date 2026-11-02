"""Host-side broker for the M2-11a session channel (frozen schema v1).

The broker owns the parent end of a per-run socketpair.  It serves one worker
request at a time, enforces the three-op allow-list, and fails closed: every
error frame is followed by closing the channel.  The admission token is held
only as an opaque reference; it is never placed in a frame, log, or exception.
"""

from __future__ import annotations

import json
import math
from typing import Any, Callable, Mapping, Protocol

SESSION_CHANNEL_VERSION = "astrid.session-channel/v1"

_ALLOWED_OPS = frozenset({"submit", "progress", "cancel"})
_REQUEST_KEYS = frozenset({"version", "run_id", "seq", "op", "args"})
_SUBMIT_ARG_KEYS = frozenset({"settings", "timeout_seconds"})
_CANCEL_ARG_KEYS = frozenset({"reason"})


class SessionChannelClosed(RuntimeError):
    """The channel is closed; no further requests are served."""


class HostSession(Protocol):
    """The three host operations the broker exposes over the channel."""

    def submit(self, settings: dict[str, Any], timeout_seconds: float) -> dict[str, Any]: ...

    def progress(self) -> dict[str, Any]: ...

    def cancel(self, reason: str) -> dict[str, Any]: ...

    def validate_settings(self, settings: Mapping[str, Any]) -> None:
        """Raise ValueError when the settings are not acceptable."""
        ...


class _Refusal(Exception):
    """Internal: carries an error code and a fixed, token-free message."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(code)
        self.code = code
        self.message = message


class SessionChannelBroker:
    """Serve one worker's session-channel requests for one run."""

    def __init__(
        self,
        run_id: str,
        session: HostSession,
        sock: Any,
        *,
        admission_token: object | None = None,
    ) -> None:
        if not isinstance(run_id, str) or not run_id:
            raise ValueError("run_id must be a non-empty string")
        self._run_id = run_id
        self._session = session
        self._sock = sock
        # Opaque reference only; never read back into a frame.  Dropped on close.
        self._admission: object | None = admission_token
        self._last_seq = 0
        self._submitted = False
        self._cancel_requested = False
        self._ended = False
        self._closed = False

    def __repr__(self) -> str:
        state = "closed" if self._closed else ("ended" if self._ended else "open")
        return f"SessionChannelBroker(run_id={self._run_id!r}, state={state})"

    @property
    def closed(self) -> bool:
        return self._closed

    def end_run(self) -> None:
        """Mark the run terminal.

        The channel stays open for one final reply: the worker's next
        ``progress`` (or ``cancel``) gets ``"terminal"`` with the final snapshot,
        and the broker closes right after sending it.  Call ``close()`` to force
        the close if the worker never asks.
        """
        self._ended = True

    def close(self) -> None:
        """Close the channel.  Idempotent."""
        if self._closed:
            return
        self._closed = True
        self._admission = None
        try:
            self._sock.close()
        except OSError:
            pass

    def serve_one(self) -> dict[str, Any]:
        """Read one request, answer it, and return the response frame sent."""
        if self._closed:
            raise SessionChannelClosed("session channel is closed")

        from astrid.core.execution.generic_host import HostError, _read_activation_frame

        try:
            request = _read_activation_frame(self._sock)
        except (HostError, OSError, ValueError):
            return self._fail(self._last_seq, "protocol", "malformed or unreadable request frame")

        try:
            seq, op, args = self._check_envelope(request)
        except _Refusal as refusal:
            echo = request.get("seq") if _is_seq(request.get("seq")) else self._last_seq
            return self._fail(echo, refusal.code, refusal.message)

        self._last_seq = seq
        try:
            if op == "submit":
                result = self._submit(args)
            elif op == "progress":
                result = self._progress(args)
            else:
                result = self._cancel(args)
        except _Refusal as refusal:
            return self._fail(seq, refusal.code, refusal.message)

        frame = {
            "version": SESSION_CHANNEL_VERSION,
            "run_id": self._run_id,
            "seq": seq,
            "ok": True,
            "result": result,
        }
        try:
            _assert_canonical(frame)
        except (TypeError, ValueError):
            return self._fail(seq, "host_error", "host result could not be encoded")
        self._send(frame)
        if result.get("state") == "terminal":
            self.close()
        return frame

    # -- envelope -----------------------------------------------------------

    def _check_envelope(self, request: Mapping[str, Any]) -> tuple[int, str, dict[str, Any]]:
        if set(request) != _REQUEST_KEYS:
            raise _Refusal("protocol", "request has missing or unexpected fields")
        if request["version"] != SESSION_CHANNEL_VERSION:
            raise _Refusal("protocol", "unsupported session channel version")
        if request["run_id"] != self._run_id:
            raise _Refusal("protocol", "run_id does not match this run")
        seq = request["seq"]
        if not _is_seq(seq):
            raise _Refusal("protocol", "seq must be an integer")
        if self._last_seq == 0:
            if seq != 1:
                raise _Refusal("protocol", "seq must start at 1")
        elif seq <= self._last_seq:
            raise _Refusal("protocol", "seq must strictly increase")
        op = request["op"]
        if not isinstance(op, str) or op not in _ALLOWED_OPS:
            raise _Refusal("not_allowed", "operation is not allowed on this channel")
        args = request["args"]
        if not isinstance(args, dict):
            raise _Refusal("protocol", "args must be an object")
        return seq, op, args

    # -- ops ----------------------------------------------------------------

    def _submit(self, args: dict[str, Any]) -> dict[str, Any]:
        if self._submitted:
            raise _Refusal("submit_already_used", "submit was already used for this run")
        if self._ended:
            raise _Refusal("run_ended", "run has already ended")
        if set(args) != _SUBMIT_ARG_KEYS:
            raise _Refusal("protocol", "submit args must be exactly settings and timeout_seconds")
        settings = args["settings"]
        timeout = args["timeout_seconds"]
        if not _is_number(timeout) or not math.isfinite(timeout) or timeout <= 0:
            raise _Refusal("protocol", "timeout_seconds must be a positive finite number")
        if not isinstance(settings, dict):
            raise _Refusal("invalid_settings", "settings must be an object")
        try:
            self._session.validate_settings(settings)
        except ValueError:
            raise _Refusal("invalid_settings", "settings were rejected by host validation") from None
        except Exception:
            raise _Refusal("host_error", "host session validation failed") from None
        # Consume the one submit before calling out, so a raising submit is not retried.
        self._submitted = True
        result = self._host_call(self._session.submit, dict(settings), float(timeout))
        if not isinstance(result, Mapping):
            raise _Refusal("host_error", "host submit returned a non-object result")
        return dict(result)

    def _progress(self, args: dict[str, Any]) -> dict[str, Any]:
        if args:
            raise _Refusal("protocol", "progress takes no args")
        snapshot = self._host_call(self._session.progress)
        if not isinstance(snapshot, Mapping):
            raise _Refusal("host_error", "host progress returned a non-object snapshot")
        return {
            "state": "terminal" if self._ended else "running",
            "snapshot": dict(snapshot),
        }

    def _cancel(self, args: dict[str, Any]) -> dict[str, Any]:
        if set(args) != _CANCEL_ARG_KEYS or not isinstance(args["reason"], str):
            raise _Refusal("protocol", "cancel args must be exactly a string reason")
        if self._ended:
            return {"state": "terminal"}
        if self._cancel_requested:
            # Idempotent: the underlying session.cancel runs at most once.
            return {"state": "already_cancelled"}
        self._cancel_requested = True
        reason = args["reason"]
        self._host_call(self._session.cancel, reason)
        return {"state": "cancel_requested"}

    def _host_call(self, fn: Callable[..., Any], *args: Any) -> Any:
        try:
            return fn(*args)
        except Exception:
            raise _Refusal("host_error", "host session operation failed") from None

    # -- responses ----------------------------------------------------------

    def _fail(self, seq: int, code: str, message: str) -> dict[str, Any]:
        """Send an error frame, then close.  Every error closes the channel."""
        frame = {
            "version": SESSION_CHANNEL_VERSION,
            "run_id": self._run_id,
            "seq": seq,
            "ok": False,
            "error": {"code": code, "message": message},
        }
        try:
            self._send(frame)
        except SessionChannelClosed:
            pass
        self.close()
        return frame

    def _send(self, frame: Mapping[str, Any]) -> None:
        from astrid.core.execution.generic_host import _send_activation_frame

        try:
            _send_activation_frame(self._sock, frame)
        except OSError:
            self.close()
            raise SessionChannelClosed("session channel peer is gone") from None


def _is_seq(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and value >= 1


def _is_number(value: Any) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def _assert_canonical(frame: Mapping[str, Any]) -> None:
    json.dumps(frame, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False)


__all__ = [
    "SESSION_CHANNEL_VERSION",
    "HostSession",
    "SessionChannelBroker",
    "SessionChannelClosed",
]
