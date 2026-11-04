"""The fixed direct provider stays closed until a reviewed private context exists."""

from __future__ import annotations

import inspect

import pytest

from scripts.m4_6_platform_session_provider import (
    PLATFORM_SESSION_PROVIDER,
    TrustedPlatformContextUnavailable,
)


def test_fixed_provider_has_only_the_contract_open_signature_and_fails_closed() -> None:
    signature = inspect.signature(PLATFORM_SESSION_PROVIDER.open_authenticated_session)
    assert tuple(signature.parameters) == ("binding", "owner")
    assert "trusted_origin" not in signature.parameters

    with pytest.raises(TrustedPlatformContextUnavailable, match="admission is closed"):
        PLATFORM_SESSION_PROVIDER.open_authenticated_session(object(), object())
