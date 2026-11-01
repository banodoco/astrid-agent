from __future__ import annotations

from dataclasses import dataclass

from astrid.core.contracts.errors import (
    AstridError,
    build_state_snapshot,
    coerce_astrid_error,
    error_from_result,
    normalize_valid_options,
    wrap_degraded_error,
)
from astrid.core.contracts.exec_error import ExecError
from astrid.core.project.runtime import ProjectRuntimeError
from astrid.core.project.schema import ProjectValidationError


@dataclass(frozen=True)
class _FakeResult:
    error: object | None = None
    ok: bool = False
    returncode: int | None = None


def test_astrid_error_exposes_canonical_and_legacy_fields() -> None:
    err = AstridError(
        "bad transition kind",
        valid_options=("cross-fade", "wipe"),
        recovery_command="astrid timelines transition set --kind cross-fade",
        state_snapshot={"project": "demo", "timeline": "main"},
        code="invalid_transition_kind",
    )

    assert err.cause == "bad transition kind"
    assert err.message == err.cause
    assert err.reason == err.cause
    assert err.recovery == err.recovery_command

    envelope = err.to_envelope()
    assert envelope["cause"] == "bad transition kind"
    assert envelope["valid_options"] == ["cross-fade", "wipe"]
    assert envelope["recovery_command"] == "astrid timelines transition set --kind cross-fade"
    assert envelope["state_snapshot"] == {"project": "demo", "timeline": "main"}
    assert envelope["message"] == envelope["reason"] == "bad transition kind"
    assert envelope["recovery"] == envelope["recovery_command"]
    assert envelope["code"] == "invalid_transition_kind"


def test_normalize_valid_options_and_state_snapshot_helpers() -> None:
    assert normalize_valid_options("cross-fade", ("cross-fade", "wipe"), "", None) == (
        "cross-fade",
        "wipe",
    )
    snapshot = build_state_snapshot({"project": "demo"}, timeline="main", none_value=None)
    assert snapshot == {"project": "demo", "timeline": "main"}


def test_coerce_astrid_error_maps_legacy_exception_fields() -> None:
    legacy = ProjectRuntimeError(
        cause="step is blocked",
        recovery_command="astrid projects ls",
        code="project_blocked",
    )

    err = coerce_astrid_error(legacy)

    assert err.cause == "step is blocked"
    assert err.recovery_command == "astrid projects ls"
    assert err.code == "project_blocked"
    # ProjectRuntimeError is itself an AstridError subclass: coerce_astrid_error
    # returns the same instance unchanged (no source_type is stamped) when no
    # merge is required.
    assert err is legacy
    assert err.source_type is None


def test_error_from_result_converts_non_exception_exec_error_payloads() -> None:
    result = _FakeResult(
        error=ExecError(
            code="missing_binaries",
            type="precondition",
            message="missing required binaries: ffmpeg",
            recovery="install ffmpeg and retry",
        ),
        ok=False,
        returncode=127,
    )

    err = error_from_result(result)

    assert err is not None
    assert err.cause == "missing required binaries: ffmpeg"
    assert err.recovery_command == "install ffmpeg and retry"
    assert err.code == "missing_binaries"
    assert err.state_snapshot["result_type"] == "_FakeResult"
    assert err.state_snapshot["ok"] is False
    assert err.state_snapshot["returncode"] == 127


def test_wrap_degraded_error_marks_unstructured_failures() -> None:
    err = wrap_degraded_error(ValueError("boom"), state_snapshot={"project": "demo"})

    assert err.degraded is True
    assert err.cause == "boom"
    assert err.source_type == "ValueError"
    assert err.state_snapshot["project"] == "demo"
    assert err.state_snapshot["original_type"] == "ValueError"
    assert "bug" in err.recovery_command.lower()


# -- missing / absent state snapshots ---------------------------------------


def test_astrid_error_with_absent_state_snapshot_yields_empty_dict() -> None:
    """state_snapshot=None / omitted → {} in both attribute and envelope."""
    err = AstridError("missing snapshot", valid_options=("a",), recovery_command="rc")

    assert err.state_snapshot == {}
    envelope = err.to_envelope()
    assert envelope["state_snapshot"] == {}


def test_build_state_snapshot_with_none_returns_empty_dict() -> None:
    assert build_state_snapshot(None) == {}
    assert build_state_snapshot(None, extra=None) == {}
    assert build_state_snapshot(None, present="val") == {"present": "val"}


def test_build_state_snapshot_with_non_dict_stores_under_value() -> None:
    snapshot = build_state_snapshot(42)
    assert snapshot == {"value": 42}


