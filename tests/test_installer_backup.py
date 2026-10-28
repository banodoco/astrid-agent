"""Update backups reserve disk space and preserve the logs of a failed update."""

import os
import shutil
import tempfile
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from astrid.installer.backend import Backend
from astrid.installer.core import (
    BackupSpaceError,
    Installer,
    Version,
    current,
    read_json,
    switch,
    write_json,
)


def workspace(tmp_path, system):
    program, data = tmp_path / "program", tmp_path / "support"
    realm = data / "runtime/realms/selected"
    realm.mkdir(parents=True)
    (realm / "realm.sqlite3").write_bytes(b"selected database")
    (realm / "media").write_bytes(b"selected media")
    write_json(data / "runtime/catalog.json", {
        "version": 1,
        "selected_realm_id": "selected",
        "realms": [{"realm_id": "selected", "display_name": "Selected workspace", "data_root": str(realm)}],
        "source_profiles": {},
    })
    write_json(data / "runtime/discovery.json", {"transient": True})
    (data / "runtime/instance.lock").write_bytes(b"live lock")
    (data / "runtime/bootstrap.lock").write_bytes(b"bootstrap lock")
    other = data / "runtime/realms/other"
    other.mkdir()
    (other / "excluded").write_bytes(b"unselected media" * 100)
    logs = data / "logs"
    logs.mkdir()
    (logs / "install.log").write_text("before the update\n")
    (logs / "runtime-service.log").write_text("service output\n")
    backend = Backend(program, logs / "install.log", tmp_path / "uv")
    backend.service.system = system
    version = Version(program / "versions/old", tmp_path / "python", tmp_path / "profile", {})

    def runtime(_version, _data, *args, **_kwargs):
        if args[0] == "backup":
            shutil.copytree(realm, Path(args[2]))
        return {}

    backend.runtime = Mock(side_effect=runtime)
    backend.stop = Mock()
    return backend, data, realm, version


@pytest.mark.parametrize("system", ["linux", "win32"])
def test_full_backup_checks_realm_support_and_margin_before_starting(tmp_path, monkeypatch, system):
    backend, data, realm, version = workspace(tmp_path, system)
    selected_size = sum(path.stat().st_size for path in realm.iterdir())
    support_size = (data / "runtime/catalog.json").stat().st_size
    assert backend._backup_size(data, support=True) == support_size
    required = selected_size + support_size + 256 * 1024 * 1024
    usage = Mock(return_value=SimpleNamespace(free=required - 1))
    monkeypatch.setattr(shutil, "disk_usage", usage)

    with pytest.raises(BackupSpaceError) as failure:
        backend.backup(version, data, {"realm_root": str(realm)})

    assert str(failure.value) == "There is not enough free disk space to back up your workspace."
    usage.assert_called_once_with(backend.root)
    backend.runtime.assert_not_called()
    backend.stop.assert_not_called()
    assert list((backend.root / "backups").iterdir()) == []
    assert (data / "logs/install.log").read_text() == "before the update\n"

    usage.return_value.free = required
    backup = backend.backup(version, data, {"realm_root": str(realm)})
    snapshot = Path(backup["root"]) / "support"
    assert backend._backup_size(snapshot) == support_size
    assert not (snapshot / "logs").exists()
    assert not (snapshot / "runtime/realms").exists()
    assert not (snapshot / "runtime/discovery.json").exists()


