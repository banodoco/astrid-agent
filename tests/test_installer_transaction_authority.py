"""A Runtime-authorized stop survives controller death and public CLI replay."""

import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from runtime_protocol import platform
from runtime_protocol.upgrade import inspect_canonical_schema

from astrid.installer import app_update, cli
from astrid.installer.backend import Backend
from astrid.installer.core import Installer, Version, current, read_json, switch, write_json
from astrid.installer.service import LABEL, installer_lock_path
from tests.test_installer_lifecycle import _installer_runtime, _publish_owner


class ControllerDied(BaseException):
    """Model process death without entering the updater's exception rollback."""


def _stopped_transaction(tmp_path, monkeypatch, kind):
    """Retain a real Backend/C1 stop, then discard the controller."""
    from astrid.installer.core import transaction_stop_context

    backend, data, paths, realm, boundary, _calls = _installer_runtime(tmp_path, monkeypatch)
    root = backend.root
    manager = "pinokio" if kind == "app" else None
    backend.managed_by = manager
    folder = root / "versions" / "old"
    folder.mkdir(parents=True)
    old = Version(folder, Path(sys.executable), folder / "profile.json", {}, 17, manager)
    write_json(folder / "version.json", old.document())
    switch(root, old)
    record = {"owner": LABEL, "program_root": str(root), "data_root": str(data),
              "managed_by": manager}
    write_json(root / "install.json", record)
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

    _publish_owner(paths)
    with transaction_stop_context(backend, transaction, save):
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
        boundary.process_birth_identity = lambda pid: identity["process_birth_id"] if pid == identity["pid"] else None
        boundary.endpoint_metadata = lambda **_: {
            "runtime_instance_id": identity["runtime_instance_id"], "realm_id": identity["active_realm"]}

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
    with pytest.raises(RuntimeOperationError) as raised:
        recover()
    assert raised.value.stop_refused
    assert boundary.stop_calls == 0
    for path in tracked:
        assert path.read_bytes() == before[path], path
    for name in ("restore", "register", "start", "verify", "persist_identity"):
        getattr(backend, name).assert_not_called()
    backend.service.disable.assert_not_called()
    backend.service.stop.assert_not_called()
    assert {path: path.read_bytes() for path in published} == published


@pytest.mark.parametrize("kind", ["migration", "app"])
def test_fresh_backend_accepts_exact_json_authority_with_real_c1_live_checks(
        tmp_path, monkeypatch, kind):
    backend, paths, _realm, boundary, _transaction, recover = _stopped_transaction(
        tmp_path, monkeypatch, kind)
    _publish_owner(paths)
    boundary.live = True
    recover()
    assert boundary.stop_calls == 1
    assert read_json(backend.root / "update-transaction.json")["step"] == "rolled_back"
    backend.start.assert_called_once()


@pytest.mark.parametrize("kind", ["migration", "app"])
@pytest.mark.parametrize("binding", [
    "valid", "missing-intent", "wrong-operation", "unauthorized-version",
    "wrong-owner-identity", "publication-only", "stale-binding",
])
def test_recovery_requires_authorized_launch_intent_and_live_bound_identity(
        tmp_path, monkeypatch, kind, binding):
    from astrid.installer.core import RuntimeOperationError

    backend, paths, realm, boundary, transaction, recover = _stopped_transaction(
        tmp_path, monkeypatch, kind)
    operation_id = transaction["operation_id"]
    intent = {"operation_id": operation_id, "purpose": "pinokio_start" if kind == "app" else "service_start",
              "version": transaction["old"]}
    if binding == "wrong-operation":
        intent["operation_id"] = "f" * 32
    elif binding == "unauthorized-version":
        version = Version.load(transaction["old"])
        intent["version"] = Version(
            version.root, version.python, version.profile, version.env, 99, version.managed_by,
        ).document()
    if binding != "missing-intent":
        transaction["launch_intent"] = intent
    write_json(backend.root / "update-transaction.json", transaction)
    tracked = [backend.root / "update-transaction.json", backend.root / "current",
               backend.service.selection, realm / "realm.sqlite3"]
    before = {path: path.read_bytes() for path in tracked}
    publication = _publish_owner(paths, pid=888, birth="transaction-child-birth")
    publication.update(runtime_instance_id="transaction-child", installer_operation_id=(
        "f" * 32 if binding == "stale-binding" else operation_id))
    write_json(paths.discovery_path, publication)
    write_json(paths.instance_lock_path, {**publication, "realm_id": "realm-1"})
    boundary.live = True
    boundary.process_birth_identity = lambda pid: "transaction-child-birth" if pid == 888 else None
    metadata = {
        "runtime_instance_id": "transaction-child", "realm_id": "realm-1",
        "identity": {"release": 99 if binding == "wrong-owner-identity" else 17,
                     "managed_by": "pinokio" if kind == "app" else None},
    }
    if binding != "publication-only":
        metadata["installer_operation_id"] = publication["installer_operation_id"]
    boundary.endpoint_metadata = lambda **_: metadata.copy()
    if binding == "valid":
        recover()
        assert boundary.stop_calls == 1
        assert read_json(backend.root / "update-transaction.json")["step"] == "rolled_back"
        backend.start.assert_called_once()
    else:
        with pytest.raises((RuntimeOperationError, ValueError)) as raised:
            recover()
        if isinstance(raised.value, RuntimeOperationError):
            assert raised.value.stop_refused
        else:
            assert "launch intent is not authorized" in str(raised.value)
        assert boundary.stop_calls == 0
        for path in tracked:
            assert path.read_bytes() == before[path], path
        backend.service.disable.assert_not_called()
        backend.service.stop.assert_not_called()
        backend.restore.assert_not_called()
        backend.start.assert_not_called()


