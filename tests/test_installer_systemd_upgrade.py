"""Portable contract tests; doubles here are not native systemd qualification."""
from contextlib import contextmanager
from dataclasses import replace
from pathlib import Path
import json
import os
import sqlite3
import sys

import pytest

from astrid.installer.backend import Backend
from astrid.installer.core import Installer, RuntimeOperationError, Version, current, recover_transaction, switch, update_context, write_json
from astrid.installer.service import LABEL, Service, command, render
from astrid.installer.systemd_stop import (
    MANAGER, SERVICE, UNIT, SystemdInvocationStop, SystemdStopError, _BusError,
    STOP_RELATIONSHIPS, STOP_ACTIONS, prepare_systemd_stop,
)
from tests.test_installer_busy_fence import Boundary, publish, realm_fixture, snapshot


class Bus:
    """Explicit service-manager protocol double, with no process authority."""
    def __init__(self, properties, identity):
        self.properties = properties
        self.identity = identity
        self.calls = []
        self.on_stop = None
        self.job_present = False

    def call(self, path, interface, method, *args, **_kwargs):
        self.calls.append((path, interface, method, args))
        if method == "GetUnit":
            return MANAGER + "/unit/astrid_2eservice"
        if method == "GetUnitByInvocationID":
            label = args[0].hex()
            if label[0].isdigit():
                label = f"_{ord(label[0]):02x}" + label[1:]
            return MANAGER + "/unit/" + label
        raise AssertionError((path, interface, method, args))

    def close(self):
        self.closed = True

    def property(self, path, interface, name):
        if "/job/" in path:
            if self.job_present:
                return "running"
            raise _BusError("org.freedesktop.systemd1.NoSuchJob", "job settled")
        return self.properties[name]


def cold_bus(service, version, data):
    argv = command(version, data, windows=False, log=service.log)
    return Bus({
        "Id": "astrid.service", "FragmentPath": str(service.path), "DropInPaths": [],
        "LoadState": "loaded", "InvocationID": [], "UnitFileState": "enabled",
        "MainPID": 0, "ExecStart": [[argv[0], argv, False]], "ExecStop": [], "ExecStopPost": [],
        "KillMode": "mixed", "KillSignal": 15, "FinalKillSignal": 9, "SendSIGKILL": True,
        "TimeoutStopFailureMode": "terminate", "TimeoutStopUSec": 90_000_000,
        "ActiveState": "inactive", "Job": [0, "/"], "Result": "success", "NeedDaemonReload": False,
        "StopWhenUnneeded": False, "RefuseManualStop": False,
        **{key: [] for key in STOP_RELATIONSHIPS}, **{key: "none" for key in STOP_ACTIONS},
    }, {})


@pytest.fixture
def systemd(tmp_path, monkeypatch):
    root, data = tmp_path / "program", tmp_path / "data"
    oldroot = root / "versions" / "old"
    oldroot.mkdir(parents=True)
    version = Version(oldroot, oldroot / "python", oldroot / "profile.json", {}, release=15)
    write_json(oldroot / "version.json", version.document())
    switch(root, version)
    service = Service(root, tmp_path / "log", lambda *_: pytest.fail("label command"),
                      home=tmp_path, system="linux")
    service.path = tmp_path / "astrid.service"
    service.selection = tmp_path / "selection.json"
    write_json(service.selection, {"owner": LABEL, "program_root": str(root), "data_root": str(data)})
    service.path.write_text(render("linux", version, data, service.log))
    owner = {"pid": 321, "process_birth_id": "fixture-birth", "runtime_instance_id": "old",
             "realm_id": "realm", "realm_root": str(data / "realm"),
             "endpoint": "http://127.0.0.1:12345", "owner_lock": str(data / "instance.lock")}
    argv = command(version, data, windows=False, log=service.log)
    properties = {
        "Id": "astrid.service", "FragmentPath": str(service.path), "DropInPaths": [],
        "LoadState": "loaded", "InvocationID": [17] * 16, "UnitFileState": "enabled",
        "MainPID": owner["pid"], "ExecStart": [[argv[0], argv, False, 0, 0, 0, 0, 0, 0, 0]],
        "ExecStop": [], "ExecStopPost": [], "KillMode": "mixed", "KillSignal": 15,
        "FinalKillSignal": 9, "SendSIGKILL": True, "TimeoutStopFailureMode": "terminate",
        "TimeoutStopUSec": 90_000_000, "ActiveState": "active", "Job": [0, "/"], "Result": "success",
        "NeedDaemonReload": False, "StopWhenUnneeded": False, "RefuseManualStop": False,
        **{key: [] for key in STOP_RELATIONSHIPS}, **{key: "none" for key in STOP_ACTIONS},
    }
    identity = {"pid": owner["pid"], "birth_id": owner["process_birth_id"], "uid": os.getuid()}
    monkeypatch.setattr("astrid.installer.systemd_stop.platform.process_identity",
                        lambda _pid: dict(identity) if identity else None)
    return service, version, data, owner, Bus(properties, identity)


