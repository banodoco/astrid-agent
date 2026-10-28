"""A Runtime-authorized stop survives controller death and public CLI replay."""

import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import astrid_runtime
import pytest
from runtime_protocol import platform
from runtime_protocol.upgrade import inspect_canonical_schema

from astrid.installer import app_update, cli, core
from astrid.installer.backend import Backend
from astrid.installer.core import Installer, Version, current, read_json, switch, write_json
from astrid.installer.service import LABEL, installer_lock_path
from tests.test_installer_lifecycle import _installer_runtime, _publish_owner


class ControllerDied(BaseException):
    """Model process death without entering the updater's exception rollback."""


def _stopped_transaction(tmp_path, monkeypatch, kind):
    """Retain a real Backend/C1 stop, then discard the controller."""
    backend, data, paths, realm, boundary, _calls = _installer_runtime(tmp_path, monkeypatch)
    root = backend.root
    manager = "pinokio" if kind == "app" else None
    backend.managed_by = manager
    folder = root / "versions" / "old"
    folder.mkdir(parents=True)
    if manager == "pinokio":
        python = folder / "py" / "bin" / "python"
        python.parent.mkdir(parents=True)
        python.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
        runtime_checkout = Path(astrid_runtime.__file__).resolve().parents[1]
        source_checkout = Path(__file__).resolve().parents[1]
        environment = python.parent.parent
        declaration = folder / "services.json"
        write_json(declaration, {"services": []})
        profile = {
            "profile": "astrid", "runtime_checkout": str(runtime_checkout),
            "source_checkout": str(source_checkout), "runtime_environment": str(environment),
            "source_digest": "", "service_declaration": str(declaration),
        }
        write_json(folder / "profile.json", profile)
        write_json(paths.source_profiles_dir / "astrid.json", profile)
        catalog = read_json(paths.catalog_path)
        catalog["source_profiles"] = {"astrid": profile}
        write_json(paths.catalog_path, catalog)
        old = Version(folder, python, folder / "profile.json", {
            "ASTRID_RUNTIME_DATA_ROOT": str(data),
        }, 17, manager)
    else:
        old = Version(folder, Path(sys.executable), folder / "profile.json", {}, 17, manager)
    write_json(folder / "version.json", old.document())
    switch(root, old)
    record = {"owner": LABEL, "program_root": str(root), "data_root": str(data),
              "managed_by": manager}
    write_json(root / "install.json", record)
    if manager == "pinokio":
        record.update({
            "realm_id": "realm-1", "realm_root": str(realm),
            "identity": {"release": old.release, "managed_by": "pinokio"},
        })
        write_json(backend.service.selection, record)
        backend.service.recorded = lambda: read_json(backend.service.selection)
    else:
        write_json(backend.service.selection, record)
        backend.service.recorded = lambda: record.copy()
    transaction = {
        "format": "astrid-install-update/v1", "operation_id": "e" * 32,
        "transaction_kind": kind, "step": "stopping", "data_root": str(data),
        "realm": {"realm_id": "realm-1", "realm_root": str(realm)},
        "old": old.document(), "candidate": old.document(), "backup": None,
    }
    installer = Installer(root, data, backend, say=lambda _: None)

    def save(step, **details):
        installer._save_update(transaction, step, **details)

    stop_owner = boundary.stop_owner

    def stop_owner_and_mark_absent(**kwargs):
        result = stop_owner(**kwargs)
        boundary.live = False
        return result

    boundary.stop_owner = stop_owner_and_mark_absent
    _publish_owner(paths)
    save("stopping")
    backend.stop(old, data)
    save("stopped")
    fresh = Backend(root, backend.log, backend.uv)
    fresh.managed_by = manager
    fresh.service.recorded = lambda: read_json(backend.service.selection)
    fresh.service.systemd = lambda: False
    fresh.service.validate_stop_target = lambda *_args, **_kwargs: None
    fresh.service.login_service_exists = lambda: False
    fresh.service.disable = Mock()
    fresh.service.stop = Mock()
    for name in ("restore", "register", "start", "verify", "persist_identity"):
        setattr(fresh, name, Mock())
    fresh_boundary = type(boundary)()
    fresh_boundary.live = False
    # Race cases publish a replacement by toggling this fresh boundary live;
    # a successful C1 stop must then report that exact process absent.
    fresh_stop_owner = fresh_boundary.stop_owner

    def fresh_stop_owner_and_mark_absent(**kwargs):
        result = fresh_stop_owner(**kwargs)
        fresh_boundary.live = False
        fresh_boundary.process_birth_identity = lambda _pid: None
        return result

    fresh_boundary.stop_owner = fresh_stop_owner_and_mark_absent
    monkeypatch.setattr("astrid_runtime.runtime_boundary.LocalRuntimeBoundary", lambda: fresh_boundary)

    def recover():
        if kind == "app":
            return app_update.recover_app(root, fresh, say=lambda _: None, lifecycle=SimpleNamespace(
                validate=lambda *_: None, stop=fresh.stop, start=fresh.start))
        return Installer(root, data, fresh, say=lambda _: None)._recover_update()

    return fresh, paths, realm, fresh_boundary, transaction, recover