def _stale_publication_from_authority(authority, paths):
    """Create the two public files for the exact incarnation retained by C1."""
    discovery = {
        "pid": authority["pid"],
        "process_birth_id": authority["process_birth_id"],
        "runtime_instance_id": authority["runtime_instance_id"],
        "active_realm": authority["realm_id"],
        "realm_root": authority["realm_root"],
        "endpoint": authority.get("endpoint", "http://127.0.0.1:7777"),
    }
    marker = {**discovery, "realm_id": authority["realm_id"]}
    write_json(paths.discovery_path, discovery)
    write_json(paths.instance_lock_path, marker)
    return discovery, marker


@pytest.mark.parametrize("kind", ["migration", "app"])
@pytest.mark.parametrize("layout", ["discovery_and_lock", "lock_only"])
def test_recovery_accepts_exact_retained_stale_publication_after_repeated_absence(
        tmp_path, monkeypatch, kind, layout):
    backend, paths, realm, boundary, transaction, recover = _stopped_transaction(
        tmp_path, monkeypatch, kind)
    authority = read_json(backend.root / "update-transaction.json")["stop_authority"]
    _stale_publication_from_authority(authority, paths)
    if layout == "lock_only":
        paths.discovery_path.unlink()
    boundary.live = False
    recover()

    assert boundary.stop_calls == 0
    assert read_json(backend.root / "update-transaction.json")["step"] == "rolled_back"
    assert read_json(backend.root / "update-transaction.json")["stop_authority"] == authority
    assert backend.start.call_count == 1


@pytest.mark.parametrize("kind", ["migration", "app"])
@pytest.mark.parametrize("layout", ["discovery_and_lock", "lock_only"])
@pytest.mark.parametrize("alteration", ["pid", "birth", "instance", "realm", "root", "copied_operation"])
def test_recovery_refuses_unrelated_stale_publication_even_with_copied_operation_tag(
        tmp_path, monkeypatch, kind, layout, alteration):
    from astrid.installer.core import RuntimeOperationError

    backend, paths, realm, boundary, transaction, recover = _stopped_transaction(
        tmp_path, monkeypatch, kind)
    journal = backend.root / "update-transaction.json"
    retained = read_json(journal)
    operation_id = retained["operation_id"]
    backup_root = backend.root / "backups" / "retained"
    backup_root.mkdir(parents=True)
    backup_file = backup_root / "sentinel.bin"
    backup_file.write_bytes(b"retained recovery backup")
    retained["backup"] = {"root": str(backup_root), "kind": "test"}
    intent = {"operation_id": operation_id,
              "purpose": "pinokio_start" if kind == "app" else "service_start",
              "version": retained["old"]}
    retained["launch_intent"] = intent
    write_json(journal, retained)
    authority = retained["stop_authority"]
    discovery, marker = _stale_publication_from_authority(authority, paths)
    changed = {
        "pid": ("pid", 888), "birth": ("process_birth_id", "replacement-birth"),
        "instance": ("runtime_instance_id", "replacement-instance"),
        "realm": ("active_realm", "replacement-realm"),
        "root": ("realm_root", str(tmp_path / "replacement-realm")),
        "copied_operation": ("pid", 888),
    }
    key, value = changed[alteration]
    discovery[key] = value
    marker[key] = value
    if key == "active_realm":
        marker["realm_id"] = value
    if alteration == "copied_operation":
        # The replacement copied the serialized operation tag. Its endpoint
        # is unavailable, so the tag cannot authorize a different process.
        discovery["installer_operation_id"] = operation_id
        marker["installer_operation_id"] = operation_id
    write_json(paths.discovery_path, discovery)
    write_json(paths.instance_lock_path, marker)
    if layout == "lock_only":
        paths.discovery_path.unlink()
    boundary.live = False
    tracked = [journal, backend.root / "current", backend.service.selection,
               realm / "realm.sqlite3", paths.discovery_path, paths.instance_lock_path, backup_file]
    before = {path: path.read_bytes() if path.exists() else None for path in tracked}

    with pytest.raises(RuntimeOperationError) as raised:
        recover()

    assert raised.value.stop_refused
    assert boundary.stop_calls == 0
    assert read_json(journal) == retained
    for path in tracked:
        assert (path.read_bytes() if path.exists() else None) == before[path]
    for name in ("restore", "register", "start", "verify", "persist_identity"):
        getattr(backend, name).assert_not_called()
    backend.service.disable.assert_not_called()
    backend.service.stop.assert_not_called()