@pytest.mark.parametrize("first_byte", [0x11, 0xA1])
@pytest.mark.parametrize("enabled", ["enabled", "disabled"])
def test_exact_invocation_observation_is_not_atomic_stop_authority(systemd, first_byte, enabled, monkeypatch):
    service, version, data, owner, bus = systemd
    bus.properties["InvocationID"][0] = first_byte
    bus.properties["UnitFileState"] = enabled
    before = service.path.read_bytes(), service.selection.read_bytes()
    stop = SystemdInvocationStop(service, version, data, owner, bus)
    with pytest.raises(SystemdStopError, match="cannot atomically fence"):
        stop.stop(owner)
    monkeypatch.setattr("astrid.installer.systemd_stop._ManagerBus", lambda: bus)
    with pytest.raises(SystemdStopError, match="cannot atomically fence"):
        with prepare_systemd_stop(service, version, data, owner):
            pytest.fail("live authority was yielded")
    assert bus.closed
    assert not any(call[2] in {"Stop", "Kill", "StopUnit"} for call in bus.calls)
    assert "unit/_31" in stop.path if first_byte == 0x11 else "unit/a1" in stop.path
    assert (service.path.read_bytes(), service.selection.read_bytes()) == before
    assert bus.properties["UnitFileState"] == enabled


@pytest.mark.parametrize("key,value", [
    ("MainPID", 999), ("InvocationID", [18] * 16), ("FragmentPath", "/foreign/unit"),
    ("DropInPaths", ["/foreign/drop-in"]), ("ExecStop", [["foreign"]]),
    ("ExecStopPost", [["foreign"]]), ("KillSignal", 9), ("KillMode", "control-group"),
    ("FinalKillSignal", 15), ("SendSIGKILL", False), ("TimeoutStopUSec", 999_000_000),
    ("TimeoutStopFailureMode", "kill"), ("UnitFileState", "masked"),
    ("Job", [12, MANAGER + "/job/12"]), ("ActiveState", "activating"),
    ("NeedDaemonReload", True), ("OnSuccess", ["foreign.service"]),
    ("PropagatesStopTo", ["foreign.service"]), ("SuccessAction", "exit"),
])
def test_changed_loaded_unit_or_owner_refuses_without_action(systemd, key, value):
    service, version, data, owner, bus = systemd
    stop = SystemdInvocationStop(service, version, data, owner, bus)
    before = service.path.read_bytes(), service.selection.read_bytes()
    bus.properties[key] = value
    with pytest.raises(SystemdStopError):
        stop.validate(owner)
    assert not any(call[2] == "Stop" for call in bus.calls)
    assert (service.path.read_bytes(), service.selection.read_bytes()) == before


def test_loaded_foreign_command_is_refused_even_with_matching_unit_filename(systemd):
    service, version, data, owner, bus = systemd
    bus.properties["ExecStart"][0][1] = [str(version.python), "-c", "print('foreign')"]
    with pytest.raises(SystemdStopError, match="loaded systemd action"):
        SystemdInvocationStop(service, version, data, owner, bus)
    assert not any(call[2] == "Stop" for call in bus.calls)


def test_changed_process_birth_has_no_action(systemd):
    service, version, data, owner, bus = systemd
    stop = SystemdInvocationStop(service, version, data, owner, bus)
    bus.identity["birth_id"] = "reused"
    with pytest.raises(SystemdStopError, match="incarnation"):
        stop.validate(owner)
    assert not any(call[2] == "Stop" for call in bus.calls)


def test_invocation_disappearing_during_observation_never_falls_back_to_label(systemd):
    service, version, data, owner, bus = systemd
    stop = SystemdInvocationStop(service, version, data, owner, bus)
    observed = bus.property
    def replaced(path, interface, name):
        if path == stop.path:
            raise _BusError("org.freedesktop.DBus.Error.UnknownObject", "old invocation gone")
        return observed(path, interface, name)
    bus.property = replaced
    with pytest.raises(_BusError, match="old invocation gone"):
        stop.validate(owner)
    assert not any(call[2] == "Stop" for call in bus.calls)
    assert bus.identity["pid"] == owner["pid"]


@pytest.mark.parametrize("state", ["inactive", "failed"])
def test_cold_quiescent_failed_unit_is_consistent_at_admission_and_exit_check(systemd, state):
    service, version, data, owner, bus = systemd
    bus.properties.update(MainPID=0, ActiveState=state, Result="timeout")
    bus.identity.clear()
    stop = SystemdInvocationStop(service, version, data, None, bus)
    stop.validate(None)
    stop.assert_stopped()
    assert not any(call[2] == "Stop" for call in bus.calls)


def test_null_invocation_is_only_accepted_for_verified_quiescence(systemd):
    service, version, data, owner, bus = systemd
    bus.properties.update(InvocationID=[], MainPID=0, ActiveState="inactive")
    bus.identity.clear()
    stop = SystemdInvocationStop(service, version, data, None, bus)
    stop.validate(None)
    stop.assert_stopped()
    assert not any(call[2] in {"Stop", "GetUnitByInvocationID"} for call in bus.calls)
    with pytest.raises(SystemdStopError):
        SystemdInvocationStop(service, version, data, owner, bus)


