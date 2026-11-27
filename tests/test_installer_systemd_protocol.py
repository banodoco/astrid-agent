"""Alternative A protocol/C1/C2 tests with explicit portable manager doubles.

These exercise production admission and transaction code. They neither run a
user systemd manager nor qualify a served-origin Release 15 installation.
"""
from dataclasses import replace
from types import SimpleNamespace
import hashlib
import os
import sqlite3

import pytest

from astrid.installer import systemd_stop as sd
from astrid.installer.backend import Backend
from astrid.installer.core import Installer, RuntimeOperationError, current, read_json, switch, write_json
from astrid.installer.service import Service, render
from tests.test_installer_systemd_upgrade import Bus, cold_bus, systemd, upgrade, watched


@pytest.fixture
def clock(systemd, monkeypatch):
    value = [100.0]
    monkeypatch.setattr(sd, "time", SimpleNamespace(monotonic=lambda: value[0]))
    monkeypatch.setattr(sd, "STOP_SECONDS", 3)
    systemd[-1].properties["TimeoutStopUSec"] = 3_000_000
    return value


@pytest.mark.parametrize("change", ["parent", "uid", "type", "pidfile", "notify", "delegate", "control", "updater"])
def test_original_manager_direct_child_and_external_updater_are_required(systemd, monkeypatch, change):
    service, version, data, owner, bus = systemd
    if change == "parent":
        bus.owner_identity["parent_pid"] = 999
    elif change == "uid":
        bus.owner_identity["uid"] += 1
    elif change == "updater":
        previous = sd._process_cgroup
        monkeypatch.setattr(sd, "_process_cgroup", lambda pid:
                            bus.properties["ControlGroup"] + "/child" if pid == os.getpid() else previous(pid))
    else:
        key, value = {"type": ("Type", "forking"), "pidfile": ("PIDFile", "/tmp/pid"),
                      "notify": ("NotifyAccess", "all"), "delegate": ("Delegate", True),
                      "control": ("ControlPID", 999)}[change]
        bus.properties[key] = value
    before = service.path.read_bytes(), service.selection.read_bytes()
    with pytest.raises(sd.SystemdStopError):
        sd.SystemdInvocationStop(service, version, data, owner, bus)
    assert not any(call[2] == "Stop" for call in bus.calls)
    assert (service.path.read_bytes(), service.selection.read_bytes()) == before


@pytest.mark.parametrize("change", ["reload", "replacement", "removal", "job", "manager"])
def test_pre_fence_lifecycle_events_refuse_without_stop(systemd, change):
    service, version, data, owner, bus = systemd
    stop = sd.SystemdInvocationStop(service, version, data, owner, bus)
    if change == "reload":
        bus.emit("Reloading", True)
        bus.emit("Reloading", False)
    elif change in {"replacement", "removal"}:
        bus.emit("UnitNew" if change == "replacement" else "UnitRemoved", "astrid.service", stop.label_path)
    elif change == "job":
        bus.emit("JobNew", 7, sd.MANAGER + "/job/7", "astrid.service")
    else:
        bus.fault = "original manager lost"
    with pytest.raises(sd.SystemdStopError):
        stop.validate(owner)
    assert not any(call[2] == "Stop" for call in bus.calls)


@pytest.mark.parametrize("failure", ["failed", "canceled", "timeout", "missing-receipt", "other-job",
                                    "malformed-job", "lost-reply", "manager", "new-invocation",
                                    "hooks", "enablement", "service-timeout"])
