"""PK-5: the built wheel boots in a clean interpreter with zero pack sources.

"Zero pack sources": no repository checkout on any path, no ``ASTRID_PACKS_PATH``, an empty
home (no personal or managed packs). Everything Astrid loads must come from the wheel.
"""
from __future__ import annotations

import json

import pytest

from tests.integrations._installed_wheel import ROOT, build_installed_wheel

CORE_PACKS = {"vibecomfy", "generation", "rendering", "timeline"}


@pytest.fixture(scope="module")
def installed(tmp_path_factory):
    return build_installed_wheel(tmp_path_factory.mktemp("installed-boot-conformance"))


def test_wheel_and_site_carry_no_repository_sources(installed):
    assert not any(name.startswith(("tests/", "scripts/", ".git")) for name in installed.wheel_names)
    assert not any((installed.site / name).exists() for name in (".git", "pyproject.toml", "scripts", "tests"))
    assert "ASTRID_PACKS_PATH" not in installed.env
    assert not any((installed.root / "home").iterdir())


def test_installed_boot_prints_help_from_the_wheel(installed):
    result = installed.run("-m", "astrid", "--help")
    assert result.returncode == 0, result.stdout[-2000:] + result.stderr[-2000:]
    assert result.stdout.startswith("usage: astrid")
    probe = installed.run("-c", "import astrid, sys, json; print(json.dumps([astrid.__file__, sys.path]))")
    assert probe.returncode == 0, probe.stderr[-2000:]
    origin, path = json.loads(probe.stdout)
    assert origin.startswith(str(installed.site))
    assert not any(entry == str(ROOT) or entry.startswith(str(ROOT) + "/") for entry in path)


def test_installed_pack_inventory_comes_only_from_the_wheel(installed):
    result = installed.run("-m", "astrid.core.pack.cli", "list", "--json")
    assert result.returncode == 0, result.stdout[-2000:] + result.stderr[-2000:]
    packs = json.loads(result.stdout)["packs"]
    roots = {pack["id"]: pack["root"] for pack in packs}
    assert CORE_PACKS <= set(roots)
    assert all(root.startswith(str(installed.site / "astrid")) for root in roots.values()), roots
    assert str(ROOT) + "/" not in result.stdout  # (the env folder is a sibling: <checkout>.env)