@pytest.mark.parametrize("kind", ["migration", "app"])
@pytest.mark.parametrize("moment", ["entry", "between_reads", "serialized", "final"])
@pytest.mark.parametrize("changed", ["pid", "birth", "instance", "realm", "root"])
def test_recovery_preserves_transaction_and_replacement_at_every_admission(
        tmp_path, monkeypatch, kind, moment, changed):
    """C1 rejects foreign realms and safely stops a current owner replacement."""
    import importlib
    from contextlib import contextmanager

    from astrid.installer.core import RuntimeOperationError

    backend, paths, realm, boundary, _transaction, recover = _stopped_transaction(
        tmp_path, monkeypatch, kind)
    tracked = [backend.root / "update-transaction.json", backend.root / "current",
               backend.service.selection, realm / "realm.sqlite3"]
    before = {path: path.read_bytes() for path in tracked}
    published = {}

    def replacement():
        identity = _publish_owner(paths)
        key, value = {
            "pid": ("pid", 888), "birth": ("process_birth_id", "replacement-birth"),
            "instance": ("runtime_instance_id", "replacement-instance"),
            "realm": ("active_realm", "replacement-realm"),
            "root": ("realm_root", str(tmp_path / "different-realm")),
        }[changed]
        identity[key] = value
        write_json(paths.discovery_path, identity)
        write_json(paths.instance_lock_path, {**identity, "realm_id": identity["active_realm"]})
        published.update({path: path.read_bytes()
                          for path in (paths.discovery_path, paths.instance_lock_path)})
        boundary.live = True
        boundary.process_birth_identity = lambda pid: (
            identity["process_birth_id"] if pid == identity["pid"] else None)
        boundary.endpoint_metadata = lambda **_: {
            "runtime_instance_id": identity["runtime_instance_id"],
            "realm_id": identity["active_realm"],
        }

    bootstrap = importlib.import_module("astrid_runtime.bootstrap")
    if moment == "entry":
        replacement()
    elif moment == "between_reads":
        read_support_json = bootstrap._read_support_json
        replaced = False

        def replace_between_reads(path):
            nonlocal replaced
            result = read_support_json(path)
            if path == paths.discovery_path and not replaced:
                replaced = True
                replacement()
            return result

        monkeypatch.setattr(bootstrap, "_read_support_json", replace_between_reads)
    else:
        attribute = "_bootstrap_mutex" if moment == "serialized" else "interruption_fence"
        original = getattr(bootstrap, attribute)

        @contextmanager
        def replace_at_fence(*args, **kwargs):
            with original(*args, **kwargs) as result:
                replacement()
                yield result

        monkeypatch.setattr(bootstrap, attribute, replace_at_fence)

    if changed in {"realm", "root"} or moment in {"between_reads", "final"}:
        with pytest.raises(RuntimeOperationError) as raised:
            recover()
        assert not core.is_lifecycle_refusal(raised.value)
        assert boundary.stop_calls == 0
        for path in tracked:
            assert path.read_bytes() == before[path], path
        for name in ("restore", "register", "start", "verify", "persist_identity"):
            getattr(backend, name).assert_not_called()
        backend.service.disable.assert_not_called()
        backend.service.stop.assert_not_called()
        assert {path: path.read_bytes() for path in published} == published
    else:
        recover()
        assert boundary.stop_calls == 1
        assert read_json(backend.root / "update-transaction.json")["step"] == "rolled_back"
        backend.start.assert_called_once()


@pytest.mark.parametrize("kind", ["migration", "app"])
def test_fresh_backend_accepts_exact_json_authority_with_real_c1_live_checks(tmp_path, monkeypatch, kind):
    backend, data, paths, realm, boundary, manager_calls = _installer_runtime(tmp_path, monkeypatch)
    backend.managed_by = "pinokio" if kind == "app" else None
    _publish_owner(paths)

    backend.stop(None, data)

    assert boundary.stop_calls == 1
    assert not paths.discovery_path.exists()
    assert not paths.instance_lock_path.exists()
    assert "installer_operation_id" not in read_json(paths.catalog_path)
    assert manager_calls == ([] if kind == "app" else ["disable", "stop"])

    # A fresh controller has no in-memory evidence. C1 admits the exact
    # selected realm with no published Runtime owner and no historical tag.
    from astrid.installer.backend import Backend
    fresh = Backend(backend.root, backend.log, backend.uv, managed_by=backend.managed_by)
    fresh.service.recorded = backend.service.recorded
    fresh.service.login_service_exists = lambda: False
    fresh.service.disable = lambda: manager_calls.append("fresh-disable")
    fresh.service.stop = lambda: manager_calls.append("fresh-stop")
    fresh.service.validate_stop_target = lambda *_args, **_kwargs: None
    fresh_boundary = type(boundary)()
    fresh_boundary.live = False
    monkeypatch.setattr("astrid_runtime.runtime_boundary.LocalRuntimeBoundary", lambda: fresh_boundary)
    fresh.stop(None, data)
    assert fresh_boundary.stop_calls == 0
    assert manager_calls[-2:] == ([] if kind == "app" else ["fresh-disable", "fresh-stop"])


@pytest.mark.parametrize("case", ["foreign_realm", "unknown_owner", "pid_reused"])
def test_real_c1_refuses_untrusted_owner_before_manager_action(tmp_path, monkeypatch, case):
    from astrid.installer.core import RuntimeOperationError

    backend, data, paths, realm, boundary, manager_calls = _installer_runtime(tmp_path, monkeypatch)
    _publish_owner(paths)
    if case == "foreign_realm":
        catalog = read_json(paths.catalog_path)
        catalog["selected_realm_id"] = "foreign-realm"
        write_json(paths.catalog_path, catalog)
    elif case == "unknown_owner":
        boundary.identity_error = OSError("owner identity unavailable")
    else:
        boundary.process_birth_identity = lambda _pid: "reused-pid-birth"

    before = {
        "catalog": paths.catalog_path.read_bytes(),
        "discovery": paths.discovery_path.read_bytes(),
        "lock": paths.instance_lock_path.read_bytes(),
    }
    with pytest.raises(RuntimeOperationError) as raised:
        backend.stop(None, data)
    assert not core.is_lifecycle_refusal(raised.value)
    assert boundary.stop_calls == 0
    assert manager_calls == []
    assert paths.catalog_path.read_bytes() == before["catalog"]
    assert paths.discovery_path.read_bytes() == before["discovery"]
    assert paths.instance_lock_path.read_bytes() == before["lock"]


def test_real_c1_cleans_proven_stale_publication_without_historical_receipt(tmp_path, monkeypatch):
    backend, data, paths, realm, boundary, manager_calls = _installer_runtime(tmp_path, monkeypatch)
    _publish_owner(paths)
    boundary.live = False
    boundary.is_pid_alive = lambda _pid: False
    boundary.process_birth_identity = lambda _pid: None
    backend.stop(None, data)
    assert boundary.stop_calls == 0
    assert not paths.discovery_path.exists()
    assert not paths.instance_lock_path.exists()
    assert manager_calls == ["disable", "stop"]


@pytest.mark.parametrize("kind", ["migration", "app"])
def test_recovery_requires_authorized_launch_intent_and_live_bound_identity(tmp_path, monkeypatch, kind):
    """The selected live Runtime owner is revalidated; journals carry no launch authority."""
    backend, paths, _realm, boundary, _transaction, recover = _stopped_transaction(
        tmp_path, monkeypatch, kind)
    _publish_owner(paths)
    boundary.live = True

    recover()

    assert boundary.stop_calls == 1
    assert read_json(backend.root / "update-transaction.json")["step"] == "rolled_back"
    backend.start.assert_called_once()
    assert "stop_authority" not in read_json(backend.root / "update-transaction.json")
    assert "launch_intent" not in read_json(backend.root / "update-transaction.json")