def test_large_backup_reserves_ten_percent_when_larger_than_minimum_margin(tmp_path, monkeypatch):
    backend, data, realm, version = workspace(tmp_path, "linux")
    size = 4 * 1024 * 1024 * 1024
    monkeypatch.setattr(backend, "_backup_size", lambda path, **_kwargs: size if path == realm else 0)
    monkeypatch.setattr(shutil, "disk_usage", lambda _path: SimpleNamespace(free=size + size // 10 - 1))

    with pytest.raises(BackupSpaceError):
        backend.backup(version, data, {"realm_root": str(realm)})

    backend.runtime.assert_not_called()


@pytest.mark.parametrize("step", ["up", "backup", "stop", "support", "metadata", "cancel"])
def test_failed_full_backup_removes_its_partial_snapshot(tmp_path, monkeypatch, step):
    import astrid.installer.backend as module

    backend, data, realm, version = workspace(tmp_path, "linux")
    original_runtime, original_copy = backend.runtime, backend._copy
    failure = KeyboardInterrupt() if step == "cancel" else OSError("backup failed")

    def runtime(*args, **kwargs):
        operation = args[2]
        if operation == step or (operation == "backup" and step == "cancel"):
            (next((backend.root / "backups").iterdir()) / "partial").write_bytes(b"partial data")
            raise failure
        return original_runtime(*args, **kwargs)

    backend.runtime = runtime
    if step == "stop":
        backend.stop = Mock(side_effect=failure)

    def copy(source, destination, **kwargs):
        original_copy(source, destination, **kwargs)
        if step == "support":
            raise failure

    monkeypatch.setattr(backend, "_copy", copy)
    if step == "metadata":
        monkeypatch.setattr(module, "write_json", Mock(side_effect=failure))

    with pytest.raises(type(failure)):
        backend.backup(version, data, {"realm_root": str(realm)})

    assert list((backend.root / "backups").iterdir()) == []
    assert (data / "logs/install.log").read_text() == "before the update\n"


def test_failed_clone_backup_removes_its_partial_snapshot(tmp_path, monkeypatch):
    backend, data, realm, version = workspace(tmp_path, "darwin")

    def clone(source, destination):
        shutil.copy2(source, destination)
        raise OSError("clone failed")

    monkeypatch.setattr("runtime_protocol.fsclone.clone_or_copy_file", clone)

    with pytest.raises(OSError, match="clone failed"):
        backend.backup(version, data, {"realm_root": str(realm)})

    assert list((backend.root / "backups").iterdir()) == []
    backend.runtime.assert_not_called()


def test_rollback_preserves_live_logs_even_in_an_older_snapshot(tmp_path, monkeypatch):
    backend, data, realm, version = workspace(tmp_path, "darwin")
    monkeypatch.setattr("runtime_protocol.fsclone.clone_or_copy_file", shutil.copy2)
    identity = {"realm_id": "selected", "realm_root": str(realm)}
    backup = backend.backup(version, data, identity)
    snapshot = Path(backup["root"]) / "support"
    assert not (snapshot / "logs").exists()
    shutil.copytree(data / "logs", snapshot / "logs")  # Previous installer snapshots included logs.
    (realm / "realm.sqlite3").write_bytes(b"failed migration")
    (data / "logs/install.log").write_text("the update failed here\n")
    (data / "logs/runtime-service.log").write_text("live shutdown output\n")
    remove = shutil.rmtree

    def remove_except_logs(path, *args, **kwargs):
        assert Path(path) != data / "logs", "the rollback must leave open Windows log handles alone"
        return remove(path, *args, **kwargs)

    monkeypatch.setattr(shutil, "rmtree", remove_except_logs)
    lines = []
    backend.say = lines.append
    backend.restore(version, data, identity, backup)
    failed_line = next(line for line in lines if "failed update's workspace copy" in line)
    failed_copy = Path(failed_line.removeprefix("The failed update's workspace copy is kept at ").removesuffix(
        ". You can delete it once Astrid works."
    ))
    assert failed_copy.is_dir()
    assert (failed_copy / "realm.sqlite3").read_bytes() == b"failed migration"
    assert (realm / "realm.sqlite3").read_bytes() == b"selected database"
    assert (data / "logs/install.log").read_text() == "the update failed here\n"
    assert (data / "logs/runtime-service.log").read_text() == "live shutdown output\n"
    assert (data / "runtime/realms/other/excluded").is_file()


def test_backup_size_propagates_unreadable_directory_errors(tmp_path, monkeypatch):
    backend, data, _realm, _version = workspace(tmp_path, "linux")

    def unreadable(_source, *, onerror):
        onerror(PermissionError("cannot read workspace"))
        return iter(())

    monkeypatch.setattr("astrid.installer.backend.os.walk", unreadable)

    with pytest.raises(PermissionError, match="cannot read workspace"):
        backend._backup_size(data, support=True)


@pytest.mark.posix_native
def test_support_rollback_preserves_posix_file_and_directory_modes(tmp_path, monkeypatch):
    backend, data, realm, version = workspace(tmp_path, "darwin")
    tool = data / "helpers/run"
    tool.parent.mkdir(mode=0o750)
    tool.write_bytes(b"#!/bin/sh\nexit 0\n")
    tool.chmod(0o750)
    monkeypatch.setattr("runtime_protocol.fsclone.clone_or_copy_file", shutil.copy2)
    identity = {"realm_id": "selected", "realm_root": str(realm)}
    backup = backend.backup(version, data, identity)

    backend.restore(version, data, identity, backup)

    assert tool.stat().st_mode & 0o777 == 0o750
    assert tool.parent.stat().st_mode & 0o777 == 0o750


@pytest.fixture
def windows_permissions(monkeypatch):
    from runtime_protocol import platform, windows_security

    # Model protected DACLs separately from POSIX modes: copy2/copytree do not
    # preserve them on Windows. Existing directories must still be validated.
    protected = set()

    def validate(path):
        if Path(path) not in protected:
            raise PermissionError(f"unprotected existing DACL: {path}")

    def private_directory(path):
        path = Path(path)
        if path.exists():
            validate(path)
        else:
            path.mkdir(parents=True)
            protected.add(path)

    def read_private(path):
        path = Path(path)
        validate(path.parent)
        validate(path)
        return path.read_bytes()

    remove_tree, unlink = shutil.rmtree, Path.unlink

    def remove_directory(path, *args, **kwargs):
        remove_tree(path, *args, **kwargs)
        protected.difference_update({p for p in protected if p == path or Path(path) in p.parents})

    def remove_file(path, *args, **kwargs):
        unlink(path, *args, **kwargs)
        protected.discard(path)

    def private_temporary(directory, prefix):
        descriptor, name = tempfile.mkstemp(dir=directory, prefix=prefix)
        protected.add(Path(name))
        return descriptor, name

    def replace(source, destination):
        os.replace(source, destination)
        if Path(source) in protected:
            protected.discard(Path(source))
            protected.add(Path(destination))

    monkeypatch.setattr(platform, "sys", SimpleNamespace(platform="win32"))
    monkeypatch.setattr(platform, "_move_file_write_through", replace)
    monkeypatch.setattr(platform, "private_temporary", private_temporary)
    monkeypatch.setattr(windows_security, "private_directory", private_directory)
    monkeypatch.setattr(windows_security, "restrict", lambda path: protected.add(Path(path)))
    monkeypatch.setattr(windows_security, "read_private", read_private)
    monkeypatch.setattr(shutil, "rmtree", remove_directory)
    monkeypatch.setattr(Path, "unlink", remove_file)
    return protected


def test_windows_failed_update_restores_private_support_and_can_retry(
    tmp_path, windows_permissions, monkeypatch,
):
    from astrid_runtime.paths import RuntimePaths
    from runtime_protocol import platform

    backend, data, realm, _version = workspace(tmp_path, "win32")
    identity = {"realm_id": "selected", "realm_root": str(realm)}
    retained = {
        data / "credentials/selected.json": b"workspace credential",
        data / "runtime/backup-auth.key": b"backup key",
        data / "runtime/source-profiles/astrid.json": b"source profile",
    }
    for path, payload in retained.items():
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(payload)

    def version(name):
        folder = backend.root / "versions" / name
        python = folder / "py/Scripts/python.exe"
        python.parent.mkdir(parents=True)
        python.touch()
        value = Version(folder, python, folder / "profile.json", {})
        write_json(folder / "version.json", value.document())
        return value

    old, failed, retry = (version(name) for name in ("old", "failed", "retry"))
    switch(backend.root, old)
    windows_permissions.update({tmp_path, *tmp_path.rglob("*")})
    backend.workspace = Mock(return_value=identity)
    backend.stage = Mock(side_effect=[failed, retry])
    # These candidate interpreters are placeholders. Admission and file-lock
    # semantics have their own tests; retain real backup/restore and DACL checks.
    backend.validate_candidate = Mock()
    monkeypatch.setattr(platform, "lock_file", Mock())
    monkeypatch.setattr(platform, "unlock_file", Mock())
    backend.register = Mock()
    backend.finish = Mock()
    backend.open = Mock()
    # The launch-admission script is published through PowerShell on Windows; this
    # POSIX host cannot run it. Its own tests cover the script.
    backend.publish_launch_admission = Mock()

    def restore_realm(argv, **_kwargs):
        # Only the offline realm restore subprocess is faked. Support rollback
        # and the transaction below use the real installer implementations.
        shutil.copytree(Path(argv[-3]), Path(argv[-2]))

    backend.run = Mock(side_effect=restore_realm)

    def upgrade(candidate, _data):
        if candidate == failed:
            (realm / "realm.sqlite3").write_bytes(b"failed migration")
            for path in retained:
                path.write_bytes(b"candidate state")
            raise RuntimeError("candidate upgrade failed")

    backend.upgrade = Mock(side_effect=upgrade)

    def start(_version, _data):
        RuntimePaths.current(data_root=data).ensure_support_dirs()
        for path, payload in retained.items():
            assert platform.read_private_file(path) == payload

    backend.start = Mock(side_effect=start)
    backend.verify = Mock()
    lines = []
    backend.say = lines.append
    installer = Installer(backend.root, data, backend, say=lambda _line: None)

    with pytest.raises(RuntimeError, match="^candidate upgrade failed$"):
        installer.run("update", {})

    assert current(backend.root) == old
    failed_line = next(line for line in lines if "failed update's workspace copy" in line)
    failed_copy = Path(failed_line.removeprefix("The failed update's workspace copy is kept at ").removesuffix(
        ". You can delete it once Astrid works."
    ))
    assert failed_copy.is_dir()
    assert (failed_copy / "realm.sqlite3").read_bytes() == b"failed migration"
    assert read_json(backend.root / "update-transaction.json")["step"] == "rolled_back"
    backend.start.assert_called_once_with(old, data)
    assert (realm / "realm.sqlite3").read_bytes() == b"selected database"
    assert (data / "runtime/realms/other/excluded").is_file()
    assert (data / "logs/install.log").read_text() == "before the update\n"

    installer.run("update", {})

    assert current(backend.root) == retry
    assert read_json(backend.root / "update-transaction.json")["step"] == "complete"
    assert backend.start.call_args_list[-1].args == (retry, data)
    backend.verify.assert_any_call(old, data, "selected")
    backend.verify.assert_any_call(retry, data, "selected")


@pytest.mark.parametrize("directory", [".", "runtime"])
def test_windows_restore_does_not_restrict_retained_unsafe_directory(
    tmp_path, windows_permissions, directory,
):
    backend, data, realm, version = workspace(tmp_path, "win32")
    windows_permissions.update({tmp_path, *tmp_path.rglob("*")})
    identity = {"realm_id": "selected", "realm_root": str(realm)}
    backup = backend.backup(version, data, identity)
    backend.run = Mock(side_effect=lambda argv, **_: shutil.copytree(Path(argv[-3]), Path(argv[-2])))
    unsafe = data / directory
    windows_permissions.discard(unsafe)

    with pytest.raises(PermissionError, match="unprotected existing DACL"):
        backend.restore(version, data, identity, backup)

    assert unsafe not in windows_permissions


def test_interrupted_support_file_copy_never_publishes_partial_catalog(tmp_path, monkeypatch):
    from runtime_protocol import platform

    backend, data, realm, version = workspace(tmp_path, "darwin")
    monkeypatch.setattr("runtime_protocol.fsclone.clone_or_copy_file", shutil.copy2)
    identity = {"realm_id": "selected", "realm_root": str(realm)}
    backup = backend.backup(version, data, identity)
    catalog = data / "runtime/catalog.json"
    snapshot_catalog = Path(backup["root"]) / "support/runtime/catalog.json"
    expected = snapshot_catalog.read_bytes()
    durable_replace = platform.durable_replace

    def interrupted_replace(source, target, *args, **kwargs):
        # The catalog is validated, then written through Runtime's atomic JSON
        # publish: the temporary file is complete or absent, and the interruption
        # lands before it can replace the live catalog.
        if Path(target) == catalog:
            raise KeyboardInterrupt("support copy interrupted")
        return durable_replace(source, target, *args, **kwargs)

    monkeypatch.setattr(platform, "durable_replace", interrupted_replace)
    before = catalog.read_bytes()
    with pytest.raises(KeyboardInterrupt, match="support copy interrupted"):
        backend.restore(version, data, identity, backup)

    assert catalog.read_bytes() == before  # The live catalog is never replaced by a partial one.
    assert not list(catalog.parent.glob(".catalog.json.*"))  # The interrupted temporary is removed.
    monkeypatch.setattr(platform, "durable_replace", durable_replace)
    backend.restore(version, data, identity, backup)
    assert catalog.read_bytes() == expected
