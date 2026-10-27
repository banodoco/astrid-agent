"""Real SIGKILLs exercise retained-image rollback across installer/Runtime steps."""

# Integration dependencies must be checked before importing the real backend.
# ruff: noqa: E402

from __future__ import annotations

import os
import re
import signal
import sqlite3
import subprocess
import sys
import uuid
from pathlib import Path

import pytest

pytestmark = [pytest.mark.integration, pytest.mark.linux_native]  # SIGKILL crash evidence; needs the Runtime

from astrid_runtime.bootstrap import BootstrapConfig, SourceProfile
from astrid_runtime.paths import RuntimePaths
from runtime_protocol.store import SCHEMA_VERSION, RealmStore
from runtime_protocol.upgrade import inspect_canonical_schema

from astrid.installer import core
from astrid.installer.backend import Backend
from astrid.installer.core import Installer, Version, current, read_json, switch, write_json


def _event(base, name):
    with (base / "events").open("a", encoding="utf-8") as stream:
        stream.write(name + "\n")
        stream.flush()
        os.fsync(stream.fileno())


def _kill(point, wanted):
    if point == wanted:
        os.kill(os.getpid(), signal.SIGKILL)


def _version(root, name, release=0, managed_by=None):
    folder = root / "versions" / name
    folder.mkdir(parents=True)
    version = Version(folder, Path(sys.executable), folder / "profile.json", {}, release, managed_by)
    write_json(folder / "version.json", version.document())
    return version


def _setup(base, *, previous_schema=True):
    root, data = base / "program", base / "data"
    old = _version(root, "old", release=17)
    switch(root, old)
    write_json(root / "install.json", {"owner": "haus.astrid.runtime", "data_root": str(data)})
    paths = RuntimePaths.current(data_root=data)
    paths.ensure_support_dirs()
    realm = paths.realms_dir / "realm-1"
    RealmStore.initialize(realm, realm_id="realm-1").close()
    if previous_schema:
        with sqlite3.connect(realm / "realm.sqlite3") as connection:
            connection.execute("ALTER TABLE timelines DROP COLUMN name")
            connection.execute("DROP TABLE user_app_state")  # added by schema v31
            connection.execute("UPDATE runtime_schema SET version=29 WHERE id=1")
    import astrid_runtime
    checkout = str(Path(astrid_runtime.__file__).resolve().parents[1])
    profile = SourceProfile(profile="astrid", runtime_checkout=checkout, source_checkout=checkout)
    write_json(paths.catalog_path, {
        "version": 1, "selected_realm_id": "realm-1",
        "realms": [{"realm_id": "realm-1", "display_name": "Astrid", "data_root": str(realm)}],
        "source_profiles": {"astrid": profile.as_dict()},
    })
    write_json(paths.source_profiles_dir / "astrid.json", profile.as_dict())
    credentials = paths.runtime_support / "credentials"
    credentials.mkdir(exist_ok=True)
    (credentials / "owner.token").write_text("test-owner")
    (data / "retained.txt").write_text("original user data")


