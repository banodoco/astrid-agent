"""Frozen, generic Vibe profile conformance inputs for the B-6 handoff.

This module contains data only.  It deliberately does not import an engine,
provider, transport, or worker implementation.  The composition root passes
these rows to the kernel manifest builder.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

# These five identities are accepted B-lane fixture inputs.  They are carried
# unchanged so a boot manifest cannot accidentally bless a different fixture
# corpus.
FROZEN_FIXTURE_DIGESTS: dict[str, str] = {
    "attempt-output-fixture.json": "951b9b4ce85dfc3ab999af705c4a3732acaa1d45468fa0ce0eaaf9edc941b31e",
    "fake-runner-transcript.json": "379c7821ecf6213ced2ede36c2fb991d82c56167616d7e21aa23d7e3890e43e0",
    "host-instance-paired-fixture.json": "4171922e65ea94a2c1af9f9abb7c8d5559125e354ca6ebb7f5efe1266ca22b4f",
    "portable-capability-fixture.json": "9284b056aa8a38f884599a7634be0fe0cb7fbfbd117823960e563375b3aa43b7",
    "task-resource-fixture.json": "ddc2d00bb893ea214d000bf618f67785c89c81d68d892864d9676d3c8b8d5164",
}

VIBE_PROFILE_ORDER: tuple[str, str] = ("pip_embedded", "checkout_server")

# Registry-derived identity.  ``binding`` is intentionally a generic profile
# binding, not an engine route; transport/provider selection is out of scope.
VIBE_PROFILE_REGISTRY: dict[str, dict[str, Any]] = {
    "pip_embedded": {
        "definition_version": 1,
        "binding": "vibe.profile",
        "output_policy": "staged_artifact",
        "probe": "accepted_profile_fixture",
    },
    "checkout_server": {
        "definition_version": 1,
        "binding": "vibe.profile",
        "output_policy": "staged_artifact",
        "probe": "accepted_profile_fixture",
    },
}


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


_PROFILE_FIXTURES: tuple[VibeProfileFixture, ...] = (
    VibeProfileFixture(
        "pip_embedded",
        "6a940bc5f16a332500a24fea729814a08846c9470b811556a16e3503be92976f",
        tuple(sorted(FROZEN_FIXTURE_DIGESTS.items())[:3]),
        "accepted D-3 profile evidence",
    ),
    VibeProfileFixture(
        "checkout_server",
        "7ac8da29bad6da2b7486df4fde4fa9a2a0c39fbd0911a6a60f988b3fb2f54fc8",
        tuple(sorted(FROZEN_FIXTURE_DIGESTS.items())[3:]),
        "accepted D-4/D-5 profile evidence",
    ),
)


def vibe_profile_specs() -> tuple[VibeProfileFixture, ...]:
    """Return the frozen profile rows in required consumption order."""
    return _PROFILE_FIXTURES


# Compatibility with the historical conformance seam's naming, without
# introducing a second registry or setup journal.
def capability_conformance_specs() -> tuple[VibeProfileFixture, ...]:
    return vibe_profile_specs()


__all__ = [
    "FROZEN_FIXTURE_DIGESTS",
    "VIBE_PROFILE_ORDER",
    "VIBE_PROFILE_REGISTRY",
    "VibeProfileFixture",
    "capability_conformance_specs",
    "vibe_profile_specs",
]