@pytest.mark.parametrize("case", ["foreign_realm", "wrong_root", "unknown_owner", "pid_reused"])
def test_recovery_refuses_unrelated_stale_publication_even_with_copied_operation_tag(tmp_path, monkeypatch, case):
    """Foreign or uncertain publications fail C1 even if a journal has an operation id."""
    from astrid.installer.core import RuntimeOperationError

    backend, paths, realm, boundary, transaction, recover = _stopped_transaction(
        tmp_path, monkeypatch, "migration")
    _publish_owner(paths)
    boundary.live = True
    if case == "foreign_realm":
        discovery = read_json(paths.discovery_path)
        discovery["active_realm"] = "foreign-realm"
        write_json(paths.discovery_path, discovery)
        write_json(paths.instance_lock_path, {**discovery, "realm_id": "foreign-realm"})
    elif case == "wrong_root":
        discovery = read_json(paths.discovery_path)
        discovery["realm_root"] = str(realm.parent / "foreign")
        write_json(paths.discovery_path, discovery)
        write_json(paths.instance_lock_path, {**discovery, "realm_id": "realm-1"})
    elif case == "unknown_owner":
        boundary.identity_error = OSError("identity unavailable")
    else:
        boundary.process_birth_identity = lambda _pid: "reused-pid-birth"

    journal = backend.root / "update-transaction.json"
    journal_before = journal.read_bytes()
    pointer_before = (backend.root / "current").read_bytes()
    with pytest.raises(RuntimeOperationError) as raised:
        recover()
    assert not core.is_lifecycle_refusal(raised.value)
    assert boundary.stop_calls == 0
    assert journal.read_bytes() == journal_before
    assert (backend.root / "current").read_bytes() == pointer_before
    backend.restore.assert_not_called()
    backend.start.assert_not_called()


def test_recovery_accepts_exact_retained_stale_publication_after_repeated_absence(tmp_path, monkeypatch):
    backend, paths, _realm, boundary, _transaction, recover = _stopped_transaction(
        tmp_path, monkeypatch, "migration")
    _publish_owner(paths)
    boundary.live = False

    recover()

    assert boundary.stop_calls == 0
    assert not paths.discovery_path.exists()
    assert not paths.instance_lock_path.exists()
    assert read_json(backend.root / "update-transaction.json")["step"] == "rolled_back"
    backend.start.assert_called_once()


def test_recovery_reconciles_foreground_with_admitted_live_owner(tmp_path, monkeypatch):
    """A foreground only selection is stopped by its recorded live PID incarnation."""
    return _foreground_stop_case(tmp_path, monkeypatch, "matching")


@pytest.mark.parametrize("mutation", ["pid", "birth", "uid", "realm", "root"])
def test_recovery_refuses_foreground_without_runtime_custody(tmp_path, monkeypatch, mutation):
    """A process identity or selected-realm mismatch cannot authorize a signal."""
    return _foreground_stop_case(tmp_path, monkeypatch, mutation)


def _foreground_stop_case(tmp_path, monkeypatch, mutation):
    from astrid.installer.core import RuntimeOperationError

    backend, data, paths, realm_root, boundary, manager_calls = _installer_runtime(tmp_path, monkeypatch)
    foreground = {"pid": 456, "birth_id": "birth-foreground"}
    record = {
        "owner": LABEL, "program_root": str(backend.root), "data_root": str(data),
        "realm_id": "realm-1", "realm_root": str(realm_root),
        "foreground": foreground,
    }
    if mutation == "realm":
        record["realm_id"] = "foreign-realm"
    elif mutation == "root":
        record["realm_root"] = str(realm_root.parent / "foreign")
    backend.service.recorded = lambda: record.copy()
    expected_uid = platform.current_user()
    identity = {"pid": 456, "birth_id": "birth-foreground", "uid": expected_uid}
    if mutation == "pid":
        identity["pid"] = 457
    elif mutation == "birth":
        identity["birth_id"] = "reused-birth"
    elif mutation == "uid":
        identity["uid"] = expected_uid + 1
    signals = []
    observations = [identity]
    monkeypatch.setattr(platform, "process_identity", lambda _pid: observations.pop(0) if observations else None)
    monkeypatch.setattr(platform, "signal_process", lambda target, signum: signals.append((target, signum)))

    if mutation != "matching":
        with pytest.raises(RuntimeOperationError) as raised:
            backend.stop(None, data)
        assert not core.is_lifecycle_refusal(raised.value)
        assert signals == []
        assert manager_calls == []
    else:
        backend.stop(None, data)
        assert signals == [(identity, platform.TERM)]
        assert manager_calls == []
    assert boundary.stop_calls == 0


def test_recovery_refuses_stale_publication_when_process_absence_is_not_proven(tmp_path, monkeypatch):
    from astrid.installer.core import RuntimeOperationError

    backend, paths, _realm, boundary, _transaction, recover = _stopped_transaction(
        tmp_path, monkeypatch, "migration")
    _publish_owner(paths)
    boundary.live = False
    boundary.is_pid_alive = lambda _pid: (_ for _ in ()).throw(OSError("liveness is unknown"))
    journal = backend.root / "update-transaction.json"
    before = journal.read_bytes()

    with pytest.raises(RuntimeOperationError) as raised:
        recover()

    assert not core.is_lifecycle_refusal(raised.value)
    assert journal.read_bytes() == before
    assert paths.discovery_path.exists()
    assert paths.instance_lock_path.exists()
    backend.restore.assert_not_called()


@pytest.mark.parametrize("kind", ["migration", "app"])
def test_recovery_rechecks_exact_stale_pid_absence_at_final_fence(tmp_path, monkeypatch, kind):
    import importlib
    from contextlib import contextmanager

    from astrid.installer.core import RuntimeOperationError

    backend, paths, _realm, boundary, _transaction, recover = _stopped_transaction(
        tmp_path, monkeypatch, kind)
    _publish_owner(paths)
    boundary.live = True
    bootstrap = importlib.import_module("astrid_runtime.bootstrap")
    mutex = bootstrap._bootstrap_mutex

    @contextmanager
    def replacement_after_absence(paths_arg):
        with mutex(paths_arg):
            _publish_owner(paths_arg, pid=887, birth="replacement-birth")
            yield

    monkeypatch.setattr(bootstrap, "_bootstrap_mutex", replacement_after_absence)
    journal = backend.root / "update-transaction.json"
    before = journal.read_bytes()

    with pytest.raises(RuntimeOperationError) as raised:
        recover()

    assert not core.is_lifecycle_refusal(raised.value)
    assert journal.read_bytes() == before
    assert read_json(paths.discovery_path)["pid"] == 887
    backend.restore.assert_not_called()