class _Backend(Backend):
    """Use real copies/restore/migration with deterministic service boundaries."""

    def __init__(self, base, kill_at):
        self.base, self.root, self.kill_at = base, base / "program", kill_at
        self.log = base / "install.log"
        self.say = print
        self.managed_by = None
        from astrid.installer.service import Service

        self.service = Service(self.root, self.log, self.run, home=base / "home", system="linux")
        self.service.selection = base / "install-selection.json"
        if not self.service.selection.exists():
            write_json(self.service.selection, {
                "owner": "haus.astrid.runtime", "program_root": str(self.root),
                "data_root": str(base / "data"), "identity": {"release": 17, "managed_by": None},
                "unrelated": "preserved",
            })

    def existing(self, _data):
        return None

    def has_workspace(self, _data):
        return True

    def reinstallable(self, _data):
        return False

    def workspace(self, _version, data):
        catalog = read_json(data / "runtime" / "catalog.json")
        realm = catalog["realms"][0]
        return {"realm_id": realm["realm_id"], "realm_root": realm["data_root"]}

    def stop(self, _version, data):
        _event(self.base, "stop")
        for name in ("discovery.json", "instance.lock"):
            (data / "runtime" / name).unlink(missing_ok=True)

    def backup(self, _version, data, realm):
        # A new snapshot must never capture the interrupted Runtime journal.
        journal = read_json(data / "runtime" / "upgrade-journal.json")
        assert journal.get("state") in {None, "complete"}, journal
        _event(self.base, "backup")
        destination = self.root / "backups" / uuid.uuid4().hex
        destination.mkdir(parents=True)
        kind = "clone"
        if (self.base / "authenticated").exists():
            from runtime_protocol.backup import create_backup

            store = RealmStore(realm["realm_root"])
            try:
                create_backup(store, destination / "realm", key_path=data / "runtime/backup-auth.key")
            finally:
                store.close()
            kind = "runtime"
        else:
            self._copy(Path(realm["realm_root"]), destination / "realm")
        self._copy(data, destination / "support", support=True)
        result = {"root": str(destination), "kind": kind}
        write_json(destination / "backup.json", result)
        _kill("backup-return", self.kill_at)
        return result

    def stage(self, _manifest, root, _data):
        candidate = _version(root, "new-" + uuid.uuid4().hex[:8], release=18)
        _kill("stage-return", self.kill_at)
        return candidate

    def upgrade(self, _version, _data):
        result = subprocess.run([sys.executable, __file__, "runtime", str(self.base), self.kill_at],
                                capture_output=True, text=True, timeout=30)
        if result.returncode == -signal.SIGKILL:
            # A machine/process-group crash cannot enter core's exception path.
            os.kill(os.getpid(), signal.SIGKILL)
        assert result.returncode == 0, result.stderr

    def restore(self, old, data, realm, backup):
        _event(self.base, "restore")
        super().restore(old, data, realm, backup)
        _kill("restore-return", self.kill_at)

    def register(self, version, _data):
        record = self.service.recorded()
        record.pop("uninstalled", None)
        write_json(self.service.selection, {**record, "identity": {
            "release": version.release, "managed_by": version.managed_by}})
        _event(self.base, "register")

    def start(self, _version, _data):
        _event(self.base, "start")

    def verify(self, _version, data, realm_id, **_kwargs):
        realm = self.workspace(_version, data)
        assert realm["realm_id"] == realm_id == "realm-1"
        assert inspect_canonical_schema(realm["realm_root"])["realm_id"] == realm_id
        assert (data / "retained.txt").read_text() == "original user data"
        _event(self.base, "verify")

    def open(self):
        _event(self.base, "open")


def _run(base, kill_at="", mode="update"):
    env = {**os.environ, "PYTHONPATH": os.pathsep.join([
        str(Path(__file__).resolve().parents[1]), os.environ.get("PYTHONPATH", ""),
    ])}
    result = subprocess.run([sys.executable, __file__, mode, str(base), kill_at],
                            env=env, capture_output=True, text=True, timeout=45)
    return result


def _assert_retry(base, *, restore_expected):
    events = base / "events"
    before = events.read_text().splitlines() if events.exists() else []
    for attempt in range(3):
        result = _run(base)
        assert result.returncode == 0, result.stderr
        if attempt == 0 and restore_expected:
            match = re.search(r"The failed update's workspace copy is kept at (.+)\. You can delete it once Astrid works\.", result.stdout)
            assert match, result.stdout
            failed_copy = Path(match.group(1))
            assert failed_copy.name.startswith("realm-1.failed-")
            assert failed_copy.is_dir()
        transaction = read_json(base / "program" / "update-transaction.json")
        assert transaction["step"] == "complete"
        assert current(base / "program").root.name.startswith("new-")
        assert read_json(base / "data/runtime/upgrade-journal.json")["state"] == "complete"
        realm = base / "data/runtime/realms/realm-1"
        assert inspect_canonical_schema(realm)["version"] == SCHEMA_VERSION
    after = (base / "events").read_text().splitlines()[len(before):]
    if restore_expected:
        assert after.index("restore") < after.index("backup")
    else:
        assert "restore" not in after


