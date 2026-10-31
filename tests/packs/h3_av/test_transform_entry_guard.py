"""Exercise the real module entry without Runtime, engines, or H3 execution."""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

from astrid.core.env_vars import ASTRID_INTERNAL_INVOCATION, ASTRID_TASK_RUN_ID


MODULE = "astrid.packs.h3_av.actions.transform.run"
REPO_ROOT = Path(__file__).resolve().parents[3]
TASK_ID = "413b2f81-2205-4150-8cc4-c1f734d20dbb"


def _module_entry(tmp_path: Path, *, internal: bool, task_id: str | None):
    working_directory = tmp_path / "module-cwd"
    working_directory.mkdir()
    environment = dict(os.environ)
    environment.pop(ASTRID_INTERNAL_INVOCATION, None)
    environment.pop(ASTRID_TASK_RUN_ID, None)
    if internal:
        environment[ASTRID_INTERNAL_INVOCATION] = "1"
    if task_id is not None:
        environment[ASTRID_TASK_RUN_ID] = task_id
    environment["PYTHONDONTWRITEBYTECODE"] = "1"
    environment["PYTHONPATH"] = os.pathsep.join(
        filter(None, (str(REPO_ROOT), environment.get("PYTHONPATH")))
    )
    result = subprocess.run(
        [sys.executable, "-m", MODULE, "--help"],
        cwd=working_directory,
        env=environment,
        capture_output=True,
        text=True,
        check=False,
        timeout=30,
    )
    assert list(working_directory.iterdir()) == []
    return result


@pytest.mark.parametrize("task_id", [None, TASK_ID])
def test_direct_module_retains_canonical_refusal(tmp_path, task_id):
    result = _module_entry(tmp_path, internal=False, task_id=task_id)

    assert result.returncode == 2
    assert "not meant to be invoked directly" in result.stderr
    assert "sdk.invoke('h3_av.transform'" in result.stderr
    assert "usage:" not in result.stdout


@pytest.mark.parametrize("task_id", [None, "", " \t"])
def test_internal_module_refuses_without_tracked_parent_before_parsing(tmp_path, task_id):
    result = _module_entry(tmp_path, internal=True, task_id=task_id)

    assert result.returncode == 2
    assert "Runtime-admitted parent task" in result.stderr
    assert ASTRID_TASK_RUN_ID in result.stderr
    assert "not meant to be invoked directly" not in result.stderr
    assert "usage:" not in result.stdout


def test_internal_module_with_task_marker_can_only_reach_help(tmp_path):
    # Help stops in argparse. The marker permits entry; it proves no Runtime
    # admission, managed association, child execution, or settlement.
    result = _module_entry(tmp_path, internal=True, task_id=TASK_ID)

    assert result.returncode == 0
    assert "Run a bounded H3 audiovisual transform" in result.stdout
    assert "--request" in result.stdout
    assert result.stderr == ""