def test_candidate_loaded_without_recovery_context_is_refused(systemd):
    service, version, data, owner, bus = systemd
    candidate = replace(version, root=service.root / "versions" / "candidate",
                        python=service.root / "versions" / "candidate" / "python", release=16)
    write_json(candidate.root / "version.json", candidate.document())
    argv = command(candidate, data, windows=False, log=service.log)
    bus.properties.update(MainPID=0, ActiveState="inactive", InvocationID=[],
                          ExecStart=[[argv[0], argv, False]])
    bus.identity.clear()
    before = service.path.read_bytes(), service.selection.read_bytes()
    with pytest.raises(SystemdStopError, match="loaded systemd action"):
        SystemdInvocationStop(service, version, data, None, bus)
    assert not any(call[2] in {"Stop", "Kill", "StopUnit"} for call in bus.calls)
    assert (service.path.read_bytes(), service.selection.read_bytes()) == before


@pytest.fixture
def upgrade(tmp_path, monkeypatch):
    backend, old, data, paths, realm, calls = realm_fixture(tmp_path, monkeypatch, blocker=None)
    old = replace(old, release=15)
    write_json(old.root / "version.json", old.document())
    publish(paths, realm)
    # A real lived-in R15 start already owns this private serialization inode.
    paths.bootstrap_lock_path.touch(mode=0o600)
    boundary = Boundary(paths, calls)
    boundary.live = True
    boundary.is_pid_alive = lambda _pid: boundary.live
    boundary.process_birth_identity = lambda _pid: "birth" if boundary.live else None
    monkeypatch.setattr("astrid_runtime.runtime_boundary.LocalRuntimeBoundary", lambda: boundary)

    class Supervisor:
        def __init__(self, owner):
            self.owner = owner
            self.failure = None
            self.enabled_state = True
        def validate(self, owner):
            assert owner == self.owner
            if self.failure:
                raise self.failure
        def stop(self, owner):
            self.validate(owner)
            assert json.loads((backend.root / "update-transaction.json").read_text())["step"] == "stopping"
            with sqlite3.connect(realm / "realm.sqlite3", timeout=0) as db:
                with pytest.raises(sqlite3.OperationalError, match="locked"):
                    db.execute("BEGIN IMMEDIATE")
            calls.append("supervised-stop")
            boundary.live = False
        def assert_stopped(self):
            assert not boundary.live

    supervisors = []
    @contextmanager
    def prepare(_version, _data, owner, **_kwargs):
        supervisor = Supervisor(owner)
        supervisors.append(supervisor)
        yield supervisor
    monkeypatch.setattr(backend.service, "prepare_supervised_upgrade_stop", prepare)
    monkeypatch.setattr("astrid.installer.core.install_runtime", lambda _root: calls.append("launcher"))
    monkeypatch.setattr("astrid.installer.core.install_launcher", lambda _root: calls.append("command"))
    monkeypatch.setattr(backend, "publish_launch_admission", lambda *_: calls.append("publish"))
    # This fixture exercises lifecycle ordering on the minimal real C1 schema;
    # history-guard migration has its own canonical-workspace tests.
    monkeypatch.setattr(backend, "remove_history_guards", lambda *_: None)
    candidate = replace(old, root=backend.root / "versions" / "candidate", release=16)
    write_json(candidate.root / "version.json", candidate.document())
    installer = Installer(backend.root, data, backend, say=lambda *_: None)

    def run(prepare=None, activate=None):
        def stage():
            calls.append("stage")
            return candidate
        def activate_candidate(*_):
            assert not boundary.live
            calls.append("activate")
        return installer._run_update_transaction(
            old, {"realm_id": "selected", "realm_root": str(realm)}, prepare=prepare or stage,
            stop=backend.stop, switch_candidate=lambda value: switch(backend.root, value),
            switch_old=lambda previous, _candidate: switch(backend.root, previous),
            activate=activate or activate_candidate, activate_old=lambda *_: calls.append("rollback"), kind="app")
    return backend, old, candidate, data, paths, realm, boundary, calls, supervisors, run


def watched(backend, data):
    return snapshot([*data.rglob("*"), *backend.root.rglob("*"), backend.service.path, backend.service.selection])


@pytest.fixture
def cold_candidate(upgrade, monkeypatch):
    backend, old, candidate, data, paths, realm, boundary, calls, _supervisors, _run = upgrade
    old = replace(old, python=old.root / "py" / "bin" / "python")
    candidate = replace(candidate, python=candidate.root / "py" / "bin" / "python",
                        profile=candidate.root / "profile.json")
    for version in (old, candidate):
        version.python.parent.mkdir(parents=True)
        version.python.symlink_to(sys.executable)
        write_json(version.profile, {"profile": "astrid", "fixture": "portable C1"})
        write_json(version.root / "version.json", version.document())
    switch(backend.root, old)
    boundary.live = False
    service = backend.service
    service.path.write_text(render("linux", candidate, data, service.log))
    bus = cold_bus(service, candidate, data)
    monkeypatch.setattr("astrid.installer.systemd_stop._ManagerBus", lambda: bus)
    monkeypatch.setattr(service, "prepare_supervised_upgrade_stop", Service.prepare_supervised_upgrade_stop.__get__(service))
    transaction = {"format": "astrid-install-update/v1", "operation_id": "b" * 32,
                   "transaction_kind": "migration", "step": "rollback_selected", "data_root": str(data),
                   "old": old.document(), "candidate": candidate.document(), "backup": None,
                   "realm": {"realm_id": "selected", "realm_root": str(realm)},
                   "legacy_systemd_enabled": False}
    write_json(backend.root / "update-transaction.json", transaction)
    return backend, old, candidate, data, paths, realm, boundary, calls, bus, transaction