def test_post_admission_uncertainty_never_retries_or_becomes_completion(systemd, clock, failure):
    service, version, data, owner, bus = systemd
    stop = sd.SystemdInvocationStop(service, version, data, owner, bus)
    def act():
        if failure == "lost-reply":
            raise sd._BusError("org.freedesktop.DBus.Error.NoReply", "request may have acted")
        bus.settle(result=failure if failure in {"failed", "canceled", "timeout"} else "done")
        if failure == "missing-receipt":
            bus.events[:] = [event for event in bus.events if event[0] != "JobRemoved"]
        elif failure == "other-job":
            bus.emit("JobRemoved", 77, sd.MANAGER + "/job/77", "astrid.service", "done")
        elif failure == "malformed-job":
            bus.emit("JobRemoved", 77, sd.MANAGER + "/job/88", "astrid.service", "done")
        elif failure == "manager":
            bus.fault = "retained manager disconnected"
        elif failure == "new-invocation":
            bus.properties["InvocationID"] = [33] * 16
        elif failure == "hooks":
            bus.properties["ExecStopPost"] = [["authorized-later-hook"]]
        elif failure == "enablement":
            bus.properties["UnitFileState"] = "disabled"
        elif failure == "service-timeout":
            bus.properties["Result"] = "timeout"
    bus.on_stop = act
    bus.on_poll = lambda wait: clock.__setitem__(0, clock[0] + int(wait))
    with pytest.raises(sd.SystemdStopError):
        stop.stop(owner)
    with pytest.raises(sd.SystemdStopError, match="already spent"):
        stop.stop(owner)
    assert stop.submitted and not stop.completed
    assert [call[2] for call in bus.calls].count("Stop") == 1
    assert not any(call[2] in {"Kill", "StopUnit", "CancelJob", "StartUnit", "Reload"} for call in bus.calls)


@pytest.mark.parametrize("unresolved", ["child", "zombie", "reused", "escaped"])
def test_full_drain_requires_independent_original_owner_and_child_absence(systemd, monkeypatch, clock, unresolved):
    service, version, data, owner, bus = systemd
    child = {"pid": 654, "birth_id": "child-birth", "uid": os.getuid(), "parent_pid": owner["pid"]}
    bus.extra_processes[654] = child
    stop = sd.SystemdInvocationStop(service, version, data, owner, bus)
    group = bus.properties["ControlGroup"]
    def act():
        bus.settle()
        # GetProcesses is now empty, but an independently observed old child
        # remains. A manager reply/empty census alone cannot discharge it.
        if unresolved == "zombie":
            monkeypatch.setattr(sd, "_present", lambda pid, **_kw: {"exited_unreaped": True} if pid == 654 else None)
        else:
            observed = {**child, **({"birth_id": "reused"} if unresolved == "reused" else {})}
            monkeypatch.setattr(sd, "_present", lambda pid, **_kw: observed if pid == 654 else None)
            if unresolved == "escaped":
                monkeypatch.setattr(sd, "_process_cgroup", lambda _pid: group + "-escaped")
    bus.on_stop = act
    bus.on_poll = lambda wait: clock.__setitem__(0, clock[0] + int(wait))
    with pytest.raises(sd.SystemdStopError):
        stop.stop(owner)
    assert not stop.completed and [call[2] for call in bus.calls].count("Stop") == 1


def test_still_draining_child_is_reobserved_without_resetting_deadline(systemd, clock):
    service, version, data, owner, bus = systemd
    child = {"pid": 654, "birth_id": "child-birth", "uid": os.getuid(), "parent_pid": owner["pid"]}
    bus.extra_processes[654] = child
    stop = sd.SystemdInvocationStop(service, version, data, owner, bus)
    def act():
        bus.settle()
        bus.extra_processes[654] = child
    def progress(wait):
        clock[0] += int(wait)
        if clock[0] >= 102:
            bus.extra_processes.clear()
    bus.on_stop, bus.on_poll = act, progress
    stop.stop(owner)
    stop.assert_stopped()
    assert stop.completed and clock[0] == 102 and stop.stop_deadline == 103
    clock[0] = 103
    with pytest.raises(sd.SystemdStopError, match="deadline"):
        stop.assert_stopped()  # C1 completion shares the original budget.


@pytest.mark.parametrize("mode", ["reloaded", "gc", "reload-removal", "missing-without-removal", "replaced"])
def test_reload_reconstruction_is_distinct_from_correlated_unit_gc(systemd, clock, mode):
    service, version, data, owner, bus = systemd
    bus.properties["UnitFileState"] = "disabled"
    stop = sd.SystemdInvocationStop(service, version, data, owner, bus)
    def act():
        if mode in {"reloaded", "reload-removal"}:
            bus.emit("Reloading", True)
            bus.emit("UnitRemoved", "astrid.service", stop.label_path)
            bus.emit("UnitNew", "astrid.service", stop.label_path)
            bus.emit("Reloading", False)
        bus.settle()
        if mode != "reloaded":
            bus.unloaded = True
        if mode in {"gc", "replaced"}:
            bus.emit("UnitRemoved", "astrid.service", stop.label_path)
        if mode == "replaced":
            bus.emit("UnitNew", "astrid.service", stop.label_path)
    bus.on_stop = act
    bus.on_poll = lambda wait: clock.__setitem__(0, clock[0] + int(wait))
    if mode in {"reloaded", "gc"}:
        stop.stop(owner)
        stop.assert_stopped()
        assert stop.completed
    else:
        with pytest.raises(sd.SystemdStopError):
            stop.stop(owner)
        assert not stop.completed
    assert [call[2] for call in bus.calls].count("Stop") == 1


