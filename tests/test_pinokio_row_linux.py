"""The Linux row must refuse before touching a host or launcher."""

import os
from pathlib import Path
import subprocess

from scripts.pinokio_row_linux import run


def test_preflight_refuses_with_driver_stage_contract():
    config = {
        "row": "pinokio-linux", "agent_root": "/agent", "pinokio_root": "/pinokio",
        "install_root": "/install", "manifest": "/manifest",
        "standin_manifest": "/standin", "task_command": ["task"],
    }
    stages = run(config)
    assert len(stages) == 1
    assert set(stages[0]) == {"name", "status", "error"}
    assert stages[0]["name"] == "install"
    assert stages[0]["status"] == "fail"
    assert "R1.1 Linux Pinokio limitation" in stages[0]["error"]
    assert "pinokio/session.mjs:290" in stages[0]["error"]
    assert "SIGKILL" in stages[0]["error"]
    assert "before install or session start" in stages[0]["error"]
    assert config["task_command"] == ["task"]


def test_preflight_never_inspects_config_or_touches_host(monkeypatch):
    class UntouchableConfig:
        def __getattribute__(self, name):
            raise AssertionError("preflight inspected config")

    def forbidden(*args, **kwargs):
        raise AssertionError("preflight touched host state")

    with monkeypatch.context() as patch:
        patch.setattr(subprocess, "Popen", forbidden)
        patch.setattr(os, "kill", forbidden)
        patch.setattr(os, "killpg", forbidden)
        patch.setattr(Path, "open", forbidden)
        first = run(UntouchableConfig())
        second = run(UntouchableConfig())
    assert first == second
    assert first is not second and first[0] is not second[0]