@pytest.mark.parametrize("step", ["recovering", "rollback_selected"])
@pytest.mark.parametrize("state", ["inactive", "failed"])
@pytest.mark.parametrize("enabled", [True, False])
def test_cold_candidate_loaded_rollback_recovers_after_real_c1(cold_candidate, step, state, enabled):
    backend, old, candidate, data, paths, realm, boundary, calls, bus, transaction = cold_candidate
    transaction.update(step=step, legacy_systemd_enabled=not enabled)
    write_json(backend.root / "update-transaction.json", transaction)
    bus.properties.update(ActiveState=state, UnitFileState="enabled" if enabled else "disabled")
    unit_before = backend.service.path.read_bytes()
    def activate(version, _data, _realm):
        assert version == old and current(backend.root) == old
        assert not paths.discovery_path.exists() and not paths.instance_lock_path.exists()
        journal = json.loads((backend.root / "update-transaction.json").read_text())
        assert journal["step"] == "rollback_selected" and journal["legacy_systemd_enabled"] is enabled
        assert backend.service.path.read_bytes() == unit_before
        calls.append("cold-activate-old")
    recover_transaction(backend.root, data, backend, stop=backend.stop,
                        switch_old=lambda *_: None, activate_old=activate, say=lambda *_: None)
    assert json.loads((backend.root / "update-transaction.json").read_text())["step"] == "rolled_back"
    assert current(backend.root) == old and "cold-activate-old" in calls
    assert not any(name in calls for name in ("runtime-stop", "stop", "disable", "stop_foreground"))
    assert not any(call[2] in {"Stop", "Kill", "StopUnit"} for call in bus.calls)
    assert (data / "workspace-sentinel").read_bytes() == b"user work"


@pytest.mark.parametrize("change", ["busy", "live", "reused", "candidate", "profile", "other-version", "job", "unit"])
def test_cold_candidate_loaded_refusal_has_no_recovery_writes(cold_candidate, change):
    backend, old, candidate, data, paths, realm, boundary, calls, bus, transaction = cold_candidate
    if change == "busy":
        with sqlite3.connect(realm / "realm.sqlite3") as db:
            db.execute("INSERT INTO tasks VALUES ('busy','render','running','now')")
    elif change == "live":
        boundary.live = True
    elif change == "reused":
        boundary.process_birth_identity = lambda _pid: "new-owner"
    elif change == "candidate":
        write_json(candidate.root / "version.json", {**candidate.document(), "release": 17})
    elif change == "profile":
        candidate.profile.unlink()
        candidate.profile.symlink_to(old.profile)
    elif change == "other-version":
        other = replace(candidate, root=backend.root / "versions" / "other")
        write_json(other.root / "version.json", other.document())
        argv = command(other, data, log=backend.service.log)
        bus.properties["ExecStart"] = [[argv[0], argv, False]]
    elif change == "job":
        bus.properties["Job"] = [1, MANAGER + "/job/1"]
    else:
        bus.properties["NeedDaemonReload"] = True
    before = watched(backend, data)
    with pytest.raises((RuntimeOperationError, ValueError)):
        recover_transaction(backend.root, data, backend, stop=backend.stop,
                            switch_old=lambda *_: pytest.fail("selection write"),
                            activate_old=lambda *_: pytest.fail("activation"), say=lambda *_: None)
    assert watched(backend, data) == before and calls == []
    assert not any(call[2] in {"Stop", "Kill", "StopUnit"} for call in bus.calls)


@pytest.mark.parametrize("change", ["journal", "candidate", "loaded"])
def test_cold_candidate_binding_is_rechecked_at_final_fence(cold_candidate, monkeypatch, change):
    from astrid.installer.backend import _LegacyStopAdmission
    backend, old, candidate, data, _paths, _realm, _boundary, calls, bus, transaction = cold_candidate
    original = _LegacyStopAdmission.validate
    seen = 0
    captured = None
    def validate(self, inspection):
        nonlocal seen, captured
        seen += 1
        if seen == 4:
            with sqlite3.connect(_realm / "realm.sqlite3", timeout=0) as db:
                with pytest.raises(sqlite3.OperationalError, match="locked"):
                    db.execute("BEGIN IMMEDIATE")
            if change == "journal":
                write_json(backend.root / "update-transaction.json", {**transaction, "operation_id": "c" * 32})
            elif change == "candidate":
                candidate.profile.write_text('{"changed":true}')
            else:
                argv = command(old, data, log=backend.service.log)
                bus.properties["ExecStart"] = [[argv[0], argv, False]]
            captured = watched(backend, data)
        original(self, inspection)
    monkeypatch.setattr(_LegacyStopAdmission, "validate", validate)
    with update_context(backend, transaction, recovering=True), pytest.raises(RuntimeOperationError):
        backend.stop(old, data)
    assert captured is not None and watched(backend, data) == captured and calls == []


