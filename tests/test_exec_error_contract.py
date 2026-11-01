"""ExecError contract: public fields, frozen dataclass, serialization,
envelope mapping, and ExecAstridError raised-path behavior.

T13 (original): ExecError contract on ExecutorRunResult and GenerationResult.
T21 (extended): Public fields, frozen dataclass, serialization output,
envelope mapping (``message`` → ``cause``, ``recovery`` → ``recovery_command``),
and ``ExecAstridError`` raised-path behavior.
"""

from __future__ import annotations

from dataclasses import FrozenInstanceError, asdict

import pytest

from astrid.core.contracts.errors import AstridError, AstridErrorEnvelope, _json_safe
from astrid.core.contracts.exec_error import (
    ExecAstridError,
    ExecError,
    error_from_missing_binaries,
    error_from_returncode,
)
from astrid.core.execution.executor.runner import ExecutorRunResult
from astrid.core.generation.backends.base import GenerationResult


# ============================================================================
# T13: factory helpers and result dataclass contracts (unchanged)
# ============================================================================


def test_error_from_returncode_derivation() -> None:
    assert error_from_returncode(None) is None
    assert error_from_returncode(0) is None
    derived = error_from_returncode(2)
    assert derived is not None
    assert derived.code == "nonzero_exit"


def test_error_from_missing_binaries_derivation() -> None:
    assert error_from_missing_binaries(()) is None
    derived = error_from_missing_binaries(("ffmpeg",))
    assert derived is not None
    assert derived.code == "missing_binaries"
    assert "ffmpeg" in derived.message


def test_executor_run_result_ok_derives_from_error() -> None:
    success = ExecutorRunResult(executor_id="x", kind="external", returncode=0)
    assert success.ok is True
    assert success.error is None


def test_executor_run_result_dry_run_and_skip_are_ok() -> None:
    dry = ExecutorRunResult(executor_id="x", kind="external", dry_run=True)
    skip = ExecutorRunResult(
        executor_id="x", kind="external", skipped=True, skipped_reason="cond"
    )
    assert dry.ok is True
    assert skip.ok is True


def test_executor_run_result_missing_binaries_surfaces_error() -> None:
    result = ExecutorRunResult(
        executor_id="x", kind="external", missing_binaries=("ffmpeg",)
    )
    assert result.ok is False
    assert result.error is not None
    assert result.error.code == "missing_binaries"


def test_executor_run_result_blocked_surfaces_error() -> None:
    blocked = ExecutorRunResult(
        executor_id="x",
        kind="external",
        error=ExecError(
            code="blocked",
            type="gate",
            message="run rejected by gate",
            recovery="resolve the gate and retry",
        ),
    )
    assert blocked.ok is False
    assert blocked.error is not None
    assert blocked.error.code == "blocked"


def test_executor_run_result_explicit_error_not_overwritten() -> None:
    # An explicit error survives even when returncode is 0.
    result = ExecutorRunResult(
        executor_id="x",
        kind="external",
        returncode=0,
        error=ExecError(code="blocked", type="gate", message="blocked"),
    )
    assert result.ok is False
    assert result.error.code == "blocked"


def test_generation_result_ok_derives_from_error() -> None:
    success = GenerationResult(model_actual="image/z_image")
    assert success.ok is True
    assert success.error is None

    failed = GenerationResult(
        error=ExecError(code="backend_error", type="process", message="boom")
    )
    assert failed.ok is False
    assert failed.error is not None
    assert failed.error.code == "backend_error"


# ============================================================================
# T21: public fields — all four fields are accessible via attribute access
# ============================================================================


def test_exec_error_public_fields_are_accessible() -> None:
    """code, type, message, recovery are plain dataclass fields."""
    err = ExecError(
        code="nonzero_exit",
        type="process",
        message="executor exited with returncode 1",
        recovery="inspect the executor output/logs and retry",
    )
    assert err.code == "nonzero_exit"
    assert err.type == "process"
    assert err.message == "executor exited with returncode 1"
    assert err.recovery == "inspect the executor output/logs and retry"


def test_exec_error_default_recovery_is_empty_string() -> None:
    """recovery defaults to '' when omitted."""
    err = ExecError(code="timeout", type="process", message="timed out")
    assert err.recovery == ""


# ============================================================================
# T21: frozen dataclass behavior — mutation raises FrozenInstanceError
# ============================================================================


