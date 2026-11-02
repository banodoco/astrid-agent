"""R0 systemd Restart=always handoff stays ahead of the Runtime exit fence."""

from __future__ import annotations

import sys
import importlib
from pathlib import Path

import pytest

from astrid.installer import core
from astrid.installer.backend import Backend
from astrid.installer.service import render
from tests.test_installer_busy_fence import Boundary, publish, realm_fixture


def _version(backend: Backend, name: str, release: int) -> core.Version:
    folder = backend.root / "versions" / name
    folder.mkdir(parents=True, exist_ok=True)
    profile = folder / "profile.json"
    core.write_json(profile, {"profile": "astrid"})
    version = core.Version(folder, Path(sys.executable), profile, {}, release)
    core.write_json(folder / "version.json", version.document())
    return version


def _r0_unit(backend: Backend, version: core.Version, data):
    return render("linux", version, data, backend.log, env_file=backend.service.env_file).replace(
        "Restart=on-failure", "Restart=always",
    ).encode("utf-8")


def _prepared_systemd_r0(tmp_path, monkeypatch, *, unit_file_state="enabled"):
    backend, old, data, paths, realm, calls = realm_fixture(tmp_path, monkeypatch, blocker=None)
    service = backend.service
    service.system = "linux"
    service.systemd = lambda: True
    original = _r0_unit(backend, old, data)
    service.path.write_bytes(original)
    transaction = {
        "format": "astrid-install-update/v1", "operation_id": "a" * 32,
        "transaction_kind": "migration", "step": "staged", "data_root": str(data),
        "old": old.document(), "candidate": old.document(),
        "realm": {"realm_id": "selected", "realm_root": str(realm)}, "backup": None,
    }
    core.write_json(backend.root / "update-transaction.json", transaction)
    backend._update_transaction = (transaction, False)
    native = {"running": True, "unit_file_state": unit_file_state}
    events = []

    def run(argv):
        argv = [str(value) for value in argv]
        if argv == ["systemctl", "--user", "show-environment"]:
            return "PATH=/usr/bin"
        if argv[:4] == ["systemctl", "--user", "show", "astrid.service"]:
            property_name = argv[-1]
            if property_name == "--property=UnitFileState":
                return f"UnitFileState={native['unit_file_state']}"
            assert property_name == "--property=MainPID,FragmentPath,LoadState"
            return (f"MainPID={123 if native['running'] else 0}\n"
                    f"FragmentPath={service.path}\nLoadState=loaded")
        if argv[:3] == ["systemctl", "--user", "disable"]:
            events.append("systemctl-disable")
            native["unit_file_state"] = "disabled"
            return ""
        if argv == ["systemctl", "--user", "stop", "astrid.service"]:
            assert native["running"], "systemd stop must precede the Runtime C1 exit path"
            events.append("systemctl-stop")
            native["running"] = False
            return ""
        if argv == ["systemctl", "--user", "daemon-reload"]:
            events.append("daemon-reload")
            return ""
        if argv[:3] == ["systemctl", "--user", "enable"]:
            events.append("systemctl-enable")
            native["unit_file_state"] = unit_file_state
            return ""
        raise AssertionError(f"unexpected systemd command: {argv!r}")

    service.run = run
    publish(paths, realm)
    boundary = Boundary(paths, calls)
    boundary.is_pid_alive = lambda _pid: native["running"]
    boundary.process_birth_identity = lambda _pid: "birth" if native["running"] else None
    boundary.stop_owner = lambda **_kwargs: pytest.fail(
        "C1 must observe the owner already stopped by the authenticated systemd unit")
    monkeypatch.setattr("astrid_runtime.runtime_boundary.LocalRuntimeBoundary", lambda: boundary)
    backend.publish_launch_admission(old, data)
    return backend, old, data, paths, realm, transaction, original, native, events, calls


def test_r0_systemd_disable_and_stop_precede_runtime_c1(tmp_path, monkeypatch):
    (backend, old, data, paths, _realm, transaction, original, native, events,
     _calls) = _prepared_systemd_r0(tmp_path, monkeypatch)
    assert backend.service.path.read_bytes() == original
    assert transaction["systemd_r0_unit"]["sha256"]

    bootstrap = importlib.import_module("astrid_runtime.bootstrap")

    actual_fence = bootstrap._fenced_stop_and_manager

    def observe_c1(*args, **kwargs):
        assert events == ["systemctl-disable", "systemctl-stop"]
        return actual_fence(*args, **kwargs)

    monkeypatch.setattr(bootstrap, "_fenced_stop_and_manager", observe_c1)
    backend.stop(old, data)

    assert events == ["systemctl-disable", "systemctl-stop"]
    assert native["running"] is False
    assert native["unit_file_state"] == "disabled"
    assert transaction["systemd_r0_unit"]["stop_intent"] is True
    assert not paths.discovery_path.exists()
    assert not paths.instance_lock_path.exists()


@pytest.mark.parametrize("prior_state,restore_event", [
    ("enabled", "systemctl-enable"),
    ("disabled", "systemctl-disable"),
])
def test_r0_systemd_recovery_restores_exact_definition_and_prior_enablement(
        tmp_path, monkeypatch, prior_state, restore_event):
    (backend, old, data, _paths, _realm, transaction, original, native, events,
     _calls) = _prepared_systemd_r0(tmp_path, monkeypatch, unit_file_state=prior_state)
    candidate = _version(backend, "candidate", old.release + 1)
    candidate_unit = render(
        "linux", candidate, data, backend.log, env_file=backend.service.env_file,
    ).encode("utf-8")
    backend.service.path.write_bytes(candidate_unit)
    native["running"] = False
    native["unit_file_state"] = "disabled"
    transaction["candidate"] = candidate.document()
    transaction["step"] = "recovering"
    core.write_json(backend.root / "update-transaction.json", transaction)
    backend._update_transaction = (transaction, True)
    events.clear()

    Backend.register(backend, old, data)

    assert backend.service.path.read_bytes() == original
    assert native["unit_file_state"] == prior_state
    assert events == ["daemon-reload", restore_event]


def test_r0_systemd_changed_definition_refuses_before_disable_or_stop(tmp_path, monkeypatch):
    (backend, old, data, paths, _realm, _transaction, _original, native, events,
     _calls) = _prepared_systemd_r0(tmp_path, monkeypatch)
    backend.service.path.write_bytes(
        backend.service.path.read_bytes().replace(b"Restart=always", b"Restart=no", 1))

    with pytest.raises(core.RuntimeOperationError, match="no longer matches the retained R0 definition"):
        backend.stop(old, data)

    assert native["running"] is True
    assert events == []
    assert paths.discovery_path.exists()
    assert paths.instance_lock_path.exists()


def test_recovery_candidate_publication_preserves_retained_r0_unit(tmp_path, monkeypatch):
    backend, old, data, _paths, _realm, _transaction, original, _native, _events, _calls = (
        _prepared_systemd_r0(tmp_path, monkeypatch))
    candidate = _version(backend, "candidate", old.release + 1)

    backend.publish_launch_admission(candidate, data)

    assert backend.service.path.read_bytes() == original
