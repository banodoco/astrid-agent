"""Crash/relogin and wrong-process regressions for the independent P1 review.

All manager commands, Runtime launches and OS process observations are modeled.
Filesystem backup/restore, owner flocks, embedded admission and Runtime C1 are real.
"""
from __future__ import annotations

import importlib
import json
import os
import plistlib
import shutil
import sys
import types
from pathlib import Path

import pytest
from runtime_protocol import platform
from astrid_runtime.bootstrap import down as runtime_down

from astrid.installer.backend import Backend
from astrid.installer.core import RuntimeOperationError, Version, rollback_transaction, switch, update_context, write_json
from astrid.installer.service import Service
from tests.test_installer_busy_fence import Boundary, publish
from tests.test_installer_release0_manager_stop import installation as captured_installation

pytestmark = pytest.mark.posix_native


def installation(tmp_path, monkeypatch, system):
    from runtime_protocol.store import RealmStore
    result = captured_installation(tmp_path, monkeypatch, system)
    backend, old, data, paths, realm, lock, fd, calls, state = result
    # Use a real canonical schema for recovery identity verification. No daemon
    # runs: the descriptor only models the manager-owned process's lifetime.
    canonical = tmp_path / "canonical"
    RealmStore.initialize(canonical, realm_id="selected").close()
    shutil.copytree(canonical, realm, dirs_exist_ok=True,
                    ignore=lambda _parent, names: {"owner.lock"}.intersection(names))
    return result


def journal(backend, old, data, realm, *, step="stopping", backup=None):
    value = {"format": "astrid-install-update/v1", "operation_id": "a" * 32,
             "transaction_kind": "migration", "data_root": str(data), "old": old.document(),
             "candidate": None, "realm": {"realm_id": "selected", "realm_root": str(realm)},
             "backup": backup, "step": step}
    write_json(backend.root / "update-transaction.json", value)
    switch(backend.root, old)
    return value


def fresh_controller(backend):
    fresh = Backend(backend.root, backend.log, backend.uv)
    fresh.service = Service(backend.root, backend.log, backend.service.run,
                            home=backend.service.home, system=backend.service.system)
    return fresh


def save(backend, transaction, step, **details):
    transaction.update(step=step, **details)
    write_json(backend.root / "update-transaction.json", transaction)


def launch(manager, old, data, state):
    if manager.system == "darwin":
        return plistlib.loads(manager.path.read_bytes())["ProgramArguments"]
    return state["action"]


@pytest.mark.parametrize("system", ["linux", "darwin"])
@pytest.mark.parametrize("step", ["backed_up", "recovering", "rollback_selected"])
def test_crash_relogin_cannot_edit_before_recovery(tmp_path, monkeypatch, system, step):
    backend, old, data, paths, realm, lock, fd, calls, state = installation(tmp_path, monkeypatch, system)
    try:
        sentinel = realm / "user-document.txt"
        sentinel.write_text("before update")
        transaction = journal(backend, old, data, realm)
        original = backend.service.path.read_bytes()
        with update_context(backend, transaction):
            backend.publish_launch_admission(old, data)
            backend.stop(old, data)
        # Model R0's own shutdown removing its publications. Agent never does so.
        paths.discovery_path.unlink()
        paths.instance_lock_path.unlink()
        saved = backend.root / "backups/saved"
        Backend._copy(backend, realm, saved / "realm", clone=True)
        Backend._copy(backend, data, saved / "support", clone=True, support=True)
        backup = {"kind": "clone", "root": str(saved)}
        save(backend, transaction, step, backup=backup)
        # A fresh controller has no local stop receipt. Execute the persisted
        # action a subsequent login would use, replacing only the Runtime CLI.
        fresh = fresh_controller(backend)
        cli = types.ModuleType("astrid_runtime.cli")
        reached = []
        def cli_main(args):
            reached.append(args)
            sentinel.write_text("accepted after crash")
            return 0
        cli.main = cli_main
        monkeypatch.setitem(sys.modules, "astrid_runtime.cli", cli)
        argv = launch(fresh.service, old, data, state)
        with pytest.raises(ValueError, match="interrupted|Choose Update to finish"):
            exec(argv[-1], {})
        assert not reached and sentinel.read_text() == "before update"
        assert transaction["transaction_context"]["release0_service"]["text"].encode() == original
        # Replay the production rollback and restore against real saved data.
        # The gate stays installed throughout activation and journal writes.
        def activate(*_):
            fresh.service.restore_release0(old, data, transaction["transaction_context"]["release0_service"])
        monkeypatch.setattr(importlib.import_module("astrid_runtime.bootstrap"), "down", runtime_down)
        rollback_transaction(backend.root, data, fresh, transaction, stop=fresh.stop,
                             switch_old=lambda *_: switch(backend.root, old), activate_old=activate,
                             save=lambda step, **details: save(fresh, transaction, step, **details))
        assert sentinel.read_text() == "before update"
        assert transaction["step"] == "rolled_back"
        # Terminal admission permits login again.
        with pytest.raises(SystemExit) as result:
            exec(launch(fresh.service, old, data, state)[-1], {})
        assert result.value.code == 0 and reached
    finally:
        os.close(fd)


