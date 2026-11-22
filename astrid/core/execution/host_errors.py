"""Shared errors raised by the generic execution host."""

from __future__ import annotations


class HostError(RuntimeError):
    """A controlled host-side error suitable for a worker diagnostic."""

    def __init__(self, message: str, *, safe_message: str | None = None) -> None:
        super().__init__(message)
        self.safe_message = safe_message
