"""PK-5: the shots and references command lines ship in core and work from the installed wheel.

The former ``astrid/packs/shots/cli.py`` and ``astrid/packs/references/cli.py`` now live at
``astrid/core/cli/domain_shots.py`` and ``astrid/core/cli/domain_references.py``; storage stays in
the Runtime. Both families stay nested (``astrid timelines shots``, ``astrid media references``).
"""
from __future__ import annotations

import json

import pytest

from tests.integrations._installed_wheel import build_installed_wheel


@pytest.fixture(scope="module")
def installed(tmp_path_factory):
    return build_installed_wheel(tmp_path_factory.mktemp("installed-shots-references"))


def test_wheel_ships_core_cli_modules_and_no_shots_or_references_pack(installed):
    assert "astrid/core/cli/domain_shots.py" in installed.wheel_names
    assert "astrid/core/cli/domain_references.py" in installed.wheel_names
    assert not any(name.startswith(("astrid/packs/shots/", "astrid/packs/references/"))
                   for name in installed.wheel_names)


def test_installed_modules_resolve_from_the_wheel_only(installed):
    probe = installed.run("-c", """
import importlib.util, json, sys
from pathlib import Path
import astrid.core.cli.domain_shots as shots, astrid.core.cli.domain_references as references
print(json.dumps({
    "shots": shots.__file__, "references": references.__file__,
    "packs": [name for name in ("astrid.packs.shots", "astrid.packs.references")
              if importlib.util.find_spec(name) is not None],
    "isolated": bool(sys.flags.isolated),
}))
""")
    assert probe.returncode == 0, probe.stderr[-3000:]
    payload = json.loads(probe.stdout.strip().splitlines()[-1])
    assert payload["isolated"] is True
    for key in ("shots", "references"):
        assert payload[key].startswith(str(installed.site)), payload
    assert payload["packs"] == []


@pytest.mark.parametrize(("argv", "verbs"), [
    (("timelines", "shots", "--help"), ("list", "show", "create", "add", "remove", "reorder")),
    (("media", "references", "--help"), ("create", "update", "archive", "associate", "link", "list", "show")),
])
def test_installed_nested_command_prints_help(installed, argv, verbs):
    result = installed.run("-m", "astrid", *argv)
    assert result.returncode == 0, result.stdout[-2000:] + result.stderr[-2000:]
    assert result.stdout.startswith(f"usage: astrid {argv[0]} {argv[1]}")
    for verb in verbs:
        assert verb in result.stdout


@pytest.mark.parametrize("family", ["shots", "references"])
def test_installed_has_no_top_level_family(installed, family):
    result = installed.run("-m", "astrid", family, "--help")
    assert result.returncode != 0
    assert f"unknown command {family!r}" in result.stdout + result.stderr