@pytest.mark.parametrize("system", ["linux", "darwin"])
def test_rollback_selected_replay_preserves_activation_edits(tmp_path, monkeypatch, system):
    backend, old, data, paths, realm, lock, fd, calls, state = installation(tmp_path, monkeypatch, system)
    try:
        sentinel = realm / "user-document.txt"
        sentinel.write_text("backup")
        transaction = journal(backend, old, data, realm)
        with update_context(backend, transaction):
            backend.publish_launch_admission(old, data)
            backend.stop(old, data)
        saved = backend.root / "backups/saved"
        Backend._copy(backend, realm, saved / "realm", clone=True)
        Backend._copy(backend, data, saved / "support", clone=True, support=True)
        save(backend, transaction, "rollback_selected", backup={"kind": "clone", "root": str(saved)})
        sentinel.write_text("accepted during old-version activation")
        for _ in range(2):
            # Model another crash before the terminal write, with a new Backend.
            fresh = fresh_controller(backend)
            def crash_at_terminal(step, **details):
                if step == "rolled_back":
                    raise KeyboardInterrupt("crash after activation")
                save(fresh, transaction, step, **details)
            with pytest.raises(KeyboardInterrupt):
                rollback_transaction(backend.root, data, fresh, transaction, stop=fresh.stop,
                                     switch_old=lambda *_: None, activate_old=lambda *_: None,
                                     save=crash_at_terminal)
            assert transaction["step"] == "rollback_selected"
            assert sentinel.read_text() == "accepted during old-version activation"
            assert not list(realm.parent.glob(realm.name + ".failed-*"))
    finally:
        os.close(fd)


@pytest.mark.parametrize("defect", ["foreign-loaded", "old-foreign-process", "wrong-realm-argv", "wrong-cgroup",
                                     "wrong-exe", "missing-birth-scope", "stop-hook", "birth-on-reload",
                                     "invocation-on-reload", "pid-on-reload"])
def test_foreign_or_replaced_systemd_invocation_never_stops(tmp_path, monkeypatch, defect):
    backend, old, data, paths, realm, lock, fd, calls, state = installation(tmp_path, monkeypatch, "linux")
    try:
        original = backend.service.run
        if defect == "foreign-loaded":
            state["action"] = ["/usr/bin/sleep", "9999"]
        elif defect == "old-foreign-process":
            state["process_action"] = [str(old.python), "-c", "print('other workload')"]
        elif defect == "wrong-realm-argv":
            state["process_action"] = [*state["process_action"][:-1], state["process_action"][-1].replace(str(data), str(tmp_path / "other"))]
        elif defect == "wrong-cgroup":
            state["cgroup"] = "/other.service"
        elif defect == "wrong-exe":
            other = tmp_path / "foreign-python"
            other.touch()
            (tmp_path / "proc/exe").unlink()
            (tmp_path / "proc/exe").symlink_to(other)
        elif defect == "missing-birth-scope":
            state["birth"] = "proc-start-ticks:101"
        elif defect == "stop-hook":
            state["hooks"]["ExecStop"] = [["/foreign", ["/foreign"], False, 0, 0, 0, 0, 0, 0, 0]]
        def run(argv, **kwargs):
            result = original(argv, **kwargs)
            if "daemon-reload" in argv:
                if defect == "birth-on-reload":
                    state["birth"] = "proc-start-ticks:102;boot:test;pidns:55"
                elif defect == "invocation-on-reload":
                    state["invocation"] = "b" * 32
                elif defect == "pid-on-reload":
                    state["pid"] += 1
            return result
        backend.service.run = run
        with pytest.raises(RuntimeOperationError):
            backend.stop(old, data)
        assert not any("stop" in call or "bootout" in call for call in calls)
        assert state["loaded"]
        if "on-reload" not in defect:
            assert not any("daemon-reload" in call for call in calls)
    finally:
        os.close(fd)


