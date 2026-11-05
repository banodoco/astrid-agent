"""The unsafe frozen Windows launcher must be refused before any action."""

import os
import subprocess

from scripts import pinokio_row_windows


def test_refuses_before_accessing_config_or_launching_processes(monkeypatch):
    class UntouchableConfig:
        def __getattribute__(self, name):
            raise AssertionError(f"config accessed: {name}")

        def __getitem__(self, key):
            raise AssertionError(f"config accessed: {key}")

    def forbidden(*args, **kwargs):
        raise AssertionError("process operation attempted")

    monkeypatch.setattr(subprocess, "Popen", forbidden)
    monkeypatch.setattr(os, "system", forbidden)
    monkeypatch.setattr(os, "kill", forbidden)
    steps = pinokio_row_windows.run(UntouchableConfig())
    assert steps == [{
        "name": "install",
        "status": "fail",
        "error": (
            "R1.1 Windows Pinokio row refused before install: pinned candidate "
            "pinokio/session.mjs:277-278 can invoke taskkill.exe /T /F for "
            "an open child; the row forbids force termination."
        ),
    }]


def test_refusal_does_not_touch_paths_and_returns_fresh_results(tmp_path):
    marker = tmp_path / "existing-data"
    marker.write_bytes(b"preserve me")
    missing = tmp_path / "install"
    config = {"install_root": str(missing), "pinokio_root": str(marker)}
    first = pinokio_row_windows.run(config)
    first[0]["status"] = "pass"
    assert pinokio_row_windows.run(config)[0]["status"] == "fail"
    assert marker.read_bytes() == b"preserve me"
    assert not missing.exists()
    assert sorted(p.name for p in tmp_path.iterdir()) == ["existing-data"]
