"""The pinned launcher's unsafe start must not cause any row mutation."""

import os
import subprocess

from scripts import pinokio_row_macos


def test_refuses_before_install_or_process_creation(tmp_path, monkeypatch):
    marker = tmp_path / "existing-data"
    marker.write_text("preserve", encoding="utf-8")

    class UnavailableConfig:
        def __getattribute__(self, name):
            raise AssertionError("preflight must not inspect or mutate config")

    def forbidden(*args, **kwargs):
        raise AssertionError("preflight must not launch or signal a process")

    monkeypatch.setattr(subprocess, "Popen", forbidden)
    monkeypatch.setattr(os, "kill", forbidden)
    monkeypatch.setattr(os, "killpg", forbidden)
    result = pinokio_row_macos.run(UnavailableConfig())

    assert len(result) == 1
    assert set(result[0]) == {"name", "status", "error"}
    assert result[0]["name"] == "install"
    assert result[0]["status"] == "fail"
    assert "Install not attempted" in result[0]["error"]
    assert "session.mjs:370" in result[0]["error"]
    assert ":255" in result[0]["error"]
    assert "SIGKILL at :290" in result[0]["error"]
    assert marker.read_text(encoding="utf-8") == "preserve"
    assert list(tmp_path.iterdir()) == [marker]
