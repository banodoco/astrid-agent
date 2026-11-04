"""Pack actions raise AstridError / AstridValueError; run_pack_main renders them with a recovery hint."""

from __future__ import annotations

import pytest

from astrid.core.contracts.errors import AstridError, AstridValueError
from astrid.core.element.cli import build_parser
from astrid.core.pack.entrypoint import run_pack_main


def test_astrid_value_error_is_still_a_value_error() -> None:
    assert issubclass(AstridValueError, AstridError)
    with pytest.raises(ValueError):
        raise AstridValueError("bad input")


@pytest.mark.parametrize("exc", [AstridError("boom"), AstridValueError("boom")])
def test_run_pack_main_adds_default_recovery_to_bare_astrid_errors(exc: AstridError, capsys) -> None:
    def runner() -> int:
        raise exc

    assert run_pack_main("demo.cap", runner) == 2
    err = capsys.readouterr().err
    assert "boom" in err
    assert "recovery: python3 -m astrid executors run demo.cap --help" in err
    assert "demo.cap" in err
    assert exc.recovery == exc.recovery_command
    assert exc.state_snapshot == {"argv": [], "capability_id": "demo.cap"}


def test_run_pack_main_keeps_explicit_recovery(capsys) -> None:
    def runner() -> int:
        raise AstridError("boom", recovery_command="do the thing")

    run_pack_main("demo.cap", runner)
    assert "recovery: do the thing" in capsys.readouterr().err


def test_project_pack_resolve_kind_accepts_catalog_names() -> None:
    parser = build_parser()
    base = ["project-pack", "resolve", "--realm", "r", "--project", "p", "--pack", "k", "--element", "e", "--revision", "x"]
    for kind in ("effect", "effects", "transition"):
        assert parser.parse_args([*base, "--kind", kind]).kind == kind
    with pytest.raises(SystemExit):
        parser.parse_args([*base, "--kind", "bogus"])


def test_run_pack_main_preserves_sdk_error_details(capsys) -> None:
    from astrid.sdk.exceptions import CapabilityValidationError

    def runner() -> int:
        raise CapabilityValidationError("bad mode", details={"field": "mode", "fix": "choose fast"})

    assert run_pack_main("demo.cap", runner) == 2
    err = capsys.readouterr().err
    assert "code: validation" in err
    assert "field: mode" in err
    assert "fix: choose fast" in err
    assert "recovery: python3 -m astrid executors run demo.cap --help" in err


@pytest.mark.parametrize("error", [ValueError("bad input"), RuntimeError("bad input")])
def test_run_pack_main_keeps_controlled_error_recovery(error, capsys) -> None:
    def runner() -> int:
        raise error

    assert run_pack_main("demo.cap", runner, recovery_command="choose a valid input") == 2
    stderr = capsys.readouterr().err
    assert "fix: choose a valid input" in stderr
    assert "recovery: choose a valid input" in stderr
    assert "unstructured" not in stderr
    assert "Traceback" not in stderr


def test_run_pack_main_keeps_unknown_error_degraded(capsys) -> None:
    def runner() -> int:
        raise LookupError("unexpected")

    assert run_pack_main("demo.cap", runner) == 1
    stderr = capsys.readouterr().err
    assert stderr.splitlines()[0] == "unstructured - this is a bug."
    assert "unexpected" in stderr
    assert "report this bug" in stderr
    assert '"original_type": "LookupError"' in stderr
    assert "Traceback" not in stderr
