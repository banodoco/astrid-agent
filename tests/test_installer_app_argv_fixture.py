"""The App's generated install arguments match the real installer CLI."""

import json
from pathlib import Path

import pytest

from astrid.installer import cli
from astrid.installer.choices import resolve_choices


FIXTURE = Path(__file__).parent / "fixtures" / "installer" / "app_argv.fixture.json"
CASES = json.loads(FIXTURE.read_text(encoding="utf-8"))["cases"]


def test_fixture_has_cases():
    assert CASES


@pytest.mark.parametrize("case", CASES, ids=lambda case: case["id"])
def test_app_argv_resolves_installer_choices(case, capsys):
    args = cli.parser().parse_args(case["argv"])
    choices = resolve_choices(explicit=args.choices, accept_recommended=args.yes)
    assert {name: "on" if value else "off" for name, value in choices.items()} == case["choices"]

    with pytest.raises(SystemExit) as exc:
        cli.parser().parse_args([*case["argv"], "--unknown-app-fixture-flag"])
    assert exc.value.code == 2
    assert "unrecognized arguments: --unknown-app-fixture-flag" in capsys.readouterr().err