def test_build_state_snapshot_with_path_serializes() -> None:
    from pathlib import Path

    snapshot = build_state_snapshot(Path("/tmp/run"))
    assert snapshot == {"value": "/tmp/run"}


def test_error_from_result_with_no_error_returns_none() -> None:
    clean = _FakeResult(error=None, ok=True, returncode=0)
    assert error_from_result(clean) is None


def test_wrap_degraded_error_without_state_snapshot() -> None:
    err = wrap_degraded_error(RuntimeError("unexpected"))

    assert err.degraded is True
    assert err.cause == "unexpected"
    assert err.state_snapshot == {"original_type": "RuntimeError"}


# -- AstridErrorEnvelope protocol -------------------------------------------


def test_astrid_error_envelope_protocol_isinstance_positive() -> None:
    from astrid.core.contracts.errors import AstridErrorEnvelope

    err = AstridError("something")
    assert isinstance(err, AstridErrorEnvelope)


def test_astrid_error_envelope_protocol_isinstance_negative() -> None:
    from astrid.core.contracts.errors import AstridErrorEnvelope

    class Plain:
        pass

    assert not isinstance(Plain(), AstridErrorEnvelope)


# -- coerce_astrid_error rewrapping / edge cases ----------------------------


def test_coerce_astrid_error_rewraps_with_merged_snapshot() -> None:
    inner = AstridError("inner", state_snapshot={"a": 1})
    outer = coerce_astrid_error(inner, state_snapshot={"b": 2})

    assert outer.cause == "inner"
    assert outer.valid_options == inner.valid_options
    assert outer.state_snapshot == {"a": 1, "b": 2}
    # inner was created without explicit source_type, so it stays None
    assert outer.source_type is None


def test_coerce_astrid_error_returns_same_instance_when_no_merge_needed() -> None:
    inner = AstridError("same")
    outer = coerce_astrid_error(inner)
    assert outer is inner


def test_coerce_astrid_error_from_plain_string() -> None:
    err = coerce_astrid_error("something went wrong")

    assert err.cause == "something went wrong"
    assert err.source_type == "str"
    assert err.valid_options == ()


def test_coerce_astrid_error_from_none() -> None:
    err = coerce_astrid_error(None)

    # str(None) is "None"
    assert err.cause == "None"
    assert err.source_type == "NoneType"


def test_coerce_astrid_error_from_object_with_legacy_message() -> None:
    @dataclass(frozen=True)
    class LegacyErr:
        message: str
        recovery: str = ""
        code: str = "E001"

    legacy = LegacyErr(message="failed", recovery="retry", code="E001")
    err = coerce_astrid_error(legacy)

    assert err.cause == "failed"
    assert err.recovery_command == "retry"
    assert err.code == "E001"
    assert err.source_type == "LegacyErr"


def test_coerce_astrid_error_chain_through_result() -> None:
    result = _FakeResult(
        error=ExecError(
            code="nonzero_exit",
            type="process",
            message="exit 1",
            recovery="inspect logs",
        ),
        ok=False,
        returncode=1,
    )
    err = coerce_astrid_error(result, state_snapshot={"ctx": "pipeline"})

    assert err.cause == "exit 1"
    assert err.code == "nonzero_exit"
    assert err.recovery_command == "inspect logs"
    # The snapshot should carry both the result-level state and caller-provided ctx
    assert err.state_snapshot.get("result_type") == "_FakeResult"
    assert err.state_snapshot.get("ctx") == "pipeline"
    assert err.state_snapshot.get("returncode") == 1


# -- normalize_valid_options edge cases -------------------------------------


def test_normalize_valid_options_from_object_with_valid_options_attr() -> None:
    # An object bearing its own .valid_options, e.g. another AstridError
    source = AstridError("x", valid_options=("alpha", "beta"))

    result = normalize_valid_options(source)
    assert result == ("alpha", "beta")


def test_normalize_valid_options_from_set_and_frozenset() -> None:
    result = normalize_valid_options({"a", "b"}, frozenset({"b", "c"}))
    # Set iteration order is hash-dependent, so compare as sorted.
    assert sorted(result) == ["a", "b", "c"]
    assert len(result) == 3  # 'b' deduplicated


def test_normalize_valid_options_all_empty_and_none() -> None:
    result = normalize_valid_options(None, "", "   ", [], ())
    assert result == ()


# -- AstridError defaults and optional fields -------------------------------


def test_astrid_error_minimal_constructor() -> None:
    err = AstridError("minimal")

    assert err.cause == "minimal"
    assert err.valid_options == ()
    assert err.recovery_command == ""
    assert err.state_snapshot == {}
    assert err.code is None
    assert err.degraded is False
    assert err.source_type is None
    # Legacy fields
    assert err.message == "minimal"
    assert err.reason == "minimal"
    assert err.recovery == ""


