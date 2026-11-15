"""Every `astrid generate <subcommand>` the generation CLI defines is reachable
through the console entry point the installed product uses (`astrid` ->
`astrid.console:main` -> gateway), run as a subprocess."""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

from astrid.core.generation.cli import subcommand_names

SUBCOMMANDS = subcommand_names()


def _astrid() -> list[str]:
    script = Path(sys.executable).parent / "astrid"
    if script.exists():
        return [str(script)]
    return [sys.executable, "-c", "import sys; from astrid.console import main; sys.exit(main())"]


def _run(args: list[str], tmp_path: Path, timeout: int = 120):
    env = {**os.environ, "ASTRID_RUNTIME_DATA_ROOT": str(tmp_path / "data")}
    return subprocess.run([*_astrid(), "generate", *args], capture_output=True, text=True,
                          timeout=timeout, cwd=tmp_path, env=env)


def test_subcommand_list_covers_known_commands():
    assert {"list", "failure", "describe", "release", "workflow", "targets", "settings",
            "launch", "defaults", "image", "video", "audio", "align"} <= set(SUBCOMMANDS)


@pytest.mark.parametrize("name", SUBCOMMANDS)
def test_every_subcommand_help_through_gateway(name, tmp_path):
    result = _run([name, "--help"], tmp_path)
    assert result.returncode == 0, result.stdout + result.stderr
    assert f"astrid generate {name}" in result.stdout


def test_failure_help_shows_failure_command(tmp_path):
    result = _run(["failure", "--help"], tmp_path)
    assert result.returncode == 0, result.stderr
    assert "failure" in result.stdout and "receipt" in result.stdout.lower()
    assert "invalid choice" not in result.stderr


def test_failure_json_without_receipt(tmp_path):
    result = _run(["failure", "--json"], tmp_path)
    assert "invalid choice" not in result.stderr
    assert "no failure receipt" in result.stderr
    assert result.returncode != 0


def test_list_still_served_by_listing(tmp_path):
    result = _run(["list", "--json"], tmp_path, timeout=300)
    assert result.returncode == 0, result.stderr
    assert result.stdout.lstrip().startswith("[")


def test_unknown_word_is_rejected(tmp_path):
    result = _run(["nonesuch"], tmp_path)
    assert result.returncode != 0
    assert "invalid choice" in result.stderr