@pytest.mark.parametrize("step", [
    "prepared", "stopped", "backup-return", "backed_up", "stage-return", "staged",
    "upgraded", "pointer-publish", "switched", "registered", "started", "verified", "complete",
])
def test_sigkill_each_installer_durable_step_then_three_successful_updates(tmp_path, step):
    _setup(tmp_path)
    result = _run(tmp_path, step)
    assert result.returncode == -signal.SIGKILL, result.stderr
    _assert_retry(tmp_path, restore_expected=step not in {"prepared", "stopped", "backup-return", "complete"})


@pytest.mark.parametrize("state", [
    "planned", "stopping", "stopped", "migration-committed", "activated", "restarted", "complete",
])
def test_sigkill_actual_runtime_upgrade_then_recover_before_new_backup(tmp_path, state):
    _setup(tmp_path)
    result = _run(tmp_path, "runtime:" + state)
    assert result.returncode == -signal.SIGKILL, result.stderr
    journal = read_json(tmp_path / "data/runtime/upgrade-journal.json")
    assert journal["state"] == ("stopped" if state == "migration-committed" else state)
    transaction = read_json(tmp_path / "program/update-transaction.json")
    assert transaction["backup"]["root"]
    assert transaction["realm"]["realm_id"] == journal["realm_id"]
    if state == "migration-committed":
        assert inspect_canonical_schema(transaction["realm"]["realm_root"])["version"] == SCHEMA_VERSION
        assert inspect_canonical_schema(Path(transaction["backup"]["root"]) / "realm")["version"] == 29
    _assert_retry(tmp_path, restore_expected=True)


@pytest.mark.parametrize("point", ["restore-realm-removed", "restore-support-removed", "restore-return"])
def test_sigkill_during_recovery_replays_same_retained_backup(tmp_path, point):
    _setup(tmp_path)
    assert _run(tmp_path, "runtime:stopped").returncode == -signal.SIGKILL
    original = read_json(tmp_path / "program/update-transaction.json")
    result = _run(tmp_path, point)
    assert result.returncode == -signal.SIGKILL, result.stderr
    interrupted = read_json(tmp_path / "program/update-transaction.json")
    assert interrupted["operation_id"] == original["operation_id"]
    assert interrupted["backup"] == original["backup"]
    assert interrupted["step"] == "recovering"
    _assert_retry(tmp_path, restore_expected=True)


def test_repair_also_recovers_before_workspace_inspection(tmp_path):
    _setup(tmp_path)
    assert _run(tmp_path, "runtime:stopped").returncode == -signal.SIGKILL
    result = _run(tmp_path, mode="install")
    assert result.returncode == 0, result.stderr
    selected = current(tmp_path / "program")
    assert selected.root.name == "old"
    assert selected.document()["identity"] == read_json(selected.root / "version.json")["identity"]
    assert selected.document()["identity"] == {"release": 17, "managed_by": None}
    selection = read_json(tmp_path / "install-selection.json")
    assert selection["identity"] == selected.document()["identity"]
    assert selection["unrelated"] == "preserved"
    assert read_json(tmp_path / "program/update-transaction.json")["step"] == "rolled_back"
    assert not (tmp_path / "data/runtime/upgrade-journal.json").exists()


@pytest.mark.parametrize("point", ["restore-realm-removed", "restore-key-removed", "restore-return"])
def test_authenticated_runtime_backup_recovers_even_if_live_key_was_removed(tmp_path, point):
    _setup(tmp_path, previous_schema=False)
    (tmp_path / "authenticated").touch()
    assert _run(tmp_path, "runtime:stopped").returncode == -signal.SIGKILL
    assert read_json(tmp_path / "program/update-transaction.json")["backup"]["kind"] == "runtime"
    result = _run(tmp_path, point)
    assert result.returncode == -signal.SIGKILL, result.stderr
    if point == "restore-key-removed":
        assert not (tmp_path / "data/runtime/backup-auth.key").exists()
    _assert_retry(tmp_path, restore_expected=True)