def test_to_envelope_omits_code_and_source_type_when_none() -> None:
    err = AstridError("no extras")
    envelope = err.to_envelope()

    assert "code" not in envelope
    assert "source_type" not in envelope


def test_to_envelope_includes_code_and_source_type_when_set() -> None:
    err = AstridError("has extras", code="E42", source_type="MyError")
    envelope = err.to_envelope()

    assert envelope["code"] == "E42"
    assert envelope["source_type"] == "MyError"


def test_astrid_error_degraded_flag_in_envelope() -> None:
    err = AstridError("degraded", degraded=True)
    assert err.degraded is True
    assert err.to_envelope()["degraded"] is True

    normal = AstridError("normal")
    assert normal.degraded is False
    assert normal.to_envelope()["degraded"] is False


def test_coerce_astrid_error_degraded_merge() -> None:
    inner = AstridError("inner", degraded=False)
    outer = coerce_astrid_error(inner, degraded=True)

    assert outer.degraded is True
    assert outer.cause == "inner"


# ============================================================================
# T19: migrated core error → AstridError class contract
# ============================================================================


def test_project_runtime_error_is_astrid_error() -> None:
    err = ProjectRuntimeError(cause="blocked", recovery_command="astrid projects ls")
    assert isinstance(err, AstridError)
    # Legacy fields preserved per T18 migration contract.
    assert err.reason == "blocked"
    assert err.recovery == "astrid projects ls"
    assert err.cause == "blocked"
    assert err.message == "blocked"


def test_project_validation_error_is_astrid_error() -> None:
    err = ProjectValidationError("no project file at /tmp/projects/X.json")
    assert isinstance(err, AstridError)
    assert err.cause == "no project file at /tmp/projects/X.json"
    assert err.message == "no project file at /tmp/projects/X.json"
    assert err.reason == "no project file at /tmp/projects/X.json"
    assert err.degraded is False


def test_project_runtime_error_is_astrid_error() -> None:
    err = ProjectRuntimeError("project 'demo' already exists")
    assert isinstance(err, AstridError)
    assert err.cause == "project 'demo' already exists"
    assert err.message == "project 'demo' already exists"
    assert err.reason == "project 'demo' already exists"
    assert err.degraded is False


def test_project_validation_error_is_astrid_error() -> None:
    err = ProjectValidationError("source.kind must be one of {...}")
    assert isinstance(err, AstridError)
    assert isinstance(err, ValueError)  # legacy MRO preserved
    assert err.cause == "source.kind must be one of {...}"
    assert err.message == "source.kind must be one of {...}"
    assert err.reason == "source.kind must be one of {...}"
    assert err.degraded is False


def test_all_migrated_errors_coerce_to_canonical_envelope() -> None:
    """Every migrated error class round-trips through coerce_astrid_error."""
    cases: list[tuple[AstridError, str]] = [
        (ProjectRuntimeError("project exists"), "project exists"),
        (ProjectValidationError("invalid field"), "invalid field"),
    ]
    for orig, expected_cause in cases:
        coerced = coerce_astrid_error(orig)
        assert coerced.cause == expected_cause
        assert coerced.degraded is False
        # envelope round-trip
        env = coerced.to_envelope()
        assert env["cause"] == expected_cause
        assert env["message"] == expected_cause
        assert env["reason"] == expected_cause
        assert env["degraded"] is False


def test_recovery_suffix_appends_next_command_to_stringified_failure() -> None:
    from astrid.core.contracts.errors import recovery_suffix

    err = AstridError(
        "ffmpeg is not installed",
        recovery_command="install ffmpeg and retry",
    )

    # RuntimeError only receives ``cause``, so the recovery is absent from str().
    assert str(err) == "ffmpeg is not installed"
    assert recovery_suffix(err) == " Next: install ffmpeg and retry"
    assert str(err) + recovery_suffix(err) == "ffmpeg is not installed Next: install ffmpeg and retry"


def test_recovery_suffix_is_empty_without_recovery_or_for_plain_exceptions() -> None:
    from astrid.core.contracts.errors import recovery_suffix

    assert recovery_suffix(AstridError("no fix known")) == ""
    assert recovery_suffix(AstridError("blank", recovery_command="   ")) == ""
    assert recovery_suffix(RuntimeError("plain failure")) == ""


def test_recovery_suffix_does_not_repeat_command_already_in_message() -> None:
    from astrid.core.contracts.errors import recovery_suffix

    err = AstridError(
        "run astrid doctor --json first",
        recovery_command="astrid doctor --json",
    )

    assert recovery_suffix(err) == ""