def c1_owner(backend, paths, realm, fd, calls, monkeypatch):
    monkeypatch.setattr(importlib.import_module("astrid_runtime.bootstrap"), "down", runtime_down)
    boundary = Boundary(paths, calls)
    boundary.live = False
    boundary.is_pid_alive = lambda _pid: boundary.live
    boundary.process_birth_identity = lambda _pid: "birth" if boundary.live else None
    boundary.validate_owner = lambda **kw: (boundary.live and kw["process_birth_id"] == "birth"
                                           and kw["expected_realm_id"] == "selected"
                                           and Path(kw["expected_realm_root"]) == realm)
    def stop_owner(**kwargs):
        assert kwargs["process_birth_id"] == "birth" and kwargs["realm_id"] == "selected"
        calls.append("c1-owner-stop")
        platform.unlock_file(fd)
        boundary.live = False
        return {"status": "stopped"}
    boundary.stop_owner = stop_owner
    backend._stop_boundary = boundary
    def start():
        assert not boundary.live
        platform.lock_file(fd, blocking=False)
        publish(paths, realm)
        boundary.live = True
    return boundary, start


@pytest.mark.parametrize("operation", ["backup", "upgrade"])
@pytest.mark.parametrize("system", ["linux", "darwin"])
def test_independent_runtime_owner_cleanup_uses_real_c1(tmp_path, monkeypatch, operation, system):
    backend, old, data, paths, realm, lock, fd, calls, state = installation(tmp_path, monkeypatch, system)
    try:
        transaction = journal(backend, old, data, realm)
        with update_context(backend, transaction):
            backend.publish_launch_admission(old, data)
            backend.stop(old, data)
            # Model R0-owned publication cleanup after a normal shutdown.
            paths.discovery_path.unlink()
            paths.instance_lock_path.unlink()
            boundary, start = c1_owner(backend, paths, realm, fd, calls, monkeypatch)
            runtime_calls = []
            def runtime(version, selected_data, command, *args, **kwargs):
                runtime_calls.append(command)
                if command in {"up", "upgrade"}:
                    # Intent must already be durable if the updater now dies.
                    durable = json.loads((backend.root / "update-transaction.json").read_text())
                    assert durable["transaction_context"]["release0_runtime_owner"]["purpose"] == operation
                    start()
                if command == "backup":
                    destination = Path(args[args.index("--destination") + 1])
                    shutil.copytree(realm, destination)
                return {}
            monkeypatch.setattr(backend, "runtime", runtime)
            if operation == "backup":
                result = Backend.backup(backend, old, data, transaction["realm"])
                assert (Path(result["root"]) / "backup.json").is_file()
                assert runtime_calls == (["up", "backup"] if system == "linux" else [])
            else:
                candidate_root = backend.root / "versions/candidate"
                candidate = Version(candidate_root, candidate_root / "py/bin/python", candidate_root / "profile.json", {}, release=1)
                Backend.upgrade(backend, candidate, data)
                assert boundary.live
                # A fresh controller must recover the handoff from disk.
                fresh = fresh_controller(backend)
                fresh._stop_boundary = boundary
                fresh.stop(candidate, data)
                assert runtime_calls == ["upgrade"]
            assert not boundary.live
            Backend._wait_owner_lock_free(realm, timeout=0)
            expected = 0 if operation == "backup" and system == "darwin" else 1
            assert calls.count("c1-owner-stop") == expected
            assert sum(isinstance(call, list) and ("stop" in call or "bootout" in call) for call in calls) == 1
    finally:
        os.close(fd)