def test_real_installer_recovery_persists_fresh_enablement(cold_candidate, monkeypatch):
    backend, old, _candidate, data, _paths, _realm, _boundary, _calls, bus, _transaction = cold_candidate
    installer = Installer(backend.root, data, backend, say=lambda *_: None)
    monkeypatch.setattr(installer, "_activate", lambda *_: None)
    installer._recover_update()
    journal = json.loads((backend.root / "update-transaction.json").read_text())
    assert journal["step"] == "rolled_back" and journal["legacy_systemd_enabled"] is True
    assert bus.properties["UnitFileState"] == "enabled" and current(backend.root) == old


@pytest.mark.parametrize("enabled", [False, True])
def test_cold_recovery_re_registers_old_with_actual_enablement(cold_candidate, monkeypatch, enabled):
    backend, old, _candidate, data, paths, _realm, _boundary, _calls, bus, _transaction = cold_candidate
    bus.properties["UnitFileState"] = "enabled" if enabled else "disabled"
    backend.service.choices = {"autostart": not enabled}
    backend.service.env_file = backend.root / "service-env.json"
    commands = []
    def manager(argv):
        assert not paths.discovery_path.exists() and not paths.instance_lock_path.exists()
        assert read_json(backend.root / "update-transaction.json")["step"] == "rollback_selected"
        commands.append(argv)
        if argv[2] == "show":
            return bus.properties["UnitFileState"] + "\n"
        assert argv[2] == "daemon-reload"
        return ""
    from astrid.installer.core import read_json
    monkeypatch.setattr(backend.service, "run", manager)
    monkeypatch.setattr(backend, "_prior_chat_settings", lambda *_: (None, {}))
    monkeypatch.setattr(backend, "persist_identity", lambda *_: None)
    installer = Installer(backend.root, data, backend, say=lambda *_: None)
    monkeypatch.setattr(installer, "_activate", lambda version, _realm: Backend.register(backend, version, data))
    installer._recover_update()
    assert commands and any(argv[2] == "daemon-reload" for argv in commands)
    assert not any(argv[2] in {"enable", "disable", "stop", "kill"} for argv in commands)
    assert str(old.python) in backend.service.path.read_text()
    journal = read_json(backend.root / "update-transaction.json")
    assert journal["step"] == "rolled_back" and journal["legacy_systemd_enabled"] is enabled


def test_shared_transaction_commits_only_after_supervised_exit(upgrade):
    backend, old, candidate, data, paths, realm, boundary, calls, supervisors, run = upgrade
    run()
    assert current(backend.root) == candidate
    assert calls == ["stage", "prepare", "supervised-stop", "launcher", "publish", "activate", "command"]
    assert not boundary.live
    assert not paths.discovery_path.exists()
    assert json.loads((backend.root / "update-transaction.json").read_text())["step"] == "complete"
    assert (data / "workspace-sentinel").read_bytes() == b"user work"


def test_detached_live_defers_without_staging_or_new_journal_and_cold_retry_continues(upgrade):
    backend, old, candidate, data, paths, realm, boundary, calls, supervisors, run = upgrade
    backend.service.path.unlink()
    record = backend.service.recorded()
    write_json(backend.service.selection, {**record, "foreground": {"pid": 123, "birth_id": "birth"}})
    before = watched(backend, data)
    with pytest.raises(RuntimeOperationError, match="Update deferred") as caught:
        run()
    assert caught.value.details == {"safe": False, "deferred_until_quiescent": True}
    assert watched(backend, data) == before
    assert calls == [] and supervisors == []
    assert not (backend.root / "update-transaction.json").exists()
    boundary.live = False  # Normal lifecycle exits independently; no updater signal.
    run()
    assert current(backend.root) == candidate
    assert "runtime-stop" not in calls and "supervised-stop" not in calls
    assert (data / "workspace-sentinel").read_bytes() == b"user work"


@pytest.mark.parametrize("entry", ["update", "install-local", "install-remote"])
def test_public_busy_update_refuses_before_pending_cleanup_or_install_claims(upgrade, monkeypatch, tmp_path, capsys, entry):
    from astrid.installer import cli
    from tests.test_installer_cli_recovery import _valid_cli_manifest
    backend, old, candidate, data, paths, realm, boundary, calls, supervisors, run = upgrade
    backend.service.path.unlink()
    write_json(backend.service.selection, {**backend.service.recorded(),
               "foreground": {"pid": 123, "birth_id": "birth"}})
    with sqlite3.connect(realm / "realm.sqlite3") as db:
        db.execute("INSERT INTO tasks VALUES ('busy','render','running','2000-01-01T00:00:00Z')")
    monkeypatch.setattr(cli, "Backend", lambda *_args, **_kwargs: backend)
    monkeypatch.setattr(cli, "installer_lock_path", lambda *_: tmp_path / "installer.lock")
    monkeypatch.setattr("runtime_protocol.platform.log_dir", lambda: tmp_path / "logs")
    monkeypatch.setattr(backend, "source_checkout_ownership", lambda *_: False)
    monkeypatch.setattr(cli.update_job, "sweep_owned_jobs",
                        lambda *_a, **_k: pytest.fail("pending cleanup preceded update admission"))
    manifest = _valid_cli_manifest(16)
    manifest_path = tmp_path / "requested-manifest.json"
    write_json(manifest_path, manifest)
    def download(_root, destination):
        assert not destination.is_relative_to(backend.root) and not destination.is_relative_to(data)
        write_json(destination, manifest)
    monkeypatch.setattr(cli, "_download_manifest", download)
    before = watched(backend, data)
    argv = ["--root", str(backend.root), "--yes"]
    if entry == "update":
        argv.append("--update")
    if entry != "install-remote":
        argv.extend(["--manifest", str(manifest_path)])
    # Explicit update's existing public outcome code is 3 when it keeps the
    # previous release; the install-line refusal uses the ordinary error code.
    assert cli.main(argv) == (3 if entry == "update" else 1)
    assert "Astrid is busy" in capsys.readouterr().out
    assert watched(backend, data) == before and calls == []