@pytest.mark.parametrize("kind", ["migration", "app"])
@pytest.mark.parametrize("foreground", ["matching", "wrong_pid", "wrong_birth", "wrong_realm", "wrong_realm_root"])
def test_recovery_reconciles_foreground_with_admitted_live_owner(
        tmp_path, monkeypatch, kind, foreground):
    from astrid.installer.core import RuntimeOperationError

    backend, paths, realm, boundary, transaction, recover = _stopped_transaction(
        tmp_path, monkeypatch, kind)
    operation_id = transaction["operation_id"]
    transaction["launch_intent"] = {
        "operation_id": operation_id,
        "purpose": "pinokio_start" if kind == "app" else "service_start",
        "version": transaction["old"],
    }
    write_json(backend.root / "update-transaction.json", transaction)
    publication = _publish_owner(paths, pid=888, birth="activated-owner-birth")
    publication["runtime_instance_id"] = "activated-owner-instance"
    publication["installer_operation_id"] = operation_id
    write_json(paths.discovery_path, publication)
    write_json(paths.instance_lock_path, {**publication, "realm_id": "realm-1"})
    boundary.live = True
    boundary.process_birth_identity = lambda pid: "activated-owner-birth" if pid == 888 else None
    boundary.endpoint_metadata = lambda **_: {
        "runtime_instance_id": "activated-owner-instance", "realm_id": "realm-1",
        "installer_operation_id": operation_id,
        "identity": {"release": 17, "managed_by": "pinokio" if kind == "app" else None},
    }
    foreground_record = {
        "pid": 888, "birth_id": "activated-owner-birth", "realm_id": "realm-1",
        "realm_root": str(realm),
    }
    if foreground == "wrong_pid":
        foreground_record["pid"] = 889
    elif foreground == "wrong_birth":
        foreground_record["birth_id"] = "unrelated-birth"
    elif foreground == "wrong_realm":
        foreground_record["realm_id"] = "another-realm"
    elif foreground == "wrong_realm_root":
        foreground_record["realm_root"] = str(tmp_path / "another-realm")
    selection = read_json(backend.service.selection)
    selection["foreground"] = foreground_record
    write_json(backend.service.selection, selection)
    journal = backend.root / "update-transaction.json"
    before = {path: path.read_bytes() for path in (
        journal, backend.root / "current", backend.service.selection, realm / "realm.sqlite3",
    )}

    if foreground == "matching":
        recover()
        assert boundary.stop_calls == 1
        assert read_json(journal)["step"] == "rolled_back"
        return

    with pytest.raises((RuntimeOperationError, ValueError)):
        recover()
    assert boundary.stop_calls == 0
    for path, contents in before.items():
        assert path.read_bytes() == contents
    backend.restore.assert_not_called()
    backend.start.assert_not_called()
    backend.service.disable.assert_not_called()
    backend.service.stop.assert_not_called()


@pytest.mark.parametrize("kind", ["migration", "app"])
def test_recovery_refuses_foreground_without_runtime_custody(tmp_path, monkeypatch, kind):
    from astrid.installer.core import RuntimeOperationError

    backend, paths, realm, boundary, _transaction, recover = _stopped_transaction(
        tmp_path, monkeypatch, kind)
    boundary.live = False
    authority = read_json(backend.root / "update-transaction.json")["stop_authority"]
    boundary.live = True
    boundary.process_birth_identity = lambda pid: (
        authority["process_birth_id"] if pid == authority["pid"] else None
    )
    selection = read_json(backend.service.selection)
    selection["foreground"] = {
        "pid": authority["pid"], "birth_id": authority["process_birth_id"],
        "realm_id": authority["realm_id"], "realm_root": authority["realm_root"],
    }
    write_json(backend.service.selection, selection)
    tracked = [backend.root / "update-transaction.json", backend.root / "current",
               backend.service.selection, realm / "realm.sqlite3"]
    before = {path: path.read_bytes() for path in tracked}

    with pytest.raises((RuntimeOperationError, ValueError)):
        recover()

    assert boundary.stop_calls == 0
    for path in tracked:
        assert path.read_bytes() == before[path]
    backend.restore.assert_not_called()
    backend.start.assert_not_called()
    backend.service.disable.assert_not_called()
    backend.service.stop.assert_not_called()