def test_exec_error_is_frozen_dataclass() -> None:
    """Setting any field on an ExecError instance raises FrozenInstanceError."""
    err = ExecError(code="x", type="y", message="z")
    with pytest.raises(FrozenInstanceError):
        err.code = "mutated"  # type: ignore[misc]


def test_exec_error_is_frozen_dataclass_recovery_field() -> None:
    """Setting recovery on an ExecError instance raises FrozenInstanceError."""
    err = ExecError(code="x", type="y", message="z", recovery="retry")
    with pytest.raises(FrozenInstanceError):
        err.recovery = "mutated"  # type: ignore[misc]


# ============================================================================
# T21: serialization output — to_envelope() and asdict() round-trip
# ============================================================================


def test_exec_error_to_envelope_shape() -> None:
    """to_envelope() returns canonical + legacy mirrored keys."""
    err = ExecError(
        code="missing_binaries",
        type="precondition",
        message="missing required binaries: ffmpeg",
        recovery="install ffmpeg and retry",
    )
    envelope = err.to_envelope()

    # Canonical keys
    assert envelope["error_type"] == "ExecError"
    assert envelope["cause"] == "missing required binaries: ffmpeg"
    assert envelope["valid_options"] == []
    assert envelope["recovery_command"] == "install ffmpeg and retry"
    assert envelope["state_snapshot"] == {
        "code": "missing_binaries",
        "type": "precondition",
    }
    assert envelope["degraded"] is False
    # Legacy mirrored keys
    assert envelope["message"] == "missing required binaries: ffmpeg"
    assert envelope["reason"] == "missing required binaries: ffmpeg"
    assert envelope["recovery"] == "install ffmpeg and retry"
    assert envelope["code"] == "missing_binaries"
    assert envelope["source_type"] == "ExecError"


def test_exec_error_to_envelope_no_recovery() -> None:
    """to_envelope() with empty recovery still carries all keys."""
    err = ExecError(code="timeout", type="process", message="timed out")
    envelope = err.to_envelope()
    assert envelope["recovery_command"] == ""
    assert envelope["recovery"] == ""


def test_exec_error_asdict_roundtrip() -> None:
    """asdict(ExecError(...)) produces the four-field dict for SDK serialization."""
    err = ExecError(
        code="nonzero_exit",
        type="process",
        message="exit 1",
        recovery="inspect logs",
    )
    raw = asdict(err)
    assert raw == {
        "code": "nonzero_exit",
        "type": "process",
        "message": "exit 1",
        "recovery": "inspect logs",
    }


def test_exec_error_json_safe_roundtrip() -> None:
    """_json_safe(ExecError(...)) produces the same four-field dict as asdict."""
    err = ExecError(
        code="blocked",
        type="gate",
        message="run rejected",
        recovery="resolve gate",
    )
    safe = _json_safe(err)
    assert safe == {
        "code": "blocked",
        "type": "gate",
        "message": "run rejected",
        "recovery": "resolve gate",
    }


def test_exec_error_serialization_is_stable() -> None:
    """Serialization shape does not carry envelope-only properties as dict keys."""
    err = ExecError(code="stale", type="process", message="stale epoch")
    raw = asdict(err)
    # Only the four real dataclass fields appear — *not* the property-backed
    # envelope protocol fields (cause, valid_options, recovery_command,
    # state_snapshot, degraded).
    assert set(raw.keys()) == {"code", "type", "message", "recovery"}
    assert "cause" not in raw
    assert "valid_options" not in raw
    assert "recovery_command" not in raw
    assert "state_snapshot" not in raw
    assert "degraded" not in raw


# ============================================================================
# T21: envelope mapping — property bridge tests
# ============================================================================


def test_exec_error_envelope_properties_cause_mirrors_message() -> None:
    """cause property returns the same value as message."""
    err = ExecError(code="x", type="y", message="the message", recovery="retry")
    assert err.cause == err.message == "the message"


def test_exec_error_envelope_properties_recovery_command_mirrors_recovery() -> None:
    """recovery_command property returns the same value as recovery."""
    err = ExecError(code="x", type="y", message="z", recovery="retry now")
    assert err.recovery_command == err.recovery == "retry now"


def test_exec_error_envelope_properties_valid_options_is_empty() -> None:
    """valid_options is always an empty tuple."""
    err = ExecError(code="x", type="y", message="z")
    assert err.valid_options == ()


