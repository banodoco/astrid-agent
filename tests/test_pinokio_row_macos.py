"""The macOS row must refuse before touching the candidate or user state."""

import os
import subprocess

from scripts import pinokio_row_macos


def test_preflight_refuses_before_access_or_mutation(tmp_path, monkeypatch):
    marker = tmp_path / "user-data"
    marker.write_text("preserve", encoding="utf-8")

    class InaccessibleConfig:
        def __getattribute__(self, name):
            raise AssertionError("preflight must not inspect configuration")

    def forbidden(*args, **kwargs):
        raise AssertionError("preflight must not create or signal a process")

    monkeypatch.setattr(subprocess, "Popen", forbidden)
    monkeypatch.setattr(os, "kill", forbidden)
    monkeypatch.setattr(os, "killpg", forbidden)
    stages = pinokio_row_macos.run(InaccessibleConfig())

    assert len(stages) == 1
    assert set(stages[0]) == {"name", "status", "error"}
    assert stages[0]["name"] == "install"
    assert stages[0]["status"] == "fail"
    assert "R1.1 macOS Pinokio no-force limitation" in stages[0]["error"]
    assert "pinokio/session.mjs:255" in stages[0]["error"]
    assert "pinokio/session.mjs:290" in stages[0]["error"]
    assert "SIGKILL" in stages[0]["error"]
    assert marker.read_text(encoding="utf-8") == "preserve"
    assert list(tmp_path.iterdir()) == [marker]