@pytest.mark.parametrize("kind", ["migration", "app"])
@pytest.mark.parametrize("boundary_name", ["stop-return", "stopped"])
def test_cli_recovers_completed_stop_with_fresh_controller(tmp_path, monkeypatch, kind, boundary_name):
    backend, data, paths, realm, boundary, _calls = _installer_runtime(tmp_path, monkeypatch)
    root = backend.root
    managed_by = "pinokio" if kind == "app" else None
    backend.managed_by = managed_by
    versions = []
    for name, release in (("old", 17), ("candidate", 18)):
        folder = root / "versions" / name
        folder.mkdir(parents=True)
        version = Version(folder, Path(sys.executable), folder / "profile.json", {}, release, managed_by)
        write_json(folder / "version.json", version.document())
        versions.append(version)
    old, candidate = versions
    switch(root, old)
    record = {"owner": LABEL, "program_root": str(root), "data_root": str(data)}
    marker = {"owner": LABEL, "data_root": str(data)}
    if managed_by:
        record["managed_by"] = marker["managed_by"] = managed_by
    write_json(root / "install.json", marker)
    backend.service.recorded = lambda: record.copy()
    _publish_owner(paths)
    installer = Installer(root, data, backend, say=lambda _: None, managed_by=managed_by)
    save = installer._save_update

    def save_and_die(transaction, step, **details):
        save(transaction, step, **details)
        if step == boundary_name:
            raise ControllerDied()

    def stop_and_die(version, workspace):
        backend.stop(version, workspace)
        boundary.live = False
        if boundary_name == "stop-return":
            raise ControllerDied()

    monkeypatch.setattr(installer, "_save_update", save_and_die)
    with pytest.raises(ControllerDied):
        installer._run_update_transaction(
            old, {"realm_id": "realm-1", "realm_root": str(realm)},
            prepare=lambda: candidate, stop=stop_and_die,
            switch_candidate=lambda *_: pytest.fail("switch before injected death"),
            switch_old=lambda *_: pytest.fail("exception rollback after process death"),
            activate=lambda *_: pytest.fail("activation before injected death"),
            activate_old=lambda *_: pytest.fail("exception rollback after process death"),
            kind=kind, restore_workspace=kind != "app",
        )
    journal = root / "update-transaction.json"
    retained = read_json(journal)
    assert retained["step"] == ("stopping" if boundary_name == "stop-return" else "stopped")
    assert "stop_authority" not in retained
    assert "launch_intent" not in retained
    assert not paths.discovery_path.exists()
    assert not paths.instance_lock_path.exists()

    fresh = Backend(root, backend.log, tmp_path / "unavailable-uv")
    fresh.managed_by = managed_by
    fresh.service.recorded = lambda: record.copy()
    fresh.service.systemd = lambda: False
    fresh.service.validate_stop_target = lambda *_args, **_kwargs: None
    fresh.service.disable = lambda: None
    fresh.service.stop = lambda: None
    fresh.service.login_service_exists = lambda: False
    fresh_boundary = type(boundary)()
    fresh_boundary.live = False
    monkeypatch.setattr("astrid_runtime.runtime_boundary.LocalRuntimeBoundary", lambda: fresh_boundary)
    ready = []

    def verify(version, workspace, realm_id):
        assert version == old
        assert inspect_canonical_schema(realm, expected_realm_id=realm_id)["realm_id"] == "realm-1"
        ready.append((version.release, realm_id))

    fresh.register = lambda *_: None
    fresh.start = lambda *_: None
    fresh.verify = verify
    fresh.source_checkout_ownership = Mock(side_effect=AssertionError("mutable preflight before recovery"))
    fresh.backup = Mock(side_effect=AssertionError("backup during recovery"))
    fresh.upgrade = Mock(side_effect=AssertionError("migration during recovery"))
    fresh.restore = Mock(side_effect=AssertionError("workspace restore without backup"))
    monkeypatch.setattr(cli, "Backend", lambda *_args, **_kwargs: fresh)
    monkeypatch.setattr(platform, "log_dir", lambda: tmp_path / "logs")
    monkeypatch.setattr(cli, "download", lambda *_: pytest.fail("download before recovery"))
    monkeypatch.setattr(cli, "validate_manifest", lambda *_: pytest.fail("new inputs before recovery"))

    if kind == "app":
        def foreground_start(version, workspace, realm_id):
            lock = installer_lock_path(fresh.service.home, fresh.service.system)
            with lock.open("a+b") as stream:
                platform.lock_file(stream.fileno(), blocking=False)
                platform.unlock_file(stream.fileno())
            verify(version, workspace, realm_id)

        lifecycle = SimpleNamespace(validate=lambda *_: None, stop=fresh.stop, start=foreground_start)
        monkeypatch.setattr(app_update, "PinokioLifecycle", lambda *_: lifecycle)
        fresh.persist_identity = lambda version: None
        arguments = ["--update-app", "--managed-by", "pinokio"]
    else:
        arguments = ["--update"]
    assert cli.main([
        *arguments, "--root", str(root), "--uv", str(tmp_path / "unavailable-uv"),
        "--manifest", str(tmp_path / "unavailable-manifest"),
    ]) == 0
    assert current(root) == old
    assert read_json(journal)["step"] == "rolled_back"
    assert ready == [(17, "realm-1")]
    assert fresh_boundary.stop_calls == 0
    fresh.backup.assert_not_called()
    fresh.upgrade.assert_not_called()
    fresh.restore.assert_not_called()