def _runtime(base, kill_at):
    from types import SimpleNamespace

    from astrid_runtime import operator_upgrade

    paths = RuntimePaths.current(data_root=base / "data")
    realm = paths.realms_dir / "realm-1"

    def publish():
        discovery = {
            "pid": os.getpid(), "active_realm": "realm-1", "endpoint": "http://127.0.0.1:17540",
            "runtime_instance_id": "test-instance", "process_birth_id": "test-birth", "realm_root": str(realm),
        }
        write_json(paths.discovery_path, discovery)
        write_json(paths.instance_lock_path, {**discovery, "realm_id": "realm-1"})
        return discovery

    class Boundary:
        def is_pid_alive(self, _pid): return True
        def validate_owner(self, **_kwargs): return True
        def prepare_restart(self, **_kwargs): pass
        def stop_owner(self, **_kwargs): pass
        def endpoint_metadata(self, **_kwargs):
            return {"runtime_instance_id": "test-instance", "realm_id": "realm-1"}
        def health(self, **_kwargs): return True
        def connect(self, **_kwargs): return SimpleNamespace(doctor=lambda: {"ok": True})

    def start(*_args):
        discovery = publish()
        return SimpleNamespace(endpoint=discovery["endpoint"], pid=os.getpid(), runtime_instance_id="test-instance")

    journal = operator_upgrade._journal
    migrate = operator_upgrade.migrate_canonical_to_current

    def journal_and_kill(*args, **kwargs):
        result = journal(*args, **kwargs)
        _kill("runtime:" + kwargs["state"], kill_at)
        return result

    def migrate_and_kill(*args, **kwargs):
        result = migrate(*args, **kwargs)
        _kill("runtime:migration-committed", kill_at)
        return result

    publish()
    operator_upgrade._bootstrap_locked = start
    operator_upgrade._journal = journal_and_kill
    operator_upgrade.migrate_canonical_to_current = migrate_and_kill
    operator_upgrade.upgrade_workspace(paths, Boundary(), BootstrapConfig())


def _installer(base, kill_at, mode):
    from astrid.installer import backend as backend_module

    root, data = base / "program", base / "data"
    save = Installer._save_update
    publish = core.switch
    replace = backend_module.os.replace
    rmtree = backend_module.shutil.rmtree
    unlink = Path.unlink

    def save_and_kill(self, transaction, step, **details):
        save(self, transaction, step, **details)
        _kill(step, kill_at)

    def switch_and_kill(*args):
        publish(*args)
        _kill("pointer-publish", kill_at)

    def replace_and_kill(source, target, *args, **kwargs):
        replace(source, target, *args, **kwargs)
        if Path(source) == data / "runtime/realms/realm-1":
            _kill("restore-realm-removed", kill_at)

    def rmtree_and_kill(path, *args, **kwargs):
        rmtree(path, *args, **kwargs)
        if Path(path) == data / "runtime/credentials":
            _kill("restore-support-removed", kill_at)

    def unlink_and_kill(path, *args, **kwargs):
        unlink(path, *args, **kwargs)
        if path == data / "runtime/backup-auth.key":
            _kill("restore-key-removed", kill_at)

    Installer._save_update = save_and_kill
    core.switch = switch_and_kill
    backend_module.os.replace = replace_and_kill
    backend_module.shutil.rmtree = rmtree_and_kill
    Path.unlink = unlink_and_kill
    backend = _Backend(base, kill_at)
    backend.restart_required = lambda: False
    Installer(root, data, backend, say=print).run(mode, {})


if __name__ == "__main__":
    mode, base, kill_at = sys.argv[1], Path(sys.argv[2]), sys.argv[3]
    if mode == "runtime":
        _runtime(base, kill_at)
    else:
        _installer(base, kill_at, mode)
