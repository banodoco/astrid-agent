"""The frozen Windows launcher cannot safely run under the row contract."""

import os
import subprocess

from scripts import pinokio_row_windows


def test_preflight_fails_before_reading_inputs_or_running_commands(monkeypatch):
    class UntouchableConfig:
        def __getitem__(self, key):
            raise AssertionError(f"config accessed: {key}")

    def forbidden(*args, **kwargs):
        raise AssertionError("process operation attempted")

    monkeypatch.setattr(subprocess, "Popen", forbidden)
    monkeypatch.setattr(os, "system", forbidden)
    monkeypatch.setattr(os, "kill", forbidden)
    records = pinokio_row_windows.run(UntouchableConfig())
    assert len(records) == 1
    assert records[0]["name"] == "install"
    assert records[0]["status"] == "fail"
    assert "session.mjs:273-278" in records[0]["error"]
    assert "taskkill.exe /T /F" in records[0]["error"]
    assert "lifecycle.mjs:24-26" in records[0]["error"]


def test_preflight_preserves_existing_data_and_returns_fresh_records(tmp_path):
    marker = tmp_path / "existing-data"
    marker.write_bytes(b"preserve me")
    root = tmp_path / "install"
    config = {"install_root": str(root), "pinokio_root": str(marker)}
    first = pinokio_row_windows.run(config)
    first[0]["status"] = "pass"
    assert pinokio_row_windows.run(config)[0]["status"] == "fail"
    assert marker.read_bytes() == b"preserve me"
    assert not root.exists()
    assert list(tmp_path.iterdir()) == [marker]
