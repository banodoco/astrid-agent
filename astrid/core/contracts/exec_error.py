"""Structured execution error attached to capability run results.

A run result is successful exactly when it carries no :class:`ExecError`.
Callers derive ``ok`` from ``error is None`` rather than inspecting a raw
process ``returncode`` or a separate ``missing_binaries`` flag, so every
failure mode (nonzero exit, missing binary, gate/produce-check rejection)
flows through a single typed channel.

:class:`ExecError` remains a frozen result dataclass (not an Exception
subclass).  For raised execution failures use :class:`ExecAstridError`,
which inherits from :class:`AstridError` and carries the same structured
execution-failure metadata.
"""

from __future__ import annotations

from dataclasses import InitVar, dataclass
from typing import Any

from astrid.core.contracts.errors import AstridError


@dataclass(frozen=True, init=False, eq=False)
class ExecError:
    """Why a capability run did not succeed.

    Attributes:
        code: Stable machine-readable identifier (e.g. ``"missing_binaries"``,
            ``"nonzero_exit"``, ``"blocked"``).
        type: Coarse category the code belongs to (e.g. ``"precondition"``,
            ``"process"``, ``"gate"``).
        message: Human-readable description of the failure.
        recovery: Optional hint describing how to recover.
        field: Optional affected input or response field.
        fix: Optional actionable fix, defaulting to recovery.
    """

    code: str
    type: str
    message: str
    recovery: str = ""
    field: InitVar[str | None] = None
    fix: InitVar[str | None] = None

    def __init__(
        self, code: str, type: str, message: str, recovery: str = "", *,
        field: str | None = None, fix: str | None = None,
    ) -> None:
        # Keep the four historical dataclass fields (and asdict shape) intact.
        object.__setattr__(self, "code", code)
        object.__setattr__(self, "type", type)
        object.__setattr__(self, "message", message)
        object.__setattr__(self, "recovery", recovery)
        object.__setattr__(self, "_field", field)
        object.__setattr__(self, "_fix", fix)

    def __eq__(self, other: object) -> bool:
        if other.__class__ is not self.__class__:
            return NotImplemented
        return (self.code, self.type, self.message, self.recovery, self.field, self.fix) == (
            other.code, other.type, other.message, other.recovery, other.field, other.fix
        )

    def __hash__(self) -> int:
        return hash((self.code, self.type, self.message, self.recovery, self.field, self.fix))

    # -- AstridErrorEnvelope protocol compatibility ---------------------------
    # These properties allow ExecError-bearing result dataclasses to render
    # through the shared envelope pipeline without being an Exception subclass.
    # The extra values are not dataclass fields, keeping the existing
    # asdict/JSON shape unchanged.

    @property
    def field(self) -> str | None:
        """Affected field, when supplied."""
        return getattr(self, "_field", None)

    @property
    def fix(self) -> str:
        """Expose an explicit fix or the existing recovery hint."""
        return getattr(self, "_fix", None) or self.recovery

    @property
    def cause(self) -> str:
        """Canonical envelope cause — mirrors ``message``."""
        return self.message

    @property
    def valid_options(self) -> tuple[str, ...]:
        """ExecError has no built-in valid-options surface; always empty."""
        return ()

    @property
    def recovery_command(self) -> str:
        """Canonical envelope recovery command — mirrors ``recovery``."""
        return self.recovery

    @property
    def state_snapshot(self) -> dict[str, Any]:
        """Compact state snapshot exposing code and type for renderers."""
        return {"code": self.code, "type": self.type}

    @property
    def degraded(self) -> bool:
        """ExecError is never a degraded unstructured envelope."""
        return False

    def to_envelope(self) -> dict[str, Any]:
        """Return the serializable render envelope for this ExecError."""
        return {
            "error_type": self.__class__.__name__,
            "cause": self.cause,
            "valid_options": list(self.valid_options),
            "recovery_command": self.recovery_command,
            "state_snapshot": self.state_snapshot,
            "degraded": self.degraded,
            # Legacy mirrored keys retained for existing callers/tests.
            "message": self.message,
            "field": self.field,
            "fix": self.fix,
            "reason": self.message,
            "recovery": self.recovery,
            "code": self.code,
            "source_type": "ExecError",
        }


class ExecAstridError(AstridError):
    """Raised when a capability execution failure must propagate as an exception.

    Use this instead of a bare ``ExecError`` dataclass when the failure
    needs to unwind the call stack (e.g. from an orchestrator that cannot
    continue after a child executor fails).  Constructors such as
    :func:`error_from_returncode` and :func:`error_from_missing_binaries`
    continue to return plain :class:`ExecError` dataclasses — they are
    never raised.
    """

    def __init__(
        self,
        cause: str,
        *,
        exec_error: ExecError | None = None,
        code: str | None = None,
        type: str | None = None,
        recovery: str | None = None,
        **kwargs: Any,
    ) -> None:
        explicit_recovery = recovery
        if exec_error is not None:
            code = code or exec_error.code
            type = type or exec_error.type
            recovery = recovery or exec_error.recovery
            if exec_error.field and not kwargs.get("field"):
                kwargs["field"] = exec_error.field
            if exec_error.fix and not explicit_recovery and not kwargs.get("fix"):
                kwargs["fix"] = exec_error.fix
        if recovery and not kwargs.get("fix") and not kwargs.get("recovery_command"):
            kwargs["fix"] = recovery
        super().__init__(
            cause,
            code=code,
            source_type="ExecAstridError",
            **kwargs,
        )
        self.exec_code = code
        self.exec_type = type
        self.exec_recovery = recovery or ""


def error_from_returncode(returncode: int | None) -> ExecError | None:
    """Derive an :class:`ExecError` from a process return code.

    Returns ``None`` for ``None`` (no process ran) or ``0`` (success); a
    populated :class:`ExecError` for any nonzero exit.
    """
    if returncode is None or returncode == 0:
        return None
    return ExecError(
        code="nonzero_exit",
        type="process",
        message=f"executor exited with returncode {returncode}",
        recovery="inspect the executor output/logs and retry",
    )


def error_from_missing_binaries(missing_binaries: tuple[str, ...]) -> ExecError | None:
    """Derive an :class:`ExecError` from a missing-binary precondition."""
    if not missing_binaries:
        return None
    return ExecError(
        code="missing_binaries",
        type="precondition",
        message=f"missing required binaries: {', '.join(missing_binaries)}",
        recovery="install the missing binaries and retry",
    )


__all__ = [
    "ExecAstridError",
    "ExecError",
    "error_from_missing_binaries",
    "error_from_returncode",
]