def test_exec_error_envelope_properties_state_snapshot_exposes_code_and_type() -> None:
    """state_snapshot returns a dict with code and type keys."""
    err = ExecError(code="timeout", type="process", message="timed out")
    assert err.state_snapshot == {"code": "timeout", "type": "process"}


def test_exec_error_envelope_properties_degraded_is_always_false() -> None:
    """ExecError is never a degraded envelope."""
    err = ExecError(code="x", type="y", message="z")
    assert err.degraded is False


def test_exec_error_satisfies_astrid_error_envelope_protocol() -> None:
    """ExecError implements the AstridErrorEnvelope protocol."""
    err = ExecError(code="nonzero_exit", type="process", message="exit 1", recovery="retry")
    assert isinstance(err, AstridErrorEnvelope)


# ============================================================================
# T21: ExecAstridError raised-path behavior
# ============================================================================


def test_exec_astrid_error_is_astrid_error_subclass() -> None:
    """ExecAstridError inherits from AstridError."""
    err = ExecAstridError("execution failed")
    assert isinstance(err, AstridError)
    assert isinstance(err, Exception)


def test_exec_astrid_error_can_be_raised_and_caught() -> None:
    """ExecAstridError can be raised like any exception."""
    with pytest.raises(ExecAstridError) as exc_info:
        raise ExecAstridError("child executor failed")
    assert exc_info.value.cause == "child executor failed"


def test_exec_astrid_error_raises_through_astrid_error_handler() -> None:
    """ExecAstridError can be caught as AstridError."""
    with pytest.raises(AstridError) as exc_info:
        raise ExecAstridError("execution failure")
    assert exc_info.value.cause == "execution failure"


def test_exec_astrid_error_with_exec_error_inherits_fields() -> None:
    """Passing exec_error= populates code, type, recovery from the ExecError."""
    ee = ExecError(
        code="nonzero_exit",
        type="process",
        message="exit 1",
        recovery="inspect logs",
    )
    err = ExecAstridError("execution failed", exec_error=ee)
    assert err.cause == "execution failed"
    assert err.exec_code == "nonzero_exit"
    assert err.exec_type == "process"
    assert err.exec_recovery == "inspect logs"


def test_exec_astrid_error_with_exec_error_and_explicit_overrides() -> None:
    """Explicit code/type/recovery take precedence over exec_error values."""
    ee = ExecError(
        code="nonzero_exit",
        type="process",
        message="exit 1",
        recovery="default recovery",
    )
    err = ExecAstridError(
        "execution failed",
        exec_error=ee,
        code="CUSTOM_CODE",
        type="custom_type",
        recovery="custom recovery",
    )
    assert err.exec_code == "CUSTOM_CODE"
    assert err.exec_type == "custom_type"
    assert err.exec_recovery == "custom recovery"


def test_exec_astrid_error_without_exec_error_has_no_exec_fields() -> None:
    """Without exec_error, exec_code/exec_type are None, exec_recovery is ''."""
    err = ExecAstridError("simple failure")
    assert err.exec_code is None
    assert err.exec_type is None
    assert err.exec_recovery == ""


def test_exec_astrid_error_to_envelope_shape() -> None:
    """ExecAstridError.to_envelope() carries the expected shape.

    Note: ``recovery`` is intentionally stored on ``exec_recovery`` and is
    *not* propagated into the base ``AstridError.recovery_command``.  The
    envelope therefore reflects the base class default (empty string).
    """
    ee = ExecError(
        code="missing_binaries",
        type="precondition",
        message="missing ffmpeg",
        recovery="install ffmpeg",
    )
    err = ExecAstridError("execution failed", exec_error=ee)
    envelope = err.to_envelope()
    assert envelope["error_type"] == "ExecAstridError"
    assert envelope["cause"] == "execution failed"
    assert envelope["source_type"] == "ExecAstridError"
    assert envelope["code"] == "missing_binaries"
    # Legacy fields — message/reason mirror cause, recovery is empty
    # (the exec-level recovery lives on .exec_recovery, not in the envelope)
    assert envelope["message"] == "execution failed"
    assert envelope["reason"] == "execution failed"
    assert envelope["recovery"] == ""


def test_exec_astrid_error_is_astrid_error_envelope() -> None:
    """ExecAstridError satisfies the AstridErrorEnvelope protocol."""
    err = ExecAstridError("failure")
    assert isinstance(err, AstridErrorEnvelope)