@pytest.mark.parametrize("system", ["linux", "darwin"])
@pytest.mark.parametrize("defect", ["birth", "realm"])
def test_temporary_owner_identity_refusal_preserves_owner(tmp_path, monkeypatch, system, defect):
    backend, old, data, paths, realm, lock, fd, calls, state = installation(tmp_path, monkeypatch, system)
    try:
        transaction = journal(backend, old, data, realm)
        with update_context(backend, transaction):
            backend.publish_launch_admission(old, data)
            backend.stop(old, data)
            paths.discovery_path.unlink()
            paths.instance_lock_path.unlink()
            boundary, start = c1_owner(backend, paths, realm, fd, calls, monkeypatch)
            backend._handoff_release0_runtime(old, data, "backup")
            start()
            if defect == "birth":
                boundary.process_birth_identity = lambda _pid: "foreign-birth"
                boundary.validate_owner = lambda **_: False
            else:
                value = json.loads(paths.discovery_path.read_text())
                value["realm_root"] = str(tmp_path / "other")
                write_json(paths.discovery_path, value)
            with pytest.raises(RuntimeOperationError):
                backend.stop(old, data)
            assert boundary.live and "c1-owner-stop" not in calls
            with pytest.raises(RuntimeError, match="lock is still held"):
                Backend._wait_owner_lock_free(realm, timeout=0)
    finally:
        os.close(fd)


@pytest.mark.parametrize("system", ["linux", "darwin"])
@pytest.mark.parametrize("gap", ["realm", "realm-and-catalog"])
def test_crash_between_restore_renames_recovers_with_gate(tmp_path, monkeypatch, system, gap):
    backend, old, data, paths, realm, lock, fd, calls, state = installation(tmp_path, monkeypatch, system)
    try:
        sentinel = realm / "user-document.txt"
        sentinel.write_text("retained data")
        transaction = journal(backend, old, data, realm)
        with update_context(backend, transaction):
            backend.publish_launch_admission(old, data)
            backend.stop(old, data)
        paths.discovery_path.unlink()
        paths.instance_lock_path.unlink()
        saved = backend.root / "backups/saved"
        Backend._copy(backend, realm, saved / "realm", clone=True)
        Backend._copy(backend, data, saved / "support", clone=True, support=True)
        save(backend, transaction, "recovering", backup={"kind": "clone", "root": str(saved)})
        realm.rename(realm.with_name(realm.name + ".failed-interrupted"))
        if gap == "realm-and-catalog":
            paths.catalog_path.unlink()
        fresh = fresh_controller(backend)
        with pytest.raises(ValueError, match="interrupted"):
            exec(launch(fresh.service, old, data, state)[-1], {})
        monkeypatch.setattr(importlib.import_module("astrid_runtime.bootstrap"), "down", runtime_down)
        rollback_transaction(backend.root, data, fresh, transaction, stop=fresh.stop,
                             switch_old=lambda *_: switch(backend.root, old), activate_old=lambda *_: None,
                             save=lambda step, **details: save(fresh, transaction, step, **details))
        assert sentinel.read_text() == "retained data"
        assert transaction["step"] == "rolled_back"
        assert Path(json.loads(paths.catalog_path.read_text())["realms"][0]["data_root"]) == realm
    finally:
        os.close(fd)