def test_admission_has_one_total_budget_for_many_reads(systemd, clock):
    service, version, data, owner, bus = systemd
    original = bus.property
    def slow(*args):
        clock[0] += 0.2
        return original(*args)
    bus.property = slow
    with pytest.raises(sd.SystemdStopError, match="deadline"):
        sd.SystemdInvocationStop(service, version, data, owner, bus)
    assert 105 <= clock[0] < 106
    assert not any(call[2] == "Stop" for call in bus.calls)


@pytest.mark.parametrize("correlated", [True, False])
def test_unit_gc_during_property_read_requires_fresh_explicit_lookup_and_removal(systemd, clock, correlated):
    service, version, data, owner, bus = systemd
    stop = sd.SystemdInvocationStop(service, version, data, owner, bus)
    original = bus.property
    def disappeared(path, interface, name):
        if stop.submitted and bus.properties["MainPID"] == 0 and not bus.unloaded:
            bus.unloaded = True
            if correlated:
                bus.emit("UnitRemoved", "astrid.service", stop.label_path)
            raise sd._BusError("org.freedesktop.DBus.Error.UnknownObject", "unit GC during read")
        return original(path, interface, name)
    bus.property = disappeared
    bus.on_poll = lambda wait: clock.__setitem__(0, clock[0] + int(wait))
    if correlated:
        stop.stop(owner)
        stop.assert_stopped()
        assert stop.completed
    else:
        with pytest.raises(sd.SystemdStopError, match="deadline"):
            stop.stop(owner)
        assert not stop.completed
    assert [call[2] for call in bus.calls].count("Stop") == 1