@pytest.mark.parametrize("refusal", ["busy", "replacement"])
def test_fresh_controller_recovery_refuses_without_restore_or_relaunch(tmp_path, monkeypatch, refusal):
    import importlib
    import sqlite3
    from contextlib import contextmanager

    from astrid.installer.core import RuntimeOperationError

    backend, data, paths, realm, boundary, _calls = _installer_runtime(tmp_path, monkeypatch)
    root = backend.root
    folder = root / "versions" / "old"
    folder.mkdir(parents=True)
    old = Version(folder, Path(sys.executable), folder / "profile.json", {}, 17)
    write_json(folder / "version.json", old.document())
    switch(root, old)
    write_json(root / "install.json", {"owner": LABEL, "data_root": str(data)})
    _publish_owner(paths)
    transaction = {
        "format": "astrid-install-update/v1", "operation_id": "c" * 32,
        "transaction_kind": "migration", "step": "stopping", "data_root": str(data),
        "realm": {"realm_id": "realm-1", "realm_root": str(realm)},
        "old": old.document(), "candidate": old.document(), "backup": None,
    }
    installer = Installer(root, data, backend, say=lambda _: None)

    def save(step, **details):
        installer._save_update(transaction, step, **details)

    save("stopping")
    backend.stop(old, data)
    save("stopped")
    before = (root / "update-transaction.json").read_bytes()

    fresh = Backend(root, backend.log, backend.uv)
    fresh.service.recorded = backend.service.recorded
    fresh.service.systemd = lambda: False
    fresh.service.validate_stop_target = lambda *_args, **_kwargs: None
    fresh.service.login_service_exists = lambda: False
    fresh.service.disable = Mock(side_effect=AssertionError("manager action after refusal"))
    fresh.service.stop = Mock(side_effect=AssertionError("manager action after refusal"))
    for name in ("restore", "register", "start", "verify"):
        setattr(fresh, name, Mock(side_effect=AssertionError(f"{name} after refusal")))
    fresh_boundary = type(boundary)()
    fresh_boundary.live = False
    monkeypatch.setattr("astrid_runtime.runtime_boundary.LocalRuntimeBoundary", lambda: fresh_boundary)
    if refusal == "busy":
        with sqlite3.connect(realm / "realm.sqlite3") as connection:
            connection.execute(
                "INSERT INTO tasks (id, run_id, capability, status, created_at, updated_at) "
                "VALUES ('busy-1', 'run-1', 'render', 'running', '2026-10-27', '2026-10-27')"
            )
    else:
        bootstrap = importlib.import_module("astrid_runtime.bootstrap")
        mutex = bootstrap._bootstrap_mutex

        @contextmanager
        def replacement_before_final_admission(paths):
            with mutex(paths):
                replacement = _publish_owner(paths, pid=888, birth="replacement-birth")
                replacement["active_realm"] = "foreign-realm"
                write_json(paths.discovery_path, replacement)
                write_json(paths.instance_lock_path, {**replacement, "realm_id": "foreign-realm"})
                yield

        monkeypatch.setattr(bootstrap, "_bootstrap_mutex", replacement_before_final_admission)
    with pytest.raises(RuntimeOperationError) as raised:
        Installer(root, data, fresh, say=lambda _: None)._recover_update()
    assert core.is_lifecycle_refusal(raised.value) is (refusal == "busy")
    if refusal == "busy":
        assert raised.value.details["blockers"][0]["task_id"] == "busy-1"
    else:
        assert "realm" in str(raised.value).lower()
        assert read_json(paths.discovery_path)["pid"] == 888
    assert (root / "update-transaction.json").read_bytes() == before
    assert current(root) == old
    assert fresh_boundary.stop_calls == 0
    for name in ("restore", "register", "start", "verify"):
        getattr(fresh, name).assert_not_called()


@pytest.mark.integration
@pytest.mark.linux_native
@pytest.mark.parametrize("kind", ["migration", "app"])
def test_new_interpreter_recovers_real_completed_c1_stop(tmp_path, monkeypatch, kind):
    import os
    import subprocess
    import textwrap

    from astrid_runtime.bootstrap import SourceProfile
    from astrid_runtime.runtime_boundary import LocalRuntimeBoundary

    backend, data, paths, realm, _boundary, _calls = _installer_runtime(tmp_path, monkeypatch)
    root = backend.root
    managed_by = "pinokio" if kind == "app" else None
    backend.managed_by = managed_by
    folder = root / "versions" / "old"
    folder.mkdir(parents=True)
    profile = folder / "profile.json"
    import astrid_runtime
    write_json(profile, SourceProfile(
        profile="astrid", runtime_checkout=str(Path(astrid_runtime.__file__).resolve().parents[1]),
        source_checkout=str(Path(__file__).resolve().parents[1]),
    ).as_dict())
    old = Version(folder, Path(sys.executable), profile, {}, 17, managed_by)
    write_json(folder / "version.json", old.document())
    switch(root, old)
    record = {"owner": LABEL, "program_root": str(root), "data_root": str(data),
              "managed_by": managed_by}
    write_json(root / "install.json", record)
    write_json(backend.service.selection, record)
    backend.service.recorded = lambda: record.copy()
    backend.service.validate_stop_target = lambda *_args, **_kwargs: None
    # This test needs the configured real profile; the helper normally supplies
    # a minimal source object to tests which only exercise stop admission.
    monkeypatch.undo()
    monkeypatch.setenv("HOME", str(backend.service.home))
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "xdg-data"))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg-config"))
    retained = platform.spawn_tree(
        [sys.executable, "-c", "import time; time.sleep(60)"], detached=True,
        stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )
    native = LocalRuntimeBoundary()
    birth = native.process_birth_identity(retained.pid)
    assert birth

    class InitialController:
        process_birth_identity = staticmethod(native.process_birth_identity)

        def prepare_restart(self, **_kwargs):
            pass

        def is_pid_alive(self, pid):
            return native.process_birth_identity(pid) is not None

        def validate_owner(self, **_kwargs):
            return True  # Modeled endpoint, real OS child.

        def endpoint_metadata(self, **_kwargs):
            return {"runtime_instance_id": "test-instance", "realm_id": "realm-1"}

        def stop_owner(self, **identity):
            assert native.process_birth_identity(retained.pid) == identity["process_birth_id"] == birth
            platform.stop_tree(retained, grace=1)
            assert retained.poll() is not None
            return {"status": "stopped"}

    monkeypatch.setattr("astrid_runtime.runtime_boundary.LocalRuntimeBoundary", InitialController)
    _publish_owner(paths, pid=retained.pid, birth=birth)
    installer = Installer(root, data, backend, say=lambda _: None)
    transaction = {
        "format": "astrid-install-update/v1", "operation_id": "d" * 32,
        "transaction_kind": kind, "step": "stopping", "data_root": str(data),
        "realm": {"realm_id": "realm-1", "realm_root": str(realm)},
        "old": old.document(), "candidate": old.document(), "backup": None,
    }

    def save(step, **details):
        installer._save_update(transaction, step, **details)

    try:
        save("stopping")
        backend.stop(old, data)
        # Leave the on-disk step at stopping, exactly the stop-return crash gap.
        assert read_json(root / "update-transaction.json")["step"] == "stopping"
        assert not paths.discovery_path.exists()
        script = textwrap.dedent('''
            import sys
            from pathlib import Path
            from types import SimpleNamespace
            from astrid.installer import app_update, cli
            from astrid.installer.backend import Backend
            from astrid.installer.core import current, read_json
            from astrid.installer.service import installer_lock_path
            from runtime_protocol import platform
            from runtime_protocol.upgrade import inspect_canonical_schema
            root, data, kind = Path(sys.argv[1]), Path(sys.argv[2]), sys.argv[3]
            backend = Backend(root, root / 'recovery.log', root / 'absent-uv')
            backend.managed_by = 'pinokio' if kind == 'app' else None
            backend.service.systemd = lambda: False
            backend.service.validate_stop_target = lambda *a, **kw: None
            backend.service.login_service_exists = lambda: False
            backend.service.disable = lambda: None
            backend.service.stop = lambda: None
            ready = []
            def verify(version, workspace, realm_id):
                realm = read_json(root / 'update-transaction.json')['realm']
                assert inspect_canonical_schema(realm['realm_root'], expected_realm_id=realm_id)['realm_id'] == realm_id
                assert version.release == 17
                ready.append(realm_id)
            backend.register = lambda *a: None
            backend.start = lambda *a: None
            backend.verify = verify
            cli.Backend = lambda *a, **kw: backend
            platform.log_dir = lambda: root / 'logs'
            cli.download = lambda *a: (_ for _ in ()).throw(AssertionError('fresh input before recovery'))
            arguments = ['--root', str(root), '--uv', str(root / 'absent-uv'), '--manifest', str(root / 'absent-manifest')]
            if kind == 'app':
                def start(version, workspace, realm_id):
                    with installer_lock_path(backend.service.home, backend.service.system).open('a+b') as stream:
                        platform.lock_file(stream.fileno(), blocking=False)
                        platform.unlock_file(stream.fileno())
                    verify(version, workspace, realm_id)
                app_update.PinokioLifecycle = lambda *a: SimpleNamespace(validate=lambda *a: None, stop=backend.stop, start=start)
                arguments += ['--update-app', '--managed-by', 'pinokio']
            assert cli.main(arguments) == 0
            assert read_json(root / 'update-transaction.json')['step'] == 'rolled_back'
            assert current(root).release == 17
            assert ready == ['realm-1']
        ''')
        result = subprocess.run(
            [sys.executable, "-c", script, str(root), str(data), kind],
            env=os.environ.copy(), capture_output=True, text=True, timeout=30,
        )
        assert result.returncode == 0, result.stdout + result.stderr
        assert read_json(root / "update-transaction.json")["step"] == "rolled_back"
    finally:
        if retained.poll() is None:
            platform.stop_tree(retained, grace=0.2)


