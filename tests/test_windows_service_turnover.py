"""Windows service turnover observations are diagnostic only."""
from __future__ import annotations

import importlib
from unittest.mock import Mock

import pytest

from astrid.installer.backend import Backend
from astrid.installer.core import RuntimeOperationError
from tests.test_installer_busy_fence import realm_fixture


def test_windows_turnover_observer_fires_at_stop_owner_and_start_boundaries(
        tmp_path, monkeypatch):
    backend, old, data, _paths, _realm, calls = realm_fixture(
        tmp_path, monkeypatch, system="win32", blocker=None)
    observed = []
    backend._service_turnover_observer = observed.append

    def down(_paths, _boundary, *, stop_manager, **_kwargs):
        stop_manager()
        return {"status": "stopped"}

    monkeypatch.setattr(importlib.import_module("astrid_runtime.bootstrap"), "down", down)
    assert backend.stop(old, data) == {"status": "stopped"}
    backend.service.start = Mock(return_value=True)
    Backend.start(backend, old, data)

    assert observed == ["before-stop", "owner-absent", "after-start-request"]
    assert calls == ["disable", "stop"]
    backend.service.start.assert_called_once_with()


def test_windows_turnover_observer_failure_cannot_change_stop_or_refusal(
        tmp_path, monkeypatch):
    backend, old, data, _paths, _realm, calls = realm_fixture(
        tmp_path, monkeypatch, system="win32", blocker=None)
    backend._service_turnover_observer = Mock(side_effect=RuntimeError("observer failed"))

    def down(_paths, _boundary, *, stop_manager, **_kwargs):
        stop_manager()
        return {"status": "stopped"}

    monkeypatch.setattr(importlib.import_module("astrid_runtime.bootstrap"), "down", down)
    assert backend.stop(old, data) == {"status": "stopped"}
    assert calls == ["disable", "stop"]

    # A normal C1 refusal remains the same refusal with the failing observer.
    refusal = RuntimeError("C1 refused")

    def refuse(*_args, **_kwargs):
        raise refusal

    monkeypatch.setattr(importlib.import_module("astrid_runtime.bootstrap"), "down", refuse)
    with pytest.raises(RuntimeOperationError) as raised:
        backend.stop(old, data)
    assert str(raised.value) == str(refusal)
    assert raised.value.__cause__ is refusal
    assert calls == ["disable", "stop"]