@pytest.fixture
def managed_transaction(upgrade, monkeypatch):
    backend, old, candidate, data, paths, realm, boundary, calls, _sup, _run = upgrade
    candidate = replace(candidate, python=candidate.root / "python", profile=candidate.root / "profile.json")
    write_json(candidate.root / "version.json", candidate.document())
    write_json(candidate.profile, {"profile": "astrid", "fixture": "portable candidate"})
    service = backend.service
    service.env_file = backend.root / "service-env.json"
    service.path.write_text(render("linux", old, data, service.log))
    bus = cold_bus(service, old, data)
    group = "/user.slice/user-1000.slice/user@1000.service/app.slice/astrid.service"
    bus.properties.update(MainPID=123, ActiveState="active", SubState="running", InvocationID=[17] * 16,
                          ControlGroup=group)
    bus.owner_identity.update(pid=123, birth_id="birth", uid=os.getuid(), parent_pid=100)
    monkeypatch.setattr(sd, "_ManagerBus", lambda: bus)
    monkeypatch.setattr(sd.platform, "process_identity", bus.process)
    monkeypatch.setattr(sd, "_present", bus.process)
    monkeypatch.setattr(sd, "_process_cgroup", lambda pid:
                        group.rsplit("/app.slice/", 1)[0] + "/init.scope" if pid == 100 else
                        group + "-update-worker" if pid == os.getpid() else group)
    monkeypatch.setattr(service, "prepare_supervised_upgrade_stop", Service.prepare_supervised_upgrade_stop.__get__(service))
    monkeypatch.setattr(service, "run", lambda *_: pytest.fail("fresh systemctl bypass"))
    monkeypatch.setattr(backend, "publish_launch_admission", Backend.publish_launch_admission.__get__(backend))
    monkeypatch.setattr(backend, "register", Backend.register.__get__(backend))
    monkeypatch.setattr(backend, "start", Backend.start.__get__(backend))
    monkeypatch.setattr(backend, "_prior_chat_settings", lambda *_: (None, {}))
    monkeypatch.setattr(backend, "finish", lambda *_: calls.append("finish"))
    monkeypatch.setattr(backend, "verify", lambda *_: calls.append("verified"))
    boundary.is_pid_alive = lambda pid: bus.process(pid) is not None
    boundary.process_birth_identity = lambda pid: (bus.process(pid) or {}).get("birth_id")
    boundary.endpoint_metadata = lambda **_: {
        "runtime_instance_id": read_json(paths.discovery_path).get("runtime_instance_id"), "realm_id": "selected"}
    def stop():
        with sqlite3.connect(realm / "realm.sqlite3", timeout=0) as db:
            with pytest.raises(sqlite3.OperationalError, match="locked"):
                db.execute("BEGIN IMMEDIATE")
        assert read_json(paths.instance_lock_path)["state"] == "stopping"
        bus.settle()
        boundary.live = False
        calls.append("manager-stop")
    def start():
        assert not paths.discovery_path.exists() and not paths.instance_lock_path.exists()
        assert read_json(backend.root / "update-transaction.json")["step"] in {"switched", "rollback_selected"}
        pid, birth, instance = 400 + bus.sequence, f"birth-{bus.sequence}", f"new-{bus.sequence}"
        bus.owner_identity.update(pid=pid, birth_id=birth, uid=os.getuid(), parent_pid=100)
        bus.properties.update(MainPID=pid, ControlPID=0, ActiveState="active", SubState="running", Job=[0, "/"],
                              InvocationID=[32 + bus.sequence] * 16, Result="success", ControlGroup=group)
        publication = {"pid": pid, "process_birth_id": birth, "runtime_instance_id": instance,
                       "active_realm": "selected", "realm_id": "selected", "realm_root": str(realm),
                       "endpoint": "http://127.0.0.1:12345"}
        write_json(paths.discovery_path, publication)
        write_json(paths.instance_lock_path, publication)
        bus.emit("JobRemoved", bus.sequence, f"{sd.MANAGER}/job/{bus.sequence}", "astrid.service", "done")
        boundary.live = True
        calls.append("manager-start")
    bus.on_stop, bus.on_start = stop, start
    installer = Installer(backend.root, data, backend, say=lambda *_: None)
    def run(*, activate=None, prepare=None):
        return installer._run_update_transaction(
            old, {"realm_id": "selected", "realm_root": str(realm)}, prepare=prepare or (lambda: candidate),
            stop=backend.stop, switch_candidate=lambda version: switch(backend.root, version),
            switch_old=lambda previous, _candidate: switch(backend.root, previous),
            activate=activate or (lambda version, _data, workspace: installer._activate(version, workspace["realm_id"])),
            activate_old=lambda version, _data, workspace: installer._activate(version, workspace["realm_id"]))
    return backend, old, candidate, data, paths, realm, boundary, calls, bus, installer, run


@pytest.mark.parametrize("enabled", [True, False])
def test_same_manager_survives_full_c2_register_start_and_readiness(managed_transaction, enabled):
    backend, old, candidate, data, paths, realm, boundary, calls, bus, installer, run = managed_transaction
    bus.properties["UnitFileState"] = "enabled" if enabled else "disabled"
    backend.service.choices = {"autostart": not enabled}
    run()
    assert current(backend.root) == candidate and bus.closed
    assert read_json(backend.root / "update-transaction.json")["step"] == "complete"
    assert bus.properties["UnitFileState"] == ("enabled" if enabled else "disabled")
    assert [call[2] for call in bus.calls if call[2] in {"Stop", "Reload", "StartUnit"}] == ["Stop", "Reload", "StartUnit"]
    assert calls.index("manager-stop") < calls.index("manager-start") < calls.index("verified") < calls.index("finish")
    assert not any(item in calls for item in ("runtime-stop", "stop", "disable", "stop_foreground"))
    assert (data / "workspace-sentinel").read_bytes() == b"user work"


