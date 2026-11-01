"""Shared error-envelope contract for operator and agent-facing failures.

This module introduces the canonical Astrid recoverability envelope:

* ``cause`` — what went wrong
* ``valid_options`` — recovery-safe alternatives, when known
* ``recovery_command`` — the next command the caller should run
* ``state_snapshot`` — compact state the renderer can surface verbatim

The contract must also tolerate legacy error shapes already present in the
codebase (``message`` / ``reason`` / ``recovery``) and non-exception result
objects such as ``ExecError``-bearing dataclasses.
"""

from __future__ import annotations

import json
import sys
from dataclasses import asdict, is_dataclass
from pathlib import Path
from typing import Any, Protocol, runtime_checkable


def _json_safe(value: Any) -> Any:
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if isinstance(value, Path):
        return str(value)
    if isinstance(value, dict):
        return {str(key): _json_safe(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json_safe(item) for item in value]
    to_dict = getattr(value, "to_dict", None)
    if callable(to_dict):
        return _json_safe(to_dict())
    if is_dataclass(value):
        return _json_safe(asdict(value))
    return str(value)


def normalize_valid_options(*values: object) -> tuple[str, ...]:
    """Return a stable, de-duplicated tuple of non-empty option strings."""

    flattened: list[str] = []
    for value in values:
        if value is None:
            continue
        if isinstance(value, str):
            if value.strip():
                flattened.append(value)
            continue
        if isinstance(value, (list, tuple, set, frozenset)):
            for item in value:
                if isinstance(item, str) and item.strip():
                    flattened.append(item)
            continue
        valid_options = getattr(value, "valid_options", None)
        if valid_options is not None and valid_options is not value:
            flattened.extend(normalize_valid_options(valid_options))

    seen: set[str] = set()
    ordered: list[str] = []
    for item in flattened:
        if item not in seen:
            seen.add(item)
            ordered.append(item)
    return tuple(ordered)


def build_state_snapshot(
    snapshot: object | None = None,
    /,
    **fields: object,
) -> dict[str, Any]:
    """Return a JSON-safe state snapshot with ``None`` values omitted."""

    payload: dict[str, Any] = {}
    if snapshot is not None:
        if isinstance(snapshot, dict):
            payload.update(snapshot)
        else:
            raw = _json_safe(snapshot)
            if isinstance(raw, dict):
                payload.update(raw)
            else:
                payload["value"] = raw
    for key, value in fields.items():
        if value is not None:
            payload[key] = value
    return {str(key): _json_safe(value) for key, value in payload.items() if value is not None}


def _merge_state_snapshots(*snapshots: object | None) -> dict[str, Any]:
    merged: dict[str, Any] = {}
    for snapshot in snapshots:
        if snapshot is not None:
            merged.update(build_state_snapshot(snapshot))
    return merged


@runtime_checkable
class AstridErrorEnvelope(Protocol):
    """Protocol implemented by renderable Astrid recoverability envelopes."""

    cause: str
    valid_options: tuple[str, ...]
    recovery_command: str
    state_snapshot: dict[str, Any]
    degraded: bool

    def to_envelope(self) -> dict[str, Any]:
        """Return the serializable render envelope."""


class AstridError(RuntimeError):
    """Canonical recoverability error envelope."""

    def __init__(
        self,
        cause: str,
        *,
        valid_options: object = (),
        recovery_command: str | None = None,
        state_snapshot: object | None = None,
        code: str | None = None,
        degraded: bool = False,
        source_type: str | None = None,
    ) -> None:
        super().__init__(cause)
        self.cause = cause
        self.valid_options = normalize_valid_options(valid_options)
        self.recovery_command = recovery_command or ""
        self.state_snapshot = build_state_snapshot(state_snapshot)
        self.code = code
        self.degraded = degraded
        self.source_type = source_type
        # Legacy field mappings retained for existing callers/tests.
        self.message = cause
        self.reason = cause
        self.recovery = self.recovery_command

    def to_envelope(self) -> dict[str, Any]:
        payload = {
            "error_type": self.__class__.__name__,
            "cause": self.cause,
            "valid_options": list(self.valid_options),
            "recovery_command": self.recovery_command,
            "state_snapshot": build_state_snapshot(self.state_snapshot),
            "degraded": self.degraded,
            # Legacy mirrored keys.
            "message": self.message,
            "reason": self.reason,
            "recovery": self.recovery,
        }
        if self.code:
            payload["code"] = self.code
        if self.source_type:
            payload["source_type"] = self.source_type
        return payload


class AstridValueError(AstridError, ValueError):
    """``AstridError`` for invalid input that callers may still catch as ``ValueError``."""


def error_from_result(result: object) -> AstridError | None:
    """Convert a non-exception result object into an ``AstridError``.

    Returns ``None`` when the object carries no structured error.
    """

    inner = getattr(result, "error", None)
    if inner is None:
        return None
    snapshot = build_state_snapshot(
        result,
        result_type=type(result).__name__,
        ok=getattr(result, "ok", None),
    )
    return coerce_astrid_error(inner, state_snapshot=snapshot)


def coerce_astrid_error(
    value: object,
    *,
    state_snapshot: object | None = None,
    degraded: bool = False,
) -> AstridError:
    """Normalize exceptions, legacy shapes, and result payloads."""

    if isinstance(value, AstridError):
        if state_snapshot is None and degraded == value.degraded:
            return value
        return AstridError(
            value.cause,
            valid_options=value.valid_options,
            recovery_command=value.recovery_command,
            state_snapshot=_merge_state_snapshots(value.state_snapshot, state_snapshot),
            code=value.code,
            degraded=value.degraded or degraded,
            source_type=value.source_type,
        )

    result_error = error_from_result(value)
    if result_error is not None:
        if state_snapshot is None and degraded == result_error.degraded:
            return result_error
        return coerce_astrid_error(
            result_error,
            state_snapshot=_merge_state_snapshots(result_error.state_snapshot, state_snapshot),
            degraded=degraded,
        )

    cause = _first_text_attr(value, "cause", "message", "reason")
    if cause is None:
        cause = str(value)
    recovery_command = _first_text_attr(value, "recovery_command", "recovery") or ""
    options = normalize_valid_options(getattr(value, "valid_options", ()))
    code = _first_text_attr(value, "code")
    source_type = type(value).__name__
    snapshot = build_state_snapshot(getattr(value, "state_snapshot", None))
    if state_snapshot is not None:
        snapshot.update(build_state_snapshot(state_snapshot))
    if degraded and "bug" not in recovery_command.lower():
        recovery_command = recovery_command or "retry the command; if it repeats, report this bug"
    return AstridError(
        cause,
        valid_options=options,
        recovery_command=recovery_command,
        state_snapshot=snapshot,
        code=code,
        degraded=degraded,
        source_type=source_type,
    )


def wrap_degraded_error(
    value: object,
    *,
    state_snapshot: object | None = None,
) -> AstridError:
    """Wrap an escaped bare exception as a degraded Astrid envelope."""

    snapshot = build_state_snapshot(
        state_snapshot,
        original_type=type(value).__name__,
    )
    return coerce_astrid_error(value, state_snapshot=snapshot, degraded=True)


def recovery_suffix(exc: BaseException) -> str:
    """Return `` Next: <recovery_command>`` when ``exc`` carries one, else ``""``.

    ``AstridError`` passes only ``cause`` to ``RuntimeError``, so ``str(exc)``
    drops the recovery command; callers that stringify a failure append this.
    """

    recovery = getattr(exc, "recovery_command", None)
    if not isinstance(recovery, str) or not recovery.strip():
        return ""
    recovery = recovery.strip()
    if recovery in str(exc):
        return ""
    return f" Next: {recovery}"


def render_astrid_error(error: AstridError) -> int:
    """Render an ``AstridError`` envelope to stderr and return its exit code."""

    if error.degraded:
        print("unstructured - this is a bug.", file=sys.stderr)
    print(error.cause, file=sys.stderr)
    if error.valid_options:
        print(f"valid options: {', '.join(error.valid_options)}", file=sys.stderr)
    if error.recovery_command:
        print(f"recovery: {error.recovery_command}", file=sys.stderr)
    if error.state_snapshot:
        print(
            f"state snapshot: {json.dumps(error.state_snapshot, sort_keys=True)}",
            file=sys.stderr,
        )
    return 1 if error.degraded else 2


def _first_text_attr(value: object, *names: str) -> str | None:
    for name in names:
        candidate = getattr(value, name, None)
        if isinstance(candidate, str) and candidate:
            return candidate
    return None


__all__ = [
    "AstridError",
    "AstridErrorEnvelope",
    "AstridValueError",
    "build_state_snapshot",
    "coerce_astrid_error",
    "error_from_result",
    "normalize_valid_options",
    "render_astrid_error",
    "wrap_degraded_error",
]
