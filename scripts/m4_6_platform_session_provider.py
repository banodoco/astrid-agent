"""Fixed direct-native provider entry point for the Q platform-session contract.

The module name and ``PLATFORM_SESSION_PROVIDER`` export are source-fixed by
Q's contract.  This provider intentionally has no caller-selected provider,
trusted-origin argument, capability, token, or handle.  The required
root/platform authenticated collector context has not been supplied or
reviewed, so opening a live session always fails closed.

``m4_6_process_observer`` supplies the B-owned local OS observation primitive
that a later reviewed authenticated session may retain from custody bind through
finalization.  It is not promoted to trusted authority here.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    # The observed B base predates Q's corrected dataclasses.  These imports are
    # type-only so this fixed provider remains importable while Q's correction
    # awaits review/freeze and merge.  No provisional trusted context is imported.
    from .native_service_adapter import (
        AuthenticatedPlatformSession,
        PlatformSessionBinding,
    )


class TrustedPlatformContextUnavailable(RuntimeError):
    """Raised until root/platform provides and B reviews a private context channel."""


class _FailClosedDirectNativeProvider:
    """The sole direct slot provider; it cannot be selected or configured by callers."""

    def open_authenticated_session(
        self,
        binding: PlatformSessionBinding,
        owner: object,
    ) -> AuthenticatedPlatformSession:
        # Do not inspect ``binding`` or ``owner`` as a substitute for trusted
        # origin.  In particular, environment-derived claims and serialized Q
        # observations must not accidentally become a local admission channel.
        del binding, owner
        raise TrustedPlatformContextUnavailable(
            "trusted collector context/channel is unavailable; direct hosted admission is closed"
        )


PLATFORM_SESSION_PROVIDER = _FailClosedDirectNativeProvider()