def test_disabled_old_unit_gc_loads_only_new_registration_after_completed_c1(managed_transaction):
    backend, old, candidate, data, paths, realm, boundary, calls, bus, installer, run = managed_transaction
    bus.properties["UnitFileState"] = "disabled"
    stop = bus.on_stop
    def stop_and_gc():
        stop()
        bus.emit("UnitRemoved", "astrid.service", sd.MANAGER + "/unit/astrid_2eservice")
        bus.unloaded = True
    bus.on_stop = stop_and_gc
    run()
    assert [call[2] for call in bus.calls if call[2] in {"Stop", "Reload", "LoadUnit", "StartUnit"}] == [
        "Stop", "Reload", "LoadUnit", "StartUnit"]
    assert current(backend.root) == candidate and bus.properties["UnitFileState"] == "disabled"


def test_managed_candidate_route_never_bypasses_session_with_native_child_reference(managed_transaction):
    backend, old, candidate, data, paths, realm, boundary, calls, bus, installer, run = managed_transaction
    class NativeReference:
        def verify(self, _expected):
            pytest.fail("managed update bypassed its retained manager with native child custody")
    boundary._process = SimpleNamespace(pid=123, _runtime_native_authority=NativeReference())
    run()
    assert [call[2] for call in bus.calls].count("Stop") == 1
    assert "runtime-stop" not in calls


def test_candidate_rollback_gets_fresh_invocation_stop_on_same_manager(managed_transaction, monkeypatch):
    backend, old, candidate, data, paths, realm, boundary, calls, bus, installer, run = managed_transaction
    def verify(version, *_):
        calls.append("verified")
        if version == candidate:
            raise RuntimeError("forced candidate readiness failure")
    monkeypatch.setattr(backend, "verify", verify)
    with pytest.raises(RuntimeError, match="forced candidate readiness failure"):
        run()
    stops = [call for call in bus.calls if call[2] == "Stop"]
    assert len(stops) == 2 and stops[0][0] != stops[1][0]
    assert all(call[1:] == (sd.UNIT, "Stop", ("fail",)) for call in stops)
    assert [call[2] for call in bus.calls if call[2] in {"Stop", "Reload", "StartUnit"}] == [
        "Stop", "Reload", "StartUnit", "Stop", "Reload", "StartUnit"]
    assert current(backend.root) == old and read_json(backend.root / "update-transaction.json")["step"] == "rolled_back"
    assert bus.closed and (data / "workspace-sentinel").read_bytes() == b"user work"


@pytest.mark.parametrize("change", ["restarted", "policy"])
def test_rollback_does_not_chase_changed_candidate_invocation_or_policy(managed_transaction, monkeypatch, change):
    backend, old, candidate, data, paths, realm, boundary, calls, bus, installer, run = managed_transaction
    captured = {}
    def verify(version, *_):
        assert version == candidate
        if change == "restarted":
            bus.properties["InvocationID"] = [99] * 16
            bus.owner_identity["birth_id"] = "replacement-child"
            for path in (paths.discovery_path, paths.instance_lock_path):
                write_json(path, {**read_json(path), "runtime_instance_id": "replacement",
                                  "process_birth_id": "replacement-child"})
        else:
            bus.properties["ExecStopPost"] = [["changed-authorized-policy"]]
        captured.update(watched(backend, data))
        raise RuntimeError("candidate readiness failed")
    monkeypatch.setattr(backend, "verify", verify)
    with pytest.raises(RuntimeError):
        run()
    assert captured and watched(backend, data) == captured
    assert current(backend.root) == candidate and read_json(backend.root / "update-transaction.json")["step"] == "switched"
    assert [call[2] for call in bus.calls].count("Stop") == 1
    assert not any(call[2] in {"Kill", "StopUnit"} for call in bus.calls)


@pytest.mark.parametrize("point", ["before-registration", "before-start", "before-rollback"])
def test_manager_loss_at_c2_boundaries_forbids_fresh_connection_or_rollback_writes(managed_transaction, monkeypatch, point):
    backend, old, candidate, data, paths, realm, boundary, calls, bus, installer, run = managed_transaction
    captured = {}
    def lose():
        bus.fault = "original manager disconnected"
        captured.update(watched(backend, data))
    if point == "before-registration":
        def activate(*_):
            lose()
            backend.register(candidate, data)
    elif point == "before-start":
        def activate(*_):
            backend.register(candidate, data)
            lose()
            backend.start(candidate, data)
    else:
        def activate(*_):
            installer._activate(candidate, "selected")
            lose()
            raise RuntimeError("candidate verification failed after manager loss")
    with pytest.raises((RuntimeError, sd.SystemdStopError)):
        run(activate=activate)
    assert captured and watched(backend, data) == captured
    assert current(backend.root) == candidate and read_json(backend.root / "update-transaction.json")["step"] == "switched"
    assert [call[2] for call in bus.calls].count("Stop") == 1
    assert [call[2] for call in bus.calls].count("StartUnit") == int(point == "before-rollback")


