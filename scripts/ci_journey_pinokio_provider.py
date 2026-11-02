"""Canonical-journey adapter for the typed R-24 Linux producer invocation.

This module only binds the journey-owned JSON values to M-QUAL's typed
consumer.  A structurally valid provider result is not authenticated evidence
and does not authorize the M4-4a executor or qualify a journey row.
"""

from __future__ import annotations

import json
import os
import sys
from collections.abc import Mapping
from pathlib import Path
from typing import Any

if __package__:
    from .ci_journey_provider import (
        LinuxProviderOutput,
        ProviderContractError,
        ProviderInvocationError,
        ProviderRequest,
        invoke_linux_provider,
    )
else:
    from ci_journey_provider import (
        LinuxProviderOutput,
        ProviderContractError,
        ProviderInvocationError,
        ProviderRequest,
        invoke_linux_provider,
    )


DIAGNOSTIC_NAME = "r24-provider.stderr.json"
MAX_DIAGNOSTIC_BYTES = 1024 * 1024


class JourneyProviderError(RuntimeError):
    """The journey-owned typed provider boundary failed closed."""


def _canonical(value: object) -> bytes:
    try:
        return json.dumps(
            value,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=True,
            allow_nan=False,
        ).encode("ascii")
    except (TypeError, ValueError, UnicodeError, RecursionError) as exc:
        raise JourneyProviderError("R-24 provider input is not canonical JSON") from exc


def _exact_mapping(value: object, fields: set[str], label: str) -> dict[str, Any]:
    if not isinstance(value, Mapping) or set(value) != fields:
        raise JourneyProviderError(f"{label} requires exactly {sorted(fields)}")
    return dict(value)


def _retain_diagnostic(evidence: Path, raw: bytes) -> None:
    if not raw:
        return
    if len(raw) > MAX_DIAGNOSTIC_BYTES:
        raise JourneyProviderError("R-24 provider diagnostic exceeds the retained limit")
    if not isinstance(evidence, Path) or not evidence.is_absolute():
        raise JourneyProviderError("absolute journey evidence directory required")
    evidence.mkdir(parents=True, exist_ok=True)
    destination = evidence / DIAGNOSTIC_NAME
    if destination.exists() or destination.is_symlink():
        raise JourneyProviderError("R-24 provider diagnostic destination already exists")
    destination.write_bytes(raw)


def invoke_linux_provider_for_journey(
    raw: object,
    evidence: Path,
) -> LinuxProviderOutput:
    """Invoke one immutable provider without promoting its structural output."""
    if os.name != "posix" or not sys.platform.startswith("linux"):
        raise JourneyProviderError("R-24 typed provider is Linux-only")
    value = _exact_mapping(raw, {"request", "config", "producer_root"}, "r24_linux_provider")
    request_value = _exact_mapping(
        value["request"],
        {"schema", "candidate", "provider", "stop", "config_sha256", "run_id"},
        "r24_linux_provider.request",
    )
    config_value = _exact_mapping(
        value["config"],
        {"checkouts", "pterm", "node", "node_sha256", "pterm_entry_sha256", "unws_sha256"},
        "r24_linux_provider.config",
    )
    producer_root = value["producer_root"]
    if not isinstance(producer_root, str) or not os.path.isabs(producer_root):
        raise JourneyProviderError("r24_linux_provider.producer_root must be absolute")
    request = ProviderRequest.parse(_canonical(request_value))
    try:
        output = invoke_linux_provider(request, _canonical(config_value), producer_root)
    except ProviderInvocationError as exc:
        _retain_diagnostic(evidence, exc.stderr)
        raise JourneyProviderError("R-24 Linux provider invocation failed closed") from exc
    except ProviderContractError as exc:
        raise JourneyProviderError("R-24 Linux provider binding was rejected") from exc
    _retain_diagnostic(evidence, output.stderr)
    return output
