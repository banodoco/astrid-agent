"""Stopping the login owner tolerates the two races a macOS update hits."""

import subprocess
from unittest.mock import Mock

import pytest

from astrid.installer.backend import Backend
from astrid.installer.service import Service

BOOTSTRAP_EIO = "Bootstrap failed: 5: Input/output error\nTry re-running the command as root for richer errors."


def _discovery(data):
    path = data / "runtime" / "discovery.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text('{"pid": 1, "process_birth_id": "b"}', encoding="utf-8")
    return path


def _failed_down(monkeypatch, backend):
    monkeypatch.setattr(backend, "runtime", Mock(
        side_effect=subprocess.CalledProcessError(1, ["python"], output='{"ok": false}')))


def test_down_accepts_an_owner_that_withdrew_while_draining(tmp_path, monkeypatch):
    """`disable` signals first: the owner can erase discovery before `down` reads it."""
    backend = Backend(tmp_path / "program", tmp_path / "log", tmp_path / "uv")
    _failed_down(monkeypatch, backend)
    _discovery(tmp_path / "data")
    monkeypatch.setattr(Backend, "_discovery_owner_state", staticmethod(lambda path: "stale"))

    backend._down(None, tmp_path / "data", drain=0)  # The stop already happened.


def test_down_reports_an_owner_still_live_after_the_drain(tmp_path, monkeypatch):
    backend = Backend(tmp_path / "program", tmp_path / "log", tmp_path / "uv")
    _failed_down(monkeypatch, backend)
    _discovery(tmp_path / "data")
    monkeypatch.setattr(Backend, "_discovery_owner_state", staticmethod(lambda path: "live"))

    with pytest.raises(subprocess.CalledProcessError):
        backend._down(None, tmp_path / "data", drain=0)


def test_down_is_benign_once_no_owner_is_recorded_at_all(tmp_path, monkeypatch):
    """`stop` only drains a recorded owner, so an erased record means it is gone."""
    backend = Backend(tmp_path / "program", tmp_path / "log", tmp_path / "uv")
    _failed_down(monkeypatch, backend)

    backend._down(None, tmp_path / "data", drain=0)


def test_register_retries_launchds_transient_bootstrap_rejection(tmp_path, monkeypatch):
    service = Service(tmp_path / "program", tmp_path / "log", Mock(), home=tmp_path / "home",
                      system="darwin", say=Mock())
    calls = []

    def run(argv, **kwargs):
        calls.append(argv)
        if len(calls) < 3:
            raise subprocess.CalledProcessError(5, argv, output=BOOTSTRAP_EIO)
        return ""

    monkeypatch.setattr(service, "run", run)
    monkeypatch.setattr(service, "settings", lambda: {})

    service.register(Mock(root=tmp_path / "version", env={}), tmp_path / "data")

    assert len(calls) == 3
    assert calls[0][:2] == ["launchctl", "bootstrap"]


def test_register_surfaces_a_bootstrap_failure_it_cannot_retry(tmp_path, monkeypatch):
    service = Service(tmp_path / "program", tmp_path / "log", Mock(), home=tmp_path / "home",
                      system="darwin", say=Mock())
    run = Mock(side_effect=subprocess.CalledProcessError(
        1, ["launchctl"], output="Bootstrap failed: 1: Operation not permitted"))
    monkeypatch.setattr(service, "run", run)
    monkeypatch.setattr(service, "settings", lambda: {})

    with pytest.raises(subprocess.CalledProcessError):
        service.register(Mock(root=tmp_path / "version", env={}), tmp_path / "data")

    assert run.call_count == 1