@pytest.mark.parametrize("change", ["attempt", "operation", "missing", "busy"])
def test_update_now_final_fence_preserves_accepted_job_and_pending_data(managed_transaction, change):
    backend, old, candidate, data, paths, realm, boundary, calls, bus, installer, run = managed_transaction
    operation = "c" * 32
    backend._update_job_operation_id = operation
    marker = read_json(backend.root / "install.json")
    job = {"operation_id": operation, "state": "active", "attempt": {"epoch": 1, "nonce": "d" * 32}}
    write_json(backend.root / "install.json", {**marker, "update_job": job})
    captured = {}
    def stage():
        if change == "busy":
            with sqlite3.connect(realm / "realm.sqlite3") as db:
                db.execute("INSERT INTO tasks VALUES ('pending','render','running','now')")
        else:
            altered = ({**job, "attempt": {"epoch": 2, "nonce": "e" * 32}} if change == "attempt"
                       else {**job, "operation_id": "f" * 32})
            write_json(backend.root / "install.json", marker if change == "missing" else {**marker, "update_job": altered})
        captured.update(watched(backend, data))
        return candidate
    with pytest.raises(RuntimeOperationError):
        run(prepare=stage)
    assert watched(backend, data) == captured and current(backend.root) == old
    assert not any(call[2] in {"Stop", "StartUnit", "Reload"} for call in bus.calls)
    assert not (backend.root / "update-transaction.json").exists()


