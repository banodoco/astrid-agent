"""Frozen, generic Vibe profile conformance inputs for the B-6 handoff.

This module contains data only.  It deliberately does not import an engine,
provider, transport, or worker implementation.  The composition root passes
these rows to the kernel manifest builder.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

# The checkout_server boot row is removed with the checkout route.  No accepted
# profile fixture remains, so the frozen manifest carries an empty profile set.
FROZEN_FIXTURE_DIGESTS: dict[str, str] = {}

VIBE_PROFILE_ORDER: tuple[str, ...] = ()

# Registry-derived identity.  Empty: no profile row is admitted at boot.
VIBE_PROFILE_REGISTRY: dict[str, dict[str, Any]] = {}


@dataclass(frozen=True, slots=True)
class VibeProfileFixture:
    """One accepted profile row consumed by the generic manifest path."""

    profile_id: str
    accepted_receipt_sha256: str
    fixture_digests: tuple[tuple[str, str], ...]
    accepted_input: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "profile_id": self.profile_id,
            "accepted_receipt_sha256": self.accepted_receipt_sha256,
            "fixture_digests": dict(self.fixture_digests),
            "accepted_input": self.accepted_input,
        }


_PROFILE_FIXTURES: tuple[VibeProfileFixture, ...] = ()


def vibe_profile_specs() -> tuple[VibeProfileFixture, ...]:
    """Return the frozen profile rows in required consumption order."""
    return _PROFILE_FIXTURES


__all__ = [
    "FROZEN_FIXTURE_DIGESTS",
    "VIBE_PROFILE_ORDER",
    "VIBE_PROFILE_REGISTRY",
    "VibeProfileFixture",
    "vibe_profile_specs",
]