def _readable_restore_gap(tmp_path, monkeypatch, *, managed_by=None):
    """Build the approved absent-catalog, readable-realm recovery shape."""
    import importlib

    backend, data, paths, realm, boundary, manager_calls = _installer_runtime(tmp_path, monkeypatch)
    bootstrap = importlib.import_module("astrid_runtime.bootstrap")
    runtime_io = importlib.import_module("astrid_runtime.io")
    atomic_write_json = bootstrap.atomic_write_json
    io_atomic_write_json = runtime_io.atomic_write_json
    backend._restore_catalog_writes = []

    def track_catalog_write(path, value):
        if Path(path) == paths.catalog_path:
            backend._restore_catalog_writes.append(Path(path))
        return atomic_write_json(path, value)

    def track_io_catalog_write(path, value):
        if Path(path) == paths.catalog_path:
            backend._restore_catalog_writes.append(Path(path))
        return io_atomic_write_json(path, value)

    monkeypatch.setattr(bootstrap, "atomic_write_json", track_catalog_write)
    monkeypatch.setattr(runtime_io, "atomic_write_json", track_io_catalog_write)
    root = backend.root
    backend.managed_by = managed_by
    old_root = root / "versions" / "old"
    candidate_root = root / "versions" / "candidate"
    old_root.mkdir(parents=True)
    candidate_root.mkdir(parents=True)
    old_profile, candidate_profile = old_root / "profile.json", candidate_root / "profile.json"
    write_json(old_profile, {})
    write_json(candidate_profile, {})
    old = Version(old_root, Path(sys.executable), old_profile, {}, 16, managed_by)
    candidate = Version(candidate_root, Path(sys.executable), candidate_profile, {}, 17, managed_by)
    write_json(old_root / "version.json", old.document())
    write_json(candidate_root / "version.json", candidate.document())
    switch(root, candidate)

    selection = {
        "owner": LABEL, "program_root": str(root), "data_root": str(data),
        "realm_id": "realm-1", "realm_root": str(realm),
    }
    marker = {"owner": LABEL, "data_root": str(data)}
    if managed_by:
        selection["managed_by"] = marker["managed_by"] = managed_by
    write_json(root / "install.json", marker)
    write_json(backend.service.selection, selection)
    backend.service.recorded = lambda: read_json(backend.service.selection)
    backend.service.validate_stop_target = Mock()
    backend.service.disable = Mock()
    backend.service.stop = Mock()

    backup_root = root / "backups" / "restore-1"
    (backup_root / "support" / "runtime").mkdir(parents=True)
    retained_catalog = read_json(paths.catalog_path)
    retained_catalog_path = backup_root / "support" / "runtime" / "catalog.json"
    write_json(retained_catalog_path, retained_catalog)
    paths.catalog_path.unlink()

    transaction = {
        "format": "astrid-install-update/v1", "operation_id": "a" * 32,
        "transaction_kind": "migration", "step": "recovering", "data_root": str(data),
        "realm": {"realm_id": "realm-1", "realm_root": str(realm)},
        "old": old.document(), "candidate": candidate.document(),
        "backup": {"root": str(backup_root), "kind": "clone"},
    }
    journal_path = root / "update-transaction.json"
    write_json(journal_path, transaction)
    return backend, data, paths, realm, boundary, manager_calls, old, candidate, backup_root, journal_path


def _backup_bytes(root):
    result = {}
    for path in sorted(root.rglob("*")):
        relative = path.relative_to(root)
        if path.is_symlink():
            result[relative] = ("symlink", path.readlink())
        elif path.is_file():
            result[relative] = ("file", path.read_bytes())
        elif path.is_dir():
            result[relative] = ("directory", None)
    return result


