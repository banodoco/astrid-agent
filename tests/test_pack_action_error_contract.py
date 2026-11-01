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