def test_same_release_install_remains_repair_without_legacy_stop_admission(upgrade, tmp_path, monkeypatch):
    from astrid.installer import cli
    from tests.test_installer_cli_recovery import _valid_cli_manifest
    backend, old, candidate, data, paths, realm, boundary, calls, supervisors, run = upgrade
    manifest = _valid_cli_manifest(15)
    path = tmp_path / "manifest.json"
    write_json(path, manifest)
    monkeypatch.setattr(backend, "preflight_update", lambda *_: pytest.fail("repair entered update"))
    assert cli._preflight_update_manifest(backend, "install", old, data, path) == manifest


@pytest.mark.parametrize("damage", ["missing-disabled-unit", "retained-enable-link", "dangling-unit", "mixed-foreground"])
def test_changed_registration_cannot_downgrade_to_detached_quiescence(upgrade, damage):
    backend, old, candidate, data, paths, realm, boundary, calls, supervisors, run = upgrade
    boundary.live = False
    if damage != "mixed-foreground":
        backend.service.path.unlink()
    if damage == "retained-enable-link":
        folder = backend.service.path.parent / "default.target.wants"
        folder.mkdir()
        (folder / backend.service.path.name).symlink_to(backend.service.path)
    if damage == "dangling-unit":
        backend.service.path.symlink_to(backend.service.path.with_name("absent"))
    if damage == "mixed-foreground":
        write_json(backend.service.selection, {**backend.service.recorded(), "foreground": {"pid": 123, "birth_id": "birth"}})
    before = watched(backend, data)
    with pytest.raises(RuntimeOperationError, match="registration"):
        run()
    assert watched(backend, data) == before and calls == [] and supervisors == []


def test_no_manager_and_reused_pid_refuse_before_staging_or_writes(upgrade, monkeypatch):
    backend, old, candidate, data, paths, realm, boundary, calls, supervisors, run = upgrade
    @contextmanager
    def unavailable(*_):
        raise SystemdStopError("authenticated manager unavailable")
        yield
    monkeypatch.setattr(backend.service, "prepare_supervised_upgrade_stop", unavailable)
    before = watched(backend, data)
    with pytest.raises(SystemdStopError, match="manager unavailable"):
        run()
    assert watched(backend, data) == before and calls == []
    boundary.process_birth_identity = lambda _pid: "reused-birth"
    with pytest.raises(Exception, match="birth changed"):
        run()
    assert watched(backend, data) == before and calls == []


def test_final_supervisor_refusal_has_no_journal_launcher_or_selection_write(upgrade, monkeypatch):
    backend, old, candidate, data, paths, realm, boundary, calls, supervisors, run = upgrade
    before = watched(backend, data)
    def stage():
        supervisor = supervisors[0]
        original = supervisor.validate
        def validate(owner):
            original(owner)
            with sqlite3.connect(realm / "realm.sqlite3", timeout=0) as db:
                try:
                    db.execute("BEGIN IMMEDIATE")
                    db.rollback()
                except sqlite3.OperationalError:
                    raise SystemdStopError("unit changed at final fence")
        supervisor.validate = validate
        return candidate
    with pytest.raises(RuntimeOperationError, match="unit changed at final fence"):
        run(prepare=stage)
    assert watched(backend, data) == before
    assert not (backend.root / "update-transaction.json").exists()
    assert calls == ["prepare"]


def test_busy_realm_refuses_before_manager_and_transaction(upgrade):
    backend, old, candidate, data, paths, realm, boundary, calls, supervisors, run = upgrade
    with sqlite3.connect(realm / "realm.sqlite3") as db:
        db.execute("INSERT INTO tasks VALUES ('busy','render','running',NULL)")
    before = watched(backend, data)
    with pytest.raises(Exception, match="busy"):
        run()
    assert watched(backend, data) == before and supervisors == [] and calls == []


def test_missing_existing_lock_refuses_without_creating_it_or_staging(upgrade):
    backend, old, candidate, data, paths, realm, boundary, calls, supervisors, run = upgrade
    paths.bootstrap_lock_path.unlink()
    before = watched(backend, data)
    with pytest.raises(Exception, match="private bootstrap lock"):
        run()
    assert watched(backend, data) == before and supervisors == [] and calls == []


