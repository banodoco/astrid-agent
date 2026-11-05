"""The candidate's unsafe start must fail before any row mutation."""

import os
from pathlib import Path
import subprocess

from scripts.pinokio_row_linux import run


def test_refusal_preserves_first_failed_step():
    steps = run({"row": "pinokio-linux"})
    assert steps == [{
        "name": "install", "status": "fail", "error": steps[0]["error"]
    }]
    assert "pinokio/session.mjs:370" in steps[0]["error"]
    assert ":252-255" in steps[0]["error"]
    assert ":290" in steps[0]["error"]
    assert "SIGKILL" in steps[0]["error"]


def test_refusal_does_not_read_config_or_mutate(monkeypatch):
    class NoConfig:
        def __getattribute__(self, name):
            raise AssertionError("config accessed")

    def forbidden(*args, **kwargs):
        raise AssertionError("host touched")

    monkeypatch.setattr(subprocess, "Popen", forbidden)
    monkeypatch.setattr(os, "kill", forbidden)
    monkeypatch.setattr(os, "killpg", forbidden)
    monkeypatch.setattr(Path, "open", forbidden)
    assert run(NoConfig())[0]["status"] == "fail"