@pytest.mark.parametrize("kind", ["migration", "app"])
@pytest.mark.parametrize("lookup", ["pid_reuse", "uncertain"])
def test_recovery_refuses_stale_publication_when_process_absence_is_not_proven(
        tmp_path, monkeypatch, kind, lookup):
    from astrid.installer.core import RuntimeOperationError

    backend, paths, realm, boundary, _transaction, recover = _stopped_transaction(
        tmp_path, monkeypatch, kind)
    journal = backend.root / "update-transaction.json"
    authority = read_json(journal)["stop_authority"]
    _stale_publication_from_authority(authority, paths)
    boundary.live = False
    if lookup == "pid_reuse":
        boundary.process_birth_identity = lambda pid: "reused-pid-birth" if pid == authority["pid"] else None
    else:
        def uncertain(_pid):
            raise OSError("process identity lookup unavailable")
        boundary.process_birth_identity = uncertain
    tracked = [journal, backend.root / "current", backend.service.selection,
               realm / "realm.sqlite3", paths.discovery_path, paths.instance_lock_path]
    before = {path: path.read_bytes() for path in tracked}

    with pytest.raises((RuntimeOperationError, ValueError)):
        recover()

    assert boundary.stop_calls == 0
    for path in tracked:
        assert path.read_bytes() == before[path]
    backend.restore.assert_not_called()
    backend.start.assert_not_called()
    backend.service.disable.assert_not_called()
    backend.service.stop.assert_not_called()


@pytest.mark.parametrize("kind", ["migration", "app"])
def test_recovery_rechecks_exact_stale_pid_absence_at_final_fence(tmp_path, monkeypatch, kind):
    import importlib
    from contextlib import contextmanager

    from astrid.installer.core import RuntimeOperationError

    backend, paths, realm, boundary, _transaction, recover = _stopped_transaction(
        tmp_path, monkeypatch, kind)
    journal = backend.root / "update-transaction.json"
    authority = read_json(journal)["stop_authority"]
    _stale_publication_from_authority(authority, paths)
    boundary.live = False
    birth = [None]
    boundary.process_birth_identity = lambda pid: birth[0] if pid == authority["pid"] else None
    bootstrap = importlib.import_module("astrid_runtime.bootstrap")
    original_fence = bootstrap.interruption_fence

    @contextmanager
    def pid_reused_at_final_fence(*args, **kwargs):
        with original_fence(*args, **kwargs) as result:
            birth[0] = "reused-pid-birth"
            boundary.live = True
            yield result

    monkeypatch.setattr(bootstrap, "interruption_fence", pid_reused_at_final_fence)
    tracked = [journal, backend.root / "current", backend.service.selection,
               realm / "realm.sqlite3", paths.discovery_path, paths.instance_lock_path]
    before = {path: path.read_bytes() for path in tracked}

    with pytest.raises(RuntimeOperationError) as raised:
        recover()

    assert raised.value.stop_refused
    assert boundary.stop_calls == 0
    for path in tracked:
        assert path.read_bytes() == before[path]
    backend.restore.assert_not_called()
    backend.start.assert_not_called()
    backend.service.disable.assert_not_called()
    backend.service.stop.assert_not_called()


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
    assert retained["stop_authority"]["pid"] == 777
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
    fresh.preflight_stop = Mock(side_effect=AssertionError("mutable preflight before recovery"))
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

    from astrid.installer.core import RuntimeOperationError, transaction_stop_context

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

    with transaction_stop_context(backend, transaction, save):
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
                _publish_owner(paths, pid=888, birth="replacement-birth")
                yield

        monkeypatch.setattr(bootstrap, "_bootstrap_mutex", replacement_before_final_admission)
    with pytest.raises(RuntimeOperationError) as raised:
        Installer(root, data, fresh, say=lambda _: None)._recover_update()
    assert raised.value.stop_refused
    if refusal == "busy":
        assert raised.value.details["blockers"][0]["task_id"] == "busy-1"
    else:
        assert "replacement owner publication" in str(raised.value)
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

    from astrid.installer.core import transaction_stop_context

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
        with transaction_stop_context(backend, transaction, save):
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