def _assert_restore_refusal_unchanged(backend, paths, realm, boundary, manager_calls,
                                      backup_root, journal_path, pointer_bytes,
                                      journal_bytes, backup_before, realm_bytes):
    assert journal_path.read_bytes() == journal_bytes
    assert _backup_bytes(backup_root) == backup_before
    assert not paths.catalog_path.exists()
    assert (backend.root / "current").read_bytes() == pointer_bytes
    assert boundary.stop_calls == 0
    assert backend._restore_catalog_writes == []
    assert (realm / "realm.sqlite3").read_bytes() == realm_bytes
    assert manager_calls == []
    # Refusal proof is "no manager mutation". The non-Pinokio public stop path
    # runs the read-only OS-manager probe before C1 by design (Backend.stop), so
    # callers that never reach stop assert the probe is absent themselves.
    backend.service.disable.assert_not_called()
    backend.service.stop.assert_not_called()
    assert (realm / "realm.sqlite3").is_file()


@pytest.mark.parametrize("case", ["busy", "foreign_realm", "wrong_root", "wrong_data_root",
                                  "malformed_selection", "linked_backup_catalog", "publication",
                                  "unknown_owner", "pid_reused"])
def test_backend_preflight_refuses_readable_restore_gap_before_writes(tmp_path, monkeypatch, case):
    import sqlite3

    from astrid.installer.core import RuntimeOperationError

    backend, data, paths, realm, boundary, manager_calls, _old, _candidate, backup, journal = (
        _readable_restore_gap(tmp_path, monkeypatch))
    if case == "busy":
        with sqlite3.connect(realm / "realm.sqlite3") as connection:
            connection.execute(
                "INSERT INTO tasks (id, run_id, capability, status, created_at, updated_at) "
                "VALUES ('restore-busy', 'run-1', 'render', 'running', '2026-10-27', '2026-10-27')"
            )
    elif case == "foreign_realm":
        selected = read_json(backend.service.selection)
        selected["realm_id"] = "foreign-realm"
        write_json(backend.service.selection, selected)
    elif case == "wrong_root":
        selected = read_json(backend.service.selection)
        selected["realm_root"] = str(tmp_path / "different-readable-realm")
        write_json(backend.service.selection, selected)
    elif case == "wrong_data_root":
        selected = read_json(backend.service.selection)
        selected["data_root"] = str(tmp_path / "different-data")
        write_json(backend.service.selection, selected)
    elif case == "malformed_selection":
        backend.service.selection.write_text("{broken", encoding="utf-8")
    elif case == "linked_backup_catalog":
        catalog = backup / "support" / "runtime" / "catalog.json"
        payload = catalog.read_bytes()
        catalog.unlink()
        outside = tmp_path / "outside-catalog.json"
        outside.write_bytes(payload)
        catalog.symlink_to(outside)
    elif case in {"publication", "unknown_owner", "pid_reused"}:
        identity = _publish_owner(paths, pid=999999, birth="unknown-birth")
        if case == "pid_reused":
            identity["process_birth_id"] = "reused-pid-old-birth"
        write_json(paths.discovery_path, identity)
        write_json(paths.instance_lock_path, {**identity, "realm_id": "realm-1"})

    pointer_before = (backend.root / "current").read_bytes()
    journal_before = journal.read_bytes()
    backup_before = _backup_bytes(backup)
    realm_before = (realm / "realm.sqlite3").read_bytes()
    with pytest.raises(RuntimeOperationError) as raised:
        # This public Backend entry independently derives disk context; there is
        # no transaction_stop_context installed around the call.
        backend.preflight_stop(data)
    # This preflight rejects the interrupted restore before the C1 lifecycle
    # report; even a busy realm here is not stop-refusal authority.
    assert not core.is_lifecycle_refusal(raised.value)
    _assert_restore_refusal_unchanged(
        backend, paths, realm, boundary, manager_calls, backup, journal,
        pointer_before, journal_before, backup_before, realm_before)
    # Preflight never reaches Backend.stop, so no OS-manager probe may run.
    backend.service.validate_stop_target.assert_not_called()


def _run_public_restore_refusal(tmp_path, monkeypatch, *, managed_by, case):
    import sqlite3

    backend, data, paths, realm, boundary, manager_calls, old, candidate, backup, journal = (
        _readable_restore_gap(tmp_path, monkeypatch, managed_by=managed_by))
    if case == "busy":
        with sqlite3.connect(realm / "realm.sqlite3") as connection:
            connection.execute(
                "INSERT INTO tasks (id, run_id, capability, status, created_at, updated_at) "
                "VALUES ('restore-busy', 'run-1', 'render', 'running', '2026-10-27', '2026-10-27')"
            )
    elif case == "foreign_realm":
        selected = read_json(backend.service.selection)
        selected["realm_id"] = "foreign-realm"
        write_json(backend.service.selection, selected)
    elif case == "wrong_root":
        selected = read_json(backend.service.selection)
        selected["realm_root"] = str(tmp_path / "different-readable-realm")
        write_json(backend.service.selection, selected)
    elif case == "malformed_selection":
        backend.service.selection.write_text("{broken", encoding="utf-8")
    elif case in {"publication", "unknown_owner", "pid_reused"}:
        identity = _publish_owner(paths, pid=999999, birth="unknown-birth")
        if case == "pid_reused":
            identity["process_birth_id"] = "reused-pid-old-birth"
        write_json(paths.discovery_path, identity)
        write_json(paths.instance_lock_path, {**identity, "realm_id": "realm-1"})

    from runtime_protocol import platform

    backend.source_checkout_ownership = Mock(return_value=False)
    backend.restore = Mock(side_effect=AssertionError("workspace restore before C1"))
    backend.register = Mock(side_effect=AssertionError("register before C1"))
    backend.start = Mock(side_effect=AssertionError("start before C1"))
    backend.verify = Mock(side_effect=AssertionError("verify before C1"))
    boundary.stop_calls = 0
    monkeypatch.setattr(cli, "Backend", lambda *_args, **_kwargs: backend)
    monkeypatch.setattr(platform, "log_dir", lambda: tmp_path / "logs")
    lifecycle_calls = {"validate": 0, "stop": 0, "start": 0}
    if managed_by == "pinokio":
        def lifecycle(_root, _backend):
            return SimpleNamespace(
                validate=lambda *_: lifecycle_calls.__setitem__("validate", lifecycle_calls["validate"] + 1),
                stop=lambda version, workspace: (lifecycle_calls.__setitem__("stop", lifecycle_calls["stop"] + 1),
                                                  backend.stop(version, workspace)),
                start=lambda *_: lifecycle_calls.__setitem__("start", lifecycle_calls["start"] + 1),
            )
        monkeypatch.setattr(app_update, "PinokioLifecycle", lifecycle)
        args = ["--update", "--managed-by", "pinokio", "--root", str(backend.root)]
    else:
        args = ["--update", "--root", str(backend.root)]

    pointer_before = (backend.root / "current").read_bytes()
    journal_before = journal.read_bytes()
    backup_before = _backup_bytes(backup)
    realm_before = (realm / "realm.sqlite3").read_bytes()
    selection_before = backend.service.selection.read_bytes()
    assert cli.main([
        *args, "--uv", str(tmp_path / "not-needed-uv"),
        "--manifest", str(tmp_path / "not-needed-manifest"),
    ]) == 1
    _assert_restore_refusal_unchanged(
        backend, paths, realm, boundary, manager_calls, backup, journal,
        pointer_before, journal_before, backup_before, realm_before)
    assert backend.service.selection.read_bytes() == selection_before
    backend.restore.assert_not_called()
    backend.register.assert_not_called()
    backend.start.assert_not_called()
    backend.verify.assert_not_called()
    assert lifecycle_calls["start"] == 0
    if managed_by == "pinokio":
        # The real public Pinokio recovery must reach Backend.stop/C1; the
        # C1 manager callback and lifecycle start remain forbidden on refusal.
        assert lifecycle_calls["stop"] == (0 if case == "malformed_selection" else 1)
        # Pinokio owns its manager, so Backend.stop never probes the OS manager.
        backend.service.validate_stop_target.assert_not_called()


