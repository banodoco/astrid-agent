"""Public pack-facing surface for the M2-11a session channel.

Pack actions reach these names through ``astrid.sdk`` (for example
``from astrid.sdk import SessionChannelError``). This module is the lazy
target of those exports. Nothing else is exported: no broker, compile logic
or transport internals.
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