def test_timeout_preserves_stopping_journal_without_retry_or_activation(upgrade):
    backend, old, candidate, data, paths, realm, boundary, calls, supervisors, run = upgrade
    def stage():
        def timeout(_owner):
            calls.append("stop-timeout")
            raise TimeoutError("native stop timeout")
        supervisors[0].stop = timeout
        return candidate
    with pytest.raises(RuntimeOperationError, match="native stop timeout"):
        run(prepare=stage)
    assert current(backend.root) == old
    assert boundary.live
    assert calls == ["prepare", "stop-timeout"]
    journal = json.loads((backend.root / "update-transaction.json").read_text())
    assert journal["step"] == "stopping"
    assert (data / "workspace-sentinel").read_bytes() == b"user work"


def test_quiescent_selection_switch_after_admission_refuses_all_update_writes(upgrade):
    backend, old, candidate, data, paths, realm, boundary, calls, supervisors, run = upgrade
    boundary.live = False
    before_after_change = {}
    def stage():
        # A second quiescent selection must not authorize the transaction's
        # original workspace. It is sufficient to change the catalog identity;
        # no caller is allowed to recreate an owner mapping from None.
        selected = json.loads(paths.catalog_path.read_text())
        selected["realms"][0]["realm_id"] = "replacement"
        selected["selected_realm_id"] = "replacement"
        write_json(paths.catalog_path, selected)
        paths.discovery_path.unlink()
        paths.instance_lock_path.unlink()
        before_after_change.update(watched(backend, data))
        return candidate
    with pytest.raises(RuntimeOperationError, match="workspace changed|realm selection changed"):
        run(prepare=stage)
    assert watched(backend, data) == before_after_change
    assert calls == [] and current(backend.root) == old
    assert not (backend.root / "update-transaction.json").exists()


def test_ordinary_lifecycle_stop_does_not_enter_upgrade_adapter(upgrade, monkeypatch):
    backend, old, candidate, data, paths, realm, boundary, calls, supervisors, run = upgrade
    def not_upgrade(*_, **__):
        pytest.fail("ordinary stop entered legacy update admission")
    monkeypatch.setattr(backend, "legacy_update_stop", not_upgrade)
    backend.preflight_stop(data)
    backend.stop(old, data)
    assert "runtime-stop" in calls and "supervised-stop" not in calls
    assert supervisors == []


@pytest.mark.parametrize("valid", [False, True])
def test_only_already_retained_native_child_uses_existing_pr130_path(upgrade, valid):
    from types import SimpleNamespace
    backend, old, candidate, data, paths, realm, boundary, calls, supervisors, run = upgrade
    observed = []
    class NativeReference:
        def verify(self, expected):
            observed.append(expected)
            if not valid:
                raise RuntimeError("retained native child is no longer valid")
    boundary._process = SimpleNamespace(pid=123, _runtime_native_authority=NativeReference())
    backend._stop_boundary = boundary
    before = watched(backend, data)
    if valid:
        with backend.legacy_update_stop(old, data) as admission:
            assert admission is None  # No replacement stop implementation.
    else:
        with pytest.raises(RuntimeError, match="no longer valid"):
            with backend.legacy_update_stop(old, data):
                pytest.fail("invalid authority admitted")
    assert observed == [{"pid": 123, "birth_id": "birth", "uid": os.getuid()}]
    assert watched(backend, data) == before and calls == [] and supervisors == []


@pytest.mark.parametrize("enabled", [False, True])
def test_full_transaction_preserves_actual_enablement_despite_saved_autostart(upgrade, monkeypatch, enabled):
    backend, old, candidate, data, paths, realm, boundary, calls, supervisors, run = upgrade
    boundary.live = False
    original_prepare = backend.service.prepare_supervised_upgrade_stop
    @contextmanager
    def prepare(*args):
        with original_prepare(*args) as admission:
            admission.enabled_state = enabled
            yield admission
    monkeypatch.setattr(backend.service, "prepare_supervised_upgrade_stop", prepare)
    record = backend.service.recorded()
    write_json(backend.service.selection, {**record, "choices": {"autostart": True}})
    backend.service.env_file = backend.root / "service-env.json"
    commands = []
    def systemctl(argv):
        commands.append(argv)
        if argv[2] == "show":
            return "enabled\n" if enabled else "disabled\n"
        assert argv[2] == "daemon-reload"
        return ""
    monkeypatch.setattr(backend.service, "run", systemctl)
    monkeypatch.setattr(backend, "_prior_chat_settings", lambda *_: (None, {}))
    monkeypatch.setattr(backend, "persist_identity", lambda *_: None)
    run(activate=lambda version, workspace, _realm: Backend.register(backend, version, workspace))
    assert current(backend.root) == candidate
    assert any(argv[2] == "daemon-reload" for argv in commands)
    assert not any(argv[2] in {"enable", "disable", "stop", "kill"} for argv in commands)
    journal = json.loads((backend.root / "update-transaction.json").read_text())
    assert journal["legacy_systemd_enabled"] is enabled and journal["step"] == "complete"