@pytest.mark.parametrize("case", ["busy", "foreign_realm", "wrong_root", "malformed_selection",
                                  "publication", "unknown_owner", "pid_reused"])
def test_public_installer_update_refuses_readable_restore_gap_without_mutations(
        tmp_path, monkeypatch, case):
    _run_public_restore_refusal(tmp_path, monkeypatch, managed_by=None, case=case)


@pytest.mark.parametrize("case", ["busy", "foreign_realm", "wrong_root", "malformed_selection",
                                  "publication", "unknown_owner", "pid_reused"])
def test_pinokio_public_update_refuses_readable_restore_gap_without_mutations(
        tmp_path, monkeypatch, case):
    _run_public_restore_refusal(tmp_path, monkeypatch, managed_by="pinokio", case=case)


@pytest.mark.parametrize("journal_state", ["missing", "linked"])
def test_active_restore_refuses_missing_or_linked_disk_journal_before_legacy_fallback(
        tmp_path, monkeypatch, journal_state):
    from astrid.installer.core import RuntimeOperationError

    backend, data, paths, realm, boundary, manager_calls, _old, _candidate, backup, journal = (
        _readable_restore_gap(tmp_path, monkeypatch))
    active_transaction = read_json(journal)
    journal_bytes = journal.read_bytes()
    pointer_bytes = (backend.root / "current").read_bytes()
    backup_before = _backup_bytes(backup)
    backend._update_transaction = (active_transaction, True)
    if journal_state == "missing":
        journal.unlink()
    else:
        target = tmp_path / "linked-update-transaction.json"
        target.write_bytes(journal_bytes)
        journal.unlink()
        journal.symlink_to(target)

    with pytest.raises(RuntimeOperationError) as raised:
        backend.stop(None, data)
    assert not core.is_lifecycle_refusal(raised.value)
    assert boundary.stop_calls == 0
    assert manager_calls == []
    backend.service.disable.assert_not_called()
    backend.service.stop.assert_not_called()
    assert (backend.root / "current").read_bytes() == pointer_bytes
    assert _backup_bytes(backup) == backup_before
    assert not paths.catalog_path.exists()
    assert (realm / "realm.sqlite3").is_file()
    if journal_state == "missing":
        assert not journal.exists()
    else:
        assert journal.is_symlink()
        assert journal.readlink() == target


def test_pinokio_public_update_recovers_idle_missing_catalog_with_real_backend_c1_and_restore(
        tmp_path, monkeypatch):
    import shutil

    backend, data, paths, realm, boundary, manager_calls, old, _candidate, backup, journal = (
        _readable_restore_gap(tmp_path, monkeypatch, managed_by="pinokio"))
    (realm / "restore-sentinel.txt").write_text("old workspace", encoding="utf-8")
    shutil.copytree(realm, backup / "realm")
    (realm / "restore-sentinel.txt").write_text("candidate workspace", encoding="utf-8")
    lifecycle_calls = {"validate": 0, "stop": 0, "start": 0}

    def lifecycle(_root, _backend):
        def start(version, workspace, realm_id):
            lifecycle_calls["start"] += 1
            assert version.release == old.release
            assert workspace == data and realm_id == "realm-1"
            assert (realm / "restore-sentinel.txt").read_text(encoding="utf-8") == "old workspace"
            catalog = read_json(paths.catalog_path)
            assert catalog["selected_realm_id"] == "realm-1"

        return SimpleNamespace(
            validate=lambda *_: lifecycle_calls.__setitem__("validate", lifecycle_calls["validate"] + 1),
            stop=lambda version, workspace: (lifecycle_calls.__setitem__("stop", lifecycle_calls["stop"] + 1),
                                              backend.stop(version, workspace)),
            start=start,
        )

    from runtime_protocol import platform
    monkeypatch.setattr(cli, "Backend", lambda *_args, **_kwargs: backend)
    monkeypatch.setattr(platform, "log_dir", lambda: tmp_path / "logs")
    monkeypatch.setattr(app_update, "PinokioLifecycle", lifecycle)
    assert cli.main([
        "--update", "--managed-by", "pinokio", "--root", str(backend.root),
        "--uv", str(tmp_path / "not-needed-uv"),
        "--manifest", str(tmp_path / "not-needed-manifest"),
    ]) == 0

    assert lifecycle_calls == {"validate": 1, "stop": 1, "start": 1}
    assert boundary.stop_calls == 0  # No published Runtime owner needed a signal.
    assert manager_calls == []
    assert read_json(paths.catalog_path)["selected_realm_id"] == "realm-1"
    assert backend._restore_catalog_writes == [paths.catalog_path, paths.catalog_path]
    assert (realm / "restore-sentinel.txt").read_text(encoding="utf-8") == "old workspace"
    assert current(backend.root) == old
    final_journal = read_json(journal)
    assert final_journal["step"] == "rolled_back"
    assert "stop_authority" not in final_journal
    assert backup.is_dir() and (backup / "realm" / "restore-sentinel.txt").read_text(encoding="utf-8") == "old workspace"