def test_failed_linux_backup_stops_temporary_owner_through_c1(tmp_path, monkeypatch):
    backend, old, data, paths, realm, lock, fd, calls, state = installation(tmp_path, monkeypatch, "linux")
    try:
        transaction = journal(backend, old, data, realm)
        with update_context(backend, transaction):
            backend.publish_launch_admission(old, data)
            backend.stop(old, data)
            paths.discovery_path.unlink()
            paths.instance_lock_path.unlink()
            boundary, start = c1_owner(backend, paths, realm, fd, calls, monkeypatch)
            def runtime(version, selected_data, command, *args, **kwargs):
                if command == "up":
                    start()
                elif command == "backup":
                    raise RuntimeOperationError("backup RPC failed")
                return {}
            monkeypatch.setattr(backend, "runtime", runtime)
            with pytest.raises(RuntimeOperationError, match="backup RPC failed"):
                Backend.backup(backend, old, data, transaction["realm"])
            assert not boundary.live and calls.count("c1-owner-stop") == 1
            Backend._wait_owner_lock_free(realm, timeout=0)
            assert list((backend.root / "backups").iterdir()) == []
    finally:
        os.close(fd)


@pytest.mark.parametrize("system", ["linux", "darwin"])
def test_candidate_cleanup_without_local_handoff_uses_c1(tmp_path, monkeypatch, system):
    backend, old, data, paths, realm, lock, fd, calls, state = installation(tmp_path, monkeypatch, system)
    try:
        backend.stop(old, data)
        boundary, start = c1_owner(backend, paths, realm, fd, calls, monkeypatch)
        start()
        candidate_root = backend.root / "versions/candidate"
        candidate = Version(candidate_root, candidate_root / "py/bin/python", candidate_root / "profile.json", {}, release=1)
        fresh = fresh_controller(backend)
        fresh._stop_boundary = boundary
        fresh.stop(candidate, data)
        assert not boundary.live and calls.count("c1-owner-stop") == 1
        assert sum(isinstance(call, list) and ("stop" in call or "bootout" in call) for call in calls) == 1
    finally:
        os.close(fd)


@pytest.mark.parametrize("system", ["linux", "darwin"])
def test_upgrade_owner_is_stopped_before_real_rollback_restore(tmp_path, monkeypatch, system):
    backend, old, data, paths, realm, lock, fd, calls, state = installation(tmp_path, monkeypatch, system)
    try:
        sentinel = realm / "user-document.txt"
        sentinel.write_text("original data")
        transaction = journal(backend, old, data, realm)
        with update_context(backend, transaction):
            backend.publish_launch_admission(old, data)
            backend.stop(old, data)
            paths.discovery_path.unlink()
            paths.instance_lock_path.unlink()
            saved = backend.root / "backups/saved"
            Backend._copy(backend, realm, saved / "realm", clone=True)
            Backend._copy(backend, data, saved / "support", clone=True, support=True)
            save(backend, transaction, "backed_up", backup={"kind": "clone", "root": str(saved)})
            boundary, start = c1_owner(backend, paths, realm, fd, calls, monkeypatch)
            candidate_root = backend.root / "versions/candidate"
            candidate = Version(candidate_root, candidate_root / "py/bin/python", candidate_root / "profile.json", {}, release=1)
            transaction["candidate"] = candidate.document()
            monkeypatch.setattr(backend, "runtime", lambda *_args, **_kwargs: start())
            Backend.upgrade(backend, candidate, data)
            sentinel.write_text("candidate modified data")
        fresh = fresh_controller(backend)
        fresh._stop_boundary = boundary
        def activate(*_):
            assert not boundary.live
            assert sentinel.read_text() == "original data"
            fresh.service.restore_release0(old, data, transaction["transaction_context"]["release0_service"])
        rollback_transaction(backend.root, data, fresh, transaction, stop=fresh.stop,
                             switch_old=lambda *_: switch(backend.root, old), activate_old=activate,
                             save=lambda step, **details: save(fresh, transaction, step, **details))
        assert calls.count("c1-owner-stop") == 1
        assert transaction["step"] == "rolled_back" and sentinel.read_text() == "original data"
        assert (saved / "realm/user-document.txt").read_text() == "original data"
        assert any((p / "user-document.txt").read_text() == "candidate modified data"
                   for p in realm.parent.glob(realm.name + ".failed-*"))
    finally:
        os.close(fd)
