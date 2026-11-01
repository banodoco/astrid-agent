"""Exercise the checked-in CLI surface through the public entrypoints."""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
BUNDLE = json.loads((ROOT / "schemas/public-boundary/bundle.json").read_text(encoding="utf-8"))
SURFACE_PATH = ROOT / BUNDLE["files"]["cliSurface"]
_DATA_ENV = (
    "BANODOCO_LOCAL_DATA_ROOT",
    "BANODOCO_LOCAL_SOURCE_MANIFEST",
    "ASTRID_LOCAL_DATA_ROOT",
    "ASTRID_RUNTIME_DATA_ROOT",
    "ASTRID_AGENT_LOCAL_DATA_ROOT",
    "ASTRID_AGENT_LOCAL_SOURCE_MANIFEST",
    "ASTRID_RUNTIME_LOCAL_DATA_ROOT",
    "ASTRID_RUNTIME_LOCAL_SOURCE_MANIFEST",
)


def _nodes(node: dict, path: tuple[str, ...] = ()):
    yield path, node
    for name, child in sorted(node.get("verbs", {}).items()):
        yield from _nodes(child, (*path, name))


def _isolated_env(tmp_path: Path) -> dict[str, str]:
    env = dict(os.environ)
    for name in _DATA_ENV:
        env.pop(name, None)
    home = tmp_path / "home"
    data = tmp_path / "data"
    home.mkdir(exist_ok=True)
    data.mkdir(exist_ok=True)
    env.update(
        HOME=str(home),
        XDG_CONFIG_HOME=str(home / ".config"),
        XDG_CACHE_HOME=str(home / ".cache"),
        ASTRID_RUNTIME_DATA_ROOT=str(data / "runtime"),
        ASTRID_LOCAL_DATA_ROOT=str(data / "local"),
    )
    return env


def test_gateway_help_matches_every_exported_family_and_verb(monkeypatch, capsys, tmp_path):
    surface = json.loads(SURFACE_PATH.read_text(encoding="utf-8"))
    from astrid.core.gateway import main

    # Help must stay a pure parser path even in a cold, isolated environment.
    from astrid.sdk import autobootstrap

    for name in _DATA_ENV:
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("ASTRID_RUNTIME_DATA_ROOT", str(tmp_path / "runtime"))
    monkeypatch.setenv("ASTRID_LOCAL_DATA_ROOT", str(tmp_path / "local"))
    monkeypatch.setattr(
        autobootstrap,
        "ensure_runtime",
        lambda *args, **kwargs: pytest.fail("help unexpectedly started the Runtime launcher"),
    )

    checked = 0
    for family, family_node in sorted(surface["families"].items()):
        for path, node in _nodes(family_node, (family,)):
            capsys.readouterr()
            assert main([*path, "--help"]) == 0, f"help failed for {' '.join(path)}"
            help_text = capsys.readouterr().out
            assert "usage:" in help_text.lower(), f"no usage line for {' '.join(path)}"
            help_flags = set(re.findall(r"(?<!\S)(-{1,2}[A-Za-z0-9][A-Za-z0-9-]*)", help_text))
            for flag in node.get("flags", {}):
                assert flag in help_flags, f"{flag} missing from {' '.join(path)} help"
            checked += 1

    assert checked > len(surface["families"])


@pytest.mark.parametrize(
    "args",
    [
        ["--help"],
        ["projects", "--help"],
        ["documents", "create", "--help"],
        ["timelines", "shots", "--help"],
    ],
)
def test_python_module_help_uses_the_same_public_cli(tmp_path, args):
    result = subprocess.run(
        [sys.executable, "-m", "astrid", *args],
        cwd=ROOT,
        env=_isolated_env(tmp_path),
        text=True,
        capture_output=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    assert "usage:" in result.stdout.lower()

