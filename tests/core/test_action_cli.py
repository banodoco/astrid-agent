"""``astrid action run``: the one command that runs the clip through the public action.

The argument tests need no Runtime.  The ``integration`` test starts a real ephemeral Runtime and pack
host and invokes the real ``h3_av.transform`` action.  Inputs are the clip request and asset map (override
with ``ASTRID_CLIP_REQUEST`` / ``ASTRID_CLIP_ASSET_MAP``).

Not covered here: a completed run (exit 0, receipt placement ``local``).  The action first requires an
installed local ``vibecomfy-h3`` engine (``astrid engines install vibecomfy-h3``: git sources, locks, model
files), which the fake ComfyUI in ``tests/helpers/h3_cpu_runtime.py`` cannot stand in for.
"""
from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from astrid import console
from astrid.core import action_cli

CLIP_REQUEST = Path(os.environ.get("ASTRID_CLIP_REQUEST", "/home/user/proof/job/request.json"))
CLIP_ASSET_MAP = Path(os.environ.get("ASTRID_CLIP_ASSET_MAP", "/home/user/proof/job/asset-map.json"))


def _argv(out: Path, *, request: Path = CLIP_REQUEST, asset_map: Path = CLIP_ASSET_MAP,
          placement: str = "local", action: str = "h3_av.transform") -> list[str]:
    return ["action", "run", action, "--request", str(request), "--asset-map", str(asset_map),
            "--placement", placement, "--out", str(out)]


def _processes_mentioning(root: Path) -> list[str]:
    rows = []
    for entry in Path("/proc").iterdir():
        if entry.name.isdigit():
            try:
                command = (entry / "cmdline").read_bytes().replace(b"\0", b" ").decode(errors="replace")
            except OSError:
                continue
            if str(root) in command and str(os.getpid()) != entry.name:
                rows.append(command)
    return rows


# --- arguments (no Runtime) -------------------------------------------------------------------------------

@pytest.mark.parametrize("change", [{"placement": "runpod"}, {"action": "h3_av.prepare"}])
def test_unsupported_placement_or_action_is_a_usage_error(tmp_path, capsys, change):
    with pytest.raises(SystemExit) as exit_info:
        action_cli.main(_argv(tmp_path / "out", **change)[1:])
    assert exit_info.value.code == 2
    assert "astrid action:" in capsys.readouterr().err
    assert not (tmp_path / "out").exists()


def test_missing_input_file_fails_before_any_runtime_starts(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(action_cli, "ephemeral_runtime",
                        lambda *_a, **_k: pytest.fail("a Runtime was started for a missing input"))
    code = action_cli.main(_argv(tmp_path / "out", request=tmp_path / "absent.json")[1:])
    assert code == 1
    assert json.loads(capsys.readouterr().out)["ok"] is False


def test_installed_console_routes_action_without_opening_the_agent(monkeypatch):
    seen = []
    monkeypatch.setattr(action_cli, "main", lambda argv: seen.append(argv) or 7)
    assert console.main(["action", "run", "x"]) == 7
    assert seen == [["run", "x"]]


# --- real ephemeral Runtime -------------------------------------------------------------------------------

@pytest.fixture
def isolated_home(tmp_path, monkeypatch):
    """Own HOME and TMPDIR: no ambient engine install, and every ephemeral workspace lands under tmp_path."""
    home, temp = tmp_path / "action-home", tmp_path / "action-tmp"
    home.mkdir()
    temp.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("TMPDIR", str(temp))
    monkeypatch.setattr("tempfile.tempdir", None)
    return home, temp


@pytest.mark.integration
def test_run_reaches_the_real_action_and_leaves_no_runtime_behind(isolated_home, tmp_path, capfd):
    """Without an installed local engine the action itself refuses; the verb must report it and clean up."""
    _home, temp = isolated_home
    out = tmp_path / "out"
    code = action_cli.main(_argv(out)[1:])
    captured = capfd.readouterr()
    summary = json.loads(captured.out)
    assert code == 1 and summary["ok"] is False and summary["placement"] == "local"
    assert "local H3 engine is not ready (engine_not_installed)" in captured.err  # raised by h3_av.transform itself
    assert out.is_dir()
    assert [p for p in temp.iterdir() if p.name.startswith("astrid-action-")] == []  # workspace removed
    assert _processes_mentioning(temp) == []  # pack host and Runtime stopped
