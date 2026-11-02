"""Opt-in, read-only installer predicate observations.

The installer has several deliberately fail-closed admission gates.  This
module is only a recorder for the values those gates already read; it never
returns an authorization decision and deliberately swallows logging failures.
"""

from __future__ import annotations

import contextlib
import contextvars
import hashlib
import json
import os
import sys
from collections.abc import Iterator
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit

ENVIRONMENT_FLAG = "ASTRID_INSTALLER_ALL_PREDICATES_DIAGNOSTICS"
FORMAT = "astrid-installer-all-predicates/v1"
PREFIX = "[astrid installer all-predicates v1] "
_records: contextvars.ContextVar[list[dict] | None] = contextvars.ContextVar(
    "astrid_installer_predicate_records", default=None)


def enabled() -> bool:
    """Whether an operator explicitly requested observational output."""
    value = os.environ.get(ENVIRONMENT_FLAG, "")
    return value.strip().lower() in {"1", "true", "yes", "on"} or _records.get() is not None


def _safe_value(value: object, *, name: str = "") -> object:
    """Make a bounded, JSON-safe diagnostic value without serializing secrets."""
    if value is None or isinstance(value, (bool, int, float)):
        return value
    if isinstance(value, Path):
        value = str(value)
    if isinstance(value, bytes):
        return {"sha256": hashlib.sha256(value).hexdigest(), "bytes": len(value)}
    if isinstance(value, str):
        if "endpoint" in name:
            try:
                parsed = urlsplit(value)
                if parsed.scheme:
                    host = parsed.hostname or ""
                    port = f":{parsed.port}" if parsed.port is not None else ""
                    return urlunsplit((parsed.scheme, host + port, parsed.path[:256], "", ""))
            except (TypeError, ValueError):
                return "<invalid-endpoint>"
        return value[:512]
    if isinstance(value, (list, tuple)):
        return [_safe_value(item, name=name) for item in value[:32]]
    if isinstance(value, dict):
        return {str(key)[:96]: _safe_value(item, name=str(key))
                for key, item in list(value.items())[:32]}
    return repr(value)[:512]


def record(phase: str, predicate: str, *, expected: object = None, observed: object = None,
           result: str, provenance: str, reason: str | None = None,
           depends_on: str | None = None) -> None:
    """Emit one passive predicate result when diagnostics are enabled.

    ``result`` is intentionally distinct from a boolean: missing evidence is
    ``unknown`` and a check skipped after a failed prerequisite is
    ``unavailable``.  Neither is authorization.
    """
    if not enabled() or result not in {"pass", "fail", "unknown", "unavailable"}:
        return
    try:
        item = {
            "format": FORMAT,
            "phase": phase,
            "predicate": predicate,
            "expected": _safe_value(expected, name=predicate),
            "observed": _safe_value(observed, name=predicate),
            "result": result,
            "provenance": provenance[:256],
        }
        if reason:
            item["reason"] = reason[:256]
        if depends_on:
            item["depends_on"] = depends_on[:256]
        captured = _records.get()
        if captured is not None:
            captured.append(item)
        if os.environ.get(ENVIRONMENT_FLAG, "").strip().lower() in {"1", "true", "yes", "on"}:
            print(PREFIX + json.dumps(item, sort_keys=True, separators=(",", ":")), file=sys.stderr)
    except (OSError, TypeError, ValueError):
        # Diagnostics must never mask the original authorization outcome.
        return


def check(phase: str, predicate: str, *, expected: object, observed: object,
          provenance: str, reason: str | None = None) -> bool:
    """Record equality/boolean predicates and return their existing outcome."""
    outcome = observed if isinstance(expected, bool) and isinstance(observed, bool) else observed == expected
    record(phase, predicate, expected=expected, observed=observed,
           result="pass" if outcome else "fail", provenance=provenance, reason=reason)
    return outcome


def unknown(phase: str, predicate: str, *, expected: object, provenance: str,
            reason: str) -> None:
    record(phase, predicate, expected=expected, result="unknown", provenance=provenance, reason=reason)


def unavailable(phase: str, predicate: str, *, expected: object, provenance: str,
                depends_on: str, reason: str) -> None:
    record(phase, predicate, expected=expected, result="unavailable", provenance=provenance,
           depends_on=depends_on, reason=reason)


@contextlib.contextmanager
def capture() -> Iterator[list[dict]]:
    """Collect records for focused tests without enabling process stderr output."""
    captured: list[dict] = []
    token = _records.set(captured)
    try:
        yield captured
    finally:
        _records.reset(token)
