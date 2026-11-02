"""Public pack-facing surface for the M2-11a session channel.

Pack actions import the error type and the client shape from here. Nothing
else is exported: no broker, compile logic or transport internals.
"""

import typing

from astrid.core.execution.session_client import SessionChannelError

__all__ = ["SessionChannelError", "SessionClientProtocol"]


class SessionClientProtocol(typing.Protocol):
    """Shape of the worker-side session client handed to a pack action."""

    def submit(self, inputs: typing.Mapping[str, typing.Any], timeout_seconds: float) -> dict[str, typing.Any]:
        ...

    def progress(self) -> dict[str, typing.Any]:
        ...

    def cancel(self, reason: str) -> dict[str, typing.Any]:
        ...