@pytest.mark.parametrize("outcome", ["updated", "busy", "uncertain-stop"])
def test_public_update_job_entry_keeps_real_admission_and_settlement_contract(
        managed_transaction, monkeypatch, tmp_path, capsys, outcome):
    from astrid.installer import cli, update_job
    from astrid.installer.service import install_selection, installer_lock_path
    from runtime_protocol import platform
    from tests.test_installer_cli_recovery import _valid_cli_manifest

    backend, old, candidate, data, paths, realm, boundary, calls, bus, installer, run = managed_transaction
    home = tmp_path / "home"
    home.mkdir(exist_ok=True)
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("USERPROFILE", str(home))
    monkeypatch.setenv("XDG_DATA_HOME", str(home / ".local/share"))
    monkeypatch.setenv("LOCALAPPDATA", str(home / "AppData/Local"))
    service = backend.service
    record = service.recorded()
    service.home = home
    service.selection = install_selection(home, service.system)
    write_json(service.selection, record)
    service.activation_lock_path = installer_lock_path(home, service.system)
    selected = tmp_path / "selected-inputs"
    platform.private_directory(selected)
    manifest, bootstrap = selected / "manifest", selected / "bootstrap"
    write_json(manifest, _valid_cli_manifest(16))
    platform.owner_only(manifest)
    bootstrap.write_text("#!/bin/sh\nexit 0\n")
    platform.owner_only(bootstrap)
    operation = "c" * 32
    request = {
        "root": str(backend.root), "data_root": str(data), "selection_path": str(service.selection),
        "old_version": old.document(), "old_instance_id": "instance",
        "old_identity": {"release": 15, "managed_by": None},
        "operation_id": operation, "job_name": f"astrid-update-{operation}",
        "log_path": str(backend.root / "logs" / "update-portable.log"),
        "metadata": {"manifest_path": str(manifest), "bootstrap_path": str(bootstrap), "release": 16,
                     "manifest_sha256": hashlib.sha256(manifest.read_bytes()).hexdigest(),
                     "bootstrap_sha256": hashlib.sha256(bootstrap.read_bytes()).hexdigest()},
    }
    monkeypatch.setattr(update_job, "update_job_availability", lambda: {"available": True})
    monkeypatch.setattr(update_job, "_probe_identity", lambda _data: (
        read_json(paths.discovery_path)["runtime_instance_id"],
        {"release": current(backend.root).release, "managed_by": None}))
    job = update_job.accept(request)  # Actual current P29/P30 acceptance/schema.
    job = update_job._write_job(backend.root, operation, state="active", message="Update is running.")
    saved_attempt, saved_inputs = dict(job["attempt"]), dict(job["metadata"])
    before = watched(backend, data)
    if outcome == "busy":
        with sqlite3.connect(realm / "realm.sqlite3") as db:
            db.execute("INSERT INTO tasks VALUES ('busy-worker','render','running','now')")
        before = watched(backend, data)
    elif outcome == "uncertain-stop":
        def lost_reply():
            raise sd._BusError("org.freedesktop.DBus.Error.NoReply", "stop outcome uncertain")
        bus.on_stop = lost_reply

    entered = [False]
    connections = []
    def connect():
        # Each preliminary read-only admission is a separate connection. Only
        # the actual C2 lifetime gets the bus whose Stop/Start we observe.
        observed = bus if entered[0] else Bus(bus.properties, bus.owner_identity)
        connections.append(observed)
        return observed
    monkeypatch.setattr(sd, "_ManagerBus", connect)
    original_transaction = Installer._run_update_transaction
    def transaction(self, *args, **kwargs):
        entered[0] = True
        return original_transaction(self, *args, **kwargs)
    monkeypatch.setattr(Installer, "_run_update_transaction", transaction)
    monkeypatch.setattr(cli, "Backend", lambda *_a, **_kw: backend)
    monkeypatch.setattr(platform, "log_dir", lambda: tmp_path / "logs")
    monkeypatch.setattr(backend, "source_checkout_ownership", lambda *_: False)
    monkeypatch.setattr(backend, "stage", lambda *_: candidate)
    monkeypatch.setattr(backend, "validate_candidate", lambda *_: None)
    monkeypatch.setattr(backend, "has_workspace", lambda *_: True)
    monkeypatch.setattr("astrid.installer.core.schema_unchanged", lambda *_: True)
    monkeypatch.setattr(backend, "sync_skills", lambda *_: {})
    monkeypatch.setattr(backend, "open", lambda: None)
    original_sweep, original_admit = update_job.sweep_owned_jobs, update_job.admit_worker
    entries = []
    def sweep(*args, **kwargs):
        entries.append("sweep")
        return original_sweep(*args, **kwargs)
    def admit(*args, **kwargs):
        entries.append("admit-worker")
        return original_admit(*args, **kwargs)
    monkeypatch.setattr(update_job, "sweep_owned_jobs", sweep)
    monkeypatch.setattr(update_job, "admit_worker", admit)
    result = cli.main(["--root", str(backend.root), "--data-root", str(data),
                       "--manifest", str(manifest), "--update-job", operation, "--yes", "--no-browser"])
    output = capsys.readouterr().out
    after = read_json(backend.root / "install.json")["update_job"]
    assert after["operation_id"] == operation and after["attempt"] == saved_attempt and after["metadata"] == saved_inputs
    assert all(connection.closed for connection in connections)
    if outcome == "busy":
        assert result == 1 and "Astrid is busy" in output
        assert entries == [] and watched(backend, data) == before and after["state"] == "active"
        assert not entered[0] and not any(call[2] == "Stop" for call in bus.calls)
    elif outcome == "uncertain-stop":
        assert result != 0 and after["state"] == "recovery-needed"
        assert read_json(backend.root / "update-transaction.json")["step"] == "stopping"
        assert current(backend.root) == old and [call[2] for call in bus.calls].count("Stop") == 1
        assert not any(call[2] in {"Reload", "StartUnit"} for call in bus.calls)
    else:
        assert result == 0, output
        assert entries == ["sweep", "admit-worker"] and after["state"] == "updated"
        journal = read_json(backend.root / "update-transaction.json")
        assert journal["step"] == "complete" and journal["update_job"] == {"operation_id": operation}
        assert after["transaction_id"] == journal["operation_id"] and current(backend.root) == candidate
        assert read_json(paths.discovery_path)["runtime_instance_id"] != "instance"
