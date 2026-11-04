"""Receipt-free old Stop admission uses actual Runtime durable idle checks."""

import os
import sqlite3

import pytest

from astrid.installer.app_update import PinokioLifecycle
from astrid.installer.core import RuntimeOperationError, write_json
from astrid.installer.custody import operation
from tests.test_installer_lifecycle import _installer_runtime


def _stopped(tmp_path, monkeypatch):
    backend, data, paths, realm, _boundary, _calls = _installer_runtime(tmp_path, monkeypatch)
    backend.managed_by = "pinokio"
    backend.root.mkdir(parents=True, exist_ok=True)
    lifecycle = object.__new__(PinokioLifecycle)
    lifecycle.root, lifecycle.backend = backend.root, backend
    # This helper uses production Runtime storage and preflight; invoking newer
    # down would be a bug for this absent-publication, old-Stop state.
    backend.stop = lambda *_args: pytest.fail("receipt-free Stop called newer Runtime down")
    assert not paths.discovery_path.exists() and not paths.instance_lock_path.exists()
    return backend, data, paths, realm, lifecycle


def test_receipt_free_old_stop_needs_custody_and_never_calls_newer_down(tmp_path, monkeypatch):
    backend, data, paths, _realm, lifecycle = _stopped(tmp_path, monkeypatch)
    assert backend.pinokio_stopped(data) is False
    with operation(backend.root, backend) as custody:
        assert custody is not None
        assert backend.pinokio_stopped(data) is True
        lifecycle.stop(None, data)
        custody.check()
    assert not paths.discovery_path.exists() and not paths.instance_lock_path.exists()


def test_receipt_free_old_stop_still_refuses_durable_busy_work(tmp_path, monkeypatch):
    backend, data, _paths, realm, lifecycle = _stopped(tmp_path, monkeypatch)
    with sqlite3.connect(realm / "realm.sqlite3") as connection:
        connection.execute("INSERT INTO runs(id,capability,status,created_at,updated_at) "
                           "VALUES ('run','render','running','t','t')")
        connection.execute("INSERT INTO tasks(id,run_id,capability,status,created_at,updated_at) "
                           "VALUES ('task','run','render','running','t','t')")
    with operation(backend.root, backend):
        with pytest.raises(RuntimeOperationError, match="Astrid is busy"):
            lifecycle.stop(None, data)


@pytest.mark.parametrize("publication", ["discovery_path", "instance_lock_path"])
def test_remaining_publication_keeps_c1_authority(tmp_path, monkeypatch, publication):
    backend, data, paths, _realm, _lifecycle = _stopped(tmp_path, monkeypatch)
    path = getattr(paths, publication)
    write_json(path, {"unknown": True})
    before = path.read_bytes()
    with operation(backend.root, backend):
        assert backend.pinokio_stopped(data) is False
    assert path.read_bytes() == before


def test_receipt_free_old_stop_does_not_admit_live_controller(tmp_path, monkeypatch):
    backend, data, _paths, _realm, lifecycle = _stopped(tmp_path, monkeypatch)
    write_json(backend.root / "pinokio-control.json", {
        "schema": 1, "pid": os.getpid(), "port": 12345, "token": "a" * 64,
    })
    with operation(backend.root, backend):
        with pytest.raises(ValueError, match="has not finished stopping"):
            lifecycle.stop(None, data)