def test_timeout_cold_recovery_rechecks_quiescence_before_launcher_or_rollback(upgrade):
    backend, old, candidate, data, paths, realm, boundary, calls, supervisors, run = upgrade
    def stage():
        def timeout(_owner):
            calls.append("stop-timeout")
            raise TimeoutError("native stop timeout")
        supervisors[0].stop = timeout
        return candidate
    with pytest.raises(RuntimeOperationError, match="native stop timeout"):
        run(prepare=stage)
    boundary.live = False  # Independent lifecycle exit after the failed attempt.
    calls.clear()
    recover_transaction(backend.root, data, backend, stop=backend.stop,
                        switch_old=lambda previous, _candidate: switch(backend.root, previous),
                        activate_old=lambda *_: calls.append("rollback"), say=lambda *_: None)
    assert current(backend.root) == old
    assert calls == ["launcher", "publish", "rollback", "command"]
    assert json.loads((backend.root / "update-transaction.json").read_text())["step"] == "rolled_back"
    assert (data / "workspace-sentinel").read_bytes() == b"user work"


def test_activation_failure_rolls_back_without_resignalling_old_owner(upgrade):
    backend, old, candidate, data, paths, realm, boundary, calls, supervisors, run = upgrade
    def activation(*_):
        raise RuntimeError("candidate activation failed")
    with pytest.raises(RuntimeError, match="candidate activation failed"):
        run(activate=activation)
    assert current(backend.root) == old
    assert calls.count("supervised-stop") == 1
    assert "rollback" in calls
    assert json.loads((backend.root / "update-transaction.json").read_text())["step"] == "rolled_back"


@pytest.mark.parametrize("gap", ["realm", "catalog", "intact"])
@pytest.mark.parametrize("loaded_candidate", [False, True])
def test_r15_interrupted_restore_keeps_existing_absence_fence(tmp_path, monkeypatch, gap, loaded_candidate):
    from tests.test_installer_lifecycle import _installer_runtime
    backend, data, paths, realm, boundary, calls = _installer_runtime(tmp_path, monkeypatch)
    boundary.live = False
    oldroot = backend.root / "versions" / "old"
    old = Version(oldroot, oldroot / "py" / "bin" / "python", oldroot / "profile.json", {}, release=15)
    old.python.parent.mkdir(parents=True)
    old.python.symlink_to(sys.executable)
    write_json(old.profile, {"profile": "astrid", "fixture": "portable restore"})
    write_json(oldroot / "version.json", old.document())
    switch(backend.root, old)
    backup_root = backend.root / "backups" / "retained"
    backup_root.mkdir(parents=True)
    backend._copy(realm, backup_root / "realm")
    backend._copy(data, backup_root / "support", support=True)
    backup = {"root": str(backup_root), "kind": "clone"}
    write_json(backup_root / "backup.json", backup)
    write_json(backend.service.selection, {"owner": LABEL, "program_root": str(backend.root),
               "data_root": str(data), "realm_id": "realm-1", "realm_root": str(realm)})
    backend.service.path.parent.mkdir(parents=True, exist_ok=True)
    backend.service.path.write_text("fixture registered unit")
    monkeypatch.setattr(backend.service, "recorded", lambda: json.loads(backend.service.selection.read_text()))
    monkeypatch.setattr(backend.service, "validate_stop_target", lambda *_a, **_k: None)
    class ColdManager:
        enabled_state = False
        def validate(self, owner):
            assert owner is None and not boundary.live
        def assert_stopped(self):
            assert not boundary.live
    @contextmanager
    def cold_manager(*_):
        yield ColdManager()
    monkeypatch.setattr(backend.service, "prepare_supervised_upgrade_stop", cold_manager)
    def not_legacy(*_a, **_k):
        pytest.fail("RestoreSelection was combined with new legacy admission")
    monkeypatch.setattr(backend, "legacy_update_stop", not_legacy)
    journal = {"format": "astrid-install-update/v1", "operation_id": "a" * 32,
               "transaction_kind": "migration", "step": "recovering", "data_root": str(data),
               "old": old.document(), "candidate": None, "backup": backup,
               "realm": {"realm_id": "realm-1", "realm_root": str(realm)}}
    if loaded_candidate:
        folder = backend.root / "versions" / "candidate"
        candidate = replace(old, root=folder, python=folder / "py" / "bin" / "python",
                            profile=folder / "profile.json", release=16)
        candidate.python.parent.mkdir(parents=True)
        candidate.python.symlink_to(sys.executable)
        write_json(candidate.profile, {"profile": "astrid", "fixture": "portable restore candidate"})
        write_json(folder / "version.json", candidate.document())
        journal["candidate"] = candidate.document()
        backend.service.path.write_text(render("linux", candidate, data, backend.service.log))
        bus = cold_bus(backend.service, candidate, data)
        bus.properties["UnitFileState"] = "disabled"
        monkeypatch.setattr("astrid.installer.systemd_stop._ManagerBus", lambda: bus)
        monkeypatch.setattr(backend.service, "prepare_supervised_upgrade_stop",
                            Service.prepare_supervised_upgrade_stop.__get__(backend.service))
    write_json(backend.root / "update-transaction.json", journal)
    if gap == "realm":
        realm.rename(realm.with_name("parked-realm"))
    if gap == "catalog":
        paths.catalog_path.unlink()
    with update_context(backend, journal, recovering=True):
        result = backend.stop(old, data)
    assert result["status"] == "stopped" and boundary.stop_calls == 0
    assert calls == []  # No mutable-label stop/disable during R15 restore.
    assert journal["legacy_systemd_enabled"] is False
    assert json.loads(paths.catalog_path.read_text())["selected_realm_id"] == "realm-1"
